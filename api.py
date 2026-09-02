"""Read-only FastAPI for Jony (dashboard integration / health checks).
The loop is the single writer; this process only reads. CORS is open because
the opt-app dashboard fetches this API straight from the browser (same
pattern as Tyagach's :8100)."""
from __future__ import annotations

import json

import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from core import strategy
from core.proximity import entry_proximity
from db import repo
from services import config, portfolio

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
    conn = repo.connect()
    try:
        halted, why = repo.get_exec_halt(conn)
    finally:
        conn.close()
    return {"ok": True, "bot": config.BOT_TAG, "mode": config.TRADING_MODE,
            "testnet": config.BYBIT_TESTNET, "exec_halt": halted,
            "exec_halt_reason": why}


@app.post("/exec/unhalt")
def exec_unhalt():
    """Operator lifts an execution halt after checking the exchange book.
    bot_control is the one table the API may write (same as pause)."""
    conn = repo.connect()
    try:
        halted, why = repo.get_exec_halt(conn)
        repo.set_exec_halt(conn, False, None)
        n = repo.reset_close_attempts(conn)     # else next tick halts again (r3 int #2)
        return {"ok": True, "was_halted": halted, "reason": why, "close_attempts_reset": n}
    finally:
        conn.close()


@app.get("/orders")
def orders(limit: int = 50):
    conn = repo.connect()
    try:
        return {"active": repo.active_orders(conn),
                "recent": repo.recent_orders(conn, limit)}
    finally:
        conn.close()


@app.get("/iv")
def iv_recent(limit: int = 288):
    conn = repo.connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM iv_log ORDER BY ts_ms DESC LIMIT ?", (limit,))][::-1]
    finally:
        conn.close()


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
        posture, posture_ms = repo.get_risk_posture(conn)
        st["risk_posture"] = posture
        st["risk_posture_effective"] = portfolio.effective_posture(
            posture, posture_ms, int(time.time() * 1000))
        st["posture_updated_ms"] = posture_ms
        now = int(time.time() * 1000)
        st["acct_breaker"] = portfolio.account_breaker(
            repo.closed_pnls(conn, now - 30 * 24 * 3_600_000),
            st.get("equity_usd") or 0.0, now)

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


def _ser_gen(g: dict) -> dict:
    return {**g, "regime_filter": list(g["regime_filter"])}


@app.get("/params")
def params():
    """Strategy/account parameters — the dashboard renders these instead of
    hardcoding a copy that would drift from the bot."""
    return {
        "coins": {c: list(s) for c, s in strategy.COIN_SIDES.items()},
        # put_gen — легаси-ключ (ETH), фронт мигрирует на put_gen_by_coin
        "put_gen": _ser_gen(strategy.PUT_GEN),
        "put_gen_by_coin": {c: _ser_gen(g)
                            for c, g in strategy.PUT_GEN_BY_COIN.items()},
        "ret_7d_threshold": strategy.RET_7D_THRESHOLD,
        "call_gen": _ser_gen(strategy.CALL_GEN),
        "put_exit": strategy.PUT_EXIT,
        "call_exit": strategy.CALL_EXIT,
        "account": {
            "start_equity_usd": config.START_EQUITY_USD,
            "margin_pct_per_trade": config.MARGIN_PCT_PER_TRADE,
            "max_open_positions": config.MAX_OPEN_POSITIONS,
            "per_coin_cap": config.PER_COIN_CAP,
            "port_margin_cap": config.PORT_MARGIN_CAP,
            "cb_pause_hours": config.CB_PAUSE_HOURS,
            "cooldown_min": config.COOLDOWN_BARS * 5,
            "target_expiry_h": config.TARGET_EXPIRY_H,
        },
        "backtest": {
            "finding": "config C: puts-only, per-coin gates, ret7d 1.0, pkc=1 "
                       "(2026-08-17, честный v2, обе сигмы — RESEARCH_LOG Phase 7)",
            "train_return_pct": 22.8, "train_max_dd_pct": 21.9,
            "holdout_return_pct": 28.8, "holdout_max_dd_pct": 13.2,
            "trades_per_day": 0.25,   # CALIB (консервативная сигма); CLAMP: +62.2/+37.9
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


@app.post("/posture/{value}")
def set_posture(value: str):
    """Manual operator override of the advisor-managed risk posture."""
    if value not in ("normal", "tight", "lockdown"):
        raise HTTPException(400, "posture must be normal|tight|lockdown")
    conn = repo.connect()
    try:
        repo.set_risk_posture(conn, value, int(time.time() * 1000))
        return {"ok": True, "risk_posture": value}
    finally:
        conn.close()


@app.get("/proximity")
def proximity():
    """Entry-proximity gauge per coin (display-only — see core/proximity.py).
    Reads whatever loop.py's last per-minute gate check persisted; never
    recomputes klines/indicators itself (this process is read-only)."""
    conn = repo.connect()
    try:
        import time as _t
        now_ms = int(_t.time() * 1000)
        out = {}
        for coin in strategy.COIN_SIDES:
            ws = repo.get_window_status(conn, coin)
            if ws is None:
                out[coin] = entry_proximity({}, None, now_ms)
                out[coin]["active_side"] = None
                continue
            p = entry_proximity(ws["ev"], ws, now_ms)
            p["active_side"] = ws["ev"].get("active_side")
            out[coin] = p
        return out
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


@app.post("/close_position/{pos_id}")
def close_position(pos_id: int):
    """Partial close: queues a single-position buyback for the loop to
    execute on its next tick (~5s), same single-writer pattern as /close_all
    but scoped to one position and WITHOUT pausing the bot. 404s immediately
    if the position isn't currently open, so the dashboard doesn't show a
    stale "closing…" state for a position that already resolved."""
    conn = repo.connect()
    try:
        if repo.get_open_position(conn, pos_id) is None:
            raise HTTPException(status_code=404, detail=f"position {pos_id} not open")
        repo.request_close_position(conn, pos_id, int(time.time() * 1000))
        return {"ok": True, "note": "loop will buy back this position on next tick"}
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


@app.get("/advice/recent")
def advice_recent(limit: int = 24):
    """Newest-first advisor recommendations (written by advisor.py)."""
    import pathlib
    path = pathlib.Path("data/advice.jsonl")
    if not path.exists():
        return []
    lines = path.read_text().strip().splitlines()
    return [json.loads(ln) for ln in lines[-limit:]][::-1]


@app.get("/advice/score")
def advice_score():
    """Advisor self-scoring: for every past CLOSE/HOLD call on a position
    that has since closed, compare the MTM profit at advice time with the
    final realized pnl. saved_usd > 0 on a CLOSE call means following it
    would have kept money that was later given back; on a HOLD call
    saved_usd > 0 means holding earned more than exiting then."""
    import pathlib
    path = pathlib.Path("data/advice.jsonl")
    if not path.exists():
        return {"n_advices": 0, "details": []}
    # хвост файла: advice.jsonl только растёт (полный payload в каждой записи),
    # безлимитный парс со временем превысил бы таймауты потребителей (ревью 2026-08-17)
    records = [json.loads(ln)
               for ln in path.read_text().strip().splitlines()[-4000:]]
    conn = repo.connect()
    try:
        final = {r["id"]: dict(r) for r in conn.execute(
            "SELECT * FROM positions WHERE status != 'open'")}
    finally:
        conn.close()
    details = []
    for rec in records:
        by_id = {p["id"]: p for p in rec["input"].get("open_positions", [])}
        for adv in rec["advice"].get("positions", []):
            pos = final.get(adv["id"])
            snap = by_id.get(adv["id"])
            if not pos or not snap or snap.get("unrealized_usd") is None:
                continue
            at_advice = snap["unrealized_usd"]
            realized = pos["pnl_usd"]
            saved = (at_advice - realized if adv["action"] == "CLOSE"
                     else realized - at_advice)
            details.append({"ts_ms": rec["ts_ms"], "pos_id": adv["id"],
                            "action": adv["action"],
                            "unrealized_at_advice": at_advice,
                            "final_pnl": realized,
                            "saved_usd": round(saved, 2)})
    agg = {}
    for act in ("CLOSE", "HOLD", "WATCH"):
        rows = [d for d in details if d["action"] == act]
        if rows:
            agg[act] = {"n": len(rows),
                        "total_saved_usd": round(sum(d["saved_usd"] for d in rows), 2),
                        "hit_rate": round(sum(1 for d in rows if d["saved_usd"] > 0)
                                          / len(rows), 3)}

    # entry source comparison: advisor-proposed vs mechanical-signal entries
    conn = repo.connect()
    try:
        all_pos = [dict(r) for r in conn.execute("SELECT * FROM positions")]
    finally:
        conn.close()
    entries = {}
    for label, match in (("advisor", True), ("bot", False)):
        rows = [p for p in all_pos
                if ('"source": "advisor"' in (p.get("signal_payload") or "")) == match]
        closed_rows = [p for p in rows if p["status"] != "open"
                       and p["pnl_usd"] is not None]
        entries[label] = {
            "n_opened": len(rows), "n_closed": len(closed_rows),
            "pnl_usd": round(sum(p["pnl_usd"] for p in closed_rows), 2),
            "win_rate": round(sum(1 for p in closed_rows if p["pnl_usd"] > 0)
                              / len(closed_rows), 3) if closed_rows else None,
        }
    # per-key × source (2026-08-22: advisor-only коллы — kill-критерий
    # 8 закрытых с WR<40% или PnL<−$20 смотрится здесь)
    by_key: dict = {}
    for p in all_pos:
        if p["status"] == "open" or p["pnl_usd"] is None:
            continue
        src = "advisor" if '"source": "advisor"' in (p.get("signal_payload") or "") else "bot"
        k = by_key.setdefault(f"{p['coin']}:{p['side']}", {}).setdefault(
            src, {"n": 0, "wins": 0, "pnl_usd": 0.0})
        k["n"] += 1; k["wins"] += p["pnl_usd"] > 0; k["pnl_usd"] = round(k["pnl_usd"] + p["pnl_usd"], 2)
    return {"n_advices": len(records), "aggregate": agg, "entries": entries,
            "by_key": by_key, "confidence_calibration": confidence_calibration(all_pos),
            "details": details[-100:]}


def confidence_calibration(all_pos: list[dict]) -> dict | None:
    """Advisor entry confidence vs outcome (2026-09-02: live proposals all
    carried 0.72 = threshold-fitting). Brier over closed advisor entries;
    distinct_conf==1 means the number carries no information."""
    cal = []
    for p in all_pos:
        sp = p.get("signal_payload") or ""
        if p.get("status") == "open" or p.get("pnl_usd") is None or '"source": "advisor"' not in sp:
            continue
        try:
            c = json.loads(sp).get("confidence")
        except (ValueError, AttributeError):
            c = None
        if isinstance(c, (int, float)) and not isinstance(c, bool) and 0 <= c <= 1:
            cal.append((float(c), p["pnl_usd"] > 0))
    if not cal:
        return None
    n = len(cal)
    return {"n": n, "brier": round(sum((c - w) ** 2 for c, w in cal) / n, 3),
            "mean_conf": round(sum(c for c, _ in cal) / n, 3),
            "win_rate": round(sum(w for _, w in cal) / n, 3),
            "distinct_conf": len({round(c, 2) for c, _ in cal})}
