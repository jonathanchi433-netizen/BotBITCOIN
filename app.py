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


def env_bool(name, default=False):
    value = os.getenv(name, str(default)).strip().lower()
    return value in {"1", "true", "yes", "on"}


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
    os.getenv("BINGX_API_SECRET", "").strip()
    or os.getenv("BINGX_SECRET_KEY", "").strip()
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
    os.getenv("BALANCE_PERCENT", "90")
)

LEVERAGE = int(
    os.getenv("LEVERAGE", "2")
)

FEE_RATE = float(
    os.getenv("FEE_RATE", "0.0005")
)

QTY_STEP = float(
    os.getenv("QTY_STEP", "0.0001")
)

MIN_QTY = float(
    os.getenv("MIN_QTY", "0.0001")
)

POSITION_MODE = os.getenv(
    "POSITION_MODE",
    "HEDGE",
).strip().upper()

# FALSE = REAL
DRY_RUN = env_bool(
    "DRY_RUN",
    False,
)

DRY_BALANCE = float(
    os.getenv("DRY_BALANCE", "1000")
)

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


def utc_now():
    return datetime.now(
        timezone.utc
    ).isoformat()
def fnum(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default

def secret_matches(
    received,
    expected,
):
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
            (
                "https://api.telegram.org/bot"
                f"{TELEGRAM_BOT_TOKEN}/sendMessage"
            ),
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message[:3900],
            },
            timeout=10,
        )
    except Exception:
        pass


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

    def _key(
        self,
        name,
    ):
        return (
            f"{STATE_PREFIX}:{name}"
        )

    def _path(
        self,
        name,
    ):
        return os.path.join(
            DATA_DIR,
            f"{STATE_PREFIX}_{name}.json",
        )

    def _redis(
        self,
        command,
    ):
        response = requests.post(
            UPSTASH_REDIS_REST_URL.rstrip("/"),
            headers={
                "Authorization": (
                    "Bearer "
                    f"{UPSTASH_REDIS_REST_TOKEN}"
                ),
                "Content-Type": (
                    "application/json"
                ),
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
                "Error de almacenamiento: "
                f"{payload}"
            )

        return payload.get("result")

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
                        self._key(name),
                    ]
                )

                return (
                    default
                    if not raw
                    else json.loads(raw)
                )

            path = self._path(name)

            if not os.path.exists(path):
                return default

            with open(
                path,
                "r",
                encoding="utf-8",
            ) as handle:
                return json.load(handle)

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
                "mode": mode,
                "updated_at": utc_now(),
            },
        )

    def get_active_trade(self):

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

            self.set(
                "trades",
                trades,
            )


store = Store()


# ============================================================
# BINGX
# ============================================================
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

    quantity = (
        math.floor(
            (value + 1e-12)
            / step
        )
        * step
    )

    return round(
        quantity,
        decimals,
    )


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

            payload = (
                response.json()
            )

        except Exception:

            raise RuntimeError(
                "BingX respondió algo "
                "inválido: "
                f"{response.text[:300]}"
            )

        if str(
            payload.get("code")
        ) != "0":

            raise RuntimeError(
                f"Error BingX: {payload}"
            )

        return payload

    def price(self):

        payload = self._request(
            "GET",
            "/openApi/swap/v2/quote/price",
            {
                "symbol":
                BINGX_SYMBOL
            },
            private=False,
        )

        data = payload.get(
            "data",
            {},
        )

        return float(
            data.get("price")
            or data.get("lastPrice")
        )

    def available_balance(self):

        if (
            DRY_RUN
            and not (
                BINGX_API_KEY
                and BINGX_API_SECRET
            )
        ):
            return DRY_BALANCE

        payload = self._request(
            "GET",
            "/openApi/swap/v2/user/balance",
        )

        data = payload.get(
            "data",
            {},
        )

        if isinstance(
            data,
            dict,
        ):

            data = data.get(
                "balance",
                data,
            )

        elif isinstance(
            data,
            list,
        ):

            data = (
                data[0]
                if data
                else {}
            )

        return float(
            data.get(
                "availableMargin"
            )
            or data.get(
                "availableBalance"
            )
            or data.get(
                "balance"
            )
            or 0
        )

    def contract_rules(self):

        step = QTY_STEP
        minimum = MIN_QTY

        try:

            payload = self._request(
                "GET",
                (
                    "/openApi/swap/"
                    "v2/quote/contracts"
                ),
                private=False,
            )

            items = payload.get(
                "data",
                [],
            )

            if isinstance(
                items,
                dict,
            ):

                items = items.get(
                    "contracts",
                    items.get(
                        "data",
                        [],
                    ),
                )

            for item in (
                items or []
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

                minimum = float(
                    item.get(
                        "tradeMinQuantity"
                    )
                    or item.get(
                        "minQty"
                    )
                    or item.get(
                        "minQuantity"
                    )
                    or minimum
                )

                break

        except Exception:
            pass

        return (
            step,
            minimum,
        )

    def set_isolated(self):

        if DRY_RUN:
            return

        try:

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

        except Exception as exc:

            text = str(
                exc
            ).lower()

            if not any(
                x in text
                for x in [
                    "already",
                    "same",
                    "no need",
                    "unchanged",
                ]
            ):
                raise

    def set_leverage(self):

        if DRY_RUN:
            return

        for side in (
            "LONG",
            "SHORT",
        ):

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
                f"Cantidad {quantity} "
                "menor que el mínimo "
                f"{minimum}. "
                "Balance disponible: "
                f"{balance:.2f} USDT"
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
            "qty_step":
            step,
            "min_qty":
            minimum,
        }

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
            float(quantity),
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

        if DRY_RUN:

            return {
                "order_id":
                (
                    "dry-"
                    f"{int(time.time()*1000)}"
                ),
                "price":
                reference_price,
                "quantity":
                quantity,
            }

        payload = self._request(
            "POST",
            (
                "/openApi/swap/"
                "v2/trade/order"
            ),
            params,
        )

        order = (
            payload
            .get(
                "data",
                {},
            )
            .get(
                "order",
                payload.get(
                    "data",
                    {},
                ),
            )
        )

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
                        check
                        .get(
                            "data",
                            {},
                        )
                        .get(
                            "order",
                            check.get(
                                "data",
                                {},
                            ),
                        )
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

        price = float(
            details.get(
                "avgPrice"
            )
            or details.get(
                "price"
            )
            or reference_price
        )

        executed = float(
            details.get(
                "executedQty"
            )
            or details.get(
                "quantity"
            )
            or quantity
        )

        return {
            "order_id":
            order_id,
            "price":
            price,
            "quantity":
            executed,
        }

    def positions(self):

        if (
            DRY_RUN
            and not (
                BINGX_API_KEY
                and BINGX_API_SECRET
            )
        ):

            trade = (
                store
                .get_active_trade()
            )

            return (
                [trade]
                if trade
                else []
            )

        payload = self._request(
            "GET",
            (
                "/openApi/swap/"
                "v2/user/positions"
            ),
            {
                "symbol":
                BINGX_SYMBOL
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

            data = data.get(
                "positions",
                data.get(
                    "data",
                    [data],
                ),
            )

        result = []

        for item in (
            data or []
        ):

            raw_amt = float(
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
                )
                or 0
            )

            side = str(
                item.get(
                    "positionSide"
                )
                or ""
            ).upper()

            if side not in {
                "LONG",
                "SHORT",
            }:

                if raw_amt > 0:
                    side = "LONG"

                elif raw_amt < 0:
                    side = "SHORT"

                else:
                    side = ""

            qty = abs(
                raw_amt
            )

            if (
                qty <= 0
                or side
                not in {
                    "LONG",
                    "SHORT",
                }
            ):
                continue

            entry = float(
                item.get(
                    "avgPrice"
                )
                or item.get(
                    "averagePrice"
                )
                or item.get(
                    "entryPrice"
                )
                or 0
            )

            leverage = int(
                float(
                    item.get(
                        "leverage"
                    )
                    or LEVERAGE
                )
            )

            margin = float(
                item.get(
                    "isolatedMargin"
                )
                or item.get(
                    "margin"
                )
                or (
                    entry
                    * qty
                    / leverage
                    if leverage
                    else 0
                )
                or 0
            )

            upnl = float(
                item.get(
                    "unrealizedProfit"
                )
                or item.get(
                    "unrealizedPnl"
                )
                or item.get(
                    "unRealizedProfit"
                )
                or 0
            )

            result.append(
                {
                    "side":
                    side,
                    "quantity":
                    qty,
                    "entry_price":
                    entry,
                    "leverage":
                    leverage,
                    "margin_used":
                    margin,
                    "unrealized_pnl":
                    upnl,
                    "raw":
                    item,
                }
            )

        return result


bingx = BingX()


# ============================================================
# ESTADÍSTICAS
# ============================================================
def parse_time(
    value,
):

    if not value:

        return datetime.min.replace(
            tzinfo=timezone.utc
        )

    try:

        return datetime.fromisoformat(
            str(value).replace(
                "Z",
                "+00:00",
            )
        )

    except Exception:

        return datetime.min.replace(
            tzinfo=timezone.utc
        )


def fnum(
    value,
    default=0.0,
):

    try:
        return float(value)

    except Exception:
        return default


def normalize_trade(
    raw,
):

    trade = dict(
        raw or {}
    )

    side = str(
        trade.get("side")
        or trade.get(
            "direction"
        )
        or ""
    ).upper()

    entry = fnum(
        trade.get(
            "entry_price",
            trade.get(
                "entry",
                0,
            ),
        )
    )

    exit_price = fnum(
        trade.get(
            "exit_price",
            trade.get(
                "exit",
                0,
            ),
        )
    )

    qty = fnum(
        trade.get(
            "quantity",
            trade.get(
                "qty",
                0,
            ),
        )
    )

    leverage = int(
        fnum(
            trade.get(
                "leverage",
                LEVERAGE,
            ),
            LEVERAGE,
        )
    )

    margin = fnum(
        trade.get(
            "margin_used",
            trade.get(
                "margin",
                0,
            ),
        )
    )

    risk = fnum(
        trade.get(
            "risk",
            0,
        )
    )

    fees = fnum(
        trade.get(
            "total_fees",
            trade.get(
                "fees",
                0,
            ),
        )
    )

    funding = fnum(
        trade.get(
            "funding",
            0,
        )
    )

    opened_at = (
        trade.get(
            "opened_at"
        )
        or trade.get(
            "open_time"
        )
        or trade.get(
            "date"
        )
        or utc_now()
    )

    closed_at = (
        trade.get(
            "closed_at"
        )
        or trade.get(
            "close_time"
        )
        or opened_at
    )

    sign = (
        1
        if side == "LONG"
        else -1
    )

    if (
        entry
        and exit_price
        and qty
    ):

        gross_calc = (
            (
                exit_price
                - entry
            )
            * qty
            * sign
        )

    else:
        gross_calc = 0

    gross = fnum(
        trade.get(
            "gross_pnl",
            trade.get(
                "grossOverride",
                gross_calc,
            ),
        ),
        gross_calc,
    )

    net = fnum(
        trade.get(
            "net_pnl",
            (
                gross
                - fees
                - funding
            ),
        ),
        (
            gross
            - fees
            - funding
        ),
    )

    if (
        margin <= 0
        and entry
        and qty
        and leverage
    ):

        margin = (
            entry
            * qty
            / leverage
        )

    if (
        entry
        and exit_price
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

    else:

        price_move = fnum(
            trade.get(
                "price_move_pct",
                0,
            )
        )

    balance_impact = fnum(
        trade.get(
            "balance_impact_pct",
            0,
        )
    )

    if risk > 0:

        r_multiple = (
            net
            / risk
        )

    else:

        r_multiple = fnum(
            trade.get(
                "r_multiple",
                0,
            )
        )

    return {
        "id":
        str(
            trade.get("id")
            or uuid.uuid4()
        ),
        "opened_at":
        str(opened_at),
        "closed_at":
        str(closed_at),
        "side":
        (
            side
            if side in {
                "LONG",
                "SHORT",
            }
            else "LONG"
        ),
        "symbol":
        str(
            trade.get(
                "symbol"
            )
            or BINGX_SYMBOL
        ),
        "quantity":
        qty,
        "entry_price":
        entry,
        "exit_price":
        exit_price,
        "leverage":
        leverage,
        "margin_used":
        margin,
        "risk":
        risk,
        "price_move_pct":
        price_move,
        "gross_pnl":
        gross,
        "total_fees":
        fees,
        "funding":
        funding,
        "net_pnl":
        net,
        "balance_impact_pct":
        balance_impact,
        "r_multiple":
        r_multiple,
        "close_reason":
        str(
            trade.get(
                "close_reason"
            )
            or trade.get(
                "reason"
            )
            or "manual/import"
        ),
        "source":
        str(
            trade.get(
                "source"
            )
            or (
                "AUTO"
                if trade.get(
                    "plan"
                )
                is not False
                else "MANUAL"
            )
        ),
        "notes":
        str(
            trade.get(
                "notes"
            )
            or ""
        ),
        "open_order_id":
        trade.get(
            "open_order_id"
        ),
        "close_order_id":
        trade.get(
            "close_order_id"
        ),
    }


def filter_trades(
    trades,
    period="all",
    month="",
):

    now = datetime.now(
        timezone.utc
    )

    if period == "last_month":

        start = (
            now
            - timedelta(
                days=30
            )
        )

        return [
            t
            for t in trades
            if parse_time(
                t.get(
                    "closed_at"
                )
            )
            >= start
        ]

    if period == "last_3_months":

        start = (
            now
            - timedelta(
                days=90
            )
        )

        return [
            t
            for t in trades
            if parse_time(
                t.get(
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
            t
            for t in trades
            if str(
                t.get(
                    "closed_at",
                    "",
                )
            )[:7]
            == month
        ]

    return list(
        trades
    )


def trade_summary(
    trades,
):

    ordered = sorted(
        trades,
        key=lambda x: x.get(
            "closed_at",
            "",
        ),
    )

    pnls = [
        fnum(
            t.get(
                "net_pnl"
            )
        )
        for t in ordered
    ]

    wins_list = [
        x
        for x in pnls
        if x > 0.0000001
    ]

    losses_list = [
        x
        for x in pnls
        if x < -0.0000001
    ]

    be_count = (
        len(pnls)
        - len(wins_list)
        - len(losses_list)
    )

    gains = sum(
        wins_list
    )

    losses_abs = abs(
        sum(
            losses_list
        )
    )

    net = sum(
        pnls
    )

    gross = sum(
        fnum(
            t.get(
                "gross_pnl"
            )
        )
        for t in ordered
    )

    fees = sum(
        fnum(
            t.get(
                "total_fees"
            )
        )
        for t in ordered
    )

    funding = sum(
        fnum(
            t.get(
                "funding"
            )
        )
        for t in ordered
    )

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    curve = []

    for t in ordered:

        equity += fnum(
            t.get(
                "net_pnl"
            )
        )

        peak = max(
            peak,
            equity,
        )

        max_dd = max(
            max_dd,
            peak - equity,
        )

        curve.append(
            round(
                equity,
                8,
            )
        )

    current_streak = 0
    streak_type = "BE"

    for pnl in reversed(
        pnls
    ):

        if pnl > 0.0000001:
            typ = "W"

        elif pnl < -0.0000001:
            typ = "L"

        else:
            typ = "BE"

        if current_streak == 0:

            streak_type = typ
            current_streak = 1

        elif typ == streak_type:

            current_streak += 1

        else:
            break

    r_values = [
        fnum(
            t.get(
                "r_multiple"
            )
        )
        for t in ordered
        if fnum(
            t.get(
                "risk"
            )
        )
        > 0
    ]

    long_trades = [
        t
        for t in ordered
        if t.get(
            "side"
        )
        == "LONG"
    ]

    short_trades = [
        t
        for t in ordered
        if t.get(
            "side"
        )
        == "SHORT"
    ]

    return {
        "trades":
        len(ordered),

        "wins":
        len(wins_list),

        "losses":
        len(losses_list),

        "be":
        be_count,

        "winrate":
        (
            len(wins_list)
            /
            (
                len(wins_list)
                + len(losses_list)
            )
            * 100
            if (
                wins_list
                or losses_list
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

        "profit_factor":
        (
            gains
            / losses_abs
            if losses_abs
            else (
                999
                if gains
                else 0
            )
        ),

        "biggest_win":
        max(
            pnls,
            default=0,
        ),

        "biggest_loss":
        min(
            pnls,
            default=0,
        ),

        "avg_win":
        (
            sum(
                wins_list
            )
            / len(
                wins_list
            )
            if wins_list
            else 0
        ),

        "avg_loss":
        (
            sum(
                losses_list
            )
            / len(
                losses_list
            )
            if losses_list
            else 0
        ),

        "max_drawdown_usd":
        max_dd,

        "current_streak":
        (
            f"{current_streak} "
            f"{streak_type}"
            if ordered
            else "0"
        ),

        "long_count":
        len(
            long_trades
        ),

        "long_pnl":
        sum(
            fnum(
                t.get(
                    "net_pnl"
                )
            )
            for t in long_trades
        ),

        "short_count":
        len(
            short_trades
        ),

        "short_pnl":
        sum(
            fnum(
                t.get(
                    "net_pnl"
                )
            )
            for t
            in short_trades
        ),

        "r_global":
        sum(
            r_values
        ),

        "curve":
        curve,
    }


def csv_bytes(
    trades,
):

    fields = [
        "id",
        "opened_at",
        "closed_at",
        "side",
        "symbol",
        "quantity",
        "entry_price",
        "exit_price",
        "leverage",
        "margin_used",
        "risk",
        "price_move_pct",
        "gross_pnl",
        "total_fees",
        "funding",
        "net_pnl",
        "r_multiple",
        "close_reason",
        "source",
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
# SINCRONIZACIÓN
# ============================================================
def active_trade():

    return (
        store
        .get_active_trade()
    )


def sync_position():

    positions = (
        bingx.positions()
    )

    stored = (
        store
        .get_active_trade()
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

            notify(
                f"{BOT_NAME}: "
                "BingX está FLAT. "
                "Se limpió la posición "
                "registrada."
            )

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
                bingx
                .available_balance()
            )

        except Exception:
            pass

        adopted = {
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
            live.get(
                "leverage",
                LEVERAGE,
            ),

            "balance_before":
            balance,

            "margin_used":
            live.get(
                "margin_used",
                0,
            ),

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
            adopted
        )

        stored = adopted

        notify(
            f"{BOT_NAME}: "
            "posición manual "
            "detectada y adoptada: "
            f"{live['side']}"
        )

    else:

        changed = (
            stored.get(
                "side"
            )
            != live.get(
                "side"
            )
            or abs(
                fnum(
                    stored.get(
                        "quantity"
                    )
                )
                -
                fnum(
                    live.get(
                        "quantity"
                    )
                )
            )
            > 1e-12
            or abs(
                fnum(
                    stored.get(
                        "entry_price"
                    )
                )
                -
                fnum(
                    live.get(
                        "entry_price"
                    )
                )
            )
            > 1e-12
        )

        if changed:

            stored = {
                **stored,

                "side":
                live[
                    "side"
                ],

                "quantity":
                live[
                    "quantity"
                ],

                "entry_price":
                live[
                    "entry_price"
                ],

                "leverage":
                live.get(
                    "leverage",
                    stored.get(
                        "leverage",
                        LEVERAGE,
                    ),
                ),

                "margin_used":
                live.get(
                    "margin_used",
                    stored.get(
                        "margin_used",
                        0,
                    ),
                ),

                "source":
                stored.get(
                    "source",
                    "SYNC",
                ),
            }

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
# OPERACIONES
# ============================================================
def open_trade(
    direction,
):

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
        sync.get(
            "reason"
        )
        ==
        "multiple_positions"
    ):

        return {
            "status":
            "skipped",
            "reason":
            "multiple_positions",
        }

    calculation = (
        bingx.quantity()
    )

    bingx.set_isolated()

    bingx.set_leverage()

    side = (
        "BUY"
        if direction == "LONG"
        else "SELL"
    )

    fill = (
        bingx.market_order(
            side,
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
        "Margen aprox: "
        f"{calculation['margin']:.2f} "
        "USDT\n"
        f"Apalancamiento: "
        f"{LEVERAGE}x | ISOLATED"
    )

    return {
        "status":
        "opened",
        "trade":
        trade,
    }


def close_trade(
    reason,
):

    sync = sync_position()

    trade = (
        store
        .get_active_trade()
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

    direction = live[
        "side"
    ]

    side = (
        "SELL"
        if direction == "LONG"
        else "BUY"
    )

    qty_to_close = (
        live.get(
            "quantity"
        )
        or trade.get(
            "quantity"
        )
    )

    fill = (
        bingx.market_order(
            side,
            direction,
            qty_to_close,
            closing=True,
        )
    )

    exit_price = fill[
        "price"
    ]

    quantity = min(
        float(
            fill[
                "quantity"
            ]
        ),
        float(
            qty_to_close
        ),
    )

    entry_price = fnum(
        live.get(
            "entry_price"
        )
        or trade.get(
            "entry_price"
        )
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

    exit_fee = (
        exit_price
        * quantity
        * FEE_RATE
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

    total_fees = (
        entry_fee
        + exit_fee
    )

    net = (
        gross
        - total_fees
    )

    margin = fnum(
        trade.get(
            "margin_used"
        )
        or live.get(
            "margin_used"
        )
    )

    balance = fnum(
        trade.get(
            "balance_before"
        )
    )

    if entry_price:

        price_move = (
            (
                exit_price
                / entry_price
                - 1
            )
            * sign
            * 100
        )

    else:
        price_move = 0

    closed = (
        normalize_trade(
            {
                **trade,

                "closed_at":
                utc_now(),

                "side":
                direction,

                "entry_price":
                entry_price,

                "exit_price":
                exit_price,

                "quantity":
                quantity,

                "margin_used":
                margin,

                "price_move_pct":
                price_move,

                "gross_pnl":
                gross,

                "total_fees":
                total_fees,

                "net_pnl":
                net,

                "balance_impact_pct":
                (
                    net
                    / balance
                    * 100
                    if balance
                    else 0
                ),

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
    )

    store.append_trade(
        closed
    )

    store.clear_active_trade()

    notify(
        f"CIERRE {direction} "
        f"{BOT_NAME}\n"
        "Movimiento: "
        f"{price_move:.3f}%\n"
        "PNL neto aprox: "
        f"{net:.2f} USDT"
    )

    return {
        "status":
        "closed",
        "trade":
        closed,
    }


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

        trade = (
            store
            .get_active_trade()
        )

        current_side = (
            trade.get(
                "side"
            )
            if trade
            else None
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

            current_side = None

        elif (
            side == "SELL"
            and current_side
            == "LONG"
        ):

            result[
                "closed"
            ] = close_trade(
                "opposite_sell_signal_5m"
            )

            current_side = None

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
BOT BTC 5M
</title>

<style>

:root{
--bg:#080808;
--card:#171717;
--card2:#101010;
--muted:#999;
--green:#00e889;
--red:#ff4b6a;
--blue:#2f7dff;
--gold:#d9a900;
--line:#2a2a2a
}

*{
box-sizing:border-box
}

body{
margin:0;
padding:16px;
background:var(--bg);
color:#fff;
font-family:Arial,sans-serif
}

main{
max-width:1200px;
margin:auto
}

h1{
text-align:center;
font-size:30px;
margin:8px 0 18px
}

.card{
background:var(--card);
padding:16px;
border-radius:17px;
margin:13px 0;
border:1px solid #222
}

.mode{
text-align:center;
font-size:36px;
font-weight:900;
color:var(--green)
}

.muted{
color:var(--muted);
font-size:12px
}

.center{
text-align:center
}

.buttons,
.grid{
display:grid;
grid-template-columns:
repeat(auto-fit,minmax(145px,1fr));
gap:10px
}

.buttons form{
margin:0
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
text-align:center;
display:block
}

.off{
background:#555
}

.long{
background:#078d49
}

.short{
background:#aa1730
}

.close{
background:#155cb6
}

.both{
background:#b18b00
}

.blue{
background:#2f7dff
}

.dark{
background:#333
}

.stat{
background:var(--card2);
padding:13px;
border-radius:13px;
text-align:center
}

.label{
color:var(--muted);
font-size:11px
}

.value{
font-size:20px;
font-weight:900;
margin-top:5px
}

.positive{
color:var(--green)
}

.negative{
color:var(--red)
}

form.row{
display:grid;
grid-template-columns:
repeat(auto-fit,minmax(145px,1fr));
gap:9px
}

input,
select,
textarea{
width:100%;
background:#0c0c0c;
color:#fff;
border:1px solid #3a3a3a;
border-radius:9px;
padding:11px
}

textarea{
min-height:72px
}

.table-wrap{
overflow:auto
}

table{
width:100%;
border-collapse:collapse;
font-size:12px;
white-space:nowrap
}

th,
td{
padding:9px 7px;
border-bottom:1px solid var(--line);
text-align:right
}

th:first-child,
td:first-child{
text-align:left
}

.badge{
display:inline-block;
padding:4px 8px;
border-radius:999px;
background:#252525;
font-size:11px
}

.live{
border:1px solid #285
}

.warn{
color:#ffd36a
}

.curve{
width:100%;
height:160px;
background:#0b0b0b;
border-radius:12px;
overflow:hidden
}

#equity{
width:100%;
height:100%
}

@media(max-width:650px){

h1{
font-size:25px
}

.mode{
font-size:30px
}

}

</style>
</head>

<body>

<main>

<h1>
₿ BOT BTC · 5M · {{ leverage }}x
</h1>


<section class="card">

<div class="muted center">
Modo actual
</div>

<div class="mode">
{{ mode }}
</div>

<div class="muted center">

{{ 'SIMULACIÓN' if dry_run else 'OPERACIÓN REAL' }}

· {{ balance_percent }}% balance

· ISOLATED

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
gap:10px;
align-items:center
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
{{ '%.2f'|format(live.entry_price) }}
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
Apalancamiento
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
${{ '%.2f'|format(live.margin_used) }}
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

${{ '%.2f'|format(live.unrealized_pnl) }}

</div>

</div>

</div>

{% else %}

<p class="muted center">
FLAT · Sin posición abierta en BTC-USDT
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

${{ '%.2f'|format(stats.net_pnl) }}

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
${{ '%.2f'|format(stats.biggest_win) }}
</div>

</div>


<div class="stat">

<div class="label">
Mayor pérdida
</div>

<div class="value negative">
${{ '%.2f'|format(stats.biggest_loss) }}
</div>

</div>


<div class="stat">

<div class="label">
Ganancia media
</div>

<div class="value positive">
${{ '%.2f'|format(stats.avg_win) }}
</div>

</div>


<div class="stat">

<div class="label">
Pérdida media
</div>

<div class="value negative">
${{ '%.2f'|format(stats.avg_loss) }}
</div>

</div>


<div class="stat">

<div class="label">
Drawdown máx
</div>

<div class="value negative">
${{ '%.2f'|format(stats.max_drawdown_usd) }}
</div>

</div>


<div class="stat">

<div class="label">
Racha actual
</div>

<div class="value">
{{ stats.current_streak }}
</div>

</div>


<div class="stat">

<div class="label">
LONG
</div>

<div class="value">

{{ stats.long_count }}

·

${{ '%.2f'|format(stats.long_pnl) }}

</div>

</div>


<div class="stat">

<div class="label">
SHORT
</div>

<div class="value">

{{ stats.short_count }}

·

${{ '%.2f'|format(stats.short_pnl) }}

</div>

</div>


<div class="stat">

<div class="label">
Comisiones
</div>

<div class="value">
${{ '%.2f'|format(stats.fees) }}
</div>

</div>


<div class="stat">

<div class="label">
Funding
</div>

<div class="value">
${{ '%.2f'|format(stats.funding) }}
</div>

</div>


</div>


<div
class="curve"
style="margin-top:12px"
>

<svg
id="equity"
viewBox="0 0 1000 160"
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
Registrar operación manual
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

<option>
LONG
</option>

<option>
SHORT
</option>

</select>


<input
name="entry_price"
type="number"
step="any"
placeholder="Entrada"
required
>


<input
name="exit_price"
type="number"
step="any"
placeholder="Salida"
required
>


<input
name="quantity"
type="number"
step="any"
placeholder="Cantidad BTC"
required
>


<input
name="margin_used"
type="number"
step="any"
placeholder="Margen USDT"
>


<input
name="risk"
type="number"
step="any"
placeholder="Riesgo $ opcional"
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
AGREGAR OPERACIÓN
</button>

</form>

</section>


<section class="card">

<h2>
Importar JSON
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
accept="application/json,.json"
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
IMPORTAR JSON
</button>

</form>

<p class="muted">
Acepta una lista de operaciones,
{"trades":[...]} o el formato del panel
de divergencias. Al agregar, los ID
repetidos se omiten.
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

<th>
Cierre
</th>

<th>
Lado
</th>

<th>
Origen
</th>

<th>
Entrada
</th>

<th>
Salida
</th>

<th>
Precio %
</th>

<th>
Margen
</th>

<th>
Fees
</th>

<th>
Funding
</th>

<th>
PnL
</th>

<th>
R
</th>

<th>
Motivo
</th>

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
{{ '%.2f'|format(t.r_multiple) }}
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
No hay operaciones
</td>

</tr>

{% endfor %}

</tbody>

</table>

</section>


<script>

const vals =
{{ stats.curve|tojson }};

const svg =
document.getElementById(
'equity'
);

if(vals.length){

const min =
Math.min(
0,
...vals
);

const max =
Math.max(
0,
...vals
);

const range =
(max-min)||1;

const pts =
vals.map(
(v,i)=>
`${vals.length===1?500:i/(vals.length-1)*1000},${145-(v-min)/range*130}`
).join(' ');

svg.innerHTML =
`<line
x1="0"
y1="${145-(0-min)/range*130}"
x2="1000"
y2="${145-(0-min)/range*130}"
stroke="#333"
/>
<polyline
points="${pts}"
fill="none"
stroke="#eee"
stroke-width="4"
vector-effect="non-scaling-stroke"
/>`;

}

</script>

</main>

</body>

</html>
"""


app = Flask(
    __name__
)


def control_authorized():

    return secret_matches(
        request.args.get(
            "secret",
            "",
        ),
        CONTROL_SECRET,
    )


def panel_data(
    period,
    month,
):

    warning = ""
    live = None

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
                "El bot bloqueará nuevas "
                "entradas hasta resolverlo."
            )

        positions = (
            bingx.positions()
        )

        live = (
            positions[0]
            if len(
                positions
            )
            == 1
            else None
        )

    except Exception as exc:

        warning = (
            "No se pudo sincronizar "
            "con BingX: "
            f"{exc}"
        )

    trades = filter_trades(
        store.get_trades(),
        period,
        month,
    )

    return (
        live,
        warning,
        trades,
        trade_summary(
            trades
        ),
    )


@app.get("/")
def home():

    return (
        f"{BOT_NAME} activo | "
        f"REAL={not DRY_RUN} | "
        f"MODE={store.get_mode()}",
        200,
    )


@app.get("/health")
def health():

    return jsonify(
        {
            "status":
            "ok",

            "bot":
            BOT_NAME,

            "symbol":
            BINGX_SYMBOL,

            "accepted_tv_symbols":
            sorted(
                TV_SYMBOLS
            ),

            "timeframe":
            "5m",

            "mode":
            store.get_mode(),

            "dry_run":
            DRY_RUN,

            "balance_percent":
            BALANCE_PERCENT,

            "leverage":
            LEVERAGE,

            "position_mode":
            POSITION_MODE,

            "state_prefix":
            STATE_PREFIX,

            "persistent_redis":
            store.redis_enabled,
        }
    )


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

    (
        live,
        warning,
        trades,
        stats,
    ) = panel_data(
        period,
        month,
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

        dry_run=
        DRY_RUN,

        leverage=
        LEVERAGE,

        balance_percent=
        BALANCE_PERCENT,

        live=
        live,

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

    mode = mode.upper()

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
        "modo cambiado a "
        f"{mode}"
    )

    return redirect(
        "/control?secret="
        f"{request.args['secret']}"
    )


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


@app.post(
    "/manual-trade"
)
def manual_trade():

    if not control_authorized():

        return (
            "Clave de panel inválida",
            403,
        )

    side = str(
        request.form.get(
            "side",
            "LONG",
        )
    ).upper()

    entry = fnum(
        request.form.get(
            "entry_price"
        )
    )

    exit_price = fnum(
        request.form.get(
            "exit_price"
        )
    )

    qty = fnum(
        request.form.get(
            "quantity"
        )
    )

    margin = fnum(
        request.form.get(
            "margin_used"
        )
    )

    risk = fnum(
        request.form.get(
            "risk"
        )
    )

    fees = fnum(
        request.form.get(
            "fees"
        )
    )

    funding = fnum(
        request.form.get(
            "funding"
        )
    )

    notes = str(
        request.form.get(
            "notes",
            "",
        )
    )

    opened = (
        request.form.get(
            "opened_at"
        )
        or utc_now()
    )

    closed = (
        request.form.get(
            "closed_at"
        )
        or utc_now()
    )

    if len(
        opened
    ) == 16:

        opened += (
            ":00+00:00"
        )

    if len(
        closed
    ) == 16:

        closed += (
            ":00+00:00"
        )

    trade = normalize_trade(
        {
            "id":
            str(
                uuid.uuid4()
            ),

            "opened_at":
            opened,

            "closed_at":
            closed,

            "side":
            side,

            "symbol":
            BINGX_SYMBOL,

            "quantity":
            qty,

            "entry_price":
            entry,

            "exit_price":
            exit_price,

            "leverage":
            LEVERAGE,

            "margin_used":
            margin,

            "risk":
            risk,

            "fees":
            fees,

            "funding":
            funding,

            "source":
            "MANUAL",

            "notes":
            notes,

            "close_reason":
            "manual_panel",
        }
    )

    store.append_trade(
        trade
    )

    return redirect(
        "/control?secret="
        f"{request.args['secret']}"
    )


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
            "Formato JSON "
            "no compatible",
            400,
        )

    incoming = [
        normalize_trade(
            item
        )
        for item in incoming
        if isinstance(
            item,
            dict,
        )
    ]

    mode = (
        request.form.get(
            "mode",
            "append",
        )
    )

    if mode == "replace":

        store.set_trades(
            incoming
        )

    else:

        existing = (
            store.get_trades()
        )

        ids = {
            str(
                t.get(
                    "id"
                )
            )
            for t in existing
        }

        for trade in incoming:

            if str(
                trade.get(
                    "id"
                )
            ) not in ids:

                existing.append(
                    trade
                )

                ids.add(
                    str(
                        trade.get(
                            "id"
                        )
                    )
                )

        store.set_trades(
            existing
        )

    return redirect(
        "/control?secret="
        f"{request.args['secret']}"
    )


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
        f"btc_trades_{period}"
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
        BOT_NAME,

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
        symbol
        not in TV_SYMBOLS
        and symbol
        != BINGX_SYMBOL.upper()
    ):

        return jsonify(
            {
                "error":
                "Símbolo BTC inválido",

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
                "señales 5M cerradas",

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
                str(exc),
            }
        ), 500


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
            str(exc),
        }

    return jsonify(
        {
            "checked_at":
            utc_now(),

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