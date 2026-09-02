"""R4 — mid-life trailing profit-lock for mechanical 120h puts on the HONEST engine (2026-09-02).
Context: live #77 BTC:P peaked +28% of credit long before 84h (0.70*hold) and was never locked
(posture=normal mechanical positions never trail; endgame rule needs 84h).
Engine: ETH:P+BTC:P, CALIB, feature_lag, sigma_path beta=0.2, replay_v2 pkc=1, $1500.
Trail is CLOSE-mark based (loop.py pnl_pct_mark), peak tracked from entry (live semantics),
fires when peak>=arm AND age>=age_frac*hold_h AND pnl<=peak-giveback; SL/TP2 (intrabar, engine)
keep priority on a same-bar tie; time_stop keeps priority at the last bar; trail buyback pays half-spread.
PRIMARY (pre-registered, decides): arm 0.30, age 0.40, giveback 0.10.
Selection bar (fixed before run): ho>=base, ho_dd<=base, tr>=0.9*base_tr, no quarter worse than base by >2pp.
Fidelity: trail-off run must equal je.coin_trades per trade AND reproduce
n=4560, tr +9.7/dd23.3, ho +16.6/dd11.1, full +24.7/dd29.4, Q [8.7, 22.9, -14.9, 20.3].
"""
from __future__ import annotations
import json, pickle, sys, time
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc, jony_engine as je
from replay_account_v2 import replay_v2

MO, CAP, PKC, EQ = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP, 1, 1500
BETA, HS = 0.2, je.HALF_SPREAD
EX = jc.PUT_EXIT; HOLD_H = EX["hold_h"]
RES = Path(__file__).parent / "results"
PKL = RES / "midlife_trail_base_2026-09-02.pkl"
OUT = RES / "midlife_trail_sweep_2026-09-02.json"
EXPECT = dict(n=4560, tr=9.7, tr_dd=23.3, ho=16.6, ho_dd=11.1, full=24.7, full_dd=29.4, q=[8.7, 22.9, -14.9, 20.3])


def sigma_grid(coin):
    d5, d1h = je.load_klines(coin, "5m"), je.load_klines(coin, "1h")
    rv = je.rolling_realized_vol(d1h["close"], 24)
    s = (jc.CALIB["b0"] + jc.CALIB["b1"] * rv).clip(jc.CALIB["floor"], jc.CALIB["ceiling"])
    g = pd.DataFrame({"start_ms": d1h["start_ms"] + 3_600_000, "sigma": s.values})  # feature_lag
    return d5, pd.merge_asof(d5[["start_ms"]], g, on="start_ms", direction="backward")["sigma"].values.astype(float)


def build(coin):
    trades = je.coin_trades(coin, sides_enabled=("P",), sigma_calib=jc.CALIB, feature_lag=True, sigma_path=True, sigma_path_beta=BETA)
    d5, sg = sigma_grid(coin)
    close, high, low, sms = d5["close"].values, d5["high"].values, d5["low"].values, d5["start_ms"].values
    bars = int(HOLD_H * 12)
    for t in trades:
        i = int(np.searchsorted(sms, t["entry_ts"])); assert sms[i] == t["entry_ts"]
        lo, hi = i + 1, min(i + 1 + bars, len(close)); m = hi - lo
        T = np.maximum(0.0, (jc.TARGET_EXPIRY_H - np.arange(1, m + 1) * 5 / 60) / (24 * 365))
        sig, K, cred = t["sigma"], t["strike"], t["entry_credit"]
        sf = sg[lo:hi]; sf = np.where(np.isnan(sf) | (sf <= 0), sig, sf); sf = sig * (sf / sig) ** BETA
        tp2_mid, sl_mid = cred * (1 - EX["tp2_pct"]) / (1 + HS), cred * (1 + EX["sl_pct"]) / (1 + HS)
        ph, pl = je._vec_bs_price("P", low[lo:hi], K, T, sf), je._vec_bs_price("P", high[lo:hi], K, T, sf)
        pc = je._vec_bs_price("P", close[lo:hi], K, T, sf)
        slh, tph = np.flatnonzero(ph >= sl_mid), np.flatnonzero(pl <= tp2_mid)
        t["first_sl"] = int(slh[0]) if len(slh) else -1
        t["first_tp"] = int(tph[0]) if len(tph) else -1
        t["p"] = (cred - pc * (1 + HS)) / cred            # close-mark pnl fraction of credit, per bar
        t["lo"] = lo
    return trades, sms


def load_base(rebuild=False):
    if PKL.exists() and not rebuild:
        return pickle.loads(PKL.read_bytes())
    data = {}
    for coin in ("ETH", "BTC"):
        t0 = time.time(); tr, sms = build(coin); data[coin] = (tr, sms)
        print(f"built {coin}: {len(tr)} trades ({time.time() - t0:.0f}s)", flush=True)
    PKL.write_bytes(pickle.dumps(data)); return data


def fidelity(data):
    bad = 0
    for coin, (trades, sms) in data.items():
        for t in trades:
            p, m = t["p"], len(t["p"])
            c = [(k, r) for k, r in ((t["first_sl"], 0), (t["first_tp"], 1)) if k >= 0]
            if c:
                k, r = min(c); res, pnl = ("sl", -EX["sl_pct"]) if r == 0 else ("tp2", EX["tp2_pct"])
            else:
                k, res, pnl = m - 1, "time_stop", p[-1]
            ok = res == t["resolution"] and int(sms[t["lo"] + k]) == t["exit_ts"] and abs(pnl - t["pnl_pct"]) < 1e-9
            bad += not ok
    return bad


def apply(t, sms, rule):
    if rule is None: return t
    p, m = t["p"], len(t["p"])
    el = np.arange(1, m + 1) * 5 / 60
    aged = el >= rule["age"] * HOLD_H
    peak = np.maximum.accumulate(np.where(aged, p, -9.0) if rule.get("peak_after_age") else p)
    fire = (peak >= rule["arm"]) & (p <= peak - rule["gb"]) & aged
    if rule.get("min_profit") is not None: fire &= p >= rule["min_profit"]
    h = np.flatnonzero(fire); ft = int(h[0]) if len(h) else -1
    c = [(k, r) for k, r in ((t["first_sl"], 0), (t["first_tp"], 1), (ft, 2)) if k >= 0]
    if not c: return t
    k, r = min(c)
    if r != 2 or k >= m - 1: return t
    n = {kk: v for kk, v in t.items() if kk not in ("p", "first_sl", "first_tp", "lo")}
    n.update(exit_ts=int(sms[t["lo"] + k]), pnl_pct=float(p[k]), resolution="trail", base_res=t["resolution"],
             base_pnl=t["pnl_pct"], base_exit_ts=t["exit_ts"], trail_h=float(el[k]), peak=float(peak[k]))
    return n


def strip(t): return {k: v for k, v in t.items() if k not in ("p", "first_sl", "first_tp", "lo")}


def gen(data, rule):
    out = []
    for coin, (trades, sms) in data.items():
        out += [apply(t, sms, rule) if rule else strip(t) for t in trades]
    out.sort(key=lambda t: t["entry_ts"]); return out


def evaluate(T):
    tr, ho = je.split(T, 0.70)
    a, b, f = (replay_v2(x, MO, CAP, per_key_cap=PKC, start_equity=EQ) for x in (tr, ho, T))
    q = [round(float(replay_v2(x, MO, CAP, per_key_cap=PKC, start_equity=EQ)["return_pct"]), 1) for x in je.quarters(T) if x]
    return dict(n=len(T), tr=round(a["return_pct"], 1), tr_dd=round(a["max_dd"], 1), ho=round(b["return_pct"], 1), ho_dd=round(b["max_dd"], 1),
                full=round(f["return_pct"], 1), full_dd=round(f["max_dd"], 1), taken=f["n_taken"], q=q,
                n_trail=sum(t["resolution"] == "trail" for t in T), res={k: sum(t["resolution"] == k for t in T) for k in ("tp2", "sl", "time_stop", "trail")})


def delta(T):
    ex = [t for t in T if t["resolution"] == "trail"]
    if not ex: return {}
    fx_t = [jc.fixed_lot_pnl(t) for t in ex]
    fx_b = [jc.fixed_lot_pnl({**t, "pnl_pct": t["base_pnl"]}) for t in ex]
    by = {}
    for r in ("tp2", "sl", "time_stop"):
        s = [(a, b, t) for a, b, t in zip(fx_t, fx_b, ex) if t["base_res"] == r]
        if s: by[r] = dict(n=len(s), fx_trail=round(sum(a for a, _, _ in s)), fx_base=round(sum(b for _, b, _ in s)),
                           avg_pnl_trail=round(float(np.mean([t["pnl_pct"] for _, _, t in s])), 3),
                           avg_pnl_base=round(float(np.mean([t["base_pnl"] for _, _, t in s])), 3))
    split_ts = T[0]["entry_ts"] + 0.70 * (T[-1]["entry_ts"] - T[0]["entry_ts"])
    return dict(n=len(ex), avg_pnl_trail=round(float(np.mean([t["pnl_pct"] for t in ex])), 3), avg_pnl_base=round(float(np.mean([t["base_pnl"] for t in ex])), 3),
                fx_trail=round(sum(fx_t)), fx_base=round(sum(fx_b)), fx_delta=round(sum(fx_t) - sum(fx_b)),
                fx_delta_train=round(sum(a - b for a, b, t in zip(fx_t, fx_b, ex) if t["entry_ts"] < split_ts)),
                fx_delta_ho=round(sum(a - b for a, b, t in zip(fx_t, fx_b, ex) if t["entry_ts"] >= split_ts)),
                avg_trail_h=round(float(np.mean([t["trail_h"] for t in ex])), 1), avg_peak=round(float(np.mean([t["peak"] for t in ex])), 3),
                by_base_res=by, by_key={k: sum(1 for t in ex if f"{t['coin']}:{t['side']}" == k) for k in ("ETH:P", "BTC:P")})


def decompose(T):
    """Diagnostic (not selection): split the replay effect of a trail into per-trade pnl vs slot-freeing (pkc=1)."""
    def sw(t, pnl, ts):
        if t["resolution"] != "trail": return t
        return {**t, "pnl_pct": t["base_pnl"] if pnl == "base" else t["pnl_pct"], "exit_ts": t["base_exit_ts"] if ts == "base" else t["exit_ts"]}
    def ev(x):
        tr, ho = je.split(x, 0.70); a, b, f = (replay_v2(y, MO, CAP, per_key_cap=PKC, start_equity=EQ) for y in (tr, ho, x))
        return dict(tr=round(a["return_pct"], 1), ho=round(b["return_pct"], 1), full=round(f["return_pct"], 1), full_dd=round(f["max_dd"], 1), taken=f["n_taken"])
    fx = lambda x: sum(jc.fixed_lot_pnl(t) for t in x)
    tr, ho = je.split(T, 0.70); trb, hob = je.split([sw(t, "base", "base") for t in T], 0.70)
    taken = []; replay_v2([sw(t, "base", "base") for t in T], MO, CAP, per_key_cap=PKC, start_equity=EQ, size_mult_fn=lambda t: taken.append(t["entry_ts"]) or 1.0)
    tk = [t for t in T if t["entry_ts"] in set(taken)]; tkx = [t for t in tk if t["resolution"] == "trail"]
    taken_subset = dict(n=len(tk), n_trail=len(tkx), fx_base=round(fx([sw(t, "base", "base") for t in tk])), fx_trail=round(fx(tk)),
                        fx_delta_trail_exits=round(fx(tkx) - fx([sw(t, "base", "base") for t in tkx])),
                        by_base_res={r: dict(n=sum(t["base_res"] == r for t in tkx), fx_delta=round(sum(jc.fixed_lot_pnl(t) - jc.fixed_lot_pnl({**t, "pnl_pct": t["base_pnl"]}) for t in tkx if t["base_res"] == r))) for r in ("tp2", "sl", "time_stop")})
    return dict(taken_subset=taken_subset,trail_pnl_base_ts=ev([sw(t, "trail", "base") for t in T]), base_pnl_trail_ts=ev([sw(t, "base", "trail") for t in T]),
                nopkc_trail=dict(full=round(replay_v2(T, MO, CAP, start_equity=EQ)["return_pct"], 1), full_dd=round(replay_v2(T, MO, CAP, start_equity=EQ)["max_dd"], 1)),
                nopkc_base=dict(full=round(replay_v2([sw(t, "base", "base") for t in T], MO, CAP, start_equity=EQ)["return_pct"], 1),
                                full_dd=round(replay_v2([sw(t, "base", "base") for t in T], MO, CAP, start_equity=EQ)["max_dd"], 1)),
                fx_total=dict(trail_tr=round(fx(tr)), trail_ho=round(fx(ho)), base_tr=round(fx(trb)), base_ho=round(fx(hob))))


def peak_stats(data):
    out = {}
    for coin, (trades, _) in data.items():
        for t in trades:
            pk = np.maximum.accumulate(t["p"]); k48 = min(len(pk), 48 * 12) - 1
            d = out.setdefault(t["resolution"], dict(n=0, peak30_any=0, peak30_by48h=0, peak30_by48h_then_end_below20=0))
            d["n"] += 1; d["peak30_any"] += bool(pk[-1] >= 0.30); d["peak30_by48h"] += bool(pk[k48] >= 0.30)
            d["peak30_by48h_then_end_below20"] += bool(pk[k48] >= 0.30 and t["pnl_pct"] < 0.20)
    return out


VARIANTS = [("baseline", None), ("PRIMARY arm0.30 age0.40 gb0.10", dict(arm=0.30, age=0.40, gb=0.10))]
VARIANTS += [(f"nb arm{a:.2f} age{g:.2f} gb0.10", dict(arm=a, age=g, gb=0.10)) for a in (0.25, 0.30, 0.35) for g in (0.30, 0.40, 0.50) if not (a == 0.30 and g == 0.40)]
VARIANTS += [("ref live-endgame literal arm0.25 age0.70", dict(arm=0.25, age=0.70, gb=0.10)),
             ("ref live-exact arm0.20 gb0.10 minp0.25 age0.70", dict(arm=0.20, age=0.70, gb=0.10, min_profit=0.25)),
             ("ref uncond arm0.20 gb0.10 age0", dict(arm=0.20, age=0.0, gb=0.10)),
             ("ref uncond arm0.30 gb0.10 age0", dict(arm=0.30, age=0.0, gb=0.10)),
             ("xtra PRIMARY peak-after-age", dict(arm=0.30, age=0.40, gb=0.10, peak_after_age=True))]


def main():
    data = load_base("--rebuild" in sys.argv)
    bad = fidelity(data); print(f"fidelity per-trade mismatches: {bad}", flush=True)
    rows, base = [], None
    for name, rule in VARIANTS:
        T = gen(data, rule); m = evaluate(T); m["variant"] = name; m["rule"] = rule
        if base is None:
            base = m; m["fidelity_ok"] = all(abs(m[k] - EXPECT[k]) < 0.05 if k != "q" else all(abs(x - y) < 0.05 for x, y in zip(m["q"], EXPECT["q"])) for k in EXPECT) and bad == 0
            print(f"fidelity vs expected: {'OK' if m['fidelity_ok'] else 'FAIL'} {EXPECT}", flush=True)
        m["pass"] = bool(m["ho"] >= base["ho"] and m["ho_dd"] <= base["ho_dd"] and m["tr"] >= 0.9 * base["tr"] and all(x >= y - 2.0 for x, y in zip(m["q"], base["q"])))
        m["delta"] = delta(T) if rule else {}
        if name.startswith("PRIMARY") or name == "nb arm0.30 age0.50 gb0.10":
            m["decompose"] = decompose(T); print(f"    DECOMP {json.dumps(m['decompose'])}", flush=True)
        rows.append(m)
        print(f"{name:46s} n={m['n']} trail={m['n_trail']:4d} taken {m['taken']:3d} | tr {m['tr']:+6.1f}/dd{m['tr_dd']:4.1f} ho {m['ho']:+6.1f}/dd{m['ho_dd']:4.1f} "
              f"full {m['full']:+6.1f}/dd{m['full_dd']:4.1f} | Q {m['q']} {'PASS' if m['pass'] else ''}", flush=True)
        if rule and m["delta"]:
            d = m["delta"]; print(f"    trail exits {d['n']} (ETH {d['by_key']['ETH:P']}/BTC {d['by_key']['BTC:P']}) avg pnl {d['avg_pnl_trail']:+.3f} vs base {d['avg_pnl_base']:+.3f} | "
                                  f"fx ${d['fx_trail']:+d} vs ${d['fx_base']:+d} = {d['fx_delta']:+d} (tr {d['fx_delta_train']:+d} / ho {d['fx_delta_ho']:+d}) | "
                                  f"avg age {d['avg_trail_h']}h peak {d['avg_peak']:.3f} | by base: " + " ".join(f"{k}:{v['n']} ${v['fx_trail']:+d}vs${v['fx_base']:+d}" for k, v in d['by_base_res'].items()), flush=True)
    ps = peak_stats(data); print("peak stats (base trades by resolution):", json.dumps(ps), flush=True)
    OUT.write_text(json.dumps(dict(expect=EXPECT, per_trade_mismatches=bad, rows=rows, peak_stats=ps), indent=1)); print("saved", OUT)


if __name__ == "__main__":
    main()
