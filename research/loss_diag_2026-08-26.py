import sqlite3,json,time,sys,statistics as st
sys.path.insert(0,"/app")
from services.bybit_client import bybit_client as bc
c=sqlite3.connect("/app/data/jony.db")
P=[dict(zip([d[0] for d in cur.description],r)) for cur in [c.execute("select * from positions where status!='open' order by id")] for r in cur]
H=3600000
K={coin:bc.get_klines(sym,"60",1300) for coin,sym in (("ETH","ETHUSDT"),("BTC","BTCUSDT"))}
def agg(name,groups):
    print(f"\n== {name}")
    for g,rows in sorted(groups.items(),key=lambda kv:str(kv[0])):
        if not rows: continue
        pn=[r["pnl_usd"] for r in rows]
        print(f"  {str(g):28s} n={len(rows):2d} sum={sum(pn):7.2f} avg={st.mean(pn):6.2f} WR={sum(p>0 for p in pn)/len(pn)*100:4.0f}% worst={min(pn):6.2f}")
for p in P:
    sp=json.loads(p["signal_payload"] or "{}"); p["r7"]=sp.get("ret_7d"); p["vp"]=sp.get("vol_pctile"); p["reg"]=sp.get("regime")
    o=p["opened_at_ms"]; pre=[b for b in K[p["coin"]] if o-7*24*H<=b["start_ms"]<o]
    hi=max(b["high"] for b in pre) if pre else None; lo=min(b["low"] for b in pre) if pre else None
    S=p["underlying_at_open"]; p["d_hi"]=(S/hi-1)*100 if hi else None; p["d_lo"]=(S/lo-1)*100 if lo else None
    p["age"]=(p["closed_at_ms"]-o)/H; p["key"]=f'{p["coin"]}:{p["side"]}'
    p["ym"]=time.strftime("%m-%d",time.gmtime(o/1000))[:5]
def bucket(v,edges):
    if v is None: return "na"
    for e in edges:
        if v<e: return f"<{e}"
    return f">={edges[-1]}"
G=lambda f:{}
def group(f):
    d={}
    for p in P: d.setdefault(f(p),[]).append(p)
    return d
agg("by exit reason",group(lambda p:p["exit_reason"]))
agg("by key",group(lambda p:p["key"]))
agg("by ret_7d at entry",group(lambda p:bucket(p["r7"],[0,5,10,20])))
agg("PUTS: distance from 7d HIGH at entry (%)",{k:v for k,v in group(lambda p:bucket(p["d_hi"],[-8,-4,-1.5]) if p["side"]=="P" else "call").items() if k!="call"})
agg("CALLS: distance from 7d LOW at entry (%)",{k:v for k,v in group(lambda p:bucket(p["d_lo"],[1.5,4,8]) if p["side"]=="C" else "put").items() if k!="put"})
agg("by vol_pctile",group(lambda p:bucket(p["vp"],[0.5,0.7,0.85])))
agg("by regime",group(lambda p:p["reg"]))
agg("by age at close (h)",group(lambda p:bucket(p["age"],[6,24,48,96])))
agg("by week",group(lambda p:p["ym"]))
agg("peak_profit reached (of credit)",group(lambda p:bucket(p["peak_profit_pct"],[0.1,0.2,0.4,0.7])))
losers=[p for p in P if p["pnl_usd"]<-2]
print("\n== losers < -$2:")
for p in losers: print(f'  #{p["id"]} {p["key"]} K{p["strike"]} r7={p["r7"]} d_hi={p["d_hi"] and round(p["d_hi"],1)} vp={p["vp"]} reg={p["reg"]} age={p["age"]:.0f}h peak={p["peak_profit_pct"] and round(p["peak_profit_pct"]*100)}% exit={p["exit_reason"]} pnl={p["pnl_usd"]:.2f}')
print("\n== same-day clusters (positions opened within 2h of each other):")
cl=[];cur=[P[0]]
for a,b in zip(P,P[1:]):
    if b["opened_at_ms"]-a["opened_at_ms"]<2*H and b["key"]==a["key"]: cur.append(b)
    else: cl.append(cur);cur=[b]
cl.append(cur)
for g in cl:
    if len(g)>1: print("  ids",[p["id"] for p in g],g[0]["key"],"sum",round(sum(p["pnl_usd"] for p in g),2))
