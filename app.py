from flask import Flask, request
import os
import time
import requests
import hmac
import hashlib

app = Flask(__name__)

# Variables de entorno (Railway)
API_KEY = os.getenv("CLAVE DE API DE BINGX")
SECRET_KEY = os.getenv("CLAVE SECRETA DE BINGX")
SYMBOL = os.getenv("SÍMBOLO")
LEVERAGE = int(os.getenv("Aprovechar"))
RISK_PERCENT = float(os.getenv("PORCENTAJE DE RIESGO"))

BASE_URL = "https://open-api.bingx.com"


def sign(params):

    query = "&".join([f"{k}={v}" for k,v in params.items()])

    signature = hmac.new(
        SECRET_KEY.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()

    return query + "&signature=" + signature


def get_balance():

    params = {
        "timestamp": int(time.time() * 1000)
    }

    query = sign(params)

    headers = {
        "X-BX-APIKEY": API_KEY
    }

    r = requests.get(
        BASE_URL + "/openApi/swap/v2/user/balance?" + query,
        headers=headers
    )

    data = r.json()

    balance = float(data["data"]["balance"]["balance"])

    return balance


def open_position(side):

    balance = get_balance()

    position_size = balance * (RISK_PERCENT / 100)

    params = {
        "symbol": SYMBOL,
        "side": side,
        "type": "MARKET",
        "quantity": position_size,
        "timestamp": int(time.time() * 1000)
    }

    query = sign(params)

    headers = {
        "X-BX-APIKEY": API_KEY
    }

    requests.post(
        BASE_URL + "/openApi/swap/v2/trade/order?" + query,
        headers=headers
    )


@app.route("/")
def home():
    return "BOT ACTIVO"


@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.json

    if data["action"] == "buy":

        print("Señal BUY recibida")

        open_position("BUY")

    elif data["action"] == "sell":

        print("Señal SELL recibida")

        open_position("SELL")

    return {"status": "ok"}
