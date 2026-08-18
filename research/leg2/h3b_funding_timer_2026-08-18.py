#!/usr/bin/env python3
"""H3b: extension of H3 (funding fade) to 3.7y (2022-12 .. 2026-08).

TEST A (decisive, clean OOS): exact H3 selected config (P=90, filter=True,
H=24h, S=5%) run UNCHANGED on the 2022-12..2024-08 segment H3 never saw.
TEST B (plateau re-sweep): full 3.7y, 70/30 split, same grid as H3, survival
criterion = plateau (train>0 AND all grid neighbors >=0 on train); only
plateau configs get one look at holdout.

Engine is byte-identical to h3_funding_fade_2026-08-18.py (signal on closed
data, entry at close of NEXT bar, intrabar stop, mid-rank exit, funding
cashflow during hold, fees 0.075%/side, $1000 notional, one position/symbol).
"""
import json, bisect
from collections import defaultdict
from datetime import datetime, timezone

LEG2 = "/Users/styserg/Desktop/Jony/research/leg2"
FRESH = "/Users/styserg/Desktop/Jony/research/fresh_data"
NOTIONAL = 1000.0
FEE_SIDE = 0.00075
WINDOW_EVENTS = 90
H3_CFG = (90, True, 24, 5)   # P, filter, H, S  — frozen from h3_results.log
LOG = []

def log(s=""):
    LOG.append(s)
    print(s)

def dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

def load_funding(sym):
    d = json.load(open(f"{LEG2}/funding_{sym}.json"))
    d.sort(key=lambda x: x[0])
    return [(int(t), float(r)) for t, r in d]

def load_candles_merged(name):
    old = json.load(open(f"{LEG2}/{name}_1h_2022.json"))
    new = json.load(open(f"{FRESH}/{name}_1h.json"))
    seen = {}
    for b in old + new:
        seen[int(b["start_ms"])] = b   # fresh overwrites old on overlap
    bars = [seen[k] for k in sorted(seen)]
    return bars

def pct_rank(window, v):
    lt = sum(1 for x in window if x < v)
    eq = sum(1 for x in window if x == v)
    return 100.0 * (lt + 0.5 * eq) / len(window)

def prepare(sym, candle_name):
    fund = load_funding(sym)
    bars = load_candles_merged(candle_name)
    f_ts = [t for t, _ in fund]
    f_rt = [r for _, r in fund]
    ranks = [None] * len(fund)
    for i in range(len(fund)):
        if i + 1 >= WINDOW_EVENTS:
            w = f_rt[i + 1 - WINDOW_EVENTS: i + 1]
            ranks[i] = pct_rank(w, f_rt[i])
    out = []
    for b in bars:
        close_t = b["start_ms"] + 3_600_000
        j = bisect.bisect_right(f_ts, close_t) - 1
        rk = ranks[j] if j >= 0 else None
        out.append({"t": close_t, "o": b["open"], "h": b["high"], "l": b["low"],
                    "c": b["close"], "rank": rk})
    return out, f_ts, f_rt

def run_config(bars, f_ts, f_rt, P, use_filter, H, S, split_t):
    trades = []
    i = 24
    n = len(bars)
    stop = S / 100.0
    while i < n - 1:
        b = bars[i]
        rk = b["rank"]
        if rk is None:
            i += 1; continue
        side = 0
        if rk > P:
            side = -1
        elif rk < 100 - P:
            side = 1
        if side and use_filter:
            r24 = bars[i]["c"] / bars[i - 24]["c"] - 1.0
            if side == -1 and not (r24 < 0): side = 0
            if side == 1 and not (r24 > 0): side = 0
        if not side:
            i += 1; continue
        ei = i + 1
        e = bars[ei]
        entry_t, entry_p = e["t"], e["c"]
        exit_i, exit_p, reason = None, None, None
        last_i = min(ei + H, n - 1)
        j = ei + 1
        while j <= last_i:
            bj = bars[j]
            if side == -1 and bj["h"] >= entry_p * (1 + stop):
                exit_i, exit_p, reason = j, entry_p * (1 + stop), "stop"; break
            if side == 1 and bj["l"] <= entry_p * (1 - stop):
                exit_i, exit_p, reason = j, entry_p * (1 - stop), "stop"; break
            if bj["rank"] is not None and 40 <= bj["rank"] <= 60 and j + 1 <= last_i:
                exit_i, exit_p, reason = j + 1, bars[j + 1]["c"], "mid"; break
            if j == last_i:
                exit_i, exit_p, reason = j, bj["c"], "time"; break
            j += 1
        if exit_i is None:
            exit_i, exit_p, reason = last_i, bars[last_i]["c"], "eod"
        exit_t = bars[exit_i]["t"]
        if side == -1:
            px_pnl = NOTIONAL * (entry_p - exit_p) / entry_p
        else:
            px_pnl = NOTIONAL * (exit_p - entry_p) / entry_p
        fees = 2 * FEE_SIDE * NOTIONAL
        lo = bisect.bisect_right(f_ts, entry_t)
        hi = bisect.bisect_right(f_ts, exit_t)
        f_pnl = 0.0
        for k in range(lo, hi):
            r = f_rt[k]
            f_pnl += (r * NOTIONAL) if side == -1 else (-r * NOTIONAL)
        net = px_pnl + f_pnl - fees
        trades.append({"side": side, "entry_t": entry_t, "exit_t": exit_t,
                       "px": px_pnl, "fund": f_pnl, "fees": fees, "net": net,
                       "train": entry_t < split_t})
        i = exit_i + 1
    return trades

def agg(trs):
    if not trs:
        return {"n": 0, "net": 0.0, "px": 0.0, "fund": 0.0, "fees": 0.0, "wr": 0.0}
    return {"n": len(trs),
            "net": sum(t["net"] for t in trs),
            "px": sum(t["px"] for t in trs),
            "fund": sum(t["fund"] for t in trs),
            "fees": sum(t["fees"] for t in trs),
            "wr": 100.0 * sum(1 for t in trs if t["net"] > 0) / len(trs)}

def qtr(ts):
    d = dt(ts)
    return f"{d.year}Q{(d.month - 1)//3 + 1}"

def report_block(tr, cond, label):
    log(f"--- {label} ---")
    for sym in ("BTCUSDT", "ETHUSDT"):
        for side, nm in [(-1, "SHORT"), (1, "LONG")]:
            a = agg([t for t in tr[sym] if cond(t) and t["side"] == side])
            log(f"  {sym} {nm:5}: n={a['n']:>3} net=${a['net']:>8.2f}  px=${a['px']:>8.2f}  "
                f"funding=${a['fund']:>7.2f}  fees=${a['fees']:>6.2f}  WR={a['wr']:.0f}%")
        a = agg([t for t in tr[sym] if cond(t)])
        log(f"  {sym} ALL  : n={a['n']:>3} net=${a['net']:>8.2f}  px=${a['px']:>8.2f}  "
            f"funding=${a['fund']:>7.2f}  fees=${a['fees']:>6.2f}  WR={a['wr']:.0f}%")
    a = agg([t for s in tr for t in tr[s] if cond(t)])
    log(f"  COMBINED    : n={a['n']:>3} net=${a['net']:>8.2f}  px=${a['px']:>8.2f}  "
        f"funding=${a['fund']:>7.2f}  fees=${a['fees']:>6.2f}  WR={a['wr']:.0f}%")
    log("")
    return a

def quarterly(tr, cond, label):
    log(f"--- QUARTERLY ({label}) ---")
    q = defaultdict(lambda: defaultdict(float)); qn = defaultdict(int)
    for s in tr:
        for t in tr[s]:
            if not cond(t): continue
            k = qtr(t["entry_t"]); q[k][s] += t["net"]; q[k]["net"] += t["net"]; qn[k] += 1
    nonneg = 0
    for k in sorted(q):
        v = q[k]
        flag = "OK " if v["net"] >= 0 else "NEG"
        nonneg += v["net"] >= 0
        log(f"  {k}: n={qn[k]:>3}  net=${v['net']:>8.2f}  (BTC ${v.get('BTCUSDT',0):>7.2f} / ETH ${v.get('ETHUSDT',0):>7.2f})  {flag}")
    nq = len(q)
    pct = 100.0 * nonneg / nq if nq else 0.0
    log(f"  quarters non-negative: {nonneg}/{nq} = {pct:.0f}%")
    log("")
    return pct, min((q[k]["net"] for k in q), default=0.0)

def complementarity(tr, cond, btc_bars, label):
    bt = [b["t"] for b in btc_bars]
    def r7(ts):
        j = bisect.bisect_right(bt, ts) - 1
        if j < 168: return None
        return btc_bars[j]["c"] / btc_bars[j - 168]["c"] - 1.0
    dd_pnl, tot_pnl, dd_n = 0.0, 0.0, 0
    for s in tr:
        for t in tr[s]:
            if not cond(t): continue
            tot_pnl += t["net"]
            v = r7(t["exit_t"])
            if v is not None and v < -0.01:
                dd_pnl += t["net"]; dd_n += 1
    log(f"--- COMPLEMENTARITY ({label}; BTC ret_7d < -1% at exit) ---")
    log(f"  trades in drawdown periods: {dd_n}, net there ${dd_pnl:.2f}; total net ${tot_pnl:.2f}")
    if tot_pnl != 0:
        log(f"  share of profit in BTC-drawdown periods: {100.0*dd_pnl/tot_pnl:.0f}%"
            + ("  (sign-caveat: total<=0)" if tot_pnl < 0 else ""))
    log("")

def main():
    data = {}
    for sym, cn in [("BTCUSDT", "btc"), ("ETHUSDT", "eth")]:
        data[sym] = prepare(sym, cn)
    btc_bars = data["BTCUSDT"][0]
    t0, t1 = btc_bars[0]["t"], btc_bars[-1]["t"]
    # OOS boundary = start of the segment H3 actually used (first fresh_data bar close)
    fresh0 = min(int(b["start_ms"]) for b in json.load(open(f"{FRESH}/btc_1h.json"))) + 3_600_000
    log(f"H3b funding-timer backtest  run {datetime.now(timezone.utc).isoformat()}")
    log(f"merged period {dt(t0):%Y-%m-%d} .. {dt(t1):%Y-%m-%d}  ({len(btc_bars)} bars/symbol)")
    log(f"H3-unseen OOS segment: {dt(t0):%Y-%m-%d} .. {dt(fresh0):%Y-%m-%d} (entries before boundary)")
    log(f"notional ${NOTIONAL:.0f}, fees {FEE_SIDE*100:.3f}%/side, rank window {WINDOW_EVENTS} funding events")
    log("")

    # ================= TEST A =================
    P, f, H, S = H3_CFG
    log(f"=================== TEST A: frozen H3 config P={P} filter={f} H={H}h S={S}% on unseen 2022-12..2024-08 ===================")
    trA = {sym: run_config(*data[sym], P, f, H, S, split_t=t1 + 1) for sym in data}
    in_oos = lambda t: t["entry_t"] < fresh0
    aA = report_block(trA, in_oos, "TEST A (OOS segment only)")
    pctA, worst_qA = quarterly(trA, in_oos, "TEST A OOS segment")
    complementarity(trA, in_oos, btc_bars, "TEST A OOS segment")
    # sanity: same config on the H3 period, from the merged engine (should match H3 ~$266 total)
    aA_h3 = agg([t for s in trA for t in trA[s] if t["entry_t"] >= fresh0])
    log(f"  [sanity] same config on H3 period (2024-08..2026-08): n={aA_h3['n']} net=${aA_h3['net']:.2f} (H3 reported n=278 net=$266.41 train+holdout)")
    testA_pass = aA["net"] > 0
    log(f"  TEST A RESULT: net=${aA['net']:.2f} n={aA['n']} WR={aA['wr']:.0f}%  -> {'POSITIVE' if testA_pass else 'FAILED'}")
    log("")

    # ================= TEST B =================
    log("=================== TEST B: plateau re-sweep on full 3.7y (70/30 split) ===================")
    split_t = t0 + int(0.7 * (t1 - t0))
    log(f"split(70/30) at {dt(split_t):%Y-%m-%d}")
    Ps, Hs, Ss, Fs = (90, 95, 98), (24, 48, 72), (3, 5), (False, True)
    grid = [(p, ff, h, s) for p in Ps for ff in Fs for h in Hs for s in Ss]
    results, train_net, train_n = {}, {}, {}
    for cfg in grid:
        p, ff, h, s = cfg
        tr = {sym: run_config(*data[sym], p, ff, h, s, split_t) for sym in data}
        results[cfg] = tr
        trs = [t for sym in tr for t in tr[sym] if t["train"]]
        train_net[cfg] = sum(t["net"] for t in trs)
        train_n[cfg] = len(trs)
    log("")
    log("=== TRAIN SWEEP (net$ combined) ===")
    log(f"{'P':>3} {'filt':>4} {'H':>3} {'S%':>3} | {'n':>5} {'net$':>9}")
    for cfg in grid:
        p, ff, h, s = cfg
        log(f"{p:>3} {str(ff)[0]:>4} {h:>3} {s:>3} | {train_n[cfg]:>5} {train_net[cfg]:>9.2f}")
    log("")

    def neighbors(cfg, include_filter):
        p, ff, h, s = cfg
        out = []
        ip, ih, isx = Ps.index(p), Hs.index(h), Ss.index(s)
        for d in (-1, 1):
            if 0 <= ip + d < len(Ps): out.append((Ps[ip + d], ff, h, s))
            if 0 <= ih + d < len(Hs): out.append((p, ff, Hs[ih + d], s))
            if 0 <= isx + d < len(Ss): out.append((p, ff, h, Ss[isx + d]))
        if include_filter:
            out.append((p, not ff, h, s))
        return out

    plateau_std, plateau_strict = [], []
    for cfg in grid:
        if train_net[cfg] <= 0: continue
        nb_std = neighbors(cfg, include_filter=False)
        nb_str = neighbors(cfg, include_filter=True)
        if all(train_net[nb] >= 0 for nb in nb_std):
            plateau_std.append(cfg)
        if all(train_net[nb] >= 0 for nb in nb_str):
            plateau_strict.append(cfg)

    log(f"=== PLATEAU CONFIGS (train>0 AND all +-1-step neighbors >=0 on train) ===")
    log(f"  standard (P/H/S neighbors, filter fixed): {len(plateau_std)}  {plateau_std}")
    log(f"  strict (also filter-toggle neighbor)   : {len(plateau_strict)}  {plateau_strict}")
    log("")

    testB_pass = False
    if plateau_std:
        best = max(plateau_std, key=lambda c: train_net[c])
        p, ff, h, s = best
        log(f"=== HOLDOUT (one look) for plateau configs; best-by-train = P={p} filter={ff} H={h} S={s} ===")
        for cfg in plateau_std:
            tr = results[cfg]
            ho = [t for sym in tr for t in tr[sym] if not t["train"]]
            ho_net = sum(t["net"] for t in ho)
            wr = 100.0 * sum(1 for t in ho if t["net"] > 0) / len(ho) if ho else 0.0
            log(f"  P={cfg[0]} filter={cfg[1]} H={cfg[2]} S={cfg[3]}: train n={train_n[cfg]} net=${train_net[cfg]:.2f} | holdout n={len(ho)} net=${ho_net:.2f} WR={wr:.0f}%")
            if cfg == best:
                testB_pass = ho_net > 0
        log("")
        trB = results[best]
        report_block(trB, lambda t: not t["train"], "TEST B best plateau — HOLDOUT detail")
        pctB, worst_qB = quarterly(trB, lambda t: True, "TEST B best plateau, full 3.7y")
        complementarity(trB, lambda t: True, btc_bars, "TEST B best plateau, full period")
    else:
        log("  NO plateau configs -> no holdout look taken")
        pctB, worst_qB = 0.0, 0.0
        log("")

    # ================= VERDICT =================
    log("=== VERDICT ===")
    log(f"  TEST A (decisive OOS): net=${aA['net']:.2f} n={aA['n']} -> {'pass' if testA_pass else 'FAIL'}")
    log(f"  TEST B: plateau_std={len(plateau_std)} plateau_strict={len(plateau_strict)}; best-plateau holdout>0: {testB_pass}")
    log(f"  quarters non-negative: TEST A OOS {pctA:.0f}%" + (f" | TEST B full {pctB:.0f}%" if plateau_std else ""))
    if testA_pass and testB_pass and max(pctA, pctB if plateau_std else 0) >= 60:
        verdict = "PROCEED"
    elif testA_pass or (plateau_std and testB_pass):
        verdict = "MIXED"
    else:
        verdict = "REJECTED"
    log(f"  VERDICT: {verdict}")

    with open(f"{LEG2}/h3b_results.log", "w") as fh:
        fh.write("\n".join(LOG) + "\n")

if __name__ == "__main__":
    main()
