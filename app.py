from flask import Flask, request, jsonify, send_file
from openai import OpenAI
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
LEVERAGE = int(os.getenv("LEVERAGE", "5"))

# Riesgo de respaldo
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "30"))

# Colchón de seguridad
QTY_BUFFER = float(os.getenv("QTY_BUFFER", "0.95"))

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4").strip()
AI_FILTER_ENABLED = os.getenv("AI_FILTER_ENABLED", "true").strip().lower() == "true"

# Porcentajes internos del sistema
RISK_LOW_PERCENT = 30.0
RISK_MEDIUM_PERCENT = 55.0
RISK_HIGH_PERCENT = 85.0

BASE_URL = "https://open-api.bingx.com"

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

print(
    f"BOT CONFIG -> SYMBOL={SYMBOL}, LEVERAGE={LEVERAGE}, "
    f"QTY_BUFFER={QTY_BUFFER}, AI_FILTER_ENABLED={AI_FILTER_ENABLED}, "
    f"OPENAI_MODEL={OPENAI_MODEL}",
    flush=True
)

# =========================
# Archivos locales
# =========================
TRADES_LOG_FILE = "trades_log.csv"
EVENTS_LOG_FILE = "bot_events.csv"
STATE_FILE = "position_state.json"


# =========================
# Utilidades
# =========================
def utc_now():
    return datetime.utcnow().isoformat()


def now_ms():
    return int(time.time() * 1000)


def round_down(value, decimals=3):
    factor = 10 ** decimals
    return math.floor(value * factor) / factor


def safe_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def safe_int(value, default=None):
    try:
        return int(float(value))
    except Exception:
        return default


def extract_json_from_text(text: str):
    if not text:
        return None

    cleaned = text.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(cleaned)
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = cleaned[start:end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            return None

    return None


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

    try:
        data = response.json()
    except Exception:
        raise Exception(f"Respuesta no JSON de BingX: {response.text}")

    return data


# =========================
# Logs / estado
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


def append_trade_log(opened_at, closed_at, side, qty, entry_price, exit_price, pnl_gross, close_reason, risk_percent_used):
    ensure_files()
    with open(TRADES_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            opened_at,
            closed_at,
            side,
            SYMBOL,
            LEVERAGE,
            risk_percent_used,
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
# Lectura BingX
# =========================
def get_price():
    data = bingx_request("GET", "/openApi/swap/v2/quote/price", {"symbol": SYMBOL})
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
    data = bingx_request("GET", "/openApi/swap/v2/user/positions", {"symbol": SYMBOL})
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
            return {"side": "LONG", "qty": abs(amount), "entry_price": avg_price}
        elif amount < 0:
            return {"side": "SHORT", "qty": abs(amount), "entry_price": avg_price}

    return {"side": "NONE", "qty": 0.0, "entry_price": None}


# =========================
# IA - probabilidad
# =========================
def probability_to_risk(probability: int, alignment: str):
    """
    alignment:
    - with_htf
    - against_htf
    - neutral_htf
    """

    # Neutral: no trade para mantenerlo limpio
    if alignment == "neutral_htf":
        return "REJECT", 0.0

    # Contra 15m: solo entra con 30%, pero solo si supera 50
    if alignment == "against_htf":
        if probability <= 50:
            return "REJECT", 0.0
        return "COUNTER", RISK_LOW_PERCENT

    # A favor del 15m
    if probability < 30:
        return "REJECT", 0.0
    elif 30 <= probability <= 59:
        return "LOW", RISK_LOW_PERCENT
    elif 60 <= probability <= 80:
        return "MEDIUM", RISK_MEDIUM_PERCENT
    else:
        return "HIGH", RISK_HIGH_PERCENT


def ai_filter_signal(action, payload):
    """
    La IA no crea el setup.
    Solo evalúa calidad y da probabilidad.
    """

    if not AI_FILTER_ENABLED:
        alignment = str(payload.get("alignment", "neutral_htf")).lower().strip()
        tier, risk_percent = probability_to_risk(100, alignment)
        return {
            "decision": "APPROVE" if risk_percent > 0 else "REJECT",
            "probability": 100,
            "tier": tier,
            "risk_percent": risk_percent,
            "reason": "AI filter disabled"
        }

    if client is None:
        return {
            "decision": "REJECT",
            "probability": 0,
            "tier": "REJECT",
            "risk_percent": 0.0,
            "reason": "OPENAI_API_KEY missing"
        }

    try:
        alignment = str(payload.get("alignment", "neutral_htf")).lower().strip()

        signal_context = {
            "action": str(action).upper(),
            "symbol": payload.get("symbol", "BTCUSDT"),
            "mode": payload.get("mode", ""),
            "source": payload.get("source", ""),
            "timeframe": payload.get("timeframe", "5m"),
            "htf": payload.get("htf", "15"),
            "alignment": alignment,
            "trend_15m": str(payload.get("trend_15m", "neutral")).lower().strip(),

            "close_price": safe_float(payload.get("close_price")),
            "ema13_5m": safe_float(payload.get("ema13")),
            "ema62_5m": safe_float(payload.get("ema62")),
            "ema200_5m": safe_float(payload.get("ema200")),
            "stoch_k_5m": safe_float(payload.get("stoch_k")),
            "stoch_d_5m": safe_float(payload.get("stoch_d")),
            "atr_5m": safe_float(payload.get("atr")),
            "atr_pct_5m": safe_float(payload.get("atr_pct")),

            "close_15m": safe_float(payload.get("close_15m")),
            "ema13_15m": safe_float(payload.get("ema13_15m")),
            "ema62_15m": safe_float(payload.get("ema62_15m")),
            "ema200_15m": safe_float(payload.get("ema200_15m")),
            "stoch_k_15m": safe_float(payload.get("stoch_k_15m")),
            "stoch_d_15m": safe_float(payload.get("stoch_d_15m")),
            "atr_15m": safe_float(payload.get("atr_15m")),
            "atr_pct_15m": safe_float(payload.get("atr_pct_15m")),

            "utc_time": utc_now()
        }

        system_prompt = """
You are a conservative BTCUSDT signal quality evaluator.

The trading setup already exists.
Your job is NOT to invent trades.
Your job is ONLY to score the quality of the existing setup from 0 to 100.

Important rules:
- Main trigger is 5m.
- 15m is the structural context.
- The setup already passed the indicator logic.
- You should evaluate:
  1) ATR / expansion quality
  2) stochastic timing quality
  3) trend structure quality
  4) whether the signal is clean or noisy
- If the setup is weak, choppy, overextended, or low-quality, return a low score.
- Return ONLY valid JSON.

Return exactly:
{
  "decision": "APPROVE" or "REJECT",
  "probability": 0,
  "reason": "short explanation"
}

Probability interpretation:
- 0-29: reject
- 30-59: weak/acceptable
- 60-80: medium/good
- 81-100: strong
"""

        user_prompt = f"Evaluate this BTCUSDT signal setup:\n{json.dumps(signal_context, ensure_ascii=False)}"

        response = client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        raw_text = response.output_text.strip()
        parsed = extract_json_from_text(raw_text)

        if not parsed:
            return {
                "decision": "REJECT",
                "probability": 0,
                "tier": "REJECT",
                "risk_percent": 0.0,
                "reason": f"Invalid AI output: {raw_text[:300]}"
            }

        decision = str(parsed.get("decision", "REJECT")).upper().strip()
        probability = safe_int(parsed.get("probability", 0), 0)
        reason = str(parsed.get("reason", "")).strip()

        probability = max(0, min(100, probability))

        tier, risk_percent = probability_to_risk(probability, alignment)

        if decision != "APPROVE" or risk_percent <= 0:
            return {
                "decision": "REJECT",
                "probability": probability,
                "tier": "REJECT",
                "risk_percent": 0.0,
                "reason": reason or "Probability below execution threshold"
            }

        return {
            "decision": "APPROVE",
            "probability": probability,
            "tier": tier,
            "risk_percent": risk_percent,
            "reason": reason
        }

    except Exception as e:
        return {
            "decision": "REJECT",
            "probability": 0,
            "tier": "REJECT",
            "risk_percent": 0.0,
            "reason": f"AI filter error: {str(e)}"
        }


# =========================
# Cálculo de orden
# =========================
def calculate_order_quantity(risk_percent_override=None):
    balance = get_balance()
    price = get_price()

    selected_risk_percent = risk_percent_override if risk_percent_override is not None else RISK_PERCENT

    margin_to_use = balance * (selected_risk_percent / 100.0)
    notional = margin_to_use * LEVERAGE
    qty = notional / price

    qty = qty * QTY_BUFFER
    qty = round_down(qty, 3)

    print(
        f"DEBUG QTY -> balance={balance}, price={price}, "
        f"risk_percent={selected_risk_percent}, margin_to_use={margin_to_use}, "
        f"notional={notional}, qty_buffered={qty}",
        flush=True
    )

    if qty <= 0:
        raise Exception("La cantidad calculada es 0. Revisa balance, leverage o precio.")

    return qty, selected_risk_percent


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
# Sincronización
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
# Apertura / cierre
# =========================
def execute_open(action, risk_percent_override=None):
    state, current = sync_state_with_exchange()
    current_side = current["side"]

    # No abrir más de una posición del mismo lado
    if action == "buy" and current_side == "LONG":
        return {"message": "Ya estás en LONG, no se abre otra posición."}

    if action == "sell" and current_side == "SHORT":
        return {"message": "Ya estás en SHORT, no se abre otra posición."}

    # No abrir si hay posición opuesta todavía abierta
    if action == "buy" and current_side == "SHORT":
        return {"message": "Hay SHORT abierto. Para BUY primero debe llegar señal contraria de cierre."}

    if action == "sell" and current_side == "LONG":
        return {"message": "Hay LONG abierto. Para SELL primero debe llegar señal contraria de cierre."}

    new_qty, selected_risk_percent = calculate_order_quantity(risk_percent_override)

    open_result = open_new_position(action, new_qty)
    open_price, open_qty = extract_order_data(open_result)
    open_qty = open_qty if open_qty is not None else new_qty

    new_state = {
        "side": "LONG" if action == "buy" else "SHORT",
        "qty": open_qty,
        "entry_price": open_price,
        "opened_at": utc_now(),
        "symbol": SYMBOL,
        "leverage": LEVERAGE,
        "risk_percent": selected_risk_percent
    }
    save_state(new_state)

    return {
        "message": "BUY ejecutado" if action == "buy" else "SELL ejecutado",
        "risk_percent_used": selected_risk_percent,
        "sent_qty": open_qty,
        "opened_entry_price": open_price,
        "result": open_result
    }


def execute_close_by_opposite_signal(action):
    state, current = sync_state_with_exchange()
    current_side = current["side"]
    current_qty = current["qty"]

    # BUY señal contraria cierra SHORT
    if action == "buy" and current_side == "SHORT":
        close_result = close_position(current_side, current_qty)
        close_price, closed_qty = extract_order_data(close_result)
        closed_qty = closed_qty if closed_qty is not None else current_qty

        entry_price = state.get("entry_price") if state else None
        opened_at = state.get("opened_at") if state else ""
        prev_risk_percent = state.get("risk_percent", RISK_PERCENT) if state else RISK_PERCENT
        pnl_gross = calc_gross_pnl("SHORT", closed_qty, entry_price, close_price)

        append_trade_log(
            opened_at=opened_at,
            closed_at=utc_now(),
            side="SHORT",
            qty=closed_qty,
            entry_price=entry_price,
            exit_price=close_price,
            pnl_gross=pnl_gross,
            close_reason="opposite_buy_signal_close_only",
            risk_percent_used=prev_risk_percent
        )

        clear_state()

        return {
            "message": "SHORT cerrado por señal BUY contraria",
            "closed_qty": closed_qty,
            "closed_entry_price": entry_price,
            "closed_exit_price": close_price,
            "closed_pnl_gross": pnl_gross,
            "close_result": close_result
        }

    # SELL señal contraria cierra LONG
    if action == "sell" and current_side == "LONG":
        close_result = close_position(current_side, current_qty)
        close_price, closed_qty = extract_order_data(close_result)
        closed_qty = closed_qty if closed_qty is not None else current_qty

        entry_price = state.get("entry_price") if state else None
        opened_at = state.get("opened_at") if state else ""
        prev_risk_percent = state.get("risk_percent", RISK_PERCENT) if state else RISK_PERCENT
        pnl_gross = calc_gross_pnl("LONG", closed_qty, entry_price, close_price)

        append_trade_log(
            opened_at=opened_at,
            closed_at=utc_now(),
            side="LONG",
            qty=closed_qty,
            entry_price=entry_price,
            exit_price=close_price,
            pnl_gross=pnl_gross,
            close_reason="opposite_sell_signal_close_only",
            risk_percent_used=prev_risk_percent
        )

        clear_state()

        return {
            "message": "LONG cerrado por señal SELL contraria",
            "closed_qty": closed_qty,
            "closed_entry_price": entry_price,
            "closed_exit_price": close_price,
            "closed_pnl_gross": pnl_gross,
            "close_result": close_result
        }

    return None


# =========================
# Rutas Flask
# =========================
@app.route("/", methods=["GET"])
def home():
    return "BOT V2 ACTIVO - IA SOLO PARA PROBABILIDAD/TAMAÑO", 200


@app.route("/logs", methods=["GET"])
def download_trade_logs():
    ensure_files()
    return send_file(TRADES_LOG_FILE, as_attachment=True)


@app.route("/events", methods=["GET"])
def download_event_logs():
    ensure_files()
    return send_file(EVENTS_LOG_FILE, as_attachment=True)


@app.route("/state", methods=["GET"])
def get_state():
    state = load_state()
    current = get_current_position_info()
    return jsonify({
        "saved_state": state,
        "exchange_position": current
    }), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    print("Señal recibida:", data, flush=True)

    action_raw = str(data.get("action", "")).upper().strip()
    action = "buy" if action_raw == "BUY" else "sell" if action_raw == "SELL" else ""

    if action not in ["buy", "sell"]:
        append_event_log(action_raw, "Acción inválida", {"received": data})
        return jsonify({
            "ok": False,
            "error": "Acción inválida",
            "received": data
        }), 400

    try:
        state, current = sync_state_with_exchange()
        current_side = current["side"]

        # =========================
        # 1) Si ya hay posición opuesta, señal contraria = cerrar
        # =========================
        close_result = execute_close_by_opposite_signal(action)
        if close_result is not None:
            print("CIERRE POR SEÑAL CONTRARIA ->", close_result, flush=True)
            append_event_log(action, close_result.get("message", "Cierre por señal contraria"), close_result)
            return jsonify({
                "ok": True,
                "result": close_result
            }), 200

        # =========================
        # 2) Si no hay posición abierta del lado contrario, evaluar entrada con IA
        # =========================
        ai_result = ai_filter_signal(action, data)

        print(
            f"AI RESULT -> decision={ai_result.get('decision')}, "
            f"probability={ai_result.get('probability')}, "
            f"tier={ai_result.get('tier')}, "
            f"risk_percent={ai_result.get('risk_percent')}, "
            f"reason={ai_result.get('reason')}",
            flush=True
        )

        append_event_log(action, "AI filter evaluated", ai_result)

        if ai_result["decision"] != "APPROVE":
            print("TRADE BLOQUEADO -> probabilidad insuficiente", flush=True)
            return jsonify({
                "ok": True,
                "filtered": True,
                "message": "Trade bloqueado por probabilidad insuficiente",
                "ai_result": ai_result,
                "received": data
            }), 200

        result = execute_open(
            action,
            risk_percent_override=ai_result["risk_percent"]
        )
        result["ai_result"] = ai_result

        print("Resultado trade:", result, flush=True)
        append_event_log(action, result.get("message", "Trade ejecutado"), result)

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