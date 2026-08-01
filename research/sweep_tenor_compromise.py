"""'CALL only at 72h, PUT stays 168h' compromise (user, 2026-08-02): since
CALL showed the clearest single-lever win in sweep_tenor_exits.py
(tp2_pct 0.70->0.65), test moving only CALL to the 72h tenor while PUT
keeps its live 168h setup untouched, instead of moving both sides.

RESULT: dominated, not a middle ground. Holdout return (+2224.0%) is
LOWER than both the plain 168h baseline (+2440.5%) and the full 72h combo
(+2730.7%, see validate_tenor_combo.py) — and drawdown is WORSE than the
168h baseline in all 4 quarters (12.8/12.2/12.9/8.6% vs 10.4/10.2/10.1/
6.9%), not just some. Partially mixing tenors across sides doesn't
isolate the CALL-side edge cleanly; it seems to interact badly with
shared portfolio-level constraints (margin caps, per-coin/circuit-breaker
limits) that the coin_trades() account replay applies jointly across
sides. Confirms the same lesson as sweep_tenor_combined_signal.py (open
both tenors simultaneously — also dominated). Line closed; the only
variant with genuine (if risk-tradeoff) upside remains the FULL 72h
combo from validate_tenor_combo.py.

Run: python3 sweep_tenor_compromise.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je
from sweep_tenor import scale_exit

MO, CAP, CB_MODE = 6, 4, "per_key"
CALL_EXIT_72 = dict(scale_exit(jc.CALL_EXIT, 72.0), tp2_pct=0.65)


def run():
    put_trades = je.coin_trades("ETH", sides_enabled=("P",), expiry_h=168.0, put_exit=jc.PUT_EXIT)
    call_trades = je.coin_trades("ETH", sides_enabled=("C",), expiry_h=72.0, call_exit=CALL_EXIT_72) + \
                  je.coin_trades("BTC", sides_enabled=("C",), expiry_h=72.0, call_exit=CALL_EXIT_72)
    trades = put_trades + call_trades
    tr, ho = je.split(trades, 0.70)
    r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE)
    r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE)
    print(f"train:   ret={r_tr['return_pct']:+.1f}% dd={r_tr['max_dd']:.1f}%")
    print(f"holdout: ret={r_ho['return_pct']:+.1f}% dd={r_ho['max_dd']:.1f}%")

    base_trades = je.coin_trades("ETH") + je.coin_trades("BTC")
    base_qs = je.quarters(base_trades)
    qs = je.quarters(trades)
    print("\nQuarter robustness vs 168h-only live baseline:")
    for i, q in enumerate(qs):
        r = je.replay_account(q, MO, CAP, cb_mode=CB_MODE) if q else {"return_pct": 0, "max_dd": 0}
        rb = je.replay_account(base_qs[i], MO, CAP, cb_mode=CB_MODE) if base_qs[i] else {"return_pct": 0, "max_dd": 0}
        flag = "better" if (r["return_pct"] >= rb["return_pct"] and r["max_dd"] <= rb["max_dd"]) else "worse"
        print(f"  Q{i+1}: ret={r['return_pct']:+7.1f}% (168h-only {rb['return_pct']:+7.1f}%)  "
             f"dd={r['max_dd']:5.1f}% (168h-only {rb['max_dd']:5.1f}%)  [{flag}]")


if __name__ == "__main__":
    run()
