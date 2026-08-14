"""Jony runtime config — account/engine constants from the validated basket
backtest + env-driven deployment knobs. Strategy gates live in core/strategy.py."""
from __future__ import annotations

import os

TRADING_MODE = os.getenv("JONY_TRADING_MODE", "paper").strip().lower()

# ── Account engine (backtest-locked; do not tune without a new backtest) ──
START_EQUITY_USD = float(os.getenv("JONY_START_EQUITY_USD", "800"))
MARGIN_PCT_PER_TRADE = float(os.getenv("JONY_MARGIN_PCT", "0.15"))
# MAX_OPEN/PER_COIN_CAP raised 6->10 / 4->6 (2026-08-02): margin sizing
# (PORT_MARGIN_CAP below) was already the real risk budget — these count
# caps sat below what margin allows, throttling trades margin would have
# taken anyway. 10/6 is the elbow where the 2yr backtest's returns plateau
# (margin becomes binding beyond it): holdout +2440.5%->+6091.9%, aggregate
# holdout maxDD improves 8.6%->8.3%. Real live-signal-history replay (bot's
# actual trades since 2026-07-02, not synthetic) confirms with no tradeoff:
# 37->42 trades, win rate 73.0%->83.3%, return +45.0%->+76.0%, maxDD
# 4.1%->3.5%. See ~/Desktop/Jony/SESSION_HANDOFF_2026-08-02.md §10-11.
MAX_OPEN_POSITIONS = int(os.getenv("JONY_MAX_OPEN", "10"))
PER_COIN_CAP = int(os.getenv("JONY_PER_COIN_CAP", "6"))
# Max concurrent positions per (coin,side) key. The honest v2 engine showed
# same-key stacking is the account's dominant drawdown mechanism (65%->24%
# train / 42%->11% holdout DD at cap=1 — research/sweep_per_key_cap_v2.py);
# the live book independently reproduced the failure mode (6 identical ETH
# puts, then 76% of equity in one expiry). Default 1 since 2026-08-14.
PER_KEY_CAP = int(os.getenv("JONY_PER_KEY_CAP", "1"))
PORT_MARGIN_CAP = 0.80          # portfolio margin ceiling × equity
IM_RATE = 0.10                  # initial-margin approx: 10% of strike + premium
DYN_SIZE_WR_FLOOR = 0.40        # halve size when 10-trade WR under this
# CB arms unconditionally on any single losing close (loop.py::_close) — no
# consecutive-loss counter exists live or in the backtest engine
# (research/jony_engine.py::replay_account has the identical any-single-loss
# rule). A prior CB_CONSEC_LIMIT constant implied a tunable threshold that
# nothing ever read; removed 2026-08-02 rather than left as a misleading
# no-op (see ~/Desktop/Jony/SESSION_HANDOFF_2026-08-02.md §14).
CB_PAUSE_HOURS = 8

# ── Entry mechanics (live Sniper1 conventions, validated) ──
FLICKER_TOLERANCE = 1           # tol1 debounce
ENTRY_FIRE_SECOND = 50          # fire near candle close
FIVE_MIN = 5                    # window size (minutes) for the per-coin gate
                                 # debounce in loop.py; also the canonical
                                 # source for core/proximity.py's window_id()
                                 # so the live loop and the dashboard gauge
                                 # can never drift onto different window sizes
COOLDOWN_BARS = 6               # 30 min per (coin, side)
TARGET_EXPIRY_H = 168           # weekly options both sides — matches the
                                # backtest's credit pricing (hold_h caps the
                                # actual holding time per side)
MIN_EXPIRY_H = 6                # never open an option expiring sooner

# ── Coins (market conventions; verify against live chain on deploy) ──
COIN_SPEC = {
    "ETH": {"symbol": "ETHUSDT", "lot": 0.1},
    "BTC": {"symbol": "BTCUSDT", "lot": 0.01},
}

# ── Fees (Bybit options, same model as backtest) ──
FEE_RATE = 0.0003               # of underlying notional, per side
FEE_CAP_OF_PREMIUM = 0.125      # fee capped at 12.5% of premium

# ── Ops ──
LOOP_SLEEP_S = 5
KLINE_LIMIT_5M = 2200           # > BARS_7D=2016, headroom for ret_7d
KLINE_LIMIT_15M = 300
KLINE_LIMIT_1H = 300
EQUITY_SNAPSHOT_EVERY_MIN = 30
STUCK_SETTLEMENT_ALERT_MIN = 15  # alert once if a position can't settle
                                 # this long past expiry (Bybit outage) —
                                 # self-healing retry continues either way

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BOT_TAG = "Jony"

# ── Risk posture / trailing profit-lock (advisor Stage A, 2026-08-14) ──
# The UNCONDITIONAL trail failed its deploy gate (research/
# gate_trail_deploy_2026-08-14.py: cuts bleeding in bad regimes but loses to
# base exits on portfolio holdout return), so the trail arms ONLY in posture
# 'tight'/'lockdown', which the advisor sets when market risk is elevated —
# base exits stay untouched in 'normal'. Params = the (0.20, 0.10) point from
# the Phase-6 grid: earliest arming, fastest lock — the protective corner,
# appropriate because tight mode IS the elevated-risk state.
TRAIL_ARM = float(os.getenv("JONY_TRAIL_ARM", "0.20"))         # profit fraction of credit
TRAIL_GIVEBACK = float(os.getenv("JONY_TRAIL_GIVEBACK", "0.10"))  # retrace from peak
LOCKDOWN_STALE_H = float(os.getenv("JONY_LOCKDOWN_STALE_H", "4"))  # dead-advisor guard

# ── Account-level circuit breaker (2026-08-15) ──
# Mechanical brake on REALIZED PnL, computed in code above the LLM advisor:
# blocks new entries (bot + advisor via the shared try_fire path) and floors
# posture to 'tight'. Never force-closes. Rolling windows, stateless.
ACCT_CB_DAILY_PCT = float(os.getenv("JONY_ACCT_CB_DAILY_PCT", "0.025"))   # of equity / 24h
ACCT_CB_WEEKLY_PCT = float(os.getenv("JONY_ACCT_CB_WEEKLY_PCT", "0.05"))  # of equity / 7d
ACCT_CB_STREAK_N = int(os.getenv("JONY_ACCT_CB_STREAK_N", "3"))           # consecutive losses
ACCT_CB_STREAK_BLOCK_H = float(os.getenv("JONY_ACCT_CB_STREAK_BLOCK_H", "24"))
