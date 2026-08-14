"""Exit-parameter sweep per key, evaluated ONLY with honest metrics (2026-08-14).

Motivation: PUT_EXIT tp2=0.70/sl=1.75 needs WR 71.4% to break even; observed
~67% (backtest) / 88% on 17 live trades (small sample). Phase 1 showed no key
subset is positive on train with baseline exits. This sweep asks: does ANY
(tp2, sl, hold_h) make a key's raw fixed-lot expectancy positive on TRAIN,
and does that survive holdout + quarters + both sigma models?

Protocol (anti-overfit, mirrors the BUBU sweep discipline):
  1. sweep on TRAIN only, sigma=CALIB (markIv-fitted, realistic);
  2. report top configs per key by train fixed-lot $;
  3. validation of survivors happens in a SEPARATE step (validate script),
     never inside the same selection loop.

Research-only. Writes results to exit_sweep_results_2026-08-14.json.
"""
from __future__ import annotations

import itertools
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je

CALIB = {"b0": 0.3487, "b1": 0.2646, "floor": 0.25, "ceiling": 1.05}

TP2S = (0.40, 0.50, 0.60, 0.70, 0.85)
SLS = (0.50, 0.75, 1.00, 1.50, 1.75)
HOLDS = (24, 72, 120)

KEYS = (("ETH", "P"), ("ETH", "C"), ("BTC", "C"))


def fixed_lot_pnl(t: dict) -> float:
    qty = t["lot"]
    notional = t["strike"] * qty
    premium_total = t["entry_credit"] * qty
    fee_open = jc.fee_usd(notional, premium_total)
    exit_credit = t["entry_credit"] * (1 - t["pnl_pct"])
    fee_close = jc.fee_usd(notional, exit_credit * qty)
    return (t["entry_credit"] - exit_credit) * qty - fee_open - fee_close


def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    n = len(trades)
    return {
        "n": n,
        "wr": sum(1 for t in trades if t["pnl_pct"] > 0) / n,
        "mean_pct": sum(t["pnl_pct"] for t in trades) / n * 100,
        "fixed_usd": sum(fixed_lot_pnl(t) for t in trades),
    }


def main() -> None:
    out = []
    t0 = time.time()
    combos = list(itertools.product(TP2S, SLS, HOLDS))
    for coin, side in KEYS:
        key = f"{coin}:{side}"
        for i, (tp2, sl, hold) in enumerate(combos):
            exit_cfg = {"tp2_pct": tp2, "sl_pct": sl, "hold_h": hold}
            kwargs = {"put_exit": exit_cfg} if side == "P" else {"call_exit": exit_cfg}
            trades = je.coin_trades(coin, sides_enabled=(side,),
                                    sigma_calib=CALIB, **kwargs)
            tr, ho = je.split(trades, 0.70)
            rec = {"key": key, "tp2": tp2, "sl": sl, "hold_h": hold,
                   "train": stats(tr), "holdout": stats(ho)}
            out.append(rec)
            if i % 15 == 0:
                el = time.time() - t0
                print(f"[{el:7.0f}s] {key} {i + 1}/{len(combos)} "
                      f"tp2={tp2} sl={sl} hold={hold} "
                      f"train${rec['train'].get('fixed_usd', 0):+.0f}", flush=True)
    Path(__file__).with_name("exit_sweep_results_2026-08-14.json").write_text(
        json.dumps(out))
    print(f"\ndone in {time.time() - t0:.0f}s, {len(out)} configs")

    for coin, side in KEYS:
        key = f"{coin}:{side}"
        rows = [r for r in out if r["key"] == key and r["train"]["n"] > 0]
        rows.sort(key=lambda r: r["train"]["fixed_usd"], reverse=True)
        print(f"\n=== {key}: top-5 by TRAIN fixed-lot $ (selection metric) ===")
        for r in rows[:5]:
            tr, ho = r["train"], r["holdout"]
            print(f"  tp2={r['tp2']:.2f} sl={r['sl']:.2f} hold={r['hold_h']:3d}  "
                  f"train ${tr['fixed_usd']:+9.2f} WR {tr['wr']:.1%} n={tr['n']}  |  "
                  f"holdout ${ho.get('fixed_usd', 0):+9.2f} WR {ho.get('wr', 0):.1%} n={ho.get('n', 0)}")
        base = next((r for r in rows if r["tp2"] == 0.70 and
                     r["sl"] == (1.75 if side == "P" else 0.75) and
                     r["hold_h"] == (120 if side == "P" else 24)), None)
        if base:
            print(f"  [live baseline] tp2=0.70 sl={base['sl']:.2f} hold={base['hold_h']} "
                  f"train ${base['train']['fixed_usd']:+9.2f} holdout ${base['holdout']['fixed_usd']:+9.2f}")


if __name__ == "__main__":
    main()
