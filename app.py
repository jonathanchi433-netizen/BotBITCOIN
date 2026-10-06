import csv
import hashlib
import hmac
import io
import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_file,
)


# ============================================================
# CONFIGURACIÓN
# ============================================================
BOT_NAME = "BOT BTC BINGX 5M"
BINGX_BASE_URL = "https://open-api.bingx.com"

BINGX_SYMBOL = os.getenv("BINGX_SYMBOL", "BTC-USDT").strip()

TV_SYMBOLS = {
    item.strip().upper()
    for item in os.getenv(
        "TV_SYMBOLS",
        "BTCUSDT,BTCUSDT.P,BINGX:BTCUSDT.P,BTC-USDT",
    ).split(",")
    if item.strip()
}

BINGX_API_KEY = os.getenv(
    "BINGX_API_KEY",
    "",
).strip()

BINGX_API_SECRET = (
    os.getenv(
        "BINGX_API_SECRET",
        "",
    ).strip()
    or os.getenv(
        "BINGX_SECRET_KEY",
        "",
    ).strip()
)

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "",
).strip()

CONTROL_SECRET = os.getenv(
    "CONTROL_SECRET",
    WEBHOOK_SECRET,
).strip()

MONITOR_SECRET = os.getenv(
    "MONITOR_SECRET",
    "",
).strip()

BALANCE_PERCENT = float(
    os.getenv(
        "BALANCE_PERCENT",
        "90",
    )
)

LEVERAGE = int(
    os.getenv(
        "LEVERAGE",
        "2",
    )
)

FEE_RATE = float(
    os.getenv(
        "FEE_RATE",
        "0.0005",
    )
)

QTY_STEP_FALLBACK = float(
    os.getenv(
        "QTY_STEP",
        "0.0001",
    )
)

MIN_QTY_FALLBACK = float(
    os.getenv(
        "MIN_QTY",
        "0.0001",
    )
)

POSITION_MODE = os.getenv(
    "POSITION_MODE",
    "HEDGE",
).strip().upper()

UPSTASH_REDIS_REST_URL = os.getenv(
    "UPSTASH_REDIS_REST_URL",
    "",
).strip()

UPSTASH_REDIS_REST_TOKEN = os.getenv(
    "UPSTASH_REDIS_REST_TOKEN",
    "",
).strip()

STATE_PREFIX = os.getenv(
    "STATE_PREFIX",
    "bot_btc_5m",
).strip()

DATA_DIR = os.getenv(
    "DATA_DIR",
    ".",
).strip()

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "",
).strip()

VALID_MODES = [
    "OFF",
    "LONG_ONLY",
    "SHORT_ONLY",
    "CLOSE_ONLY",
    "BOTH",
]

SIGNAL_LOCK = threading.RLock()

app = Flask(__name__)


# ============================================================
# UTILIDADES
# ============================================================

def utc_now():
    return datetime.now(
        timezone.utc
    ).isoformat()


def fnum(
    value,
    default=0.0,
):
    try:
        return float(value)
    except Exception:
        return default


def fint(
    value,
    default=0,
):
    try:
        return int(
            float(value)
        )
    except Exception:
        return default


def parse_time(value):
    if not value:
        return datetime.min.replace(
            tzinfo=timezone.utc
        )

    try:
        dt = datetime.fromisoformat(
            str(value).replace(
                "Z",
                "+00:00",
            )
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt

    except Exception:
        return datetime.min.replace(
            tzinfo=timezone.utc
        )


def secret_matches(
    received,
    expected,
):
    return (
        bool(
            received
            and expected
        )
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
            (
                "https://api.telegram.org/bot"
                f"{TELEGRAM_BOT_TOKEN}"
                "/sendMessage"
            ),
            json={
                "chat_id":
                TELEGRAM_CHAT_ID,

                "text":
                str(message)[:3900],
            },
            timeout=10,
        )

    except Exception:
        pass


def floor_step(
    value,
    step,
):
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
            (
                float(value)
                + 1e-12
            )
            / step
        )
        * step
    )

    return round(
        result,
        decimals,
    )


# ============================================================
# ALMACENAMIENTO
# ============================================================

class Store:

    def __init__(self):
        os.makedirs(
            DATA_DIR,
            exist_ok=True,
        )

        self.lock = (
            threading.RLock()
        )

    @property
    def redis_enabled(self):
        return bool(
            UPSTASH_REDIS_REST_URL
            and UPSTASH_REDIS_REST_TOKEN
        )

    def _key(
        self,
        name,
    ):
        return (
            f"{STATE_PREFIX}:"
            f"{name}"
        )

    def _path(
        self,
        name,
    ):
        return os.path.join(
            DATA_DIR,
            (
                f"{STATE_PREFIX}_"
                f"{name}.json"
            ),
        )

    def _redis(
        self,
        command,
    ):
        response = requests.post(
            UPSTASH_REDIS_REST_URL.rstrip(
                "/"
            ),
            headers={
                "Authorization": (
                    "Bearer "
                    f"{UPSTASH_REDIS_REST_TOKEN}"
                ),
                "Content-Type":
                "application/json",
            },
            json=command,
            timeout=12,
        )

        payload = (
            response.json()
        )

        if (
            response.status_code
            >= 400
            or payload.get(
                "error"
            )
        ):
            raise RuntimeError(
                "Error Upstash: "
                f"{payload}"
            )

        return payload.get(
            "result"
        )

    def get(
        self,
        name,
        default,
    ):
        with self.lock:

            if self.redis_enabled:

                raw = self._redis(
                    [
                        "GET",
                        self._key(
                            name
                        ),
                    ]
                )

                if not raw:
                    return default

                return json.loads(
                    raw
                )

            path = self._path(
                name
            )

            if not os.path.exists(
                path
            ):
                return default

            with open(
                path,
                "r",
                encoding="utf-8",
            ) as handle:
                return json.load(
                    handle
                )

    def set(
        self,
        name,
        value,
    ):
        with self.lock:

            raw = json.dumps(
                value,
                ensure_ascii=False,
            )

            if self.redis_enabled:

                self._redis(
                    [
                        "SET",
                        self._key(
                            name
                        ),
                        raw,
                    ]
                )

                return

            with open(
                self._path(
                    name
                ),
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

    def set_mode(
        self,
        mode,
    ):
        if mode not in VALID_MODES:
            raise ValueError(
                "Modo inválido"
            )

        self.set(
            "mode",
            {
                "mode":
                mode,

                "updated_at":
                utc_now(),
            },
        )

    def get_active_trade(
        self,
    ):
        return self.get(
            "active_trade",
            None,
        )

    def set_active_trade(
        self,
        trade,
    ):
        self.set(
            "active_trade",
            trade,
        )

    def clear_active_trade(
        self,
    ):
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

    def set_trades(
        self,
        trades,
    ):
        self.set(
            "trades",
            list(trades),
        )

    def append_trade(
        self,
        trade,
    ):
        with self.lock:

            trades = (
                self.get_trades()
            )

            trades.append(
                trade
            )

            self.set_trades(
                trades
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

            params[
                "timestamp"
            ] = int(
                time.time()
                * 1000
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
                url += (
                    f"?{query}"
                )

        response = requests.request(
            method,
            url,
            headers=headers,
            timeout=20,
        )

        try:
            payload = (
                response.json()
            )

        except Exception:
            raise RuntimeError(
                "BingX devolvió "
                "una respuesta no JSON: "
                f"{response.status_code} "
                f"{response.text[:300]}"
            )

        if (
            response.status_code
            >= 400
            or str(
                payload.get(
                    "code"
                )
            )
            != "0"
        ):
            raise RuntimeError(
                f"Error BingX: "
                f"{payload}"
            )

        return payload

    # ========================================================
    # PRECIO
    # ========================================================

    def price(self):
        payload = self._request(
            "GET",
            (
                "/openApi/swap/"
                "v2/quote/price"
            ),
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
            data.get(
                "price"
            )
            or data.get(
                "lastPrice"
            ),
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

    def available_balance(
        self,
    ):
        payload = self._request(
            "GET",
            (
                "/openApi/swap/"
                "v3/user/balance"
            ),
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

        for item in (
            data or []
        ):

            if not isinstance(
                item,
                dict,
            ):
                continue

            asset = str(
                item.get(
                    "asset",
                    "",
                )
            ).upper()

            if (
                asset
                and asset != "USDT"
            ):
                continue

            available = fnum(
                item.get(
                    "availableMargin"
                )
                or item.get(
                    "availableBalance"
                )
                or item.get(
                    "balance"
                ),
                -1,
            )

            if available >= 0:
                return available

        raise RuntimeError(
            "No se pudo leer "
            "el balance USDT "
            "disponible"
        )

    # ========================================================
    # REGLAS DEL CONTRATO
    # ========================================================

    def contract_rules(
        self,
    ):
        step = (
            QTY_STEP_FALLBACK
        )

        minimum = (
            MIN_QTY_FALLBACK
        )

        try:

            payload = self._request(
                "GET",
                (
                    "/openApi/swap/"
                    "v2/quote/contracts"
                ),
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
                    data.get(
                        "contracts"
                    )
                    or data.get(
                        "data"
                    )
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
                    !=
                    BINGX_SYMBOL.upper()
                ):
                    continue

                precision = (
                    item.get(
                        "quantityPrecision"
                    )
                )

                if (
                    precision
                    is not None
                ):

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

        except Exception:
            pass

        return (
            step,
            minimum,
        )

    # ========================================================
    # MARGEN AISLADO
    # ========================================================

    def set_isolated(
        self,
    ):
        current = self._request(
            "GET",
            (
                "/openApi/swap/"
                "v2/trade/marginType"
            ),
            {
                "symbol":
                BINGX_SYMBOL,
            },
        )

        data = current.get(
            "data",
            {},
        )

        margin_type = str(
            data.get(
                "marginType",
                "",
            )
        ).upper()

        if (
            margin_type
            == "ISOLATED"
        ):
            return "ISOLATED"

        self._request(
            "POST",
            (
                "/openApi/swap/"
                "v2/trade/marginType"
            ),
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

    def set_leverage(
        self,
    ):
        if (
            POSITION_MODE
            == "HEDGE"
        ):
            sides = (
                "LONG",
                "SHORT",
            )

        else:
            sides = (
                "BOTH",
            )

        for side in sides:

            self._request(
                "POST",
                (
                    "/openApi/swap/"
                    "v2/trade/leverage"
                ),
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

    def quantity(
        self,
    ):
        balance = (
            self.available_balance()
        )

        price = self.price()

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
                f"{minimum}"
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
        }

    # ========================================================
    # POSICIONES REALES
    # ========================================================

    def positions(
        self,
    ):
        payload = self._request(
            "GET",
            (
                "/openApi/swap/"
                "v2/user/positions"
            ),
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

            if (
                item.get(
                    "symbol"
                )
                and str(
                    item.get(
                        "symbol"
                    )
                ).upper()
                !=
                BINGX_SYMBOL.upper()
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

            if abs(
                amount
            ) <= 0:
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

            quantity = abs(
                amount
            )

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

    def _position_side(
        self,
        direction,
    ):
        if (
            POSITION_MODE
            == "HEDGE"
        ):
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

        if (
            POSITION_MODE
            != "HEDGE"
        ):
            params[
                "reduceOnly"
            ] = (
                "true"
                if closing
                else "false"
            )

        payload = self._request(
            "POST",
            (
                "/openApi/swap/"
                "v2/trade/order"
            ),
            params,
        )

        order = payload.get(
            "data",
            {},
        )

        if (
            isinstance(
                order,
                dict,
            )
            and isinstance(
                order.get(
                    "order"
                ),
                dict,
            )
        ):
            order = order[
                "order"
            ]

        order_id = (
            order.get(
                "orderId"
            )
            or order.get(
                "orderID"
            )
        )

        details = order

        if order_id:

            for _ in range(6):

                time.sleep(
                    0.35
                )

                try:

                    check = (
                        self._request(
                            "GET",
                            (
                                "/openApi/"
                                "swap/v2/"
                                "trade/order"
                            ),
                            {
                                "symbol":
                                BINGX_SYMBOL,

                                "orderId":
                                order_id,
                            },
                        )
                    )

                    details = (
                        check.get(
                            "data",
                            {},
                        )
                    )

                    if (
                        isinstance(
                            details,
                            dict,
                        )
                        and isinstance(
                            details.get(
                                "order"
                            ),
                            dict,
                        )
                    ):
                        details = (
                            details[
                                "order"
                            ]
                        )

                    if (
                        details.get(
                            "avgPrice"
                        )
                        or details.get(
                            "executedQty"
                        )
                    ):
                        break

                except Exception:
                    pass

        price = fnum(
            details.get(
                "avgPrice"
            )
            or details.get(
                "price"
            )
            or reference_price,
            reference_price,
        )

        executed = fnum(
            details.get(
                "executedQty"
            )
            or details.get(
                "quantity"
            )
            or quantity,
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
# NORMALIZAR OPERACIONES
# ============================================================

def normalize_trade(
    raw,
):
    raw = dict(
        raw or {}
    )

    side = str(
        raw.get(
            "side"
        )
        or raw.get(
            "direction"
        )
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
            raw.get(
                "entry"
            ),
        ),
        0,
    )

    exit_price = fnum(
        raw.get(
            "exit_price",
            raw.get(
                "exit"
            ),
        ),
        0,
    )

    quantity = fnum(
        raw.get(
            "quantity",
            raw.get(
                "qty"
            ),
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
            raw.get(
                "margin"
            ),
        ),
        0,
    )

    risk = fnum(
        raw.get(
            "risk_usd",
            raw.get(
                "risk"
            ),
        ),
        0,
    )

    fees = fnum(
        raw.get(
            "total_fees",
            raw.get(
                "fees"
            ),
        ),
        0,
    )

    funding = fnum(
        raw.get(
            "funding"
        ),
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
        gross = raw.get(
            "grossOverride"
        )

    if gross is None:
        gross = (
            calculated_gross
        )

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
        (
            gross
            - fees
            - funding
        ),
    )

    opened_at = (
        raw.get(
            "opened_at"
        )
        or raw.get(
            "open_time"
        )
        or raw.get(
            "date"
        )
        or utc_now()
    )

    closed_at = (
        raw.get(
            "closed_at"
        )
        or raw.get(
            "close_time"
        )
        or raw.get(
            "date"
        )
        or opened_at
    )

    price_move = 0

    if (
        entry > 0
        and exit_price > 0
    ):
        price_move = (
            (
                exit_price
                / entry
                - 1
            )
            * sign
            * 100
        )

    return {
        "id":
        str(
            raw.get(
                "id"
            )
            or uuid.uuid4()
        ),

        "opened_at":
        str(
            opened_at
        ),

        "closed_at":
        str(
            closed_at
        ),

        "side":
        side,

        "symbol":
        str(
            raw.get(
                "symbol"
            )
            or BINGX_SYMBOL
        ),

        "quantity":
        quantity,

        "entry_price":
        entry,

        "exit_price":
        exit_price,

        "leverage":
        leverage,

        "margin_used":
        margin,

        "risk_usd":
        risk,

        "price_move_pct":
        fnum(
            raw.get(
                "price_move_pct"
            ),
            price_move,
        ),

        "gross_pnl":
        gross,

        "total_fees":
        fees,

        "funding":
        funding,

        "net_pnl":
        net,

        "r_multiple":
        (
            net / risk
            if risk > 0
            else fnum(
                raw.get(
                    "r_multiple"
                ),
                0,
            )
        ),

        "close_reason":
        str(
            raw.get(
                "close_reason"
            )
            or raw.get(
                "reason"
            )
            or "manual/import"
        ),

        "source":
        str(
            raw.get(
                "source"
            )
            or "IMPORT"
        ),

        "notes":
        str(
            raw.get(
                "notes"
            )
            or ""
        ),

        "open_order_id":
        raw.get(
            "open_order_id"
        ),

        "close_order_id":
        raw.get(
            "close_order_id"
        ),
    }


# ============================================================
# SINCRONIZACIÓN CON BINGX
# ============================================================

def sync_position():
    positions = (
        bingx.positions()
    )

    stored = (
        store.get_active_trade()
    )

    if len(
        positions
    ) > 1:

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
            live[
                "side"
            ],

            "symbol":
            BINGX_SYMBOL,

            "quantity":
            live[
                "quantity"
            ],

            "entry_price":
            live[
                "entry_price"
            ],

            "leverage":
            live[
                "leverage"
            ],

            "balance_before":
            balance,

            "margin_used":
            live[
                "margin_used"
            ],

            "entry_fee":
            (
                live[
                    "entry_price"
                ]
                * live[
                    "quantity"
                ]
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
            "posición manual "
            "detectada y adoptada: "
            f"{live['side']}"
        )

    else:

        stored[
            "side"
        ] = live[
            "side"
        ]

        stored[
            "quantity"
        ] = live[
            "quantity"
        ]

        stored[
            "entry_price"
        ] = live[
            "entry_price"
        ]

        stored[
            "leverage"
        ] = live[
            "leverage"
        ]

        stored[
            "margin_used"
        ] = live[
            "margin_used"
        ]

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

def open_trade(
    direction,
):
    sync = (
        sync_position()
    )

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
        sync.get(
            "reason"
        )
        ==
        "multiple_positions"
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

    quantity = fill[
        "quantity"
    ]

    entry_price = fill[
        "price"
    ]

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

def close_trade(
    reason,
):
    sync = (
        sync_position()
    )

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

    exit_price = fill[
        "price"
    ]

    quantity = min(
        fnum(
            fill[
                "quantity"
            ],
            live[
                "quantity"
            ],
        ),
        live[
            "quantity"
        ],
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
            live[
                "leverage"
            ],

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

    notify(
        f"CIERRE {direction} "
        f"{BOT_NAME}\n"
        f"PnL neto aprox.: "
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
# LÓGICA DE SEÑALES
# ============================================================

def process_signal(
    side,
):
    with SIGNAL_LOCK:

        mode = (
            store.get_mode()
        )

        sync = (
            sync_position()
        )

        if (
            sync.get(
                "reason"
            )
            ==
            "multiple_positions"
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
            trade.get(
                "side"
            )
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

        # Señal contraria cierra.
        # NO revierte en la misma alerta.

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

        if (
            mode
            == "CLOSE_ONLY"
        ):
            return result

        if (
            side == "BUY"
            and mode in {
                "LONG_ONLY",
                "BOTH",
            }
            and current_side
            is None
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
            and current_side
            is None
        ):

            result[
                "opened"
            ] = open_trade(
                "SHORT"
            )

        return result


# ============================================================
# FILTRO DE HISTORIAL
# ============================================================

def filter_trades(
    trades,
    period="all",
    month="",
):
    now = datetime.now(
        timezone.utc
    )

    if (
        period
        == "last_month"
    ):

        start = (
            now
            - timedelta(
                days=30
            )
        )

        return [
            trade
            for trade in trades
            if parse_time(
                trade.get(
                    "closed_at"
                )
            )
            >= start
        ]

    if (
        period
        == "last_3_months"
    ):

        start = (
            now
            - timedelta(
                days=90
            )
        )

        return [
            trade
            for trade in trades
            if parse_time(
                trade.get(
                    "closed_at"
                )
            )
            >= start
        ]

    if (
        period
        == "specific_month"
        and month
    ):

        return [
            trade
            for trade in trades
            if str(
                trade.get(
                    "closed_at",
                    "",
                )
            )[:7]
            == month
        ]

    return list(
        trades
    )


# ============================================================
# ESTADÍSTICAS
# ============================================================

def trade_summary(
    trades,
):
    ordered = sorted(
        trades,
        key=lambda item:
        item.get(
            "closed_at",
            "",
        ),
    )

    wins = []
    losses = []
    breakeven = []

    for trade in ordered:

        pnl = fnum(
            trade.get(
                "net_pnl"
            ),
            0,
        )

        if pnl > 0.01:
            wins.append(
                trade
            )

        elif pnl < -0.01:
            losses.append(
                trade
            )

        else:
            breakeven.append(
                trade
            )

    net = sum(
        fnum(
            trade.get(
                "net_pnl"
            ),
            0,
        )
        for trade in ordered
    )

    gross = sum(
        fnum(
            trade.get(
                "gross_pnl"
            ),
            0,
        )
        for trade in ordered
    )

    fees = sum(
        fnum(
            trade.get(
                "total_fees"
            ),
            0,
        )
        for trade in ordered
    )

    funding = sum(
        fnum(
            trade.get(
                "funding"
            ),
            0,
        )
        for trade in ordered
    )

    gains = sum(
        fnum(
            trade.get(
                "net_pnl"
            ),
            0,
        )
        for trade in wins
    )

    losses_abs = abs(
        sum(
            fnum(
                trade.get(
                    "net_pnl"
                ),
                0,
            )
            for trade in losses
        )
    )

    if losses_abs > 0:

        profit_factor = (
            gains
            / losses_abs
        )

    elif gains > 0:

        profit_factor = 999

    else:

        profit_factor = 0

    running = 0.0
    peak = 0.0
    max_drawdown = 0.0

    curve = [
        0.0
    ]

    for trade in ordered:

        running += fnum(
            trade.get(
                "net_pnl"
            ),
            0,
        )

        peak = max(
            peak,
            running,
        )

        max_drawdown = max(
            max_drawdown,
            peak - running,
        )

        curve.append(
            round(
                running,
                8,
            )
        )

    r_global = 0.0

    for trade in ordered:

        risk = fnum(
            trade.get(
                "risk_usd"
            ),
            0,
        )

        if risk > 0:

            r_global += (
                fnum(
                    trade.get(
                        "net_pnl"
                    ),
                    0,
                )
                / risk
            )

    long_trades = [
        trade
        for trade in ordered
        if trade.get(
            "side"
        )
        == "LONG"
    ]

    short_trades = [
        trade
        for trade in ordered
        if trade.get(
            "side"
        )
        == "SHORT"
    ]

    streak_count = 0
    streak_type = ""

    for trade in reversed(
        ordered
    ):

        pnl = fnum(
            trade.get(
                "net_pnl"
            ),
            0,
        )

        current = (
            "W"
            if pnl > 0.01
            else
            "L"
            if pnl < -0.01
            else
            "BE"
        )

        if streak_count == 0:

            streak_type = (
                current
            )

            streak_count = 1

        elif (
            current
            == streak_type
        ):

            streak_count += 1

        else:
            break

    return {
        "trades":
        len(
            ordered
        ),

        "wins":
        len(
            wins
        ),

        "losses":
        len(
            losses
        ),

        "be":
        len(
            breakeven
        ),

        "winrate":
        (
            len(wins)
            /
            (
                len(wins)
                + len(losses)
            )
            * 100
            if (
                wins
                or losses
            )
            else 0
        ),

        "gross_pnl":
        gross,

        "fees":
        fees,

        "funding":
        funding,

        "net_pnl":
        net,

        "r_global":
        r_global,

        "profit_factor":
        profit_factor,

        "max_drawdown":
        max_drawdown,

        "biggest_win":
        max(
            [
                fnum(
                    trade.get(
                        "net_pnl"
                    ),
                    0,
                )
                for trade in wins
            ],
            default=0,
        ),

        "biggest_loss":
        min(
            [
                fnum(
                    trade.get(
                        "net_pnl"
                    ),
                    0,
                )
                for trade in losses
            ],
            default=0,
        ),

        "avg_win":
        (
            gains
            / len(
                wins
            )
            if wins
            else 0
        ),

        "avg_loss":
        (
            sum(
                fnum(
                    trade.get(
                        "net_pnl"
                    ),
                    0,
                )
                for trade in losses
            )
            / len(
                losses
            )
            if losses
            else 0
        ),

        "long_count":
        len(
            long_trades
        ),

        "long_pnl":
        sum(
            fnum(
                trade.get(
                    "net_pnl"
                ),
                0,
            )
            for trade in long_trades
        ),

        "short_count":
        len(
            short_trades
        ),

        "short_pnl":
        sum(
            fnum(
                trade.get(
                    "net_pnl"
                ),
                0,
            )
            for trade in short_trades
        ),

        "streak":
        (
            f"{streak_count} "
            f"{streak_type}"
            if ordered
            else "0"
        ),

        "curve":
        curve,
    }


# ============================================================
# CSV
# ============================================================

def csv_bytes(
    trades,
):
    fields = [
        "id",
        "opened_at",
        "closed_at",
        "side",
        "symbol",
        "source",
        "quantity",
        "entry_price",
        "exit_price",
        "leverage",
        "margin_used",
        "risk_usd",
        "price_move_pct",
        "gross_pnl",
        "total_fees",
        "funding",
        "net_pnl",
        "r_multiple",
        "close_reason",
        "notes",
    ]

    buffer = io.StringIO()

    writer = csv.DictWriter(
        buffer,
        fieldnames=fields,
        extrasaction="ignore",
    )

    writer.writeheader()

    writer.writerows(
        trades
    )

    return (
        buffer
        .getvalue()
        .encode(
            "utf-8"
        )
    )


# ============================================================
# PANEL HTML
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
--bg:#080a0e;
--card:#15181e;
--card2:#0e1116;
--muted:#969daa;
--green:#00e889;
--red:#ff526b;
--blue:#397cff;
--gold:#d5a820;
--line:#272d37;
}

*{
box-sizing:border-box;
}

body{
margin:0;
padding:16px;
background:var(--bg);
color:#fff;
font-family:Arial,sans-serif;
}

main{
max-width:1200px;
margin:auto;
}

h1{
text-align:center;
font-size:30px;
margin:8px 0 5px;
}

.subtitle{
text-align:center;
color:var(--muted);
font-size:13px;
margin-bottom:18px;
}

.card{
background:var(--card);
border:1px solid var(--line);
padding:16px;
border-radius:17px;
margin:13px 0;
}

.mode{
text-align:center;
font-size:38px;
font-weight:900;
color:var(--green);
}

.muted{
color:var(--muted);
font-size:12px;
}

.center{
text-align:center;
}

.buttons,
.grid{
display:grid;
grid-template-columns:
repeat(
auto-fit,
minmax(145px,1fr)
);
gap:10px;
}

.buttons form{
margin:0;
}

button,
.btn{
width:100%;
border:0;
border-radius:13px;
padding:14px;
color:#fff;
font-size:15px;
font-weight:800;
cursor:pointer;
text-decoration:none;
display:block;
text-align:center;
}

.off{
background:#555c67;
}

.long{
background:#07884c;
}

.short{
background:#a91e36;
}

.close{
background:#1c61b8;
}

.both{
background:#af870e;
}

.blue{
background:#397cff;
}

.dark{
background:#343942;
}

.stat{
background:var(--card2);
border:1px solid var(--line);
padding:13px;
border-radius:13px;
text-align:center;
}

.label{
color:var(--muted);
font-size:11px;
text-transform:uppercase;
}

.value{
font-size:20px;
font-weight:900;
margin-top:5px;
}

.positive{
color:var(--green);
}

.negative{
color:var(--red);
}

.live{
border-color:#17694c;
}

.warn{
color:#ffd76a;
}

form.row{
display:grid;
grid-template-columns:
repeat(
auto-fit,
minmax(145px,1fr)
);
gap:9px;
}

input,
select{
width:100%;
background:#0b0e12;
color:#fff;
border:1px solid #3a414d;
border-radius:9px;
padding:11px;
}

.table-wrap{
overflow:auto;
}

table{
width:100%;
border-collapse:collapse;
font-size:12px;
white-space:nowrap;
}

th,
td{
padding:9px 7px;
border-bottom:1px solid var(--line);
text-align:right;
}

th:first-child,
td:first-child{
text-align:left;
}

.badge{
display:inline-block;
padding:4px 8px;
background:#282d35;
border-radius:999px;
font-size:11px;
}

.curvebox{
width:100%;
height:170px;
background:#0b0d11;
border-radius:12px;
margin-top:14px;
overflow:hidden;
}

#curve{
width:100%;
height:100%;
}

@media(max-width:650px){

h1{
font-size:25px;
}

.mode{
font-size:30px;
}

}

</style>

</head>


<body>

<main>

<h1>
₿ BOT BTC BINGX
</h1>

<div class="subtitle">

BTC-USDT ·
5M ·
{{ leverage }}x ·
ISOLATED ·
{{ balance_percent }}%
del balance · REAL

</div>


<section class="card">

<div class="muted center">
MODO ACTUAL
</div>

<div class="mode">
{{ mode }}
</div>

</section>


<section class="buttons">

{% for item in modes %}

<form
method="post"
action="/setmode/{{ item }}?secret={{ secret }}"
>

<button
class="{{
'off'
if item=='OFF'
else
'long'
if item=='LONG_ONLY'
else
'short'
if item=='SHORT_ONLY'
else
'close'
if item=='CLOSE_ONLY'
else
'both'
}}"
>

{{
{
'OFF':'OFF',
'LONG_ONLY':'SOLO LONG',
'SHORT_ONLY':'SOLO SHORT',
'CLOSE_ONLY':'SOLO CERRAR',
'BOTH':'AMBOS'
}[item]
}}

</button>

</form>

{% endfor %}

</section>


<section class="card live">

<div
style="
display:flex;
justify-content:space-between;
align-items:center;
gap:10px;
"
>

<h2 style="margin:0">
Posición real BingX
</h2>

<form
method="post"
action="/sync?secret={{ secret }}"
>

<button
class="blue"
style="padding:9px 12px"
>

SINCRONIZAR

</button>

</form>

</div>


{% if live %}

<div
class="grid"
style="margin-top:12px"
>

<div class="stat">

<div class="label">
Dirección
</div>

<div class="value">
{{ live.side }}
</div>

</div>


<div class="stat">

<div class="label">
Entrada
</div>

<div class="value">

${{
'%.2f'
|format(
live.entry_price
)
}}

</div>

</div>


<div class="stat">

<div class="label">
Cantidad BTC
</div>

<div class="value">
{{ live.quantity }}
</div>

</div>


<div class="stat">

<div class="label">
Leverage
</div>

<div class="value">
{{ live.leverage }}x
</div>

</div>


<div class="stat">

<div class="label">
Margen
</div>

<div class="value">

${{
'%.2f'
|format(
live.margin_used
)
}}

</div>

</div>


<div class="stat">

<div class="label">
PnL flotante
</div>

<div
class="value {{
'positive'
if live.unrealized_pnl>=0
else
'negative'
}}"
>

${{
'%.2f'
|format(
live.unrealized_pnl
)
}}

</div>

</div>


<div class="stat">

<div class="label">
Liquidación
</div>

<div class="value">

{% if live.liquidation_price %}

${{
'%.2f'
|format(
live.liquidation_price
)
}}

{% else %}

—

{% endif %}

</div>

</div>


<div class="stat">

<div class="label">
Origen
</div>

<div class="value">
{{ active_source }}
</div>

</div>

</div>


{% else %}

<p class="muted center">

FLAT ·
Sin posición BTC abierta

</p>

{% endif %}


{% if sync_warning %}

<p class="warn">

{{ sync_warning }}

</p>

{% endif %}

</section>


<section class="card">

<h2>
Estadísticas
</h2>

<div class="grid">


<div class="stat">

<div class="label">
Operaciones
</div>

<div class="value">
{{ stats.trades }}
</div>

</div>


<div class="stat">

<div class="label">
Ganadas
</div>

<div class="value positive">
{{ stats.wins }}
</div>

</div>


<div class="stat">

<div class="label">
Perdidas
</div>

<div class="value negative">
{{ stats.losses }}
</div>

</div>


<div class="stat">

<div class="label">
BE
</div>

<div class="value">
{{ stats.be }}
</div>

</div>


<div class="stat">

<div class="label">
Winrate
</div>

<div class="value">

{{ '%.1f'|format(stats.winrate) }}%

</div>

</div>


<div class="stat">

<div class="label">
PnL global
</div>

<div
class="value {{
'positive'
if stats.net_pnl>=0
else
'negative'
}}"
>

${{
'%.2f'
|format(
stats.net_pnl
)
}}

</div>

</div>


<div class="stat">

<div class="label">
R global
</div>

<div
class="value {{
'positive'
if stats.r_global>=0
else
'negative'
}}"
>

{{ '%.2f'|format(stats.r_global) }}R

</div>

</div>


<div class="stat">

<div class="label">
Profit factor
</div>

<div class="value">

{{ '%.2f'|format(stats.profit_factor) }}

</div>

</div>


<div class="stat">

<div class="label">
Mayor ganancia
</div>

<div class="value positive">

${{
'%.2f'
|format(
stats.biggest_win
)
}}

</div>

</div>


<div class="stat">

<div class="label">
Mayor pérdida
</div>

<div class="value negative">

${{
'%.2f'
|format(
stats.biggest_loss
)
}}

</div>

</div>


<div class="stat">

<div class="label">
Ganancia media
</div>

<div class="value positive">

${{
'%.2f'
|format(
stats.avg_win
)
}}

</div>

</div>


<div class="stat">

<div class="label">
Pérdida media
</div>

<div class="value negative">

${{
'%.2f'
|format(
stats.avg_loss
)
}}

</div>

</div>


<div class="stat">

<div class="label">
Drawdown máximo
</div>

<div class="value negative">

${{
'%.2f'
|format(
stats.max_drawdown
)
}}

</div>

</div>


<div class="stat">

<div class="label">
Racha actual
</div>

<div class="value">
{{ stats.streak }}
</div>

</div>


<div class="stat">

<div class="label">
LONG
</div>

<div class="value">

{{ stats.long_count }}
·
${{
'%.2f'
|format(
stats.long_pnl
)
}}

</div>

</div>


<div class="stat">

<div class="label">
SHORT
</div>

<div class="value">

{{ stats.short_count }}
·
${{
'%.2f'
|format(
stats.short_pnl
)
}}

</div>

</div>


<div class="stat">

<div class="label">
Comisiones
</div>

<div class="value">

${{
'%.2f'
|format(
stats.fees
)
}}

</div>

</div>


<div class="stat">

<div class="label">
Funding
</div>

<div class="value">

${{
'%.2f'
|format(
stats.funding
)
}}

</div>

</div>

</div>


<div class="curvebox">

<svg
id="curve"
viewBox="0 0 1000 170"
preserveAspectRatio="none"
>
</svg>

</div>

</section>


<section class="card">

<h2>
Periodo
</h2>

<form
class="row"
method="get"
action="/control"
>

<input
type="hidden"
name="secret"
value="{{ secret }}"
>


<select name="period">

<option
value="all"
{{ 'selected' if period=='all' }}
>
Historial completo
</option>

<option
value="last_month"
{{ 'selected' if period=='last_month' }}
>
Últimos 30 días
</option>

<option
value="last_3_months"
{{ 'selected' if period=='last_3_months' }}
>
Últimos 3 meses
</option>

<option
value="specific_month"
{{ 'selected' if period=='specific_month' }}
>
Mes específico
</option>

</select>


<input
type="month"
name="month"
value="{{ month }}"
>


<button class="blue">
VER
</button>

</form>

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


<select
name="side"
required
>

<option value="LONG">
LONG
</option>

<option value="SHORT">
SHORT
</option>

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
name="gross_pnl"
type="number"
step="any"
placeholder="PnL bruto opcional"
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
Importar archivo JSON
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
AGREGAR OPERACIONES
</option>

<option value="replace">
REEMPLAZAR HISTORIAL
</option>

</select>


<button class="blue">
IMPORTAR
</button>

</form>

<p class="muted">

AGREGAR conserva el historial
y omite los ID repetidos.

</p>

</section>


<section class="buttons">


<a
class="btn close"
href="/download?secret={{ secret }}&period={{ period }}&month={{ month }}"
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


<section class="card table-wrap">

<table>

<thead>

<tr>

<th>Cierre</th>
<th>Lado</th>
<th>Origen</th>
<th>Entrada</th>
<th>Salida</th>
<th>Movimiento</th>
<th>Margen</th>
<th>Fees</th>
<th>Funding</th>
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

<span class="badge">
{{ t.source }}
</span>

</td>

<td>
{{ '%.2f'|format(t.entry_price) }}
</td>

<td>
{{ '%.2f'|format(t.exit_price) }}
</td>

<td>
{{ '%.3f'|format(t.price_move_pct) }}%
</td>

<td>
${{ '%.2f'|format(t.margin_used) }}
</td>

<td>
${{ '%.2f'|format(t.total_fees) }}
</td>

<td>
${{ '%.2f'|format(t.funding) }}
</td>

<td
class="{{
'positive'
if t.net_pnl>=0
else
'negative'
}}"
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
colspan="12"
class="muted center"
>

Todavía no hay operaciones.

</td>

</tr>

{% endfor %}

</tbody>

</table>

</section>


<script>

const values =
{{ stats.curve|tojson }};

const svg =
document.getElementById(
"curve"
);

if(values.length){

const minimum =
Math.min(
0,
...values
);

const maximum =
Math.max(
0,
...values
);

const range =
(maximum-minimum)||1;

const zeroY =
150
-
(
(0-minimum)
/
range
*
130
);

let points="";

values.forEach(
(value,index)=>{

const x =
values.length===1
?
500
:
index
/
(values.length-1)
*
1000;

const y =
150
-
(
(value-minimum)
/
range
*
130
);

points +=
`${x},${y} `;

}
);

svg.innerHTML =
`
<line
x1="0"
y1="${zeroY}"
x2="1000"
y2="${zeroY}"
stroke="#333"
stroke-width="2"
/>

<polyline
points="${points}"
fill="none"
stroke="#fff"
stroke-width="4"
vector-effect="non-scaling-stroke"
/>
`;

}

</script>

</main>

</body>

</html>
"""


# ============================================================
# AUTORIZACIÓN DEL PANEL
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
            sorted(
                TV_SYMBOLS
            ),

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

    period = request.args.get(
        "period",
        "all",
    )

    month = request.args.get(
        "month",
        "",
    )

    live = None
    warning = ""
    active_source = "—"

    try:

        sync = (
            sync_position()
        )

        if (
            sync.get(
                "reason"
            )
            ==
            "multiple_positions"
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

            if live:
                active_source = str(
                    live.get(
                        "source",
                        "BINGX",
                    )
                )

    except Exception as exc:

        warning = (
            "Error sincronizando "
            f"BingX: {exc}"
        )

    trades = filter_trades(
        store.get_trades(),
        period,
        month,
    )

    stats = trade_summary(
        trades
    )

    return render_template_string(
        PANEL_HTML,

        secret=
        request.args[
            "secret"
        ],

        mode=
        store.get_mode(),

        modes=
        VALID_MODES,

        leverage=
        LEVERAGE,

        balance_percent=
        BALANCE_PERCENT,

        live=
        live,

        active_source=
        active_source,

        sync_warning=
        warning,

        period=
        period,

        month=
        month,

        trades=
        list(
            reversed(
                trades
            )
        ),

        stats=
        stats,
    )


# ============================================================
# CAMBIO DE MODO
# ============================================================

@app.post(
    "/setmode/<mode>"
)
def set_mode(
    mode,
):
    if not control_authorized():
        return (
            "Clave de panel inválida",
            403,
        )

    mode = (
        mode.upper()
    )

    if mode not in VALID_MODES:

        return jsonify(
            {
                "error":
                "Modo inválido"
            }
        ), 400

    store.set_mode(
        mode
    )

    notify(
        f"{BOT_NAME}: "
        f"modo cambiado a "
        f"{mode}"
    )

    return redirect(
        "/control?secret="
        f"{request.args['secret']}"
    )


# ============================================================
# SINCRONIZAR
# ============================================================

@app.post("/sync")
def sync_route():
    if not control_authorized():
        return (
            "Clave de panel inválida",
            403,
        )

    try:

        sync_position()

    except Exception as exc:

        notify(
            f"ERROR SYNC "
            f"{BOT_NAME}: "
            f"{exc}"
        )

    return redirect(
        "/control?secret="
        f"{request.args['secret']}"
    )


# ============================================================
# OPERACIÓN MANUAL
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

    gross_input = (
        request.form.get(
            "gross_pnl"
        )
    )

    net_input = (
        request.form.get(
            "net_pnl"
        )
    )

    if gross_input not in {
        None,
        "",
    }:

        raw[
            "gross_pnl"
        ] = fnum(
            gross_input,
            0,
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

    trade = normalize_trade(
        raw
    )

    store.append_trade(
        trade
    )

    return redirect(
        "/control?secret="
        f"{request.args['secret']}"
    )


# ============================================================
# IMPORTAR JSON
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
            f"JSON inválido: "
            f"{exc}",
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

    if (
        import_mode
        == "replace"
    ):

        store.set_trades(
            normalized
        )

    else:

        existing = (
            store.get_trades()
        )

        existing_ids = {
            str(
                trade.get(
                    "id"
                )
            )
            for trade in existing
            if trade.get(
                "id"
            )
        }

        for trade in normalized:

            trade_id = str(
                trade.get(
                    "id"
                )
            )

            if (
                trade_id
                in existing_ids
            ):
                continue

            existing.append(
                trade
            )

            existing_ids.add(
                trade_id
            )

        store.set_trades(
            existing
        )

    return redirect(
        "/control?secret="
        f"{request.args['secret']}"
    )


# ============================================================
# DESCARGAR CSV
# ============================================================

@app.get("/download")
def download():
    if not control_authorized():
        return (
            "Clave de panel inválida",
            403,
        )

    period = request.args.get(
        "period",
        "all",
    )

    month = request.args.get(
        "month",
        "",
    )

    trades = filter_trades(
        store.get_trades(),
        period,
        month,
    )

    filename = (
        f"btc_trades_"
        f"{period}"
    )

    if month:
        filename += (
            f"_{month}"
        )

    filename += ".csv"

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
        filename,
    )


# ============================================================
# EXPORTAR JSON
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

    payload = {
        "app":
        "Jonathan Trader · "
        "BOT BTC",

        "version":
        1,

        "exportedAt":
        utc_now(),

        "trades":
        store.get_trades(),
    }

    raw = json.dumps(
        payload,
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
# WEBHOOK TRADINGVIEW
# ============================================================

@app.post("/webhook")
def webhook():
    payload = (
        request.get_json(
            silent=True
        )
        or {}
    )

    if not secret_matches(
        payload.get(
            "secret"
        ),
        WEBHOOK_SECRET,
    ):

        return jsonify(
            {
                "error":
                "Clave de webhook "
                "inválida"
            }
        ), 403

    side = str(
        payload.get(
            "side"
        )
        or payload.get(
            "action"
        )
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

        return jsonify(
            {
                "error":
                "Usa BUY o SELL"
            }
        ), 400

    if (
        symbol not in TV_SYMBOLS
        and symbol
        != BINGX_SYMBOL.upper()
    ):

        return jsonify(
            {
                "error":
                "Símbolo BTC "
                "no permitido",

                "received":
                symbol,
            }
        ), 400

    if timeframe not in {
        "5",
        "5m",
        "05",
        "05m",
    }:

        return jsonify(
            {
                "error":
                "Solo se aceptan "
                "señales 5M",

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

        return jsonify(
            {
                "ok":
                True,

                "result":
                result,
            }
        )

    except Exception as exc:

        notify(
            f"ERROR "
            f"{BOT_NAME}: "
            f"{exc}"
        )

        return jsonify(
            {
                "ok":
                False,

                "error":
                str(
                    exc
                ),
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
            str(
                exc
            ),
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