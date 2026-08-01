"""Option-tenor sweep — user's idea 2026-08-02, prompted by the mark_iv
forward-VRP finding (research/fresh_data/iv_history_eth.jsonl analysis,
chat-only, not persisted as a script): real edge (IV > realized vol that
actually materializes) is strongest for near-dated options (+11% spread,
74.7% IV>RV at 6-12h dte) and decays toward zero by 24-36h dte. Jony trades
weekly (jc.TARGET_EXPIRY_H=168h) — this sweep asks whether a shorter tenor,
closer to where the measured edge lives, backtests better than weekly.

Caveat (same as always): this backtest still prices via synthetic
BS-off-realized-vol (SIGMA_CLAMP), not real option IV — a shorter tenor
doesn't get any more "real" pricing data than weekly does. The mark_iv
finding is directional motivation for testing this, not an input to the
backtest itself.

hold_h scaling: PUT_EXIT/CALL_EXIT hold_h (120h/24h) were tuned for the
168h tenor. First pass here scales hold_h proportionally to the new
expiry_h (same fraction of tenor as today: PUT ~71%, CALL ~14%) and leaves
tp2_pct/sl_pct unchanged (tenor-invariant % thresholds, neutral starting
assumption) — if a tenor looks promising, retune TP2/SL for it specifically
before considering deploy, same as the round-3 exit-tuning process.

Run: python3 sweep_tenor.py
"""
from __future__ import annotations

import multiprocessing as mp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je

MO, CAP, CB_MODE = 6, 4, "per_key"
BASE_EXPIRY_H = jc.TARGET_EXPIRY_H  # 168h, what live PUT_EXIT/CALL_EXIT hold_h were tuned against

TENORS = [24, 48, 72, 168]


def scale_exit(exit_cfg: dict, expiry_h: float) -> dict:
    ratio = expiry_h / BASE_EXPIRY_H
    return {**exit_cfg, "hold_h": exit_cfg["hold_h"] * ratio}


def days(trades: list[dict]) -> float:
    return (trades[-1]["entry_ts"] - trades[0]["entry_ts"]) / 86_400_000 if trades else 0.0


def eval_tenor(expiry_h: float) -> dict:
    put_exit = scale_exit(jc.PUT_EXIT, expiry_h)
    call_exit = scale_exit(jc.CALL_EXIT, expiry_h)
    trades = je.coin_trades("ETH", expiry_h=expiry_h, put_exit=put_exit, call_exit=call_exit) + \
             je.coin_trades("BTC", expiry_h=expiry_h, put_exit=put_exit, call_exit=call_exit)
    tr, ho = je.split(trades, 0.70)
    r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE)
    r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE)
    res_counts = {}
    for t in trades:
        res_counts[t["resolution"]] = res_counts.get(t["resolution"], 0) + 1
    return {
        "label": f"{expiry_h:.0f}h" + (" (baseline)" if expiry_h == BASE_EXPIRY_H else ""),
        "expiry_h": expiry_h,
        "put_hold_h": put_exit["hold_h"], "call_hold_h": call_exit["hold_h"],
        "train_ret": r_tr["return_pct"], "train_dd": r_tr["max_dd"],
        "train_tpd": r_tr["n_taken"] / days(tr) if tr else 0.0,
        "holdout_ret": r_ho["return_pct"], "holdout_dd": r_ho["max_dd"],
        "holdout_tpd": r_ho["n_taken"] / days(ho) if ho else 0.0,
        "n_total": len(trades),
        "resolution_counts": res_counts,
    }


def eval_tenor_quarters(expiry_h: float) -> tuple:
    put_exit = scale_exit(jc.PUT_EXIT, expiry_h)
    call_exit = scale_exit(jc.CALL_EXIT, expiry_h)
    trades = je.coin_trades("ETH", expiry_h=expiry_h, put_exit=put_exit, call_exit=call_exit) + \
             je.coin_trades("BTC", expiry_h=expiry_h, put_exit=put_exit, call_exit=call_exit)
    qs = je.quarters(trades)
    out = []
    for i, q in enumerate(qs):
        if not q:
            out.append((i, 0, 0))
            continue
        r = je.replay_account(q, MO, CAP, cb_mode=CB_MODE)
        out.append((i, r["return_pct"], r["max_dd"]))
    return expiry_h, out


def print_table(results: list[dict]) -> None:
    base = next(r for r in results if r["expiry_h"] == BASE_EXPIRY_H)
    hdr = (f"{'tenor':14s} {'put_hold':>9s} {'call_hold':>9s} {'train_ret':>10s} {'train_dd':>9s} "
          f"{'tr_tpd':>7s} {'holdout_ret':>12s} {'ho_dd':>7s} {'ho_tpd':>7s} {'n':>6s}")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        flag = ""
        if r["expiry_h"] != base["expiry_h"]:
            improves = r["train_ret"] > base["train_ret"] and r["holdout_ret"] > base["holdout_ret"]
            dd_ok = r["holdout_dd"] <= base["holdout_dd"]
            flag = " <-- candidate" if (improves and dd_ok) else ""
        print(f"{r['label']:14s} {r['put_hold_h']:8.1f}h {r['call_hold_h']:8.1f}h {r['train_ret']:+9.1f}% "
             f"{r['train_dd']:8.1f}% {r['train_tpd']:6.2f} {r['holdout_ret']:+11.1f}% {r['holdout_dd']:6.1f}% "
             f"{r['holdout_tpd']:6.2f} {r['n_total']:6d}{flag}")
    print()
    for r in results:
        print(f"  {r['label']:14s} resolutions: {r['resolution_counts']}")


if __name__ == "__main__":
    print("=== Option-tenor sweep (expiry_h, hold_h scaled proportionally) ===")
    with mp.Pool(min(len(TENORS), mp.cpu_count())) as pool:
        results = pool.map(eval_tenor, TENORS)
    print_table(results)

    base = next(r for r in results if r["expiry_h"] == BASE_EXPIRY_H)
    candidates = [r for r in results if r["expiry_h"] != base["expiry_h"]
                 and r["train_ret"] > base["train_ret"] and r["holdout_ret"] > base["holdout_ret"]]
    print("\n=== Candidates: quarter robustness ===")
    if not candidates:
        print("  no tenor beat baseline (168h) on both train and holdout — skipping quarter check")
    else:
        _, base_q = eval_tenor_quarters(BASE_EXPIRY_H)
        with mp.Pool(min(len(candidates), mp.cpu_count())) as pool:
            q_results = pool.map(eval_tenor_quarters, [r["expiry_h"] for r in candidates])
        for expiry_h, qs in q_results:
            print(f"  {expiry_h:.0f}h:")
            for i, ret, dd in qs:
                b_ret, b_dd = base_q[i][1], base_q[i][2]
                flag = "better" if (ret >= b_ret and dd <= b_dd) else "worse"
                print(f"    Q{i+1}: ret={ret:+7.1f}% (base {b_ret:+7.1f}%)  dd={dd:5.1f}% (base {b_dd:5.1f}%)  [{flag}]")
