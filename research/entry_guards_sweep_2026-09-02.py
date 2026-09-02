"""Entry guards sweep on the HONEST engine (2026-09-02), research-only.
R2: never sell a put ITM at entry (strike > entry_spot from ATM rounding).
R3: book guard — no new short put while an open short put is ITM (needs entry_ok_fn in replay_v2).
Engine: ETH:P+BTC:P, CALIB, feature_lag, sigma_path beta=0.2; replay_v2 pkc=1, $1500.
Selection bar (fixed BEFORE the run): ho_ret >= base AND ho_dd <= base AND tr_ret >= 0.9*base_tr
AND no quarter worse than base by > 2 pp.
"""
from __future__ import annotations
import json, pickle, sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc, jony_engine as je
from replay_account_v2 import replay_v2

HERE = Path(__file__).resolve().parent
RES = HERE / "results"
PKL = RES / "entry_guards_trades_2026-09-02.pkl"
OUT = RES / "entry_guards_sweep_2026-09-02.json"
MO, CAP, PKC, EQ = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP, 1, 1500
EXPECT = dict(n=4560, tr=9.7, tr_dd=23.3, ho=16.6, ho_dd=11.1, full=24.7, full_dd=29.4, q=[8.7, 22.9, -14.9, 20.3])


def gen():
    if PKL.exists():
        return pickle.loads(PKL.read_bytes())
    t0 = time.time(); out = []
    for coin in ("ETH", "BTC"):
        out += je.coin_trades(coin, sides_enabled=("P",), sigma_calib=jc.CALIB, feature_lag=True, sigma_path=True, sigma_path_beta=0.2)
    out.sort(key=lambda t: t["entry_ts"])
    PKL.write_bytes(pickle.dumps(out)); print(f"generated {len(out)} trades in {time.time()-t0:.0f}s", flush=True)
    return out


def rp(T, smf=None, eok=None):
    return replay_v2(T, MO, CAP, per_key_cap=PKC, start_equity=EQ, size_mult_fn=smf, entry_ok_fn=eok)


def evaluate(T, smf=None, eok=None):
    f = rp(T, smf, eok)
    tr, ho = je.split(T, 0.70)
    a, b = rp(tr, smf, eok), rp(ho, smf, eok)
    q = [round(float(rp(x, smf, eok)["return_pct"]), 1) for x in je.quarters(T) if x]
    return dict(n=len(T), taken=f["n_taken"], tr=round(a["return_pct"], 1), tr_dd=round(a["max_dd"], 1),
                ho=round(b["return_pct"], 1), ho_dd=round(b["max_dd"], 1),
                full=round(f["return_pct"], 1), full_dd=round(f["max_dd"], 1), q=q,
                n_skipped_guard=f.get("n_skipped_guard", 0))


def passes(m, base):
    return (m["ho"] >= base["ho"] and m["ho_dd"] <= base["ho_dd"] and m["tr"] >= 0.9 * base["tr"]
            and all(mq >= bq - 2.0 for mq, bq in zip(m["q"], base["q"])))


# ---------- R2 helpers ----------
def mny(t):  # strike vs spot, in bp (positive = strike above spot = ITM for a put)
    return (t["strike"] / t["entry_spot"] - 1) * 1e4

def itm(t, tol=0.0):
    return t["side"] == "P" and t["strike"] > t["entry_spot"] * (1 - tol)

def F(pred): return lambda T: [t for t in T if not pred(t)]
def S(pred, m=0.5): return lambda t: m if pred(t) else 1.0

def desc_stats(T):
    rows = []
    for coin in ("ETH", "BTC", "ALL"):
        sub = [t for t in T if coin == "ALL" or t["coin"] == coin]
        for label, pred in (("ITM(strike>spot)", lambda t: itm(t)), ("barely_OTM(0..25bp)", lambda t: not itm(t) and itm(t, 0.0025)),
                            ("OTM(>25bp)", lambda t: not itm(t, 0.0025))):
            g = [t for t in sub if pred(t)]
            if not g:
                rows.append(dict(coin=coin, group=label, n=0)); continue
            pnl = [jc.fixed_lot_pnl(t) for t in g]
            rows.append(dict(coin=coin, group=label, n=len(g), share=round(len(g) / len(sub) * 100, 1),
                             avg_lot_pnl=round(float(np.mean(pnl)), 3), sum_lot_pnl=round(float(np.sum(pnl)), 1),
                             win_rate=round(float(np.mean([p > 0 for p in pnl])) * 100, 1),
                             avg_pnl_pct=round(float(np.mean([t["pnl_pct"] for t in g])) * 100, 2),
                             avg_mny_bp=round(float(np.mean([mny(t) for t in g])), 1)))
    return rows


# ---------- R3 helpers ----------
class Spot:
    def __init__(self):
        self.d = {}
        for coin in ("ETH", "BTC"):
            k = je.load_klines(coin, "5m")
            self.d[coin] = (k["start_ms"].values.astype(np.int64), k["close"].values.astype(float))
    def at(self, coin, ts):
        s, c = self.d[coin]
        i = np.searchsorted(s, ts, side="right") - 1
        return float(c[max(i, 0)])

def book_guard(spot: Spot, scope="any", mult=0.0, depth=0.0, counter=None):
    def fn(t, open_positions, now):
        if t["side"] != "P":
            return 1.0
        for p in open_positions:
            if p["side"] != "P" or (scope == "same" and p["coin"] != t["coin"]):
                continue
            if spot.at(p["coin"], now) < p["strike"] * (1 - depth):
                if counter is not None: counter[0] += 1
                return mult
        return 1.0
    return fn


def main():
    T = gen(); print("honest candidates", len(T), flush=True)
    base = evaluate(T)
    fid = {k: base[k] for k in EXPECT}
    if fid != EXPECT:
        print("FIDELITY FAIL", fid, "expected", EXPECT); OUT.write_text(json.dumps({"fidelity": "FAIL", "got": fid, "expect": EXPECT}, indent=1)); sys.exit(1)
    # non-breaking check of the entry_ok_fn patch: no-op guard must reproduce baseline exactly
    noop = evaluate(T, eok=lambda t, o, n: 1.0)
    assert {k: noop[k] for k in EXPECT} == EXPECT and noop["taken"] == base["taken"], noop
    print("fidelity OK; entry_ok_fn no-op identical", flush=True)

    desc = desc_stats(T)
    print("\n-- R2 descriptive (fixed-lot pnl in USD, 1 lot) --")
    for r in desc:
        print(f"{r['coin']:4s} {r['group']:20s} n={r['n']:4d} share={r.get('share',0):5.1f}% avg_lot_pnl={r.get('avg_lot_pnl',0):+7.3f} sum={r.get('sum_lot_pnl',0):+8.1f} "
              f"WR={r.get('win_rate',0):5.1f}% avg_pnl%={r.get('avg_pnl_pct',0):+6.2f} mny={r.get('avg_mny_bp',0):+6.1f}bp")

    # composition of the trades the baseline replay actually sizes (pkc=1 takes ~175 of 4560)
    taken = []; r = rp(T, eok=lambda t, o, n: taken.append(t) or 1.0)
    comp = []
    for coin in ("ETH", "BTC"):
        for label, pred in (("ITM", lambda t: itm(t)), ("OTM", lambda t: not itm(t))):
            g = [t for t in taken if t["coin"] == coin and pred(t)]
            pnl = [jc.fixed_lot_pnl(t) for t in g]
            comp.append(dict(coin=coin, group=label, n=len(g), sum_lot_pnl=round(float(np.sum(pnl)), 1) if g else 0,
                             win_rate=round(float(np.mean([x > 0 for x in pnl])) * 100, 1) if g else 0))
    print(f"\n-- baseline replay composition (reached sizing={len(taken)}, taken={r['n_taken']}, skipped_size={r['n_skipped_size']}) --")
    for c in comp:
        print(f"{c['coin']} {c['group']} n={c['n']:3d} sum_lot_pnl={c['sum_lot_pnl']:+7.1f} WR={c['win_rate']:5.1f}%")

    spot = Spot(); cnt = {}
    def BG(name, **kw):
        cnt[name] = [0]; return book_guard(spot, counter=cnt[name], **kw)
    VARIANTS = [
        ("R2a_filter_itm", F(lambda t: itm(t)), None, None),
        ("R2b_size0.5_itm", None, S(lambda t: itm(t)), None),
        ("R2b_size0.25_itm", None, S(lambda t: itm(t), 0.25), None),
        ("R2c_filter_itm_tol10bp", F(lambda t: itm(t, 0.0010)), None, None),
        ("R2c_filter_itm_tol25bp", F(lambda t: itm(t, 0.0025)), None, None),
        ("R2c_filter_itm_tol50bp", F(lambda t: itm(t, 0.0050)), None, None),
        ("R3a_skip_anycoin_itm", None, None, BG("R3a_skip_anycoin_itm", scope="any")),
        ("R3b_skip_samecoin_itm", None, None, BG("R3b_skip_samecoin_itm", scope="same")),
        ("R3c_size0.5_anycoin_itm", None, None, BG("R3c_size0.5_anycoin_itm", scope="any", mult=0.5)),
        ("R3c_size0.25_anycoin_itm", None, None, BG("R3c_size0.25_anycoin_itm", scope="any", mult=0.25)),
        ("R3d_skip_anycoin_itm>=0.5%", None, None, BG("R3d_skip_anycoin_itm>=0.5%", scope="any", depth=0.005)),
        ("R3d_skip_anycoin_itm>=1%", None, None, BG("R3d_skip_anycoin_itm>=1%", scope="any", depth=0.01)),
        ("R3d_skip_anycoin_itm>=2%", None, None, BG("R3d_skip_anycoin_itm>=2%", scope="any", depth=0.02)),
        ("R2a+R3a", F(lambda t: itm(t)), None, BG("R2a+R3a", scope="any")),
        # POST-HOC (added after seeing the descriptive table: ITM loss is ETH-only) — exploratory, not part of the pre-registered set
        ("POSTHOC_R2a_filter_itm_ETHonly", F(lambda t: t["coin"] == "ETH" and itm(t)), None, None),
        ("POSTHOC_R2b_size0.5_itm_ETHonly", None, S(lambda t: t["coin"] == "ETH" and itm(t)), None),
    ]
    rows = [dict(base, variant="baseline", n_guard_hits=0, pass_bar=True)]
    print("\n-- replay --")
    print(f"{'baseline':28s} n={base['n']:4d} taken {base['taken']:3d} | tr {base['tr']:+6.1f}/dd{base['tr_dd']:4.1f} ho {base['ho']:+6.1f}/dd{base['ho_dd']:4.1f} full {base['full']:+6.1f}/dd{base['full_dd']:4.1f} | Q {base['q']}")
    for name, filt, smf, eok in VARIANTS:
        sub = filt(T) if filt else T
        if name in cnt: cnt[name][0] = 0
        m = evaluate(sub, smf, eok)
        hits = cnt[name][0] if name in cnt else 0  # counts guard hits across full+tr+ho+quarters replays
        m["variant"] = name; m["n_guard_hits_full"] = None; m["pass_bar"] = passes(m, base)
        if name in cnt:  # full-replay-only hit count
            cnt[name][0] = 0; rp(sub, smf, eok); m["n_guard_hits_full"] = cnt[name][0]
        rows.append(m)
        print(f"{name:28s} n={m['n']:4d} taken {m['taken']:3d} | tr {m['tr']:+6.1f}/dd{m['tr_dd']:4.1f} ho {m['ho']:+6.1f}/dd{m['ho_dd']:4.1f} full {m['full']:+6.1f}/dd{m['full_dd']:4.1f} | Q {m['q']} "
              f"skip={m['n_skipped_guard']} hits={m['n_guard_hits_full']} {'PASS' if m['pass_bar'] else ''}", flush=True)
    OUT.write_text(json.dumps({"fidelity": "OK", "expect": EXPECT, "bar": "ho>=base & ho_dd<=base & tr>=0.9*base_tr & q_i>=base_q_i-2pp",
                               "r2_descriptive": desc, "baseline_taken_composition": comp, "variants": rows}, indent=1, default=float))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
