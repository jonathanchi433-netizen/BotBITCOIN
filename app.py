import csv
import hashlib
import hmac
import io
import json
import logging
import math
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, redirect, render_template_string, request, send_file


# ============================================================
# CONFIGURACIÓN
# ============================================================

BOT_NAME = "BOT BTC BINGX 5M"
BINGX_BASE_URL = "https://open-api.bingx.com"

BINGX_SYMBOL = os.getenv("BINGX_SYMBOL", "BTC-USDT").strip().upper()

TV_SYMBOLS = {
    item.strip().upper()
    for item in os.getenv(
        "TV_SYMBOLS",
        "BTC-USDT,BTCUSDT,BTCUSDT.P,BINGX:BTCUSDT.P",
    ).split(",")
    if item.strip()
}

BINGX_API_KEY = os.getenv("BINGX_API_KEY", "").strip()
BINGX_API_SECRET = (
    os.getenv("BINGX_API_SECRET", "").strip()
    or os.getenv("BINGX_SECRET_KEY", "").strip()
)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
CONTROL_SECRET = os.getenv("CONTROL_SECRET", WEBHOOK_SECRET).strip()
MONITOR_SECRET = os.getenv("MONITOR_SECRET", "").strip()

BALANCE_PERCENT = float(os.getenv("BALANCE_PERCENT", "90"))
LEVERAGE = int(os.getenv("LEVERAGE", "2"))
FEE_RATE = float(os.getenv("FEE_RATE", "0.0005"))

QTY_STEP_FALLBACK = float(os.getenv("QTY_STEP", "0.0001"))
MIN_QTY_FALLBACK = float(os.getenv("MIN_QTY", "0.0001"))

POSITION_MODE = os.getenv("POSITION_MODE", "HEDGE").strip().upper()

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").strip()
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
STATE_PREFIX = os.getenv("STATE_PREFIX", "bot_btc_5m").strip()
DATA_DIR = os.getenv("DATA_DIR", ".").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

VALID_MODES = ["OFF", "LONG_ONLY", "SHORT_ONLY", "CLOSE_ONLY", "BOTH"]

SIGNAL_LOCK = threading.RLock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(BOT_NAME)

app = Flask(__name__)


# ============================================================
# UTILIDADES
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def fnum(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def fint(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def parse_time(value):
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)

    try:
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt

    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def secret_matches(received, expected):
    return (
        bool(received and expected)
        and hmac.compare_digest(
            str(received),
            str(expected),
        )
    )


def notify(message):
    if not (
        TELEGRAM_BOT_TOKEN
        and TELEGRAM_CHAT_ID
    ):
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": str(message)[:3900],
            },
            timeout=10,
        )

    except Exception:
        pass


def floor_step(value, step):
    if step <= 0:
        return value

    decimals = max(
        0,
        len(
            f"{step:.12f}"
            .rstrip("0")
            .split(".")[-1]
        ),
    )

    result = (
        math.floor(
            (float(value) + 1e-12)
            / step
        )
        * step
    )

    return round(
        result,
        decimals,
    )


def redact_error(message):
    text = str(message)

    for secret in (
        BINGX_API_KEY,
        BINGX_API_SECRET,
        WEBHOOK_SECRET,
        CONTROL_SECRET,
    ):
        if secret:
            text = text.replace(
                secret,
                "***",
            )

    return text


# ============================================================
# ALMACENAMIENTO
# ============================================================

class Store:

    def __init__(self):
        os.makedirs(
            DATA_DIR,
            exist_ok=True,
        )

        self.lock = threading.RLock()

    @property
    def redis_enabled(self):
        return bool(
            UPSTASH_REDIS_REST_URL
            and UPSTASH_REDIS_REST_TOKEN
        )

    def _key(self, name):
        return f"{STATE_PREFIX}:{name}"

    def _path(self, name):
        return os.path.join(
            DATA_DIR,
            f"{STATE_PREFIX}_{name}.json",
        )

    def _redis(self, command):
        response = requests.post(
            UPSTASH_REDIS_REST_URL.rstrip("/"),
            headers={
                "Authorization":
                f"Bearer {UPSTASH_REDIS_REST_TOKEN}",

                "Content-Type":
                "application/json",
            },
            json=command,
            timeout=12,
        )

        payload = response.json()

        if (
            response.status_code >= 400
            or payload.get("error")
        ):
            raise RuntimeError(
                f"Error Upstash: {payload}"
            )

        return payload.get("result")

    def get(self, name, default):
        with self.lock:

            if self.redis_enabled:

                raw = self._redis(
                    [
                        "GET",
                        self._key(name),
                    ]
                )

                if not raw:
                    return default

                return json.loads(raw)

            path = self._path(name)

            if not os.path.exists(path):
                return default

            with open(
                path,
                "r",
                encoding="utf-8",
            ) as handle:
                return json.load(handle)

    def set(self, name, value):
        with self.lock:

            raw = json.dumps(
                value,
                ensure_ascii=False,
            )

            if self.redis_enabled:

                self._redis(
                    [
                        "SET",
                        self._key(name),
                        raw,
                    ]
                )

                return

            with open(
                self._path(name),
                "w",
                encoding="utf-8",
            ) as handle:

                json.dump(
                    value,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )

    def get_mode(self):
        state = self.get(
            "mode",
            {},
        )

        mode = state.get(
            "mode",
            "OFF",
        )

        if mode in VALID_MODES:
            return mode

        return "OFF"

    def set_mode(self, mode):
        if mode not in VALID_MODES:
            raise ValueError(
                "Modo inválido"
            )

        self.set(
            "mode",
            {
                "mode": mode,
                "updated_at": utc_now(),
            },
        )

    def get_active_trade(self):
        return self.get(
            "active_trade",
            None,
        )

    def set_active_trade(self, trade):
        self.set(
            "active_trade",
            trade,
        )

    def clear_active_trade(self):
        self.set(
            "active_trade",
            None,
        )

    def get_trades(self):
        trades = self.get(
            "trades",
            [],
        )

        if isinstance(
            trades,
            list,
        ):
            return trades

        return []

    def set_trades(self, trades):
        self.set(
            "trades",
            list(trades),
        )

    def append_trade(self, trade):
        with self.lock:

            trades = self.get_trades()

            trades.append(trade)

            self.set_trades(trades)

    def get_events(self):
        events = self.get(
            "events",
            [],
        )

        if isinstance(
            events,
            list,
        ):
            return events

        return []

    def add_event(
        self,
        kind,
        detail,
    ):
        with self.lock:

            events = self.get_events()

            events.append(
                {
                    "time":
                    utc_now(),

                    "kind":
                    str(kind),

                    "detail":
                    redact_error(detail),
                }
            )

            self.set(
                "events",
                events[-100:],
            )


store = Store()


# ============================================================
# BINGX
# ============================================================

class BingX:

    def _request(
        self,
        method,
        path,
        params=None,
        private=True,
    ):
        params = dict(
            params or {}
        )

        headers = {}

        if private:

            if not (
                BINGX_API_KEY
                and BINGX_API_SECRET
            ):
                raise RuntimeError(
                    "Las API de BingX "
                    "no están configuradas"
                )

            params["timestamp"] = int(
                time.time() * 1000
            )

            params.setdefault(
                "recvWindow",
                5000,
            )

            query = urlencode(
                sorted(
                    params.items()
                )
            )

            signature = hmac.new(
                BINGX_API_SECRET.encode(),
                query.encode(),
                hashlib.sha256,
            ).hexdigest()

            url = (
                f"{BINGX_BASE_URL}"
                f"{path}"
                f"?{query}"
                f"&signature={signature}"
            )

            headers[
                "X-BX-APIKEY"
            ] = BINGX_API_KEY

        else:

            query = urlencode(
                params
            )

            url = (
                f"{BINGX_BASE_URL}"
                f"{path}"
            )

            if query:
                url += f"?{query}"

        response = requests.request(
            method,
            url,
            headers=headers,
            timeout=20,
        )

        try:
            payload = response.json()

        except Exception:
            raise RuntimeError(
                "BingX devolvió respuesta "
                "no JSON: "
                f"{response.status_code} "
                f"{response.text[:300]}"
            )

        if (
            response.status_code >= 400
            or str(
                payload.get("code")
            )
            != "0"
        ):
            raise RuntimeError(
                f"Error BingX: {payload}"
            )

        return payload

    # ========================================================
    # PRECIO
    # ========================================================

    def price(self):

        payload = self._request(
            "GET",
            "/openApi/swap/v2/quote/price",
            {
                "symbol":
                BINGX_SYMBOL,
            },
            private=False,
        )

        data = payload.get(
            "data",
            {},
        )

        if isinstance(
            data,
            list,
        ):
            data = (
                data[0]
                if data
                else {}
            )

        price = fnum(
            data.get("price")
            or data.get("lastPrice"),
            0,
        )

        if price <= 0:
            raise RuntimeError(
                "No se pudo obtener "
                "el precio de BTC"
            )

        return price

    # ========================================================
    # BALANCE
    # ========================================================

    def available_balance(self):
        """
        BingX v3:
        data.balance.availableMargin
        """

        payload = self._request(
            "GET",
            "/openApi/swap/v3/user/balance",
        )

        data = payload.get(
            "data",
            {},
        )

        candidates = []

        if isinstance(
            data,
            dict,
        ):

            nested = data.get(
                "balance"
            )

            if isinstance(
                nested,
                dict,
            ):
                candidates.append(
                    nested
                )

            elif isinstance(
                nested,
                list,
            ):
                candidates.extend(
                    x
                    for x in nested
                    if isinstance(
                        x,
                        dict,
                    )
                )

            balances = data.get(
                "balances"
            )

            if isinstance(
                balances,
                list,
            ):
                candidates.extend(
                    x
                    for x in balances
                    if isinstance(
                        x,
                        dict,
                    )
                )

            candidates.append(data)

        elif isinstance(
            data,
            list,
        ):

            candidates.extend(
                x
                for x in data
                if isinstance(
                    x,
                    dict,
                )
            )

        for item in candidates:

            asset = str(
                item.get("asset")
                or item.get("currency")
                or item.get("coin")
                or ""
            ).upper()

            if (
                asset
                and asset != "USDT"
            ):
                continue

            for key in (
                "availableMargin",
                "availableBalance",
                "available",
                "maxWithdrawAmount",
                "balance",
            ):

                if key not in item:
                    continue

                value = item.get(key)

                if isinstance(
                    value,
                    (dict, list),
                ):
                    continue

                available = fnum(
                    value,
                    -1,
                )

                if available >= 0:
                    return available

        raise RuntimeError(
            "No se pudo leer el "
            "balance USDT disponible. "
            "BingX respondió con una "
            "estructura no reconocida."
        )

    # ========================================================
    # REGLAS CONTRATO
    # ========================================================

    def contract_rules(self):

        step = QTY_STEP_FALLBACK
        minimum = MIN_QTY_FALLBACK

        try:

            payload = self._request(
                "GET",
                "/openApi/swap/v2/quote/contracts",
                private=False,
            )

            data = payload.get(
                "data",
                [],
            )

            if isinstance(
                data,
                dict,
            ):

                data = (
                    data.get("contracts")
                    or data.get("data")
                    or [data]
                )

            for item in (
                data or []
            ):

                if (
                    str(
                        item.get(
                            "symbol",
                            "",
                        )
                    ).upper()
                    != BINGX_SYMBOL
                ):
                    continue

                precision = item.get(
                    "quantityPrecision"
                )

                if precision is not None:

                    step = (
                        10
                        ** (
                            -int(
                                precision
                            )
                        )
                    )

                minimum = fnum(
                    item.get(
                        "tradeMinQuantity"
                    )
                    or item.get(
                        "minQty"
                    )
                    or item.get(
                        "minQuantity"
                    ),
                    minimum,
                )

                break

        except Exception as exc:

            logger.warning(
                "No se pudieron leer "
                "reglas del contrato: %s",
                exc,
            )

        return (
            step,
            minimum,
        )

    # ========================================================
    # POSICIONES
    # ========================================================

    def positions(self):

        payload = self._request(
            "GET",
            "/openApi/swap/v2/user/positions",
            {
                "symbol":
                BINGX_SYMBOL,
            },
        )

        data = payload.get(
            "data",
            [],
        )

        if isinstance(
            data,
            dict,
        ):
            data = [data]

        result = []

        for item in (
            data or []
        ):

            if not isinstance(
                item,
                dict,
            ):
                continue

            symbol = str(
                item.get(
                    "symbol",
                    "",
                )
            ).upper()

            if (
                symbol
                and symbol != BINGX_SYMBOL
            ):
                continue

            amount = fnum(
                item.get(
                    "positionAmt"
                )
                or item.get(
                    "positionAmount"
                )
                or item.get(
                    "availableAmt"
                )
                or item.get(
                    "quantity"
                ),
                0,
            )

            if abs(amount) <= 0:
                continue

            side = str(
                item.get(
                    "positionSide",
                    "",
                )
            ).upper()

            if side not in {
                "LONG",
                "SHORT",
            }:

                side = (
                    "LONG"
                    if amount > 0
                    else "SHORT"
                )

            quantity = abs(amount)

            entry_price = fnum(
                item.get(
                    "avgPrice"
                )
                or item.get(
                    "entryPrice"
                )
                or item.get(
                    "averagePrice"
                ),
                0,
            )

            leverage = fint(
                item.get(
                    "leverage"
                ),
                LEVERAGE,
            )

            margin = fnum(
                item.get(
                    "initialMargin"
                )
                or item.get(
                    "isolatedMargin"
                )
                or item.get(
                    "margin"
                ),
                0,
            )

            if (
                margin <= 0
                and entry_price > 0
                and leverage > 0
            ):
                margin = (
                    entry_price
                    * quantity
                    / leverage
                )

            result.append(
                {
                    "side":
                    side,

                    "quantity":
                    quantity,

                    "entry_price":
                    entry_price,

                    "leverage":
                    leverage,

                    "margin_used":
                    margin,

                    "unrealized_pnl":
                    fnum(
                        item.get(
                            "unrealizedProfit"
                        )
                        or item.get(
                            "unrealizedPnl"
                        )
                        or item.get(
                            "unRealizedProfit"
                        ),
                        0,
                    ),

                    "liquidation_price":
                    fnum(
                        item.get(
                            "liquidationPrice"
                        ),
                        0,
                    ),

                    "position_id":
                    item.get(
                        "positionId"
                    ),
                }
            )

        return result

    # ========================================================
    # MODO DE POSICIÓN
    # ========================================================

    def position_mode_is_hedge(
        self,
    ):

        payload = self._request(
            "GET",
            "/openApi/swap/v1/positionSide/dual",
        )

        data = payload.get(
            "data",
            {},
        )

        raw = (
            data.get(
                "dualSidePosition"
            )
            if isinstance(
                data,
                dict,
            )
            else data
        )

        if isinstance(
            raw,
            bool,
        ):
            return raw

        return (
            str(raw)
            .strip()
            .lower()
            == "true"
        )

    def ensure_position_mode(
        self,
    ):

        if POSITION_MODE != "HEDGE":
            return

        if self.position_mode_is_hedge():
            return

        self._request(
            "POST",
            "/openApi/swap/v1/positionSide/dual",
            {
                "dualSidePosition":
                "true",
            },
        )

    # ========================================================
    # MARGEN
    # ========================================================

    def margin_type(self):

        payload = self._request(
            "GET",
            "/openApi/swap/v2/trade/marginType",
            {
                "symbol":
                BINGX_SYMBOL,
            },
        )

        data = payload.get(
            "data",
            {},
        )

        if not isinstance(
            data,
            dict,
        ):
            data = {}

        return str(
            data.get(
                "marginType",
                "",
            )
        ).upper()

    def set_isolated(self):

        current = self.margin_type()

        if current == "ISOLATED":
            return "ISOLATED"

        self._request(
            "POST",
            "/openApi/swap/v2/trade/marginType",
            {
                "symbol":
                BINGX_SYMBOL,

                "marginType":
                "ISOLATED",
            },
        )

        return "ISOLATED"

    # ========================================================
    # APALANCAMIENTO
    # ========================================================

    def set_leverage(self):

        sides = (
            ("LONG", "SHORT")
            if POSITION_MODE == "HEDGE"
            else ("BOTH",)
        )

        for side in sides:

            self._request(
                "POST",
                "/openApi/swap/v2/trade/leverage",
                {
                    "symbol":
                    BINGX_SYMBOL,

                    "side":
                    side,

                    "leverage":
                    LEVERAGE,
                },
            )

    # ========================================================
    # CANTIDAD
    # ========================================================

    def quantity(self):

        balance = (
            self.available_balance()
        )

        price = (
            self.price()
        )

        margin = (
            balance
            * BALANCE_PERCENT
            / 100
        )

        (
            step,
            minimum,
        ) = self.contract_rules()

        quantity = floor_step(
            (
                margin
                * LEVERAGE
                / price
            ),
            step,
        )

        if quantity < minimum:

            raise RuntimeError(
                "Cantidad calculada "
                f"{quantity} "
                "menor que mínimo "
                f"{minimum}. "
                "Balance disponible: "
                f"{balance:.4f} USDT"
            )

        return {
            "balance":
            balance,

            "price":
            price,

            "margin":
            margin,

            "quantity":
            quantity,

            "step":
            step,

            "minimum":
            minimum,
        }

    def _position_side(
        self,
        direction,
    ):

        if POSITION_MODE == "HEDGE":
            return direction

        return "BOTH"

    # ========================================================
    # ORDEN MARKET
    # ========================================================

    def market_order(
        self,
        order_side,
        direction,
        quantity,
        closing=False,
    ):

        reference_price = (
            self.price()
        )

        (
            step,
            _,
        ) = self.contract_rules()

        quantity = floor_step(
            quantity,
            step,
        )

        if quantity <= 0:
            raise RuntimeError(
                "La cantidad de la "
                "orden quedó en 0"
            )

        params = {
            "symbol":
            BINGX_SYMBOL,

            "side":
            order_side,

            "positionSide":
            self._position_side(
                direction
            ),

            "type":
            "MARKET",

            "quantity":
            quantity,
        }

        if POSITION_MODE != "HEDGE":

            params[
                "reduceOnly"
            ] = (
                "true"
                if closing
                else "false"
            )

        payload = self._request(
            "POST",
            "/openApi/swap/v2/trade/order",
            params,
        )

        data = payload.get(
            "data",
            {},
        )

        if (
            isinstance(
                data,
                dict,
            )
            and isinstance(
                data.get("order"),
                dict,
            )
        ):
            data = data["order"]

        order_id = None
        price = reference_price
        executed = quantity

        if isinstance(
            data,
            dict,
        ):

            order_id = (
                data.get("orderId")
                or data.get("orderID")
            )

            price = fnum(
                data.get("avgPrice")
                or data.get("price"),
                reference_price,
            )

            if price <= 0:
                price = reference_price

            executed = fnum(
                data.get("executedQty")
                or data.get("quantity"),
                quantity,
            )

        return {
            "order_id":
            order_id,

            "price":
            price,

            "quantity":
            executed,
        }


bingx = BingX()


# ============================================================
# HISTORIAL
# ============================================================

def normalize_trade(raw):

    raw = dict(
        raw or {}
    )

    side = str(
        raw.get("side")
        or raw.get("direction")
        or ""
    ).upper()

    if side in {
        "BUY",
        "L",
    }:
        side = "LONG"

    elif side in {
        "SELL",
        "S",
    }:
        side = "SHORT"

    if side not in {
        "LONG",
        "SHORT",
    }:
        raise ValueError(
            "La operación necesita "
            "side LONG o SHORT"
        )

    entry = fnum(
        raw.get(
            "entry_price",
            raw.get("entry"),
        ),
        0,
    )

    exit_price = fnum(
        raw.get(
            "exit_price",
            raw.get("exit"),
        ),
        0,
    )

    quantity = fnum(
        raw.get(
            "quantity",
            raw.get("qty"),
        ),
        0,
    )

    leverage = fnum(
        raw.get(
            "leverage",
            LEVERAGE,
        ),
        LEVERAGE,
    )

    margin = fnum(
        raw.get(
            "margin_used",
            raw.get("margin"),
        ),
        0,
    )

    risk = fnum(
        raw.get(
            "risk_usd",
            raw.get("risk"),
        ),
        0,
    )

    fees = fnum(
        raw.get(
            "total_fees",
            raw.get("fees"),
        ),
        0,
    )

    funding = fnum(
        raw.get("funding"),
        0,
    )

    if (
        quantity <= 0
        and margin > 0
        and entry > 0
        and leverage > 0
    ):
        quantity = (
            margin
            * leverage
            / entry
        )

    sign = (
        1
        if side == "LONG"
        else -1
    )

    calculated_gross = 0

    if (
        entry > 0
        and exit_price > 0
        and quantity > 0
    ):

        calculated_gross = (
            (
                exit_price
                - entry
            )
            * quantity
            * sign
        )

    gross = raw.get(
        "gross_pnl"
    )

    if gross is None:
        gross = calculated_gross

    gross = fnum(
        gross,
        calculated_gross,
    )

    net = raw.get(
        "net_pnl"
    )

    if net is None:

        net = (
            gross
            - fees
            - funding
        )

    net = fnum(
        net,
        gross - fees - funding,
    )

    move_pct = 0

    if (
        entry > 0
        and exit_price > 0
    ):

        move_pct = (
            (
                exit_price
                - entry
            )
            / entry
            * 100
            * sign
        )

    r_multiple = 0

    if risk > 0:
        r_multiple = (
            net
            / risk
        )

    return {
        **raw,

        "id":
        str(
            raw.get("id")
            or uuid.uuid4()
        ),

        "opened_at":
        raw.get("opened_at")
        or utc_now(),

        "closed_at":
        raw.get("closed_at")
        or utc_now(),

        "side":
        side,

        "symbol":
        raw.get("symbol")
        or BINGX_SYMBOL,

        "entry_price":
        entry,

        "exit_price":
        exit_price,

        "quantity":
        quantity,

        "leverage":
        leverage,

        "margin_used":
        margin,

        "risk_usd":
        risk,

        "gross_pnl":
        gross,

        "total_fees":
        fees,

        "funding":
        funding,

        "net_pnl":
        net,

        "price_move_pct":
        move_pct,

        "r_multiple":
        r_multiple,

        "source":
        raw.get(
            "source",
            "AUTO",
        ),

        "close_reason":
        raw.get(
            "close_reason",
            "",
        ),
    }


def trade_summary(trades):

    ordered = sorted(
        trades,
        key=lambda x:
        x.get(
            "closed_at",
            "",
        ),
    )

    wins = [
        t
        for t in ordered
        if fnum(
            t.get("net_pnl"),
            0,
        )
        > 0.01
    ]

    losses = [
        t
        for t in ordered
        if fnum(
            t.get("net_pnl"),
            0,
        )
        < -0.01
    ]

    be = [
        t
        for t in ordered
        if abs(
            fnum(
                t.get("net_pnl"),
                0,
            )
        )
        <= 0.01
    ]

    net = sum(
        fnum(
            t.get("net_pnl"),
            0,
        )
        for t in ordered
    )

    gross = sum(
        fnum(
            t.get("gross_pnl"),
            0,
        )
        for t in ordered
    )

    fees = sum(
        fnum(
            t.get("total_fees"),
            0,
        )
        for t in ordered
    )

    funding = sum(
        fnum(
            t.get("funding"),
            0,
        )
        for t in ordered
    )

    win_sum = sum(
        fnum(
            t.get("net_pnl"),
            0,
        )
        for t in wins
    )

    loss_sum = abs(
        sum(
            fnum(
                t.get("net_pnl"),
                0,
            )
            for t in losses
        )
    )

    if loss_sum > 0:

        pf = (
            win_sum
            / loss_sum
        )

    elif win_sum > 0:

        pf = 999

    else:

        pf = 0

    running = 0
    peak = 0
    max_dd = 0

    for t in ordered:

        running += fnum(
            t.get("net_pnl"),
            0,
        )

        peak = max(
            peak,
            running,
        )

        max_dd = max(
            max_dd,
            peak - running,
        )

    r_global = 0

    for t in ordered:

        risk = fnum(
            t.get("risk_usd"),
            0,
        )

        if risk > 0:

            r_global += (
                fnum(
                    t.get("net_pnl"),
                    0,
                )
                / risk
            )

    return {
        "count":
        len(ordered),

        "wins":
        len(wins),

        "losses":
        len(losses),

        "be":
        len(be),

        "winrate":
        (
            len(wins)
            / len(ordered)
            * 100
        )
        if ordered
        else 0,

        "net":
        net,

        "gross":
        gross,

        "fees":
        fees,

        "funding":
        funding,

        "pf":
        pf,

        "max_dd":
        max_dd,

        "r_global":
        r_global,
    }


def csv_bytes(trades):

    output = io.StringIO()

    fields = [
        "id",
        "opened_at",
        "closed_at",
        "side",
        "symbol",
        "entry_price",
        "exit_price",
        "quantity",
        "leverage",
        "margin_used",
        "gross_pnl",
        "total_fees",
        "funding",
        "net_pnl",
        "risk_usd",
        "r_multiple",
        "source",
        "close_reason",
    ]

    writer = csv.DictWriter(
        output,
        fieldnames=fields,
        extrasaction="ignore",
    )

    writer.writeheader()

    for trade in trades:
        writer.writerow(trade)

    return output.getvalue().encode(
        "utf-8"
    )


# ============================================================
# SINCRONIZACIÓN
# ============================================================

def sync_position():

    positions = (
        bingx.positions()
    )

    stored = (
        store.get_active_trade()
    )

    if len(positions) > 1:

        return {
            "status":
            "warning",

            "reason":
            "multiple_positions",

            "positions":
            positions,
        }

    if not positions:

        if stored:
            store.clear_active_trade()

        return {
            "status":
            "flat",

            "position":
            None,
        }

    live = positions[0]

    if not stored:

        balance = 0

        try:
            balance = (
                bingx.available_balance()
            )
        except Exception:
            pass

        stored = {
            "id":
            str(
                uuid.uuid4()
            ),

            "opened_at":
            utc_now(),

            "side":
            live["side"],

            "symbol":
            BINGX_SYMBOL,

            "quantity":
            live["quantity"],

            "entry_price":
            live["entry_price"],

            "leverage":
            live["leverage"],

            "balance_before":
            balance,

            "margin_used":
            live["margin_used"],

            "entry_fee":
            (
                live["entry_price"]
                * live["quantity"]
                * FEE_RATE
            ),

            "open_order_id":
            None,

            "source":
            "MANUAL_SYNC",
        }

        store.set_active_trade(
            stored
        )

        notify(
            f"{BOT_NAME}: "
            "posición manual detectada "
            f"y adoptada: {live['side']}"
        )

    else:

        stored["side"] = (
            live["side"]
        )

        stored["quantity"] = (
            live["quantity"]
        )

        stored["entry_price"] = (
            live["entry_price"]
        )

        stored["leverage"] = (
            live["leverage"]
        )

        stored["margin_used"] = (
            live["margin_used"]
        )

        store.set_active_trade(
            stored
        )

    return {
        "status":
        "synced",

        "position":
        {
            **stored,
            **live,
        },
    }


# ============================================================
# APERTURA
# ============================================================

def open_trade(direction):

    sync = sync_position()

    if sync.get(
        "position"
    ):

        return {
            "status":
            "skipped",

            "reason":
            "position_already_open",
        }

    if (
        sync.get("reason")
        == "multiple_positions"
    ):

        return {
            "status":
            "blocked",

            "reason":
            "multiple_positions",
        }

    calculation = (
        bingx.quantity()
    )

    bingx.ensure_position_mode()
    bingx.set_isolated()
    bingx.set_leverage()

    order_side = (
        "BUY"
        if direction == "LONG"
        else "SELL"
    )

    fill = (
        bingx.market_order(
            order_side,
            direction,
            calculation[
                "quantity"
            ],
            closing=False,
        )
    )

    quantity = (
        fill["quantity"]
    )

    entry_price = (
        fill["price"]
    )

    time.sleep(0.45)

    try:

        live_positions = (
            bingx.positions()
        )

        matching = [
            p
            for p in live_positions
            if p.get("side")
            == direction
        ]

        if matching:

            live = matching[0]

            quantity = (
                live.get("quantity")
                or quantity
            )

            entry_price = (
                live.get("entry_price")
                or entry_price
            )

    except Exception:
        pass

    entry_fee = (
        entry_price
        * quantity
        * FEE_RATE
    )

    trade = {
        "id":
        str(
            uuid.uuid4()
        ),

        "opened_at":
        utc_now(),

        "side":
        direction,

        "symbol":
        BINGX_SYMBOL,

        "quantity":
        quantity,

        "entry_price":
        entry_price,

        "leverage":
        LEVERAGE,

        "balance_before":
        calculation[
            "balance"
        ],

        "margin_used":
        calculation[
            "margin"
        ],

        "entry_fee":
        entry_fee,

        "open_order_id":
        fill.get(
            "order_id"
        ),

        "source":
        "AUTO",
    }

    store.set_active_trade(
        trade
    )

    store.add_event(
        "ORDER_OPENED",
        (
            f"{direction} "
            f"qty={quantity} "
            f"price={entry_price} "
            "margin≈"
            f"{calculation['margin']:.4f}"
        ),
    )

    notify(
        f"APERTURA {direction} "
        f"{BOT_NAME}\n"
        f"Precio: {entry_price}\n"
        f"Cantidad: {quantity}\n"
        "Margen aprox.: "
        f"{calculation['margin']:.2f} "
        "USDT\n"
        f"Apalancamiento: "
        f"{LEVERAGE}x\n"
        "Margen: ISOLATED"
    )

    return {
        "status":
        "opened",

        "trade":
        trade,
    }


# ============================================================
# CIERRE
# ============================================================

def close_trade(reason):

    sync = sync_position()

    trade = (
        store.get_active_trade()
    )

    live = sync.get(
        "position"
    )

    if (
        not trade
        or not live
    ):

        return {
            "status":
            "skipped",

            "reason":
            "no_position_to_close",
        }

    direction = (
        live["side"]
    )

    order_side = (
        "SELL"
        if direction == "LONG"
        else "BUY"
    )

    fill = (
        bingx.market_order(
            order_side,
            direction,
            live[
                "quantity"
            ],
            closing=True,
        )
    )

    exit_price = (
        fill["price"]
    )

    quantity = min(
        fnum(
            fill["quantity"],
            live["quantity"],
        ),
        live["quantity"],
    )

    entry_price = fnum(
        live.get(
            "entry_price"
        )
        or trade.get(
            "entry_price"
        ),
        0,
    )

    sign = (
        1
        if direction == "LONG"
        else -1
    )

    gross = (
        (
            exit_price
            - entry_price
        )
        * quantity
        * sign
    )

    entry_fee = fnum(
        trade.get(
            "entry_fee"
        ),
        (
            entry_price
            * quantity
            * FEE_RATE
        ),
    )

    exit_fee = (
        exit_price
        * quantity
        * FEE_RATE
    )

    total_fees = (
        entry_fee
        + exit_fee
    )

    net = (
        gross
        - total_fees
    )

    margin = fnum(
        live.get(
            "margin_used"
        )
        or trade.get(
            "margin_used"
        ),
        0,
    )

    closed = normalize_trade(
        {
            **trade,

            "closed_at":
            utc_now(),

            "side":
            direction,

            "symbol":
            BINGX_SYMBOL,

            "quantity":
            quantity,

            "entry_price":
            entry_price,

            "exit_price":
            exit_price,

            "leverage":
            live["leverage"],

            "margin_used":
            margin,

            "gross_pnl":
            gross,

            "total_fees":
            total_fees,

            "funding":
            0,

            "net_pnl":
            net,

            "close_reason":
            reason,

            "close_order_id":
            fill.get(
                "order_id"
            ),

            "source":
            trade.get(
                "source",
                "AUTO",
            ),
        }
    )

    store.append_trade(
        closed
    )

    store.clear_active_trade()

    store.add_event(
        "ORDER_CLOSED",
        (
            f"{direction} "
            f"qty={quantity} "
            f"price={exit_price} "
            f"net≈{net:.4f} "
            f"reason={reason}"
        ),
    )

    notify(
        f"CIERRE {direction} "
        f"{BOT_NAME}\n"
        "PnL neto aprox.: "
        f"{net:.2f} USDT\n"
        f"Motivo: {reason}"
    )

    return {
        "status":
        "closed",

        "trade":
        closed,
    }


# ============================================================
# SEÑALES
# ============================================================

def process_signal(side):

    with SIGNAL_LOCK:

        mode = (
            store.get_mode()
        )

        sync = (
            sync_position()
        )

        if (
            sync.get("reason")
            == "multiple_positions"
        ):

            return {
                "status":
                "blocked",

                "reason":
                "multiple_positions",
            }

        trade = (
            store.get_active_trade()
        )

        current_side = (
            trade.get("side")
            if trade
            else None
        )

        if mode == "OFF":

            return {
                "status":
                "ignored",

                "reason":
                "mode_off",

                "mode":
                mode,
            }

        result = {
            "status":
            "processed",

            "mode":
            mode,

            "closed":
            None,

            "opened":
            None,
        }

        # Señal contraria:
        # cierra pero NO revierte
        # en la misma alarma.

        if (
            side == "BUY"
            and current_side
            == "SHORT"
        ):

            result[
                "closed"
            ] = close_trade(
                "opposite_buy_signal_5m"
            )

            return result

        if (
            side == "SELL"
            and current_side
            == "LONG"
        ):

            result[
                "closed"
            ] = close_trade(
                "opposite_sell_signal_5m"
            )

            return result

        if mode == "CLOSE_ONLY":
            return result

        if (
            side == "BUY"
            and mode in {
                "LONG_ONLY",
                "BOTH",
            }
            and current_side is None
        ):

            result[
                "opened"
            ] = open_trade(
                "LONG"
            )

        elif (
            side == "SELL"
            and mode in {
                "SHORT_ONLY",
                "BOTH",
            }
            and current_side is None
        ):

            result[
                "opened"
            ] = open_trade(
                "SHORT"
            )

        return result


# ============================================================
# PANEL
# ============================================================

PANEL_HTML = r"""
<!doctype html>
<html lang="es">

<head>

<meta charset="utf-8">

<meta
name="viewport"
content="width=device-width,initial-scale=1"
>

<title>
BOT BTC BINGX
</title>

<style>

:root{
--bg:#05070a;
--card:#12161c;
--line:#29313c;
--text:#f5f7fa;
--muted:#99a3b1;
--green:#00df87;
--red:#ff4963;
--blue:#357df6;
--gold:#c99a08;
}

*{
box-sizing:border-box
}

body{
margin:0;
background:var(--bg);
color:var(--text);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif
}

main{
max-width:920px;
margin:auto;
padding:24px
}

h1{
font-size:42px;
margin:18px 0 8px
}

h2{
font-size:30px
}

.center{
text-align:center
}

.muted{
color:var(--muted)
}

.card{
background:var(--card);
border:1px solid var(--line);
border-radius:28px;
padding:28px;
margin:22px 0
}

.mode{
font-size:54px;
font-weight:900;
color:var(--green);
text-align:center
}

.grid{
display:grid;
grid-template-columns:1fr 1fr;
gap:14px
}

.btn,
button{
display:block;
border:0;
border-radius:20px;
padding:22px 16px;
color:#fff;
text-decoration:none;
font-size:22px;
font-weight:800;
text-align:center;
cursor:pointer;
width:100%
}

.off{
background:#68717f
}

.long{
background:#07934f
}

.short{
background:#bf233e
}

.close{
background:#2769bd
}

.both{
background:#bd9009
}

.blue{
background:var(--blue)
}

.dark{
background:#303640
}

.position{
border-color:#0d9b63
}

.big{
font-size:28px;
font-weight:800
}

.stats{
display:grid;
grid-template-columns:1fr 1fr;
gap:12px
}

.stat{
background:#0d1015;
border:1px solid var(--line);
border-radius:20px;
padding:18px
}

.stat .n{
font-size:28px;
font-weight:800
}

.good{
color:var(--green)
}

.bad{
color:var(--red)
}

.row{
display:grid;
grid-template-columns:repeat(2,1fr);
gap:12px
}

input,
select{
width:100%;
padding:16px;
border-radius:14px;
border:1px solid var(--line);
background:#0b0e12;
color:#fff;
font-size:16px
}

table{
width:100%;
border-collapse:collapse;
font-size:14px
}

th,
td{
padding:10px;
border-bottom:1px solid var(--line);
text-align:left
}

.tablewrap{
overflow:auto
}

.event{
background:#0b0e12;
border:1px solid var(--line);
border-radius:14px;
padding:12px;
margin:8px 0;
font-family:ui-monospace,monospace;
font-size:13px
}

.warn{
color:#ffd166;
font-weight:700
}

@media(max-width:600px){

main{
padding:14px
}

h1{
font-size:34px
}

.mode{
font-size:46px
}

.row{
grid-template-columns:1fr
}

}

</style>

</head>

<body>

<main>

<div class="center">

<h1>
₿ BOT BTC BINGX
</h1>

<div class="muted">

{{ symbol }}
· 5M
· {{ leverage }}x
· ISOLATED
· {{ '%.1f'|format(balance_percent) }}% del balance
· REAL

</div>

</div>


<section class="card">

<div class="center muted">
MODO ACTUAL
</div>

<div class="mode">
{{ mode }}
</div>

</section>


<section class="grid">

<form
method="post"
action="/setmode/OFF?secret={{ secret }}"
>
<button class="off">
OFF
</button>
</form>


<form
method="post"
action="/setmode/LONG_ONLY?secret={{ secret }}"
>
<button class="long">
SOLO LONG
</button>
</form>


<form
method="post"
action="/setmode/SHORT_ONLY?secret={{ secret }}"
>
<button class="short">
SOLO SHORT
</button>
</form>


<form
method="post"
action="/setmode/CLOSE_ONLY?secret={{ secret }}"
>
<button class="close">
SOLO CERRAR
</button>
</form>


<form
method="post"
action="/setmode/BOTH?secret={{ secret }}"
>
<button class="both">
AMBOS
</button>
</form>


<a
class="btn blue"
href="/diagnostic?secret={{ secret }}"
>
PROBAR BINGX
</a>

</section>


<section class="card position">

<div class="grid">

<div>
<h2>
Posición real BingX
</h2>
</div>

<form
method="post"
action="/sync?secret={{ secret }}"
>

<button class="blue">
SINCRONIZAR
</button>

</form>

</div>


{% if warning %}

<p class="warn">
{{ warning }}
</p>

{% endif %}


{% if live %}

<div class="big">

{{ live.side }}
· {{ live.quantity }} BTC

</div>

<p>

Entrada
{{ '%.2f'|format(live.entry_price) }}
·
{{ live.leverage }}x
·
PnL flotante
{{ '%.4f'|format(live.unrealized_pnl) }}
USDT

</p>

{% else %}

<p class="center muted">

FLAT · Sin posición BTC abierta

</p>

{% endif %}

</section>


<section class="card">

<h2>
Estadísticas
</h2>

<div class="stats">

<div class="stat">
<div class="muted">OPERACIONES</div>
<div class="n">{{ stats.count }}</div>
</div>

<div class="stat">
<div class="muted">GANADAS</div>
<div class="n good">{{ stats.wins }}</div>
</div>

<div class="stat">
<div class="muted">PERDIDAS</div>
<div class="n bad">{{ stats.losses }}</div>
</div>

<div class="stat">
<div class="muted">BE</div>
<div class="n">{{ stats.be }}</div>
</div>

<div class="stat">
<div class="muted">WINRATE</div>
<div class="n">
{{ '%.1f'|format(stats.winrate) }}%
</div>
</div>

<div class="stat">
<div class="muted">PNL NETO</div>
<div class="n {{ 'good' if stats.net>=0 else 'bad' }}">
${{ '%.2f'|format(stats.net) }}
</div>
</div>

<div class="stat">
<div class="muted">PROFIT FACTOR</div>
<div class="n">
{{ '%.2f'|format(stats.pf) }}
</div>
</div>

<div class="stat">
<div class="muted">MAX DD</div>
<div class="n">
${{ '%.2f'|format(stats.max_dd) }}
</div>
</div>

<div class="stat">
<div class="muted">R GLOBAL</div>
<div class="n">
{{ '%.2f'|format(stats.r_global) }}R
</div>
</div>

<div class="stat">
<div class="muted">FEES EST.</div>
<div class="n">
${{ '%.2f'|format(stats.fees) }}
</div>
</div>

</div>

</section>


<section class="card">

<h2>
Últimos eventos
</h2>

{% for e in events %}

<div class="event">

{{ e.time[:19].replace('T',' ') }}
·
{{ e.kind }}
·
{{ e.detail }}

</div>

{% else %}

<p class="muted">
Todavía no hay eventos.
</p>

{% endfor %}

</section>


<section class="card">

<h2>
Agregar operación manual
</h2>

<form
class="row"
method="post"
action="/manual-trade?secret={{ secret }}"
>

<select name="side">
<option>LONG</option>
<option>SHORT</option>
</select>

<input
name="entry_price"
type="number"
step="any"
placeholder="Precio entrada"
required
>

<input
name="exit_price"
type="number"
step="any"
placeholder="Precio salida"
required
>

<input
name="quantity"
type="number"
step="any"
placeholder="Cantidad BTC"
>

<input
name="margin_used"
type="number"
step="any"
placeholder="Margen USDT"
>

<input
name="risk_usd"
type="number"
step="any"
placeholder="Riesgo $ opcional"
>

<input
name="net_pnl"
type="number"
step="any"
placeholder="PnL neto real opcional"
>

<input
name="fees"
type="number"
step="any"
placeholder="Comisiones"
>

<input
name="funding"
type="number"
step="any"
placeholder="Funding"
>

<input
name="opened_at"
type="datetime-local"
>

<input
name="closed_at"
type="datetime-local"
>

<input
name="notes"
placeholder="Notas"
>

<button class="long">
AGREGAR
</button>

</form>

</section>


<section class="card">

<h2>
Importar historial JSON
</h2>

<form
class="row"
method="post"
action="/import-json?secret={{ secret }}"
enctype="multipart/form-data"
>

<input
type="file"
name="file"
accept=".json,application/json"
required
>

<select name="mode">
<option value="append">
AGREGAR
</option>
<option value="replace">
REEMPLAZAR
</option>
</select>

<button class="blue">
IMPORTAR
</button>

</form>

</section>


<section class="grid">

<a
class="btn dark"
href="/download?secret={{ secret }}"
>
DESCARGAR CSV
</a>

<a
class="btn dark"
href="/export-json?secret={{ secret }}"
>
EXPORTAR JSON
</a>

</section>


<section class="card tablewrap">

<table>

<thead>

<tr>

<th>Cierre</th>
<th>Lado</th>
<th>Entrada</th>
<th>Salida</th>
<th>Margen</th>
<th>Fees</th>
<th>PnL neto</th>
<th>R</th>
<th>Motivo</th>

</tr>

</thead>

<tbody>

{% for t in trades %}

<tr>

<td>
{{ t.closed_at[:19].replace('T',' ') }}
</td>

<td>
{{ t.side }}
</td>

<td>
{{ '%.2f'|format(t.entry_price) }}
</td>

<td>
{{ '%.2f'|format(t.exit_price) }}
</td>

<td>
${{ '%.2f'|format(t.margin_used) }}
</td>

<td>
${{ '%.2f'|format(t.total_fees) }}
</td>

<td
class="{{ 'good' if t.net_pnl>=0 else 'bad' }}"
>
${{ '%.2f'|format(t.net_pnl) }}
</td>

<td>
{{ '%.2f'|format(t.r_multiple) }}R
</td>

<td>
{{ t.close_reason }}
</td>

</tr>

{% else %}

<tr>

<td
colspan="9"
class="center muted"
>
Todavía no hay operaciones.
</td>

</tr>

{% endfor %}

</tbody>

</table>

</section>

</main>

</body>

</html>
"""


# ============================================================
# AUTORIZACIÓN
# ============================================================

def control_authorized():

    return secret_matches(
        request.args.get(
            "secret",
            "",
        ),
        CONTROL_SECRET,
    )


# ============================================================
# HOME
# ============================================================

@app.get("/")
def home():

    return (
        f"{BOT_NAME} ACTIVO | "
        "REAL | "
        f"SYMBOL={BINGX_SYMBOL} | "
        f"MODE={store.get_mode()}",
        200,
    )


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():

    return jsonify(
        {
            "status":
            "ok",

            "bot":
            BOT_NAME,

            "real_trading":
            True,

            "symbol":
            BINGX_SYMBOL,

            "timeframe":
            "5m",

            "leverage":
            LEVERAGE,

            "balance_percent":
            BALANCE_PERCENT,

            "position_mode":
            POSITION_MODE,

            "mode":
            store.get_mode(),

            "tv_symbols":
            sorted(TV_SYMBOLS),

            "state_prefix":
            STATE_PREFIX,

            "redis":
            store.redis_enabled,
        }
    )


# ============================================================
# PANEL
# ============================================================

@app.get("/control")
def control():

    if not control_authorized():
        return (
            "Clave de panel inválida",
            403,
        )

    live = None
    warning = ""

    try:

        sync = (
            sync_position()
        )

        if (
            sync.get("reason")
            == "multiple_positions"
        ):

            warning = (
                "Hay más de una "
                "posición BTC abierta. "
                "Las nuevas entradas "
                "están bloqueadas."
            )

        else:

            live = sync.get(
                "position"
            )

    except Exception as exc:

        warning = (
            "Error sincronizando "
            "BingX: "
            f"{redact_error(exc)}"
        )

    trades = (
        store.get_trades()
    )

    stats = (
        trade_summary(trades)
    )

    events = list(
        reversed(
            store.get_events()[-12:]
        )
    )

    return render_template_string(
        PANEL_HTML,

        secret=
        request.args[
            "secret"
        ],

        mode=
        store.get_mode(),

        symbol=
        BINGX_SYMBOL,

        leverage=
        LEVERAGE,

        balance_percent=
        BALANCE_PERCENT,

        live=
        live,

        warning=
        warning,

        trades=
        list(
            reversed(trades)
        ),

        stats=
        stats,

        events=
        events,
    )


# ============================================================
# CAMBIO MODO
# ============================================================

@app.post(
    "/setmode/<mode>"
)
def set_mode(mode):

    if not control_authorized():
        return (
            "Clave de panel inválida",
            403,
        )

    mode = mode.upper()

    if mode not in VALID_MODES:

        return jsonify(
            {
                "error":
                "Modo inválido"
            }
        ), 400

    store.set_mode(mode)

    store.add_event(
        "MODE",
        f"Modo cambiado a {mode}",
    )

    notify(
        f"{BOT_NAME}: "
        f"modo cambiado a {mode}"
    )

    return redirect(
        "/control?secret="
        f"{request.args['secret']}"
    )


# ============================================================
# SYNC
# ============================================================

@app.post("/sync")
def sync_route():

    if not control_authorized():
        return (
            "Clave de panel inválida",
            403,
        )

    try:

        result = (
            sync_position()
        )

        store.add_event(
            "SYNC",
            json.dumps(
                result,
                ensure_ascii=False,
            )[:800],
        )

    except Exception as exc:

        store.add_event(
            "SYNC_ERROR",
            redact_error(exc),
        )

    return redirect(
        "/control?secret="
        f"{request.args['secret']}"
    )


# ============================================================
# DIAGNÓSTICO BINGX
# ============================================================

@app.get("/diagnostic")
def diagnostic():

    if not control_authorized():

        return jsonify(
            {
                "error":
                "No autorizado"
            }
        ), 403

    checks = {
        "time":
        utc_now(),

        "symbol":
        BINGX_SYMBOL,

        "mode":
        store.get_mode(),
    }

    try:

        checks[
            "price"
        ] = bingx.price()

        checks[
            "balance_available_usdt"
        ] = (
            bingx.available_balance()
        )

        checks[
            "positions"
        ] = bingx.positions()

        checks[
            "hedge_mode"
        ] = (
            bingx.position_mode_is_hedge()
        )

        checks[
            "margin_type"
        ] = (
            bingx.margin_type()
        )

        (
            step,
            minimum,
        ) = (
            bingx.contract_rules()
        )

        checks[
            "qty_step"
        ] = step

        checks[
            "min_qty"
        ] = minimum

        checks[
            "status"
        ] = "ok"

        store.add_event(
            "DIAGNOSTIC_OK",
            (
                "balance="
                f"{checks['balance_available_usdt']} "
                "hedge="
                f"{checks['hedge_mode']} "
                "margin="
                f"{checks['margin_type']}"
            ),
        )

        return jsonify(
            checks
        )

    except Exception as exc:

        message = (
            redact_error(exc)
        )

        store.add_event(
            "DIAGNOSTIC_ERROR",
            message,
        )

        return jsonify(
            {
                **checks,

                "status":
                "error",

                "error":
                message,
            }
        ), 500


# ============================================================
# MANUAL TRADE
# ============================================================

@app.post(
    "/manual-trade"
)
def manual_trade():

    if not control_authorized():

        return (
            "Clave de panel inválida",
            403,
        )

    raw = {
        "id":
        str(
            uuid.uuid4()
        ),

        "opened_at":
        request.form.get(
            "opened_at"
        )
        or utc_now(),

        "closed_at":
        request.form.get(
            "closed_at"
        )
        or utc_now(),

        "side":
        request.form.get(
            "side",
            "LONG",
        ),

        "symbol":
        BINGX_SYMBOL,

        "entry_price":
        fnum(
            request.form.get(
                "entry_price"
            ),
            0,
        ),

        "exit_price":
        fnum(
            request.form.get(
                "exit_price"
            ),
            0,
        ),

        "quantity":
        fnum(
            request.form.get(
                "quantity"
            ),
            0,
        ),

        "margin_used":
        fnum(
            request.form.get(
                "margin_used"
            ),
            0,
        ),

        "risk_usd":
        fnum(
            request.form.get(
                "risk_usd"
            ),
            0,
        ),

        "fees":
        fnum(
            request.form.get(
                "fees"
            ),
            0,
        ),

        "funding":
        fnum(
            request.form.get(
                "funding"
            ),
            0,
        ),

        "leverage":
        LEVERAGE,

        "source":
        "MANUAL",

        "notes":
        request.form.get(
            "notes",
            "",
        ),

        "close_reason":
        "manual_panel",
    }

    net_input = (
        request.form.get(
            "net_pnl"
        )
    )

    if net_input not in {
        None,
        "",
    }:

        raw[
            "net_pnl"
        ] = fnum(
            net_input,
            0,
        )

    store.append_trade(
        normalize_trade(
            raw
        )
    )

    return redirect(
        "/control?secret="
        f"{request.args['secret']}"
    )


# ============================================================
# IMPORT JSON
# ============================================================

@app.post(
    "/import-json"
)
def import_json():

    if not control_authorized():

        return (
            "Clave de panel inválida",
            403,
        )

    uploaded = (
        request.files.get(
            "file"
        )
    )

    if not uploaded:

        return (
            "Falta archivo JSON",
            400,
        )

    try:

        payload = json.loads(
            uploaded
            .read()
            .decode(
                "utf-8"
            )
        )

    except Exception as exc:

        return (
            f"JSON inválido: {exc}",
            400,
        )

    if isinstance(
        payload,
        dict,
    ):

        incoming = payload.get(
            "trades",
            [],
        )

    elif isinstance(
        payload,
        list,
    ):

        incoming = payload

    else:

        return (
            "Formato JSON inválido",
            400,
        )

    normalized = [
        normalize_trade(
            item
        )
        for item in incoming
        if isinstance(
            item,
            dict,
        )
    ]

    import_mode = (
        request.form.get(
            "mode",
            "append",
        )
    )

    if import_mode == "replace":

        store.set_trades(
            normalized
        )

    else:

        existing = (
            store.get_trades()
        )

        existing_ids = {
            str(
                t.get("id")
            )
            for t in existing
            if t.get("id")
        }

        for trade in normalized:

            if (
                str(
                    trade.get("id")
                )
                in existing_ids
            ):
                continue

            existing.append(
                trade
            )

            existing_ids.add(
                str(
                    trade.get("id")
                )
            )

        store.set_trades(
            existing
        )

    return redirect(
        "/control?secret="
        f"{request.args['secret']}"
    )


# ============================================================
# CSV
# ============================================================

@app.get("/download")
def download():

    if not control_authorized():

        return (
            "Clave de panel inválida",
            403,
        )

    trades = (
        store.get_trades()
    )

    return send_file(
        io.BytesIO(
            csv_bytes(
                trades
            )
        ),
        mimetype=
        "text/csv",
        as_attachment=True,
        download_name=
        "btc_bot_historial.csv",
    )


# ============================================================
# EXPORT JSON
# ============================================================

@app.get(
    "/export-json"
)
def export_json():

    if not control_authorized():

        return (
            "Clave de panel inválida",
            403,
        )

    raw = json.dumps(
        {
            "app":
            "BOT BTC BINGX 5M",

            "version":
            2,

            "exportedAt":
            utc_now(),

            "trades":
            store.get_trades(),
        },
        ensure_ascii=False,
        indent=2,
    ).encode(
        "utf-8"
    )

    return send_file(
        io.BytesIO(
            raw
        ),
        mimetype=
        "application/json",
        as_attachment=True,
        download_name=
        "btc_bot_historial.json",
    )


# ============================================================
# WEBHOOK
# ============================================================

@app.post("/webhook")
def webhook():

    payload = (
        request.get_json(
            silent=True
        )
        or {}
    )

    safe_payload = {
        "side":
        payload.get("side")
        or payload.get("action"),

        "symbol":
        payload.get("symbol"),

        "timeframe":
        payload.get("timeframe"),
    }

    logger.info(
        "WEBHOOK recibido: %s",
        safe_payload,
    )

    store.add_event(
        "WEBHOOK_RECEIVED",
        json.dumps(
            safe_payload,
            ensure_ascii=False,
        ),
    )

    if not secret_matches(
        payload.get(
            "secret"
        ),
        WEBHOOK_SECRET,
    ):

        store.add_event(
            "WEBHOOK_REJECTED",
            "Clave de webhook inválida",
        )

        return jsonify(
            {
                "error":
                "Clave de webhook inválida"
            }
        ), 403

    side = str(
        payload.get("side")
        or payload.get("action")
        or ""
    ).upper().strip()

    symbol = str(
        payload.get(
            "symbol",
            "",
        )
    ).upper().strip()

    timeframe = str(
        payload.get(
            "timeframe",
            "",
        )
    ).lower().strip()

    if side not in {
        "BUY",
        "SELL",
    }:

        store.add_event(
            "WEBHOOK_REJECTED",
            f"side={side}",
        )

        return jsonify(
            {
                "error":
                "Usa BUY o SELL"
            }
        ), 400

    if (
        symbol not in TV_SYMBOLS
        and symbol != BINGX_SYMBOL
    ):

        store.add_event(
            "WEBHOOK_REJECTED",
            f"symbol={symbol}",
        )

        return jsonify(
            {
                "error":
                "Símbolo BTC no permitido",

                "received":
                symbol,

                "allowed":
                sorted(TV_SYMBOLS),
            }
        ), 400

    if timeframe not in {
        "5",
        "5m",
        "05",
        "05m",
    }:

        store.add_event(
            "WEBHOOK_REJECTED",
            f"timeframe={timeframe}",
        )

        return jsonify(
            {
                "error":
                "Solo se aceptan señales 5M",

                "received":
                timeframe,
            }
        ), 400

    try:

        result = (
            process_signal(
                side
            )
        )

        store.add_event(
            "WEBHOOK_PROCESSED",
            json.dumps(
                result,
                ensure_ascii=False,
            )[:1200],
        )

        return jsonify(
            {
                "ok":
                True,

                "result":
                result,
            }
        )

    except Exception as exc:

        message = (
            redact_error(exc)
        )

        logger.exception(
            "ERROR procesando webhook"
        )

        store.add_event(
            "WEBHOOK_ERROR",
            message,
        )

        notify(
            f"ERROR {BOT_NAME}: "
            f"{message}"
        )

        return jsonify(
            {
                "ok":
                False,

                "error":
                message,
            }
        ), 500


# ============================================================
# MONITOR
# ============================================================

@app.get("/monitor")
def monitor():

    if not secret_matches(
        request.args.get(
            "token",
            "",
        ),
        MONITOR_SECRET,
    ):

        return jsonify(
            {
                "error":
                "No autorizado"
            }
        ), 401

    try:

        sync = (
            sync_position()
        )

    except Exception as exc:

        sync = {
            "status":
            "error",

            "error":
            redact_error(exc),
        }

    return jsonify(
        {
            "checked_at":
            utc_now(),

            "bot":
            BOT_NAME,

            "real_trading":
            True,

            "mode":
            store.get_mode(),

            "sync":
            sync,

            "active_trade":
            store.get_active_trade(),

            "stats_all":
            trade_summary(
                store.get_trades()
            ),

            "events":
            store.get_events()[-10:],
        }
    )


# ============================================================
# INICIO
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "10000",
            )
        ),
    )