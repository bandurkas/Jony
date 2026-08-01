"""Regime-aware CALL position sizing sweep. config E (live since 2026-08-01)
added regime=='trend' to CALL_GEN's regime_filter, which materially raised
return and frequency but also raised maxDD in 2 of 4 quarters (13-13.5% vs
~9% baseline) — the flagged risk being selling calls in a trending market.

Idea: instead of a binary allow/forbid on trend-regime CALLs, shrink position
size specifically for those trades (jc.size_position's new size_mult param).
Keeps the frequency/edge, defangs the tail risk on the specific trades that
carry it.

Run: python3 sweep_regime_sizing.py
"""
from __future__ import annotations

import multiprocessing as mp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je

MO, CAP, CB_MODE = 6, 4, "per_key"


def days(trades: list[dict]) -> float:
    return (trades[-1]["entry_ts"] - trades[0]["entry_ts"]) / 86_400_000 if trades else 0.0


def make_size_mult_fn(call_trend_mult: float):
    def fn(t):
        if t["side"] == "C" and t["regime"] == "trend":
            return call_trend_mult
        return 1.0
    return fn


MULTS = [1.0, 0.75, 0.5, 0.35, 0.25, 0.0]  # 0.0 == same as removing trend from CALL_GEN.regime_filter


def eval_mult(call_trend_mult: float) -> dict:
    trades = je.coin_trades("ETH") + je.coin_trades("BTC")  # config E gates (jc.PUT_GEN/CALL_GEN, live)
    tr, ho = je.split(trades, 0.70)
    fn = make_size_mult_fn(call_trend_mult)
    r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE, size_mult_fn=fn)
    r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE, size_mult_fn=fn)
    return {
        "call_trend_mult": call_trend_mult,
        "train_ret": r_tr["return_pct"], "train_dd": r_tr["max_dd"],
        "train_tpd": r_tr["n_taken"] / days(tr) if tr else 0.0,
        "holdout_ret": r_ho["return_pct"], "holdout_dd": r_ho["max_dd"],
        "holdout_tpd": r_ho["n_taken"] / days(ho) if ho else 0.0,
    }


def eval_mult_quarters(call_trend_mult: float) -> tuple:
    trades = je.coin_trades("ETH") + je.coin_trades("BTC")
    qs = je.quarters(trades)
    fn = make_size_mult_fn(call_trend_mult)
    out = []
    for i, q in enumerate(qs):
        if not q:
            out.append((i, 0, 0)); continue
        r = je.replay_account(q, MO, CAP, cb_mode=CB_MODE, size_mult_fn=fn)
        out.append((i, r["return_pct"], r["max_dd"]))
    return call_trend_mult, out


if __name__ == "__main__":
    print(f"Sweeping CALL trend-regime size_mult over {MULTS} (mo={MO} cap={CAP})...")
    with mp.Pool(len(MULTS)) as pool:
        results = pool.map(eval_mult, MULTS)
    hdr = f"{'call_trend_mult':>16s} {'train_ret':>10s} {'train_dd':>9s} {'tr_tpd':>7s} {'holdout_ret':>12s} {'ho_dd':>7s} {'ho_tpd':>7s}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        print(f"{r['call_trend_mult']:16.2f} {r['train_ret']:+9.1f}% {r['train_dd']:8.1f}% {r['train_tpd']:6.2f} "
             f"{r['holdout_ret']:+11.1f}% {r['holdout_dd']:6.1f}% {r['holdout_tpd']:6.2f}")

    print("\nQuarter breakdown (fresh-capital replay per quarter):")
    with mp.Pool(len(MULTS)) as pool:
        qresults = pool.map(eval_mult_quarters, MULTS)
    for mult, qs in qresults:
        print(f"\ncall_trend_mult={mult}")
        for i, ret, dd in qs:
            print(f"  Q{i+1}: ret={ret:+8.1f}%  maxDD={dd:5.1f}%")
