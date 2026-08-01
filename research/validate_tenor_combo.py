"""Combine the winning single-lever candidates from sweep_tenor_exits.py
(72h tenor) and check quarter robustness — same methodology as the original
session's validate_combined.py for the 168h exit retune. Compares against
BOTH the 72h-scaled baseline (did the combo improve the tenor?) and the
live 168h baseline (does 72h + retuned exits actually beat weekly?).

Run: python3 validate_tenor_combo.py
"""
from __future__ import annotations

import multiprocessing as mp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je
from sweep_tenor import scale_exit

MO, CAP, CB_MODE = 6, 4, "per_key"
EXPIRY_H = 72.0

BASE72_PUT = scale_exit(jc.PUT_EXIT, EXPIRY_H)
BASE72_CALL = scale_exit(jc.CALL_EXIT, EXPIRY_H)


def days(trades: list[dict]) -> float:
    return (trades[-1]["entry_ts"] - trades[0]["entry_ts"]) / 86_400_000 if trades else 0.0


def run(expiry_h, put_exit, call_exit):
    trades = je.coin_trades("ETH", expiry_h=expiry_h, put_exit=put_exit, call_exit=call_exit) + \
             je.coin_trades("BTC", expiry_h=expiry_h, put_exit=put_exit, call_exit=call_exit)
    tr, ho = je.split(trades, 0.70)
    r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE)
    r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE)
    return trades, r_tr, r_ho


def eval_combo(args):
    label, put_ov, call_ov = args
    put_exit = dict(BASE72_PUT, **(put_ov or {}))
    call_exit = dict(BASE72_CALL, **(call_ov or {}))
    trades, r_tr, r_ho = run(EXPIRY_H, put_exit, call_exit)
    return {
        "label": label,
        "train_ret": r_tr["return_pct"], "train_dd": r_tr["max_dd"],
        "train_tpd": r_tr["n_taken"] / days(trades) if trades else 0.0,
        "holdout_ret": r_ho["return_pct"], "holdout_dd": r_ho["max_dd"],
        "put_exit": put_exit, "call_exit": call_exit,
    }


def eval_combo_quarters(args):
    label, put_ov, call_ov = args
    put_exit = dict(BASE72_PUT, **(put_ov or {}))
    call_exit = dict(BASE72_CALL, **(call_ov or {}))
    trades, _, _ = run(EXPIRY_H, put_exit, call_exit)
    qs = je.quarters(trades)
    out = []
    for i, q in enumerate(qs):
        if not q:
            out.append((i, 0, 0))
            continue
        r = je.replay_account(q, MO, CAP, cb_mode=CB_MODE)
        out.append((i, r["return_pct"], r["max_dd"]))
    return label, out


COMBOS = [
    ("72h baseline (unchanged exits)", None, None),
    ("72h: PUT hold->72 only", {"hold_h": 72}, None),
    ("72h: CALL tp2->0.65 only", None, {"tp2_pct": 0.65}),
    ("72h: PUT hold->72 + CALL tp2->0.65", {"hold_h": 72}, {"tp2_pct": 0.65}),
    ("72h: PUT hold->72 + sl->1.50 + CALL tp2->0.65", {"hold_h": 72, "sl_pct": 1.50}, {"tp2_pct": 0.65}),
]

if __name__ == "__main__":
    print(f"=== 72h combo validation (train/holdout) ===")
    with mp.Pool(min(len(COMBOS), mp.cpu_count())) as pool:
        results = pool.map(eval_combo, COMBOS)
    base = results[0]
    hdr = f"{'label':45s} {'train_ret':>10s} {'train_dd':>9s} {'holdout_ret':>12s} {'ho_dd':>7s}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        print(f"{r['label']:45s} {r['train_ret']:+9.1f}% {r['train_dd']:8.1f}% "
             f"{r['holdout_ret']:+11.1f}% {r['holdout_dd']:6.1f}%")

    # also print vs the live 168h weekly baseline for direct comparison
    trades168, r_tr168, r_ho168 = run(jc.TARGET_EXPIRY_H, jc.PUT_EXIT, jc.CALL_EXIT)
    print(f"\n{'168h LIVE baseline':45s} {r_tr168['return_pct']:+9.1f}% {r_tr168['max_dd']:8.1f}% "
         f"{r_ho168['return_pct']:+11.1f}% {r_ho168['max_dd']:6.1f}%")

    print("\n=== Quarter robustness: best combo vs 168h live baseline ===")
    best = results[-1]  # the fullest combo
    qs168 = je.quarters(trades168)
    base168_q = []
    for i, q in enumerate(qs168):
        if not q:
            base168_q.append((i, 0, 0))
            continue
        r = je.replay_account(q, MO, CAP, cb_mode=CB_MODE)
        base168_q.append((i, r["return_pct"], r["max_dd"]))

    label, qs = eval_combo_quarters(COMBOS[-1])
    print(f"  {label}  vs 168h live baseline:")
    for i, ret, dd in qs:
        b_ret, b_dd = base168_q[i][1], base168_q[i][2]
        flag = "better" if (ret >= b_ret and dd <= b_dd) else "worse"
        print(f"    Q{i+1}: ret={ret:+7.1f}% (168h base {b_ret:+7.1f}%)  dd={dd:5.1f}% (168h base {b_dd:5.1f}%)  [{flag}]")
