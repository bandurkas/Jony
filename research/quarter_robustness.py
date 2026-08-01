"""Quarter-by-quarter robustness check for entry-threshold sweep candidates
(see sweep_thresholds.py). Each quarter is replayed independently from a
fresh $800 (NOT compounded across quarters) so a consistently-positive edge
across 4 different market regimes can be distinguished from one lucky
train/holdout split hiding a concentrated, regime-specific result.

Run: python3 quarter_robustness.py
"""
from __future__ import annotations

import multiprocessing as mp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep_thresholds as sw
import jony_engine as je

CANDIDATES = [
    ("baseline (live)", None, None),
    ("A: PUT+transition", {"regime_filter": ("range", "transition")}, None),
    ("C: PUT+transition + CALL vol0.45", {"regime_filter": ("range", "transition")}, {"vol_threshold": 0.45}),
    ("E: PUT+transition + CALL vol0.45 + CALL+trend", {"regime_filter": ("range", "transition")},
    {"vol_threshold": 0.45, "regime_filter": ("range", "transition", "trend")}),
]


def eval_quarters(args):
    label, put_ov, call_ov = args
    put_gen, call_gen = sw.make_variant(put_ov, call_ov)
    trades = je.coin_trades("ETH", put_gen=put_gen, call_gen=call_gen) + \
             je.coin_trades("BTC", put_gen=put_gen, call_gen=call_gen)
    qs = je.quarters(trades)
    out = []
    for i, q in enumerate(qs):
        if not q:
            out.append((i, 0, 0, 0, 0))
            continue
        r = je.replay_account(q, sw.MO, sw.CAP, cb_mode=sw.CB_MODE)
        d = sw.days(q)
        wr = sum(1 for t in q if t["pnl_pct"] > 0) / len(q) * 100
        out.append((i, r["return_pct"], r["max_dd"], r["n_taken"] / d if d else 0, wr))
    return label, out


if __name__ == "__main__":
    with mp.Pool(len(CANDIDATES)) as pool:
        results = pool.map(eval_quarters, CANDIDATES)
    for label, qs in results:
        print(f"\n{label}")
        for i, ret, dd, tpd, wr in qs:
            print(f"  Q{i+1}: ret={ret:+8.1f}%  maxDD={dd:5.1f}%  trades/day={tpd:.2f}  WR={wr:.1f}%")
