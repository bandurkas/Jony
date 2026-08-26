import sqlite3,time,math,sys
sys.path.insert(0,"/app")
from services.bybit_client import bybit_client as bc
c=sqlite3.connect("/app/data/jony.db")
P=[dict(zip([d[0] for d in cur.description],r)) for cur in [c.execute("select * from positions where status!='open' order by id")] for r in cur]
H=3600000; M5=300000
t0=min(p["opened_at_ms"] for p in P)-30*H
K={}
for coin,sym in (("ETH","ETHUSDT"),("BTC","BTCUSDT")):
    K[coin]={"60":bc.get_klines(sym,"60",int((time.time()*1000-t0)/H)+5),
             "5":bc.get_klines(sym,"5",int((time.time()*1000-t0)/M5)+5)}
    print(coin,"1h",len(K[coin]["60"]),"5m",len(K[coin]["5"]),flush=True)
def ncdf(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def bs(S,K_,T,iv,side):
    if T<=1e-9: return max(0,(K_-S) if side=="P" else (S-K_))
    d1=(math.log(S/K_)+0.5*iv*iv*T)/(iv*math.sqrt(T)); d2=d1-iv*math.sqrt(T)
    return K_*ncdf(-d2)-S*ncdf(-d1) if side=="P" else S*ncdf(d1)-K_*ncdf(d2)
def delta(S,K_,T,iv,side):
    if T<=1e-9: return (-1.0 if S<K_ else 0.0) if side=="P" else (1.0 if S>K_ else 0.0)
    d1=(math.log(S/K_)+0.5*iv*iv*T)/(iv*math.sqrt(T)); return ncdf(d1) if side=="C" else ncdf(d1)-1
def impl_iv(price,S,K_,T,side):
    lo,hi=0.05,3.0
    for _ in range(60):
        m=(lo+hi)/2
        if bs(S,K_,T,m,side)>price: hi=m
        else: lo=m
    return (lo+hi)/2
marks={}
for r in c.execute("select pos_id,ts_ms,mark from position_marks"): marks.setdefault(r[0],[]).append(r[1:])
def T_(p,ts): return max(0,(p["expiry_ms"]-ts)/H/24/365)
for p in P: p["_iv"]=impl_iv(p["entry_credit"],p["underlying_at_open"],p["strike"],max(1e-6,T_(p,p["opened_at_ms"])),p["side"])
def short_mark(p,ts,S):
    near=[m for m in marks.get(p["id"],[]) if abs(m[0]-ts)<6*60000]
    if near: return min(near,key=lambda m:abs(m[0]-ts))[1]
    return bs(S,p["strike"],T_(p,ts),p["_iv"],p["side"])
FEE=0.00055; SLIP=0.0002
base=sum(p["pnl_usd"] for p in P)
def level_for(p,kind,bars_):
    if kind=="strike": return p["strike"]
    if kind=="low24":
        pre=[b for b in K[p["coin"]]["60"] if p["opened_at_ms"]-24*H<=b["start_ms"]<p["opened_at_ms"]]
        return (min(b["low"] for b in pre) if p["side"]=="P" else max(b["high"] for b in pre)) if pre else None
    if kind=="loss20":  # price where short leg first hit -20% of credit
        for b in bars_:
            ts=b["start_ms"]+ (H if b is None else 0)
        for b in bars_:
            S=b["close"]; ts=b["start_ms"]
            if ts>=p["closed_at_ms"]: return None
            if (p["entry_credit"]-short_mark(p,ts,S))/p["entry_credit"]<=-0.20: return S
        return None
def run(kind,tf):
    step=H if tf=="60" else M5
    tot=0;n_tr=0;flips_tot=0;rows=[]
    for p in P:
        coin,side,o,cl=p["coin"],p["side"],p["opened_at_ms"],p["closed_at_ms"]
        bars_=[b for b in K[coin][tf] if o<=b["start_ms"]+step<cl]
        # bars_ close times inside the trade
        bars_=[dict(b,ts=b["start_ms"]+step) for b in bars_]
        lvl=level_for(p,kind,[dict(b,start_ms=b["ts"]) for b in bars_]) if kind=="loss20" else level_for(p,kind,None)
        if lvl is None: tot+=p["pnl_usd"];continue
        on=False;qty=0;S_on=0;hpnl=0;flips=0;start_after=None
        for b in bars_:
            S=b["close"];ts=b["ts"]
            if kind=="loss20" and S==lvl and start_after is None: start_after=ts  # arm from that moment
            if kind=="loss20" and (start_after is None or ts<start_after): continue
            bad=(S<lvl) if side=="P" else (S>lvl)
            if bad and not on:
                dl=abs(delta(S,p["strike"],T_(p,ts),p["_iv"],side))*p["qty"]
                if dl<=0: continue
                qty=dl;S_on=S*(1-SLIP) if side=="P" else S*(1+SLIP);on=True;flips+=1
                hpnl-=qty*S*FEE
            elif on and not bad:
                S_off=S*(1+SLIP) if side=="P" else S*(1-SLIP)
                hpnl+=qty*(S_on-S_off) if side=="P" else qty*(S_off-S_on)
                hpnl-=qty*S*FEE;on=False
        if on:  # close hedge at trade exit
            S_x=[b for b in K[coin]["60"] if b["start_ms"]<=cl][-1]["close"]
            hpnl+=qty*(S_on-S_x) if side=="P" else qty*(S_x-S_on); hpnl-=qty*S_x*FEE
        tot+=p["pnl_usd"]+hpnl
        if flips: n_tr+=1;flips_tot+=flips;rows.append((p["id"],round(p["pnl_usd"],1),round(p["pnl_usd"]+hpnl,1),flips))
    print(f"\n== level={kind} tf={tf}m: hedged trades {n_tr}/{len(P)}, hedge on/off cycles {flips_tot}, total={tot:.2f} delta={tot-base:+.2f}")
    print("   rows(id, actual, hedged, cycles):",rows)
print(f"base actual={base:.2f}")
for kind in ("low24","loss20","strike"):
    for tf in ("60","5"): run(kind,tf)
