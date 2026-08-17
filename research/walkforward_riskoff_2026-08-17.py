"""Risk-off overlay поверх статики C (2026-08-17) — вторая, осторожная
версия адаптации после провала winner-picking (walkforward_adaptive):
торгуем фикс. конфиг C, но еженедельно монета ВЫКЛЮЧАЕТСЯ, если её
трейлинг-4w fixed-lot PnL < -K, и включается обратно, когда трейлинг-2w >= 0.
Адаптация только режет — не выбирает.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_p = Path(__file__).parent / "walkforward_adaptive_2026-08-17.py"
spec = importlib.util.spec_from_file_location("wf", _p)
wf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wf)

DAY, WEEK, WARMUP = wf.DAY, wf.WEEK, wf.WARMUP
STATIC_C = wf.STATIC_C

K_GRID = [100.0, 200.0, 400.0]


def run_riskoff(trades, K):
    idx = wf.ComboIndex(trades)
    t0, t1 = idx.entry_ts[0], idx.exit_ts[-1]
    t = t0 + WARMUP
    on, stitched, toggles = True, [], 0
    weeks_on = weeks_off = 0
    while t < t1:
        p4, _ = idx.pnl_window(t - 28 * DAY, t)
        p2, _ = idx.pnl_window(t - 14 * DAY, t)
        if on and p4 < -K:
            on = False
            toggles += 1
        elif not on and p2 >= 0:
            on = True
            toggles += 1
        if on:
            weeks_on += 1
            stitched.extend(idx.entries_in(t, t + WEEK))
        else:
            weeks_off += 1
        t += WEEK
    return stitched, toggles, weeks_on, weeks_off


def main() -> None:
    all_combos = wf.load_combos()
    for sigma in ("CLAMP", "CALIB"):
        print(f"\n### sigma={sigma}")
        for K in K_GRID:
            agg, tg, won, woff = [], 0, 0, 0
            for coin in ("BTC", "ETH"):
                st, t_, w1, w0 = run_riskoff(all_combos[(coin, sigma)][STATIC_C[coin]], K)
                agg.extend(st)
                tg += t_
                won += w1
                woff += w0
            r = wf.fixed_curve(agg)
            print(f"K={K:5.0f} | total ${r['total']:+6d} mdd ${r['mdd']:5d} n={r['n']:4d} "
                  f"q={r['q']} toggles={tg} on/off={won}/{woff}")
        agg = []
        for coin in ("BTC", "ETH"):
            tr = all_combos[(coin, sigma)][STATIC_C[coin]]
            t0 = min(t[0] for t in tr)
            agg.extend([t for t in tr if t[0] > t0 + WARMUP])
        r = wf.fixed_curve(agg)
        print(f"STATIC C      | total ${r['total']:+6d} mdd ${r['mdd']:5d} n={r['n']:4d} q={r['q']}")


if __name__ == "__main__":
    main()
