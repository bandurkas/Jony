"""Vectorized reconstruction of Jony's basket backtest (basket_premium_backtest.py
is lost — see jony_core.py header). Produces the same gate decisions as a
literal bar-by-bar port would (verified: continuous EMA/RSI/ADX converge to
the windowed-reseed versions in core/strategy.py after their warmup — see
jony_core.py docstring), but computes indicators as vectorized rolling arrays
instead of re-deriving from scratch on every 5m bar, which is what makes 2
years of 5m data (210k+ bars/coin) tractable in plain Python.

Two-phase design (mirrors the coin_trades / replay_account split implied by
jony_trailing_backtest.py's imports):
  coin_trades(coin, data_dir) -> raw per-coin trade candidates (entry gate +
    cooldown only, NO portfolio constraints — assumes every signal fills).
  replay_account(trades, mo, cap) -> walks the time-merged candidate list
    applying MAX_OPEN/PER_COIN_CAP/CB/dyn-size/margin sizing/fees/compounding,
    exactly like the live services/portfolio.py functions.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import ndtr

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import backtest_bs as bs

DATA_DIR = Path(__file__).resolve().parent / "fresh_data"
SIGMA_CLAMP = (0.20, 1.50)
SPREAD_PCT = 2.0
HALF_SPREAD = SPREAD_PCT / 200.0


def load_klines(coin: str, tf: str) -> pd.DataFrame:
    prefix = "btc" if coin == "BTC" else "eth"
    data = json.loads((DATA_DIR / f"{prefix}_{tf}.json").read_text())
    df = pd.DataFrame(data)
    df = df.sort_values("start_ms").drop_duplicates("start_ms").reset_index(drop=True)
    return df


def continuous_ema(closes: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(closes), np.nan)
    if len(closes) < period:
        return out
    k = 2 / (period + 1)
    e = closes[:period].mean()
    out[period - 1] = e
    for i in range(period, len(closes)):
        e = closes[i] * k + e * (1 - k)
        out[i] = e
    return out


def continuous_rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    out = np.full(len(closes), np.nan)
    if len(closes) <= period:
        return out
    diffs = np.diff(closes)
    gains = np.maximum(diffs, 0.0)
    losses = np.maximum(-diffs, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / max(avg_loss, 1e-12))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / max(avg_loss, 1e-12))
        out[i + 1] = rs
    return out


def continuous_adx(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    h, low, c = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    out = np.full(n, np.nan)
    if n < 2 * period + 2:
        return out
    plus_dm = np.zeros(n - 1)
    minus_dm = np.zeros(n - 1)
    trs = np.zeros(n - 1)
    for i in range(1, n):
        up = h[i] - h[i - 1]
        dn = low[i - 1] - low[i]
        plus_dm[i - 1] = up if (up > dn and up > 0) else 0.0
        minus_dm[i - 1] = dn if (dn > up and dn > 0) else 0.0
        trs[i - 1] = max(h[i] - low[i], abs(h[i] - c[i - 1]), abs(low[i] - c[i - 1]))
    atr_v = trs[:period].sum()
    plus_v = plus_dm[:period].sum()
    minus_v = minus_dm[:period].sum()
    dxs = []
    dx_idx = []
    for i in range(period, len(trs)):
        atr_v = atr_v - atr_v / period + trs[i]
        plus_v = plus_v - plus_v / period + plus_dm[i]
        minus_v = minus_v - minus_v / period + minus_dm[i]
        if atr_v == 0:
            continue
        pdi, mdi = 100 * plus_v / atr_v, 100 * minus_v / atr_v
        dx = 0.0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi)
        dxs.append(dx)
        dx_idx.append(i + 1)  # index into candles (i is index into trs/plus_dm, offset by 1)
    if len(dxs) < period:
        return out
    a = np.mean(dxs[:period])
    out[dx_idx[period - 1]] = a
    for k in range(period, len(dxs)):
        a = (a * (period - 1) + dxs[k]) / period
        out[dx_idx[k]] = a
    return out


def rolling_realized_vol(closes: pd.Series, lookback: int = 24) -> pd.Series:
    log_ret = np.log(closes / closes.shift(1))
    std = log_ret.rolling(lookback).std(ddof=1)
    return std * np.sqrt(24 * 365)


def rolling_vol_pctile_and_high(rv: pd.Series, window: int, threshold_frac: float) -> tuple[pd.Series, pd.Series]:
    """Mirrors _evaluate_side: within the trailing `window` rv readings
    (itself requiring >=30 to activate, matching len(rolling_vols)>=30),
    vol_high = current >= sorted[int(len*threshold_frac)]; vol_pctile =
    fraction of the window strictly below current."""
    def pctile_of_last(w):
        if w.isna().any() or len(w) < 30:
            return np.nan
        arr = np.sort(w.values)
        thr = arr[int(len(arr) * threshold_frac)]
        below = (arr < w.values[-1]).sum()
        return below / len(arr) if w.values[-1] >= thr else -(below / len(arr)) - 1  # sentinel-encode high/low in sign+offset

    # Simpler: two separate rolling computations (pctile value, and high flag)
    pct = rv.rolling(window, min_periods=30).apply(
        lambda w: (w < w[-1]).sum() / len(w), raw=True)
    def high_flag(w):
        arr = np.sort(w)
        thr = arr[int(len(arr) * threshold_frac)]
        return 1.0 if w[-1] >= thr else 0.0
    high = rv.rolling(window, min_periods=30).apply(high_flag, raw=True)
    return pct, high


def direction_series(closes: np.ndarray, volumes: np.ndarray) -> np.ndarray:
    """analyze_tf's direction classification, vectorized. Returns array of
    'up'/'down'/'neutral'/'unknown' (object dtype)."""
    e20 = continuous_ema(closes, 20)
    e50 = continuous_ema(closes, 50)
    r = continuous_rsi(closes, 14)
    n = len(closes)
    out = np.full(n, "unknown", dtype=object)
    valid = ~np.isnan(e20) & ~np.isnan(e50) & ~np.isnan(r)
    up = valid & (e20 > e50) & (r > 50)
    down = valid & (e20 < e50) & (r < 50)
    out[valid] = "neutral"
    out[up] = "up"
    out[down] = "down"
    return out


def bar_ema_ratio(closes_1h: np.ndarray) -> np.ndarray:
    e50 = continuous_ema(closes_1h, 50)
    e200 = continuous_ema(closes_1h, 200)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = e50 / e200
    return ratio


_BASE_CACHE: dict[str, dict] = {}


def build_coin_base(coin: str) -> dict:
    """Everything about a coin's 5m timeline that does NOT depend on
    put_gen/call_gen: directions, regime, rv1h (pre-threshold), ema_ratio,
    mtf consensus, ret_7d allowed-sides gate. Computed once per coin
    (memoized) and reused across every sweep variant — only vol_threshold/
    regime_filter/mtf/bull_market_ratio_max change per variant, and those
    are applied cheaply in evaluate_gates() on top of this.

    Fully vectorized (no DataFrame.apply(axis=1)) — the row-wise Python
    applies in the original port cost ~68s/coin for 210k 5m bars; this is
    the same math expressed as numpy/pandas vector ops."""
    if coin in _BASE_CACHE:
        return _BASE_CACHE[coin]

    d5 = load_klines(coin, "5m")
    d15 = load_klines(coin, "15m")
    d1h = load_klines(coin, "1h")

    dir5 = direction_series(d5["close"].values, d5["volume"].values)
    dir15 = direction_series(d15["close"].values, d15["volume"].values)
    dir1h = direction_series(d1h["close"].values, d1h["volume"].values)

    regime_adx = continuous_adx(d1h, 14)
    regime = np.select([regime_adx > 35, (regime_adx >= 20) & (regime_adx <= 35), regime_adx < 20],
                       ["trend", "transition", "range"], default="unknown")

    rv1h = rolling_realized_vol(d1h["close"], lookback=24)
    ema_ratio = bar_ema_ratio(d1h["close"].values)

    d1h_ext = d1h.assign(direction=dir1h, regime=regime, ema_ratio=ema_ratio, rv1h=rv1h.values)
    d15_ext = d15.assign(direction=dir15)
    ret7d = (d5["close"] - d5["close"].shift(jc.BARS_7D)) / d5["close"].shift(jc.BARS_7D) * 100
    d5_ext = d5.assign(direction=dir5, ret7d=ret7d)

    merged = pd.merge_asof(
        d5_ext[["start_ms", "close", "direction", "ret7d"]].rename(columns={"direction": "dir5", "close": "spot"}),
        d15_ext[["start_ms", "direction"]].rename(columns={"direction": "dir15"}),
        on="start_ms", direction="backward")
    merged = pd.merge_asof(
        merged,
        d1h_ext[["start_ms", "direction", "regime", "ema_ratio", "rv1h"]].rename(columns={"direction": "dir1h"}),
        on="start_ms", direction="backward")

    ups = (merged["dir5"] == "up").astype(int) + (merged["dir15"] == "up").astype(int) + (merged["dir1h"] == "up").astype(int)
    downs = (merged["dir5"] == "down").astype(int) + (merged["dir15"] == "down").astype(int) + (merged["dir1h"] == "down").astype(int)
    mtf_dir = np.select([(ups >= 2) & (ups > downs), (downs >= 2) & (downs > ups)], ["up", "down"], default="neutral")
    mtf_aligned = np.maximum(ups.values, downs.values)  # equals ups/downs in the up/down branches (they ARE the max there)

    coin_sides = jc.COIN_SIDES[coin]
    r7 = merged["ret7d"]
    sides_code = np.select([r7.isna(), r7 > jc.RET_7D_THRESHOLD, r7 < -jc.RET_7D_THRESHOLD], [0, 1, 2], default=3)
    allowed_P = np.isin(sides_code, [1, 3]) & ("P" in coin_sides)
    allowed_C = np.isin(sides_code, [2, 3]) & ("C" in coin_sides)

    base = {
        "coin": coin, "start_ms": merged["start_ms"].values, "spot": merged["spot"].values,
        "dir1h": merged["dir1h"].values, "regime": merged["regime"].values,
        "ema_ratio": merged["ema_ratio"].values,
        # native 1h-resolution rv1h + its own start_ms — vol_threshold rolling
        # percentile must window over jc.HIST=240 HOURLY readings (~10 days),
        # not 240 5m-bars (~20h); rolled here then broadcast down to 5m in
        # evaluate_gates() (bug caught by a fidelity check against known-good
        # trade counts: rolling the already-5m-broadcast series fired signals
        # ~10x too early).
        "rv1h_native": rv1h.values, "start_ms_1h": d1h["start_ms"].values,
        "mtf_dir": mtf_dir, "mtf_aligned": mtf_aligned,
        "allowed_P": allowed_P, "allowed_C": allowed_C,
    }
    _BASE_CACHE[coin] = base
    return base


def evaluate_gates(base: dict, put_gen: dict | None = None, call_gen: dict | None = None,
                   vol_guard: dict | None = None) -> pd.DataFrame:
    """Cheap, vectorized per-variant layer on top of build_coin_base(): applies
    vol_threshold/regime_filter/mtf/bull_market_ratio_max to produce ready_P/
    ready_C. put_gen/call_gen default to jc.PUT_GEN/CALL_GEN (live-deployed).

    vol_guard (experimental, off by default — see research/sweep_vol_guard.py):
    {"lookback_h": int, "max_accel": float}. Blocks entry when current rv1h
    has risen more than max_accel-x versus lookback_h hours ago — distinct
    from vol_threshold (which requires vol to be elevated vs its OWN trailing
    percentile, rewarding entry mid-spike). Missing lookback history (warmup)
    defaults to allowed, same convention as bull_filter_ok."""
    put_gen = jc.PUT_GEN if put_gen is None else put_gen
    call_gen = jc.CALL_GEN if call_gen is None else call_gen
    n = len(base["start_ms"])
    rv1h_native = pd.Series(base["rv1h_native"])

    # roll at native 1h resolution (jc.HIST=240 hourly readings), THEN
    # broadcast down to the 5m grid via merge_asof — matches how the live
    # bot's evaluate_conditions computes rolling_vols from 1h closes.
    _, high_put_1h = rolling_vol_pctile_and_high(rv1h_native, window=jc.HIST, threshold_frac=put_gen["vol_threshold"])
    _, high_call_1h = rolling_vol_pctile_and_high(rv1h_native, window=jc.HIST, threshold_frac=call_gen["vol_threshold"])
    df_1h = pd.DataFrame({"start_ms": base["start_ms_1h"], "high_put": high_put_1h.values, "high_call": high_call_1h.values})
    broadcast = pd.merge_asof(pd.DataFrame({"start_ms": base["start_ms"]}), df_1h, on="start_ms", direction="backward")
    vol_high_put = broadcast["high_put"].fillna(0).astype(bool).values
    vol_high_call = broadcast["high_call"].fillna(0).astype(bool).values

    regime = base["regime"]
    put_regime_ok = np.isin(regime, put_gen["regime_filter"])
    call_regime_ok = np.isin(regime, call_gen["regime_filter"])

    put_min_aligned = put_gen.get("mtf_min_aligned", 2)
    put_mtf_ok = (base["mtf_dir"] == "up") & (base["mtf_aligned"] >= put_min_aligned)
    call_mtf_ok = (base["dir1h"] == "down")  # anchor_tf=1h

    bull_max = call_gen.get("bull_market_ratio_max")
    if bull_max is not None:
        er = base["ema_ratio"]
        bull_ok = np.where(np.isnan(er), True, er <= bull_max)
    else:
        bull_ok = np.ones(n, dtype=bool)

    ready_put_raw = vol_high_put & put_regime_ok & put_mtf_ok
    ready_call_raw = vol_high_call & call_regime_ok & call_mtf_ok & bull_ok

    if vol_guard is not None:
        lb = vol_guard["lookback_h"]
        accel_1h = rv1h_native / rv1h_native.shift(lb)
        guard_ok_1h = accel_1h.isna() | (accel_1h <= vol_guard["max_accel"])
        df_guard = pd.DataFrame({"start_ms": base["start_ms_1h"], "guard_ok": guard_ok_1h.values})
        bcast_guard = pd.merge_asof(pd.DataFrame({"start_ms": base["start_ms"]}), df_guard,
                                    on="start_ms", direction="backward")
        guard_ok = bcast_guard["guard_ok"].fillna(True).astype(bool).values
        ready_put_raw = ready_put_raw & guard_ok
        ready_call_raw = ready_call_raw & guard_ok

    ready_P = base["allowed_P"] & ready_put_raw
    ready_C = base["allowed_C"] & ready_call_raw

    return pd.DataFrame({"start_ms": base["start_ms"], "spot": base["spot"],
                         "ready_P": ready_P, "ready_C": ready_C, "coin": base["coin"],
                         "regime": base["regime"]})


def _vec_bs_price(side: str, S: np.ndarray, K: float, T: np.ndarray, sigma: float, r: float = 0.0) -> np.ndarray:
    """Vectorized equivalent of backtest_bs.price — same math (normal CDF via
    scipy.special.ndtr instead of scalar math.erf), for pricing a whole
    forward-bar window in one call instead of one bs.price() call per bar."""
    S = np.asarray(S, dtype=float)
    T = np.asarray(T, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        sqrtT = np.sqrt(np.maximum(T, 1e-12))
        d1 = (np.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
        d2 = d1 - sigma * sqrtT
        if side == "C":
            live = S * ndtr(d1) - K * np.exp(-r * T) * ndtr(d2)
        else:
            live = K * np.exp(-r * T) * ndtr(-d2) - S * ndtr(-d1)
    intrinsic = np.maximum(0.0, S - K) if side == "C" else np.maximum(0.0, K - S)
    return np.where(T <= 0, intrinsic, live)


def simulate_option_exit(side: str, entry_idx: int, close: np.ndarray, high: np.ndarray, low: np.ndarray,
                         start_ms: np.ndarray, sigma: float, tp2_pct: float, sl_pct: float,
                         hold_h: float, strike_round: float, expiry_h: float = jc.TARGET_EXPIRY_H,
                         vol_track: np.ndarray | None = None, vol_stop_accel: float | None = None,
                         vol_stop_require_loss: bool = False):
    """Prices off the option's REAL tenor (weekly, expiry_h=168h by default —
    matches config.TARGET_EXPIRY_H / pick_atm_option's weekly selection), not
    the planned holding time. hold_h only caps how long we keep walking
    forward before forcing a time-stop exit; it must NOT feed the BS T term,
    or the option decays to worthless on an artificially short clock and
    TP2 triggers almost immediately every time (caught via a full-basket
    sanity check that returned an impossible +18,962% total — a 168h option
    barely loses time value in its first 24-96h, so T must count down from
    168h, pausing hold_h bars into that curve, not from hold_h itself).

    Vectorized over the forward-bar window (numpy arrays, not DataFrame
    iterrows) — the original per-bar Python loop with scalar bs.price() calls
    cost ~62s for 3195 trades; this is the same first-passage-time scan
    expressed as array ops.

    vol_track/vol_stop_accel (experimental, off by default — see
    research/sweep_vol_guard.py): vol_track is a 5m-grid-aligned realized-vol
    series (same length as close/high/low, UNCLAMPED — i.e. not the
    SIGMA_CLAMP'd pricing sigma). If rv at any bar during the hold reaches
    vol_stop_accel-x the entry-time rv, the position is bought back early at
    that bar's close-based mark, regardless of price-based TP2/SL. Priority
    on a same-bar tie: sl > vol_stop > tp2 (protecting capital first)."""
    spot0 = close[entry_idx]
    strike = round(spot0 / strike_round) * strike_round
    T0 = expiry_h / (24 * 365)
    entry_mid = bs.price(side, spot0, strike, T0, sigma)
    if entry_mid <= 0.01:
        return None
    entry_credit = entry_mid * (1 - HALF_SPREAD)
    tp2_mid = entry_credit * (1 - tp2_pct) / (1 + HALF_SPREAD)
    sl_mid = entry_credit * (1 + sl_pct) / (1 + HALF_SPREAD)

    bars_limit = int(hold_h * 12)  # 5m bars — caps the walk, not the option's real tenor
    lo_idx, hi_idx = entry_idx + 1, min(entry_idx + 1 + bars_limit, len(close))
    if hi_idx <= lo_idx:
        return None
    hi_spot, lo_spot = high[lo_idx:hi_idx], low[lo_idx:hi_idx]
    m = hi_idx - lo_idx
    elapsed_h = (np.arange(1, m + 1)) * 5 / 60
    T = np.maximum(0.0, (expiry_h - elapsed_h) / (24 * 365))
    if side == "C":
        premium_high = _vec_bs_price(side, hi_spot, strike, T, sigma)
        premium_low = _vec_bs_price(side, lo_spot, strike, T, sigma)
    else:
        premium_high = _vec_bs_price(side, lo_spot, strike, T, sigma)
        premium_low = _vec_bs_price(side, hi_spot, strike, T, sigma)

    sl_hits = np.flatnonzero(premium_high >= sl_mid)
    tp_hits = np.flatnonzero(premium_low <= tp2_mid)
    first_sl = sl_hits[0] if len(sl_hits) else None
    first_tp = tp_hits[0] if len(tp_hits) else None

    first_vol = None
    if vol_track is not None and vol_stop_accel is not None:
        entry_vol = vol_track[entry_idx]
        if entry_vol > 0:
            accel = vol_track[lo_idx:hi_idx] / entry_vol
            vol_hit_mask = accel >= vol_stop_accel
            if vol_stop_require_loss:
                # only fire while the position is ALSO already underwater —
                # round-1 sweep found a pure vol-level trigger clips winners
                # that see a transient vol uptick before recovering to TP2;
                # this restricts it to the loss case the idea was meant for.
                premium_close = _vec_bs_price(side, close[lo_idx:hi_idx], strike, T, sigma)
                vol_hit_mask = vol_hit_mask & (premium_close > entry_credit)
            vol_hits = np.flatnonzero(vol_hit_mask)
            if len(vol_hits):
                first_vol = vol_hits[0]

    # priority rank for same-bar ties: sl(0) > vol_stop(1) > tp2(2)
    candidates = [(idx, rank, kind) for idx, rank, kind in
                 ((first_sl, 0, "sl"), (first_vol, 1, "vol_stop"), (first_tp, 2, "tp2")) if idx is not None]
    if candidates:
        idx, _, kind = min(candidates)  # earliest bar; ties broken by rank
        if kind == "sl":
            return {"resolution": "sl", "pnl_pct": -sl_pct, "exit_ts": int(start_ms[lo_idx + idx]),
                    "strike": strike, "entry_credit": entry_credit}
        if kind == "tp2":
            return {"resolution": "tp2", "pnl_pct": tp2_pct, "exit_ts": int(start_ms[lo_idx + idx]),
                    "strike": strike, "entry_credit": entry_credit}
        mid = bs.price(side, close[lo_idx + idx], strike, T[idx], sigma)
        buyback = mid * (1 + HALF_SPREAD)
        pnl_pct = (entry_credit - buyback) / entry_credit if entry_credit > 0 else 0.0
        return {"resolution": "vol_stop", "pnl_pct": pnl_pct, "exit_ts": int(start_ms[lo_idx + idx]),
                "strike": strike, "entry_credit": entry_credit}

    final_mid = bs.price(side, close[hi_idx - 1], strike, T[-1], sigma)
    buyback = final_mid * (1 + HALF_SPREAD)
    pnl_pct = (entry_credit - buyback) / entry_credit if entry_credit > 0 else 0.0
    return {"resolution": "time_stop", "pnl_pct": pnl_pct, "exit_ts": int(start_ms[hi_idx - 1]),
            "strike": strike, "entry_credit": entry_credit}


def coin_trades(coin: str, sides_enabled=("P", "C"), put_gen: dict | None = None,
                call_gen: dict | None = None, put_exit: dict | None = None,
                call_exit: dict | None = None, vol_guard: dict | None = None,
                vol_stop_accel: float | None = None, vol_stop_require_loss: bool = False,
                vol_stop_sides: tuple[str, ...] | None = None,
                vol_stop_regimes: tuple[str, ...] | None = None,
                expiry_h: float = jc.TARGET_EXPIRY_H,
                sigma_calib: dict | None = None) -> list[dict]:
    """put_exit/call_exit default to jc.PUT_EXIT/CALL_EXIT (live-deployed) but
    accept overrides — used by the exit-parameter sweep. vol_guard/
    vol_stop_accel are the experimental entry-guard/exit-stop from
    research/sweep_vol_guard.py — both off (None) by default.

    vol_stop_sides/vol_stop_regimes (round-3 surgical scoping, 2026-08-02):
    restrict vol_stop_accel to fire only for trades whose side/regime match
    (e.g. side='C', regime in ('trend','transition') — the exact condition
    behind the 2026-07-28 losing stretch). None = unrestricted (round-2
    behavior). Trades outside the scope get vol_track=None, i.e. price-only
    exits, same as baseline.

    expiry_h (tenor sweep, 2026-08-02, see research/sweep_tenor.py): the REAL
    option tenor priced at entry, passed straight through to
    simulate_option_exit. Defaults to jc.TARGET_EXPIRY_H (168h, weekly, live
    default). put_exit/call_exit hold_h is NOT auto-rescaled here — callers
    testing a shorter tenor must pass an hold_h-adjusted put_exit/call_exit
    themselves (see sweep_tenor.py's scale_exit).

    sigma_calib (2026-08-02, see sweep_sigma_calibration.py): pricing-sigma
    recalibration, {'b0', 'b1', 'floor', 'ceiling'} -> sigma =
    clip(b0 + b1*rv1h_native, floor, ceiling), replacing the raw
    SIGMA_CLAMP clip. Fit from 6wk of real Bybit markIv vs this engine's own
    rv1h_native over the same window (fresh_data/iv_history_eth.jsonl) —
    the live pricing sigma was found to run structurally hotter than real
    option IV. None (default) = unchanged SIGMA_CLAMP behavior. Entry gates
    are untouched either way (percentile-rank based on raw rv1h_native, a
    monotonic sigma transform doesn't change which trades fire)."""
    put_exit = jc.PUT_EXIT if put_exit is None else put_exit
    call_exit = jc.CALL_EXIT if call_exit is None else call_exit
    base = build_coin_base(coin)
    sig = evaluate_gates(base, put_gen=put_gen, call_gen=call_gen, vol_guard=vol_guard)
    d5 = load_klines(coin, "5m")
    close, high, low = d5["close"].values, d5["high"].values, d5["low"].values
    start_ms_arr = d5["start_ms"].values
    d1h = load_klines(coin, "1h")
    rv1h_raw = rolling_realized_vol(d1h["close"], lookback=24)
    if sigma_calib is None:
        rv1h = rv1h_raw.clip(*SIGMA_CLAMP)
    else:
        rv1h = (sigma_calib["b0"] + sigma_calib["b1"] * rv1h_raw).clip(
            sigma_calib["floor"], sigma_calib["ceiling"])
    # map each 5m bar to nearest-past 1h sigma reading
    sig = pd.merge_asof(sig, d1h[["start_ms"]].assign(sigma=rv1h.values), on="start_ms", direction="backward")

    vol_track_arr = None
    if vol_stop_accel is not None:
        # UNCLAMPED rv1h broadcast to the 5m grid — used only for the
        # vol_stop trigger, not for BS pricing (which uses the clamped sigma).
        df_rv_native = pd.DataFrame({"start_ms": base["start_ms_1h"], "rv1h_native": base["rv1h_native"]})
        bcast = pd.merge_asof(pd.DataFrame({"start_ms": start_ms_arr}), df_rv_native,
                              on="start_ms", direction="backward")
        vol_track_arr = bcast["rv1h_native"].values

    ready_P = sig["ready_P"].values
    ready_C = sig["ready_C"].values
    start_ms_sig = sig["start_ms"].values
    sigma_arr = sig["sigma"].values
    regime_arr = sig["regime"].values

    trades = []
    cooldown_until = {"P": -1, "C": -1}
    n = len(sig)
    for i in range(n):
        for side in ("P", "C"):
            if side not in sides_enabled or side not in jc.COIN_SIDES[coin]:
                continue
            ready = ready_P[i] if side == "P" else ready_C[i]
            if not ready:
                continue
            if start_ms_sig[i] < cooldown_until[side]:
                continue
            sigma = sigma_arr[i]
            if pd.isna(sigma) or sigma <= 0:
                continue
            ex = put_exit if side == "P" else call_exit
            in_scope = ((vol_stop_sides is None or side in vol_stop_sides) and
                       (vol_stop_regimes is None or str(regime_arr[i]) in vol_stop_regimes))
            out = simulate_option_exit(side, i, close, high, low, start_ms_arr, float(sigma),
                                       ex["tp2_pct"], ex["sl_pct"], ex["hold_h"], jc.STRIKE_ROUND[coin],
                                       expiry_h=expiry_h,
                                       vol_track=vol_track_arr if in_scope else None,
                                       vol_stop_accel=vol_stop_accel if in_scope else None,
                                       vol_stop_require_loss=vol_stop_require_loss)
            cooldown_until[side] = start_ms_sig[i] + jc.COOLDOWN_BARS * 300_000
            if out is None:
                continue
            trades.append({
                "coin": coin, "side": side, "entry_ts": int(start_ms_sig[i]),
                "exit_ts": int(out["exit_ts"]), "resolution": out["resolution"],
                "pnl_pct": out["pnl_pct"], "strike": out["strike"],
                "entry_credit": out["entry_credit"], "lot": {"ETH": 0.1, "BTC": 0.01}[coin],
                "regime": str(regime_arr[i]),
            })
    return trades


def replay_account(trades: list[dict], mo: int, cap: int, start_equity: float = jc.START_EQUITY_USD,
                   cb_mode: str = "per_key", size_mult_fn=None) -> dict:
    """cb_mode: 'per_key' (live default since 2026-08-01 — CB keyed by
    'coin:side', a loss on one leg doesn't pause the others) or 'global'
    (pre-2026-08-01 behavior — any loss pauses ALL entries for CB_PAUSE_HOURS).
    size_mult_fn(trade) -> float, default None (= 1.0 for every trade) — used
    to test regime-aware position sizing (e.g. half-size CALLs when
    regime=='trend') on top of the live dyn_size_factor logic."""
    trades = sorted(trades, key=lambda t: t["entry_ts"])
    equity = start_equity
    peak = equity
    max_dd = 0.0
    open_positions: list[dict] = []  # {coin, exit_ts, margin}
    recent_pnls: list[float] = []
    cb_until_global = -1
    cb_until_by_key: dict[str, int] = {}
    n_taken = 0
    n_skipped_cap = 0
    equity_curve = []

    for t in trades:
        now = t["entry_ts"]
        cb_key = f"{t['coin']}:{t['side']}"
        open_positions = [p for p in open_positions if p["exit_ts"] > now]
        if cb_mode == "per_key":
            if now < cb_until_by_key.get(cb_key, -1):
                continue
        elif now < cb_until_global:
            continue
        if len(open_positions) >= mo:
            n_skipped_cap += 1
            continue
        if sum(1 for p in open_positions if p["coin"] == t["coin"]) >= cap:
            n_skipped_cap += 1
            continue
        used_margin = sum(p["margin"] for p in open_positions)
        mult = size_mult_fn(t) if size_mult_fn is not None else 1.0
        qty, margin = jc.size_position(equity, used_margin, recent_pnls,
                                       t["strike"], t["entry_credit"], t["lot"], size_mult=mult)
        if qty <= 0:
            continue
        notional = t["strike"] * qty
        premium_total = t["entry_credit"] * qty
        fee_open = jc.fee_usd(notional, premium_total)
        exit_credit = t["entry_credit"] * (1 - t["pnl_pct"])
        fee_close = jc.fee_usd(notional, exit_credit * qty)
        pnl_usd = (t["entry_credit"] - exit_credit) * qty - fee_open - fee_close
        equity += pnl_usd
        n_taken += 1
        recent_pnls.append(t["pnl_pct"])
        recent_pnls = recent_pnls[-50:]
        if t["pnl_pct"] <= 0:
            cb_until_ms = t["exit_ts"] + jc.CB_PAUSE_HOURS * 3_600_000
            if cb_mode == "per_key":
                cb_until_by_key[cb_key] = cb_until_ms
            else:
                cb_until_global = cb_until_ms
        open_positions.append({"coin": t["coin"], "exit_ts": t["exit_ts"], "margin": margin})
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100 if peak > 0 else 0)
        equity_curve.append((t["exit_ts"], equity))

    return {"final": equity, "max_dd": max_dd, "n_taken": n_taken,
            "n_skipped_cap": n_skipped_cap, "curve": equity_curve,
            "return_pct": (equity - start_equity) / start_equity * 100}


def split(trades: list[dict], train_frac: float = 0.70) -> tuple[list[dict], list[dict]]:
    trades = sorted(trades, key=lambda t: t["entry_ts"])
    if not trades:
        return [], []
    ts_all = [t["entry_ts"] for t in trades]
    split_ts = ts_all[0] + train_frac * (ts_all[-1] - ts_all[0])
    tr = [t for t in trades if t["entry_ts"] < split_ts]
    ho = [t for t in trades if t["entry_ts"] >= split_ts]
    return tr, ho


def quarters(trades: list[dict]) -> list[list[dict]]:
    trades = sorted(trades, key=lambda t: t["entry_ts"])
    if not trades:
        return [[], [], [], []]
    t0, t1 = trades[0]["entry_ts"], trades[-1]["entry_ts"]
    step = (t1 - t0) / 4
    out = [[] for _ in range(4)]
    for t in trades:
        q = min(3, int((t["entry_ts"] - t0) / step)) if step > 0 else 0
        out[q].append(t)
    return out
