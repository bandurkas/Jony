"""Deploy-configuration gate for the mechanical trailing profit-lock
(2026-08-14, agent Stage A).

Two candidate semantics — they are DIFFERENT configs and only the one that
ships may be validated:
  replace — trail replaces tp2, hold extended to 120h, live sl kept
            (exactly what Phase 6 measured; per-key params = Phase 6 train
            picks: ETH:P (0.30,0.20), ETH:C (0.40,0.10), BTC:C (0.20,0.10)).
  overlay — live tp2/sl/hold all untouched, trail added as an extra exit;
            per-key (arm,giveback) picked on TRAIN here (3x3 grid), then
            validated on holdout/quarters/second sigma like everything else.

BTC:P stays on stock PUT exits in every candidate (Phase 6: trail hurts it).

Gate criteria (vs "base" = live exits all 4 keys):
  1. fixed-lot holdout total: candidate >= base on BOTH sigma models;
  2. portfolio replay_v2 (MO=10 CAP=6, per_key_cap=1) holdout: >= base;
  3. no quarter catastrophically worse than base (>$1000 degradation).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import jony_core as jc

jc.COIN_SIDES = {"ETH": ("P", "C"), "BTC": ("C", "P")}

import jony_engine as je
from replay_account_v2 import replay_v2

spec = importlib.util.spec_from_file_location(
    "tl", HERE / "trailing_lock_v2_2026-08-14.py")
tl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tl)

CALIB = tl.CALIB
LIVE_EXIT = {"P": jc.PUT_EXIT, "C": jc.CALL_EXIT}
REPLACE_PARAMS = {"ETH:P": (0.30, 0.20), "ETH:C": (0.40, 0.10),
                  "BTC:C": (0.20, 0.10)}
TRAIL_KEYS = ("ETH:P", "ETH:C", "BTC:C")
ALL_KEYS = (("ETH", "P"), ("ETH", "C"), ("BTC", "C"), ("BTC", "P"))
ARMS = (0.20, 0.30, 0.40)
GBS = (0.10, 0.15, 0.20)
MO, CAP = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP


def exit_with_rule(tr: dict, side: str, mode: str,
                   trail: tuple[float, float] | None) -> tuple[float, int]:
    """Returns (pnl_pct, exit_ts). mode: base | replace | overlay."""
    p = tr["p"]
    live = LIVE_EXIT[side]
    sl = live["sl_pct"]
    if mode == "replace" and trail is not None:
        hold_bars = min(tl.BARS, len(p))
        pw = p[:hold_bars]
        cands = []
        sl_hits = np.flatnonzero(pw <= -sl)
        if len(sl_hits):
            cands.append((sl_hits[0], 0))
        peak = np.maximum.accumulate(pw)
        hits = np.flatnonzero((peak >= trail[0]) & (pw <= peak - trail[1]))
        if len(hits):
            cands.append((hits[0], 1))
        if cands:
            idx = min(cands)[0]
            return float(pw[idx]), int(tr["exit_ts_path"][idx])
        return float(pw[-1]), int(tr["exit_ts_path"][hold_bars - 1])
    # base and overlay share live tp2/sl/hold; overlay adds the trail exit
    hold_bars = min(int(live["hold_h"] * 12), len(p))
    pw = p[:hold_bars]
    cands = []
    sl_hits = np.flatnonzero(pw <= -sl)
    if len(sl_hits):
        cands.append((sl_hits[0], 0))
    tp_hits = np.flatnonzero(pw >= live["tp2_pct"])
    if len(tp_hits):
        cands.append((tp_hits[0], 1))
    if mode == "overlay" and trail is not None:
        peak = np.maximum.accumulate(pw)
        hits = np.flatnonzero((peak >= trail[0]) & (pw <= peak - trail[1]))
        if len(hits):
            cands.append((hits[0], 2))
    if cands:
        idx = min(cands)[0]
        return float(pw[idx]), int(tr["exit_ts_path"][idx])
    return float(pw[-1]), int(tr["exit_ts_path"][hold_bars - 1])


def to_trades(paths: list[dict], side: str, mode: str,
              trail: tuple[float, float] | None) -> list[dict]:
    out = []
    for tr in paths:
        pnl, exit_ts = exit_with_rule(tr, side, mode, trail)
        out.append({"coin": tr["coin"], "side": side,
                    "entry_ts": tr["entry_ts"], "exit_ts": exit_ts,
                    "pnl_pct": pnl, "strike": tr["strike"],
                    "entry_credit": tr["entry_credit"], "lot": tr["lot"]})
    return out


def fixed_usd(trades) -> float:
    return sum(tl.fixed_lot_usd(
        {"lot": t["lot"], "strike": t["strike"], "entry_credit": t["entry_credit"]},
        t["pnl_pct"]) for t in trades)


def main() -> None:
    for sig_label, sc in (("CALIB", CALIB), ("SIGMA_CLAMP", None)):
        print(f"\n{'=' * 74}\n### sigma = {sig_label}\n{'=' * 74}")
        paths = {f"{c}:{s}": tl.build_paths(c, s, sigma_calib=sc)
                 for c, s in ALL_KEYS}

        # per-key overlay params picked on TRAIN (this sigma's own train set
        # would be selection-on-CALIB only; to avoid double-dipping, params
        # are picked ONCE on CALIB train and reused verbatim for SIGMA_CLAMP)
        if sc is CALIB:
            global OVERLAY_PARAMS
            OVERLAY_PARAMS = {}
            for key in TRAIL_KEYS:
                coin, side = key.split(":")
                pt = paths[key]
                split_ts = pt[0]["entry_ts"] + 0.70 * (pt[-1]["entry_ts"] - pt[0]["entry_ts"])
                best = None
                for arm in ARMS:
                    for gb in GBS:
                        tr_trades = [t for t in to_trades(pt, side, "overlay", (arm, gb))
                                     if t["entry_ts"] < split_ts]
                        usd = fixed_usd(tr_trades)
                        if best is None or usd > best[0]:
                            best = (usd, arm, gb)
                OVERLAY_PARAMS[key] = (best[1], best[2])
                print(f"overlay pick {key}: arm={best[1]} gb={best[2]} (train ${best[0]:+.2f})")

        configs = {
            "base": {k: ("base", None) for k in dict(
                (f"{c}:{s}", 1) for c, s in ALL_KEYS)},
            "replace": {**{k: ("replace", REPLACE_PARAMS[k]) for k in TRAIL_KEYS},
                        "BTC:P": ("base", None)},
            "overlay": {**{k: ("overlay", OVERLAY_PARAMS[k]) for k in TRAIL_KEYS},
                        "BTC:P": ("base", None)},
        }

        results = {}
        for cname, cfg in configs.items():
            all_trades = []
            for key, (mode, trail) in cfg.items():
                coin, side = key.split(":")
                all_trades += to_trades(paths[key], side, mode, trail)
            all_trades.sort(key=lambda t: t["entry_ts"])
            tr, ho = je.split(all_trades, 0.70)
            rt = replay_v2(tr, MO, CAP, per_key_cap=1)
            rh = replay_v2(ho, MO, CAP, per_key_cap=1)
            qs = [round(fixed_usd(q), 2) for q in je.quarters(all_trades)]
            results[cname] = {"fl_train": fixed_usd(tr), "fl_hold": fixed_usd(ho),
                              "rp_train": rt["return_pct"], "rp_hold": rh["return_pct"],
                              "dd_train": rt["max_dd"], "dd_hold": rh["max_dd"],
                              "quarters": qs}
            r = results[cname]
            print(f"{cname:8s} fixed$ train {r['fl_train']:+10.2f} hold {r['fl_hold']:+9.2f}"
                  f" | replay(pkc=1) train {r['rp_train']:+7.1f}%/DD{r['dd_train']:4.1f}%"
                  f" hold {r['rp_hold']:+7.1f}%/DD{r['dd_hold']:4.1f}%"
                  f" | Q {r['quarters']}")


if __name__ == "__main__":
    main()
