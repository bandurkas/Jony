#!/usr/bin/env python3
"""H3: fade extreme funding on Bybit perps (BTC/ETH), honest backtest.

Protocol:
- signal from already-known funding rates only (rate known at its fundingRateTimestamp)
- percentile rank in rolling 30d (=90 events) window of PAST funding values
- execution at close of NEXT 1h bar after signal bar
- fees 0.075%/side, notional $1000, funding cashflow during hold accounted separately
- train/holdout 70/30 by time, sweep on train only
"""
import json, bisect, math
from collections import defaultdict
from datetime import datetime, timezone

LEG2 = "/Users/styserg/Desktop/Jony/research/leg2"
FRESH = "/Users/styserg/Desktop/Jony/research/fresh_data"
NOTIONAL = 1000.0
FEE_SIDE = 0.00075          # 0.075% per side
WINDOW_EVENTS = 90          # 30d * 3 funding events/day
LOG = []

def log(s=""):
    LOG.append(s)
    print(s)

def load_funding(sym):
    d = json.load(open(f"{LEG2}/funding_{sym}.json"))
    d.sort(key=lambda x: x[0])
    return [(int(t), float(r)) for t, r in d]

def load_candles(name):
    d = json.load(open(f"{FRESH}/{name}_1h.json"))
    d.sort(key=lambda x: x["start_ms"])
    return d

def pct_rank(window, v):
    lt = sum(1 for x in window if x < v)
    eq = sum(1 for x in window if x == v)
    return 100.0 * (lt + 0.5 * eq) / len(window)

def prepare(sym, candle_name):
    """Return bars with per-bar known funding rank, and funding event list."""
    fund = load_funding(sym)
    bars = load_candles(candle_name)
    f_ts = [t for t, _ in fund]
    f_rt = [r for _, r in fund]
    # precompute rank at each funding event (rank of event i vs previous WINDOW_EVENTS incl itself)
    ranks = [None] * len(fund)
    for i in range(len(fund)):
        if i + 1 >= WINDOW_EVENTS:
            w = f_rt[i + 1 - WINDOW_EVENTS: i + 1]
            ranks[i] = pct_rank(w, f_rt[i])
    out = []
    for b in bars:
        close_t = b["start_ms"] + 3_600_000
        j = bisect.bisect_right(f_ts, close_t) - 1   # latest funding known at bar close
        rk = ranks[j] if j >= 0 else None
        out.append({"t": close_t, "o": b["open"], "h": b["high"], "l": b["low"],
                    "c": b["close"], "rank": rk, "f_idx": j})
    return out, f_ts, f_rt

def run_config(bars, f_ts, f_rt, P, use_filter, H, S, split_t):
    """Sequential engine, one position at a time. Returns trade list."""
    trades = []
    i = 24  # need ret_24h
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
        # entry at close of next bar
        ei = i + 1
        e = bars[ei]
        entry_t, entry_p = e["t"], e["c"]
        # walk forward
        exit_i, exit_p, reason = None, None, None
        last_i = min(ei + H, n - 1)
        j = ei + 1
        while j <= last_i:
            bj = bars[j]
            # intrabar stop
            if side == -1 and bj["h"] >= entry_p * (1 + stop):
                exit_i, exit_p, reason = j, entry_p * (1 + stop), "stop"; break
            if side == 1 and bj["l"] <= entry_p * (1 - stop):
                exit_i, exit_p, reason = j, entry_p * (1 - stop), "stop"; break
            # rank back to middle -> exit at close of NEXT bar
            if bj["rank"] is not None and 40 <= bj["rank"] <= 60 and j + 1 <= last_i:
                exit_i, exit_p, reason = j + 1, bars[j + 1]["c"], "mid"; break
            if j == last_i:
                exit_i, exit_p, reason = j, bj["c"], "time"; break
            j += 1
        if exit_i is None:  # ran off data end
            exit_i, exit_p, reason = last_i, bars[last_i]["c"], "eod"
        exit_t = bars[exit_i]["t"]
        # price pnl
        if side == -1:
            px_pnl = NOTIONAL * (entry_p - exit_p) / entry_p
        else:
            px_pnl = NOTIONAL * (exit_p - entry_p) / entry_p
        fees = 2 * FEE_SIDE * NOTIONAL
        # funding cashflow: events strictly after entry, up to and incl exit
        lo = bisect.bisect_right(f_ts, entry_t)
        hi = bisect.bisect_right(f_ts, exit_t)
        f_pnl = 0.0
        for k in range(lo, hi):
            r = f_rt[k]
            f_pnl += (r * NOTIONAL) if side == -1 else (-r * NOTIONAL)
        net = px_pnl + f_pnl - fees
        trades.append({"side": side, "entry_t": entry_t, "exit_t": exit_t,
                       "entry_p": entry_p, "exit_p": exit_p, "reason": reason,
                       "px": px_pnl, "fund": f_pnl, "fees": fees, "net": net,
                       "train": entry_t < split_t})
        i = exit_i + 1  # no re-entry while in position
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
    d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    return f"{d.year}Q{(d.month - 1)//3 + 1}"

def main():
    data = {}
    for sym, cn in [("BTCUSDT", "btc"), ("ETHUSDT", "eth")]:
        bars, f_ts, f_rt = prepare(sym, cn)
        data[sym] = (bars, f_ts, f_rt)
    # intersection window / split (same candle grid both symbols)
    t0 = data["BTCUSDT"][0][0]["t"]; t1 = data["BTCUSDT"][0][-1]["t"]
    split_t = t0 + int(0.7 * (t1 - t0))
    log(f"H3 funding-fade backtest  run {datetime.now(timezone.utc).isoformat()}")
    log(f"period {datetime.fromtimestamp(t0/1000,tz=timezone.utc):%Y-%m-%d} .. "
        f"{datetime.fromtimestamp(t1/1000,tz=timezone.utc):%Y-%m-%d}  "
        f"split(70/30) {datetime.fromtimestamp(split_t/1000,tz=timezone.utc):%Y-%m-%d}")
    log(f"notional ${NOTIONAL:.0f}, fees {FEE_SIDE*100:.3f}%/side, rank window {WINDOW_EVENTS} funding events (30d)")
    log("")

    grid = [(P, f, H, S) for P in (90, 95, 98) for f in (False, True)
            for H in (24, 48, 72) for S in (3, 5)]
    results = {}
    for cfg in grid:
        P, f, H, S = cfg
        tr = {sym: run_config(*data[sym], P, f, H, S, split_t) for sym in data}
        results[cfg] = tr

    # train sweep table
    log("=== TRAIN SWEEP (net$, per symbol and combined; sweep on train only) ===")
    log(f"{'P':>3} {'filt':>4} {'H':>3} {'S%':>3} | {'BTC n':>5} {'BTC net':>8} | {'ETH n':>5} {'ETH net':>8} | {'tot n':>5} {'tot net':>9}")
    rows = []
    for cfg, tr in results.items():
        P, f, H, S = cfg
        a = {s: agg([t for t in tr[s] if t["train"]]) for s in tr}
        tot_n = sum(a[s]["n"] for s in a); tot_net = sum(a[s]["net"] for s in a)
        rows.append((cfg, a, tot_n, tot_net))
        log(f"{P:>3} {str(f)[0]:>4} {H:>3} {S:>3} | {a['BTCUSDT']['n']:>5} {a['BTCUSDT']['net']:>8.2f} | "
            f"{a['ETHUSDT']['n']:>5} {a['ETHUSDT']['net']:>8.2f} | {tot_n:>5} {tot_net:>9.2f}")
    log("")

    elig = [r for r in rows if r[2] >= 50]
    elig.sort(key=lambda r: -r[3])
    log("=== TOP-5 TRAIN (n_train>=50, by combined train net$) ===")
    for cfg, a, tn, tnet in elig[:5]:
        log(f"  P={cfg[0]} filter={cfg[1]} H={cfg[2]} S={cfg[3]}  n={tn}  net=${tnet:.2f}")
    if not elig:
        log("  NO CONFIG with n_train>=50 — falling back to max-n config")
        elig = sorted(rows, key=lambda r: (-r[2], -r[3]))
    best_cfg, _, _, _ = elig[0]
    P, f, H, S = best_cfg
    log(f"\n=== SELECTED CONFIG: P={P} filter={f} H={H}h S={S}%  (criterion: train net$, n>=50) ===\n")

    tr = results[best_cfg]
    for phase, cond in [("TRAIN", lambda t: t["train"]), ("HOLDOUT", lambda t: not t["train"])]:
        log(f"--- {phase} ---")
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

    # quarterly
    log("--- QUARTERLY (selected config, combined BTC+ETH, by entry time) ---")
    q = defaultdict(lambda: defaultdict(float)); qn = defaultdict(int)
    for s in tr:
        for t in tr[s]:
            k = qtr(t["entry_t"]); q[k][s] += t["net"]; q[k]["net"] += t["net"]; qn[k] += 1
    nonneg = 0
    for k in sorted(q):
        v = q[k]
        flag = "OK " if v["net"] >= 0 else "NEG"
        nonneg += v["net"] >= 0
        log(f"  {k}: n={qn[k]:>3}  net=${v['net']:>8.2f}  (BTC ${v.get('BTCUSDT',0):>7.2f} / ETH ${v.get('ETHUSDT',0):>7.2f})  {flag}")
    nq = len(q)
    pct_ok = 100.0 * nonneg / nq if nq else 0
    log(f"  quarters non-negative: {nonneg}/{nq} = {pct_ok:.0f}%")
    log("")

    # complementarity: BTC ret_7d < -1% at trade exit
    btc_bars = data["BTCUSDT"][0]
    bt = [b["t"] for b in btc_bars]
    def btc_ret7d(ts):
        j = bisect.bisect_right(bt, ts) - 1
        if j < 168: return None
        return btc_bars[j]["c"] / btc_bars[j - 168]["c"] - 1.0
    dd_pnl, tot_pnl, dd_n = 0.0, 0.0, 0
    for s in tr:
        for t in tr[s]:
            r7 = btc_ret7d(t["exit_t"])
            tot_pnl += t["net"]
            if r7 is not None and r7 < -0.01:
                dd_pnl += t["net"]; dd_n += 1
    log("--- COMPLEMENTARITY (BTC ret_7d < -1% at trade exit) ---")
    log(f"  trades in drawdown periods: {dd_n}, net there ${dd_pnl:.2f}; total net ${tot_pnl:.2f}")
    if tot_pnl != 0:
        log(f"  share of profit in BTC-drawdown periods: {100.0*dd_pnl/tot_pnl:.0f}%"
            + ("  (sign-caveat: total<=0)" if tot_pnl < 0 else ""))
    log("")

    # verdict
    tr_net = sum(t["net"] for s in tr for t in tr[s] if t["train"])
    ho = [t for s in tr for t in tr[s] if not t["train"]]
    ho_net = sum(t["net"] for t in ho)
    worst_tr = min((t["net"] for s in tr for t in tr[s]), default=0)
    # catastrophe: any quarter losing > 15% of notional, or single trade beyond stop+funding sanity
    worst_q = min((q[k]["net"] for k in q), default=0)
    catastrophe = worst_q < -0.15 * NOTIONAL
    if tr_net > 0 and ho_net > 0 and pct_ok >= 60 and not catastrophe:
        verdict = "PROCEED"
    elif tr_net > 0 and (ho_net > 0 or pct_ok >= 60):
        verdict = "MIXED"
    else:
        verdict = "REJECTED"
    log("=== VERDICT ===")
    log(f"  train net ${tr_net:.2f} | holdout net ${ho_net:.2f} (n={len(ho)}) | "
        f"quarters ok {pct_ok:.0f}% | worst quarter ${worst_q:.2f} | worst trade ${worst_tr:.2f}")
    log(f"  VERDICT: {verdict}")

    with open(f"{LEG2}/h3_results.log", "w") as fh:
        fh.write("\n".join(LOG) + "\n")

if __name__ == "__main__":
    main()
