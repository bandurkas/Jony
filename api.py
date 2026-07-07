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


@app.post("/pause")
def pause():
    conn = repo.connect()
    try:
        repo.set_paused(conn, True)
        return {"ok": True, "paused": True}
    finally:
        conn.close()


@app.post("/resume")
def resume():
    conn = repo.connect()
    try:
        repo.set_paused(conn, False)
        return {"ok": True, "paused": False}
    finally:
        conn.close()


@app.post("/close_all")
def close_all():
    """Sets the flag; the LOOP executes the buybacks on its next tick
    (within ~5s) so position writes stay with the single writer. Also pauses."""
    conn = repo.connect()
    try:
        repo.request_close_all(conn)
        return {"ok": True, "note": "loop will buy back all open positions on next tick"}
    finally:
        conn.close()


_chart_cache: dict = {"ts": 0.0, "data": None}


@app.get("/chart")
def chart(kline_limit: int = 288):
    """Spot candles per coin + open positions, for the dashboard chart.
    Cached 30s — the dashboard polls every 15s from every open tab."""
    import time as _t

    from services.bybit_client import bybit_client

    now = _t.time()
    if _chart_cache["data"] is not None and now - _chart_cache["ts"] < 30:
        return _chart_cache["data"]
    conn = repo.connect()
    try:
        open_pos = repo.open_positions(conn)
    finally:
        conn.close()
    out = {"coins": {}, "positions": open_pos}
    for coin, spec in config.COIN_SPEC.items():
        klines = bybit_client.get_klines(spec["symbol"], "5", min(kline_limit, 1000))
        out["coins"][coin] = {
            "klines": klines,
            "spot": klines[-1]["close"] if klines else None,
        }
    _chart_cache.update(ts=now, data=out)
    return out


@app.get("/positions")
def positions(limit: int = 50):
    conn = repo.connect()
    try:
        open_pos = repo.open_positions(conn)
        recent = repo.recent_positions(conn, limit)
    finally:
        conn.close()
    # Enrich open positions with live mark-to-market so the dashboard's
    # global Active-Contracts rail can show Jony's unrealized PnL like the
    # other bots. Same formula as the loop's equity snapshot:
    # unrealized = (entry_credit - mark) * qty. Best-effort, read-only.
    from services.bybit_client import bybit_client
    try:
        marks_by_coin = {c: bybit_client.get_option_marks(c)
                         for c in {p["coin"] for p in open_pos}}
    except Exception:
        marks_by_coin = {}
    for p in open_pos:
        m = marks_by_coin.get(p["coin"], {}).get(p["option_symbol"])
        if m and m.get("mark"):
            mark = m["mark"]
            p["current_mark_usd"] = round(mark, 6)
            p["unrealized_pnl_usd"] = round((p["entry_credit"] - mark) * p["qty"], 4)
        else:
            p["current_mark_usd"] = None
            p["unrealized_pnl_usd"] = None
    return {"open": open_pos, "recent": recent}


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
