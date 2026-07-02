"""Jony strategy gates — the exact config validated by the basket backtest
(opt repo backend/services/basket_premium_backtest.py, 2026-07-02, memory
finding_basket_eth_btc_call_mo4). Ported from opt-app paper_strategy's
evaluate_conditions/_evaluate_side, generalized per-coin.

DO NOT tune numbers here without a new validated backtest:
frequency levers are rejected 5x over (see opt memory REJECTED section).
"""
from __future__ import annotations

from .indicators import ema, realized_vol
from .momentum_mtf import analyze_tf, consensus, direction_filter_ok
from .regime import detect_regime

BARS_7D = 2016            # 7 days of 5m bars
RET_7D_THRESHOLD = 0.5    # V2 hybrid boundary, %
HIST = 240                # bars fed to MTF/regime/vol, matches opt-app

PUT_GEN = {
    "vol_threshold": 0.50,
    "regime_filter": ("range",),          # strict — +transition REJECTED 2026-07-02
    "mtf_direction_filter": "up",
    "mtf_anchor_tf": None,                # 3-way >=2/3 consensus
    "bull_market_ratio_max": None,
}

CALL_GEN = {
    "vol_threshold": 0.60,
    "regime_filter": ("range", "transition"),
    "mtf_direction_filter": "down",
    "mtf_anchor_tf": "1h",                # validated CALL-only anchor
    "bull_market_ratio_max": 1.05,
}

PUT_EXIT = {"tp2_pct": 0.70, "sl_pct": 2.00, "hold_h": 96}
CALL_EXIT = {"tp2_pct": 0.80, "sl_pct": 0.75, "hold_h": 24}

# Basket composition (the backtest's winner): BTC Put is FORBIDDEN
# (-7.5%/trade, no VRP edge — BTC falls less in panic vol).
COIN_SIDES = {"ETH": ("P", "C"), "BTC": ("C",)}


def gen_kwargs(side: str) -> dict:
    return PUT_GEN if side == "P" else CALL_GEN


def exit_params(side: str) -> dict:
    return PUT_EXIT if side == "P" else CALL_EXIT


def compute_ret_7d(k5: list, idx: int) -> float:
    """7-day return (%) ending at k5[idx]."""
    if idx < BARS_7D:
        return 0.0
    prev_close = k5[idx - BARS_7D]["close"]
    if prev_close <= 0:
        return 0.0
    return (k5[idx]["close"] - prev_close) / prev_close * 100


def allowed_sides(coin: str, ret_7d: float) -> list[str]:
    """V2 trend-following side selection, intersected with the coin's
    permitted sides (BTC never sells Puts)."""
    if ret_7d > RET_7D_THRESHOLD:
        sides = ["P"]
    elif ret_7d < -RET_7D_THRESHOLD:
        sides = ["C"]
    else:
        sides = ["P", "C"]
    return [s for s in sides if s in COIN_SIDES[coin]]


def _evaluate_side(side: str, mtf: dict, regime: str,
                   rolling_vols: list[float], closes_1h: list[float]) -> dict:
    """Vol/regime/MTF/bull checks for one side — mirrors opt-app exactly."""
    kw = gen_kwargs(side)
    out = {
        "vol_high": False, "regime_ok": False, "mtf_direction_ok": False,
        "vol_pctile": None, "regime": regime, "ema_ratio": None,
        "bull_filter_ok": True, "ready": False,
    }

    if len(rolling_vols) >= 30:
        current_vol = rolling_vols[-1]
        sorted_vols = sorted(rolling_vols)
        threshold_idx = int(len(sorted_vols) * kw["vol_threshold"])
        threshold = sorted_vols[threshold_idx]
        below = sum(1 for v in sorted_vols if v < current_vol)
        out["vol_pctile"] = round(below / len(sorted_vols), 3)
        out["vol_high"] = current_vol >= threshold

    out["regime_ok"] = regime in kw["regime_filter"]
    out["mtf_direction_ok"] = direction_filter_ok(
        mtf, kw["mtf_direction_filter"], kw["mtf_anchor_tf"])

    bull_max = kw["bull_market_ratio_max"]
    if bull_max is not None and len(closes_1h) >= 200:
        ema50 = ema(closes_1h, 50)
        ema200 = ema(closes_1h, 200)
        if ema50 is not None and ema200 not in (None, 0):
            ratio = ema50 / ema200
            out["ema_ratio"] = round(ratio, 4)
            out["bull_filter_ok"] = ratio <= bull_max

    out["ready"] = (out["vol_high"] and out["regime_ok"]
                    and out["mtf_direction_ok"] and out["bull_filter_ok"])
    return out


def evaluate_conditions(coin: str, k5: list, k15: list, k1h: list) -> dict:
    """Full per-minute gate check for one coin. Returns the active (ready)
    side with P priority in range zone — mirrors opt-app evaluate_conditions
    and the basket backtest's events_for_variant side pick."""
    out = {
        "ready": False, "active_side": None, "ret_7d": None, "spot": None,
        "vol_high": False, "regime_ok": False, "mtf_direction_ok": False,
        "bull_filter_ok": True, "vol_pctile": None, "regime": None,
        "mtf_direction": None, "ema_ratio": None,
    }
    if not k5 or not k15 or not k1h:
        return out
    if len(k5) < BARS_7D or len(k15) < 50 or len(k1h) < 200:
        return out

    idx = len(k5) - 1
    out["spot"] = k5[idx]["close"]
    ret_7d = compute_ret_7d(k5, idx)
    out["ret_7d"] = round(ret_7d, 2)
    sides = allowed_sides(coin, ret_7d)
    if not sides:
        return out

    s5 = k5[-HIST:]
    s15 = k15[-HIST:]
    s1h = k1h[-HIST:]
    mtf = consensus(analyze_tf(s5), analyze_tf(s15), analyze_tf(s1h))
    out["mtf_direction"] = mtf["direction"]

    closes_1h = [c["close"] for c in s1h]
    rolling_vols: list[float] = []
    for j in range(20, len(closes_1h)):
        rv = realized_vol(closes_1h[:j + 1], lookback=24)
        if rv is not None:
            rolling_vols.append(rv)
    regime = detect_regime(s1h).get("regime", "unknown")

    side_results = {s: _evaluate_side(s, mtf, regime, rolling_vols, closes_1h)
                    for s in sides}
    active_side = next((s for s in sides if side_results[s]["ready"]), None)
    out["active_side"] = active_side

    # Report the gate breakdown of the active side, else the closest one —
    # same display convention as opt-app (audit trail readability).
    def _gates(r: dict) -> int:
        return sum([r["vol_high"], r["regime_ok"], r["mtf_direction_ok"], r["bull_filter_ok"]])

    show = active_side or max(sides, key=lambda s: _gates(side_results[s]))
    res = side_results[show]
    for k in ("vol_high", "regime_ok", "mtf_direction_ok", "bull_filter_ok",
              "vol_pctile", "regime", "ema_ratio"):
        out[k] = res[k]
    out["ready"] = active_side is not None
    return out


def window_fail_step(fail_count: int, minute_ready: bool,
                     flicker_tolerance: int = 1) -> tuple[int, bool]:
    """One per-minute persistence check within a 5m window: disqualified once
    more than `flicker_tolerance` of the window's checks have failed.
    Identical to opt-app paper_loop.window_fail_step (tol1 validated)."""
    if not minute_ready:
        fail_count += 1
    return fail_count, fail_count > flicker_tolerance
