from flask import Flask, request, jsonify
import os
import time
import math
import hmac
import hashlib
import urllib.parse
import requests

app = Flask(__name__)

API_KEY = os.getenv("BINGX_API_KEY", "").strip()
SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "").strip()
SYMBOL = os.getenv("SYMBOL", "BTC-USDT").strip()
LEVERAGE = int(os.getenv("LEVERAGE", "3"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "5"))

BASE_URL = "https://open-api.bingx.com"


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

    headers = {
        "X-BX-APIKEY": API_KEY
    }

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
        except:
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


def place_order(side, quantity):
    params = {
        "symbol": SYMBOL,
        "side": side.upper(),           # BUY o SELL
        "positionSide": "BOTH",         # IMPORTANTE para modo unidireccional
        "type": "MARKET",
        "quantity": quantity
    }

    data = bingx_request("POST", "/openApi/swap/v2/trade/order", params)

    if str(data.get("code")) != "0":
        raise Exception(f"Error BingX: {data}")

    return data


def execute_flip(action):
    current_side, current_qty = get_current_position()
    new_qty = calculate_order_quantity()

    if action == "buy":
        if current_side == "LONG":
            return {"message": "Ya estás en LONG, no se abre otra posición."}

        if current_side == "SHORT":
            total_qty = round_down(current_qty + new_qty, 3)
        else:
            total_qty = new_qty

        result = place_order("BUY", total_qty)
        return {
            "message": "BUY ejecutado",
            "sent_qty": total_qty,
            "result": result
        }

    elif action == "sell":
        if current_side == "SHORT":
            return {"message": "Ya estás en SHORT, no se abre otra posición."}

        if current_side == "LONG":
            total_qty = round_down(current_qty + new_qty, 3)
        else:
            total_qty = new_qty

        result = place_order("SELL", total_qty)
        return {
            "message": "SELL ejecutado",
            "sent_qty": total_qty,
            "result": result
        }

    else:
        raise Exception(f"Acción inválida: {action}")


@app.route("/", methods=["GET"])
def home():
    return "BOT ACTIVO", 200


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
