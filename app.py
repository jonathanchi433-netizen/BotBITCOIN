from flask import Flask, request, jsonify, send_file
import os
import time
import math
import hmac
import hashlib
import urllib.parse
import requests
import csv
import json
from datetime import datetime

app = Flask(__name__)

# =========================
# Variables de entorno
# =========================
API_KEY = os.getenv("BINGX_API_KEY", "").strip()
SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "").strip()
SYMBOL = os.getenv("SYMBOL", "BTC-USDT").strip()
LEVERAGE = int(os.getenv("LEVERAGE", "3"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "100"))

# Colchón de seguridad para evitar insufficient margin
# 0.95 = usa 95% de la cantidad calculada
QTY_BUFFER = float(os.getenv("QTY_BUFFER", "0.95"))

BASE_URL = "https://open-api.bingx.com"

print(f"BOT CONFIG -> SYMBOL={SYMBOL}, LEVERAGE={LEVERAGE}, RISK_PERCENT={RISK_PERCENT}, QTY_BUFFER={QTY_BUFFER}", flush=True)

# =========================
# Archivos locales
# =========================
TRADES_LOG_FILE = "trades_log.csv"
EVENTS_LOG_FILE = "bot_events.csv"
STATE_FILE = "position_state.json"


# =========================
# Utilidades generales
# =========================
def utc_now():
    return datetime.utcnow().isoformat()


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


# =========================
# Archivos de log / estado
# =========================
def ensure_files():
    if not os.path.exists(TRADES_LOG_FILE):
        with open(TRADES_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "opened_at",
                "closed_at",
                "side",
                "symbol",
                "leverage",
                "risk_percent",
                "qty",
                "entry_price",
                "exit_price",
                "pnl_gross",
                "close_reason"
            ])

    if not os.path.exists(EVENTS_LOG_FILE):
        with open(EVENTS_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp",
                "action",
                "symbol",
                "message",
                "details"
            ])


def append_event_log(action, message, details):
    ensure_files()
    with open(EVENTS_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            utc_now(),
            action,
            SYMBOL,
            message,
            json.dumps(details, ensure_ascii=False)
        ])


def append_trade_log(opened_at, closed_at, side, qty, entry_price, exit_price, pnl_gross, close_reason):
    ensure_files()
    with open(TRADES_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            opened_at,
            closed_at,
            side,
            SYMBOL,
            LEVERAGE,
            RISK_PERCENT,
            qty,
            entry_price,
            exit_price,
            pnl_gross,
            close_reason
        ])


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def clear_state():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)


# =========================
# Lectura de datos BingX
# =========================
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


def get_current_position_info():
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
        avg_price_raw = (
            pos.get("avgPrice")
            or pos.get("averagePrice")
            or pos.get("positionAvgPrice")
            or pos.get("avgOpenPrice")
        )

        avg_price = None
        try:
            if avg_price_raw is not None:
                avg_price = float(avg_price_raw)
        except Exception:
            avg_price = None

        if side in ["LONG", "SHORT"]:
            return {
                "side": side,
                "qty": abs(amount),
                "entry_price": avg_price
            }

        if amount > 0:
            return {
                "side": "LONG",
                "qty": abs(amount),
                "entry_price": avg_price
            }
        elif amount < 0:
            return {
                "side": "SHORT",
                "qty": abs(amount),
                "entry_price": avg_price
            }

    return {
        "side": "NONE",
        "qty": 0.0,
        "entry_price": None
    }


# =========================
# Cálculo de orden
# =========================
def calculate_order_quantity():
    balance = get_balance()
    price = get_price()

    margin_to_use = balance * (RISK_PERCENT / 100.0)
    notional = margin_to_use * LEVERAGE
    qty = notional / price

    # Colchón de seguridad para evitar insufficient margin
    qty = qty * QTY_BUFFER

    qty = round_down(qty, 3)

    print(
        f"DEBUG QTY -> balance={balance}, price={price}, margin_to_use={margin_to_use}, "
        f"notional={notional}, qty_buffered={qty}",
        flush=True
    )

    if qty <= 0:
        raise Exception("La cantidad calculada es 0. Revisa balance, leverage o precio.")

    return qty


def extract_order_data(order_response):
    order = order_response.get("data", {}).get("order", {})
    avg_price_raw = order.get("avgPrice")
    executed_qty_raw = order.get("executedQty") or order.get("quantity")

    avg_price = None
    executed_qty = None

    try:
        if avg_price_raw is not None:
            avg_price = float(avg_price_raw)
    except Exception:
        avg_price = None

    try:
        if executed_qty_raw is not None:
            executed_qty = float(executed_qty_raw)
    except Exception:
        executed_qty = None

    return avg_price, executed_qty


def place_order(side, quantity, reduce_only=False):
    params = {
        "symbol": SYMBOL,
        "side": side.upper(),
        "positionSide": "BOTH",
        "type": "MARKET",
        "quantity": quantity,
        "reduceOnly": "true" if reduce_only else "false"
    }

    print(f"ENVIANDO ORDEN -> side={side}, quantity={quantity}, reduce_only={reduce_only}", flush=True)

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


def calc_gross_pnl(side, qty, entry_price, exit_price):
    if entry_price is None or exit_price is None:
        return None

    if side == "LONG":
        return round((exit_price - entry_price) * qty, 6)
    elif side == "SHORT":
        return round((entry_price - exit_price) * qty, 6)

    return None


# =========================
# Sincronización de estado
# =========================
def sync_state_with_exchange():
    state = load_state()
    current = get_current_position_info()

    if current["side"] == "NONE":
        if state is not None:
            clear_state()
        return None, current

    if state is None:
        inferred_state = {
            "side": current["side"],
            "qty": current["qty"],
            "entry_price": current["entry_price"],
            "opened_at": utc_now(),
            "symbol": SYMBOL,
            "leverage": LEVERAGE,
            "risk_percent": RISK_PERCENT
        }
        save_state(inferred_state)
        state = inferred_state

    return state, current


# =========================
# Lógica principal de flip
# =========================
def execute_flip(action):
    state, current = sync_state_with_exchange()

    current_side = current["side"]
    current_qty = current["qty"]

    new_qty = calculate_order_quantity()

    # ===================== BUY =====================
    if action == "buy":
        if current_side == "LONG":
            return {"message": "Ya estás en LONG, no se abre otra posición."}

        if current_side == "SHORT":
            close_result = close_position(current_side, current_qty)
            close_price, closed_qty = extract_order_data(close_result)
            closed_qty = closed_qty if closed_qty is not None else current_qty

            entry_price = state.get("entry_price") if state else None
            opened_at = state.get("opened_at") if state else ""
            pnl_gross = calc_gross_pnl("SHORT", closed_qty, entry_price, close_price)

            append_trade_log(
                opened_at=opened_at,
                closed_at=utc_now(),
                side="SHORT",
                qty=closed_qty,
                entry_price=entry_price,
                exit_price=close_price,
                pnl_gross=pnl_gross,
                close_reason="flip_to_long"
            )

            time.sleep(1)

            open_result = open_new_position("buy", new_qty)
            open_price, open_qty = extract_order_data(open_result)
            open_qty = open_qty if open_qty is not None else new_qty

            new_state = {
                "side": "LONG",
                "qty": open_qty,
                "entry_price": open_price,
                "opened_at": utc_now(),
                "symbol": SYMBOL,
                "leverage": LEVERAGE,
                "risk_percent": RISK_PERCENT
            }
            save_state(new_state)

            return {
                "message": "SHORT cerrado y LONG abierto",
                "closed_qty": closed_qty,
                "closed_entry_price": entry_price,
                "closed_exit_price": close_price,
                "closed_pnl_gross": pnl_gross,
                "opened_qty": open_qty,
                "opened_entry_price": open_price,
                "close_result": close_result,
                "open_result": open_result
            }

        open_result = open_new_position("buy", new_qty)
        open_price, open_qty = extract_order_data(open_result)
        open_qty = open_qty if open_qty is not None else new_qty

        new_state = {
            "side": "LONG",
            "qty": open_qty,
            "entry_price": open_price,
            "opened_at": utc_now(),
            "symbol": SYMBOL,
            "leverage": LEVERAGE,
            "risk_percent": RISK_PERCENT
        }
        save_state(new_state)

        return {
            "message": "BUY ejecutado",
            "sent_qty": open_qty,
            "opened_entry_price": open_price,
            "result": open_result
        }

    # ===================== SELL =====================
    elif action == "sell":
        if current_side == "SHORT":
            return {"message": "Ya estás en SHORT, no se abre otra posición."}

        if current_side == "LONG":
            close_result = close_position(current_side, current_qty)
            close_price, closed_qty = extract_order_data(close_result)
            closed_qty = closed_qty if closed_qty is not None else current_qty

            entry_price = state.get("entry_price") if state else None
            opened_at = state.get("opened_at") if state else ""
            pnl_gross = calc_gross_pnl("LONG", closed_qty, entry_price, close_price)

            append_trade_log(
                opened_at=opened_at,
                closed_at=utc_now(),
                side="LONG",
                qty=closed_qty,
                entry_price=entry_price,
                exit_price=close_price,
                pnl_gross=pnl_gross,
                close_reason="flip_to_short"
            )

            time.sleep(1)

            open_result = open_new_position("sell", new_qty)
            open_price, open_qty = extract_order_data(open_result)
            open_qty = open_qty if open_qty is not None else new_qty

            new_state = {
                "side": "SHORT",
                "qty": open_qty,
                "entry_price": open_price,
                "opened_at": utc_now(),
                "symbol": SYMBOL,
                "leverage": LEVERAGE,
                "risk_percent": RISK_PERCENT
            }
            save_state(new_state)

            return {
                "message": "LONG cerrado y SHORT abierto",
                "closed_qty": closed_qty,
                "closed_entry_price": entry_price,
                "closed_exit_price": close_price,
                "closed_pnl_gross": pnl_gross,
                "opened_qty": open_qty,
                "opened_entry_price": open_price,
                "close_result": close_result,
                "open_result": open_result
            }

        open_result = open_new_position("sell", new_qty)
        open_price, open_qty = extract_order_data(open_result)
        open_qty = open_qty if open_qty is not None else new_qty

        new_state = {
            "side": "SHORT",
            "qty": open_qty,
            "entry_price": open_price,
            "opened_at": utc_now(),
            "symbol": SYMBOL,
            "leverage": LEVERAGE,
            "risk_percent": RISK_PERCENT
        }
        save_state(new_state)

        return {
            "message": "SELL ejecutado",
            "sent_qty": open_qty,
            "opened_entry_price": open_price,
            "result": open_result
        }

    else:
        raise Exception(f"Acción inválida: {action}")


# =========================
# Cierre sin invertir
# =========================
def execute_close_only(action):
    state, current = sync_state_with_exchange()

    current_side = current["side"]
    current_qty = current["qty"]

    if action == "close_long":
        if current_side != "LONG":
            return {"message": "No hay LONG abierto para cerrar."}

        close_result = close_position(current_side, current_qty)
        close_price, closed_qty = extract_order_data(close_result)
        closed_qty = closed_qty if closed_qty is not None else current_qty

        entry_price = state.get("entry_price") if state else None
        opened_at = state.get("opened_at") if state else ""
        pnl_gross = calc_gross_pnl("LONG", closed_qty, entry_price, close_price)

        append_trade_log(
            opened_at=opened_at,
            closed_at=utc_now(),
            side="LONG",
            qty=closed_qty,
            entry_price=entry_price,
            exit_price=close_price,
            pnl_gross=pnl_gross,
            close_reason="close_long_only"
        )

        clear_state()

        return {
            "message": "LONG cerrado sin abrir SHORT",
            "closed_qty": closed_qty,
            "closed_entry_price": entry_price,
            "closed_exit_price": close_price,
            "closed_pnl_gross": pnl_gross,
            "close_result": close_result
        }

    elif action == "close_short":
        if current_side != "SHORT":
            return {"message": "No hay SHORT abierto para cerrar."}

        close_result = close_position(current_side, current_qty)
        close_price, closed_qty = extract_order_data(close_result)
        closed_qty = closed_qty if closed_qty is not None else current_qty

        entry_price = state.get("entry_price") if state else None
        opened_at = state.get("opened_at") if state else ""
        pnl_gross = calc_gross_pnl("SHORT", closed_qty, entry_price, close_price)

        append_trade_log(
            opened_at=opened_at,
            closed_at=utc_now(),
            side="SHORT",
            qty=closed_qty,
            entry_price=entry_price,
            exit_price=close_price,
            pnl_gross=pnl_gross,
            close_reason="close_short_only"
        )

        clear_state()

        return {
            "message": "SHORT cerrado sin abrir LONG",
            "closed_qty": closed_qty,
            "closed_entry_price": entry_price,
            "closed_exit_price": close_price,
            "closed_pnl_gross": pnl_gross,
            "close_result": close_result
        }

    else:
        raise Exception(f"Acción inválida para cierre: {action}")


# =========================
# Rutas Flask
# =========================
@app.route("/", methods=["GET"])
def home():
    return "BOT ACTIVO", 200


@app.route("/logs", methods=["GET"])
def download_trade_logs():
    ensure_files()
    return send_file(TRADES_LOG_FILE, as_attachment=True)


@app.route("/events", methods=["GET"])
def download_event_logs():
    ensure_files()
    return send_file(EVENTS_LOG_FILE, as_attachment=True)


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    print("Señal recibida:", data, flush=True)

    action = str(data.get("action", "")).lower().strip()

    valid_actions = ["buy", "sell", "close_long", "close_short"]

    if action not in valid_actions:
        append_event_log(action, "Acción inválida", {"received": data})
        return jsonify({
            "ok": False,
            "error": "Acción inválida",
            "received": data
        }), 400

    try:
        if action in ["buy", "sell"]:
            result = execute_flip(action)
        else:
            result = execute_close_only(action)

        print("Resultado trade:", result, flush=True)

        try:
            append_event_log(action, result.get("message", "Trade ejecutado"), result)
        except Exception as log_error:
            print("Error guardando event log:", log_error, flush=True)

        return jsonify({
            "ok": True,
            "result": result
        }), 200

    except Exception as e:
        print("ERROR webhook:", str(e), flush=True)

        try:
            append_event_log(action, f"ERROR webhook: {str(e)}", {"received": data})
        except Exception as log_error:
            print("Error guardando event log:", log_error, flush=True)

        return jsonify({
            "ok": False,
            "error": str(e),
            "received": data
        }), 500


if __name__ == "__main__":
    ensure_files()
    app.run(host="0.0.0.0", port=10000)