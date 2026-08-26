"""Honest 2y replay of the CURRENT live config (ETH:P + BTC:P, pkc=1, config C gates,
PUT exits 0.70/1.75/120h) — 2026-08-27.

Portfolio lookahead (CB@entry, equity@entry) is already fixed by replay_v2. This script
closes the two remaining optimistic assumptions in trade GENERATION (jony_engine):
  L  feature_lag  — 15m/1h features available at bar END (default merges the in-progress
                    bar's final close onto 5m bars: up to 55 min of future price in
                    regime/rv1h/dir1h/ema_ratio and in the pricing sigma)
  S  sigma_path   — reprice the option along the hold with the sigma known at each bar
                    (default freezes entry sigma → ignores IV expansion on adverse moves)
  spread          — 2% (default) vs 5% / 10% sensitivity (live spread-guard caps 10%)
Fidelity: variant 'v0' must reproduce entry_quality_sweep_2026-08-26 baseline
(n=4539, fx +3481/+1551, CALIB $800 tr +22.8 / ho +28.8).
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc, jony_engine as je
from replay_account_v2 import replay_v2

MO, CAP, PKC = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP, 1
VARIANTS = [
    ("v0 as-is (Phase10 baseline)", dict()),
    ("L  feature_lag",              dict(feature_lag=True)),
    ("S  sigma_path",               dict(sigma_path=True)),
    ("LS honest",                   dict(feature_lag=True, sigma_path=True)),
    ("LS honest spread5",           dict(feature_lag=True, sigma_path=True, spread_pct=5.0)),
    ("LS honest spread10",          dict(feature_lag=True, sigma_path=True, spread_pct=10.0)),
]

def gen(sig, **kw):
    out = []
    for coin in ("ETH", "BTC"):
        out += je.coin_trades(coin, sides_enabled=("P",), sigma_calib=sig, **kw)
    out.sort(key=lambda t: t["entry_ts"]); return out

def fx(ts): return sum(jc.fixed_lot_pnl(t) for t in ts)

def evaluate(T):
    tr, ho = je.split(T, 0.70)
    r = {"n": len(T), "fx_tr": round(fx(tr)), "fx_ho": round(fx(ho)),
         "wr": round(100 * sum(t["pnl_pct"] > 0 for t in T) / len(T), 1),
         "res": {k: sum(t["resolution"] == k for t in T) for k in ("tp2", "sl", "time_stop")}}
    for key in ("ETH:P", "BTC:P"):
        sub = [t for t in T if f"{t['coin']}:{t['side']}" == key]
        r[f"fx_{key}"] = round(fx(sub)); r[f"n_{key}"] = len(sub)
    for eq in (800, 1500):
        a = replay_v2(tr, MO, CAP, per_key_cap=PKC, start_equity=eq)
        b = replay_v2(ho, MO, CAP, per_key_cap=PKC, start_equity=eq)
        f = replay_v2(T, MO, CAP, per_key_cap=PKC, start_equity=eq)
        r[f"rep{eq}"] = {"tr": (round(a["return_pct"], 1), round(a["max_dd"], 1)),
                         "ho": (round(b["return_pct"], 1), round(b["max_dd"], 1)),
                         "full": (round(f["return_pct"], 1), round(f["max_dd"], 1)),
                         "skip_size_full": f["n_skipped_size"], "taken_full": f["n_taken"]}
    qs = [q for q in je.quarters(T) if q]
    r["q_fx"] = [round(fx(q)) for q in qs]
    r["q_rep1500"] = [round(replay_v2(q, MO, CAP, per_key_cap=PKC, start_equity=1500)["return_pct"], 1) for q in qs]
    return r

def main():
    rows = []
    for label, sig in (("CALIB", jc.CALIB), ("CLAMP", None)):
        for name, kw in VARIANTS:
            t0 = time.time(); T = gen(sig, **kw); m = evaluate(T)
            m.update(sigma=label, variant=name, kw=kw); rows.append(m)
            r8, r15 = m["rep800"], m["rep1500"]
            print(f"[{label}] {name:28s} n={m['n']:4d} WR {m['wr']:4.1f} res {m['res']} | fx tr {m['fx_tr']:+6d} ho {m['fx_ho']:+5d} "
                  f"(ETH:P {m['fx_ETH:P']:+6d}/{m['n_ETH:P']} BTC:P {m['fx_BTC:P']:+6d}/{m['n_BTC:P']})\n"
                  f"        $800  tr {r8['tr'][0]:+7.1f}%/dd{r8['tr'][1]:5.1f} ho {r8['ho'][0]:+7.1f}%/dd{r8['ho'][1]:5.1f} full {r8['full'][0]:+7.1f}%/dd{r8['full'][1]:5.1f} skip_size {r8['skip_size_full']}\n"
                  f"        $1500 tr {r15['tr'][0]:+7.1f}%/dd{r15['tr'][1]:5.1f} ho {r15['ho'][0]:+7.1f}%/dd{r15['ho'][1]:5.1f} full {r15['full'][0]:+7.1f}%/dd{r15['full'][1]:5.1f} skip_size {r15['skip_size_full']}\n"
                  f"        quarters fx {m['q_fx']}  rep$1500 {m['q_rep1500']}  ({time.time()-t0:.0f}s)", flush=True)
    out = Path(__file__).parent / "results" / "honest_replay_2026-08-27.json"
    out.write_text(json.dumps(rows, indent=1)); print("saved", out)

if __name__ == "__main__":
    main()
