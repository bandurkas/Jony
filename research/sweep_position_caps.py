"""MAX_OPEN_POSITIONS/PER_COIN_CAP sweep — discovered while testing the
'open both tenors simultaneously' idea (sweep_tenor_combined_signal.py):
raising the caps there recovered a lot of the trades otherwise dropped as
n_skipped_cap. Isolating the effect: does raising caps help even WITHOUT
touching tenor at all (live 168h config unchanged otherwise)? Answer: yes,
strongly, and it has nothing to do with tenor — this is a standalone lever.

Mechanism (verified in jony_core.size_position): margin sizing is already
capped independently via PORT_MARGIN_CAP=0.80 (jony_core.py:305,
`free = equity*0.80 - used_margin`) — MAX_OPEN_POSITIONS/PER_COIN_CAP are
an ADDITIONAL, stricter position-COUNT ceiling on top of that margin
budget. At the live 6/4 caps, that count ceiling binds before margin does,
throttling profitable trades the margin budget would have allowed anyway.
Raising the count ceiling doesn't bypass real risk control — margin sizing
is untouched — it just stops double-capping below what margin already
allows.

Elbow point: ~MO=10/CAP=6 — returns keep climbing up to there, then
plateau (MO=12+ barely moves the holdout number), confirming margin
becomes the binding constraint beyond that point, not position count.

Quarter robustness at MO=10/CAP=6 vs live MO=6/CAP=4 (2026-08-02): Q1 and
Q4 improve on BOTH return and drawdown; Q3 is flat on drawdown with much
higher return; Q2 is the one real cost — drawdown rises from 10.2% to
13.0% alongside ~2.7x return. Tested a gentler MO=8/CAP=5 too: Q2's
drawdown bump doesn't shrink (13.2%, actually marginally worse) despite
the smaller cap increase, so this isn't something you can dial down by
choosing a smaller bump — it looks like a structural feature of opening
more concurrent positions at all, not a magnitude-scaled cost. MO=10/CAP=6
captures ~all of the plateaued upside for the same Q2 cost as a smaller
bump, so it dominates MO=8/CAP=5.

NOT YET: real-world execution capacity at 10 concurrent option positions
(the backtest's fixed HALF_SPREAD doesn't model slippage/liquidity
degradation from holding more concurrent Bybit option positions than
tested at 6) and a closer look at what specifically drives Q2's drawdown
increase (which trades, which coin/side) — before considering deploy, this
needs the full architecture->code->review->tests->paper-deploy process,
same as every other shipped change in this repo. Nothing here touches live
code.

Run: python3 sweep_position_caps.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_engine as je

CB_MODE = "per_key"
CAP_LEVELS = [(6, 4), (8, 5), (10, 6), (12, 8), (14, 9), (16, 10), (20, 11)]
LIVE = (6, 4)


def run():
    trades = je.coin_trades("ETH") + je.coin_trades("BTC")  # pure live config otherwise
    tr, ho = je.split(trades, 0.70)

    print("=== Aggregate train/holdout across cap levels ===")
    hdr = f"{'MO':>3s} {'CAP':>3s} {'train_ret':>10s} {'train_dd':>9s} {'holdout_ret':>12s} {'ho_dd':>7s}"
    print(hdr)
    print("-" * len(hdr))
    for mo, cap in CAP_LEVELS:
        r_tr = je.replay_account(tr, mo, cap, cb_mode=CB_MODE)
        r_ho = je.replay_account(ho, mo, cap, cb_mode=CB_MODE)
        print(f"{mo:3d} {cap:3d} {r_tr['return_pct']:+9.1f}% {r_tr['max_dd']:8.1f}% "
             f"{r_ho['return_pct']:+11.1f}% {r_ho['max_dd']:6.1f}%")

    print("\n=== Quarter robustness: MO=10/CAP=6 vs live MO=6/CAP=4 ===")
    qs = je.quarters(trades)
    for i, q in enumerate(qs):
        if not q:
            continue
        r_live = je.replay_account(q, *LIVE, cb_mode=CB_MODE)
        r_new = je.replay_account(q, 10, 6, cb_mode=CB_MODE)
        flag = "better" if (r_new["return_pct"] >= r_live["return_pct"] and r_new["max_dd"] <= r_live["max_dd"]) else "mixed"
        print(f"  Q{i+1}: live(6/4) ret={r_live['return_pct']:+8.1f}% dd={r_live['max_dd']:5.1f}%  ->  "
             f"10/6 ret={r_new['return_pct']:+8.1f}% dd={r_new['max_dd']:5.1f}%  [{flag}]")


if __name__ == "__main__":
    run()
