"""Bybit v5 client (pybit) — klines with pagination (the public API caps at
1000 bars/request; ret_7d needs 2016+ 5m bars) + options chain.

Authenticates with the ex-Grogu account key when BYBIT_API_KEY/SECRET are set.
v1 has NO order-placement path at all — the key only enables authenticated
reads; going live will require adding an execution module, not just flipping
JONY_TRADING_MODE."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

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
        if key and secret:
            self.session = HTTP(testnet=False, api_key=key, api_secret=secret)
        else:
            self.session = HTTP(testnet=False)

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
        for it in items:
            parsed = parse_option_symbol(it.get("symbol", ""))
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
        """symbol → {mark, bid, ask} for open-position management."""
        return {o["symbol"]: {"mark": o["mark_price"], "bid": o["bid"], "ask": o["ask"]}
                for o in self.get_options_tickers(base_coin)}


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
