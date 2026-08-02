"""BTC PUT — never tried in this project's history. `jc.COIN_SIDES =
{"ETH": ("P","C"), "BTC": ("C",)}` restricts BTC to CALL only; grep across
every research/*.py + git log found no prior test, no comment explaining
why, nothing. All the mechanics (lot size, strike rounding, margin calc)
are already coin-aware, not side-restricted -- the block is purely
COIN_SIDES. This checks whether it's a real missed opportunity or was
correctly left off.

Uses the existing PUT_GEN/PUT_EXIT (shared across coins, same as how BTC
CALL already reuses CALL_GEN/CALL_EXIT) as the first-pass starting point --
no BTC-specific tuning yet, that's a follow-up if this looks promising.

Run: python3 sweep_btc_put.py

RESULT, REJECTED (2026-08-02): BTC PUT solo looks solid in isolation (3056
trades, 74.4% WR, holdout +320.7%/7.7%dd on a clean $800 start) -- the
gates aren't dead, PUT_GEN/PUT_EXIT reused from ETH work fine mechanically.
But added to the real portfolio it's a net negative on TWO independent
methodologies that agree with each other (unlike the MAX_OPEN_POSITIONS
case in §10-11, where they disagreed and live-history broke the tie in
favor of shipping):

1. **2yr synthetic backtest**: holdout return +6091.9%->+6903.8% (+13%,
   modest), but drawdown gets WORSE in every single quarter (Q1 9.6%->
   11.2%, Q2 13.0%->15.0%, Q3 10.1%->14.1% with a RETURN regression too,
   Q4 6.7%->10.1%). n_skipped_cap barely moves (1037->1039) -- this isn't
   good ETH trades getting crowded out by caps, it's just more concurrent
   exposure raising portfolio variance.
2. **Real live-history replay** (simulate_live_history.py's
   replay_with_log, Jony's actual live window since LIVE_START_MS): WORSE
   on both axes -- final equity $1407.69(+76.0%)->$1372.12(+71.5%), maxDD
   3.5%->6.3% (nearly doubles), same 83.3% WR.

Tried raising MAX_OPEN_POSITIONS/PER_COIN_CAP (10/6->12/8, 10/8, 14/8) to
test whether it's a capacity/slot-competition artifact (the same fix that
worked for the position-cap lever, §10-12) -- it does NOT fix this: live-
history replay only recovers to +74.0%/6.2%dd at best, still clearly worse
than the +76.0%/3.5%dd baseline on both metrics. Confirms the extra risk
comes from the BTC PUT signal itself, not a capacity artifact -- more caps
just admits more of the same lower-quality-when-portfolio-mixed trades.

Conclusion: BTC PUT does NOT get added. Not a bug, not a capacity issue --
BTC PUT trades genuinely dilute portfolio-level risk-adjusted return when
mixed with the existing ETH P+C / BTC C book, even though the side looks
fine standing alone. Live code untouched (jc.COIN_SIDES unchanged). Do not
retest without new information (e.g. a BTC-specific PUT_GEN/PUT_EXIT retune
-- not attempted here, since the failure mode is portfolio-level dilution,
not bad per-trade signal quality, so retuning the entry/exit thresholds is
unlikely to fix it without evidence otherwise).
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je

MO, CAP, CB_MODE = 10, 6, "per_key"


def with_btc_put(fn):
    """jony_engine._BASE_CACHE memoizes build_coin_base(coin) by coin string
    only -- it bakes in allowed_P/allowed_C from jc.COIN_SIDES at cache-fill
    time and does NOT know a later COIN_SIDES mutation should invalidate it.
    Any coin_trades('BTC', ...) call earlier in the SAME process (even the
    live-config baseline call) poisons this — must evict before AND after
    patching, not just restore the dict."""
    orig = jc.COIN_SIDES["BTC"]
    je._BASE_CACHE.pop("BTC", None)
    jc.COIN_SIDES["BTC"] = ("P", "C")
    try:
        return fn()
    finally:
        jc.COIN_SIDES["BTC"] = orig
        je._BASE_CACHE.pop("BTC", None)


def run():
    eth = je.coin_trades("ETH")
    btc_call_only = je.coin_trades("BTC")
    btc_put_solo = with_btc_put(lambda: je.coin_trades("BTC", sides_enabled=("P",)))
    btc_both = with_btc_put(lambda: je.coin_trades("BTC"))

    print("=== BTC PUT signal generation (first-pass, shared PUT_GEN/PUT_EXIT) ===")
    print(f"BTC CALL (live): {len(btc_call_only)} trades")
    print(f"BTC PUT (new, solo): {len(btc_put_solo)} trades")
    if btc_put_solo:
        c = Counter(t["resolution"] for t in btc_put_solo)
        wins = sum(1 for t in btc_put_solo if t["pnl_pct"] > 0)
        print(f"  win_rate={wins/len(btc_put_solo)*100:.1f}%  resolution={dict(c)}")
        tr, ho = je.split(btc_put_solo, 0.70)
        r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE)
        r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE)
        print(f"  BTC-PUT-ONLY portfolio: train={r_tr['return_pct']:+.1f}%/{r_tr['max_dd']:.1f}%dd "
              f"holdout={r_ho['return_pct']:+.1f}%/{r_ho['max_dd']:.1f}%dd")

    print("\n=== Portfolio impact: baseline (ETH P+C, BTC C) vs BTC P+C added ===")
    baseline = eth + btc_call_only
    candidate = eth + btc_both
    for label, trades in [("baseline", baseline), ("BTC P+C candidate", candidate)]:
        tr, ho = je.split(trades, 0.70)
        r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE)
        r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE)
        print(f"  {label:20s} n={len(trades):5d} train={r_tr['return_pct']:+10.1f}%/{r_tr['max_dd']:5.1f}%dd "
              f"holdout={r_ho['return_pct']:+9.1f}%/{r_ho['max_dd']:5.1f}%dd")

    print("\n=== Quarter robustness ===")
    qs_base = je.quarters(baseline)
    qs_cand = je.quarters(candidate)
    for i in range(4):
        r_b = je.replay_account(qs_base[i], MO, CAP, cb_mode=CB_MODE)
        r_c = je.replay_account(qs_cand[i], MO, CAP, cb_mode=CB_MODE)
        print(f"  Q{i+1}: baseline ret={r_b['return_pct']:+9.1f}%/dd={r_b['max_dd']:5.1f}%   "
              f"+BTC-PUT ret={r_c['return_pct']:+9.1f}%/dd={r_c['max_dd']:5.1f}%")


if __name__ == "__main__":
    run()
