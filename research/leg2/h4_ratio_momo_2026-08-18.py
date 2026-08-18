#!/usr/bin/env python3
"""H4: ETH/BTC ratio momentum (trend-following on log ratio), market-neutral pair.
Protocol: signal on closed bar, fill next bar close; fees 0.075%/side x 2 legs;
funding NOT modeled; 70/30 train/holdout by time; sweep train-only; one holdout look."""
import json, math, datetime, sys

LEG2 = "/Users/styserg/Desktop/Jony/research/leg2"
FRESH = "/Users/styserg/Desktop/Jony/research/fresh_data"
OUT = f"{LEG2}/h4_results.log"

NOTIONAL_LEG = 1000.0          # $/leg
FEE = 0.00075                  # per side per leg
STOP_BASE = 1000.0             # S% of $1000 base (~= S% adverse ratio move)

def load_merge(paths):
    seen = {}
    for p in paths:
        with open(p) as f:
            for b in json.load(f):
                seen[b["start_ms"]] = b
    return dict(sorted(seen.items()))

def utc(ms): return datetime.datetime.fromtimestamp(ms/1000, datetime.timezone.utc)

def run_config(env, L, T, S, cap_h, i_lo, i_hi, short_only=False):
    """entries allowed at signal bars in [i_lo, i_hi); force-exit at close[i_hi] (or last bar)."""
    xs, ec, bc, ts, n = env["x"], env["ec"], env["bc"], env["ts"], env["n"]
    stop_dollars = S * STOP_BASE
    i_end = min(i_hi, n - 1)
    trades = []
    pos = None
    i = max(i_lo, L)
    while i <= i_end:
        r = xs[i] - xs[i-L] if i >= L else 0.0
        if pos is None:
            if i < i_hi and abs(r) > T and (not short_only or r < 0) and i + 1 <= i_end:
                side = 1 if r > 0 else -1          # +1: long ETH / short BTC
                j = i + 1
                pos = dict(side=side, i0=j,
                           qe=NOTIONAL_LEG/ec[j], qb=NOTIONAL_LEG/bc[j],
                           pe=ec[j], pb=bc[j])
                i = j + 1
                continue
        else:
            side, qe, qb = pos["side"], pos["qe"], pos["qb"]
            pnl = side * (qe*(ec[i]-pos["pe"]) - qb*(bc[i]-pos["pb"]))
            reason = None
            if pnl <= -stop_dollars: reason = "stop"
            elif (r > 0) != (side > 0) and r != 0: reason = "flip"
            elif i - pos["i0"] >= cap_h: reason = "timecap"
            elif i == i_end: reason = "eos"
            if reason:
                j = min(i + 1, i_end) if reason != "eos" else i_end
                gross = side * (qe*(ec[j]-pos["pe"]) - qb*(bc[j]-pos["pb"]))
                fees = FEE*2*NOTIONAL_LEG + FEE*(qe*ec[j] + qb*bc[j])
                trades.append(dict(net=gross-fees, side=side, i_sig=pos["i0"]-1,
                                   i_in=pos["i0"], i_out=j, reason=reason,
                                   hold=j-pos["i0"]))
                pos = None
                i = j + 1
                continue
        i += 1
    return trades

def stats(tr):
    if not tr: return dict(net=0.0, n=0, wr=0.0, avg=0.0, mdd=0.0)
    net = sum(t["net"] for t in tr)
    eq = mx = 0.0; mdd = 0.0
    for t in tr:
        eq += t["net"]; mx = max(mx, eq); mdd = max(mdd, mx - eq)
    w = sum(1 for t in tr if t["net"] > 0)
    return dict(net=net, n=len(tr), wr=100*w/len(tr), avg=net/len(tr), mdd=mdd)

def main():
    out = []
    def w(s=""): out.append(s); print(s)
    w(f"H4 ETH/BTC ratio momentum backtest  run {datetime.datetime.now(datetime.timezone.utc).isoformat()}")

    btc = load_merge([f"{LEG2}/btc_1h_2022.json", f"{FRESH}/btc_1h.json"])
    eth = load_merge([f"{LEG2}/eth_1h_2022.json", f"{FRESH}/eth_1h.json"])
    keys = sorted(set(btc) & set(eth))
    ts = keys
    bc = [btc[k]["close"] for k in keys]
    ec = [eth[k]["close"] for k in keys]
    n = len(keys)
    gaps = sum(1 for a, b in zip(keys, keys[1:]) if b - a != 3600_000)
    w(f"bars joined={n}  span {utc(keys[0])} .. {utc(keys[-1])}  non-1h gaps={gaps}")
    xs = [math.log(e/b) for e, b in zip(ec, bc)]
    env = dict(x=xs, ec=ec, bc=bc, ts=ts, n=n)

    split = int(n * 0.70)
    w(f"train entries bars [0,{split}) = .. {utc(keys[split-1])};  holdout [{split},{n}) = {utc(keys[split])} ..")
    w(f"costs: {FEE*100}%/side/leg (~0.3% RT pair); stop = S% of ${STOP_BASE:.0f} base (~S% adverse ratio move); funding NOT modeled")

    # grid: full LxTxS at cap=720h(30d); cap=336h(14d) slice at S=3%; short-only slice at S=3%, cap=720
    Ls, Ts, Ss = [168, 336, 720], [0.02, 0.04, 0.06], [0.02, 0.03, 0.05]
    grid = []
    for L in Ls:
        for T in Ts:
            for S in Ss:
                grid.append((L, T, S, 720, False))
    for L in Ls:
        for T in Ts:
            grid.append((L, T, 0.03, 336, False))
    for L in Ls:
        for T in Ts:
            grid.append((L, T, 0.03, 720, True))
    w(f"grid cells: {len(grid)} (27 both-side cap30d + 9 both-side cap14d(S=3%) + 9 short-ETH-only cap30d(S=3%))")

    res = {}
    w("\n=== SWEEP (train only, net $ per $1000/leg pair) ===")
    w("   L   T%   S%  cap  side |      net$    n   wr%    avg$   maxDD$")
    for g in grid:
        L, T, S, cap, so = g
        tr = run_config(env, L, T, S, cap, 0, split, so)
        st = stats(tr)
        res[g] = st
        w(f" {L:>4} {T*100:>4.0f} {S*100:>4.0f} {cap//24:>4} {'shrt' if so else 'both'} | {st['net']:>9.2f} {st['n']:>4} {st['wr']:>5.1f} {st['avg']:>7.2f} {st['mdd']:>8.2f}")

    def neighbors(g):
        L, T, S, cap, so = g
        nb = []
        for dim, vals, v in (("L", Ls, L), ("T", Ts, T), ("S", Ss, S)):
            k = vals.index(v)
            for kk in (k-1, k+1):
                if 0 <= kk < len(vals):
                    gg = (vals[kk] if dim=="L" else L, vals[kk] if dim=="T" else T,
                          vals[kk] if dim=="S" else S, cap, so)
                    if gg in res: nb.append(gg)
        alt_cap = 336 if cap == 720 else 720
        gg = (L, T, S, alt_cap, so)
        if gg in res: nb.append(gg)
        return nb

    ranked = sorted(res.items(), key=lambda kv: -kv[1]["net"])
    w("\nTop-5 train:")
    for g, st in ranked[:5]:
        L, T, S, cap, so = g
        w(f"  L={L} T={T*100:.0f}% S={S*100:.0f}% cap={cap//24}d {'short-only' if so else 'both'}: net=${st['net']:.2f} n={st['n']} wr={st['wr']:.1f}% mdd=${st['mdd']:.2f}")

    chosen = None; chosen_note = ""
    for g, st in ranked:
        if st["n"] < 30: continue
        nb = neighbors(g)
        plateau = all(res[x]["net"] >= 0 for x in nb)
        if chosen is None and plateau:
            chosen, chosen_note = g, f"plateau OK ({len(nb)} neighbors all >=0)"
            break
    if chosen is None:
        # no config passes plateau: report best n>=30 by net$, verdict will fail plateau
        for g, st in ranked:
            if st["n"] >= 30:
                chosen, chosen_note = g, "PLATEAU FAILED (no n>=30 config with all-nonneg neighbors)"
                break
    L, T, S, cap, so = chosen
    nb = neighbors(chosen)
    w(f"\nSELECTED: L={L} T={T*100:.0f}% S={S*100:.0f}% cap={cap//24}d {'short-only' if so else 'both'}  [{chosen_note}]")
    w("neighbors (train net$): " + ", ".join(
        f"L{x[0]}/T{x[1]*100:.0f}/S{x[2]*100:.0f}/c{x[3]//24}{'s' if x[4] else ''}={res[x]['net']:.0f}" for x in nb))
    plateau_ok = all(res[x]["net"] >= 0 for x in nb)

    # one holdout look
    tr_tr = run_config(env, L, T, S, cap, 0, split, so)
    tr_ho = run_config(env, L, T, S, cap, split, n, so)
    st_tr, st_ho = stats(tr_tr), stats(tr_ho)
    w("\n=== SELECTED CONFIG: TRAIN vs HOLDOUT ===")
    for name, st in (("train", st_tr), ("holdout", st_ho)):
        w(f"{name:7s}: net=${st['net']:.2f}  n={st['n']}  wr={st['wr']:.1f}%  avg=${st['avg']:.2f}  maxDD=${st['mdd']:.2f}")
    allt = tr_tr + tr_ho
    reasons = {}
    for t in allt: reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    holds = sorted(t["hold"] for t in allt)
    w(f"exit reasons (all): {reasons}")
    w(f"median hold (h): {holds[len(holds)//2] if holds else 0}")
    sides = {1: 0, -1: 0}
    for t in allt: sides[t["side"]] += 1
    w(f"sides: long-ETH={sides[1]}  short-ETH={sides[-1]}")

    w("\n=== QUARTERLY (selected config, PnL by exit quarter; T=train H=holdout) ===")
    qs = {}
    for t in allt:
        d = utc(ts[t["i_out"]])
        q = f"{d.year}Q{(d.month-1)//3+1}"
        seg = "T" if t["i_sig"] < split else "H"
        e = qs.setdefault(q, [0.0, 0, set()])
        e[0] += t["net"]; e[1] += 1; e[2].add(seg)
    nn = 0
    for q in sorted(qs):
        netq, nq, segs = qs[q]
        w(f"  {q} [{'/'.join(sorted(segs))}]: net=${netq:>8.2f}  n={nq}")
        if netq >= 0: nn += 1
    qshare = nn / len(qs) if qs else 0
    w(f"quarters non-negative: {nn}/{len(qs)} = {qshare*100:.0f}%")

    # complementarity: BTC ret_7d at signal bar
    b7 = [None]*n
    for i in range(168, n): b7[i] = bc[i]/bc[i-168] - 1
    w("\n=== COMPLEMENTARITY (BTC ret_7d at entry signal) ===")
    stress_bars = sum(1 for v in b7 if v is not None and v < -0.01)
    w(f"share of bars with BTC ret_7d < -1%: {100*stress_bars/(n-168):.1f}%")
    for label, trs in (("full", allt), ("holdout", tr_ho)):
        tot = sum(t["net"] for t in trs)
        s1 = [t for t in trs if b7[t["i_sig"]] is not None and b7[t["i_sig"]] < -0.01]
        s5 = [t for t in trs if b7[t["i_sig"]] is not None and b7[t["i_sig"]] < -0.05]
        p1, p5 = sum(t["net"] for t in s1), sum(t["net"] for t in s5)
        share = f"{100*p1/tot:.0f}%" if tot > 0 else "undef (total<=0)"
        w(f"{label}: total=${tot:.2f} | stress<-1%: ${p1:.2f} ({len(s1)} tr, share={share}) | deep<-5%: ${p5:.2f} ({len(s5)} tr)")

    w("\n=== VERDICT ===")
    cat = st_tr["mdd"] > 500 or st_ho["mdd"] > 500
    cond = dict(train_pos=st_tr["net"] > 0, holdout_pos=st_ho["net"] > 0,
                plateau=plateau_ok, quarters=qshare >= 0.60, no_catastrophe=not cat)
    w("  ".join(f"{k}={v}" for k, v in cond.items()))
    if all(cond.values()): verdict = "PROCEED"
    elif cond["train_pos"] and cond["holdout_pos"]: verdict = "MIXED"
    else: verdict = "REJECTED"   # no out-of-sample confirmation => rejected
    w(f"VERDICT: {verdict}")
    w("\nAssumptions/risks: funding not modeled (perp pair carries net funding, often adverse on short-ETH leg);")
    w("stop checked on closes only (no intrabar); stop base=$1000 (~S% ratio move); no slippage beyond fees;")
    w("fills at next close; holdout looked at exactly once; T thresholds applied to log-ratio return (~pct);")
    w("NOTE: short-only cells exist only at S=3%/cap=30d, so plateau check there spans L,T neighbors only (thin).")

    with open(OUT, "w") as f: f.write("\n".join(out) + "\n")

if __name__ == "__main__":
    main()
