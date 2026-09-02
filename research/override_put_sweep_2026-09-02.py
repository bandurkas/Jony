"""R1 (Phase 14, 2026-09-02): mechanical analog of the advisor's counter-trend ("override") put.
Zone: mechanics side-off (ret_7d < -RET_7D_THRESHOLD). Live code guard (advisor.decide_entry):
chg_24h >= -0.5, dist_from_7d_low >= 1.5, not vol_accelerating (rv24 > 1.3*rv24_prev); iv-rv>0 unmeasurable here.
Override trades are ADDED to the honest mechanical book (feature_lag, CALIB, sigma_path b=0.2), replay_v2 pkc=1, $1500.
Bar (fixed before run): ho_ret>=base, ho_dd<=base, tr_ret>=0.9*base_tr, no quarter worse than base by >2pp.
Split/quarter boundaries are fixed from the BASE set (override trades must not move them).
"""
from __future__ import annotations
import json, pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc, jony_engine as je
from replay_account_v2 import replay_v2
MO, CAP, PKC, EQ = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP, 1, 1500
H = 3_600_000
RES = Path(__file__).with_name("results"); RES.mkdir(exist_ok=True)
CACHE = RES / "override_put_base_2026-09-02.pkl"

def base_trades():
    if CACHE.exists():
        return pickle.loads(CACHE.read_bytes())
    T = []
    for c in ("ETH", "BTC"):
        T += je.coin_trades(c, sides_enabled=("P",), sigma_calib=jc.CALIB, feature_lag=True, sigma_path=True, sigma_path_beta=0.2)
    for t in T: t["src"] = "mech"
    T.sort(key=lambda t: t["entry_ts"]); CACHE.write_bytes(pickle.dumps(T)); return T

_F = {}
def features(coin):
    if coin in _F: return _F[coin]
    base = je.build_coin_base(coin, feature_lag=True)
    d5 = je.load_klines(coin, "5m"); d1h = je.load_klines(coin, "1h")
    ret7d = ((d5["close"] - d5["close"].shift(jc.BARS_7D)) / d5["close"].shift(jc.BARS_7D) * 100).values
    c = d1h["close"]; lr = np.log(c / c.shift(1))
    rv24 = lr.pow(2).rolling(24).mean().pow(0.5) * np.sqrt(24 * 365)
    rv = je.rolling_realized_vol(c, 24)
    sig = (jc.CALIB["b0"] + jc.CALIB["b1"] * rv).clip(jc.CALIB["floor"], jc.CALIB["ceiling"])
    f = pd.DataFrame({"start_ms": (d1h["start_ms"] + H).values, "chg24": (c / c.shift(24) - 1).values * 100,
                      "hi7": c.rolling(168, min_periods=24).max().values, "lo7": c.rolling(168, min_periods=24).min().values,
                      "rv24": rv24.values, "rv24_prev": rv24.shift(24).values, "sigma": sig.values})
    m = pd.merge_asof(pd.DataFrame({"start_ms": d5["start_ms"].values}), f, on="start_ms", direction="backward")
    spot = d5["close"].values
    _F[coin] = dict(sm=d5["start_ms"].values, close=spot, high=d5["high"].values, low=d5["low"].values, ret7d=ret7d,
                    chg24=m["chg24"].values, d_lo=(spot / m["lo7"].values - 1) * 100, d_hi=(spot / m["hi7"].values - 1) * 100,
                    accel=(m["rv24"] / m["rv24_prev"]).values, sigma=m["sigma"].values.astype(float))
    return _F[coin]

def override_trades(coin, r7_lo=-3.0, r7_hi=-jc.RET_7D_THRESHOLD, chg24_min=-0.5, dlo_min=1.5, accel_max=1.3,
                    gap_h=4.0, hold_h=120, strike_offset=0.0):
    F = features(coin)
    ok = (F["ret7d"] < r7_hi) & (F["ret7d"] >= r7_lo) & (F["chg24"] >= chg24_min) & (F["d_lo"] >= dlo_min) & ~(F["accel"] > accel_max)
    ok &= ~np.isnan(F["chg24"]) & ~np.isnan(F["d_lo"]) & ~np.isnan(F["sigma"])
    ex = dict(jc.PUT_EXIT); ex["hold_h"] = hold_h
    out, next_ok = [], -1
    for i in np.flatnonzero(ok):
        if F["sm"][i] < next_ok: continue
        next_ok = F["sm"][i] + gap_h * H
        o = je.simulate_option_exit("P", i, F["close"], F["high"], F["low"], F["sm"], float(F["sigma"][i]), ex["tp2_pct"], ex["sl_pct"],
                                    ex["hold_h"], jc.STRIKE_ROUND[coin], strike_offset=strike_offset, sigma_grid=F["sigma"], sigma_path_beta=0.2)
        if o is None: continue
        out.append({"coin": coin, "side": "P", "entry_ts": int(F["sm"][i]), "exit_ts": int(o["exit_ts"]), "resolution": o["resolution"],
                    "pnl_pct": o["pnl_pct"], "strike": o["strike"], "entry_credit": o["entry_credit"], "lot": {"ETH": 0.1, "BTC": 0.01}[coin],
                    "regime": "ovr", "entry_spot": o["entry_spot"], "exit_spot": o["exit_spot"], "sigma": float(F["sigma"][i]), "src": "ovr",
                    "ret7d": float(F["ret7d"][i]), "chg24": float(F["chg24"][i]), "d_lo": float(F["d_lo"][i])})
    return out

def bounds(T):
    ts = sorted(t["entry_ts"] for t in T); t0, t1 = ts[0], ts[-1]
    return t0 + 0.7 * (t1 - t0), [t0 + k * (t1 - t0) / 4 for k in range(1, 4)]

def evaluate(T, split_ts, qb, smf=None):
    T = sorted(T, key=lambda t: t["entry_ts"])
    tr = [t for t in T if t["entry_ts"] < split_ts]; ho = [t for t in T if t["entry_ts"] >= split_ts]
    def R(x): return replay_v2(x, MO, CAP, per_key_cap=PKC, start_equity=EQ, size_mult_fn=smf)
    a, b, f = R(tr), R(ho), R(T)
    qs = [[t for t in T if (k == 0 or t["entry_ts"] >= qb[k - 1]) and (k == 3 or t["entry_ts"] < qb[k])] for k in range(4)]
    q = [round(R(x)["return_pct"], 1) if x else None for x in qs]
    return dict(n=len(T), n_ovr=sum(t["src"] == "ovr" for t in T), tr=round(a["return_pct"], 1), tr_dd=round(a["max_dd"], 1),
                ho=round(b["return_pct"], 1), ho_dd=round(b["max_dd"], 1), full=round(f["return_pct"], 1), full_dd=round(f["max_dd"], 1),
                taken=f["n_taken"], q=q)

def desc(T):
    if not T: return dict(n=0)
    p = [jc.fixed_lot_pnl(t) for t in T]
    return dict(n=len(T), wr=round(np.mean([t["pnl_pct"] > 0 for t in T]) * 100, 1), avg=round(float(np.mean(p)), 2), sum=round(float(np.sum(p)), 1),
                res={k: sum(t["resolution"] == k for t in T) for k in ("tp2", "sl", "time_stop")})

def verdict(r, b):
    ok = r["ho"] >= b["ho"] and r["ho_dd"] <= b["ho_dd"] and r["tr"] >= 0.9 * b["tr"] and \
         all((x is None or y is None or x >= y - 2) for x, y in zip(r["q"], b["q"]))
    return "PASS" if ok else "fail"

OVR = lambda **kw: dict(kw)
VARIANTS = [
    ("live-analog h120", OVR(), 1.0),
    ("h48", OVR(hold_h=48), 1.0),
    ("h120 size0.5", OVR(), 0.5),
    ("h48 size0.5", OVR(hold_h=48), 0.5),
    ("h120 otm0.5%", OVR(strike_offset=0.005), 1.0),
    ("h120 otm1%", OVR(strike_offset=0.01), 1.0),
    ("h48 otm0.5%", OVR(hold_h=48, strike_offset=0.005), 1.0),
    ("h48 otm1% size0.5", OVR(hold_h=48, strike_offset=0.01), 0.5),
    ("zone[-5,-1] h120", OVR(r7_lo=-5.0), 1.0),
    ("zone[-5,-1] h48", OVR(r7_lo=-5.0, hold_h=48), 1.0),
    ("strict dlo3 chg24>=0 h120", OVR(dlo_min=3.0, chg24_min=0.0), 1.0),
    ("strict dlo3 chg24>=0 h48", OVR(dlo_min=3.0, chg24_min=0.0, hold_h=48), 1.0),
    ("gap1h h120", OVR(gap_h=1.0), 1.0),
]

def main():
    B = base_trades(); split_ts, qb = bounds(B)
    base = evaluate(B, split_ts, qb)
    print("BASE", base, flush=True)
    assert (base["tr"], base["ho"], base["full"]) == (9.7, 16.6, 24.7), "fidelity"
    out = {"base": base, "base_desc": {c: desc([t for t in B if t["coin"] == c]) for c in ("ETH", "BTC")}, "variants": {}}
    print("BASE desc", out["base_desc"], flush=True)
    for name, kw, mult in VARIANTS:
        O = override_trades("ETH", **kw) + override_trades("BTC", **kw)
        smf = (lambda t, m=mult: m if t["src"] == "ovr" else 1.0) if mult != 1.0 else None
        r = evaluate(B + O, split_ts, qb, smf)
        r["verdict"] = verdict(r, base)
        r["ovr_desc"] = {c: desc([t for t in O if t["coin"] == c]) for c in ("ETH", "BTC")}
        r["ovr_desc_by_q"] = [desc([t for t in O if (k == 0 or t["entry_ts"] >= qb[k - 1]) and (k == 3 or t["entry_ts"] < qb[k])]) for k in range(4)]
        out["variants"][name] = r
        print(f"{name:28s} n_ovr={r['n_ovr']:4d} tr {r['tr']:6.1f}/{r['tr_dd']:4.1f} ho {r['ho']:6.1f}/{r['ho_dd']:4.1f} full {r['full']:6.1f}/{r['full_dd']:4.1f} "
              f"Q {r['q']} {r['verdict']}  ovr ETH {r['ovr_desc']['ETH']} BTC {r['ovr_desc']['BTC']}", flush=True)
    (RES / "override_put_sweep_2026-09-02.json").write_text(json.dumps(out, indent=1, default=str))

if __name__ == "__main__":
    main()
