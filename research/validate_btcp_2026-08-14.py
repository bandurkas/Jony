"""BTC:P full validation (2026-08-14).

trailing_lock_v2 sweep surfaced BTC:P (never traded live — rejected back in
the clairvoyant-harness era "по разбавлению") as the ONLY key positive on
both train and holdout with the stock PUT exits. Before telling the user
anything hopeful: quarters, both sigma models, portfolio replay, risk layer.

Caveat checked here explicitly: CALIB was fitted on ETH markIv — for BTC it
is an extrapolation. SIGMA_CLAMP variant shown side by side.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc

jc.COIN_SIDES = {"ETH": ("P", "C"), "BTC": ("C", "P")}

import jony_engine as je
from replay_account_v2 import replay_v2

CALIB = {"b0": 0.3487, "b1": 0.2646, "floor": 0.25, "ceiling": 1.05}
MO, CAP = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP


def fixed_lot_pnl(t: dict) -> float:
    qty = t["lot"]
    notional = t["strike"] * qty
    fee_open = jc.fee_usd(notional, t["entry_credit"] * qty)
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


def main() -> None:
    for sig_label, sc in (("CALIB(ETH-fitted!)", CALIB), ("SIGMA_CLAMP", None)):
        print(f"\n### sigma = {sig_label}")
        btcp = je.coin_trades("BTC", sides_enabled=("P",), sigma_calib=sc)
        tr, ho = je.split(btcp, 0.70)
        print(f"  BTC:P alone   train {agg(tr)} | holdout {agg(ho)}")
        print("  quarters: ", end="")
        for i, q in enumerate(je.quarters(btcp)):
            usd = sum(fixed_lot_pnl(t) for t in q)
            wr = sum(1 for t in q if t["pnl_pct"] > 0) / len(q) if q else 0
            print(f"Q{i + 1} ${usd:+8.2f}/{len(q)} (WR {wr:.0%})  ", end="")
        print()
        res = {}
        for t in btcp:
            res[t["resolution"]] = res.get(t["resolution"], 0) + 1
        print(f"  resolutions: {res}")

        # portfolio replay: live keys vs live+BTC:P, with and without pkc=1
        eth = je.coin_trades("ETH", sigma_calib=sc)
        btcc = je.coin_trades("BTC", sides_enabled=("C",), sigma_calib=sc)
        live_keys = eth + btcc
        with_p = live_keys + btcp
        for name, pool in (("live keys", live_keys), ("live+BTC:P", with_p),
                           ("BTC:P only", btcp)):
            ptr, pho = je.split(sorted(pool, key=lambda t: t["entry_ts"]), 0.70)
            for pkc in (None, 1):
                rt = replay_v2(ptr, MO, CAP, per_key_cap=pkc)
                rh = replay_v2(pho, MO, CAP, per_key_cap=pkc)
                print(f"  replay {name:11s} pkc={str(pkc):4s} "
                      f"train {rt['return_pct']:+8.1f}% DD {rt['max_dd']:5.1f}% "
                      f"| holdout {rh['return_pct']:+8.1f}% DD {rh['max_dd']:5.1f}%")


if __name__ == "__main__":
    main()
