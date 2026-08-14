"""Validate exit-sweep survivors (2026-08-14) — separate step from selection.

Selection happened in sweep_exits_v2_2026-08-14.py (train-only, sigma=CALIB).
Here each candidate exit config (picked purely on train) is re-run and judged:
  - fixed-lot expectancy on holdout (CALIB) — did the train edge generalize?
  - same under SIGMA_CLAMP (live pricing) — sigma-model robustness;
  - quarterly fixed-lot breakdown (CALIB) — regime robustness;
  - portfolio replay_v2 (event-ordered) train/holdout with per_key_cap in
    {None, 1} — does it survive real sizing/caps, and what does the risk
    layer add.

Decision-gate criteria (agreed flow): candidate must be positive on BOTH
train and holdout fixed-lot under CALIB, positive under SIGMA_CLAMP holdout,
and >= 3/4 quarters non-catastrophic. Anything else = REJECTED.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je
from replay_account_v2 import replay_v2

CALIB = {"b0": 0.3487, "b1": 0.2646, "floor": 0.25, "ceiling": 1.05}
MO, CAP = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP
TOP_N = 3


def fixed_lot_pnl(t: dict) -> float:
    qty = t["lot"]
    notional = t["strike"] * qty
    premium_total = t["entry_credit"] * qty
    fee_open = jc.fee_usd(notional, premium_total)
    exit_credit = t["entry_credit"] * (1 - t["pnl_pct"])
    fee_close = jc.fee_usd(notional, exit_credit * qty)
    return (t["entry_credit"] - exit_credit) * qty - fee_open - fee_close


def agg(trades):
    n = len(trades)
    if not n:
        return "n=0"
    wr = sum(1 for t in trades if t["pnl_pct"] > 0) / n
    usd = sum(fixed_lot_pnl(t) for t in trades)
    return f"${usd:+9.2f} WR {wr:5.1%} n={n:4d}"


def gen(coin: str, side: str, exit_cfg: dict, sigma_calib):
    kwargs = {"put_exit" if side == "P" else "call_exit": exit_cfg}
    return je.coin_trades(coin, sides_enabled=(side,), sigma_calib=sigma_calib, **kwargs)


def main() -> None:
    results = json.loads(
        Path(__file__).with_name("exit_sweep_results_2026-08-14.json").read_text())

    for key in ("ETH:P", "ETH:C", "BTC:C"):
        coin, side = key.split(":")
        rows = [r for r in results if r["key"] == key and r["train"]["n"] > 0]
        rows.sort(key=lambda r: r["train"]["fixed_usd"], reverse=True)
        print(f"\n{'=' * 74}\n### {key} — validating top-{TOP_N} train picks\n{'=' * 74}")
        for r in rows[:TOP_N]:
            exit_cfg = {"tp2_pct": r["tp2"], "sl_pct": r["sl"], "hold_h": r["hold_h"]}
            print(f"\n-- tp2={r['tp2']} sl={r['sl']} hold={r['hold_h']} "
                  f"(train pick: ${r['train']['fixed_usd']:+.2f})")
            tc = gen(coin, side, exit_cfg, CALIB)
            tr, ho = je.split(tc, 0.70)
            print(f"   CALIB       train {agg(tr)}  | holdout {agg(ho)}")
            ts = gen(coin, side, exit_cfg, None)
            tr2, ho2 = je.split(ts, 0.70)
            print(f"   SIGMA_CLAMP train {agg(tr2)}  | holdout {agg(ho2)}")
            qcells = []
            for i, q in enumerate(je.quarters(tc)):
                usd = sum(fixed_lot_pnl(t) for t in q)
                qcells.append(f"Q{i + 1} ${usd:+8.2f}/{len(q)}")
            print(f"   quarters(CALIB): " + "  ".join(qcells))
            for pkc in (None, 1):
                rt = replay_v2(tr, MO, CAP, per_key_cap=pkc)
                rh = replay_v2(ho, MO, CAP, per_key_cap=pkc)
                print(f"   replay_v2 pkc={str(pkc):4s} train {rt['return_pct']:+8.1f}% "
                      f"DD {rt['max_dd']:5.1f}%  | holdout {rh['return_pct']:+8.1f}% "
                      f"DD {rh['max_dd']:5.1f}%")


if __name__ == "__main__":
    main()
