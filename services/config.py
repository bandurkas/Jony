"""Jony runtime config — account/engine constants from the validated basket
backtest + env-driven deployment knobs. Strategy gates live in core/strategy.py."""
from __future__ import annotations

import os

TRADING_MODE = os.getenv("JONY_TRADING_MODE", "paper").strip().lower()

# ── Account engine (backtest-locked; do not tune without a new backtest) ──
START_EQUITY_USD = float(os.getenv("JONY_START_EQUITY_USD", "800"))
MARGIN_PCT_PER_TRADE = float(os.getenv("JONY_MARGIN_PCT", "0.15"))
MAX_OPEN_POSITIONS = int(os.getenv("JONY_MAX_OPEN", "4"))
PER_COIN_CAP = int(os.getenv("JONY_PER_COIN_CAP", "3"))
PORT_MARGIN_CAP = 0.80          # portfolio margin ceiling × equity
IM_RATE = 0.10                  # initial-margin approx: 10% of strike + premium
DYN_SIZE_WR_FLOOR = 0.40        # halve size when 10-trade WR under this
CB_CONSEC_LIMIT = 1             # circuit breaker: losses before pause
CB_PAUSE_HOURS = 8

# ── Entry mechanics (live Sniper1 conventions, validated) ──
FLICKER_TOLERANCE = 1           # tol1 debounce
ENTRY_FIRE_SECOND = 50          # fire near candle close
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BOT_TAG = "Jony"
