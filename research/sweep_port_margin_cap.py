"""PORT_MARGIN_CAP sweep — follow-up to sweep_position_caps.py, which found
that at the live 6/4 count caps, position COUNT was the binding constraint,
not margin; raised to MO=10/CAP=6 (now live) specifically because that's the
elbow where "margin becomes the binding constraint beyond that point, not
position count" (sweep_position_caps.py's own conclusion). That script never
tested whether PORT_MARGIN_CAP=0.80 ITSELF has further headroom now that
it's the actual binding constraint -- this does.

Ported from Tyagach's same-day finding (2026-08-02,
~/Desktop/Tyagach/src/u_cap_headroom_13cell.py): there, the equivalent
margin ceiling (MAX_TOTAL_MARGIN_PCT) had real headroom (0.60->0.80 was a
clean win) but pushing further (->0.99) stopped helping and the worst
quarter got WORSE -- not "more is strictly better." Checking whether the
same pattern holds for Jony's PORT_MARGIN_CAP.

Mechanism: PORT_MARGIN_CAP is read inside jony_core.size_position()
(research/jony_core.py:305, `free = equity*PORT_MARGIN_CAP - used_margin`)
as a plain module global -- monkey-patching `jc.PORT_MARGIN_CAP` before
calling coin_trades()/replay_account() is sufficient, no code path needs
touching for this sweep.

Run: python3 sweep_port_margin_cap.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_engine as je
import jony_core as jc

CB_MODE = "per_key"
MO, CAP = 10, 6  # live as of 2026-08-02 (sweep_position_caps.py)
MARGIN_LEVELS = [0.80, 0.85, 0.90, 0.95, 0.99]
LIVE_MARGIN = 0.80


def run():
    trades = je.coin_trades("ETH") + je.coin_trades("BTC")  # pure live config otherwise
    tr, ho = je.split(trades, 0.70)

    print("=== Aggregate train/holdout across PORT_MARGIN_CAP levels (MO=10/CAP=6 fixed) ===")
    hdr = f"{'margin_cap':>10s} {'train_ret':>10s} {'train_dd':>9s} {'holdout_ret':>12s} {'ho_dd':>7s}"
    print(hdr)
    print("-" * len(hdr))
    orig = jc.PORT_MARGIN_CAP
    try:
        for cap_level in MARGIN_LEVELS:
            jc.PORT_MARGIN_CAP = cap_level
            r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE)
            r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE)
            print(f"{cap_level:10.2f} {r_tr['return_pct']:+9.1f}% {r_tr['max_dd']:8.1f}% "
                  f"{r_ho['return_pct']:+11.1f}% {r_ho['max_dd']:6.1f}%")

        print("\n=== Quarter robustness: each margin level vs live 0.80 ===")
        qs = je.quarters(trades)
        for i, q in enumerate(qs):
            if not q:
                continue
            jc.PORT_MARGIN_CAP = LIVE_MARGIN
            r_live = je.replay_account(q, MO, CAP, cb_mode=CB_MODE)
            row = f"  Q{i+1}: live(0.80) ret={r_live['return_pct']:+8.1f}% dd={r_live['max_dd']:5.1f}%  ->  "
            for cap_level in MARGIN_LEVELS:
                if cap_level == LIVE_MARGIN:
                    continue
                jc.PORT_MARGIN_CAP = cap_level
                r_new = je.replay_account(q, MO, CAP, cb_mode=CB_MODE)
                row += f"{cap_level:.2f}: ret={r_new['return_pct']:+7.1f}%/dd={r_new['max_dd']:4.1f}%  "
            print(row)
    finally:
        jc.PORT_MARGIN_CAP = orig


if __name__ == "__main__":
    run()
