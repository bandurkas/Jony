"""Walk-forward мета-бэктест адаптивного правила выбора гейтов (2026-08-17).

Симулирует ADAPTIVE_GATES_DESIGN слой 2 по 2 годам БЕЗ lookahead:
- раз в неделю на дату t скорим все комбо белого списка ТОЛЬКО по сделкам,
  закрытым до t (окна 2w/4w/13w/26w по exit_ts);
- floors: pnl_13w > -F, pnl_26w > -F, n_4w >= N_min;
- score = 0.5*rankpct(pnl_2w) + 0.3*rankpct(pnl_4w) + 0.2*rankpct(pnl_13w);
- гистерезис H (rank-pct): смена только если претендент лучше на >= H или
  текущий провалил floors; никто не прошёл -> неделя без входов;
- сделки активного комбо, ОТКРЫТЫЕ в (t, t+7d], идут в сшитый поток;
- сшитые потоки ETH+BTC -> replay_v2 (pkc=1) + кварталы.

Gate: адаптив должен бить лучшую статику (кандидат C Phase 7) на том же
подпериоде (adaptive стартует после 26w прогрева — статики сравниваем на
том же хвосте). Мета-сетка (F, N_min, H) крошечная и печатается ЦЕЛИКОМ —
никакого выбора лучшей ячейки задним числом без пометки.
"""
from __future__ import annotations

import json
import sys
from bisect import bisect_left, bisect_right
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
DAY = 86_400_000
WEEK = 7 * DAY
WARMUP = 26 * 7 * DAY

F_GRID = [100.0, 300.0]
NMIN_GRID = [3, 8]
H_GRID = [0.10, 0.25]

STATIC_C = {"BTC": (1.0, 0.40, "RT"), "ETH": (1.0, 0.60, "R")}
STATIC_A = {"BTC": (0.5, 0.50, "RT"), "ETH": (0.5, 0.50, "RT")}


def load_combos():
    rows = json.load(open(Path(__file__).parent / "results" / "combo_trades_2026-08-17.json"))
    out = {}
    for r in rows:
        key = (r["coin"], r["sigma"])
        out.setdefault(key, {})[(r["ret7d"], r["vol"], r["regime"])] = r["trades"]
    return out


class ComboIndex:
    """Сделки комбо, отсортированные по exit_ts (для окон) и entry_ts (для сшивки)."""

    def __init__(self, trades):
        self.by_exit = sorted(trades, key=lambda t: t[1])
        self.exit_ts = [t[1] for t in self.by_exit]
        self.exit_cum = [0.0]
        for t in self.by_exit:
            self.exit_cum.append(self.exit_cum[-1] + t[2])
        self.by_entry = sorted(trades, key=lambda t: t[0])
        self.entry_ts = [t[0] for t in self.by_entry]

    def pnl_window(self, t0, t1):
        i, j = bisect_right(self.exit_ts, t0), bisect_right(self.exit_ts, t1)
        return self.exit_cum[j] - self.exit_cum[i], j - i

    def entries_in(self, t0, t1):
        i, j = bisect_right(self.entry_ts, t0), bisect_right(self.entry_ts, t1)
        return self.by_entry[i:j]


def rankpct(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    out = [0.0] * len(vals)
    for pos, i in enumerate(order):
        out[i] = pos / max(1, len(vals) - 1)
    return out


def run_adaptive(combos: dict, F: float, nmin: int, H: float):
    idx = {k: ComboIndex(tr) for k, tr in combos.items() if tr}
    keys = list(idx)
    t_start = min(c.entry_ts[0] for c in idx.values() if c.entry_ts)
    t_end = max(c.exit_ts[-1] for c in idx.values() if c.exit_ts)
    t = t_start + WARMUP
    active = None
    stitched, switches, weeks_active, weeks_idle = [], 0, 0, 0
    while t < t_end:
        scores_raw = {}
        for k in keys:
            p2, n2 = idx[k].pnl_window(t - 14 * DAY, t)
            p4, n4 = idx[k].pnl_window(t - 28 * DAY, t)
            p13, _ = idx[k].pnl_window(t - 91 * DAY, t)
            p26, _ = idx[k].pnl_window(t - 182 * DAY, t)
            if p13 > -F and p26 > -F and n4 >= nmin:
                scores_raw[k] = (p2, n2, p4, p13)
        if scores_raw:
            ks = list(scores_raw)
            # ревью 2026-08-17: пустое 2w-окно (n2=0) — «нет данных», не «0»;
            # такие комбо получают НЕЙТРАЛЬНЫЙ ранг 0.5 по 2w вместо места
            # выше всех убыточных (иначе в плохие полосы селектор системно
            # уходил в наименее активные комбо)
            with_data = [k for k in ks if scores_raw[k][1] > 0]
            r2_map = {}
            if with_data:
                r2_vals = rankpct([scores_raw[k][0] for k in with_data])
                r2_map = dict(zip(with_data, r2_vals))
            r4 = rankpct([scores_raw[k][2] for k in ks])
            r13 = rankpct([scores_raw[k][3] for k in ks])
            score = {k: 0.5 * r2_map.get(k, 0.5) + 0.3 * r4[i] + 0.2 * r13[i]
                     for i, k in enumerate(ks)}
            best = max(score, key=score.get)
            if active not in score or score[best] - score[active] >= H:
                if active != best:
                    switches += 1
                active = best
        else:
            active = None
        if active is not None:
            weeks_active += 1
            stitched.extend(idx[active].entries_in(t, t + WEEK))
        else:
            weeks_idle += 1
        t += WEEK
    return stitched, switches, weeks_active, weeks_idle, t_start + WARMUP


def fixed_curve(stitched):
    """Fixed-lot equity curve по exit_ts: total, maxDD$, кварталы."""
    ev = sorted(stitched, key=lambda t: t[1])
    cum, peak, mdd = 0.0, 0.0, 0.0
    for t in ev:
        cum += t[2]
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    if not ev:
        return {"total": 0, "mdd": 0, "q": [], "n": 0}
    t0, t1 = ev[0][1], ev[-1][1]
    step = (t1 - t0) / 4 or 1
    q = [0.0] * 4
    for t in ev:
        q[min(3, int((t[1] - t0) / step))] += t[2]
    return {"total": round(cum), "mdd": round(mdd), "n": len(ev),
            "q": [round(x) for x in q]}


def static_stream(combos, key_spec, t_from):
    tr = combos.get(key_spec, [])
    return [t for t in tr if t[0] > t_from]


def main() -> None:
    all_combos = load_combos()
    for sigma in ("CLAMP", "CALIB"):
        print(f"\n{'='*76}\n### sigma={sigma}\n{'='*76}")
        for F in F_GRID:
            for nmin in NMIN_GRID:
                for H in H_GRID:
                    total = {"total": 0, "n": 0}
                    agg = []
                    sw_all, wa_all, wi_all = 0, 0, 0
                    t_eval = None
                    for coin in ("BTC", "ETH"):
                        combos = all_combos[(coin, sigma)]
                        st, sw, wa, wi, t_from = run_adaptive(combos, F, nmin, H)
                        agg.extend(st)
                        sw_all += sw
                        wa_all += wa
                        wi_all += wi
                        t_eval = t_from
                    r = fixed_curve(agg)
                    print(f"F={F:5.0f} nmin={nmin} H={H:.2f} | "
                          f"adaptive: total ${r['total']:+7d} mdd ${r['mdd']:5d} "
                          f"n={r['n']:4d} q={r['q']} switches={sw_all} "
                          f"active/idle={wa_all}/{wi_all}", flush=True)
        # статики на том же подпериоде (t_from одинаков для всех ячеек)
        for label, spec in (("STATIC C", STATIC_C), ("STATIC A", STATIC_A)):
            agg = []
            for coin in ("BTC", "ETH"):
                combos = all_combos[(coin, sigma)]
                t_start = min(min((t[0] for t in tr), default=1 << 62)
                              for tr in combos.values())
                agg.extend(static_stream(combos, spec[coin], t_start + WARMUP))
            r = fixed_curve(agg)
            print(f"{label:14s} | total ${r['total']:+7d} mdd ${r['mdd']:5d} "
                  f"n={r['n']:4d} q={r['q']}")


if __name__ == "__main__":
    main()
