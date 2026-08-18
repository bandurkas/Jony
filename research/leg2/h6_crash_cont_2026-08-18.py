#!/usr/bin/env python3
"""H6: event-driven crash-continuation short. Event = ret_24h < -D%.
Event study first (train only), then trading grid sweep on train, one holdout look."""
import json, statistics, sys
from datetime import datetime, timezone

BASE = "/Users/styserg/Desktop/Jony/research"
FEE = 0.00075          # per side
NOTIONAL = 1000.0
TRAIN_FRAC = 0.70

def load(sym):
    rows = {}
    for p in (f"{BASE}/leg2/{sym}_1h_2022.json", f"{BASE}/fresh_data/{sym}_1h.json"):
        for r in json.load(open(p)):
            rows[int(r["start_ms"])] = r
    bars = [rows[k] for k in sorted(rows)]
    ts = sorted(rows)
    gaps = sum(1 for a, b in zip(ts, ts[1:]) if b - a != 3600_000)
    return bars, gaps

def ema(vals, n):
    k = 2 / (n + 1); out = [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out

def dt(ms): return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

def prep(bars):
    c = [b["close"] for b in bars]
    e50, e200 = ema(c, 50), ema(c, 200)
    ret24 = [None]*len(c)
    for i in range(24, len(c)):
        ret24[i] = c[i]/c[i-24] - 1
    return c, e50, e200, ret24

def find_events(c, ret24, D, lo, hi):
    """first-bar events with 24h lockout, indices in [lo, hi)"""
    evs, last = [], -10**9
    for i in range(max(lo, 24), hi):
        if ret24[i] is not None and ret24[i] < -D/100:
            if i - last >= 24:
                evs.append(i)
            last = i  # any qualifying bar extends the same-slide lockout
    return evs

def event_study(sym, bars, c, ret24, tr_end):
    print(f"\n--- EVENT STUDY {sym.upper()} (train only, bars 24..{tr_end}) ---")
    print(f"{'D%':>3} {'n':>4} | " + " | ".join(f"{'+%dh'%h:>22}" for h in (6,12,24,48,72)))
    print(f"{'':>10} | " + " | ".join(f"{'mean%':>7}{'med%':>7}{'neg%':>7} " for _ in range(5)))
    for D in (4, 6, 8):
        evs = find_events(c, ret24, D, 0, tr_end)
        row = f"{D:>3} {len(evs):>4} | "
        cells = []
        for h in (6, 12, 24, 48, 72):
            fw = [c[i+h]/c[i]-1 for i in evs if i+h < tr_end]
            if not fw:
                cells.append(f"{'--':>22}"); continue
            mean = statistics.mean(fw)*100; med = statistics.median(fw)*100
            neg = 100*sum(1 for x in fw if x < 0)/len(fw)
            cells.append(f"{mean:>7.2f}{med:>7.2f}{neg:>6.0f}% ")
        print(row + " | ".join(cells))

def run_trades(bars, c, e50, e200, ret24, D, H, S, filt, lo, hi):
    """short entry close of bar i+1; hold H; stop +S% checked from bar i+2 highs."""
    trades, i_pos_end = [], -1
    evs = find_events(c, ret24, D, lo, hi)
    for i in evs:
        if i + 1 >= hi: break
        if i + 1 <= i_pos_end: continue
        if filt and not (e50[i] < e200[i]): continue
        ent_i = i + 1
        entry = c[ent_i]
        stop_px = entry * (1 + S/100)
        exit_i, exit_px, reason = None, None, None
        last_i = min(ent_i + H, len(bars) - 1)
        for j in range(ent_i + 1, last_i + 1):
            if bars[j]["high"] >= stop_px:
                exit_i, exit_px, reason = j, stop_px, "stop"
                break
        if exit_i is None:
            exit_i, exit_px, reason = last_i, c[last_i], "time"
        gross = NOTIONAL * (entry - exit_px) / entry
        fees = NOTIONAL * FEE + NOTIONAL * (exit_px/entry) * FEE
        trades.append({"i": ent_i, "xi": exit_i, "ms": bars[ent_i]["start_ms"],
                       "net": gross - fees, "reason": reason})
        i_pos_end = exit_i
    return trades

def summ(trades):
    if not trades: return (0, 0.0, 0.0, 0.0)
    net = sum(t["net"] for t in trades)
    wr = 100*sum(1 for t in trades if t["net"] > 0)/len(trades)
    avg = net/len(trades)
    return (len(trades), net, wr, avg)

def main():
    out = {}
    for sym in ("btc", "eth"):
        bars, gaps = load(sym)
        out[sym] = bars
        print(f"{sym.upper()}: {len(bars)} bars {dt(bars[0]['start_ms']):%Y-%m-%d} -> "
              f"{dt(bars[-1]['start_ms']):%Y-%m-%d %H:%M}, hour-gaps={gaps}")
    btc_bars = out["btc"]
    # BTC ret_7d lookup for complementarity
    btc_c = {b["start_ms"]: b["close"] for b in btc_bars}
    btc_sorted = sorted(btc_c)
    import bisect
    def btc_ret7d(ms):
        j = bisect.bisect_right(btc_sorted, ms) - 1
        if j < 168: return None
        return btc_c[btc_sorted[j]]/btc_c[btc_sorted[j-168]] - 1

    for sym in ("btc", "eth"):
        bars = out[sym]
        c, e50, e200, ret24 = prep(bars)
        n = len(bars); tr_end = int(n * TRAIN_FRAC)
        print(f"\n===== {sym.upper()} ===== train bars [0,{tr_end}) end={dt(bars[tr_end-1]['start_ms']):%Y-%m-%d}, "
              f"holdout [{tr_end},{n}) start={dt(bars[tr_end]['start_ms']):%Y-%m-%d}")
        event_study(sym, bars, c, ret24, tr_end)

        print(f"\n--- TRAIN GRID {sym.upper()} (net$ on $1000, fees 0.15% RT) ---")
        print(f"{'D':>2} {'H':>3} {'S':>2} {'filt':>4} | {'n':>4} {'net$':>9} {'WR%':>6} {'avg$':>7}")
        grid = {}
        for D in (4, 6, 8):
            for H in (12, 24, 48):
                for S in (2, 3, 5):
                    for filt in (False, True):
                        tr = run_trades(bars, c, e50, e200, ret24, D, H, S, filt, 0, tr_end)
                        nT, net, wr, avg = summ(tr)
                        grid[(D, H, S, filt)] = (nT, net, wr, avg)
                        print(f"{D:>2} {H:>3} {S:>2} {'EMA' if filt else 'off':>4} | {nT:>4} {net:>9.2f} {wr:>6.1f} {avg:>7.2f}")

        ranked = sorted(grid.items(), key=lambda kv: kv[1][1], reverse=True)
        print(f"\nTOP-5 TRAIN {sym.upper()}:")
        for k, v in ranked[:5]:
            print(f"  D={k[0]} H={k[1]} S={k[2]} filt={'EMA' if k[3] else 'off'} : n={v[0]} net={v[1]:.2f} WR={v[2]:.1f} avg={v[3]:.2f}")

        # pick: best train net$ with n>=25 and plateau (median neighbor net > 0)
        def neighbors(k):
            D, H, S, f = k; ns = []
            for dD in (4,6,8):
                if dD != D: ns.append((dD,H,S,f))
            for dH in (12,24,48):
                if dH != H: ns.append((D,dH,S,f))
            for dS in (2,3,5):
                if dS != S: ns.append((D,H,dS,f))
            ns.append((D,H,S,not f))
            return [x for x in ns if x in grid]
        chosen = None
        for k, v in ranked:
            if v[0] < 25: continue
            nb = [grid[x][1] for x in neighbors(k)]
            med_nb = statistics.median(nb) if nb else -1
            pos_nb = sum(1 for x in nb if x > 0)/len(nb) if nb else 0
            if v[1] > 0 and med_nb > 0:
                chosen = (k, v, med_nb, pos_nb); break
        if chosen is None:
            k, v = ranked[0]
            nb = [grid[x][1] for x in neighbors(k)]
            chosen = (k, v, statistics.median(nb) if nb else 0,
                      (sum(1 for x in nb if x > 0)/len(nb)) if nb else 0)
            print(f"\nNO config passed (n>=25 & train>0 & neighbor-median>0); fallback = top train cell for reporting.")
        k, v, med_nb, pos_nb = chosen
        D, H, S, filt = k
        print(f"\nCHOSEN {sym.upper()}: D={D} H={H} S={S} filt={'EMA' if filt else 'off'} | "
              f"train n={v[0]} (SMALL SAMPLE)" if v[0] < 50 else
              f"\nCHOSEN {sym.upper()}: D={D} H={H} S={S} filt={'EMA' if filt else 'off'} | train n={v[0]}")
        print(f"  train net={v[1]:.2f}$ WR={v[2]:.1f}% avg={v[3]:.2f}$ | neighbors: median_net={med_nb:.2f}$ share_pos={pos_nb*100:.0f}%")

        ho = run_trades(bars, c, e50, e200, ret24, D, H, S, filt, tr_end, n)
        nH, netH, wrH, avgH = summ(ho)
        print(f"  HOLDOUT: n={nH} net={netH:.2f}$ WR={wrH:.1f}% avg={avgH:.2f}$")

        allt = run_trades(bars, c, e50, e200, ret24, D, H, S, filt, 0, n)
        q = {}
        for t in allt:
            d = dt(t["ms"]); key = f"{d.year}Q{(d.month-1)//3+1}"
            q.setdefault(key, []).append(t["net"])
        print(f"  QUARTERS (full period, chosen config; H=holdout-era):")
        ho_start_ms = bars[tr_end]["start_ms"]
        nonneg = 0
        for key in sorted(q):
            s = sum(q[key]); tag = "H" if min(t["ms"] for t in allt if f"{dt(t['ms']).year}Q{(dt(t['ms']).month-1)//3+1}"==key) >= ho_start_ms else " "
            nonneg += (s >= 0)
            print(f"    {key}{tag}: n={len(q[key]):>3} net={s:>8.2f}$")
        print(f"  quarters non-negative: {nonneg}/{len(q)} = {100*nonneg/len(q):.0f}%")

        prof_down = sum(t["net"] for t in allt if (btc_ret7d(t["ms"]) or 0) < -0.01)
        tot = sum(t["net"] for t in allt)
        n_down = sum(1 for t in allt if (btc_ret7d(t["ms"]) or 0) < -0.01)
        print(f"  COMPLEMENTARITY: {n_down}/{len(allt)} trades with BTC ret_7d<-1%; net in those = {prof_down:.2f}$ (total {tot:.2f}$)")

if __name__ == "__main__":
    main()
