"""Advisor-rule sweep on the HONEST engine (2026-08-27): mechanical stand-ins for rules an
LLM advisor could emit, using only information available at entry time. Post-filters / size
multipliers on honest ETH:P+BTC:P candidates (feature_lag, CALIB, sigma_path beta=0.2), replay_v2 pkc=1, $1500.
Selection bar (fixed BEFORE the run): ho_ret >= base AND ho_dd <= base AND tr_ret >= 0.9*base AND quarters not worse.
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc, jony_engine as je
from replay_account_v2 import replay_v2
MO, CAP, PKC, EQ = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP, 1, 1500
H = 3_600_000

def enrich(coin, trades):
    d1h = je.load_klines(coin, "1h"); rv = je.rolling_realized_vol(d1h["close"], 24)
    t1 = d1h["start_ms"] + H  # available at bar end
    f = pd.DataFrame({"start_ms": t1.values, "rv24": rv.values, "rv24_prev": rv.shift(24).values,
                      "chg24": (d1h["close"] / d1h["close"].shift(24) - 1).values * 100,
                      "hi7": d1h["high"].rolling(168, min_periods=24).max().values,
                      "lo7": d1h["low"].rolling(168, min_periods=24).min().values})
    base = je.build_coin_base(coin, feature_lag=True)
    pct, _ = je.rolling_vol_pctile_and_high(pd.Series(base["rv1h_native"]), window=jc.HIST, threshold_frac=0.5)
    g = pd.DataFrame({"start_ms": base["start_ms_1h"], "vp": pct.values})
    t = pd.DataFrame({"start_ms": [x["entry_ts"] for x in trades]})
    m = pd.merge_asof(t, f, on="start_ms", direction="backward"); m = pd.merge_asof(m, g, on="start_ms", direction="backward")
    for x, r in zip(trades, m.itertuples()):
        x["accel"] = r.rv24 / r.rv24_prev if r.rv24_prev and r.rv24_prev > 0 else None
        x["chg24"] = r.chg24; x["vp"] = None if np.isnan(r.vp) else float(r.vp)
        x["d_hi"] = (x["entry_spot"] / r.hi7 - 1) * 100 if r.hi7 else None
        x["d_lo"] = (x["entry_spot"] / r.lo7 - 1) * 100 if r.lo7 else None
        tm = time.gmtime(x["entry_ts"] / 1000); x["wd"] = tm.tm_wday; x["hr"] = tm.tm_hour
    return trades

def gen():
    out = []
    for coin in ("ETH", "BTC"):
        out += enrich(coin, je.coin_trades(coin, sides_enabled=("P",), sigma_calib=jc.CALIB, feature_lag=True, sigma_path=True, sigma_path_beta=0.2))
    out.sort(key=lambda t: t["entry_ts"]); return out

def evaluate(T, smf=None):
    tr, ho = je.split(T, 0.70)
    a = replay_v2(tr, MO, CAP, per_key_cap=PKC, start_equity=EQ, size_mult_fn=smf)
    b = replay_v2(ho, MO, CAP, per_key_cap=PKC, start_equity=EQ, size_mult_fn=smf)
    f = replay_v2(T, MO, CAP, per_key_cap=PKC, start_equity=EQ, size_mult_fn=smf)
    q = [round(replay_v2(x, MO, CAP, per_key_cap=PKC, start_equity=EQ, size_mult_fn=smf)["return_pct"], 1) for x in je.quarters(T) if x]
    return dict(n=len(T), tr=round(a["return_pct"], 1), tr_dd=round(a["max_dd"], 1), ho=round(b["return_pct"], 1), ho_dd=round(b["max_dd"], 1),
                full=round(f["return_pct"], 1), full_dd=round(f["max_dd"], 1), taken=f["n_taken"], q=q)

def F(pred): return lambda T: [t for t in T if not pred(t)]          # filter: drop when pred true
def S(pred, m=0.5): return lambda t: m if pred(t) else 1.0            # size: multiply when pred true

ACC = lambda k: (lambda t: t["accel"] is not None and t["accel"] > k)
VARIANTS = [
    ("baseline", None, None),
    # A. vol acceleration (advisor's vol_accelerating)
    *[(f"A_filter_accel>{k}", F(ACC(k)), None) for k in (1.3, 1.5, 2.0)],
    *[(f"A_size_accel>{k}", None, S(ACC(k))) for k in (1.3, 1.5, 2.0)],
    # B. vol percentile too high
    *[(f"B_filter_vp>{v}", F(lambda t, v=v: t["vp"] is not None and t["vp"] > v), None) for v in (0.85, 0.95)],
    ("B_size_vp>0.85", None, S(lambda t: t["vp"] is not None and t["vp"] > 0.85)),
    # C. posture-as-size in transition regime
    ("C_size_transition", None, S(lambda t: t["regime"] == "transition")),
    ("C_filter_transition", F(lambda t: t["regime"] == "transition"), None),
    # I. falling knife / overextended 24h
    *[(f"I_filter_chg24<-{k}", F(lambda t, k=k: t["chg24"] is not None and t["chg24"] < -k), None) for k in (2, 3, 5)],
    *[(f"I_filter_chg24>+{k}", F(lambda t, k=k: t["chg24"] is not None and t["chg24"] > k), None) for k in (3, 5)],
    ("I_size_chg24<-3", None, S(lambda t: t["chg24"] is not None and t["chg24"] < -3)),
    # J. weekend / time-of-day
    ("J_filter_weekend", F(lambda t: t["wd"] >= 5), None),
    ("J_filter_fri_after12", F(lambda t: t["wd"] == 4 and t["hr"] >= 12 or t["wd"] >= 5), None),
    ("J_size_weekend", None, S(lambda t: t["wd"] >= 5)),
    # E. near 7d high (recheck on honest engine) / near 7d low
    ("E_size_near_hi1.5", None, S(lambda t: t["d_hi"] is not None and t["d_hi"] > -1.5)),
    ("E_filter_near_lo2", F(lambda t: t["d_lo"] is not None and t["d_lo"] < 2.0), None),
    # combos of anything plausible
    ("K_size_accel1.5+trans", None, lambda t: (0.5 if ACC(1.5)(t) else 1.0) * (0.5 if t["regime"] == "transition" else 1.0)),
    ("K_filter_accel2+chg24<-3", F(lambda t: ACC(2.0)(t) or (t["chg24"] is not None and t["chg24"] < -3)), None),
]

def main():
    T = gen(); print("honest candidates", len(T), flush=True); rows = []
    base = None
    for name, filt, smf in VARIANTS:
        sub = filt(T) if filt else T
        m = evaluate(sub, smf); m["variant"] = name
        if base is None: base = m
        ok = m["ho"] >= base["ho"] and m["ho_dd"] <= base["ho_dd"] and m["tr"] >= 0.9 * base["tr"] and sum(x < 0 for x in m["q"]) <= sum(x < 0 for x in base["q"])
        m["pass"] = bool(ok); rows.append(m)
        print(f"{name:26s} n={m['n']:4d} taken {m['taken']:3d} | tr {m['tr']:+6.1f}/dd{m['tr_dd']:4.1f} ho {m['ho']:+6.1f}/dd{m['ho_dd']:4.1f} full {m['full']:+6.1f}/dd{m['full_dd']:4.1f} | Q {m['q']} {'PASS' if ok else ''}", flush=True)
    Path("results/advisor_rules_sweep_2026-08-27.json").write_text(json.dumps(rows, indent=1))

if __name__ == "__main__":
    main()
