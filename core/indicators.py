"""Pure-function indicators, ported VERBATIM from opt-app
backend/services/indicators.py (only the subset Jony's gates use).
Semantics must stay identical to the backtest — do not "improve"."""
from __future__ import annotations

import math
from typing import Sequence


def ema(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: Sequence[float], period: int = 14) -> float | None:
    """Wilder's RSI."""
    if len(closes) <= period:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def adx(candles: list[dict], period: int = 14) -> float | None:
    """Wilder's ADX(14). 0-100 — higher means stronger trend."""
    if len(candles) < 2 * period + 2:
        return None
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    trs: list[float] = []
    for i in range(1, len(candles)):
        h, low = candles[i]["high"], candles[i]["low"]
        ph, pl, pc = candles[i - 1]["high"], candles[i - 1]["low"], candles[i - 1]["close"]
        up = h - ph
        dn = pl - low
        plus_dm.append(up if up > dn and up > 0 else 0.0)
        minus_dm.append(dn if dn > up and dn > 0 else 0.0)
        trs.append(max(h - low, abs(h - pc), abs(low - pc)))

    atr_v = sum(trs[:period])
    plus_v = sum(plus_dm[:period])
    minus_v = sum(minus_dm[:period])
    dxs: list[float] = []
    for i in range(period, len(trs)):
        atr_v = atr_v - atr_v / period + trs[i]
        plus_v = plus_v - plus_v / period + plus_dm[i]
        minus_v = minus_v - minus_v / period + minus_dm[i]
        if atr_v == 0:
            continue
        plus_di = 100 * plus_v / atr_v
        minus_di = 100 * minus_v / atr_v
        if plus_di + minus_di == 0:
            dx = 0.0
        else:
            dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        dxs.append(dx)
    if len(dxs) < period:
        return None
    a = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        a = (a * (period - 1) + dx) / period
    return a


def zscore(values: Sequence[float], lookback: int = 20) -> float | None:
    """Z-score of the LAST value vs. the previous `lookback` values."""
    if len(values) < lookback + 1:
        return None
    sub = list(values[-lookback - 1:-1])
    mean = sum(sub) / len(sub)
    var = sum((v - mean) ** 2 for v in sub) / len(sub)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (values[-1] - mean) / std


def realized_vol(closes: Sequence[float], lookback: int = 24) -> float | None:
    """Annualized realized vol from log returns. Lookback in bars; assumes hourly bars by default."""
    if len(closes) < lookback + 1:
        return None
    rets = []
    for i in range(1, lookback + 1):
        if closes[-i - 1] <= 0:
            continue
        rets.append(math.log(closes[-i] / closes[-i - 1]))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var)
    # annualize: hourly → sqrt(24*365)
    return std * math.sqrt(24 * 365)
