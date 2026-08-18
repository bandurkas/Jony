#!/usr/bin/env python3
# H2: market-neutral mean-reversion ETH/BTC pair on Bybit perps, 1h bars.
# Protocol: signal on closed bar t (rolling stats use bars <= t), execution at close of t+1.
# Costs: 0.075%/side/leg -> $3.0 per $1000/leg round trip of the pair. Funding not modeled.
import json, math, datetime as dt
import numpy as np

DATA = "/Users/styserg/Desktop/Jony/research/fresh_data"
OUT  = "/Users/styserg/Desktop/Jony/research/leg2/h2_results.log"

NOTIONAL = 1000.0
FEE_SIDE = 0.00075                      # taker+slip per side per leg
RT_COST  = FEE_SIDE * 2 * 2 * NOTIONAL  # $3.00 per pair round trip
TIME_CAP = 21 * 24                      # bars
WARMUP   = 720                          # common warmup for all N
TRAIN_FRAC = 0.70

N_GRID     = [168, 336, 720]
ZIN_GRID   = [1.5, 2.0, 2.5]
ZSTOP_GRID = [3.5, 4.5]

log_lines = []
def log(s=""):
    print(s)
    log_lines.append(s)

def load(sym):
    with open(f"{DATA}/{sym}_1h.json") as f:
        rows = json.load(f)
    return {r["start_ms"]: r["close"] for r in rows}

eth = load("eth"); btc = load("btc")
ts = sorted(set(eth) & set(btc))
ec = np.array([eth[t] for t in ts]); bc = np.array([btc[t] for t in ts])
ts = np.array(ts)
n = len(ts)
gaps = np.diff(ts)
n_gaps = int((gaps != 3600000).sum())
x = np.log(ec / bc)

def d(ms): return dt.datetime.fromtimestamp(ms/1000, dt.timezone.utc)

split = int(n * TRAIN_FRAC)   # bars [WARMUP, split) = train entries; [split, n) = holdout entries
log(f"H2 ETH/BTC pair mean-reversion backtest  run {dt.datetime.now(dt.timezone.utc).isoformat()}")
log(f"bars joined={n}  span {d(ts[0])} .. {d(ts[-1])}  non-1h gaps={n_gaps}")
log(f"train entries bars [{WARMUP},{split}) = {d(ts[WARMUP])} .. {d(ts[split-1])}")
log(f"holdout entries bars [{split},{n}) = {d(ts[split])} .. {d(ts[-1])}")
log(f"costs: {FEE_SIDE*100:.3f}%/side/leg -> ${RT_COST:.2f}/round-trip pair; funding NOT modeled")

# ---------- diagnostics on train ----------
log("\n=== DIAGNOSTICS (ratio x = log ETH/BTC) ===")
xt = x[:split]
# AR(1) on train: x_t = a + b*x_{t-1}
X0, X1 = xt[:-1], xt[1:]
b_ar = np.cov(X0, X1)[0,1] / np.var(X0)
hl = -math.log(2)/math.log(b_ar) if 0 < b_ar < 1 else float("inf")
log(f"AR(1) phi (train, 1h) = {b_ar:.6f}  -> half-life = {hl:.0f} h = {hl/24:.0f} days")

# ADF (train, constant, lag 1) on x
def adf_stat(y, lags=1):
    dy = np.diff(y)
    ylag = y[lags:-1]
    rows = [np.ones_like(ylag), ylag]
    for i in range(1, lags+1):
        rows.append(dy[lags-i:len(dy)-i])
    Z = np.column_stack(rows)
    dep = dy[lags:]
    beta, res, *_ = np.linalg.lstsq(Z, dep, rcond=None)
    resid = dep - Z @ beta
    s2 = resid @ resid / (len(dep) - Z.shape[1])
    se = np.sqrt(s2 * np.linalg.inv(Z.T @ Z)[1,1])
    return beta[1] / se
log(f"ADF t-stat (train, const, 1 lag) = {adf_stat(xt):.2f}   (5% crit ~ -2.86; > crit => NOT stationary)")

log("ratio ETH/BTC by half-year (mean, subperiod trend):")
step = n // 4
for i in range(4):
    a, bnd = i*step, min((i+1)*step, n)
    seg = ec[a:bnd]/bc[a:bnd]
    log(f"  {d(ts[a]).date()}..{d(ts[bnd-1]).date()}: mean={seg.mean():.5f}  first={seg[0]:.5f} last={seg[-1]:.5f}  chg={(seg[-1]/seg[0]-1)*100:+.1f}%")
log(f"full period ratio: {ec[0]/bc[0]:.5f} -> {ec[-1]/bc[-1]:.5f}  ({(ec[-1]/bc[-1])/(ec[0]/bc[0])*100-100:+.1f}%)")

# ---------- z-scores per N (past-only incl. current closed bar) ----------
def roll_stats(arr, N):
    c = np.concatenate([[0.0], np.cumsum(arr)])
    c2 = np.concatenate([[0.0], np.cumsum(arr*arr)])
    m = np.full_like(arr, np.nan); s = np.full_like(arr, np.nan)
    idx = np.arange(N-1, len(arr))
    m[idx] = (c[idx+1] - c[idx+1-N]) / N
    var = (c2[idx+1] - c2[idx+1-N]) / N - m[idx]**2
    s[idx] = np.sqrt(np.maximum(var, 1e-18))
    return m, s

Z = {}
for N in N_GRID:
    m, s = roll_stats(x, N)
    Z[N] = (x - m) / s

# ---------- backtest ----------
def run(N, z_in, z_stop, lo, hi):
    """Entries allowed for signal bars t in [lo, hi); execution at t+1; force-close at bar hi (or n-1)."""
    z = Z[N]
    trades = []
    pos = 0          # +1 = long spread (long ETH/short BTC), -1 = short spread
    blocked = 0      # after stop/time-cap: no re-entry same dir until |z| < z_in
    entry_i = None
    last = min(hi, n-1)
    t = lo
    while t <= last:
        zt = z[t]
        if pos == 0:
            if blocked != 0 and abs(zt) < z_in:
                blocked = 0
            if t < hi and not np.isnan(zt) and abs(zt) > z_in:
                dr = -1 if zt > 0 else 1
                if dr != blocked:
                    pos, entry_i = dr, t+1   # executed at close[t+1]
                    blocked = 0
                    t += 1
                    continue
        else:
            reason = None
            if (pos == -1 and zt <= 0) or (pos == 1 and zt >= 0): reason = "cross0"
            elif abs(zt) > z_stop:                                 reason = "stop"
            elif t - entry_i >= TIME_CAP:                          reason = "timecap"
            elif t == last:                                        reason = "forced"
            if reason:
                xi = t+1 if (reason != "forced" and t+1 <= last) else t
                pnl = pos*NOTIONAL*(ec[xi]/ec[entry_i]-1) - pos*NOTIONAL*(bc[xi]/bc[entry_i]-1) - RT_COST
                trades.append(dict(ei=entry_i, xi=xi, dir=pos, pnl=pnl, reason=reason,
                                   hold=xi-entry_i))
                if reason in ("stop", "timecap"):
                    blocked = pos
                pos = 0; entry_i = None
        t += 1
    return trades

def stats(trades):
    if not trades: return dict(net=0.0, ntr=0, wr=0.0, avg=0.0, mdd=0.0)
    p = np.array([t["pnl"] for t in trades])
    eq = np.cumsum(p)
    mdd = float((np.maximum.accumulate(eq) - eq).max())
    return dict(net=float(p.sum()), ntr=len(p), wr=float((p>0).mean()*100),
                avg=float(p.mean()), mdd=mdd)

# ---------- sweep on train ----------
log("\n=== SWEEP (train only, net $ per $1000/leg) ===")
log(f"{'N':>4} {'z_in':>5} {'z_stop':>6} | {'net$':>9} {'n':>4} {'wr%':>5} {'avg$':>7} {'maxDD$':>8}")
results = []
for N in N_GRID:
    for zi in ZIN_GRID:
        for zs in ZSTOP_GRID:
            tr = run(N, zi, zs, WARMUP, split)
            st = stats(tr)
            results.append((N, zi, zs, st, tr))
            log(f"{N:>4} {zi:>5} {zs:>6} | {st['net']:>9.2f} {st['ntr']:>4} {st['wr']:>5.1f} {st['avg']:>7.2f} {st['mdd']:>8.2f}")

elig = [r for r in results if r[3]["ntr"] >= 30]
pool = elig if elig else results
if not elig:
    log("\nWARNING: no config reached n_train >= 30; selecting from all (flagged).")
pool.sort(key=lambda r: r[3]["net"], reverse=True)
log("\nTop-5 train:")
for N, zi, zs, st, _ in pool[:5]:
    log(f"  N={N} z_in={zi} z_stop={zs}: net=${st['net']:.2f} n={st['ntr']} wr={st['wr']:.1f}% avg=${st['avg']:.2f} mdd=${st['mdd']:.2f}")

N_s, zi_s, zs_s, st_tr, tr_train = pool[0]
log(f"\nSELECTED (by train net$, n>=30): N={N_s} z_in={zi_s} z_stop={zs_s}")

# ---------- holdout (single run) ----------
tr_hold = run(N_s, zi_s, zs_s, split, n-1)
st_ho = stats(tr_hold)
log("\n=== SELECTED CONFIG: TRAIN vs HOLDOUT ===")
log(f"train  : net=${st_tr['net']:.2f}  n={st_tr['ntr']}  wr={st_tr['wr']:.1f}%  avg=${st_tr['avg']:.2f}  maxDD=${st_tr['mdd']:.2f}")
log(f"holdout: net=${st_ho['net']:.2f}  n={st_ho['ntr']}  wr={st_ho['wr']:.1f}%  avg=${st_ho['avg']:.2f}  maxDD=${st_ho['mdd']:.2f}")

all_tr = tr_train + tr_hold
for t_ in all_tr:
    t_["exit_dt"] = d(ts[t_["xi"]])
reasons = {}
for t_ in all_tr: reasons[t_["reason"]] = reasons.get(t_["reason"], 0) + 1
log(f"exit reasons (all): {reasons}")
log(f"median hold (h): {np.median([t_['hold'] for t_ in all_tr]):.0f}")

# ---------- quarterly ----------
log("\n=== QUARTERLY (selected config, PnL by exit quarter; T=train H=holdout) ===")
q = {}
for t_ in all_tr:
    key = (t_["exit_dt"].year, (t_["exit_dt"].month-1)//3 + 1)
    q.setdefault(key, []).append(t_)
qpos = 0
for key in sorted(q):
    p = sum(t_["pnl"] for t_ in q[key])
    seg = "T" if all(t_["xi"] < split for t_ in q[key]) else ("H" if all(t_["xi"] >= split for t_ in q[key]) else "T/H")
    if p >= 0: qpos += 1
    log(f"  {key[0]}Q{key[1]} [{seg}]: net=${p:>8.2f}  n={len(q[key])}")
log(f"quarters non-negative: {qpos}/{len(q)} = {qpos/len(q)*100:.0f}%")

# ---------- complementarity ----------
ret7 = np.full(n, np.nan)
ret7[168:] = bc[168:]/bc[:-168] - 1
def compl(trades, label):
    if not trades: log(f"{label}: no trades"); return
    p_all = sum(t_["pnl"] for t_ in trades)
    dn = [t_ for t_ in trades if not np.isnan(ret7[t_["xi"]]) and ret7[t_["xi"]] < -0.01]
    p_dn = sum(t_["pnl"] for t_ in dn)
    share = p_dn/p_all*100 if p_all > 0 else float("nan")
    log(f"{label}: PnL in BTC-stress (ret_7d<-1% at exit) = ${p_dn:.2f} over {len(dn)} trades; "
        f"total=${p_all:.2f}; stress share of profit = {share:.0f}%" if p_all>0 else
        f"{label}: PnL in BTC-stress = ${p_dn:.2f} ({len(dn)} trades); total=${p_all:.2f} (<=0, share undefined)")
log("\n=== COMPLEMENTARITY (BTC ret_7d < -1%) ===")
stress_bars = float(np.nanmean(ret7 < -0.01))*100
log(f"share of bars in BTC-stress regime: {stress_bars:.1f}%")
compl(all_tr, "full period")
compl(tr_hold, "holdout")

# ---------- verdict ----------
log("\n=== VERDICT ===")
cat = st_tr["mdd"] > 500 or st_ho["mdd"] > 500   # catastrophe: DD > 50% of one leg's notional
ok_q = qpos/len(q) >= 0.60 if q else False
if st_tr["net"] > 0 and st_ho["net"] > 0 and ok_q and not cat:
    verdict = "PROCEED"
elif st_tr["net"] > 0 or st_ho["net"] > 0:
    verdict = "MIXED"
else:
    verdict = "REJECTED"
log(f"train>0: {st_tr['net']>0}  holdout>0: {st_ho['net']>0}  quarters>=60% non-neg: {ok_q}  catastrophe(DD>$500): {cat}")
log(f"VERDICT: {verdict}")

with open(OUT, "w") as f:
    f.write("\n".join(log_lines) + "\n")
print(f"\nsaved -> {OUT}")
