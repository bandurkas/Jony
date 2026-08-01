"""Exit-parameter retune for the 72h tenor candidate (research/sweep_tenor.py
round 1, 2026-08-02): the tenor sweep found 24/48/72h all lose to the 168h
baseline on holdout when TP2/SL% are just copied unchanged from weekly —
72h was the least-bad (better holdout maxDD than baseline, 7.7% vs 8.6%,
worse holdout return). Hypothesis: fixed %-of-premium TP2/SL widths tuned
for weekly's slow decay don't fit a much-faster-decaying 72h option; this
sweep retunes them for 72h specifically, same single-lever methodology as
sweep_exits.py's original round-3 exit retune.

expiry_h is fixed at 72h throughout (jony_engine.coin_trades(expiry_h=72)).
Base exit config is the round-1 hold_h-scaled 72h config
(PUT hold=51.4h, CALL hold=10.3h — see sweep_tenor.scale_exit), NOT the
live 168h PUT_EXIT/CALL_EXIT.

Run: python3 sweep_tenor_exits.py
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

BASE_PUT_EXIT = scale_exit(jc.PUT_EXIT, EXPIRY_H)    # tp2=0.70 sl=1.75 hold=51.4h
BASE_CALL_EXIT = scale_exit(jc.CALL_EXIT, EXPIRY_H)  # tp2=0.70 sl=0.75 hold=10.3h


def days(trades: list[dict]) -> float:
    return (trades[-1]["entry_ts"] - trades[0]["entry_ts"]) / 86_400_000 if trades else 0.0


def eval_variant(args) -> dict:
    label, put_ov, call_ov = args
    put_exit = dict(BASE_PUT_EXIT, **(put_ov or {}))
    call_exit = dict(BASE_CALL_EXIT, **(call_ov or {}))
    trades = je.coin_trades("ETH", expiry_h=EXPIRY_H, put_exit=put_exit, call_exit=call_exit) + \
             je.coin_trades("BTC", expiry_h=EXPIRY_H, put_exit=put_exit, call_exit=call_exit)
    tr, ho = je.split(trades, 0.70)
    r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE)
    r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE)
    return {
        "label": label, "put_ov": put_ov, "call_ov": call_ov,
        "train_ret": r_tr["return_pct"], "train_dd": r_tr["max_dd"],
        "train_tpd": r_tr["n_taken"] / days(tr) if tr else 0.0,
        "holdout_ret": r_ho["return_pct"], "holdout_dd": r_ho["max_dd"],
        "holdout_tpd": r_ho["n_taken"] / days(ho) if ho else 0.0,
    }


VARIANTS = [
    ("baseline (72h scaled)", None, None),
    ("CALL tp2 0.70->0.60", None, {"tp2_pct": 0.60}),
    ("CALL tp2 0.70->0.65", None, {"tp2_pct": 0.65}),
    ("CALL tp2 0.70->0.75", None, {"tp2_pct": 0.75}),
    ("CALL tp2 0.70->0.80", None, {"tp2_pct": 0.80}),
    ("CALL sl 0.75->0.50", None, {"sl_pct": 0.50}),
    ("CALL sl 0.75->0.60", None, {"sl_pct": 0.60}),
    ("CALL sl 0.75->0.90", None, {"sl_pct": 0.90}),
    ("CALL sl 0.75->1.00", None, {"sl_pct": 1.00}),
    ("CALL hold 10.3->6", None, {"hold_h": 6}),
    ("CALL hold 10.3->8", None, {"hold_h": 8}),
    ("CALL hold 10.3->14", None, {"hold_h": 14}),
    ("CALL hold 10.3->18", None, {"hold_h": 18}),
    ("PUT tp2 0.70->0.60", {"tp2_pct": 0.60}, None),
    ("PUT tp2 0.70->0.65", {"tp2_pct": 0.65}, None),
    ("PUT tp2 0.70->0.75", {"tp2_pct": 0.75}, None),
    ("PUT tp2 0.70->0.80", {"tp2_pct": 0.80}, None),
    ("PUT sl 1.75->1.25", {"sl_pct": 1.25}, None),
    ("PUT sl 1.75->1.50", {"sl_pct": 1.50}, None),
    ("PUT sl 1.75->2.00", {"sl_pct": 2.00}, None),
    ("PUT sl 1.75->2.25", {"sl_pct": 2.25}, None),
    ("PUT hold 51.4->30", {"hold_h": 30}, None),
    ("PUT hold 51.4->40", {"hold_h": 40}, None),
    ("PUT hold 51.4->60", {"hold_h": 60}, None),
    ("PUT hold 51.4->72", {"hold_h": 72}, None),
]


def print_table(results: list[dict]) -> None:
    base = next(r for r in results if r["label"].startswith("baseline"))
    hdr = f"{'label':24s} {'train_ret':>10s} {'train_dd':>9s} {'tr_tpd':>7s} {'holdout_ret':>12s} {'ho_dd':>7s} {'ho_tpd':>7s}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        flag = ""
        if r["label"] != base["label"]:
            improves = r["train_ret"] > base["train_ret"] and r["holdout_ret"] > base["holdout_ret"]
            dd_ok = r["holdout_dd"] <= base["holdout_dd"] * 1.15
            flag = " <-- candidate" if (improves and dd_ok) else ""
        print(f"{r['label']:24s} {r['train_ret']:+9.1f}% {r['train_dd']:8.1f}% {r['train_tpd']:6.2f} "
             f"{r['holdout_ret']:+11.1f}% {r['holdout_dd']:6.1f}% {r['holdout_tpd']:6.2f}{flag}")


if __name__ == "__main__":
    n_workers = min(len(VARIANTS), mp.cpu_count())
    print(f"Running {len(VARIANTS)} exit variants at expiry_h=72 on {n_workers} workers (mo={MO} cap={CAP})...")
    with mp.Pool(n_workers) as pool:
        results = pool.map(eval_variant, VARIANTS)
    print_table(results)
