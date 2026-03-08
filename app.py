from flask import Flask, request, jsonify, send_file
import os
import time
import math
import hmac
import hashlib
import urllib.parse
import requests
from datetime import datetime

app = Flask(__name__)

API_KEY = os.getenv("BINGX_API_KEY", "").strip()
SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "").strip()
SYMBOL = os.getenv("SYMBOL", "BTC-USDT").strip()
LEVERAGE = int(os.getenv("LEVERAGE", "3"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "100"))

BASE_URL = "https://open-api.bingx.com"
LOG_FILE = "trades_log.csv"


def now_ms():
    return int(time.time() * 1000)


def round_down(value, decimals=3):
    factor = 10 ** decimals
    return math.floor(value * factor) / factor


def sign_params(params: dict) -> str:
    params["timestamp"] = now_ms()
    sorted_params = sorted(params.items())
    query = urllib.parse.urlencode(sorted_params)
    signature = hmac.new(
        SECRET_KEY.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"{query}&signature={signature}"


def bingx_request(method, path, params=None):
    if params is None:
        params = {}

    query = sign_params(params)
    url = f"{BASE_URL}{path}?{query}"
    headers = {"X-BX-APIKEY": API_KEY}

    if method.upper() == "GET":
        response = requests.get(url, headers=headers, timeout=20)
    else:
        response = requests.post(url, headers=headers, timeout=20)

    data = response.json()
    return data


def get_price():
    data = bingx_request("GET", "/openApi/swap/v2/quote/price", {
        "symbol": SYMBOL
    })
    price = data.get("data", {}).get("price")
    if not price:
        raise Exception(f"No se pudo obtener el precio: {data}")
    return float(price)


def get_balance():
    data = bingx_request("GET", "/openApi/swap/v2/user/balance")
    balance_data = data.get("data", {})

    if isinstance(balance_data, dict):
        if "balance" in balance_data and isinstance(balance_data["balance"], dict):
            bal = balance_data["balance"].get("availableBalance") or balance_data["balance"].get("balance")
            if bal is not None:
                return float(bal)

        bal = balance_data.get("availableBalance") or balance_data.get("balance")
        if bal is not None:
            return float(bal)

    raise Exception(f"No se pudo obtener el balance: {data}")


def get_positions():
    data = bingx_request("GET", "/openApi/swap/v2/user/positions", {
        "symbol": SYMBOL
    })
    positions = data.get("data", [])
    if isinstance(positions, dict):
        positions = [positions]
    return positions


def get_current_position():
    positions = get_positions()

    for pos in positions:
        pos_symbol = str(pos.get("symbol", "")).strip()
        if pos_symbol != SYMBOL:
            continue

        amount = pos.get("positionAmt") or pos.get("positionAmount") or pos.get("availableAmt") or 0
        try:
            amount = float(amount)
        except Exception:
            amount = 0.0

        if amount == 0:
            continue

        side = str(pos.get("positionSide", "")).upper()

        if side in ["LONG", "SHORT"]:
            return side, abs(amount)

        if amount > 0:
            return "LONG", abs(amount)
        elif amount < 0:
            return "SHORT", abs(amount)

    return "NONE", 0.0


def calculate_order_quantity():
    balance = get_balance()
    price = get_price()

    margin_to_use = balance * (RISK_PERCENT / 100.0)
    notional = margin_to_use * LEVERAGE
    qty = notional / price
    qty = round_down(qty, 3)

    if qty <= 0:
        raise Exception("La cantidad calculada es 0. Revisa balance, leverage o precio.")

    return qty


def place_order(side, quantity, reduce_only=False):
    params = {
        "symbol": SYMBOL,
        "side": side.upper(),
        "positionSide": "BOTH",
        "type": "MARKET",
        "quantity": quantity,
        "reduceOnly": "true" if reduce_only else "false"
    }

    data = bingx_request("POST", "/openApi/swap/v2/trade/order", params)

    if str(data.get("code")) != "0":
        raise Exception(f"Error BingX: {data}")

    return data


def close_position(current_side, current_qty):
    if current_side == "LONG":
        return place_order("SELL", round_down(current_qty, 3), reduce_only=True)
    elif current_side == "SHORT":
        return place_order("BUY", round_down(current_qty, 3), reduce_only=True)
    return None


def open_new_position(action, qty):
    if action == "buy":
        return place_order("BUY", qty, reduce_only=False)
    elif action == "sell":
        return place_order("SELL", qty, reduce_only=False)
    else:
        raise Exception("Acción inválida para abrir posición.")


def ensure_log_file():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write("timestamp,action,message,symbol,leverage,risk_percent,result_json\n")


def append_trade_log(action, result):
    ensure_log_file()
    message = str(result.get("message", "")).replace(",", ";").replace("\n", " ")
    result_json = str(result).replace(",", ";").replace("\n", " ")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(
            f"{datetime.utcnow().isoformat()},{action},{message},{SYMBOL},{LEVERAGE},{RISK_PERCENT},{result_json}\n"
        )


def execute_flip(action):
    current_side, current_qty = get_current_position()
    new_qty = calculate_order_quantity()

    if action == "buy":
        if current_side == "LONG":
            return {"message": "Ya estás en LONG, no se abre otra posición."}

        if current_side == "SHORT":
            close_result = close_position(current_side, current_qty)
            time.sleep(1)
            open_result = open_new_position("buy", new_qty)
            return {
                "message": "SHORT cerrado y LONG abierto",
                "closed_qty": current_qty,
                "opened_qty": new_qty,
                "close_result": close_result,
                "open_result": open_result
            }

        open_result = open_new_position("buy", new_qty)
        return {
            "message": "BUY ejecutado",
            "sent_qty": new_qty,
            "result": open_result
        }

    elif action == "sell":
        if current_side == "SHORT":
            return {"message": "Ya estás en SHORT, no se abre otra posición."}

        if current_side == "LONG":
            close_result = close_position(current_side, current_qty)
            time.sleep(1)
            open_result = open_new_position("sell", new_qty)
            return {
                "message": "LONG cerrado y SHORT abierto",
                "closed_qty": current_qty,
                "opened_qty": new_qty,
                "close_result": close_result,
                "open_result": open_result
            }

        open_result = open_new_position("sell", new_qty)
        return {
            "message": "SELL ejecutado",
            "sent_qty": new_qty,
            "result": open_result
        }

    else:
        raise Exception(f"Acción inválida: {action}")


@app.route("/", methods=["GET"])
def home():
    return "BOT ACTIVO", 200


@app.route("/logs", methods=["GET"])
def download_logs():
    ensure_log_file()
    return send_file(LOG_FILE, as_attachment=True)


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    print("Señal recibida:", data, flush=True)

    action = str(data.get("action", "")).lower().strip()

    if action not in ["buy", "sell"]:
        return jsonify({
            "ok": False,
            "error": "Acción inválida",
            "received": data
        }), 400

    try:
        result = execute_flip(action)
        print("Resultado trade:", result, flush=True)

        try:
            append_trade_log(action, result)
        except Exception as log_error:
            print("Error guardando log:", log_error, flush=True)

        return jsonify({
            "ok": True,
            "result": result
        }), 200

    except Exception as e:
        print("ERROR webhook:", str(e), flush=True)
        return jsonify({
            "ok": False,
            "error": str(e),
            "received": data
        }), 500