import sqlite3,time,math,json,sys
sys.path.insert(0,"/app")
from services.bybit_client import bybit_client as bc
c=sqlite3.connect("/app/data/jony.db")
print("statuses:",c.execute("select status,count(*) from positions group by status").fetchall())
P=[dict(zip([d[0] for d in cur.description],r)) for cur in [c.execute("select * from positions where status!='open' order by id")] for r in cur]
print("closed n=",len(P))
# klines 1h
H=3600000
t0=min(p["opened_at_ms"] for p in P)-30*H
K={}
for coin,sym in (("ETH","ETHUSDT"),("BTC","BTCUSDT")):
    need=int((time.time()*1000-t0)/H)+5
    K[coin]=bc.get_klines(sym,"60",need)
    print(coin,"bars",len(K[coin]),time.strftime('%m-%d',time.gmtime(K[coin][0]["start_ms"]/1000)))
def bars(coin,a,b): return [k for k in K[coin] if a<=k["start_ms"]<b]
def ncdf(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def bs(S,K_,T,iv,side):
    if T<=0: return max(0,(K_-S) if side=="P" else (S-K_))
    d1=(math.log(S/K_)+0.5*iv*iv*T)/(iv*math.sqrt(T)); d2=d1-iv*math.sqrt(T)
    return K_*ncdf(-d2)-S*ncdf(-d1) if side=="P" else S*ncdf(d1)-K_*ncdf(d2)
def impl_iv(price,S,K_,T,side):
    lo,hi=0.05,3.0
    for _ in range(60):
        m=(lo+hi)/2
        if bs(S,K_,T,m,side)>price: hi=m
        else: lo=m
    return (lo+hi)/2
marks={}
for r in c.execute("select pos_id,ts_ms,mark,mark_iv from position_marks"): marks.setdefault(r[0],[]).append(r[1:])
def mark_at(p,ts,S):
    # real mark if within 10 min, else BS with entry-implied IV (or last known mark_iv)
    ms=marks.get(p["id"],[])
    near=[m for m in ms if abs(m[0]-ts)<10*60000]
    if near: return min(near,key=lambda m:abs(m[0]-ts))[1],"real"
    T=max(0,(p["expiry_ms"]-ts)/H/24/365)
    return bs(S,p["strike"],T,p["_iv"],p["side"]),"bs"
for p in P:
    T0=max(1e-6,(p["expiry_ms"]-p["opened_at_ms"])/H/24/365)
    p["_iv"]=impl_iv(p["entry_credit"],p["underlying_at_open"],p["strike"],T0,p["side"])
def run(ref_h,lo_floor,hi_floor,label):
    trig=[];tot_actual=0;tot_cf=0
    for p in P:
        coin,side=p["coin"],p["side"]; o,cl=p["opened_at_ms"],p["closed_at_ms"]
        pre=bars(coin,o-ref_h*H,o)
        if not pre: continue
        ref=min(b["low"] for b in pre) if side=="P" else max(b["high"] for b in pre)
        hit=None
        for b in bars(coin,o,cl):
            ts=b["start_ms"]+H; S=b["close"]
            if ts>=cl: break
            itm=(S<p["strike"]) if side=="P" else (S>p["strike"])
            broke=(S<ref) if side=="P" else (S>ref)
            if not(itm and broke): continue
            m,src=mark_at(p,ts,S)
            pnl_frac=(p["entry_credit"]-m)/p["entry_credit"]
            if lo_floor<=pnl_frac<=hi_floor:
                hit=(ts,S,m,src,pnl_frac,ref); break
        if hit:
            ts,S,m,src,pf,ref=hit
            cf=(p["entry_credit"]-m*1.01)*p["qty"]-p["fee_open_usd"]*2
            trig.append((p["id"],f"{coin}:{side}",p["strike"],ref,round(S),round(pf*100),src,round(p["pnl_usd"],2),round(cf,2),p["exit_reason"],round((ts-o)/H,1)))
            tot_actual+=p["pnl_usd"]; tot_cf+=cf
    print(f"\n== {label}: ref={ref_h}h corridor=[{lo_floor},{hi_floor}] triggered={len(trig)}/{len(P)} actual={tot_actual:.2f} cf={tot_cf:.2f} delta={tot_cf-tot_actual:+.2f}")
    for t in trig: print("  id",t[0],t[1],"K",t[2],"ref",t[3],"spot",t[4],f"pnl@stop {t[5]}%",t[6],"| actual",t[7],"→ cf",t[8],t[9],f"age {t[10]}h")
for ref_h in (24,48):
    run(ref_h,-0.55,-0.25,"corridor")
    run(ref_h,-9,-0.25,"no-emergency-floor (any loss ≥25%)")
    run(ref_h,-9,9,"any pnl (pure structural)")
print("\nITM losers actual:",[(p["id"],round(p["pnl_usd"],2),p["exit_reason"]) for p in P if p["pnl_usd"]<-5])
