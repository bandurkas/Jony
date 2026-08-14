"""Does an IMPLEMENTABLE same-key concurrency throttle recover what v1's
clairvoyant circuit breaker was doing?

Context: replay_account_v2.py showed v1's headline numbers come almost entirely
from a CB armed at a trade's ENTRY using that trade's FINAL pnl — it stops
stacking a (coin,side) key exactly when the next trade there would lose. Not
implementable. But its *effect* was throttling same-key stacking, and that IS
implementable with present-only information: cap concurrent positions per
(coin,side).

This is also the live account's current shape: 6 concurrent ETH PUTs on the
same expiry (5 on one strike) as of 2026-08-08 — one bet, not six.

per_key_cap=None reproduces live behavior (no throttle beyond mo/cap).

Run: python3 sweep_per_key_cap_v2.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je
from replay_account_v2 import replay_v2

MO, CAP = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP
KEY_CAPS = [None, 1, 2, 3, 4, 6]


def main() -> None:
    trades = je.coin_trades("ETH") + je.coin_trades("BTC")
    tr, ho = je.split(trades, 0.70)
    qs = [q for q in je.quarters(trades) if q]

    print(f"live MO={MO} CAP={CAP}, event-ordered replay (credit@exit, cb@exit)\n")
    print(f"{'per_key':>8s} {'train ret%':>11s} {'trDD%':>7s} {'hold ret%':>11s} "
          f"{'hoDD%':>7s} {'negQ':>6s} {'worstQ%':>9s} {'taken':>6s} {'avgConc':>8s}")
    for kc in KEY_CAPS:
        r_tr = replay_v2(tr, MO, CAP, per_key_cap=kc)
        r_ho = replay_v2(ho, MO, CAP, per_key_cap=kc)
        qr = [replay_v2(q, MO, CAP, per_key_cap=kc)["return_pct"] for q in qs]
        neg = sum(1 for x in qr if x < 0)
        label = "live(off)" if kc is None else str(kc)
        print(f"{label:>8s} {r_tr['return_pct']:>11.1f} {r_tr['max_dd']:>7.1f} "
              f"{r_ho['return_pct']:>11.1f} {r_ho['max_dd']:>7.1f} "
              f"{neg:>3d}/{len(qr):<2d} {min(qr):>9.1f} "
              f"{r_tr['n_taken']:>6d} {r_tr['avg_concurrent']:>8.2f}")

    print("\nper-quarter detail (return% / maxDD%):")
    hdr = "  ".join(f"{('live' if k is None else f'key<={k}'):>16s}" for k in KEY_CAPS)
    print(f"{'quarter':9s} {hdr}")
    for i, q in enumerate(qs):
        cells = []
        for kc in KEY_CAPS:
            r = replay_v2(q, MO, CAP, per_key_cap=kc)
            cells.append(f"{r['return_pct']:+8.1f}/{r['max_dd']:5.1f}")
        print(f"  Q{i+1:<6d} " + "  ".join(f"{c:>16s}" for c in cells))


if __name__ == "__main__":
    main()
