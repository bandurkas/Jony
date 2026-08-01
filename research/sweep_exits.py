"""Exit-parameter sweep (TP2/SL/hold_h for PUT and CALL) — untouched all
session so far; only entry gates (sweep_thresholds.py) and CALL trend-regime
sizing (sweep_regime_sizing.py) have been tested. Same single-lever
sensitivity methodology, evaluated on top of config E entry gates
(jc.PUT_GEN/CALL_GEN, live) with the live account backbone (per-key CB,
MO6/cap4).

Run: python3 sweep_exits.py
"""
from __future__ import annotations

import copy
import multiprocessing as mp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je

MO, CAP, CB_MODE = 6, 4, "per_key"

BASE_PUT_EXIT = dict(jc.PUT_EXIT)     # tp2_pct=0.70 sl_pct=2.00 hold_h=96
BASE_CALL_EXIT = dict(jc.CALL_EXIT)   # tp2_pct=0.80 sl_pct=0.75 hold_h=24


def days(trades: list[dict]) -> float:
    return (trades[-1]["entry_ts"] - trades[0]["entry_ts"]) / 86_400_000 if trades else 0.0


def eval_variant(args) -> dict:
    label, put_ov, call_ov = args
    put_exit = dict(BASE_PUT_EXIT, **(put_ov or {}))
    call_exit = dict(BASE_CALL_EXIT, **(call_ov or {}))
    trades = je.coin_trades("ETH", put_exit=put_exit, call_exit=call_exit) + \
             je.coin_trades("BTC", put_exit=put_exit, call_exit=call_exit)
    tr, ho = je.split(trades, 0.70)
    r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE)
    r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE)
    return {
        "label": label,
        "train_ret": r_tr["return_pct"], "train_dd": r_tr["max_dd"],
        "train_tpd": r_tr["n_taken"] / days(tr) if tr else 0.0,
        "holdout_ret": r_ho["return_pct"], "holdout_dd": r_ho["max_dd"],
        "holdout_tpd": r_ho["n_taken"] / days(ho) if ho else 0.0,
    }


VARIANTS = [
    ("baseline (config E exits)", None, None),
    # CALL tp2_pct: lower = easier to hit (less premium decay needed) -> faster/more frequent profit-taking, smaller per-trade win
    ("CALL tp2 0.80->0.70", None, {"tp2_pct": 0.70}),
    ("CALL tp2 0.80->0.75", None, {"tp2_pct": 0.75}),
    ("CALL tp2 0.80->0.85", None, {"tp2_pct": 0.85}),
    ("CALL tp2 0.80->0.90", None, {"tp2_pct": 0.90}),
    # CALL sl_pct: lower = tighter stop (cuts losers faster, less room for noise)
    ("CALL sl 0.75->0.60", None, {"sl_pct": 0.60}),
    ("CALL sl 0.75->0.65", None, {"sl_pct": 0.65}),
    ("CALL sl 0.75->0.85", None, {"sl_pct": 0.85}),
    ("CALL sl 0.75->1.00", None, {"sl_pct": 1.00}),
    # CALL hold_h: shorter = forces time-stop sooner if neither TP/SL hit
    ("CALL hold 24->12", None, {"hold_h": 12}),
    ("CALL hold 24->18", None, {"hold_h": 18}),
    ("CALL hold 24->36", None, {"hold_h": 36}),
    ("CALL hold 24->48", None, {"hold_h": 48}),
    # PUT tp2_pct
    ("PUT tp2 0.70->0.60", {"tp2_pct": 0.60}, None),
    ("PUT tp2 0.70->0.65", {"tp2_pct": 0.65}, None),
    ("PUT tp2 0.70->0.75", {"tp2_pct": 0.75}, None),
    ("PUT tp2 0.70->0.80", {"tp2_pct": 0.80}, None),
    # PUT sl_pct
    ("PUT sl 2.00->1.50", {"sl_pct": 1.50}, None),
    ("PUT sl 2.00->1.75", {"sl_pct": 1.75}, None),
    ("PUT sl 2.00->2.25", {"sl_pct": 2.25}, None),
    # PUT hold_h
    ("PUT hold 96->48", {"hold_h": 48}, None),
    ("PUT hold 96->72", {"hold_h": 72}, None),
    ("PUT hold 96->120", {"hold_h": 120}, None),
]


def print_table(results: list[dict]) -> None:
    base = next(r for r in results if r["label"].startswith("baseline"))
    hdr = f"{'label':30s} {'train_ret':>10s} {'train_dd':>9s} {'tr_tpd':>7s} {'holdout_ret':>12s} {'ho_dd':>7s} {'ho_tpd':>7s}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        flag = ""
        if r["label"] != base["label"]:
            improves = r["train_ret"] > base["train_ret"] and r["holdout_ret"] > base["holdout_ret"]
            dd_ok = r["holdout_dd"] <= base["holdout_dd"] * 1.15
            flag = " <-- candidate" if (improves and dd_ok) else ""
        print(f"{r['label']:30s} {r['train_ret']:+9.1f}% {r['train_dd']:8.1f}% {r['train_tpd']:6.2f} "
             f"{r['holdout_ret']:+11.1f}% {r['holdout_dd']:6.1f}% {r['holdout_tpd']:6.2f}{flag}")


if __name__ == "__main__":
    n_workers = min(len(VARIANTS), mp.cpu_count())
    print(f"Running {len(VARIANTS)} exit variants on {n_workers} workers (mo={MO} cap={CAP})...")
    with mp.Pool(n_workers) as pool:
        results = pool.map(eval_variant, VARIANTS)
    print_table(results)
