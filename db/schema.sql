-- Jony SQLite schema. Single-writer (loop); API reads via own connections (WAL).

CREATE TABLE IF NOT EXISTS bot_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    started_at_ms INTEGER NOT NULL,
    start_equity_usd REAL NOT NULL,
    equity_usd REAL NOT NULL,
    cb_cooldown_until_ms INTEGER NOT NULL DEFAULT 0,  -- unused since the CB-isolation
                                                       -- change (2026-08-01) — kept so
                                                       -- old rows/backups still parse;
                                                       -- superseded by cb_until_json.
    cb_until_json TEXT NOT NULL DEFAULT '{}',     -- {"ETH:P": until_ms, ...} — per
                                                   -- (coin,side) circuit breaker, so a
                                                   -- losing streak on one leg doesn't
                                                   -- pause entries on the others (backtest:
                                                   -- +25% trades/day, holdout return/maxDD
                                                   -- both improved vs the old global CB —
                                                   -- see chat memory 2026-08-01)
    recent_pnls_json TEXT NOT NULL DEFAULT '[]',
    last_fired_json TEXT NOT NULL DEFAULT '{}'   -- {"ETH:P": ts_ms, ...} cooldowns
);

CREATE TABLE IF NOT EXISTS bot_control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    paused INTEGER NOT NULL DEFAULT 0,
    close_all_requested INTEGER NOT NULL DEFAULT 0,  -- API sets, loop executes+resets
    risk_posture TEXT NOT NULL DEFAULT 'normal',     -- normal | tight | lockdown;
                                                     -- advisor/API write, loop reads.
                                                     -- tight arms the trailing
                                                     -- profit-lock, lockdown also
                                                     -- closes profitable positions
                                                     -- and blocks new entries
    posture_updated_ms INTEGER NOT NULL DEFAULT 0    -- staleness guard: a lockdown
                                                     -- older than LOCKDOWN_STALE_H
                                                     -- degrades to tight so a dead
                                                     -- advisor can't freeze entries
                                                     -- forever
);

-- Partial (single-position) close requests: API inserts a row, loop deletes
-- it once executed (read-and-reset, same convention as close_all_requested,
-- but per-position and multi-row so more than one can queue between ticks).
-- Deliberately does NOT pause the bot or touch bot_control -- unlike Close
-- All (an emergency stop), closing one position is an ordinary risk-
-- management action that shouldn't halt new entries on the other legs.
CREATE TABLE IF NOT EXISTS close_requests (
    position_id INTEGER PRIMARY KEY,
    requested_at_ms INTEGER NOT NULL
);

-- Advisor-proposed entries: advisor inserts a row, loop pops it and runs the
-- SAME try_fire path as a mechanical signal (CB, cooldown, caps, margin all
-- re-checked by the single writer) with the advisor's rationale as the
-- signal payload (source=advisor -> per-source scoring in /advice/score).
CREATE TABLE IF NOT EXISTS entry_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    side TEXT NOT NULL,
    requested_at_ms INTEGER NOT NULL,
    payload TEXT                                 -- JSON advisor rationale
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    side TEXT NOT NULL,                          -- 'P' / 'C'
    option_symbol TEXT NOT NULL,
    strike REAL NOT NULL,
    expiry_ms INTEGER NOT NULL,
    qty REAL NOT NULL,
    opened_at_ms INTEGER NOT NULL,
    underlying_at_open REAL NOT NULL,
    entry_credit REAL NOT NULL,                  -- per contract, USD
    entry_source TEXT NOT NULL,                  -- 'bid' / 'mark_fallback'
    margin_usd REAL NOT NULL,
    fee_open_usd REAL NOT NULL,
    tp2_pct REAL NOT NULL,
    sl_pct REAL NOT NULL,
    hold_h INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',         -- open / closed_tp2 / closed_sl /
                                                 -- closed_time / closed_manual /
                                                 -- closed_trail (posture-driven
                                                 -- profit-lock exit)
    peak_profit_pct REAL NOT NULL DEFAULT 0,     -- running max of mark-based
                                                 -- pnl fraction while open;
                                                 -- feeds the trailing lock
    closed_at_ms INTEGER,
    exit_debit REAL,                             -- per contract, USD
    exit_reason TEXT,
    pnl_pct REAL,                                -- of premium
    pnl_usd REAL,
    signal_payload TEXT                          -- JSON gate snapshot at fire
);
CREATE INDEX IF NOT EXISTS positions_status ON positions(status, opened_at_ms DESC);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts_ms INTEGER PRIMARY KEY,
    equity_usd REAL NOT NULL,                    -- realized
    unrealized_usd REAL NOT NULL DEFAULT 0,
    open_positions INTEGER NOT NULL DEFAULT 0
);

-- Per-coin live debounce state, written by loop.py on every per-minute gate
-- check, read by api.py's /proximity endpoint (core/proximity.py). The API
-- process never sees loop.py's in-memory `win` dict directly (separate
-- process, read-only connection) -- this table is how the dashboard gauge
-- gets the same debounce/close-tick state the real entry decision uses,
-- same purpose as opt-app's window_status_json for Sniper1's gauge.
CREATE TABLE IF NOT EXISTS window_status (
    coin TEXT PRIMARY KEY,
    wid INTEGER NOT NULL,
    min_in_window INTEGER NOT NULL,
    disqualified INTEGER NOT NULL,
    ev_json TEXT NOT NULL,
    checked_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    coin TEXT,
    active_side TEXT,
    accepted INTEGER,                            -- 1/0/NULL
    reject_reason TEXT,
    spot REAL,
    payload TEXT                                 -- JSON full gate eval
);
CREATE INDEX IF NOT EXISTS signal_audit_recent ON signal_audit(ts_ms DESC);

-- P1 2026-08-17: реальная история марок открытых позиций (1 строка/мин/позицию
-- из manage_exits). Цель: честная калибровка сигмы и ре-тюн TP2/SL на
-- реальных премиях (модельная сигма структурно горячее markIv — RESEARCH_LOG).
CREATE TABLE IF NOT EXISTS position_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    pos_id INTEGER NOT NULL,
    option_symbol TEXT NOT NULL,
    mark REAL,
    bid REAL,
    ask REAL,
    mark_iv REAL,
    underlying REAL,
    delta REAL,
    pnl_pct_mark REAL
);
CREATE INDEX IF NOT EXISTS position_marks_pos ON position_marks(pos_id, ts_ms);
-- дедуп по минуте на уровне БД: рестарты loop не задваивают выборку
-- (INSERT OR IGNORE в repo.insert_position_mark; ревью 2026-08-17)
CREATE UNIQUE INDEX IF NOT EXISTS position_marks_pos_minute
    ON position_marks(pos_id, (ts_ms / 60000));
