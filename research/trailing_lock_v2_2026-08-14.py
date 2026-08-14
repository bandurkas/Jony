"""Profit-protection exits — mechanical test of the user's live observation
(2026-08-14): "positions show MTM profit after 1-3 days, then a sharp move
wipes most of it; exiting early without greed keeps the profit."

Three exit families, all CLOSE-mark based (consistent framework so variants
are comparable; baseline re-implemented in the same framework):
  base        — live tp2/sl/hold rules, close-based.
  trail(A,G)  — trailing profit lock (Tyagach-style, per position): once MTM
                profit (fraction of credit) reaches A, exit when it retraces
                to A-G... actually peak-G; disaster sl kept; hold_h=120 for
                room. Replaces tp2.
  profit@X    — at hour X, if position MTM profit > 0, close it (the literal
                "не жадничать" rule); tp2/sl/hold live otherwise.

Keys: ETH:P, ETH:C, BTC:C and (new, never honestly tested) BTC:P.
Sigma = CALIB (markIv-fitted, realistic). Selection on TRAIN only; holdout
reported alongside but the pick is by train. Research-only.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc

# BTC:P experimental key — must be patched BEFORE any build_coin_base call
jc.COIN_SIDES = {"ETH": ("P", "C"), "BTC": ("C", "P")}

import jony_engine as je
import backtest_bs as bs

CALIB = {"b0": 0.3487, "b1": 0.2646, "floor": 0.25, "ceiling": 1.05}
HALF_SPREAD = je.HALF_SPREAD
MAX_HOLD_H = 120
BARS = MAX_HOLD_H * 12

LIVE_EXIT = {"P": jc.PUT_EXIT, "C": jc.CALL_EXIT}


def build_paths(coin: str, side: str):
    """Signal entries for (coin,side) + per-trade MTM profit-fraction paths
    p[t] = (credit - buyback(t)) / credit at 5m close marks, t over 120h."""
    base = je.build_coin_base(coin)
    sig = je.evaluate_gates(base)
    d5 = je.load_klines(coin, "5m")
    close = d5["close"].values
    start_ms_arr = d5["start_ms"].values
    d1h = je.load_klines(coin, "1h")
    rv1h_raw = je.rolling_realized_vol(d1h["close"], lookback=24)
    sigma_series = (CALIB["b0"] + CALIB["b1"] * rv1h_raw).clip(CALIB["floor"], CALIB["ceiling"])
    sig = pd.merge_asof(sig, d1h[["start_ms"]].assign(sigma=sigma_series.values),
                        on="start_ms", direction="backward")
    ready = sig["ready_P"].values if side == "P" else sig["ready_C"].values
    sigma_arr = sig["sigma"].values
    sms = sig["start_ms"].values

    T0 = jc.TARGET_EXPIRY_H / (24 * 365)
    elapsed_h = np.arange(1, BARS + 1) * 5 / 60
    T_path = np.maximum(0.0, (jc.TARGET_EXPIRY_H - elapsed_h) / (24 * 365))

    trades = []
    cooldown_until = -1
    n = len(sig)
    for i in range(n):
        if not ready[i] or sms[i] < cooldown_until:
            continue
        sigma = sigma_arr[i]
        cooldown_next = sms[i] + jc.COOLDOWN_BARS * 300_000
        if pd.isna(sigma) or sigma <= 0:
            continue
        cooldown_until = cooldown_next
        spot0 = close[i]
        strike = round(spot0 / jc.STRIKE_ROUND[coin]) * jc.STRIKE_ROUND[coin]
        entry_mid = bs.price(side, spot0, strike, T0, float(sigma))
        if entry_mid <= 0.01:
            continue
        credit = entry_mid * (1 - HALF_SPREAD)
        hi = min(i + 1 + BARS, len(close))
        if hi <= i + 1:
            continue
        m = hi - (i + 1)
        mids = je._vec_bs_price(side, close[i + 1:hi], strike, T_path[:m], float(sigma))
        buyback = mids * (1 + HALF_SPREAD)
        p = (credit - buyback) / credit  # profit fraction path
        trades.append({"coin": coin, "side": side, "entry_ts": int(sms[i]),
                       "exit_ts_path": start_ms_arr[i + 1:hi], "p": p,
                       "strike": strike, "entry_credit": credit,
                       "lot": {"ETH": 0.1, "BTC": 0.01}[coin]})
    return trades


def apply_rule(tr: dict, rule: dict) -> tuple[float, int]:
    """Returns (pnl_pct, exit_ts) for one trade under an exit rule."""
    p = tr["p"]
    kind = rule["kind"]
    sl = rule["sl"]
    hold_bars = min(int(rule["hold_h"] * 12), len(p))
    pw = p[:hold_bars]
    sl_hits = np.flatnonzero(pw <= -sl)
    first_sl = sl_hits[0] if len(sl_hits) else None

    if kind == "base":
        tp_hits = np.flatnonzero(pw >= rule["tp2"])
        first_tp = tp_hits[0] if len(tp_hits) else None
        cands = [(x, r) for x, r in ((first_sl, 0), (first_tp, 1)) if x is not None]
        if cands:
            idx = min(cands)[0]
            return float(pw[idx]), int(tr["exit_ts_path"][idx])
    elif kind == "trail":
        peak = np.maximum.accumulate(pw)
        armed = peak >= rule["arm"]
        give = armed & (pw <= peak - rule["giveback"])
        hits = np.flatnonzero(give)
        first_tr = hits[0] if len(hits) else None
        cands = [(x, r) for x, r in ((first_sl, 0), (first_tr, 1)) if x is not None]
        if cands:
            idx = min(cands)[0]
            return float(pw[idx]), int(tr["exit_ts_path"][idx])
    elif kind == "profit_at":
        x_bar = int(rule["at_h"] * 12) - 1
        tp_hits = np.flatnonzero(pw >= rule["tp2"])
        first_tp = tp_hits[0] if len(tp_hits) else None
        first_px = None
        if x_bar < len(pw):
            # from hour X on, first bar with any profit
            later = np.flatnonzero(pw[x_bar:] > 0)
            if len(later):
                first_px = x_bar + later[0]
        cands = [(x, r) for x, r in ((first_sl, 0), (first_tp, 1), (first_px, 2)) if x is not None]
        if cands:
            idx = min(cands)[0]
            return float(pw[idx]), int(tr["exit_ts_path"][idx])
    return float(pw[-1]), int(tr["exit_ts_path"][hold_bars - 1])


def fixed_lot_usd(tr: dict, pnl_pct: float) -> float:
    qty = tr["lot"]
    notional = tr["strike"] * qty
    fee_open = jc.fee_usd(notional, tr["entry_credit"] * qty)
    exit_credit = tr["entry_credit"] * (1 - pnl_pct)
    fee_close = jc.fee_usd(notional, exit_credit * qty)
    return (tr["entry_credit"] - exit_credit) * qty - fee_open - fee_close


def main() -> None:
    t0 = time.time()
    results = []
    for coin, side in (("ETH", "P"), ("ETH", "C"), ("BTC", "C"), ("BTC", "P")):
        key = f"{coin}:{side}"
        trades = build_paths(coin, side)
        print(f"[{time.time() - t0:6.0f}s] {key}: {len(trades)} signal trades", flush=True)
        live = LIVE_EXIT[side]
        rules = [{"kind": "base", "label": "base(live)", "tp2": live["tp2_pct"],
                  "sl": live["sl_pct"], "hold_h": live["hold_h"]}]
        for arm in (0.20, 0.30, 0.40):
            for gb in (0.10, 0.15, 0.20):
                rules.append({"kind": "trail", "label": f"trail(arm{arm:.2f},gb{gb:.2f})",
                              "arm": arm, "giveback": gb, "sl": live["sl_pct"],
                              "hold_h": MAX_HOLD_H})
        for at_h in (24, 48, 72):
            rules.append({"kind": "profit_at", "label": f"profit@{at_h}h",
                          "at_h": at_h, "tp2": live["tp2_pct"], "sl": live["sl_pct"],
                          "hold_h": max(live["hold_h"], at_h + 24)})
        split_ts = trades[0]["entry_ts"] + 0.70 * (trades[-1]["entry_ts"] - trades[0]["entry_ts"])
        for rule in rules:
            outs = [(tr, *apply_rule(tr, rule)) for tr in trades]
            rec_tr = [(t, p) for t, p, _ in outs if t["entry_ts"] < split_ts]
            rec_ho = [(t, p) for t, p, _ in outs if t["entry_ts"] >= split_ts]
            row = {"key": key, "label": rule["label"]}
            for nm, rec in (("train", rec_tr), ("holdout", rec_ho)):
                usd = sum(fixed_lot_usd(t, p) for t, p in rec)
                wr = sum(1 for _, p in rec if p > 0) / len(rec) if rec else 0.0
                row[nm] = {"usd": usd, "wr": wr, "n": len(rec)}
            results.append(row)
        print(f"[{time.time() - t0:6.0f}s] {key} done", flush=True)

    Path(__file__).with_name("trailing_lock_results_2026-08-14.json").write_text(
        json.dumps(results))
    for key in ("ETH:P", "ETH:C", "BTC:C", "BTC:P"):
        rows = [r for r in results if r["key"] == key]
        rows.sort(key=lambda r: r["train"]["usd"], reverse=True)
        print(f"\n=== {key} (sorted by TRAIN fixed-lot $) ===")
        for r in rows:
            tr, ho = r["train"], r["holdout"]
            mark = " <-- base" if r["label"] == "base(live)" else ""
            print(f"  {r['label']:24s} train ${tr['usd']:+9.2f} WR {tr['wr']:5.1%} n={tr['n']:4d}"
                  f"  | holdout ${ho['usd']:+9.2f} WR {ho['wr']:5.1%} n={ho['n']:4d}{mark}")
    print(f"\ntotal {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
