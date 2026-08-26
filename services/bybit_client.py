"""Bybit v5 client (pybit) — klines with pagination (the public API caps at
1000 bars/request; ret_7d needs 2016+ 5m bars) + options chain.

Authenticates when BYBIT_API_KEY/SECRET are set. Order placement lives in the
thin wrappers at the bottom (Track B 2026-08-26) and is only ever called by
services/execution.LiveExecutor — the loop never touches pybit directly."""
from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone

from pybit.exceptions import InvalidRequestError
from pybit.unified_trading import HTTP


RETRY_DELAYS_S = (1, 2, 4)  # 3 attempts total per call


def _with_retry(label: str, fn):
    """Retries a single Bybit call with our own backoff instead of trusting
    pybit's built-in 10006 handler — that handler unconditionally reads
    response.headers["X-Bapi-Limit-Reset-Timestamp"] and Bybit doesn't always
    send that header on a rate-limit response, so pybit raises a bare
    KeyError instead of sleeping+retrying (observed live on Jony
    2026-07-02, ~10x/24h). By the time our own delay elapses the rate-limit
    window has normally reset, so the retry succeeds without ever touching
    pybit's broken branch again."""
    last_err: Exception | None = None
    for attempt, delay in enumerate((0,) + RETRY_DELAYS_S):
        if delay:
            time.sleep(delay)
        try:
            return fn()
        except InvalidRequestError as e:
            # retCode != 0 — a definitive answer from Bybit (bad params,
            # duplicate link id, already filled/cancelled). Retrying would
            # only burn 7 s of tick time (review 2026-08-26 #10).
            print(f"[bybit] {label} rejected: {e}", flush=True)
            return None
        except Exception as e:
            last_err = e
            print(f"[bybit] {label} attempt {attempt + 1}/{len(RETRY_DELAYS_S) + 1} "
                  f"failed: {e}", flush=True)
    print(f"[bybit] {label} gave up after {len(RETRY_DELAYS_S) + 1} attempts: "
          f"{last_err}", flush=True)
    return None


class BybitClient:
    def __init__(self):
        key = os.getenv("BYBIT_API_KEY", "").strip()
        secret = os.getenv("BYBIT_API_SECRET", "").strip()
        testnet = os.getenv("BYBIT_TESTNET", "0").strip() == "1"
        self.has_key = bool(key and secret)
        if self.has_key:
            self.session = HTTP(testnet=testnet, api_key=key, api_secret=secret)
        else:
            self.session = HTTP(testnet=testnet)
        self._tick_cache: dict[str, float] = {}

    def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict]:
        """Oldest→newest candles; paginates when limit > 1000. A page that
        fails all retries stops pagination but keeps whatever earlier pages
        already succeeded (partial history), instead of discarding
        everything — evaluate_conditions already treats a short window as
        not-ready, so a slightly incomplete list degrades gracefully."""
        out: list[dict] = []
        end_ms: int | None = None
        remaining = limit
        while remaining > 0:
            batch = min(remaining, 1000)
            kwargs = dict(category="linear", symbol=symbol,
                          interval=interval, limit=batch)
            if end_ms is not None:
                kwargs["end"] = end_ms
            result = _with_retry(
                f"klines({symbol},{interval})",
                lambda kw=kwargs: self.session.get_kline(**kw)["result"]["list"])
            if result is None:
                break
            raw = result
            if not raw:
                break
            # raw is newest→oldest
            chunk = [{
                "start_ms": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
            } for r in raw]
            out = chunk[::-1] + out
            remaining -= len(chunk)
            end_ms = chunk[-1]["start_ms"] - 1
            if len(chunk) < batch:
                break
        return out[-limit:]

    def get_options_tickers(self, base_coin: str) -> list[dict]:
        """Live options chain with bid/ask/mark, parsed symbol fields."""
        items = _with_retry(
            f"options tickers({base_coin})",
            lambda: self.session.get_tickers(
                category="option", baseCoin=base_coin)["result"]["list"])
        if items is None:
            return []
        out = []
        suffix = os.getenv("JONY_OPTION_SETTLE_SUFFIX", "-USDT")
        for it in items:
            sym = it.get("symbol", "")
            if suffix and not sym.endswith(suffix):
                continue                    # USDT-settled book only (review #7)
            parsed = parse_option_symbol(sym)
            if not parsed:
                continue
            out.append({
                "symbol": it["symbol"],
                "expiry_ms": parsed["expiry_ms"],
                "strike": parsed["strike"],
                "side": parsed["side"],
                "bid": _f(it.get("bid1Price")),
                "ask": _f(it.get("ask1Price")),
                "mark_price": _f(it.get("markPrice")),
                "underlying_price": _f(it.get("underlyingPrice")),
                "delta": _f(it.get("delta")),
                "mark_iv": _f(it.get("markIv")),
            })
        return out

    def get_option_marks(self, base_coin: str) -> dict[str, dict]:
        """symbol → mark/bid/ask + mark_iv/underlying/delta (тот же API-вызов;
        доп. поля пишутся в position_marks — P1 2026-08-17)."""
        return {o["symbol"]: {"mark": o["mark_price"], "bid": o["bid"], "ask": o["ask"],
                              "mark_iv": o["mark_iv"], "underlying": o["underlying_price"],
                              "delta": o["delta"]}
                for o in self.get_options_tickers(base_coin)}


    # ── Execution wrappers (Track B). Each returns None on total failure. ──

    def tick_size(self, symbol: str) -> float:
        """0.0 = unknown this call (not cached — review #11)."""
        if symbol not in self._tick_cache:
            r = _with_retry(f"instruments({symbol})",
                            lambda: self.session.get_instruments_info(
                                category="option", symbol=symbol)["result"]["list"])
            tick = _f(r[0]["priceFilter"]["tickSize"]) if r else 0.0
            if tick > 0:
                self._tick_cache[symbol] = tick
            return tick
        return self._tick_cache[symbol]

    def place_order(self, symbol: str, side: str, qty: float, price: float,
                    link_id: str, reduce_only: bool) -> tuple[str, str | None]:
        """side: 'Sell'|'Buy'. Limit GTC. Returns (outcome, orderId):
        ('ok', id) | ('rejected', None) — Bybit answered retCode!=0 |
        ('unknown', None) — transport failure, the order MAY exist (review r2 F4)."""
        try:
            r = self.session.place_order(
                category="option", symbol=symbol, side=side,
                orderType="Limit", qty=fmt_qty(qty), price=fmt_px(price),
                timeInForce="GTC", orderLinkId=link_id, reduceOnly=reduce_only)["result"]
            return ("ok", r.get("orderId")) if r.get("orderId") else ("rejected", None)
        except InvalidRequestError as e:
            print(f"[bybit] place({symbol} {side} {qty}@{price}) rejected: {e}", flush=True)
            return "rejected", None
        except Exception as e:
            print(f"[bybit] place({symbol} {side} {qty}@{price}) unknown outcome: {e}", flush=True)
            return "unknown", None

    def amend_order(self, symbol: str, order_id: str, price: float) -> bool:
        r = _with_retry(f"amend({symbol} {order_id} -> {price})",
                        lambda: self.session.amend_order(
                            category="option", symbol=symbol, orderId=order_id,
                            price=fmt_px(price))["result"])
        return r is not None

    def cancel_order(self, symbol: str, order_id: str | None,
                     link_id: str | None = None) -> bool:
        kw = {"orderId": order_id} if order_id else {"orderLinkId": link_id}
        r = _with_retry(f"cancel({symbol} {order_id or link_id})",
                        lambda: self.session.cancel_order(
                            category="option", symbol=symbol, **kw)["result"])
        return r is not None

    def get_order(self, symbol: str, link_id: str) -> dict | None:
        """Open first, then history — Bybit drops filled/cancelled orders from
        realtime quickly. Returns the raw order dict, {} when BOTH endpoints
        answered and neither knows the link id (definitively absent), or None
        when the API could not be reached (unknown — caller must wait)."""
        absent = True
        for fn in (self.session.get_open_orders, self.session.get_order_history):
            r = _with_retry(f"order({link_id})",
                            lambda fn=fn: fn(category="option", symbol=symbol,
                                             orderLinkId=link_id)["result"]["list"])
            if r:
                return r[0]
            if r is None:
                absent = False
        return {} if absent else None

    def get_open_orders_all(self) -> list[dict] | None:
        """Every resting option order on the account (reconcile: zombie/foreign
        orders). None = API failure."""
        r = _with_retry("open_orders(all)",
                        lambda: self.session.get_open_orders(
                            category="option", settleCoin="USDT", limit=50)["result"]["list"])
        return None if r is None else [{"symbol": o.get("symbol"),
                                        "orderLinkId": o.get("orderLinkId"),
                                        "orderId": o.get("orderId")} for o in r]

    def get_executions(self, symbol: str, order_id: str) -> list[dict]:
        r = _with_retry(f"executions({order_id})",
                        lambda: self.session.get_executions(
                            category="option", symbol=symbol, orderId=order_id,
                            limit=100)["result"]["list"])
        return r or []

    def get_option_positions(self, base_coin: str) -> dict[str, dict] | None:
        """symbol -> {size, side, avg_price, im}. None = API failure (caller must
        treat as unknown, never as 'flat')."""
        r = _with_retry(f"positions({base_coin})",
                        lambda: self.session.get_positions(
                            category="option", baseCoin=base_coin,
                            limit=200)["result"]["list"])
        if r is None:
            return None
        out = {}
        for p in r:
            size = _f(p.get("size"))
            if size == 0:
                continue
            out[p["symbol"]] = {"size": size, "side": p.get("side"),
                                "avg_price": _f(p.get("avgPrice")),
                                "im": _f(p.get("positionIM"))}
        return out

    def get_delivery(self, symbol: str) -> dict | None:
        r = _with_retry(f"delivery({symbol})",
                        lambda: self.session.get_option_delivery_record(
                            category="option", symbol=symbol, limit=5)["result"]["list"])
        return r[0] if r else None

    def key_status(self) -> dict | None:
        r = _with_retry("query-api", lambda: self.session.get_api_key_information()["result"])
        return r


def fmt_qty(q: float) -> str:
    return f"{q:.4f}".rstrip("0").rstrip(".")


def fmt_px(p: float) -> str:
    return f"{p:.4f}".rstrip("0").rstrip(".")


def round_to_tick(price: float, tick: float, up: bool) -> float:
    """Round to the instrument tick. up=True rounds toward the passive side for
    a seller (higher), False for a buyer (lower)."""
    if tick <= 0:
        return round(price, 4)
    n = price / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, 6)


def parse_option_symbol(symbol: str) -> dict | None:
    # Bybit option symbol: BASE-DDMMMYY-STRIKE-{C|P}[-QUOTE], expiry 08:00 UTC
    if not symbol:
        return None
    parts = symbol.split("-")
    if len(parts) < 4:
        return None
    _, date_part, strike_part, side = parts[0], parts[1], parts[2], parts[3]
    if side not in ("C", "P"):
        return None
    try:
        strike = float(strike_part)
    except ValueError:
        return None
    try:
        dt = datetime.strptime(date_part, "%d%b%y").replace(
            hour=8, minute=0, tzinfo=timezone.utc)
    except ValueError:
        return None
    return {"strike": strike, "side": side,
            "expiry_ms": int(dt.timestamp() * 1000)}


def pick_atm_option(chain: list[dict], spot: float, side: str,
                    target_expiry_h: float, min_expiry_h: float,
                    now_ms: int | None = None) -> dict | None:
    """ATM contract closest to target expiry: filter side + min expiry,
    nearest expiry to target, then nearest strike to spot — mirrors opt-app
    paper_loop.pick_bybit_atm_option."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    target_ms = now_ms + target_expiry_h * 3_600_000
    candidates = [o for o in chain
                  if o["side"] == side
                  and o["expiry_ms"] > now_ms + min_expiry_h * 3_600_000]
    if not candidates:
        return None
    candidates.sort(key=lambda o: abs(o["expiry_ms"] - target_ms))
    best_expiry = candidates[0]["expiry_ms"]
    same_expiry = [o for o in candidates if o["expiry_ms"] == best_expiry]
    same_expiry.sort(key=lambda o: abs(o["strike"] - spot))
    return same_expiry[0] if same_expiry else None


def _f(v) -> float:
    try:
        if v in (None, "", "null"):
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


bybit_client = BybitClient()
