"""What-if: replay Jony's actual live history (since the bot's real start,
LIVE_START_MS below) as if the CURRENT config (config E entry gates + the
2026-08-01 exit retune) had been running from day one, starting from the
same $800. Prints a per-trade table (not just aggregate return/maxDD).

LIVE_START_MS = Jony's real bot_state.started_at_ms from VPS3
(2026-07-02 09:49:43 UTC) — read via `docker exec jony-jony_loop-1
python3 -c "from db import repo; ..."` on VPS3, not hardcoded from memory.

Run: python3 simulate_live_history.py
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je

LIVE_START_MS = 1782985783360
MO, CAP, CB_MODE = 6, 4, "per_key"


def replay_with_log(trades: list[dict], mo: int, cap: int, start_equity: float = jc.START_EQUITY_USD,
                    cb_mode: str = "per_key") -> list[dict]:
    """Same logic as jony_engine.replay_account but returns a per-trade log
    instead of just aggregate stats — this is the table the user asked for."""
    trades = sorted(trades, key=lambda t: t["entry_ts"])
    equity = start_equity
    open_positions: list[dict] = []
    recent_pnls: list[float] = []
    cb_until_by_key: dict[str, int] = {}
    log = []

    for t in trades:
        now = t["entry_ts"]
        cb_key = f"{t['coin']}:{t['side']}"
        open_positions = [p for p in open_positions if p["exit_ts"] > now]
        if now < cb_until_by_key.get(cb_key, -1):
            continue
        if len(open_positions) >= mo:
            continue
        if sum(1 for p in open_positions if p["coin"] == t["coin"]) >= cap:
            continue
        used_margin = sum(p["margin"] for p in open_positions)
        qty, margin = jc.size_position(equity, used_margin, recent_pnls,
                                       t["strike"], t["entry_credit"], t["lot"])
        if qty <= 0:
            continue
        notional = t["strike"] * qty
        premium_total = t["entry_credit"] * qty
        fee_open = jc.fee_usd(notional, premium_total)
        exit_credit = t["entry_credit"] * (1 - t["pnl_pct"])
        fee_close = jc.fee_usd(notional, exit_credit * qty)
        pnl_usd = (t["entry_credit"] - exit_credit) * qty - fee_open - fee_close
        equity_before = equity
        equity += pnl_usd
        recent_pnls.append(t["pnl_pct"])
        recent_pnls = recent_pnls[-50:]
        if t["pnl_pct"] <= 0:
            cb_until_by_key[cb_key] = t["exit_ts"] + jc.CB_PAUSE_HOURS * 3_600_000
        open_positions.append({"coin": t["coin"], "exit_ts": t["exit_ts"], "margin": margin})
        log.append({
            "entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"], "coin": t["coin"], "side": t["side"],
            "resolution": t["resolution"], "regime": t.get("regime"), "pnl_pct": t["pnl_pct"],
            "pnl_usd": pnl_usd, "equity_before": equity_before, "equity_after": equity, "qty": qty,
        })
    return log


if __name__ == "__main__":
    trades = je.coin_trades("ETH") + je.coin_trades("BTC")
    trades = sorted(trades, key=lambda t: t["entry_ts"])
    sim_trades = [t for t in trades if t["entry_ts"] >= LIVE_START_MS]
    log = replay_with_log(sim_trades, MO, CAP, cb_mode=CB_MODE)

    print(f"{'#':>3s} {'entry (UTC)':16s} {'coin':4s} {'side':4s} {'resolution':10s} "
         f"{'pnl%':>7s} {'pnl$':>9s} {'equity':>9s}")
    print("-" * 78)
    for i, r in enumerate(log, 1):
        dt = datetime.datetime.fromtimestamp(r["entry_ts"] / 1000, tz=datetime.timezone.utc)
        print(f"{i:3d} {dt.strftime('%Y-%m-%d %H:%M'):16s} {r['coin']:4s} {r['side']:4s} "
             f"{r['resolution']:10s} {r['pnl_pct']*100:+6.1f}% {r['pnl_usd']:+8.2f} {r['equity_after']:9.2f}")

    total_pnl = sum(r["pnl_usd"] for r in log)
    wins = sum(1 for r in log if r["pnl_usd"] > 0)
    print("-" * 78)
    print(f"Total: {len(log)} trades, {wins} wins / {len(log)-wins} losses (WR {wins/len(log)*100:.1f}%), "
         f"net {total_pnl:+.2f} -> final equity {log[-1]['equity_after']:.2f}" if log else "no trades")
