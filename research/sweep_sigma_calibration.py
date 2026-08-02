"""Pricing-sigma recalibration — follow-up to the live-vs-backtest win-rate
gap investigation (2026-08-02): 21/21 real closed trades resolved via
time_stop/manual, ZERO via tp2/sl, vs ~20-50% predicted by the backtest.
Root cause: SIGMA_CLAMP=(0.20, 1.50) pricing sigma runs structurally hotter
than real Bybit option markIv (fresh_data/iv_history_eth.jsonl, 6wk, 6272
records) -- this engine's own rv1h_native averages 0.605 clamped over the
2yr backtest vs real markIv averaging 0.471 over the recent 6wk live window.

Calibration: linear fit of real avg(atm_call,atm_put) markIv against this
engine's own rv1h_native (unclamped), computed on the SAME 6wk overlap
window (see fresh_data/iv_vs_rv1h_overlap.csv, built ad hoc from
iv_history_eth.jsonl + load_klines('ETH','1h')):
    markIv = 0.3487 + 0.2646 * rv1h_native   (R^2=0.40, resid std 0.073)
Floor/ceiling set to the real markIv range observed (0.29-0.975) with a
little headroom: (0.25, 1.05).

Run: python3 sweep_sigma_calibration.py

RESULT, INCONCLUSIVE / NOT DEPLOYED (2026-08-02): the root finding stands --
21/21 real closed trades resolved via time_stop/manual, ZERO via tp2/sl,
vs ~20-50% predicted by the OLD (SIGMA_CLAMP) model, a ~1/3000 coincidence
if live matched the model. But this linear sigma recalibration does not
cleanly fix it:

1. Validated against all 21 real trades by replaying simulate_option_exit
   with the SAME real entry/strike/spot-path (see the validation snippet
   in this session, saved to scratchpad, not committed -- rebuild via
   jony_engine.simulate_option_exit per real position if needed). OLD vs
   NEW sigma: pnl_pct MAE ~6.4 vs ~6.3 (a wash), entry-credit MAE got
   WORSE (9.7% -> 16.5%). NEW still predicts 3/21 early exits (OLD: 4/21)
   -- both still far from the real 0/21.
2. Counterintuitive mechanism found: lowering sigma does NOT reduce
   predicted premium swings -- it INCREASES % sensitivity, because a
   lower-sigma option has less extrinsic/time-value cushion, so the same
   spot move is a bigger fraction of a smaller premium base. This is why
   sweep_calibrated_exits.py's resolution table shows MORE early exits
   under calibrated sigma (e.g. ETH C: 26.3%->46.2%), the opposite of the
   naive expectation.
3. Retuned TP2/SL under sigma_calib (see sweep_calibrated_exits.py) and
   quarter-tested the best candidate (CALL hold_h->12 + PUT tp2_pct->0.65)
   against the live baseline: Q2 wins clean, Q3/Q4 roughly tie, but Q1
   regresses hard (ret +1456.3%->+621.5%, dd 9.6%->15.8%). Same
   "real trade-off, not a clean win" verdict as the tenor sweep -- not
   worth deploying on this evidence.

Conclusion: the pricing-sigma level is real and worth revisiting, but a
6-week near-dated IV proxy + 21 noisy real trades with no intraday mark
history isn't enough to nail the fix. Do NOT deploy sigma_calib as tuned
here. The credible next step is instrumenting real per-position mark
logging in manage_exits() (loop.py) going forward -- see
SESSION_HANDOFF_2026-08-02.md's Jony-improvement thread -- then redo this
calibration once a few weeks of real intraday mark history exists.
sigma_calib itself stays in coin_trades() as a tested, working, opt-in
research hook (fidelity-checked against sigma_calib=None reproducing exact
live numbers) for whenever that data arrives.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_engine as je
import jony_core as jc

MO, CAP = 10, 6
CB_MODE = "per_key"

CALIB = {"b0": 0.3487, "b1": 0.2646, "floor": 0.25, "ceiling": 1.05}


def resolution_table(trades, label):
    for side in ("P", "C"):
        sub = [t for t in trades if t["side"] == side]
        if not sub:
            continue
        c = Counter(t["resolution"] for t in sub)
        n = len(sub)
        print(f"  {label} {side}: n={n} "
              f"time_stop={c.get('time_stop',0)/n*100:.1f}% "
              f"tp2={c.get('tp2',0)/n*100:.1f}% sl={c.get('sl',0)/n*100:.1f}%")


def run():
    old_eth = je.coin_trades("ETH")
    old_btc = je.coin_trades("BTC")
    new_eth = je.coin_trades("ETH", sigma_calib=CALIB)
    new_btc = je.coin_trades("BTC", sigma_calib=CALIB)

    print("=== Resolution distribution: OLD (SIGMA_CLAMP) vs NEW (calibrated) ===")
    resolution_table(old_eth, "OLD ETH")
    resolution_table(new_eth, "NEW ETH")
    resolution_table(old_btc, "OLD BTC")
    resolution_table(new_btc, "NEW BTC")

    old_trades = old_eth + old_btc
    new_trades = new_eth + new_btc
    old_tr, old_ho = je.split(old_trades, 0.70)
    new_tr, new_ho = je.split(new_trades, 0.70)

    print("\n=== Portfolio return/dd: OLD vs NEW (live TP2/SL thresholds unchanged) ===")
    for label, tr, ho in [("OLD", old_tr, old_ho), ("NEW", new_tr, new_ho)]:
        r_tr = je.replay_account(tr, MO, CAP, cb_mode=CB_MODE)
        r_ho = je.replay_account(ho, MO, CAP, cb_mode=CB_MODE)
        print(f"  {label}: train={r_tr['return_pct']:+.1f}%/{r_tr['max_dd']:.1f}%dd  "
              f"holdout={r_ho['return_pct']:+.1f}%/{r_ho['max_dd']:.1f}%dd")


if __name__ == "__main__":
    run()
