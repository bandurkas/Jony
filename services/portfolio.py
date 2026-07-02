"""Account-engine logic (pure functions): sizing, caps, CB, dyn-size, fees —
mirrors the basket backtest's replay_account exactly."""
from __future__ import annotations

from . import config


def fee_usd(notional: float, premium_total: float) -> float:
    return min(notional * config.FEE_RATE,
               abs(premium_total) * config.FEE_CAP_OF_PREMIUM)


def dyn_size_factor(recent_pnls: list[float]) -> float:
    """Halve size when last-10 win rate < 40% (pnls are per-trade fractions)."""
    if len(recent_pnls) >= 10:
        wr = sum(1 for p in recent_pnls[-10:] if p > 0) / 10.0
        if wr < config.DYN_SIZE_WR_FLOOR:
            return 0.5
    return 1.0


def cb_active(cb_until_ms: int, now_ms: int) -> bool:
    return now_ms < cb_until_ms


def margin_per_lot(strike: float, premium: float, lot: float) -> float:
    return (config.IM_RATE * strike + premium) * lot


def size_position(equity: float, used_margin: float, recent_pnls: list[float],
                  strike: float, premium: float, lot: float) -> tuple[float, float]:
    """Returns (qty, margin_usd); qty=0 → margin-blocked. Compounding: budget
    scales with current equity, capped by free portfolio margin."""
    free = max(0.0, equity * config.PORT_MARGIN_CAP - used_margin)
    dyn = dyn_size_factor(recent_pnls)
    budget = min(equity * config.MARGIN_PCT_PER_TRADE * dyn, free)
    m_lot = margin_per_lot(strike, premium, lot)
    if m_lot <= 0:
        return 0.0, 0.0
    n_lots = int(budget // m_lot)
    if n_lots < 1:
        return 0.0, 0.0
    return n_lots * lot, n_lots * m_lot


def can_open(open_pos: list[dict], coin: str) -> str | None:
    """None = allowed; otherwise the block reason."""
    if len(open_pos) >= config.MAX_OPEN_POSITIONS:
        return "max_open_positions"
    if sum(1 for p in open_pos if p["coin"] == coin) >= config.PER_COIN_CAP:
        return "per_coin_cap"
    return None
