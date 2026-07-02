"""Read-only FastAPI for Jony (dashboard integration / health checks).
The loop is the single writer; this process only reads. CORS is open because
the opt-app dashboard fetches this API straight from the browser (same
pattern as Tyagach's :8100)."""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core import strategy
from db import repo
from services import config

app = FastAPI(title="Jony", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

# The loop owns writes; applying the (idempotent) schema here only protects
# reads from the startup race where the API answers before the loop's first tick.
_conn = repo.connect()
repo.apply_schema(_conn)
_conn.close()


@app.get("/health")
def health():
    return {"ok": True, "bot": config.BOT_TAG, "mode": config.TRADING_MODE}


@app.get("/state")
def state():
    conn = repo.connect()
    try:
        st = repo.try_get_state(conn)
        if st is None:
            return {"initialized": False}
        st["initialized"] = True
        st["recent_pnls_json"] = json.loads(st["recent_pnls_json"])
        st["last_fired_json"] = json.loads(st["last_fired_json"])
        st["paused"] = repo.is_paused(conn)

        closed = [dict(r) for r in conn.execute(
            "SELECT pnl_usd FROM positions WHERE status != 'open'"
            " AND pnl_usd IS NOT NULL")]
        wins = sum(1 for r in closed if r["pnl_usd"] > 0)
        st["n_closed"] = len(closed)
        st["wins"] = wins
        st["losses"] = len(closed) - wins
        st["win_rate"] = wins / len(closed) if closed else None
        st["total_pnl_usd"] = round(sum(r["pnl_usd"] for r in closed), 2)
        st["open_position_count"] = len(repo.open_positions(conn))

        # max drawdown over realized-equity snapshots (peak-to-trough, %)
        eq = [r["equity_usd"] for r in conn.execute(
            "SELECT equity_usd FROM equity_snapshots ORDER BY ts_ms")]
        peak, max_dd = st["start_equity_usd"], 0.0
        for v in eq + [st["equity_usd"]]:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak)
        st["max_dd_pct"] = round(max_dd * 100, 2)
        return st
    finally:
        conn.close()


@app.get("/params")
def params():
    """Strategy/account parameters — the dashboard renders these instead of
    hardcoding a copy that would drift from the bot."""
    return {
        "coins": {c: list(s) for c, s in strategy.COIN_SIDES.items()},
        "put_gen": {**strategy.PUT_GEN,
                    "regime_filter": list(strategy.PUT_GEN["regime_filter"])},
        "call_gen": {**strategy.CALL_GEN,
                     "regime_filter": list(strategy.CALL_GEN["regime_filter"])},
        "put_exit": strategy.PUT_EXIT,
        "call_exit": strategy.CALL_EXIT,
        "account": {
            "start_equity_usd": config.START_EQUITY_USD,
            "margin_pct_per_trade": config.MARGIN_PCT_PER_TRADE,
            "max_open_positions": config.MAX_OPEN_POSITIONS,
            "per_coin_cap": config.PER_COIN_CAP,
            "port_margin_cap": config.PORT_MARGIN_CAP,
            "cb_consec_limit": config.CB_CONSEC_LIMIT,
            "cb_pause_hours": config.CB_PAUSE_HOURS,
            "cooldown_min": config.COOLDOWN_BARS * 5,
            "target_expiry_h": config.TARGET_EXPIRY_H,
        },
        "backtest": {
            "finding": "finding_basket_eth_btc_call_mo4 (2026-07-02)",
            "full_return_pct": 126.3, "max_dd_pct": 20.0,
            "holdout_return_pct": 3.7, "trades_per_day": 1.05,
        },
    }


@app.get("/positions")
def positions(limit: int = 50):
    conn = repo.connect()
    try:
        return {"open": repo.open_positions(conn),
                "recent": repo.recent_positions(conn, limit)}
    finally:
        conn.close()


@app.get("/equity")
def equity(limit: int = 2000):
    conn = repo.connect()
    try:
        return repo.equity_series(conn, limit)
    finally:
        conn.close()


@app.get("/audit/recent")
def audit(limit: int = 200):
    conn = repo.connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM signal_audit ORDER BY ts_ms DESC LIMIT ?", (limit,))]
        return rows
    finally:
        conn.close()
