-- Jony SQLite schema. Single-writer (loop); API reads via own connections (WAL).

CREATE TABLE IF NOT EXISTS bot_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    started_at_ms INTEGER NOT NULL,
    start_equity_usd REAL NOT NULL,
    equity_usd REAL NOT NULL,
    cb_cooldown_until_ms INTEGER NOT NULL DEFAULT 0,
    recent_pnls_json TEXT NOT NULL DEFAULT '[]',
    last_fired_json TEXT NOT NULL DEFAULT '{}'   -- {"ETH:P": ts_ms, ...} cooldowns
);

CREATE TABLE IF NOT EXISTS bot_control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    paused INTEGER NOT NULL DEFAULT 0
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
    status TEXT NOT NULL DEFAULT 'open',         -- open / closed_tp2 / closed_sl / closed_time / closed_manual
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
