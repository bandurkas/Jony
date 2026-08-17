"""Entry-proximity gauge — ported from opt-app's Sniper1 (archive/grogu
branch, backend/services/paper_strategy.py::entry_proximity), adapted to
Jony's actual gate factors and per-coin window debounce state.

Jony has no ADX factor at all (Sniper1-specific); the four factors here are
the ones Jony's own gates actually check (core.strategy._evaluate_side):
  - vol   : vol_pctile (continuous 0-1, already computed by evaluate_conditions)
  - regime: regime_ok (bool)
  - mtf   : tfs_aligned/3 (continuous 0-1 -- how many of 5m/15m/1h agree on
            direction; not itself the gate for CALL's 1h-anchored filter,
            but the same informative proxy Sniper1 used)
  - bull  : bull_filter_ok (bool; PUT's bull_market_ratio_max is None so it
            defaults True, same convention as the original)
Weights (vol .20 / regime .30 / mtf .30 / bull .20) are Sniper1's own
(adx .30 / mtf .20 / vol .15 / regime .20 / bull .15) with adx's slice
redistributed proportionally across the remaining four, then rounded to
clean numbers -- there's no backtest behind this split (display-only, see
below), just a reasonable carry-over of the original's relative emphasis.

Display/observability ONLY -- does not drive position sizing or entries
(core.strategy/loop.py are untouched by this module). The one invariant
carried over VERBATIM from the original, because it's the load-bearing
correctness property, not a stylistic choice: 100% is reserved for `ready`
AND a *confirmed* live debounce window (per-coin `win[coin]` state in
loop.py: not disqualified, at the close-tick minute `min_in_window ==
FIVE_MIN - 1`) -- never for the raw per-minute gate snapshot alone, since
several more per-minute checks could still flip an early-window "ready" to
disqualified before the bot would actually fire. `window_status` is only
trusted when BOTH fresh (WINDOW_STATUS_STALE_MS) AND for the SAME window as
`ev` -- a stale or cross-window status must not be applied in either
direction. This exact bug (gauge showing "100%/entry" at minute 1 of 5, or
trusting a stale/cross-window status) is what broke Sniper1's gauge on
2026-06-25; porting the fix's shape, not just the happy path, is the point.
"""
from __future__ import annotations

import time

from services.config import FIVE_MIN

WINDOW_STATUS_STALE_MS = 90_000  # >1 missed per-minute tick from loop.py

WEIGHTS = {"vol": 0.20, "regime": 0.30, "mtf": 0.30, "bull": 0.20}


def window_id(epoch_min: int) -> int:
    """5m-window id: floor(minute / FIVE_MIN). Same definition loop.py's
    `wid = epoch_min // FIVE_MIN` uses inline -- kept here too so window
    status written by the loop and read by the gauge always agree."""
    return epoch_min // FIVE_MIN


def _f(x: float | None) -> float:
    return 0.0 if x is None else max(0.0, min(1.0, float(x)))


def entry_proximity(ev: dict, window_status: dict | None = None,
                    now_ms: int | None = None) -> dict:
    """`ev` is one coin's evaluate_conditions() snapshot (the "show" side's
    gate breakdown -- active side if ready, else the closest candidate).
    `window_status` is that coin's persisted per-minute check state (see
    db.repo.upsert_window_status / loop.py): {wid, min_in_window,
    disqualified, checked_at_ms}."""
    if ev.get("no_side_allowed"):
        # селектор сторон не оставил ни одной (напр. даунтренд при puts-only):
        # гейты не считались, композит из нулей+bull-заглушки (вечные 20%)
        # вводил в заблуждение — честная зона "side-off" с 0% (2026-08-17)
        return {
            "proximity_pct": 0.0,
            "zone": "side-off",
            "factors": {"vol": 0.0, "regime": 0.0, "mtf": 0.0, "bull": 0.0},
            "weights": WEIGHTS,
            "debounce_unknown": False,
            "window_disqualified": False,
        }
    f_vol = _f(ev.get("vol_pctile"))
    f_regime = 1.0 if ev.get("regime_ok") else 0.0
    f_mtf = _f((ev.get("tfs_aligned") or 0) / 3.0)
    f_bull = 1.0 if ev.get("bull_filter_ok") else 0.0
    composite = 100.0 * (WEIGHTS["vol"] * f_vol + WEIGHTS["regime"] * f_regime
                         + WEIGHTS["mtf"] * f_mtf + WEIGHTS["bull"] * f_bull)

    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    debounce_unknown = True
    window_disqualified = False
    at_close_tick = False
    if window_status and window_status.get("checked_at_ms") is not None:
        fresh = (now_ms - int(window_status["checked_at_ms"])) <= WINDOW_STATUS_STALE_MS
        same_window = window_status.get("wid") == window_id(now_ms // 60_000)
        if fresh and same_window:
            debounce_unknown = False
            window_disqualified = bool(window_status.get("disqualified"))
            at_close_tick = window_status.get("min_in_window") == FIVE_MIN - 1

    ready = (bool(ev.get("ready")) and not debounce_unknown
             and not window_disqualified and at_close_tick)
    pct = 100.0 if ready else min(99.0, composite)
    pct = round(pct, 1)
    if pct >= 100.0:
        zone = "entry"        # all gates pass AND debounce window confirmed — bot will fire
    elif pct >= 80.0:
        zone = "ready"        # one factor short of entry
    elif pct >= 50.0:
        zone = "preparing"    # factors aligning
    else:
        zone = "waiting"      # far from a signal
    return {
        "proximity_pct": pct,
        "zone": zone,
        "factors": {"vol": round(f_vol, 3), "regime": round(f_regime, 3),
                    "mtf": round(f_mtf, 3), "bull": round(f_bull, 3)},
        "weights": WEIGHTS,
        "debounce_unknown": debounce_unknown,
        "window_disqualified": window_disqualified,
    }
