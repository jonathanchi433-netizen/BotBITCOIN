from flask import Flask, request, jsonify
import os
import time
import math
import hmac
import hashlib
import urllib.parse
import requests

app = Flask(__name__)

# =========================
# Variables de entorno
# =========================
API_KEY = os.getenv("BINGX_API_KEY", "").strip()
SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "").strip()
SYMBOL = os.getenv("SYMBOL", "BTCUSDT").strip().upper()
LEVERAGE = int(os.getenv("LEVERAGE", "3"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "5"))
QTY_DECIMALS = int(os.getenv("QTY_DECIMALS", "4"))

BASE_URL = "https://open-api.bingx.com"


# =========================
# Utilidades
# =========================
def now_ms() -> int:
    return int(time.time() * 1000)


def round_down(value: float, decimals: int) -> float:
    factor = 10 ** decimals
    return math.floor(value * factor) / factor


def sign_params(params: dict) -> str:
    # Ordenamos para una firma estable
    items = sorted((k, str(v)) for k, v in params.items())
    query = urllib.parse.urlencode(items)
    signature = hmac.new(
        SECRET_KEY.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"{query}&signature={signature}"


def bingx_request(method: str, path: str, params: dict | None = None):
    if not API_KEY or not SECRET_KEY:
        raise RuntimeError("Faltan BINGX_API_KEY o BINGX_SECRET_KEY en Railway.")

    params = params.copy() if params else {}
    params["timestamp"] = now_ms()

    signed_query = sign_params(params)
    url = f"{BASE_URL}{path}?{signed_query}"

    headers = {
        "X-BX-APIKEY": API_KEY
    }

    if method.upper() == "GET":
        resp = requests.get(url, headers=headers, timeout=20)
    elif method.upper() == "POST":
        resp = requests.post(url, headers=headers, timeout=20)
    else:
        raise ValueError(f"Método no soportado: {method}")

    resp.raise_for_status()
    data = resp.json()

    # BingX normalmente devuelve code == 0 cuando sale bien
    if isinstance(data, dict) and str(data.get("code")) not in {"0", "None"}:
        raise RuntimeError(f"Error BingX: {data}")

    return data


# =========================
# Endpoints BingX
# =========================
def get_last_price(symbol: str) -> float:
    data = bingx_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    payload = data.get("data", {})
    price = payload.get("price") or payload.get("lastPrice") or payload.get("close")
    if price is None:
        raise RuntimeError(f"No pude leer el precio: {data}")
    return float(price)


def get_available_balance_usdt() -> float:
    data = bingx_request("GET", "/openApi/swap/v2/user/balance")
    payload = data.get("data", {})

    # Respuesta defensiva: a veces viene dict, a veces lista
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        # Algunos formatos envuelven balance dentro de data.balance
        if isinstance(payload.get("balance"), dict):
            bal = payload["balance"]
            for key in ("availableBalance", "balance", "equity"):
                if bal.get(key) is not None:
                    return float(bal[key])
        items = [payload]
    else:
        items = []

    for item in items:
        asset = str(item.get("asset", "USDT")).upper()
        if asset == "USDT":
            for key in ("availableBalance", "balance", "equity"):
                if item.get(key) is not None:
                    return float(item[key])

    raise RuntimeError(f"No encontré balance USDT en la respuesta: {data}")


def get_current_position(symbol: str):
    """
    Devuelve:
      ("LONG", qty) o ("SHORT", qty) o ("NONE", 0.0)
    """
    data = bingx_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
    payload = data.get("data", [])

    if isinstance(payload, dict):
        items = [payload]
    else:
        items = payload

    for item in items:
        if str(item.get("symbol", "")).upper() != symbol.upper():
            continue

        amt_raw = (
            item.get("positionAmt")
            or item.get("positionAmount")
            or item.get("availableAmt")
            or item.get("qty")
            or 0
        )

        try:
            amt = float(amt_raw)
        except Exception:
            amt = 0.0

        if abs(amt) == 0:
            continue

        side_raw = str(
            item.get("positionSide")
            or item.get("side")
            or ""
        ).upper()

        if side_raw in {"LONG", "SHORT"}:
            return side_raw, abs(amt)

        # Fallback por signo
        if amt > 0:
            return "LONG", abs(amt)
        if amt < 0:
            return "SHORT", abs(amt)

    return "NONE", 0.0


def place_market_order(symbol: str, side: str, quantity: float):
    params = {
        "symbol": symbol,
        "side": side.upper(),   # BUY o SELL
        "type": "MARKET",
        "quantity": f"{quantity:.{QTY_DECIMALS}f}",
    }
    return bingx_request("POST", "/openApi/swap/v2/trade/order", params)


# =========================
# Lógica de tamaño y flip
# =========================
def calc_target_qty(balance_usdt: float, price: float) -> float:
    """
    Usa 5% del balance como margen
    y multiplica por leverage para el notional.
    """
    margin_usdt = balance_usdt * (RISK_PERCENT / 100.0)
    notional_usdt = margin_usdt * LEVERAGE
    qty = notional_usdt / price
    qty = round_down(qty, QTY_DECIMALS)
    if qty <= 0:
        raise RuntimeError("La cantidad calculada salió 0. Revisa balance o QTY_DECIMALS.")
    return qty


def execute_flip(action: str, symbol: str):
    action = action.lower().strip()
    if action not in {"buy", "sell"}:
        raise RuntimeError(f"Acción inválida: {action}")

    balance = get_available_balance_usdt()
    price = get_last_price(symbol)
    target_qty = calc_target_qty(balance, price)

    current_side, current_qty = get_current_position(symbol)

    # buy -> si ya estás long, no hace nada
    # buy -> si estabas short, manda qty = short actual + nuevo target
    if action == "buy":
        if current_side == "LONG":
            return {
                "ok": True,
                "message": "Ya existe LONG. No hago nada.",
                "current_side": current_side,
                "current_qty": current_qty,
                "target_qty": target_qty,
            }

        qty_to_send = target_qty + current_qty if current_side == "SHORT" else target_qty
        result = place_market_order(symbol, "BUY", qty_to_send)
        return {
            "ok": True,
            "message": "BUY ejecutado",
            "qty_sent": qty_to_send,
            "price_ref": price,
            "balance_ref": balance,
            "bingx": result,
        }

    # sell -> si ya estás short, no hace nada
    # sell -> si estabas long, manda qty = long actual + nuevo target
    if action == "sell":
        if current_side == "SHORT":
            return {
                "ok": True,
                "message": "Ya existe SHORT. No hago nada.",
                "current_side": current_side,
                "current_qty": current_qty,
                "target_qty": target_qty,
            }

        qty_to_send = target_qty + current_qty if current_side == "LONG" else target_qty
        result = place_market_order(symbol, "SELL", qty_to_send)
        return {
            "ok": True,
            "message": "SELL ejecutado",
            "qty_sent": qty_to_send,
            "price_ref": price,
            "balance_ref": balance,
            "bingx": result,
        }

    raise RuntimeError("Acción no manejada.")


# =========================
# Rutas Flask
# =========================
@app.route("/", methods=["GET"])
def home():
    return "BOT ACTIVO", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    print("Señal recibida:", data, flush=True)

    action = str(data.get("action", "")).lower().strip()
    symbol = str(data.get("symbol", SYMBOL)).upper().strip()

    # modo prueba opcional
    if str(data.get("test", "")).lower() == "true":
        return jsonify({
            "ok": True,
            "mode": "test",
            "received": data
        }), 200

    try:
        result = execute_flip(action, symbol)
        print("Resultado trade:", result, flush=True)
        return jsonify(result), 200
    except Exception as e:
        print("ERROR webhook:", str(e), flush=True)
        return jsonify({
            "ok": False,
            "error": str(e),
            "received": data
        }), 500
