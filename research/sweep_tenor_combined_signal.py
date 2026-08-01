"""'Open both tenors' idea (user, 2026-08-02): instead of picking one tenor
per side, fire TWO trades on every signal — one priced/managed at live
168h, one at the retuned 72h combo (validate_tenor_combo.py's winner) —
doubling exposure per signal instead of replacing it.

RESULT: worse than every other tested variant. Portfolio-level effects
dominate — two correlated positions fired off the same signal don't
diversify (same underlying, same direction), they just double down.
MAX_OPEN_POSITIONS/PER_COIN_CAP absorb most of the extra volume as skipped
entries anyway (8396/14810 train, 3486/14810 holdout), and what does get
through raises drawdown in every quarter (16.4/11.7/13.6/7.0% vs 168h-only
baseline's 10.4/10.2/10.1/6.9%) while holdout return is LOWER than even
the plain 168h baseline (+1917.2% vs +2440.5%). Confirms the same lesson
as the CALL@72h/PUT@168h compromise (also tested, also worse than either
pure strategy, see chat/handoff) — partially or additively mixing tenors
within the same portfolio is dominated, not a middle ground. Line closed.

Run: python3 sweep_tenor_combined_signal.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je
from sweep_tenor import scale_exit

MO, CAP, CB_MODE = 6, 4, "per_key"

PUT_EXIT_72_FULL = dict(scale_exit(jc.PUT_EXIT, 72.0), hold_h=72.0)
CALL_EXIT_72 = dict(scale_exit(jc.CALL_EXIT, 72.0), tp2_pct=0.65)


def run():
    trades_168 = je.coin_trades("ETH", expiry_h=168.0, put_exit=jc.PUT_EXIT, call_exit=jc.CALL_EXIT) + \
                je.coin_trades("BTC", expiry_h=168.0, put_exit=jc.PUT_EXIT, call_exit=jc.CALL_EXIT)
    trades_72 = je.coin_trades("ETH", expiry_h=72.0, put_exit=PUT_EXIT_72_FULL, call_exit=CALL_EXIT_72) + \
               je.coin_trades("BTC", expiry_h=72.0, put_exit=PUT_EXIT_72_FULL, call_exit=CALL_EXIT_72)
    both = trades_168 + trades_72
    tr, ho = je.split(both, 0.70)
    r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE)
    r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE)
    print(f"raw trades generated: {len(both)} (168h: {len(trades_168)}, 72h: {len(trades_72)})")
    print(f"train:   ret={r_tr['return_pct']:+.1f}% dd={r_tr['max_dd']:.1f}% taken={r_tr['n_taken']} skipped_cap={r_tr.get('n_skipped_cap')}")
    print(f"holdout: ret={r_ho['return_pct']:+.1f}% dd={r_ho['max_dd']:.1f}% taken={r_ho['n_taken']} skipped_cap={r_ho.get('n_skipped_cap')}")

    base_qs = je.quarters(trades_168)
    qs = je.quarters(both)
    print("\nQuarter robustness vs 168h-only live baseline:")
    for i, q in enumerate(qs):
        r = je.replay_account(q, MO, CAP, cb_mode=CB_MODE) if q else {"return_pct": 0, "max_dd": 0}
        rb = je.replay_account(base_qs[i], MO, CAP, cb_mode=CB_MODE) if base_qs[i] else {"return_pct": 0, "max_dd": 0}
        flag = "better" if (r["return_pct"] >= rb["return_pct"] and r["max_dd"] <= rb["max_dd"]) else "worse"
        print(f"  Q{i+1}: ret={r['return_pct']:+7.1f}% (168h-only {rb['return_pct']:+7.1f}%)  "
             f"dd={r['max_dd']:5.1f}% (168h-only {rb['max_dd']:5.1f}%)  [{flag}]")


if __name__ == "__main__":
    run()
