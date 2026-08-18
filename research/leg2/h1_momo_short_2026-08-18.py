#!/usr/bin/env python3
"""H1: momentum-short leg on Bybit perps (BTC/ETH, 1h bars).
Protocol: signals on closed bars, execution at NEXT bar close, costs 0.075%/side,
fixed $1000 notional, 70/30 train/holdout by time, sweep on train only.
Funding NOT modeled (shorts on average RECEIVE funding -> conservative omission).
"""
import json, sys, datetime

DATA = '/Users/styserg/Desktop/Jony/research/fresh_data'
OUT_LOG = '/Users/styserg/Desktop/Jony/research/leg2/h1_results.log'
COST_SIDE = 0.00075          # 0.055% taker + 0.02% slippage, per side
NOTIONAL = 1000.0
WARMUP = 400                 # bars: covers EMA200 burn-in + 168h ret
TRAIN_FRAC = 0.70
CATASTROPHIC_Q = -200.0      # quarterly net$ below this = catastrophic (20% of notional)

_log_fh = open(OUT_LOG, 'w')
def log(*a):
    s = ' '.join(str(x) for x in a)
    print(s)
    _log_fh.write(s + '\n')
    _log_fh.flush()

def load(sym):
    bars = json.load(open(f'{DATA}/{sym}_1h.json'))
    bars.sort(key=lambda b: b['start_ms'])
    # sanity: gaps
    gaps = sum(1 for i in range(1, len(bars)) if bars[i]['start_ms'] - bars[i-1]['start_ms'] != 3600000)
    log(f'[data] {sym}: {len(bars)} bars, {gaps} gaps, '
        f'{datetime.datetime.utcfromtimestamp(bars[0]["start_ms"]/1000):%Y-%m-%d} .. '
        f'{datetime.datetime.utcfromtimestamp(bars[-1]["start_ms"]/1000):%Y-%m-%d}')
    return bars

def ema(closes, n):
    k = 2.0 / (n + 1)
    out = [closes[0]]
    for c in closes[1:]:
        out.append(c * k + out[-1] * (1 - k))
    return out

class Mkt:
    def __init__(self, sym):
        self.sym = sym
        b = load(sym)
        self.o = [x['open'] for x in b]
        self.h = [x['high'] for x in b]
        self.l = [x['low'] for x in b]
        self.c = [x['close'] for x in b]
        self.t = [x['start_ms'] for x in b]
        self.n = len(b)
        self.e50 = ema(self.c, 50)
        self.e200 = ema(self.c, 200)
        self.r7d = [None]*self.n
        self.r24 = [None]*self.n
        for i in range(self.n):
            if i >= 168: self.r7d[i] = self.c[i]/self.c[i-168] - 1
            if i >= 24:  self.r24[i] = self.c[i]/self.c[i-24] - 1

# ---- entry conditions (evaluated on CLOSED bar i, uses data <= i only) ----
def make_entry(kind, p):
    if kind == 'A':   # ret_7d < -X%
        x = -p/100.0
        return lambda m, i: m.r7d[i] is not None and m.r7d[i] < x
    else:             # B: EMA50<EMA200 AND ret_24h < -Y%
        y = -p/100.0
        return lambda m, i: m.e50[i] < m.e200[i] and m.r24[i] is not None and m.r24[i] < y

ENTRIES = [('A1','A',1),('A2','A',2),('A3','A',3),('A5','A',5),
           ('B0','B',0),('B1','B',1),('B2','B',2)]

# exit spec: dict(style, S, T, Z, H, cond_exit)
CORE_EXITS = [
    ('F_3_5',  dict(style='F',  S=3, T=5, H=168)),
    ('F_5_8',  dict(style='F',  S=5, T=8, H=168)),
    ('TR_3',   dict(style='TR', Z=3,       H=168)),
    ('TC_72',  dict(style='TC',            H=72)),
]
COND_EXIT = ('COND_S5', dict(style='COND', S=5, H=168))
COND_ENTRIES = {'A2','A3','B1'}   # condition-off exit only for these (keeps grid ~60)

def run_short(m, entry_fn, ex):
    """Short-only backtest. Signal at close of bar i -> enter at close[i+1].
    Exits checked intrabar from bar entry_i+1 onward; worst-case ordering (stop first).
    Returns list of trades: (entry_i, exit_i, pnl$, sig_r7d)."""
    trades = []
    i = WARMUP
    n = m.n
    while i < n - 1:
        if not entry_fn(m, i):
            i += 1; continue
        ei = i + 1                      # execution bar (enter at its close)
        ep = m.c[ei]
        stop = ep * (1 + ex.get('S', 0)/100.0) if ex['style'] in ('F','COND') else None
        take = ep * (1 - ex.get('T', 0)/100.0) if ex['style'] == 'F' else None
        z = ex.get('Z')
        min_low = ep                    # trailing anchor (data known at entry)
        cap_i = ei + ex['H']
        pending_cond = False
        xi, xp = None, None
        j = ei + 1
        while j < n:
            # 1) intrabar protective exits, worst case first (short: stop above)
            if stop is not None:
                if m.o[j] >= stop: xi, xp = j, m.o[j]; break     # gap through stop
                if m.h[j] >= stop: xi, xp = j, stop; break
            if z is not None:
                tstop = min_low * (1 + z/100.0)                  # from PRIOR bars only
                if m.o[j] >= tstop: xi, xp = j, m.o[j]; break
                if m.h[j] >= tstop: xi, xp = j, tstop; break
            if take is not None:
                if m.o[j] <= take: xi, xp = j, m.o[j]; break
                if m.l[j] <= take: xi, xp = j, take; break
            # 2) close-based exits
            if pending_cond:            # condition vanished at close of j-1 -> exit at close[j]
                xi, xp = j, m.c[j]; break
            if j >= cap_i:
                xi, xp = j, m.c[j]; break
            if ex['style'] == 'COND' and not entry_fn(m, j):
                pending_cond = True
            if z is not None and m.l[j] < min_low:
                min_low = m.l[j]        # update trail AFTER checks (no same-bar lookahead)
            j += 1
        if xi is None:                  # end of data: force close at last bar close
            xi, xp = n - 1, m.c[n - 1]
        pnl = NOTIONAL * (ep - xp) / ep - NOTIONAL * 2 * COST_SIDE
        trades.append((ei, xi, pnl, m.r7d[i]))
        i = xi                          # flat again; may signal on exit bar close
    return trades

def run_long_mirror(m, name, kind, p, ex):
    """Symmetric long in uptrend (mirror). For the optional separate line only."""
    if kind == 'A':
        x = p/100.0
        entry_fn = lambda m, i: m.r7d[i] is not None and m.r7d[i] > x
    else:
        y = p/100.0
        entry_fn = lambda m, i: m.e50[i] > m.e200[i] and m.r24[i] is not None and m.r24[i] > y
    trades = []
    i = WARMUP; n = m.n
    while i < n - 1:
        if not entry_fn(m, i):
            i += 1; continue
        ei = i + 1; ep = m.c[ei]
        stop = ep * (1 - ex.get('S', 0)/100.0) if ex['style'] in ('F','COND') else None
        take = ep * (1 + ex.get('T', 0)/100.0) if ex['style'] == 'F' else None
        z = ex.get('Z'); max_high = ep
        cap_i = ei + ex['H']; pending_cond = False
        xi, xp = None, None
        j = ei + 1
        while j < n:
            if stop is not None:
                if m.o[j] <= stop: xi, xp = j, m.o[j]; break
                if m.l[j] <= stop: xi, xp = j, stop; break
            if z is not None:
                tstop = max_high * (1 - z/100.0)
                if m.o[j] <= tstop: xi, xp = j, m.o[j]; break
                if m.l[j] <= tstop: xi, xp = j, tstop; break
            if take is not None:
                if m.o[j] >= take: xi, xp = j, m.o[j]; break
                if m.h[j] >= take: xi, xp = j, take; break
            if pending_cond: xi, xp = j, m.c[j]; break
            if j >= cap_i: xi, xp = j, m.c[j]; break
            if ex['style'] == 'COND' and not entry_fn(m, j): pending_cond = True
            if z is not None and m.h[j] > max_high: max_high = m.h[j]
            j += 1
        if xi is None: xi, xp = n - 1, m.c[n - 1]
        pnl = NOTIONAL * (xp - ep) / ep - NOTIONAL * 2 * COST_SIDE
        trades.append((ei, xi, pnl, m.r7d[i]))
        i = xi
    return trades

def agg(trades, lo=None, hi=None):
    """Filter by entry bar index, return (n, net$, wins)."""
    sel = [t for t in trades if (lo is None or t[0] >= lo) and (hi is None or t[0] < hi)]
    net = sum(t[2] for t in sel)
    wins = sum(1 for t in sel if t[2] > 0)
    return len(sel), net, wins

def quarter(ms):
    d = datetime.datetime.utcfromtimestamp(ms/1000)
    return f'{d.year}Q{(d.month-1)//3+1}'

def main():
    mkts = {s: Mkt(s) for s in ('btc', 'eth')}
    n = mkts['btc'].n
    split = int(n * TRAIN_FRAC)
    ts = mkts['btc'].t
    log(f'[split] bar {split}/{n}  train ends {datetime.datetime.utcfromtimestamp(ts[split]/1000):%Y-%m-%d %H:%M} UTC')
    log(f'[costs] {COST_SIDE*100:.3f}%/side ({2*COST_SIDE*100:.2f}% RT), notional ${NOTIONAL:.0f}, funding NOT modeled (short receives on avg -> conservative)')

    # ---- build grid ----
    grid = []
    for sym in ('btc', 'eth'):
        for ename, kind, p in ENTRIES:
            for xname, ex in CORE_EXITS:
                grid.append((sym, ename, kind, p, xname, ex))
            if ename in COND_ENTRIES:
                grid.append((sym, ename, kind, p, COND_EXIT[0], COND_EXIT[1]))
    log(f'[grid] {len(grid)} configs (sweep on TRAIN only)')

    results = []
    for sym, ename, kind, p, xname, ex in grid:
        m = mkts[sym]
        trades = run_short(m, make_entry(kind, p), ex)
        ntr, net, wins = agg(trades, hi=split)
        results.append(dict(sym=sym, entry=ename, exit=xname, ex=ex, kind=kind, p=p,
                            trades=trades, n_train=ntr, train_net=net,
                            train_wr=(wins/ntr*100 if ntr else 0)))

    # ---- train table (sorted) ----
    results.sort(key=lambda r: -r['train_net'])
    log('\n=== TRAIN sweep (top 15 of grid, by train net$) ===')
    log(f'{"cfg":<24}{"n_tr":>6}{"WR%":>7}{"net$":>10}')
    for r in results[:15]:
        log(f'{r["sym"]}/{r["entry"]}/{r["exit"]:<12}{r["n_train"]:>6}{r["train_wr"]:>7.1f}{r["train_net"]:>10.2f}')
    log('\n--- bottom 5 ---')
    for r in results[-5:]:
        log(f'{r["sym"]}/{r["entry"]}/{r["exit"]:<12}{r["n_train"]:>6}{r["train_wr"]:>7.1f}{r["train_net"]:>10.2f}')

    # ---- selection: best train net$ with n_train >= 100 ----
    eligible = [r for r in results if r['n_train'] >= 100]
    flag = ''
    if eligible:
        chosen = max(eligible, key=lambda r: r['train_net'])
    else:
        chosen = max((r for r in results if r['n_train'] >= 30), key=lambda r: r['train_net'], default=results[0])
        flag = ' [FLAG: no config reached n_train>=100; relaxed to n>=30 — small sample]'
    log('\n=== TOP-5 TRAIN (eligible n_train>=100)' + (' — NONE eligible' if not eligible else '') + ' ===')
    for r in (eligible if eligible else results)[:5]:
        log(f'{r["sym"]}/{r["entry"]}/{r["exit"]:<12} n={r["n_train"]:>4} WR={r["train_wr"]:.1f}% net=${r["train_net"]:.2f}')
    log(f'\n[SELECTED] {chosen["sym"]}/{chosen["entry"]}/{chosen["exit"]}{flag}')
    log(f'  entry: {"ret_7d < -%d%%" % chosen["p"] if chosen["kind"]=="A" else "EMA50<EMA200 & ret_24h < -%d%%" % chosen["p"]}')
    log(f'  exit : {chosen["ex"]}')

    # ---- single holdout run for chosen (no re-selection permitted) ----
    m = mkts[chosen['sym']]
    tr = chosen['trades']
    n_h, net_h, w_h = agg(tr, lo=split)
    n_t, net_t, w_t = chosen['n_train'], chosen['train_net'], None
    log('\n=== CHOSEN CONFIG: train vs holdout ===')
    log(f'  TRAIN  : n={n_t:>4}  net=${net_t:>9.2f}  WR={chosen["train_wr"]:.1f}%')
    log(f'  HOLDOUT: n={n_h:>4}  net=${net_h:>9.2f}  WR={(w_h/n_h*100 if n_h else 0):.1f}%')

    # ---- quarterly breakdown, full history (attributed by exit time) ----
    qs = {}
    for ei, xi, pnl, r7 in tr:
        q = quarter(m.t[xi])
        qs.setdefault(q, [0, 0.0])
        qs[q][0] += 1; qs[q][1] += pnl
    log('\n=== Quarterly net$ (full history, chosen config) ===')
    qkeys = sorted(qs)
    nonneg = 0; catastrophic = []
    for q in qkeys:
        cnt, net = qs[q]
        mark = ''
        if net >= 0: nonneg += 1
        if net < CATASTROPHIC_Q: catastrophic.append(q); mark = '  <-- CATASTROPHIC'
        log(f'  {q}: n={cnt:>3}  net=${net:>9.2f}{mark}')
    pct_nonneg = nonneg / len(qkeys) * 100 if qkeys else 0
    log(f'  -> {nonneg}/{len(qkeys)} quarters non-negative ({pct_nonneg:.0f}%), catastrophic(<{CATASTROPHIC_Q}$): {catastrophic or "none"}')

    # ---- complementarity: share of profit when ret_7d < -1% (Jony puts off/suffering) ----
    log('\n=== Complementarity vs Jony (periods with ret_7d < -1% at entry signal) ===')
    for label, lo, hi in (('full', None, None), ('holdout', split, None)):
        sel = [t for t in tr if (lo is None or t[0] >= lo)]
        tot = sum(t[2] for t in sel)
        dn = [t for t in sel if t[3] is not None and t[3] < -0.01]
        dn_pnl = sum(t[2] for t in dn)
        share = (dn_pnl / tot * 100) if tot > 0 else float('nan')
        log(f'  [{label}] total net=${tot:.2f}; net in ret7d<-1% regime=${dn_pnl:.2f} '
            f'({len(dn)}/{len(sel)} trades); share of profit={share:.1f}%'
            + ('' if tot > 0 else ' [total<=0, share undefined]'))

    # ---- optional: symmetric long mirror (separate line, NOT part of selection) ----
    lt = run_long_mirror(m, chosen['entry'], chosen['kind'], chosen['p'], chosen['ex'])
    ln_t, lnet_t, _ = agg(lt, hi=split)
    ln_h, lnet_h, _ = agg(lt, lo=split)
    log(f'\n[optional] symmetric LONG mirror ({chosen["sym"]}): train n={ln_t} net=${lnet_t:.2f}; holdout n={ln_h} net=${lnet_h:.2f}')

    # ---- verdict ----
    ok_train = net_t > 0
    ok_hold = net_h > 0
    ok_q = pct_nonneg >= 60
    ok_cat = not catastrophic
    if ok_train and ok_hold and ok_q and ok_cat:
        verdict = 'PROCEED'
    elif ok_train and (ok_hold or ok_q):
        verdict = 'MIXED'
    else:
        verdict = 'REJECTED'
    log('\n=== VERDICT ===')
    log(f'  train>0: {ok_train}  holdout>0: {ok_hold}  quarters>=60% nonneg: {ok_q} ({pct_nonneg:.0f}%)  no catastrophic Q: {ok_cat}')
    log(f'  --> {verdict}{flag}')

    # dump chosen trades for audit
    with open('/Users/styserg/Desktop/Jony/research/leg2/chosen_trades.json', 'w') as f:
        json.dump([dict(entry_ts=m.t[a], exit_ts=m.t[b], pnl=round(p, 2),
                        sig_ret7d=(round(r, 4) if r is not None else None),
                        seg=('train' if a < split else 'holdout'))
                   for a, b, p, r in tr], f, indent=1)
    log('\n[artifacts] leg2/h1_results.log, leg2/chosen_trades.json')

if __name__ == '__main__':
    main()
