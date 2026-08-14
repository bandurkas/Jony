"""Is Jony's problem the SIGNAL or the SIZING?

replay_account_v2 showed the live-faithful portfolio replay is deeply negative
where v1 showed +449695%. Two very different diagnoses fit that:
  (a) the per-trade edge is negative      -> signal problem, sizing can't fix it
  (b) the per-trade edge is positive but compounding at 15% margin/trade with a
      175%-of-credit stop turns it negative -> sizing problem, fixable

This separates them: raw per-trade stats, then a FIXED-SIZE replay (constant
1 lot, no compounding, no margin budget) against the compounded one, on the
same event ordering.

Run: python3 diag_edge_vs_sizing_v2.py
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je
from replay_account_v2 import replay_v2


def raw_stats(name: str, ts: list[dict]) -> None:
    if not ts:
        return
    pnl = [t["pnl_pct"] for t in ts]
    wins = [p for p in pnl if p > 0]
    print(f"{name:9s} n={len(ts):>6d}  WR={len(wins)/len(ts)*100:5.1f}%  "
          f"mean={statistics.mean(pnl)*100:+7.2f}%  median={statistics.median(pnl)*100:+7.2f}%  "
          f"min={min(pnl)*100:+8.1f}%  max={max(pnl)*100:+7.1f}%")
    by_res: dict[str, list[float]] = {}
    for t in ts:
        by_res.setdefault(t["resolution"], []).append(t["pnl_pct"])
    for res, v in sorted(by_res.items(), key=lambda kv: -len(kv[1])):
        print(f"          {res:12s} n={len(v):>6d} ({len(v)/len(ts)*100:4.1f}%)  "
              f"mean={statistics.mean(v)*100:+7.2f}%  total={sum(v)*100:+9.1f}%")


def fixed_size_replay(trades: list[dict], mo: int, cap: int,
                      per_key_cap: int | None = None) -> dict:
    """Same admission rules as replay_v2 (time-ordered, caps, live CB), but
    every accepted trade is exactly 1 lot — no equity feedback, no margin
    budget. Isolates the signal's cumulative $ edge from compounding."""
    trades = sorted(trades, key=lambda t: t["entry_ts"])
    open_positions: list[dict] = []
    cb_until: dict[str, int] = {}
    pnl_total = 0.0
    curve = []
    peak = 0.0
    max_dd_usd = 0.0
    n = 0
    import heapq
    pending: list[tuple[int, int, dict]] = []
    seq = 0
    for t in trades:
        now = t["entry_ts"]
        while pending and pending[0][0] <= now:
            ets, _, pos = heapq.heappop(pending)
            pnl_total += pos["pnl_usd"]
            if pos["pnl_pct"] <= 0:
                cb_until[pos["k"]] = ets + jc.CB_PAUSE_HOURS * 3_600_000
            peak = max(peak, pnl_total)
            max_dd_usd = max(max_dd_usd, peak - pnl_total)
            curve.append((ets, pnl_total))
        open_positions = [p for p in open_positions if p["exit_ts"] > now]
        k = f"{t['coin']}:{t['side']}"
        if now < cb_until.get(k, -1):
            continue
        if len(open_positions) >= mo:
            continue
        if sum(1 for p in open_positions if p["coin"] == t["coin"]) >= cap:
            continue
        if per_key_cap is not None and \
                sum(1 for p in open_positions if p["k"] == k) >= per_key_cap:
            continue
        qty = t["lot"]
        notional = t["strike"] * qty
        exit_credit = t["entry_credit"] * (1 - t["pnl_pct"])
        pnl_usd = (t["entry_credit"] - exit_credit) * qty \
            - jc.fee_usd(notional, t["entry_credit"] * qty) \
            - jc.fee_usd(notional, exit_credit * qty)
        n += 1
        pos = {"coin": t["coin"], "exit_ts": t["exit_ts"], "k": k,
               "pnl_usd": pnl_usd, "pnl_pct": t["pnl_pct"]}
        open_positions.append(pos)
        seq += 1
        heapq.heappush(pending, (t["exit_ts"], seq, pos))
    while pending:
        ets, _, pos = heapq.heappop(pending)
        pnl_total += pos["pnl_usd"]
        peak = max(peak, pnl_total)
        max_dd_usd = max(max_dd_usd, peak - pnl_total)
    return {"pnl_usd": pnl_total, "n": n, "max_dd_usd": max_dd_usd}


def main() -> None:
    trades = je.coin_trades("ETH") + je.coin_trades("BTC")
    tr, ho = je.split(trades, 0.70)
    MO, CAP = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP

    print("=== raw per-trade stats (every signal, before portfolio admission) ===")
    raw_stats("ALL", trades)
    print()
    for side in ("P", "C"):
        raw_stats(f"side={side}", [t for t in trades if t["side"] == side])
    print()
    for coin in ("ETH", "BTC"):
        raw_stats(coin, [t for t in trades if t["coin"] == coin])

    print("\n=== fixed 1-lot (no compounding) vs compounded, same admission ===")
    print(f"{'split':9s} {'keycap':>7s} {'fixed $pnl':>12s} {'fixed maxDD$':>13s} "
          f"{'n':>6s} {'compounded ret%':>16s} {'cDD%':>7s}")
    for name, sub in (("train", tr), ("holdout", ho)):
        for kc in (None, 1):
            f = fixed_size_replay(sub, MO, CAP, per_key_cap=kc)
            c = replay_v2(sub, MO, CAP, per_key_cap=kc)
            print(f"{name:9s} {str(kc):>7s} {f['pnl_usd']:>12.2f} {f['max_dd_usd']:>13.2f} "
                  f"{f['n']:>6d} {c['return_pct']:>16.1f} {c['max_dd']:>7.1f}")


if __name__ == "__main__":
    main()
