"""Per-key honest edge decomposition (2026-08-14).

Live history (50 closed, Jul 7 - Aug 14) shows the whole profit sits in one
key: ETH:P +$95.65 (WR 88%), while ETH:C -$41.09 (WR 18%) and BTC:C -$6.89.
Question 1 of the improvement plan: does the HONEST engine (replay_v2 /
fixed-lot expectancy) agree that keys differ this much — i.e. is disabling
ETH:C (and maybe BTC:C) a real improvement, or is live just a small sample?

Everything research-only. Uses v2 event-ordered replay (the only trusted
harness after the 2026-08-08 lookahead finding) + raw fixed-1-lot expectancy
(no portfolio effects at all). Both sigma variants: live SIGMA_CLAMP and the
markIv-fitted CALIB (reality runs closer to the latter, premiums lower).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je
from replay_account_v2 import replay_v2

CALIB = {"b0": 0.3487, "b1": 0.2646, "floor": 0.25, "ceiling": 1.05}
MO, CAP = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP


def fixed_lot_pnl(t: dict) -> float:
    qty = t["lot"]
    notional = t["strike"] * qty
    premium_total = t["entry_credit"] * qty
    fee_open = jc.fee_usd(notional, premium_total)
    exit_credit = t["entry_credit"] * (1 - t["pnl_pct"])
    fee_close = jc.fee_usd(notional, exit_credit * qty)
    return (t["entry_credit"] - exit_credit) * qty - fee_open - fee_close


def describe(trades: list[dict], label: str) -> None:
    if not trades:
        print(f"  {label:26s} n=0")
        return
    n = len(trades)
    wr = sum(1 for t in trades if t["pnl_pct"] > 0) / n
    mean_pct = sum(t["pnl_pct"] for t in trades) / n * 100
    dollars = sum(fixed_lot_pnl(t) for t in trades)
    res = {}
    for t in trades:
        res[t["resolution"]] = res.get(t["resolution"], 0) + 1
    res_s = " ".join(f"{k}:{v}" for k, v in sorted(res.items()))
    print(f"  {label:26s} n={n:5d} WR={wr:6.1%} mean={mean_pct:+7.3f}%  "
          f"fixed$={dollars:+10.2f}  [{res_s}]")


def main() -> None:
    for sig_label, sigma_calib in (("SIGMA_CLAMP (live pricing)", None),
                                   ("CALIB markIv-fitted", CALIB)):
        print(f"\n{'=' * 78}\n### sigma = {sig_label}\n{'=' * 78}")
        trades = (je.coin_trades("ETH", sigma_calib=sigma_calib)
                  + je.coin_trades("BTC", sigma_calib=sigma_calib))
        tr, ho = je.split(trades, 0.70)

        for split_label, sub in (("TRAIN", tr), ("HOLDOUT", ho)):
            print(f"\n--- {split_label} (n={len(sub)}) ---")
            describe(sub, "ALL")
            for key in ("ETH:P", "ETH:C", "BTC:C"):
                coin, side = key.split(":")
                describe([t for t in sub if t["coin"] == coin and t["side"] == side], key)

        print("\n--- quarters, per key (fixed-lot $ / n) ---")
        qs = je.quarters(trades)
        keys = ("ETH:P", "ETH:C", "BTC:C")
        print(f"  {'quarter':8s} " + " ".join(f"{k:>20s}" for k in keys))
        for i, q in enumerate(qs):
            cells = []
            for key in keys:
                coin, side = key.split(":")
                sub = [t for t in q if t["coin"] == coin and t["side"] == side]
                d = sum(fixed_lot_pnl(t) for t in sub)
                cells.append(f"{d:+9.2f} /{len(sub):4d}")
            print(f"  Q{i + 1:<7d} " + " ".join(f"{c:>20s}" for c in cells))

        print("\n--- replay_v2 (event-ordered, live config MO=10 CAP=6), key subsets ---")
        subsets = [
            ("all keys (live)", ("ETH:P", "ETH:C", "BTC:C")),
            ("ETH:P only", ("ETH:P",)),
            ("ETH:P + BTC:C", ("ETH:P", "BTC:C")),
            ("no ETH:C", ("ETH:P", "BTC:C")),
            ("CALLs only", ("ETH:C", "BTC:C")),
        ]
        seen = set()
        for name, keys_on in subsets:
            if keys_on in seen:
                continue
            seen.add(keys_on)
            # split boundary from the FULL timeline so it is identical across subsets
            str_ = [t for t in tr if f"{t['coin']}:{t['side']}" in keys_on]
            sho = [t for t in ho if f"{t['coin']}:{t['side']}" in keys_on]
            rt = replay_v2(str_, MO, CAP)
            rh = replay_v2(sho, MO, CAP)
            print(f"  {name:20s} train {rt['return_pct']:+9.1f}% DD {rt['max_dd']:5.1f}%  "
                  f"| holdout {rh['return_pct']:+9.1f}% DD {rh['max_dd']:5.1f}%  "
                  f"(n {rt['n_taken']}/{rh['n_taken']})")


if __name__ == "__main__":
    main()
