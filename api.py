"""Read-only FastAPI for Jony (dashboard integration / health checks).
The loop is the single writer; this process only reads."""
from __future__ import annotations

import json

from fastapi import FastAPI

from db import repo
from services import config

app = FastAPI(title="Jony", version="1.0")


@app.get("/health")
def health():
    return {"ok": True, "bot": config.BOT_TAG, "mode": config.TRADING_MODE}


@app.get("/state")
def state():
    conn = repo.connect()
    try:
        st = repo.get_state(conn)
        st["recent_pnls_json"] = json.loads(st["recent_pnls_json"])
        st["last_fired_json"] = json.loads(st["last_fired_json"])
        st["paused"] = repo.is_paused(conn)
        return st
    finally:
        conn.close()


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
