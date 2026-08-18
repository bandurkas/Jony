#!/usr/bin/env python3
"""H7: post-crash rebound LONG. Event = first bar with ret_24h < -D%, 24h lockout
(identical to H6). Entry close of next bar. Sweep train only; one holdout look.
Weekly complementarity vs BTC crash weeks (weekly ret < -3%)."""
import json, statistics, bisect
from datetime import datetime, timezone, timedelta

BASE = "/Users/styserg/Desktop/Jony/research"
FEE = 0.00075          # per side
NOTIONAL = 1000.0
TRAIN_FRAC = 0.70
FUND_8H = 0.0001       # ~0.01%/8h long pays

D_AX, H_AX, S_AX = (4, 6, 8), (24, 48, 72), (3, 5, 8)
TP_AX = (None, 5)
FILT_AX = (False, True)   # True = require ret_6h >= 0 on signal bar

def load(sym):
    rows = {}
    for p in (f"{BASE}/leg2/{sym}_1h_2022.json", f"{BASE}/fresh_data/{sym}_1h.json"):
        for r in json.load(open(p)):
            rows[int(r["start_ms"])] = r
    bars = [rows[k] for k in sorted(rows)]
    ts = sorted(rows)
    gaps = sum(1 for a, b in zip(ts, ts[1:]) if b - a != 3600_000)
    return bars, gaps

def dt(ms): return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

def prep(bars):
    c = [b["close"] for b in bars]
    ret24 = [None]*len(c); ret6 = [None]*len(c)
    for i in range(24, len(c)):
        ret24[i] = c[i]/c[i-24] - 1
    for i in range(6, len(c)):
        ret6[i] = c[i]/c[i-6] - 1
    return c, ret24, ret6

def find_events(c, ret24, D, lo, hi):
    """first-bar events with 24h lockout, indices in [lo, hi) — same as H6"""
    evs, last = [], -10**9
    for i in range(max(lo, 24), hi):
        if ret24[i] is not None and ret24[i] < -D/100:
            if i - last >= 24:
                evs.append(i)
            last = i
    return evs

def run_trades(bars, c, ret24, ret6, D, H, S, TP, filt, lo, hi):
    """LONG entry close of bar i+1; hold H bars; stop -S% by low (fill stop px),
    TP +TP% by high (fill tp px); same-bar stop+TP -> stop (pessimistic)."""
    trades, i_pos_end = [], -1
    for i in find_events(c, ret24, D, lo, hi):
        if i + 1 >= hi: break
        if i + 1 <= i_pos_end: continue
        if filt and not (ret6[i] is not None and ret6[i] >= 0): continue
        ent_i = i + 1
        entry = c[ent_i]
        stop_px = entry * (1 - S/100)
        tp_px = entry * (1 + TP/100) if TP else None
        exit_i = exit_px = reason = None
        gap_opt = 0.0
        last_i = min(ent_i + H, len(bars) - 1)
        for j in range(ent_i + 1, last_i + 1):
            hit_stop = bars[j]["low"] <= stop_px
            hit_tp = tp_px is not None and bars[j]["high"] >= tp_px
            if hit_stop:
                exit_i, exit_px, reason = j, stop_px, "stop"
                if bars[j]["open"] < stop_px:   # gap: stop-px fill is optimistic
                    gap_opt = NOTIONAL * (stop_px - bars[j]["open"]) / entry
                break
            if hit_tp:
                exit_i, exit_px, reason = j, tp_px, "tp"
                break
        if exit_i is None:
            exit_i, exit_px, reason = last_i, c[last_i], "time"
        gross = NOTIONAL * (exit_px - entry) / entry
        fees = NOTIONAL * FEE + NOTIONAL * (exit_px/entry) * FEE
        trades.append({"i": ent_i, "xi": exit_i, "ms": bars[ent_i]["start_ms"],
                       "xms": bars[exit_i]["start_ms"], "hold_h": exit_i - ent_i,
                       "net": gross - fees, "reason": reason, "gap_opt": gap_opt})
        i_pos_end = exit_i
    return trades

def summ(trades):
    if not trades: return dict(n=0, net=0.0, wr=0.0, avg=0.0, worst=0.0, maxdd=0.0)
    net = sum(t["net"] for t in trades)
    wr = 100*sum(1 for t in trades if t["net"] > 0)/len(trades)
    worst = min(t["net"] for t in trades)
    eq = peak = mdd = 0.0
    for t in trades:
        eq += t["net"]; peak = max(peak, eq); mdd = min(mdd, eq - peak)
    return dict(n=len(trades), net=net, wr=wr, avg=net/len(trades), worst=worst, maxdd=mdd)

def week_key(d):
    iso = d.isocalendar()
    return (iso[0], iso[1])

def main():
    data = {}
    for sym in ("btc", "eth"):
        bars, gaps = load(sym)
        data[sym] = bars
        print(f"{sym.upper()}: {len(bars)} bars {dt(bars[0]['start_ms']):%Y-%m-%d} -> "
              f"{dt(bars[-1]['start_ms']):%Y-%m-%d %H:%M}, hour-gaps={gaps}")

    # BTC calendar-week returns (close of last bar of week vs prior week)
    btc_bars = data["btc"]
    wk_last = {}
    for b in btc_bars:
        wk_last[week_key(dt(b["start_ms"]))] = b["close"]
    wks = sorted(wk_last)
    wk_ret = {}
    for a, b in zip(wks, wks[1:]):
        wk_ret[b] = wk_last[b]/wk_last[a] - 1
    crash_wks = {w for w, r in wk_ret.items() if r < -0.03}
    print(f"BTC calendar weeks: {len(wks)}, crash weeks (wk ret < -3%): {len(crash_wks)}")

    for sym in ("btc", "eth"):
        bars = data[sym]
        c, ret24, ret6 = prep(bars)
        n = len(bars); tr_end = int(n * TRAIN_FRAC)
        split_ms = bars[tr_end]["start_ms"]
        print(f"\n===== {sym.upper()} ===== train [0,{tr_end}) end={dt(bars[tr_end-1]['start_ms']):%Y-%m-%d}, "
              f"holdout start={dt(split_ms):%Y-%m-%d}")
        for D in D_AX:
            evs = find_events(c, ret24, D, 0, tr_end)
            evf = [i for i in evs if ret6[i] is not None and ret6[i] >= 0]
            print(f"  train events D={D}: n={len(evs)} (ret6>=0 filter: {len(evf)})")

        print(f"\n--- TRAIN GRID {sym.upper()} (LONG, net$ on $1000, fees 0.15% RT) ---")
        print(f"{'D':>2} {'H':>3} {'S':>2} {'TP':>3} {'filt':>4} | {'n':>4} {'net$':>9} {'WR%':>6} "
              f"{'avg$':>7} {'worst$':>8} {'mDD$':>8}")
        grid = {}
        for D in D_AX:
            for H in H_AX:
                for S in S_AX:
                    for TP in TP_AX:
                        for filt in FILT_AX:
                            tr = run_trades(bars, c, ret24, ret6, D, H, S, TP, filt, 0, tr_end)
                            s = summ(tr)
                            grid[(D, H, S, TP, filt)] = s
                            print(f"{D:>2} {H:>3} {S:>2} {TP if TP else '--':>3} "
                                  f"{'r6+' if filt else 'off':>4} | {s['n']:>4} {s['net']:>9.2f} "
                                  f"{s['wr']:>6.1f} {s['avg']:>7.2f} {s['worst']:>8.2f} {s['maxdd']:>8.2f}")

        ranked = sorted(grid.items(), key=lambda kv: kv[1]["net"], reverse=True)
        print(f"\nTOP-5 TRAIN {sym.upper()}:")
        for k, v in ranked[:5]:
            print(f"  D={k[0]} H={k[1]} S={k[2]} TP={k[3] or '--'} filt={'r6+' if k[4] else 'off'} : "
                  f"n={v['n']} net={v['net']:.2f} WR={v['wr']:.1f} avg={v['avg']:.2f} worst={v['worst']:.2f}")

        def neighbors(k):
            D, H, S, TP, f = k; ns = []
            for ax, idx in ((D_AX, 0), (H_AX, 1), (S_AX, 2)):
                pos = ax.index(k[idx])
                for np_ in (pos-1, pos+1):
                    if 0 <= np_ < len(ax):
                        kk = list(k); kk[idx] = ax[np_]; ns.append(tuple(kk))
            return ns

        chosen = None
        for k, v in ranked:
            if v["n"] < 25 or v["net"] <= 0: continue
            nb = [grid[x]["net"] for x in neighbors(k)]
            if all(x >= 0 for x in nb):
                chosen = (k, v, nb); break
        if chosen is None:
            k, v = ranked[0]
            chosen = (k, v, [grid[x]["net"] for x in neighbors(k)])
            print(f"\nNO config passed gate (n>=25 & train>0 & ALL +/-1 neighbors in D,H,S >= 0);"
                  f" fallback = top train cell FOR REPORTING ONLY.")
            passed = False
        else:
            passed = True
        k, v, nb = chosen
        D, H, S, TP, filt = k
        print(f"\nCHOSEN {sym.upper()} (gate {'PASSED' if passed else 'FAILED'}): "
              f"D={D} H={H} S={S} TP={TP or '--'} filt={'r6+' if filt else 'off'}")
        print(f"  train: n={v['n']} net={v['net']:.2f}$ WR={v['wr']:.1f}% avg={v['avg']:.2f}$ "
              f"worst={v['worst']:.2f}$ maxDD={v['maxdd']:.2f}$")
        print(f"  plateau neighbors net$: {[round(x,1) for x in nb]}")

        allt = run_trades(bars, c, ret24, ret6, D, H, S, TP, filt, 0, n)
        tr_t = [t for t in allt if t["ms"] < split_ms]
        ho_t = [t for t in allt if t["ms"] >= split_ms]
        sh = summ(ho_t)
        print(f"  HOLDOUT (one look): n={sh['n']} net={sh['net']:.2f}$ WR={sh['wr']:.1f}% "
              f"avg={sh['avg']:.2f}$ worst={sh['worst']:.2f}$ maxDD={sh['maxdd']:.2f}$")

        # gap optimism + funding
        for label, tt in (("train", tr_t), ("holdout", ho_t)):
            gaps_n = sum(1 for t in tt if t["gap_opt"] > 0)
            gaps_usd = sum(t["gap_opt"] for t in tt)
            fund = sum(t["hold_h"]/8 * FUND_8H * NOTIONAL for t in tt)
            print(f"  {label}: stop-gap fills n={gaps_n}, optimism={gaps_usd:.2f}$ | "
                  f"funding drag ~{fund:.2f}$ -> adj net={sum(t['net'] for t in tt)-fund:.2f}$ "
                  f"(strict: {sum(t['net'] for t in tt)-fund-gaps_usd:.2f}$)")

        q = {}
        for t in allt:
            d = dt(t["xms"]); q.setdefault(f"{d.year}Q{(d.month-1)//3+1}", []).append(t["net"])
        nonneg = 0
        print(f"  QUARTERS (by exit, full period; H = holdout era):")
        for key in sorted(q):
            s = sum(q[key])
            tag = "H" if min(t["xms"] for t in allt
                             if f"{dt(t['xms']).year}Q{(dt(t['xms']).month-1)//3+1}" == key) >= split_ms else " "
            nonneg += (s >= 0)
            print(f"    {key}{tag}: n={len(q[key]):>3} net={s:>8.2f}$")
        print(f"  quarters non-negative: {nonneg}/{len(q)} = {100*nonneg/len(q):.0f}%")

        # weekly complementarity: PnL booked to exit week
        for label, tt in (("train", tr_t), ("holdout", ho_t)):
            in_c = [t for t in tt if week_key(dt(t["xms"])) in crash_wks]
            wk_hit = {week_key(dt(t["xms"])) for t in in_c}
            cw_era = {w for w in crash_wks
                      if (wk_last and ((label == "train") == (w <= week_key(dt(split_ms)))))}
            print(f"  CRASH-WEEK PnL ({label}): trades exiting in BTC<-3% weeks: n={len(in_c)}, "
                  f"net={sum(t['net'] for t in in_c):.2f}$ "
                  f"(covered {len(wk_hit)} of ~{len(cw_era)} era crash weeks)")

        print(f"  worst-5 trades (full period): " +
              ", ".join(f"{dt(t['ms']):%Y-%m-%d}:{t['net']:.0f}$({t['reason']})"
                        for t in sorted(allt, key=lambda x: x["net"])[:5]))

if __name__ == "__main__":
    main()
