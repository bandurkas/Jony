"""Final validation of the combined candidate found this session:
  - entry gates: config E (jc.PUT_GEN/CALL_GEN, live)
  - exits: CALL tp2_pct 0.80->0.70, PUT sl_pct 2.00->1.75, PUT hold_h 96->120
  - CALL trend-regime size_mult (0.5 vs 1.0=no change) on top

Run: python3 validate_combined.py
"""
from __future__ import annotations

import multiprocessing as mp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je
import sweep_regime_sizing as srs

MO, CAP, CB_MODE = 6, 4, "per_key"
CALL_EXIT = dict(jc.CALL_EXIT, tp2_pct=0.70)
PUT_EXIT = dict(jc.PUT_EXIT, sl_pct=1.75, hold_h=120)


def full_combo(mult: float):
    trades = je.coin_trades("ETH", put_exit=PUT_EXIT, call_exit=CALL_EXIT) + \
             je.coin_trades("BTC", put_exit=PUT_EXIT, call_exit=CALL_EXIT)
    fn = srs.make_size_mult_fn(mult)
    tr, ho = je.split(trades, 0.70)
    r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE, size_mult_fn=fn)
    r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE, size_mult_fn=fn)
    qs = je.quarters(trades)
    qout = []
    for i, q in enumerate(qs):
        if not q:
            qout.append((i, 0, 0))
            continue
        r = je.replay_account(q, MO, CAP, cb_mode=CB_MODE, size_mult_fn=fn)
        qout.append((i, r["return_pct"], r["max_dd"]))
    return mult, r_tr, r_ho, qout, len(tr), len(ho)


if __name__ == "__main__":
    with mp.Pool(2) as pool:
        results = pool.map(full_combo, [1.0, 0.5])
    for mult, r_tr, r_ho, qout, ntr, nho in results:
        print(f"\n=== FULL COMBO (new exits + CALL trend size_mult={mult}) ===")
        print(f"train:   ret={r_tr['return_pct']:+8.1f}% maxDD={r_tr['max_dd']:5.1f}% n={ntr}")
        print(f"holdout: ret={r_ho['return_pct']:+8.1f}% maxDD={r_ho['max_dd']:5.1f}% n={nho}")
        for i, ret, dd in qout:
            print(f"  Q{i+1}: ret={ret:+8.1f}%  maxDD={dd:5.1f}%")
