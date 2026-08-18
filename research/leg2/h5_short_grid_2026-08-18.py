#!/usr/bin/env python3
# H5: short grid in confirmed down-regime (leg2 to Jony short-put). Chop harvest, not directional.
# Protocol: next-bar-close entries, limit adds by bar high, fees 0.075%/side, 70/30 train/holdout,
# sweep train-only, single config by train net$ (cycles>=30 + plateau over G/L/TP), one holdout look.
import json
from datetime import datetime, timezone

LEG2 = '/Users/styserg/Desktop/Jony/research/leg2'
FRESH = '/Users/styserg/Desktop/Jony/research/fresh_data'
LOG = f'{LEG2}/h5_results.log'
FEE = 0.00075
GS = [2.0, 3.0, 4.0]
LS = [3, 4, 6]
TPS = [1.5, 2.5]
BUSTS = [8.0, 12.0]
STACK = 1000.0
WARM = 600

out_lines = []
def log(s=''):
    out_lines.append(s)
    print(s)

def load(sym):
    a = json.load(open(f'{LEG2}/{sym}_1h_2022.json'))
    b = json.load(open(f'{FRESH}/{sym}_1h.json'))
    m = {}
    for r in a + b:
        m[r['start_ms']] = r
    bars = sorted(m.values(), key=lambda r: r['start_ms'])
    return bars

def ema(x, n):
    k = 2.0 / (n + 1)
    out = [None] * len(x)
    s = sum(x[:n]) / n
    out[n - 1] = s
    for i in range(n, len(x)):
        s = s + k * (x[i] - s)
        out[i] = s
    return out

def prep(bars):
    closes = [b['close'] for b in bars]
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    r7 = [None] * len(bars)
    for i in range(168, len(bars)):
        r7[i] = closes[i] / closes[i - 168] - 1.0
    gate = [False] * len(bars)
    bull = [False] * len(bars)
    for i in range(len(bars)):
        if i >= WARM and e50[i] is not None and e200[i] is not None:
            bull[i] = e50[i] > e200[i]
            if r7[i] is not None:
                gate[i] = (e50[i] < e200[i]) and (r7[i] < 0)
    return gate, bull, r7

def run(bars, gate, bull, G, L, TP, BUST):
    g = G / 100.0; tp = TP / 100.0; bust = BUST / 100.0
    B = STACK / L
    cycles = []
    n = len(bars)
    in_pos = False
    pending_entry = False
    pending_close = False
    fills = []      # (price, notional)
    qty = 0.0; sold = 0.0; fees = 0.0
    levels = []     # unfilled add prices
    open_i = 0

    def avg():
        return sold / qty

    def close_all(i, price, reason):
        nonlocal in_pos, fills, qty, sold, fees, levels, pending_close
        f = fees + FEE * qty * price
        pnl = sold - qty * price - f
        cycles.append({'open_i': open_i, 'close_i': i, 'pnl': pnl,
                       'fills': len(fills), 'reason': reason,
                       'notional': sold, 'avg': avg()})
        in_pos = False; fills = []; qty = 0.0; sold = 0.0; fees = 0.0
        levels = []; pending_close = False

    for i in range(n):
        bar = bars[i]
        if pending_entry:
            pending_entry = False
            p0 = bar['close']
            fills = [(p0, B)]; qty = B / p0; sold = B; fees = FEE * B
            levels = [p0 * (1.0 + k * g) for k in range(1, L)]
            open_i = i
            in_pos = True
            pending_close = False
            # regime-flip check at entry bar close
            if bull[i] and (sold - qty * bar['close'] - fees) < 0:
                pending_close = True
            continue

        if in_pos:
            # 1) adds first (conservative path ordering)
            added = False
            still = []
            for lv in levels:
                if bar['high'] >= lv:
                    fills.append((lv, B)); qty += B / lv; sold += B
                    fees += FEE * B; added = True
                else:
                    still.append(lv)
            levels = still
            # 2) bust at post-add avg (stop known in advance, updated on fills)
            bp = avg() * (1.0 + bust)
            if bar['high'] >= bp:
                fp = max(bp, bar['open'])
                close_all(i, fp, 'bust')
            elif not added:
                # 3) TP only if no add filled this bar (intrabar-order ambiguity -> no TP on add bars)
                tpp = avg() * (1.0 - tp)
                if bar['low'] <= tpp:
                    close_all(i, tpp, 'tp')
            if in_pos:
                if pending_close:
                    close_all(i, bar['close'], 'regime')
                elif bull[i] and (sold - qty * bar['close'] - fees) < 0:
                    pending_close = True
        else:
            if gate[i]:
                pending_entry = True

    if in_pos:
        close_all(n - 1, bars[-1]['close'], 'eod')
    return cycles

def metrics(cyc):
    if not cyc:
        return {'net': 0.0, 'n': 0, 'wr': 0.0, 'worst': 0.0, 'maxdd': 0.0}
    net = sum(c['pnl'] for c in cyc)
    wins = sum(1 for c in cyc if c['pnl'] > 0)
    worst = min(c['pnl'] for c in cyc)
    eq = 0.0; peak = 0.0; dd = 0.0
    for c in cyc:
        eq += c['pnl']
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {'net': net, 'n': len(cyc), 'wr': 100.0 * wins / len(cyc), 'worst': worst, 'maxdd': dd}

def qlabel(ms):
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return f'{d.year}Q{(d.month - 1) // 3 + 1}'

def mtm_curve(bars, gate, bull, G, L, TP, BUST):
    # rerun with mark-to-market equity tracking at closes and worst-high excursions
    g = G / 100.0; tp = TP / 100.0; bust = BUST / 100.0
    B = STACK / L
    n = len(bars)
    in_pos = False; pending_entry = False; pending_close = False
    qty = 0.0; sold = 0.0; fees = 0.0; levels = []
    realized = 0.0
    peak = 0.0; maxdd = 0.0
    for i in range(n):
        bar = bars[i]
        if pending_entry:
            pending_entry = False
            p0 = bar['close']
            qty = B / p0; sold = B; fees = FEE * B
            levels = [p0 * (1.0 + k * g) for k in range(1, L)]
            in_pos = True; pending_close = False
            if bull[i] and (sold - qty * bar['close'] - fees) < 0:
                pending_close = True
        elif in_pos:
            added = False; still = []
            for lv in levels:
                if bar['high'] >= lv:
                    qty += B / lv; sold += B; fees += FEE * B; added = True
                else:
                    still.append(lv)
            levels = still
            av = sold / qty
            bp = av * (1.0 + bust)
            if bar['high'] >= bp:
                fp = max(bp, bar['open'])
                realized += sold - qty * fp - (fees + FEE * qty * fp)
                in_pos = False; qty = 0; sold = 0; fees = 0; levels = []
            elif not added:
                tpp = av * (1.0 - tp)
                if bar['low'] <= tpp:
                    realized += sold - qty * tpp - (fees + FEE * qty * tpp)
                    in_pos = False; qty = 0; sold = 0; fees = 0; levels = []
            if in_pos:
                if pending_close:
                    realized += sold - qty * bar['close'] - (fees + FEE * qty * bar['close'])
                    in_pos = False; qty = 0; sold = 0; fees = 0; levels = []
                elif bull[i] and (sold - qty * bar['close'] - fees) < 0:
                    pending_close = True
        else:
            if gate[i]:
                pending_entry = True
        # equity at worst intrabar point (high) and at close
        if in_pos:
            eq_low = realized + (sold - qty * bar['high'] - fees)
        else:
            eq_low = realized
        peak = max(peak, realized + ((sold - qty * bar['close'] - fees) if in_pos else 0.0))
        maxdd = min(maxdd, eq_low - peak)
    return maxdd

# ---------- main ----------
log('=' * 78)
log('H5: SHORT GRID IN CONFIRMED DOWN-REGIME  (leg2, 2026-08-18)')
log('=' * 78)

data = {}
for sym in ['btc', 'eth']:
    bars = load(sym)
    gaps = sum(1 for j in range(1, len(bars)) if bars[j]['start_ms'] - bars[j - 1]['start_ms'] != 3600000)
    t0 = datetime.fromtimestamp(bars[0]['start_ms'] / 1000, tz=timezone.utc)
    t1 = datetime.fromtimestamp(bars[-1]['start_ms'] / 1000, tz=timezone.utc)
    gate, bull, r7 = prep(bars)
    data[sym] = {'bars': bars, 'gate': gate, 'bull': bull, 'r7': r7}
    log(f'{sym.upper()}: {len(bars)} bars  {t0:%Y-%m-%d} -> {t1:%Y-%m-%d}  irregular-steps={gaps}  '
        f'gate-on bars={sum(gate)} ({100*sum(gate)/len(bars):.1f}%)')

split = {s: int(len(data[s]['bars']) * 0.70) for s in data}
for s in data:
    d = datetime.fromtimestamp(data[s]['bars'][split[s]]['start_ms'] / 1000, tz=timezone.utc)
    log(f'{s.upper()} 70/30 split at bar {split[s]} = {d:%Y-%m-%d}')

# BTC ret7d lookup by ts for complementarity
btc_r7 = {data['btc']['bars'][i]['start_ms']: data['btc']['r7'][i] for i in range(len(data['btc']['bars']))}

log('')
log('SWEEP (train only): G x L x TP x BUST = 3x3x2x2 = 36 cells x 2 coins')
log(f'{"G":>4} {"L":>3} {"TP":>4} {"BUST":>5} | {"BTC net$":>9} {"nB":>4} | {"ETH net$":>9} {"nE":>4} | {"COMB $":>9} {"n":>4} {"WR%":>5}')

sweep = {}
for G in GS:
    for L in LS:
        for TP in TPS:
            for BUST in BUSTS:
                res = {}
                for s in data:
                    cyc = run(data[s]['bars'], data[s]['gate'], data[s]['bull'], G, L, TP, BUST)
                    tr = [c for c in cyc if c['open_i'] < split[s]]
                    ho = [c for c in cyc if c['open_i'] >= split[s]]
                    res[s] = {'all': cyc, 'tr': tr, 'ho': ho, 'trm': metrics(tr)}
                comb_net = res['btc']['trm']['net'] + res['eth']['trm']['net']
                comb_n = res['btc']['trm']['n'] + res['eth']['trm']['n']
                allw = sum(1 for s in data for c in res[s]['tr'] if c['pnl'] > 0)
                wr = 100.0 * allw / comb_n if comb_n else 0.0
                sweep[(G, L, TP, BUST)] = {'res': res, 'comb_net': comb_net, 'comb_n': comb_n, 'wr': wr}
                log(f'{G:>4.0f} {L:>3d} {TP:>4.1f} {BUST:>5.0f} | '
                    f'{res["btc"]["trm"]["net"]:>9.2f} {res["btc"]["trm"]["n"]:>4d} | '
                    f'{res["eth"]["trm"]["net"]:>9.2f} {res["eth"]["trm"]["n"]:>4d} | '
                    f'{comb_net:>9.2f} {comb_n:>4d} {wr:>5.1f}')

log('')
top5 = sorted(sweep.items(), key=lambda kv: -kv[1]['comb_net'])[:5]
log('TOP-5 by combined train net$:')
for k, v in top5:
    log(f'  G={k[0]:.0f} L={k[1]} TP={k[2]} BUST={k[3]:.0f}  net={v["comb_net"]:.2f}  n={v["comb_n"]}  WR={v["wr"]:.1f}%')

def neighbors(k):
    G, L, TP, BUST = k
    out = []
    for axis, vals, val in (('G', GS, G), ('L', LS, L), ('TP', TPS, TP)):
        i = vals.index(val)
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                kk = list(k)
                kk[('G', 'L', 'TP').index(axis)] = vals[j]
                out.append(tuple(kk))
    return out

log('')
log('PLATEAU CHECK (neighbors +-1 step over G, L, TP must have train net$ >= 0):')
candidates = []
for k, v in sorted(sweep.items(), key=lambda kv: -kv[1]['comb_net']):
    if v['comb_n'] < 30 or v['comb_net'] <= 0:
        continue
    nb = neighbors(k)
    bad = [(kk, sweep[kk]['comb_net']) for kk in nb if sweep[kk]['comb_net'] < 0]
    ok = not bad
    candidates.append((k, v, ok, bad))

for k, v, ok, bad in candidates[:8]:
    s = 'PLATEAU-OK' if ok else 'FAIL: ' + ', '.join(f'G={b[0][0]:.0f}/L={b[0][1]}/TP={b[0][2]}={b[1]:.0f}$' for b in bad)
    log(f'  G={k[0]:.0f} L={k[1]} TP={k[2]} BUST={k[3]:.0f} net={v["comb_net"]:.2f} n={v["comb_n"]} -> {s}')

chosen = next(((k, v) for k, v, ok, _ in candidates if ok), None)
log('')
if chosen is None:
    log('NO CONFIG passes selection gate (train net>0, cycles>=30, plateau). VERDICT: REJECTED')
    # ---- diagnostics on best train cell, TRAIN ONLY (holdout untouched: no config selected) ----
    bk, bv = top5[0]
    G, L, TP, BUST = bk
    log('')
    log(f'DIAGNOSTICS (train only) for best cell G={G:.0f} L={L} TP={TP} BUST={BUST:.0f}:')
    rc = {}
    for s in data:
        tr = bv['res'][s]['tr']
        tm = metrics(tr)
        log(f'  {s.upper()} train: net={tm["net"]:.2f}$ n={tm["n"]} WR={tm["wr"]:.1f}% worst cycle={tm["worst"]:.2f}$ ccDD={tm["maxdd"]:.2f}$')
        for c in tr:
            rc.setdefault(c['reason'], [0, 0.0])
            rc[c['reason']][0] += 1; rc[c['reason']][1] += c['pnl']
    log('  exit reasons (train, combined):')
    for r, (cnt, pnl) in sorted(rc.items()):
        log(f'    {r:>7}: {cnt:>4} cycles  {pnl:>9.2f}$  (avg {pnl/cnt:.2f}$)')
    qs = {}
    for s in data:
        for c in bv['res'][s]['tr']:
            q = qlabel(data[s]['bars'][c['close_i']]['start_ms'])
            qs.setdefault(q, 0.0); qs[q] += c['pnl']
    neg = sum(1 for q in qs if qs[q] < 0)
    log('  quarterly (train, combined): ' + '  '.join(f'{q}:{qs[q]:.0f}$' for q in sorted(qs)))
    log(f'  quarters nonneg: {len(qs)-neg}/{len(qs)}')
    tot = 0.0; down = 0.0; ndown = 0; ntot = 0
    for s in data:
        for c in bv['res'][s]['tr']:
            ts = data[s]['bars'][c['close_i']]['start_ms']
            r = btc_r7.get(ts)
            tot += c['pnl']; ntot += 1
            if r is not None and r < -0.01:
                down += c['pnl']; ndown += 1
    log(f'  complementarity: cycles closing in BTC ret7d<-1%: {ndown}/{ntot}, pnl there {down:.2f}$ of {tot:.2f}$ total')
    for s in data:
        dd = mtm_curve(data[s]['bars'], data[s]['gate'], data[s]['bull'], G, L, TP, BUST)
        log(f'  {s.upper()} MTM maxDD (full period, intrabar): {dd:.2f}$ ({100*dd/STACK:.1f}% of stack)')
else:
    k, v = chosen
    G, L, TP, BUST = k
    log(f'CHOSEN CONFIG: G={G:.0f}% L={L} TP={TP}% BUST={BUST:.0f}%  (B=${STACK/L:.0f}/level, stack=${STACK:.0f})')
    log('')
    log('ONE LOOK AT HOLDOUT:')
    allc = {}
    for s in data:
        r = v['res'][s]
        tm, hm = metrics(r['tr']), metrics(r['ho'])
        log(f'  {s.upper()} train : net={tm["net"]:>8.2f}  n={tm["n"]:>3d}  WR={tm["wr"]:.1f}%  worst={tm["worst"]:.2f}  ccDD={tm["maxdd"]:.2f}')
        log(f'  {s.upper()} holdout: net={hm["net"]:>8.2f}  n={hm["n"]:>3d}  WR={hm["wr"]:.1f}%  worst={hm["worst"]:.2f}  ccDD={hm["maxdd"]:.2f}')
        allc[s] = r['all']
    tr_net = sum(metrics(v['res'][s]['tr'])['net'] for s in data)
    ho_net = sum(metrics(v['res'][s]['ho'])['net'] for s in data)
    ho_n = sum(metrics(v['res'][s]['ho'])['n'] for s in data)
    log(f'  COMBINED train net = {tr_net:.2f}$   holdout net = {ho_net:.2f}$ (n={ho_n})')

    log('')
    log('QUARTERLY (combined, by cycle close):')
    qs = {}
    for s in data:
        for c in allc[s]:
            q = qlabel(data[s]['bars'][c['close_i']]['start_ms'])
            qs.setdefault(q, [0.0, 0])
            qs[q][0] += c['pnl']; qs[q][1] += 1
    neg = 0; worst_q = (None, 0.0)
    for q in sorted(qs):
        pnl, nn = qs[q]
        flag = '' if pnl >= 0 else '  <-- NEG'
        if pnl < 0: neg += 1
        if pnl < worst_q[1]: worst_q = (q, pnl)
        log(f'  {q}: {pnl:>8.2f}$  ({nn} cycles){flag}')
    nq = len(qs)
    pos_share = 100.0 * (nq - neg) / nq if nq else 0.0
    log(f'  quarters nonneg: {nq-neg}/{nq} = {pos_share:.0f}%   worst quarter: {worst_q[0]} {worst_q[1]:.2f}$')

    log('')
    log('COMPLEMENTARITY (BTC ret_7d < -1% at cycle close):')
    tot = 0.0; down = 0.0; ndown = 0; ntot = 0
    for s in data:
        for c in allc[s]:
            ts = data[s]['bars'][c['close_i']]['start_ms']
            r = btc_r7.get(ts)
            tot += c['pnl']; ntot += 1
            if r is not None and r < -0.01:
                down += c['pnl']; ndown += 1
    log(f'  cycles closing in BTC down-week: {ndown}/{ntot}  pnl there: {down:.2f}$ of total {tot:.2f}$ '
        f'({100*down/tot:.0f}% of net)' if tot != 0 else '  no pnl')

    log('')
    log('EXIT REASONS (full period, combined):')
    rc = {}
    for s in data:
        for c in allc[s]:
            rc.setdefault(c['reason'], [0, 0.0])
            rc[c['reason']][0] += 1; rc[c['reason']][1] += c['pnl']
    for r, (cnt, pnl) in sorted(rc.items()):
        log(f'  {r:>7}: {cnt:>4} cycles  {pnl:>9.2f}$')

    log('')
    log('MARK-TO-MARKET maxDD (intrabar worst via high, chosen config):')
    for s in data:
        dd = mtm_curve(data[s]['bars'], data[s]['gate'], data[s]['bull'], G, L, TP, BUST)
        log(f'  {s.upper()}: {dd:.2f}$  (on ${STACK:.0f} stack = {100*dd/STACK:.1f}%)')

    log('')
    verdict_ok = (tr_net > 0 and ho_net > 0 and pos_share >= 60.0 and worst_q[1] > -0.25 * STACK)
    if verdict_ok:
        log('VERDICT: PROCEED (train>0, holdout>0, plateau, quarters ok, no catastrophe quarter)')
    elif tr_net > 0 and (ho_net > 0 or pos_share >= 50.0):
        log(f'VERDICT: MIXED (train={tr_net:.0f}$, holdout={ho_net:.0f}$, quarters nonneg {pos_share:.0f}%, worst Q {worst_q[1]:.0f}$)')
    else:
        log(f'VERDICT: REJECTED (train={tr_net:.0f}$, holdout={ho_net:.0f}$, quarters nonneg {pos_share:.0f}%)')

log('')
log('ASSUMPTIONS / RISKS:')
log('- Adds are limit sells at precomputed levels, filled at level price when bar high touches (legal, no lookahead).')
log('- Intrabar order ambiguity resolved conservatively: adds fill first; TP suppressed on any bar where an add fills;')
log('  bust checked at post-add avg (worse stack). Real path could be milder or worse on gaps.')
log('- Bust fill at max(open, bust_price): gap-through-stop modeled, no extra slippage beyond that.')
log('- Fees 0.075%/side per fill; funding NOT modeled (short on average RECEIVES funding -> conservative).')
log('- Regime-flip exit executes next bar close after signal (1-bar lag).')
log('- Cycles spanning the 70/30 boundary attributed by OPEN time (minor leak; cycles are short).')
log('- eod force-close of any open position at last bar counted in PnL.')

open(LOG, 'w').write('\n'.join(out_lines) + '\n')
print(f'\nwritten: {LOG}')
