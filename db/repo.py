"""SQLite repo for Jony. Single-writer convention: only the loop process
writes; the API opens its own read connections (WAL allows concurrent
readers). Pattern proven by Tyagach."""
from __future__ import annotations

import json
import os
import sqlite3

DB_PATH = os.environ.get(
    "JONY_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "data", "jony.db"))


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    schema = open(os.path.join(os.path.dirname(__file__), "schema.sql")).read()
    conn.executescript(schema)
    conn.commit()


def init_state(conn: sqlite3.Connection, start_equity: float, now_ms: int) -> dict:
    row = conn.execute("SELECT * FROM bot_state WHERE id=1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO bot_state (id, started_at_ms, start_equity_usd, equity_usd)"
            " VALUES (1, ?, ?, ?)", (now_ms, start_equity, start_equity))
        conn.execute("INSERT OR IGNORE INTO bot_control (id, paused) VALUES (1, 0)")
        conn.commit()
        row = conn.execute("SELECT * FROM bot_state WHERE id=1").fetchone()
    return dict(row)


def get_state(conn: sqlite3.Connection) -> dict:
    return dict(conn.execute("SELECT * FROM bot_state WHERE id=1").fetchone())


def update_state(conn: sqlite3.Connection, **fields) -> None:
    keys = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE bot_state SET {keys} WHERE id=1", tuple(fields.values()))
    conn.commit()


def is_paused(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT paused FROM bot_control WHERE id=1").fetchone()
    return bool(row and row["paused"])


def open_positions(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM positions WHERE status='open' ORDER BY opened_at_ms")]


def insert_position(conn: sqlite3.Connection, p: dict) -> int:
    keys = ",".join(p)
    marks = ",".join("?" * len(p))
    cur = conn.execute(f"INSERT INTO positions ({keys}) VALUES ({marks})",
                       tuple(p.values()))
    conn.commit()
    return cur.lastrowid


def close_position(conn: sqlite3.Connection, pos_id: int, *, status: str,
                   closed_at_ms: int, exit_debit: float, exit_reason: str,
                   pnl_pct: float, pnl_usd: float) -> None:
    conn.execute(
        "UPDATE positions SET status=?, closed_at_ms=?, exit_debit=?,"
        " exit_reason=?, pnl_pct=?, pnl_usd=? WHERE id=?",
        (status, closed_at_ms, exit_debit, exit_reason, pnl_pct, pnl_usd, pos_id))
    conn.commit()


def insert_equity_snapshot(conn: sqlite3.Connection, ts_ms: int, equity: float,
                           unrealized: float, n_open: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO equity_snapshots (ts_ms, equity_usd, unrealized_usd,"
        " open_positions) VALUES (?, ?, ?, ?)", (ts_ms, equity, unrealized, n_open))
    conn.commit()


def insert_signal_audit(conn: sqlite3.Connection, ts_ms: int, coin: str | None,
                        active_side: str | None, accepted: bool | None,
                        reject_reason: str | None, spot: float | None,
                        payload: dict | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO signal_audit (ts_ms, coin, active_side, accepted,"
        " reject_reason, spot, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts_ms, coin, active_side,
         None if accepted is None else int(accepted),
         reject_reason, spot, json.dumps(payload) if payload else None))
    conn.commit()


def recent_positions(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM positions ORDER BY opened_at_ms DESC LIMIT ?", (limit,))]


def equity_series(conn: sqlite3.Connection, limit: int = 2000) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM equity_snapshots ORDER BY ts_ms DESC LIMIT ?", (limit,))][::-1]
