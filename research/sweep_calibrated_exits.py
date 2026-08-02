"""Exit-parameter retune under the calibrated pricing sigma (sweep_sigma_calibration.py).
Live TP2/SL (PUT 0.70/1.75/120h, CALL 0.70/0.75/24h) were tuned against the
old SIGMA_CLAMP pricing, which validation (research/validation.csv, 21 real
trades) showed does not match real Bybit option marks well. Retuning here
under sigma_calib -- same single-lever methodology as sweep_exits.py.

Run: python3 sweep_calibrated_exits.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je

MO, CAP, CB_MODE = 10, 6, "per_key"
CALIB = {"b0": 0.3487, "b1": 0.2646, "floor": 0.25, "ceiling": 1.05}

BASE_PUT_EXIT = dict(jc.PUT_EXIT)
BASE_CALL_EXIT = dict(jc.CALL_EXIT)


def days(trades):
    return (trades[-1]["entry_ts"] - trades[0]["entry_ts"]) / 86_400_000 if trades else 0.0


def eval_variant(label, put_ov, call_ov):
    put_exit = dict(BASE_PUT_EXIT, **(put_ov or {}))
    call_exit = dict(BASE_CALL_EXIT, **(call_ov or {}))
    trades = (je.coin_trades("ETH", put_exit=put_exit, call_exit=call_exit, sigma_calib=CALIB) +
             je.coin_trades("BTC", put_exit=put_exit, call_exit=call_exit, sigma_calib=CALIB))
    tr, ho = je.split(trades, 0.70)
    r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE)
    r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE)
    return {"label": label, "train_ret": r_tr["return_pct"], "train_dd": r_tr["max_dd"],
            "train_tpd": r_tr["n_taken"] / days(tr) if tr else 0.0,
            "holdout_ret": r_ho["return_pct"], "holdout_dd": r_ho["max_dd"],
            "holdout_tpd": r_ho["n_taken"] / days(ho) if ho else 0.0}


VARIANTS = [
    ("baseline (live exits, calibrated sigma)", None, None),
    ("CALL tp2 0.70->0.55", None, {"tp2_pct": 0.55}),
    ("CALL tp2 0.70->0.60", None, {"tp2_pct": 0.60}),
    ("CALL tp2 0.70->0.65", None, {"tp2_pct": 0.65}),
    ("CALL tp2 0.70->0.80", None, {"tp2_pct": 0.80}),
    ("CALL sl 0.75->0.50", None, {"sl_pct": 0.50}),
    ("CALL sl 0.75->0.60", None, {"sl_pct": 0.60}),
    ("CALL sl 0.75->0.90", None, {"sl_pct": 0.90}),
    ("CALL sl 0.75->1.00", None, {"sl_pct": 1.00}),
    ("CALL hold 24->12", None, {"hold_h": 12}),
    ("CALL hold 24->18", None, {"hold_h": 18}),
    ("CALL hold 24->36", None, {"hold_h": 36}),
    ("PUT tp2 0.70->0.55", {"tp2_pct": 0.55}, None),
    ("PUT tp2 0.70->0.60", {"tp2_pct": 0.60}, None),
    ("PUT tp2 0.70->0.65", {"tp2_pct": 0.65}, None),
    ("PUT tp2 0.70->0.80", {"tp2_pct": 0.80}, None),
    ("PUT sl 1.75->1.25", {"sl_pct": 1.25}, None),
    ("PUT sl 1.75->1.50", {"sl_pct": 1.50}, None),
    ("PUT sl 1.75->2.25", {"sl_pct": 2.25}, None),
    ("PUT hold 120->72", {"hold_h": 72}, None),
    ("PUT hold 120->96", {"hold_h": 96}, None),
    ("PUT hold 120->144", {"hold_h": 144}, None),
]


def run():
    hdr = f"{'variant':42s} {'tr_ret':>12s} {'tr_dd':>7s} {'tr_tpd':>7s} {'ho_ret':>10s} {'ho_dd':>6s} {'ho_tpd':>7s}"
    print(hdr)
    print("-" * len(hdr))
    for label, put_ov, call_ov in VARIANTS:
        r = eval_variant(label, put_ov, call_ov)
        print(f"{r['label']:42s} {r['train_ret']:+11.1f}% {r['train_dd']:6.1f}% {r['train_tpd']:7.2f} "
              f"{r['holdout_ret']:+9.1f}% {r['holdout_dd']:5.1f}% {r['holdout_tpd']:7.2f}")


if __name__ == "__main__":
    run()
