"""Volatility-acceleration guard/stop sweep — user's idea 2026-08-02, prompted
by the live-since-start simulation showing a losing stretch (trades #28-37)
that traced back to a ~4-5x realized-vol spike hitting ETH+BTC simultaneously
on 2026-07-28 (see chat: rv1h jumped 0.137->0.644 ETH, 0.095->0.402 BTC in
one day). Idea: instead of only reacting to price (today's TP2/SL), react to
vol itself — either don't enter when vol is already accelerating (entry
guard), or buy back early when vol accelerates mid-hold regardless of price
(exit vol_stop).

Important distinction from the existing vol_threshold gate: vol_threshold
requires current vol to be elevated vs its OWN trailing percentile (rewards
entering mid-spike — that's the VRP premise). This guard instead looks at
the DERIVATIVE — is vol still rising right now — which vol_threshold cannot
see. The two are meant to compose, not replace each other.

Both mechanisms implemented in jony_engine.py (evaluate_gates' vol_guard
param, simulate_option_exit's vol_track/vol_stop_accel params), off by
default — see their docstrings for exact semantics.

Run: python3 sweep_vol_guard.py
"""
from __future__ import annotations

import multiprocessing as mp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je

MO, CAP, CB_MODE = 6, 4, "per_key"


def days(trades: list[dict]) -> float:
    return (trades[-1]["entry_ts"] - trades[0]["entry_ts"]) / 86_400_000 if trades else 0.0


def eval_entry_guard(args) -> dict:
    label, lookback_h, max_accel = args
    vol_guard = None if max_accel is None else {"lookback_h": lookback_h, "max_accel": max_accel}
    trades = je.coin_trades("ETH", vol_guard=vol_guard) + je.coin_trades("BTC", vol_guard=vol_guard)
    tr, ho = je.split(trades, 0.70)
    r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE)
    r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE)
    return {
        "label": label,
        "train_ret": r_tr["return_pct"], "train_dd": r_tr["max_dd"],
        "train_tpd": r_tr["n_taken"] / days(tr) if tr else 0.0,
        "holdout_ret": r_ho["return_pct"], "holdout_dd": r_ho["max_dd"],
        "holdout_tpd": r_ho["n_taken"] / days(ho) if ho else 0.0,
        "n_total": len(trades),
    }


def eval_vol_stop(args) -> dict:
    label, accel, require_loss = args
    trades = je.coin_trades("ETH", vol_stop_accel=accel, vol_stop_require_loss=require_loss) + \
             je.coin_trades("BTC", vol_stop_accel=accel, vol_stop_require_loss=require_loss)
    tr, ho = je.split(trades, 0.70)
    r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE)
    r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE)
    n_vol_stop = sum(1 for t in trades if t["resolution"] == "vol_stop")
    return {
        "label": label,
        "train_ret": r_tr["return_pct"], "train_dd": r_tr["max_dd"],
        "train_tpd": r_tr["n_taken"] / days(tr) if tr else 0.0,
        "holdout_ret": r_ho["return_pct"], "holdout_dd": r_ho["max_dd"],
        "holdout_tpd": r_ho["n_taken"] / days(ho) if ho else 0.0,
        "n_vol_stop": n_vol_stop,
    }


ENTRY_GUARD_VARIANTS = [
    ("baseline (no guard)", None, None),
    ("guard 6h / 1.3x", 6, 1.3),
    ("guard 6h / 1.5x", 6, 1.5),
    ("guard 6h / 2.0x", 6, 2.0),
    ("guard 12h / 1.3x", 12, 1.3),
    ("guard 12h / 1.5x", 12, 1.5),
    ("guard 12h / 2.0x", 12, 2.0),
    ("guard 24h / 1.3x", 24, 1.3),
    ("guard 24h / 1.5x", 24, 1.5),
    ("guard 24h / 2.0x", 24, 2.0),
]

VOL_STOP_VARIANTS = [
    ("baseline (no vol_stop)", None, False),
    ("vol_stop 1.3x", 1.3, False),
    ("vol_stop 1.5x", 1.5, False),
    ("vol_stop 1.75x", 1.75, False),
    ("vol_stop 2.0x", 2.0, False),
    ("vol_stop 2.5x", 2.5, False),
    ("vol_stop 3.0x", 3.0, False),
]

# Round 2 (2026-08-02): round 1 found a pure vol-level trigger clips winners
# that see a transient vol uptick before recovering to TP2 — every variant
# underperformed baseline. Refinement: only fire the stop when the position
# is ALSO already underwater at that bar (mark-implied pnl < 0), so it can
# only cut losses faster, never interrupt a trade that's still winning.
VOL_STOP_GATED_VARIANTS = [
    ("baseline (no vol_stop)", None, False),
    ("gated vol_stop 1.3x", 1.3, True),
    ("gated vol_stop 1.5x", 1.5, True),
    ("gated vol_stop 1.75x", 1.75, True),
    ("gated vol_stop 2.0x", 2.0, True),
    ("gated vol_stop 2.5x", 2.5, True),
]


def eval_vol_stop_quarters(args) -> tuple:
    label, accel, require_loss = args
    trades = je.coin_trades("ETH", vol_stop_accel=accel, vol_stop_require_loss=require_loss) + \
             je.coin_trades("BTC", vol_stop_accel=accel, vol_stop_require_loss=require_loss)
    qs = je.quarters(trades)
    out = []
    for i, q in enumerate(qs):
        if not q:
            out.append((i, 0, 0))
            continue
        r = je.replay_account(q, MO, CAP, cb_mode=CB_MODE)
        out.append((i, r["return_pct"], r["max_dd"]))
    return label, out


def print_table(results: list[dict], base_label_prefix: str) -> None:
    base = next(r for r in results if r["label"].startswith(base_label_prefix))
    hdr = f"{'label':22s} {'train_ret':>10s} {'train_dd':>9s} {'tr_tpd':>7s} {'holdout_ret':>12s} {'ho_dd':>7s} {'ho_tpd':>7s}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        flag = ""
        if r["label"] != base["label"]:
            improves = r["train_ret"] > base["train_ret"] and r["holdout_ret"] > base["holdout_ret"]
            dd_ok = r["holdout_dd"] <= base["holdout_dd"]
            flag = " <-- candidate" if (improves and dd_ok) else ""
        print(f"{r['label']:22s} {r['train_ret']:+9.1f}% {r['train_dd']:8.1f}% {r['train_tpd']:6.2f} "
             f"{r['holdout_ret']:+11.1f}% {r['holdout_dd']:6.1f}% {r['holdout_tpd']:6.2f}{flag}")


if __name__ == "__main__":
    print("=== Entry-side vol-acceleration guard ===")
    with mp.Pool(min(len(ENTRY_GUARD_VARIANTS), mp.cpu_count())) as pool:
        eg_results = pool.map(eval_entry_guard, ENTRY_GUARD_VARIANTS)
    print_table(eg_results, "baseline")

    print("\n=== Exit-side proactive vol_stop (round 1: pure vol-level trigger) ===")
    with mp.Pool(min(len(VOL_STOP_VARIANTS), mp.cpu_count())) as pool:
        vs_results = pool.map(eval_vol_stop, VOL_STOP_VARIANTS)
    print_table(vs_results, "baseline")
    for r in vs_results:
        if "n_vol_stop" in r:
            print(f"  {r['label']:22s} n_vol_stop={r['n_vol_stop']}")

    print("\n=== Exit-side proactive vol_stop (round 2: loss-gated) ===")
    with mp.Pool(min(len(VOL_STOP_GATED_VARIANTS), mp.cpu_count())) as pool:
        vsg_results = pool.map(eval_vol_stop, VOL_STOP_GATED_VARIANTS)
    print_table(vsg_results, "baseline")
    for r in vsg_results:
        if "n_vol_stop" in r:
            print(f"  {r['label']:22s} n_vol_stop={r['n_vol_stop']}")
