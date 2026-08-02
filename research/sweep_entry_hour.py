"""Entry-hour veto sweep — portfolio+quarter follow-up to the raw per-trade
UTC-hour check from the Tyagach-transfer survey (2026-08-02,
SESSION_HANDOFF_2026-08-02.md §19 pt.2). That check found best hours
13-16h UTC (avg_pnl +0.03-0.045, WR 58-60%) and worst hours 20-23h UTC
(-0.0374 at 20h) + 11-12h UTC on RAW per-trade numbers only -- explicitly
flagged as not yet validated at the portfolio level (removing/keeping trades
changes which OTHER trades get admitted past MAX_OPEN_POSITIONS/PER_COIN_CAP/
PORT_MARGIN_CAP, the same lesson the Tyagach session learned the hard way
about position caps, see sweep_position_caps.py's own history).

Mechanism: coin_trades() has no entry-hour hook, so this filters the
already-generated trade list by UTC hour of entry_ts BEFORE replay_account
-- purely a post-hoc admission filter, same technique the tenor/vol-guard
sweeps used for their scoped variants. No live code touched.

Run: python3 sweep_entry_hour.py

RESULT, REJECTED (2026-08-02): the raw per-trade effect does NOT survive
portfolio+quarter testing. Vetoing 20-23h UTC drops aggregate train return
449695%->249480% (-44.5%) and holdout 6091.9%->4541.3% (-25.5%) with NO
drawdown improvement -- train dd actually gets WORSE (13.0%->16.2%). Adding
11-12h UTC to the veto makes it strictly worse again on every metric. By
quarter: veto loses on return in 3/4 quarters and never meaningfully helps
dd anywhere (Q1 is the only quarter where veto wins on return, and even
there dd is flat). Mechanism: the raw per-trade "bad hour" numbers just
correlate with fewer good trades sharing the same window -- removing them
cuts total trade count (~16-24%) and therefore compounding, a pure
mechanical loss, not a risk reduction. Same lesson as Tyagach's position-cap
history: per-trade signal != portfolio effect. Do NOT veto entry hours for
Jony without new data. Live gate logic (vol/regime/MTF/bull-filter) is left
untouched.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_engine as je

MO, CAP = 10, 6  # live as of 2026-08-02
CB_MODE = "per_key"

WORST_HOURS = {20, 21, 22, 23}
WORST_HOURS_EXT = {11, 12, 20, 21, 22, 23}  # + the two extra bad hours from the raw check

VARIANTS = {
    "baseline (live, no hour filter)": None,
    "veto 20-23h UTC": WORST_HOURS,
    "veto 11-12h + 20-23h UTC": WORST_HOURS_EXT,
}


def hour_utc(entry_ts_ms: int) -> int:
    return datetime.fromtimestamp(entry_ts_ms / 1000, tz=timezone.utc).hour


def filter_hours(trades: list[dict], veto: set[int] | None) -> list[dict]:
    if veto is None:
        return trades
    return [t for t in trades if hour_utc(t["entry_ts"]) not in veto]


def run():
    trades = je.coin_trades("ETH") + je.coin_trades("BTC")
    tr, ho = je.split(trades, 0.70)
    qs = je.quarters(trades)

    print("=== Aggregate train/holdout across entry-hour veto variants (MO=10/CAP=6, live) ===")
    hdr = f"{'variant':32s} {'n_tr':>6s} {'train_ret':>11s} {'train_dd':>9s} {'n_ho':>6s} {'holdout_ret':>12s} {'ho_dd':>7s}"
    print(hdr)
    print("-" * len(hdr))
    for name, veto in VARIANTS.items():
        tr_f = filter_hours(tr, veto)
        ho_f = filter_hours(ho, veto)
        r_tr = je.replay_account(tr_f, MO, CAP, cb_mode=CB_MODE)
        r_ho = je.replay_account(ho_f, MO, CAP, cb_mode=CB_MODE)
        print(f"{name:32s} {len(tr_f):6d} {r_tr['return_pct']:+10.1f}% {r_tr['max_dd']:8.1f}% "
              f"{len(ho_f):6d} {r_ho['return_pct']:+11.1f}% {r_ho['max_dd']:6.1f}%")

    print("\n=== Quarter robustness (fresh capital per quarter) ===")
    for i, q in enumerate(qs):
        if not q:
            continue
        print(f"  Q{i+1} ({len(q)} raw trades):")
        for name, veto in VARIANTS.items():
            q_f = filter_hours(q, veto)
            r = je.replay_account(q_f, MO, CAP, cb_mode=CB_MODE)
            print(f"    {name:32s} n={len(q_f):5d} ret={r['return_pct']:+9.1f}% dd={r['max_dd']:5.1f}%")


if __name__ == "__main__":
    run()
