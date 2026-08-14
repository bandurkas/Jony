"""Re-check the SHIPPED position-cap decision (MAX_OPEN 6->10, PER_COIN 4->6,
commit aa8315e, 2026-08-02) under the event-ordered replay.

That decision was validated with je.replay_account(), whose circuit breaker
arms from a trade's FINAL pnl at the moment the trade OPENS (jony_engine.py:582)
— i.e. it stops taking entries on a key for the whole life of a trade that is
going to lose, using information not available at that time. Position caps are
exactly the kind of portfolio-level lever that harness cannot judge honestly:
its clairvoyant CB already suppresses ~half of all candidate entries, so raising
a concurrency ceiling looks nearly free.

Run: python3 recheck_caps_v2.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je
from replay_account_v2 import replay_v2

GRID = [(6, 4), (8, 5), (10, 6), (12, 8), (16, 10)]


def main() -> None:
    trades = je.coin_trades("ETH") + je.coin_trades("BTC")
    tr, ho = je.split(trades, 0.70)
    qs = je.quarters(trades)

    for label, ca, cb in (("A  v1 harness (clairvoyant CB)", "entry", "entry"),
                          ("D  live-faithful", "exit", "exit")):
        print(f"\n=== {label} ===")
        print(f"{'MO/CAP':>8s} {'train ret%':>12s} {'trDD%':>7s} "
              f"{'hold ret%':>12s} {'hoDD%':>7s} {'neg quarters':>13s} {'worst Q%':>10s}")
        for mo, cap in GRID:
            r_tr = replay_v2(tr, mo, cap, credit_at=ca, cb_at=cb)
            r_ho = replay_v2(ho, mo, cap, credit_at=ca, cb_at=cb)
            qr = [replay_v2(q, mo, cap, credit_at=ca, cb_at=cb)["return_pct"]
                  for q in qs if q]
            neg = sum(1 for x in qr if x < 0)
            mark = "  <- LIVE" if (mo, cap) == (jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP) else ""
            print(f"{mo:>4d}/{cap:<3d} {r_tr['return_pct']:>12.1f} {r_tr['max_dd']:>7.1f} "
                  f"{r_ho['return_pct']:>12.1f} {r_ho['max_dd']:>7.1f} "
                  f"{neg:>8d}/{len(qr):<4d} {min(qr):>10.1f}{mark}")


if __name__ == "__main__":
    main()
