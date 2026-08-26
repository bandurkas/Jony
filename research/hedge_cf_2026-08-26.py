import sqlite3,time,math,sys
sys.path.insert(0,"/app")
from services.bybit_client import bybit_client as bc
c=sqlite3.connect("/app/data/jony.db")
P=[dict(zip([d[0] for d in cur.description],r)) for cur in [c.execute("select * from positions where status!='open' order by id")] for r in cur]
H=3600000
t0=min(p["opened_at_ms"] for p in P)-30*H
K={}
for coin,sym in (("ETH","ETHUSDT"),("BTC","BTCUSDT")):
    K[coin]=bc.get_klines(sym,"60",int((time.time()*1000-t0)/H)+5)
def bars(coin,a,b): return [k for k in K[coin] if a<=k["start_ms"]<b]
def spot_at(coin,ts):
    b=[k for k in K[coin] if k["start_ms"]<=ts]; return b[-1]["close"] if b else None
def ncdf(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def bs(S,K_,T,iv,side):
    if T<=1e-9: return max(0,(K_-S) if side=="P" else (S-K_))
    d1=(math.log(S/K_)+0.5*iv*iv*T)/(iv*math.sqrt(T)); d2=d1-iv*math.sqrt(T)
    return K_*ncdf(-d2)-S*ncdf(-d1) if side=="P" else S*ncdf(d1)-K_*ncdf(d2)
def delta(S,K_,T,iv,side):
    if T<=1e-9: return 0.0
    d1=(math.log(S/K_)+0.5*iv*iv*T)/(iv*math.sqrt(T)); return ncdf(d1) if side=="C" else ncdf(d1)-1
def impl_iv(price,S,K_,T,side):
    lo,hi=0.05,3.0
    for _ in range(60):
        m=(lo+hi)/2
        if bs(S,K_,T,m,side)>price: hi=m
        else: lo=m
    return (lo+hi)/2
marks={}
for r in c.execute("select pos_id,ts_ms,mark,mark_iv,underlying from position_marks"): marks.setdefault(r[0],[]).append(r[1:])
def T_(p,ts): return max(0,(p["expiry_ms"]-ts)/H/24/365)
for p in P:
    p["_iv"]=impl_iv(p["entry_credit"],p["underlying_at_open"],p["strike"],max(1e-6,T_(p,p["opened_at_ms"])),p["side"])
    p["_S_exit"]=spot_at(p["coin"],p["closed_at_ms"])
def short_mark(p,ts,S):
    near=[m for m in marks.get(p["id"],[]) if abs(m[0]-ts)<10*60000]
    if near: return min(near,key=lambda m:abs(m[0]-ts))[1]
    return bs(S,p["strike"],T_(p,ts),p["_iv"],p["side"])
SKEW=0.05  # +5 vol pts for OTM wing (conservative)
base=sum(p["pnl_usd"] for p in P)
print(f"n={len(P)} actual total={base:.2f}  (sum credits ${sum(p['entry_credit']*p['qty'] for p in P):.0f})")
def wing_K(p,S,w): return S*(1-w) if p["side"]=="P" else S*(1+w)
def run_entry_wing(w,skew):
    tot=0;cost=0;worst=[]
    for p in P:
        S0=p["underlying_at_open"];Kw=wing_K(p,p["strike"],w)
        buy=bs(S0,Kw,T_(p,p["opened_at_ms"]),p["_iv"]+skew,p["side"])*1.02
        sell=bs(p["_S_exit"],Kw,T_(p,p["closed_at_ms"]),p["_iv"]+skew,p["side"])*0.98
        d=(sell-buy)*p["qty"]; tot+=p["pnl_usd"]+d; cost+=buy*p["qty"]
        worst.append((p["pnl_usd"]+d,p["pnl_usd"],p["id"]))
    worst.sort()
    print(f"  entry wing {w*100:.1f}% skew+{skew*100:.0f}: total={tot:.2f} delta={tot-base:+.2f} wing cost paid=${cost:.2f} ({cost/sum(p['entry_credit']*p['qty'] for p in P)*100:.0f}% of credits) worst3={[(round(a,1),round(b,1),i) for a,b,i in worst[:3]]}")
def run_trigger(w,trig,mode):
    tot=0;n=0;rows=[]
    for p in P:
        coin,o,cl=p["coin"],p["opened_at_ms"],p["closed_at_ms"];hit=None
        for b in bars(coin,o,cl):
            ts=b["start_ms"]+H;S=b["close"]
            if ts>=cl: break
            m=short_mark(p,ts,S); pf=(p["entry_credit"]-m)/p["entry_credit"]
            if pf<=trig: hit=(ts,S,m);break
        if not hit: tot+=p["pnl_usd"];continue
        ts,S,m=hit;n+=1;iv=p["_iv"]+SKEW
        if mode=="wing":
            Kw=wing_K(p,S,w); buy=bs(S,Kw,T_(p,ts),iv,p["side"])*1.02
            sell=bs(p["_S_exit"],Kw,T_(p,p["closed_at_ms"]),iv,p["side"])*0.98
            d=(sell-buy)*p["qty"]
        else: # static delta hedge with perp, held to exit
            dl=delta(S,p["strike"],T_(p,ts),p["_iv"],p["side"])  # short option delta = -dl
            d=(-dl)*p["qty"]*(p["_S_exit"]-S)*-1  # hedge = opposite of short-option delta
            d-=abs(dl)*p["qty"]*S*0.0006*2  # taker fees
        tot+=p["pnl_usd"]+d; rows.append((p["id"],round(p["pnl_usd"],1),round(p["pnl_usd"]+d,1)))
    print(f"  trigger {trig*100:.0f}% {mode}{' w='+str(w) if mode=='wing' else ''}: fired {n}/{len(P)} total={tot:.2f} delta={tot-base:+.2f} rows={rows}")
print("== A. protective wing bought at entry, held to exit")
for w in (0.05,0.075,0.10):
    for skew in (0.0,0.05): run_entry_wing(w,skew)
print("== B. wing bought when short leg hits trigger loss")
for trig in (-0.20,-0.40):
    for w in (0.03,0.05): run_trigger(w,trig,"wing")
print("== C. static delta hedge (perp) at trigger, held to exit")
for trig in (-0.20,-0.40): run_trigger(0,trig,"perp")
