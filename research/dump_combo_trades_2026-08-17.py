"""Дамп пер-сделочных результатов всех комбо белого списка (2026-08-17).

Для walk-forward мета-бэктеста адаптивного правила (ADAPTIVE_GATES_DESIGN):
каждая комбинация ret7d x vol x regime, только PUT, обе монеты, обе сигмы.
На сделку: entry_ts, exit_ts, fixed-lot pnl$. Дальше все окна/скоринги —
чистый pandas без пересчёта движка.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je

CALIB = {"b0": 0.3487, "b1": 0.2646, "floor": 0.25, "ceiling": 1.05}
SIGMAS = [("CLAMP", None), ("CALIB", CALIB)]
RET7D_GRID = [0.5, 1.0, 1.5, 2.0, 3.0, 999.0]
VOL_GRID = [0.30, 0.40, 0.50, 0.60]
REGIME_GRID = {
    "R": ("range",),
    "RT": ("range", "transition"),
    "RTT": ("range", "transition", "trend"),
}


def fixed_lot_pnl(t: dict) -> float:
    qty = t["lot"]
    notional = t["strike"] * qty
    premium_total = t["entry_credit"] * qty
    fee_open = jc.fee_usd(notional, premium_total)
    exit_credit = t["entry_credit"] * (1 - t["pnl_pct"])
    fee_close = jc.fee_usd(notional, exit_credit * qty)
    return (t["entry_credit"] - exit_credit) * qty - fee_open - fee_close


def run_group(args) -> list[dict]:
    coin, ret7d = args
    jc.COIN_SIDES = {"ETH": ("P", "C"), "BTC": ("C", "P")}
    jc.RET_7D_THRESHOLD = ret7d
    je._BASE_CACHE.pop(coin, None)
    out = []
    for sig_label, calib in SIGMAS:
        for vol in VOL_GRID:
            for reg_key, regimes in REGIME_GRID.items():
                g = dict(jc.PUT_GEN)
                g["vol_threshold"] = vol
                g["regime_filter"] = regimes
                trades = je.coin_trades(coin, sides_enabled=("P",), put_gen=g,
                                        sigma_calib=calib)
                out.append({
                    "coin": coin, "sigma": sig_label, "ret7d": ret7d,
                    "vol": vol, "regime": reg_key,
                    "trades": [[t["entry_ts"], t["exit_ts"],
                                round(fixed_lot_pnl(t), 2)] for t in trades],
                })
                print(f"{coin} {sig_label} ret7d={ret7d} vol={vol} {reg_key}: "
                      f"{len(trades)} trades", flush=True)
    return out


def main() -> None:
    import multiprocessing as mp
    jobs = [(coin, r) for coin in ("BTC", "ETH") for r in RET7D_GRID]
    with mp.get_context("spawn").Pool(min(12, len(jobs))) as pool:
        groups = pool.map(run_group, jobs)
    rows = [r for g in groups for r in g]
    out = Path(__file__).parent / "results" / "combo_trades_2026-08-17.json"
    out.write_text(json.dumps(rows))
    print(f"\nsaved {len(rows)} combos -> {out} "
          f"({out.stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
