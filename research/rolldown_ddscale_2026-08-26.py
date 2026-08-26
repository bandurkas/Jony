"""Roll-down + DD-scale (2026-08-26). Roll-down: put ITM и 1h-close < 24h-лоу входа →
откуп по mid+HS и немедленное переоткрытие ATM (A: всегда; B: только если ready_P на баре).
DD-scale: size×0.5 при просадке счёта > thr% (перехват jc.size_position, видит equity)."""
from __future__ import annotations
import sys, numpy as np, pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc, jony_engine as je, backtest_bs as bs
from replay_account_v2 import replay_v2
H = 3_600_000; MO, CAP = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP

def arrays(coin):
    d5 = je.load_klines(coin, "5m")
    base = je.build_coin_base(coin); sig = je.evaluate_gates(base)
    rp = pd.merge_asof(d5[["start_ms"]], sig[["start_ms", "ready_P"]], on="start_ms", direction="backward")["ready_P"].fillna(False).values
    return d5["close"].values, d5["high"].values, d5["low"].values, d5["start_ms"].values, rp

def rolldown(coin, trades, require_ready):
    close, high, low, sms, ready = arrays(coin)
    idx_of = {int(s): i for i, s in enumerate(sms)}
    ex = jc.PUT_EXIT; out = []; n_roll = 0
    for t in trades:
        if t["side"] != "P": out.append(t); continue
        i0 = idx_of.get(t["entry_ts"]); 
        if i0 is None or i0 < 288: out.append(t); continue
        ref = low[i0 - 288:i0].min(); K = t["strike"]; sig = t["sigma"]
        j_end = idx_of.get(t["exit_ts"], i0)
        js = np.arange(i0 + 1, j_end + 1)
        if not len(js): out.append(t); continue
        hourly = ((sms[js] + 300_000) % H == 0)
        hit = js[hourly & (close[js] < ref) & (close[js] < K)]
        if not len(hit): out.append(t); continue
        j = int(hit[0]); T = max(0.0, (jc.TARGET_EXPIRY_H - (j - i0) * 5 / 60) / (24 * 365))
        mid = bs.price("P", close[j], K, T, sig); buy = mid * (1 + je.HALF_SPREAD)
        t1 = dict(t, exit_ts=int(sms[j]), exit_spot=close[j], resolution="roll_stop",
                  pnl_pct=(t["entry_credit"] - buy) / t["entry_credit"])
        out.append(t1); n_roll += 1
        if require_ready and not ready[j]: continue
        o = je.simulate_option_exit("P", j, close, high, low, sms, sig, ex["tp2_pct"], ex["sl_pct"], ex["hold_h"], jc.STRIKE_ROUND[coin])
        if o: out.append({**t, "entry_ts": int(sms[j]), "exit_ts": int(o["exit_ts"]), "resolution": "roll_" + o["resolution"],
                          "pnl_pct": o["pnl_pct"], "strike": o["strike"], "entry_credit": o["entry_credit"],
                          "entry_spot": o["entry_spot"], "exit_spot": o["exit_spot"]})
    out.sort(key=lambda t: t["entry_ts"]); return out, n_roll

_orig = jc.size_position; _st = {"peak": 0.0, "thr": None}
def _patched(equity, *a, **kw):
    _st["peak"] = max(_st["peak"], equity)
    if _st["thr"] is not None and _st["peak"] > 0 and (_st["peak"] - equity) / _st["peak"] * 100 > _st["thr"]:
        kw["size_mult"] = kw.get("size_mult", 1.0) * 0.5
    return _orig(equity, *a, **kw)
jc.size_position = _patched
import replay_account_v2 as R; R.jc.size_position = _patched

def ev(T, EQ, name, thr=None):
    _st["thr"] = thr; res = []
    for part in (je.split(T, 0.70)[0], je.split(T, 0.70)[1], T):
        _st["peak"] = 0.0; res.append(replay_v2(part, MO, CAP, start_equity=EQ, per_key_cap=1))
    a, b, f = res
    print(f"  eq{EQ} {name:28s} tr {a['return_pct']:+6.1f}/{a['max_dd']:4.1f} ho {b['return_pct']:+6.1f}/{b['max_dd']:4.1f} full {f['return_pct']:+7.1f}/{f['max_dd']:4.1f} taken {f['n_taken']}", flush=True)

for label, sigc in (("CALIB", jc.CALIB), ("CLAMP", None)):
    per = {c: je.coin_trades(c, sides_enabled=("P",), sigma_calib=sigc) for c in ("ETH", "BTC")}
    base = sorted(per["ETH"] + per["BTC"], key=lambda t: t["entry_ts"])
    print(f"[{label}] base n={len(base)}")
    variants = {"baseline": base}
    for rr, nm in ((False, "rolldown A (always re-enter)"), (True, "rolldown B (re-enter if ready)")):
        acc = []; nr = 0
        for c in ("ETH", "BTC"):
            o, n = rolldown(c, per[c], rr); acc += o; nr += n
        acc.sort(key=lambda t: t["entry_ts"]); variants[f"{nm} [{nr} rolls]"] = acc
    fx = lambda T: round(sum(jc.fixed_lot_pnl(t) for t in T))
    for nm, T in variants.items(): print(f"  fixed-lot {nm:40s} {fx(T):+7d}  q={[round(sum(jc.fixed_lot_pnl(t) for t in q)) for q in je.quarters(T)]}")
    for EQ in (800, 1500):
        for nm, T in variants.items(): ev(T, EQ, nm)
        for thr in (5, 10, 15): ev(base, EQ, f"dd-scale x0.5 if DD>{thr}%", thr)
