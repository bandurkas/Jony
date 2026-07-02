"""Trend vs range regime via ADX(14) on 1h — ported VERBATIM from opt-app
backend/services/regime.py (V3 cutoffs: trend >35, range <20)."""
from __future__ import annotations

from .indicators import adx


def detect_regime(candles_1h: list[dict]) -> dict:
    a = adx(candles_1h, 14)
    if a is None:
        return {"regime": "unknown", "adx": None}
    if a > 35:
        regime = "trend"
    elif a < 20:
        regime = "range"
    else:
        regime = "transition"
    return {"regime": regime, "adx": round(a, 1)}
