"""Trailing profit-lock for Jony's option exits — ported concept from
Tyagach (2026-08-03 session), but a DIFFERENT mechanism than what
sweep_vol_guard.py already tested and rejected (3 rounds, see
SESSION_HANDOFF_2026-08-02.md sec 5/7): that guard reacted to REALIZED VOL
as an indirect proxy for risk. This is not that -- it trails the position's
OWN pnl_pct_mark (the exact quantity manage_exits() already checks every
minute live), which vol-guard never touched.

Why this might help where vol-guard didn't: Jony's TP2 is a FIXED,
single-shot threshold (loop.py: `if pnl_pct_mark >= tp2_pct: close`).
Selling premium, max theoretical profit is pnl_pct_mark=1.0 (expires
worthless) -- capping every winner at exactly tp2_pct=0.70 forfeits
whatever decay would have happened between 70% and 100% on trades that
would have kept decaying. A trailing exit lets a winner run past 70% while
protecting against a reversal, instead of taking profit at a fixed line
regardless of what the position is actually doing.

Mechanic (vectorized, first-passage over 5m bars, same convention as
simulate_option_exit's own SL/TP: best-case-per-bar for the favorable
extreme, worst-case-per-bar for the unfavorable one -- NOT bar-close, to
stay consistent with how sl_mid/tp2_mid already threshold off
premium_high/premium_low rather than a close series):
  pnl_low_frac[i]  = best-case pnl this bar (using the bar's cheapest buyback)
  pnl_high_frac[i] = worst-case pnl this bar (using the bar's priciest buyback)
  peak[i] = running max of pnl_low_frac up to bar i
  armed[i] = peak[i] >= arm_pct
  fires when armed[i] and pnl_high_frac[i] <= peak[i] * (1 - trail_frac)
Priority on a same-bar tie: sl > trail > tp2 (protect capital first, same
ordering principle as the existing sl > vol_stop > tp2 priority).

Two variants tested per side:
  cap_tp2=True  -- trail runs ALONGSIDE the existing tp2 cap (tp2 still
                   fires if price marches straight to 70% decay without
                   ever giving back enough to arm+trail first)
  cap_tp2=False -- tp2 REMOVED, only trail (and sl/time_stop) can close a
                   winner -- tests whether the fixed 70% cap is leaving
                   money on the table

Self-check: arm_pct=1e9 (never arms), cap_tp2=True must reproduce
je.coin_trades()'s pnl_pct EXACTLY, trade-for-trade -- run --selfcheck
before trusting any swept result.

Run: python3 trailing_exit_sweep.py [--selfcheck]
"""
from __future__ import annotations

import multiprocessing as mp
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je
import backtest_bs as bs
from jony_engine import HALF_SPREAD, _vec_bs_price

MO, CAP, CB_MODE = 6, 4, "per_key"


def simulate_option_exit_trailing(side, entry_idx, close, high, low, start_ms, sigma,
                                   tp2_pct, sl_pct, hold_h, strike_round,
                                   arm_pct, trail_frac, cap_tp2=True,
                                   expiry_h=jc.TARGET_EXPIRY_H):
    spot0 = close[entry_idx]
    strike = round(spot0 / strike_round) * strike_round
    T0 = expiry_h / (24 * 365)
    entry_mid = bs.price(side, spot0, strike, T0, sigma)
    if entry_mid <= 0.01:
        return None
    entry_credit = entry_mid * (1 - HALF_SPREAD)
    tp2_mid = entry_credit * (1 - tp2_pct) / (1 + HALF_SPREAD)
    sl_mid = entry_credit * (1 + sl_pct) / (1 + HALF_SPREAD)

    bars_limit = int(hold_h * 12)
    lo_idx, hi_idx = entry_idx + 1, min(entry_idx + 1 + bars_limit, len(close))
    if hi_idx <= lo_idx:
        return None
    hi_spot, lo_spot = high[lo_idx:hi_idx], low[lo_idx:hi_idx]
    m = hi_idx - lo_idx
    elapsed_h = np.arange(1, m + 1) * 5 / 60
    T = np.maximum(0.0, (expiry_h - elapsed_h) / (24 * 365))
    if side == "C":
        premium_high = _vec_bs_price(side, hi_spot, strike, T, sigma)
        premium_low = _vec_bs_price(side, lo_spot, strike, T, sigma)
    else:
        premium_high = _vec_bs_price(side, lo_spot, strike, T, sigma)
        premium_low = _vec_bs_price(side, hi_spot, strike, T, sigma)

    sl_hits = np.flatnonzero(premium_high >= sl_mid)
    first_sl = sl_hits[0] if len(sl_hits) else None

    pnl_low_frac = (entry_credit - premium_low * (1 + HALF_SPREAD)) / entry_credit
    pnl_high_frac = (entry_credit - premium_high * (1 + HALF_SPREAD)) / entry_credit
    peak = np.maximum.accumulate(pnl_low_frac)
    armed = peak >= arm_pct
    giveback = armed & (pnl_high_frac <= peak * (1 - trail_frac))
    trail_hits = np.flatnonzero(giveback)
    first_trail = trail_hits[0] if len(trail_hits) else None

    first_tp = None
    if cap_tp2:
        tp_hits = np.flatnonzero(premium_low <= tp2_mid)
        first_tp = tp_hits[0] if len(tp_hits) else None

    candidates = [(idx, rank, kind) for idx, rank, kind in
                 ((first_sl, 0, "sl"), (first_trail, 1, "trail"), (first_tp, 2, "tp2")) if idx is not None]
    if candidates:
        idx, _, kind = min(candidates)
        if kind == "sl":
            return {"resolution": "sl", "pnl_pct": -sl_pct, "exit_ts": int(start_ms[lo_idx + idx]),
                    "strike": strike, "entry_credit": entry_credit}
        if kind == "tp2":
            return {"resolution": "tp2", "pnl_pct": tp2_pct, "exit_ts": int(start_ms[lo_idx + idx]),
                    "strike": strike, "entry_credit": entry_credit}
        return {"resolution": "trail", "pnl_pct": float(pnl_high_frac[idx]), "exit_ts": int(start_ms[lo_idx + idx]),
                "strike": strike, "entry_credit": entry_credit}

    final_mid = bs.price(side, close[hi_idx - 1], strike, T[-1], sigma)
    buyback = final_mid * (1 + HALF_SPREAD)
    pnl_pct = (entry_credit - buyback) / entry_credit if entry_credit > 0 else 0.0
    return {"resolution": "time_stop", "pnl_pct": pnl_pct, "exit_ts": int(start_ms[hi_idx - 1]),
            "strike": strike, "entry_credit": entry_credit}


def coin_trades_trailing(coin, put_exit=None, call_exit=None, arm_pct=1e9, trail_frac=0.5,
                         cap_tp2=True, sides_enabled=("P", "C")):
    put_exit = jc.PUT_EXIT if put_exit is None else put_exit
    call_exit = jc.CALL_EXIT if call_exit is None else call_exit
    base = je.build_coin_base(coin)
    sig = je.evaluate_gates(base)
    d5 = je.load_klines(coin, "5m")
    close, high, low = d5["close"].values, d5["high"].values, d5["low"].values
    start_ms_arr = d5["start_ms"].values
    d1h = je.load_klines(coin, "1h")
    rv1h = je.rolling_realized_vol(d1h["close"], lookback=24).clip(*je.SIGMA_CLAMP)
    sig = je.pd.merge_asof(sig, d1h[["start_ms"]].assign(sigma=rv1h.values), on="start_ms", direction="backward")

    ready_P, ready_C = sig["ready_P"].values, sig["ready_C"].values
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
            if je.pd.isna(sigma) or sigma <= 0:
                continue
            ex = put_exit if side == "P" else call_exit
            out = simulate_option_exit_trailing(side, i, close, high, low, start_ms_arr, float(sigma),
                                                ex["tp2_pct"], ex["sl_pct"], ex["hold_h"], jc.STRIKE_ROUND[coin],
                                                arm_pct=arm_pct, trail_frac=trail_frac, cap_tp2=cap_tp2)
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


def days(trades):
    return (trades[-1]["entry_ts"] - trades[0]["entry_ts"]) / 86_400_000 if trades else 0.0


def selfcheck():
    print("[selfcheck] arm_pct=1e9 (never arms), cap_tp2=True must equal je.coin_trades() exactly...")
    ok = True
    for coin in ("ETH", "BTC"):
        base = je.coin_trades(coin)
        trail = coin_trades_trailing(coin, arm_pct=1e9, trail_frac=0.5, cap_tp2=True)
        base_pnls = [round(t["pnl_pct"], 8) for t in base]
        trail_pnls = [round(t["pnl_pct"], 8) for t in trail]
        n_trail_fired = sum(1 for t in trail if t["resolution"] == "trail")
        match = base_pnls == trail_pnls and n_trail_fired == 0
        if not match:
            ok = False
        print(f"  {coin}: n_base={len(base_pnls)} n_trail_run={len(trail_pnls)} "
              f"n_trail_fired={n_trail_fired} match={match}")
    print(f"[selfcheck] {'PASS' if ok else 'FAIL'}")
    return ok


ARM_GRID = (0.10, 0.20, 0.30, 0.40, 0.50)
TRAIL_GRID = (0.2, 0.3, 0.5, 0.7)


def eval_variant(args):
    label, side, arm_pct, trail_frac, cap_tp2 = args
    put_exit = dict(jc.PUT_EXIT)
    call_exit = dict(jc.CALL_EXIT)
    sides_enabled = (side,) if side else ("P", "C")
    trades = coin_trades_trailing("ETH", put_exit=put_exit, call_exit=call_exit,
                                  arm_pct=arm_pct, trail_frac=trail_frac, cap_tp2=cap_tp2,
                                  sides_enabled=sides_enabled)
    if side is None or side in jc.COIN_SIDES["BTC"]:
        trades += coin_trades_trailing("BTC", put_exit=put_exit, call_exit=call_exit,
                                       arm_pct=arm_pct, trail_frac=trail_frac, cap_tp2=cap_tp2,
                                       sides_enabled=sides_enabled)
    tr, ho = je.split(trades, 0.70)
    r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE)
    r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE)
    n_trail = sum(1 for t in trades if t["resolution"] == "trail")
    return {
        "label": label, "n_trail": n_trail, "n_total": len(trades),
        "train_ret": r_tr["return_pct"], "train_dd": r_tr["max_dd"],
        "train_tpd": r_tr["n_taken"] / days(tr) if tr else 0.0,
        "holdout_ret": r_ho["return_pct"], "holdout_dd": r_ho["max_dd"],
        "holdout_tpd": r_ho["n_taken"] / days(ho) if ho else 0.0,
    }


def build_variants():
    variants = [("baseline (live tp2/sl, no trail)", None, 1e9, 0.5, True)]
    for side_label, side in (("PUT", "P"), ("CALL", "C")):
        for cap in (True, False):
            cap_label = "capped@tp2" if cap else "uncapped"
            for arm in ARM_GRID:
                for trail in TRAIL_GRID:
                    label = f"{side_label} {cap_label} arm={arm} trail={trail}"
                    variants.append((label, side, arm, trail, cap))
    return variants


def print_table(results):
    base = next(r for r in results if r["label"].startswith("baseline"))
    hdr = (f"{'label':46s} {'n_trail':>8s} {'train_ret':>10s} {'train_dd':>9s} {'tr_tpd':>7s} "
          f"{'holdout_ret':>12s} {'ho_dd':>7s} {'ho_tpd':>7s}")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        flag = ""
        if r["label"] != base["label"]:
            improves = r["train_ret"] > base["train_ret"] and r["holdout_ret"] > base["holdout_ret"]
            dd_ok = r["holdout_dd"] <= base["holdout_dd"] * 1.15
            flag = " <-- candidate" if (improves and dd_ok) else ""
        print(f"{r['label']:46s} {r['n_trail']:8d} {r['train_ret']:+9.1f}% {r['train_dd']:8.1f}% "
              f"{r['train_tpd']:6.2f} {r['holdout_ret']:+11.1f}% {r['holdout_dd']:6.1f}% "
              f"{r['holdout_tpd']:6.2f}{flag}")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        ok = selfcheck()
        sys.exit(0 if ok else 1)

    variants = build_variants()
    print(f"Running {len(variants)} trailing-exit variants sequentially (mo={MO} cap={CAP})...")
    results = []
    for i, v in enumerate(variants):
        results.append(eval_variant(v))
        print(f"  [{i+1}/{len(variants)}] {v[0]}", flush=True)
    print_table(results)
