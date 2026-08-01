"""Train/holdout sweep of Jony's entry-frequency levers (vol_threshold,
regime_filter, mtf_min_aligned, bull_market_ratio_max in core/strategy.py's
PUT_GEN/CALL_GEN) — the exact territory core/strategy.py flags as
"rejected 5x over, do not tune without a new validated backtest".

Rationale for re-testing (2026-08-01): the CB-isolation + MAX_OPEN/PER_COIN_CAP
fix shipped the same day changed the account-level trade population these
levers operate on (holdout trades/day 0.63 -> 1.02, active-days 27% -> 31%).
The prior 5x-rejected backtests were run against the OLD shared-CB/tighter-cap
config, so they may no longer be the right baseline for judging these levers.

Methodology: single-lever sensitivity sweep first (one change at a time vs.
the live baseline), evaluated on BOTH train and holdout with the *new*
per-(coin,side) CB + MO6/PER_COIN_CAP4 as the fixed account-engine backbone
(matches what's actually deployed). A candidate only proceeds to the combined
round if it improves return_pct on train AND holdout without materially
worsening maxDD. This mirrors the methodology already used and reported to
the user for the CB-isolation config choice (A/B/C/D table).

Run: python3 sweep_thresholds.py
"""
from __future__ import annotations

import copy
import multiprocessing as mp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je

MO, CAP, CB_MODE = 6, 4, "per_key"  # matches the live-deployed 2026-08-01 config

BASE_PUT = dict(jc.PUT_GEN, mtf_min_aligned=2)
BASE_CALL = dict(jc.CALL_GEN)


def make_variant(put_overrides: dict | None = None, call_overrides: dict | None = None) -> tuple[dict, dict]:
    put_gen = copy.deepcopy(BASE_PUT)
    call_gen = copy.deepcopy(BASE_CALL)
    if put_overrides:
        put_gen.update(put_overrides)
    if call_overrides:
        call_gen.update(call_overrides)
    return put_gen, call_gen


def days(trades: list[dict]) -> float:
    return (trades[-1]["entry_ts"] - trades[0]["entry_ts"]) / 86_400_000 if trades else 0.0


def eval_variant(args) -> dict:
    label, put_overrides, call_overrides = args
    put_gen, call_gen = make_variant(put_overrides, call_overrides)
    trades = je.coin_trades("ETH", put_gen=put_gen, call_gen=call_gen) + \
             je.coin_trades("BTC", put_gen=put_gen, call_gen=call_gen)
    tr, ho = je.split(trades, 0.70)
    r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE)
    r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE)
    return {
        "label": label,
        "train_ret": r_tr["return_pct"], "train_dd": r_tr["max_dd"],
        "train_tpd": r_tr["n_taken"] / days(tr) if tr else 0.0,
        "holdout_ret": r_ho["return_pct"], "holdout_dd": r_ho["max_dd"],
        "holdout_tpd": r_ho["n_taken"] / days(ho) if ho else 0.0,
        "n_total": len(trades),
    }


SINGLE_LEVER_VARIANTS = [
    ("baseline (live)", None, None),
    ("PUT vol_threshold 0.50->0.45", {"vol_threshold": 0.45}, None),
    ("PUT vol_threshold 0.50->0.40", {"vol_threshold": 0.40}, None),
    ("PUT vol_threshold 0.50->0.35", {"vol_threshold": 0.35}, None),
    ("PUT vol_threshold 0.50->0.55 (tighten)", {"vol_threshold": 0.55}, None),
    ("PUT regime +transition", {"regime_filter": ("range", "transition")}, None),
    ("PUT mtf_min_aligned 2->1", {"mtf_min_aligned": 1}, None),
    ("CALL vol_threshold 0.60->0.55", None, {"vol_threshold": 0.55}),
    ("CALL vol_threshold 0.60->0.50", None, {"vol_threshold": 0.50}),
    ("CALL vol_threshold 0.60->0.45", None, {"vol_threshold": 0.45}),
    ("CALL vol_threshold 0.60->0.65 (tighten)", None, {"vol_threshold": 0.65}),
    ("CALL regime +trend", None, {"regime_filter": ("range", "transition", "trend")}),
    ("CALL bull_market_ratio_max 1.05->1.10", None, {"bull_market_ratio_max": 1.10}),
    ("CALL bull_market_ratio_max disabled", None, {"bull_market_ratio_max": None}),
]


def print_table(results: list[dict]) -> None:
    base = next(r for r in results if r["label"].startswith("baseline"))
    hdr = f"{'label':42s} {'train_ret':>10s} {'train_dd':>9s} {'tr_tpd':>7s} {'holdout_ret':>12s} {'ho_dd':>7s} {'ho_tpd':>7s}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        flag = ""
        if r["label"] != base["label"]:
            improves = r["train_ret"] > base["train_ret"] and r["holdout_ret"] > base["holdout_ret"]
            dd_ok = r["holdout_dd"] < base["holdout_dd"] * 1.5
            flag = " <-- candidate" if (improves and dd_ok) else ""
        print(f"{r['label']:42s} {r['train_ret']:+9.1f}% {r['train_dd']:8.1f}% {r['train_tpd']:6.2f} "
             f"{r['holdout_ret']:+11.1f}% {r['holdout_dd']:6.1f}% {r['holdout_tpd']:6.2f}{flag}")


if __name__ == "__main__":
    n_workers = min(len(SINGLE_LEVER_VARIANTS), mp.cpu_count())
    print(f"Running {len(SINGLE_LEVER_VARIANTS)} variants on {n_workers} workers "
         f"(mo={MO} cap={CAP} cb_mode={CB_MODE})...")
    with mp.Pool(n_workers) as pool:
        results = pool.map(eval_variant, SINGLE_LEVER_VARIANTS)
    print_table(results)
