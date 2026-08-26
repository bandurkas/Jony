"""Entry-quality sweep (2026-08-26) — кандидаты из диагностики 72 живых сделок
(loss_diag_2026-08-26.py): путы у 7д-хая avg +$1.5/WR69% vs ниже хая +$6.3/WR100%;
regime transition −$25/WR48%; vol_pctile ≥0.85 −$11.

Оси (пост-фильтры/размер поверх живых PUT-гейтов config C, коллы выкл):
  D  dist7dhigh ≥ {1.0,1.5,2.0,3.0}%   — не продавать пут у 7д-хая (зеркало CALL_MIN_DIST_7D_HIGH)
  R  BTC:P regime range-only
  V  vol_pctile ≤ {0.80,0.85,0.90}
  S  size ×0.5 вместо фильтра: near-high / transition / vol>0.85 (кумулятивно, пол 0.25)
  + комбинации D+R, D+V, D+R+V, D+S

Планка отбора (зафиксирована ДО прогона): CALIB-сигма, replay_v2(pkc=1):
  holdout ret ≥ baseline И holdout maxDD ≤ baseline И train ret ≥ 0.9×baseline
  И кварталы fixed-lot не хуже baseline; на CLAMP — не ломается (ho>0).
Пост-фильтр (не пересчёт гейтов): удаление сделки не создаёт новых (кулдаун 30 мин) —
погрешность пренебрежимая.
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc, jony_engine as je
from replay_account_v2 import replay_v2

MO, CAP, PKC = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP, 1
H = 3_600_000

def enrich(coin, trades):
    d1h = je.load_klines(coin, "1h")
    hi7 = d1h["high"].rolling(168, min_periods=24).max().shift(1)
    base = je.build_coin_base(coin)
    pct, _ = je.rolling_vol_pctile_and_high(pd.Series(base["rv1h_native"]), window=jc.HIST, threshold_frac=0.5)
    f = pd.DataFrame({"start_ms": d1h["start_ms"].values, "hi7": hi7.values})
    g = pd.DataFrame({"start_ms": base["start_ms_1h"], "vp": pct.values})
    t = pd.DataFrame({"start_ms": [x["entry_ts"] for x in trades]})
    m = pd.merge_asof(t, f, on="start_ms", direction="backward")
    m = pd.merge_asof(m, g, on="start_ms", direction="backward")
    for x, hi, vp in zip(trades, m["hi7"].values, m["vp"].values):
        x["d_hi"] = (x["entry_spot"] / hi - 1) * 100 if hi and not np.isnan(hi) else None
        x["vp"] = float(vp) if not np.isnan(vp) else None
    return trades

def gen(sig):
    out = []
    for coin in ("ETH", "BTC"):
        tr = je.coin_trades(coin, sides_enabled=("P",), sigma_calib=sig)
        out += enrich(coin, tr)
    out.sort(key=lambda t: t["entry_ts"])
    return out

def filt(trades, d=None, r=False, v=None):
    o = []
    for t in trades:
        if d is not None and t["d_hi"] is not None and t["d_hi"] > -d: continue
        if r and t["coin"] == "BTC" and t["regime"] != "range": continue
        if v is not None and t["vp"] is not None and t["vp"] > v: continue
        o.append(t)
    return o

def size_fn(near=False, trans=False, hv=False, d=1.5, v=0.85):
    def f(t):
        m = 1.0
        if near and t["d_hi"] is not None and t["d_hi"] > -d: m *= 0.5
        if trans and t["regime"] == "transition": m *= 0.5
        if hv and t["vp"] is not None and t["vp"] > v: m *= 0.5
        return max(0.25, m)
    return f

def q_stats(trades):
    sums = [sum(jc.fixed_lot_pnl(t) for t in q) for q in je.quarters(trades) if q]
    return sum(s > 0 for s in sums), len(sums), [round(s) for s in sums]

def evaluate(trades, smf=None):
    tr, ho = je.split(trades, 0.70)
    days = (trades[-1]["entry_ts"] - trades[0]["entry_ts"]) / 86_400_000
    a, b = replay_v2(tr, MO, CAP, per_key_cap=PKC, size_mult_fn=smf), replay_v2(ho, MO, CAP, per_key_cap=PKC, size_mult_fn=smf)
    qp, qn, qs = q_stats(trades)
    return {"n": len(trades), "per_day": round(len(trades) / days, 2),
            "fx_tr": round(sum(jc.fixed_lot_pnl(t) for t in tr)), "fx_ho": round(sum(jc.fixed_lot_pnl(t) for t in ho)),
            "tr_ret": round(a["return_pct"], 1), "tr_dd": round(a["max_dd"], 1),
            "ho_ret": round(b["return_pct"], 1), "ho_dd": round(b["max_dd"], 1),
            "q": f"{qp}/{qn}", "q_sums": qs}

VARIANTS = [("baseline", {}, None)]
for d in (1.0, 1.5, 2.0, 3.0): VARIANTS.append((f"D{d}", {"d": d}, None))
VARIANTS.append(("R", {"r": True}, None))
for v in (0.80, 0.85, 0.90): VARIANTS.append((f"V{v}", {"v": v}, None))
VARIANTS += [("S_near", {}, size_fn(near=True)), ("S_trans", {}, size_fn(trans=True)),
             ("S_hv", {}, size_fn(hv=True)), ("S_all", {}, size_fn(True, True, True))]
for d in (1.5, 2.0):
    VARIANTS += [(f"D{d}+R", {"d": d, "r": True}, None), (f"D{d}+V0.85", {"d": d, "v": 0.85}, None),
                 (f"D{d}+R+V0.85", {"d": d, "r": True, "v": 0.85}, None),
                 (f"D{d}+S_trans", {"d": d}, size_fn(trans=True)), (f"D{d}+S_trans+S_hv", {"d": d}, size_fn(trans=True, hv=True))]

def main():
    rows = []
    for label, sig in (("CALIB", jc.CALIB), ("CLAMP", None)):
        t0 = time.time(); T = gen(sig); print(f"[{label}] trades={len(T)} gen {time.time()-t0:.0f}s", flush=True)
        for name, kw, smf in VARIANTS:
            m = evaluate(filt(T, **kw), smf); m.update(sigma=label, variant=name); rows.append(m)
            print(f"[{label}] {name:18s} n={m['n']:4d} ({m['per_day']}/d) fx {m['fx_tr']:+6d}/{m['fx_ho']:+5d} "
                  f"rep tr {m['tr_ret']:+8.1f}% dd {m['tr_dd']:4.1f} | ho {m['ho_ret']:+7.1f}% dd {m['ho_dd']:4.1f} | q {m['q']} {m['q_sums']}", flush=True)
    out = Path(__file__).parent / "results" / "entry_quality_sweep_2026-08-26.json"
    out.write_text(json.dumps(rows, indent=1)); print("saved", out)

if __name__ == "__main__":
    main()
