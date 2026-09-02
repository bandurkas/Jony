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
                  strike: float, premium: float, lot: float,
                  size_mult: float = 1.0) -> tuple[float, float]:
    """Returns (qty, margin_usd); qty=0 → margin-blocked. Compounding: budget
    scales with current equity, capped by free portfolio margin. size_mult:
    extra multiplier on the per-trade budget (near-high sizing, Phase 10b)."""
    free = max(0.0, equity * config.PORT_MARGIN_CAP - used_margin)
    dyn = dyn_size_factor(recent_pnls) * size_mult
    budget = min(equity * config.MARGIN_PCT_PER_TRADE * dyn, free)
    m_lot = margin_per_lot(strike, premium, lot)
    if m_lot <= 0:
        return 0.0, 0.0
    n_lots = int(budget // m_lot)
    if n_lots < 1:
        return 0.0, 0.0
    return n_lots * lot, n_lots * m_lot


def trail_exit_due(peak_pct: float, pnl_pct: float,
                   arm: float = config.TRAIL_ARM,
                   giveback: float = config.TRAIL_GIVEBACK) -> bool:
    """Trailing profit-lock: once mark-based profit has reached `arm`
    (fraction of credit), exit when it retraces `giveback` from its peak.
    Pure function — posture gating happens at the call site."""
    return peak_pct >= arm and pnl_pct <= peak_pct - giveback


def effective_posture(posture: str, updated_ms: int, now_ms: int) -> str:
    """Degrade a stale lockdown to tight: lockdown blocks entries, so a dead
    advisor must not freeze the bot forever. Tight only tightens exits —
    fail-safe direction — so it may persist stale. Stale 'normal' (advisor
    dead > ADVISOR_STALE_H, heartbeat present) degrades to tight too, so
    the trail layer stays armed without an advisor."""
    stale_h = (now_ms - updated_ms) / 3_600_000
    if posture == "lockdown" and stale_h > config.LOCKDOWN_STALE_H:
        return "tight"
    # 2026-09-02: dead advisor at 'normal' left the bot without the trail
    # layer for 9h unnoticed. updated_ms==0 = advisor never ran (no guard).
    if posture == "normal" and updated_ms > 0 and \
            config.ADVISOR_STALE_H > 0 and stale_h > config.ADVISOR_STALE_H:
        return "tight"
    return posture if posture in ("normal", "tight", "lockdown") else "normal"


def account_breaker(closed: list[tuple[int, float]], equity: float,
                    now_ms: int) -> str | None:
    """Account-level mechanical brake on realized PnL: block reason or None.
    closed = [(closed_at_ms, pnl_usd), ...]. Fail-closed on unusable equity."""
    if not equity or equity <= 0:
        return "acct_cb_bad_equity"
    day = sum(p for ts, p in closed if now_ms - ts <= 24 * 3_600_000)
    week = sum(p for ts, p in closed if now_ms - ts <= 7 * 24 * 3_600_000)
    if day <= -config.ACCT_CB_DAILY_PCT * equity:
        return "acct_cb_daily"
    if week <= -config.ACCT_CB_WEEKLY_PCT * equity:
        return "acct_cb_weekly"
    tail = sorted(closed)[-config.ACCT_CB_STREAK_N:]
    if (len(tail) == config.ACCT_CB_STREAK_N
            and all(p < 0 for _, p in tail)
            and now_ms - tail[-1][0] <= config.ACCT_CB_STREAK_BLOCK_H * 3_600_000):
        return "acct_cb_streak"
    return None


def can_open(open_pos: list[dict], coin: str, side: str) -> str | None:
    """None = allowed; otherwise the block reason."""
    if len(open_pos) >= config.MAX_OPEN_POSITIONS:
        return "max_open_positions"
    if sum(1 for p in open_pos if p["coin"] == coin) >= config.PER_COIN_CAP:
        return "per_coin_cap"
    if sum(1 for p in open_pos
           if p["coin"] == coin and p["side"] == side) >= config.PER_KEY_CAP:
        return "per_key_cap"
    return None


def sl_pct_effective(sl_pct: float, equity: float, credit: float, qty: float,
                     cap_pct: float | None = None) -> float:
    """SL in $ never exceeds cap_pct × equity (decision 2026-08-26). Never
    loosens the strategy SL; cap_pct<=0 or bad inputs → unchanged."""
    if cap_pct is None:
        cap_pct = config.SL_EQUITY_CAP_PCT
    if cap_pct <= 0 or equity <= 0 or credit <= 0 or qty <= 0:
        return sl_pct
    return min(sl_pct, cap_pct * equity / (credit * qty))


def near_high_mult(side: str, dist_7d_high_pct: float | None,
                   pct: float | None = None, mult: float | None = None) -> float:
    """Put opened within `pct` of the 7d high → size × mult. Off when pct<=0
    or distance unknown (fail-open to the validated base sizing). Defaults
    are read at call time so env/tests can change them after import."""
    if pct is None:
        pct = config.NEAR_HIGH_PCT
    if mult is None:
        mult = config.NEAR_HIGH_MULT
    if pct <= 0 or side != "P" or dist_7d_high_pct is None:
        return 1.0
    return mult if dist_7d_high_pct > -pct else 1.0
