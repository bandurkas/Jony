"""Direct Python port of Jony's live strategy gates (core/strategy.py,
core/indicators.py, core/momentum_mtf.py, core/regime.py) and account engine
(services/portfolio.py, services/config.py), pulled verbatim from
/root/Jony on VPS3 2026-08-01. The original basket_premium_backtest.py that
validated the live config is missing (not in git, not on any VPS, not in
any docker image) — this is a from-spec reconstruction against the ACTUAL
DEPLOYED CODE (not a memory of it), so it should be trustworthy, but treat
the first run's baseline-reproduction check as the fidelity gate before
trusting any new-config conclusions drawn from it.
"""
from __future__ import annotations

import math
from typing import Sequence

# ───────────────────────── core/indicators.py ─────────────────────────

def ema(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: Sequence[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def adx(candles: list[dict], period: int = 14) -> float | None:
    if len(candles) < 2 * period + 2:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        h, low = candles[i]["high"], candles[i]["low"]
        ph, pl, pc = candles[i - 1]["high"], candles[i - 1]["low"], candles[i - 1]["close"]
        up = h - ph
        dn = pl - low
        plus_dm.append(up if up > dn and up > 0 else 0.0)
        minus_dm.append(dn if dn > up and dn > 0 else 0.0)
        trs.append(max(h - low, abs(h - pc), abs(low - pc)))
    atr_v = sum(trs[:period])
    plus_v = sum(plus_dm[:period])
    minus_v = sum(minus_dm[:period])
    dxs = []
    for i in range(period, len(trs)):
        atr_v = atr_v - atr_v / period + trs[i]
        plus_v = plus_v - plus_v / period + plus_dm[i]
        minus_v = minus_v - minus_v / period + minus_dm[i]
        if atr_v == 0:
            continue
        plus_di = 100 * plus_v / atr_v
        minus_di = 100 * minus_v / atr_v
        dx = 0.0 if plus_di + minus_di == 0 else 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        dxs.append(dx)
    if len(dxs) < period:
        return None
    a = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        a = (a * (period - 1) + dx) / period
    return a


def zscore(values: Sequence[float], lookback: int = 20) -> float | None:
    if len(values) < lookback + 1:
        return None
    sub = list(values[-lookback - 1:-1])
    mean = sum(sub) / len(sub)
    var = sum((v - mean) ** 2 for v in sub) / len(sub)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (values[-1] - mean) / std


def realized_vol(closes: Sequence[float], lookback: int = 24) -> float | None:
    if len(closes) < lookback + 1:
        return None
    rets = []
    for i in range(1, lookback + 1):
        if closes[-i - 1] <= 0:
            continue
        rets.append(math.log(closes[-i] / closes[-i - 1]))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var)
    return std * math.sqrt(24 * 365)


# ───────────────────────── core/momentum_mtf.py ─────────────────────────

def analyze_tf(candles: list[dict]) -> dict:
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    r = rsi(closes, 14)
    vol_z = zscore(volumes, 20)
    last_close = closes[-1] if closes else 0.0
    prev_close = closes[-2] if len(closes) >= 2 else last_close
    change_pct = (last_close - prev_close) / prev_close * 100 if prev_close else 0.0
    if e20 is None or e50 is None or r is None:
        direction = "unknown"
    elif e20 > e50 and r > 50:
        direction = "up"
    elif e20 < e50 and r < 50:
        direction = "down"
    else:
        direction = "neutral"
    return {"direction": direction, "change_pct": change_pct}


def consensus(tf_5m: dict, tf_15m: dict, tf_1h: dict) -> dict:
    tfs = [tf_5m, tf_15m, tf_1h]
    ups = sum(1 for tf in tfs if tf["direction"] == "up")
    downs = sum(1 for tf in tfs if tf["direction"] == "down")
    if ups >= 2 and ups > downs:
        direction, aligned = "up", ups
    elif downs >= 2 and downs > ups:
        direction, aligned = "down", downs
    else:
        direction, aligned = "neutral", max(ups, downs)
    return {"direction": direction, "tfs_aligned": aligned, "tf_5m": tf_5m, "tf_15m": tf_15m, "tf_1h": tf_1h}


def direction_filter_ok(mtf: dict, mtf_filter: str | None, anchor_tf: str | None = None, min_aligned: int = 2) -> bool:
    if mtf_filter is None:
        return True
    if anchor_tf == "1h":
        return mtf["tf_1h"]["direction"] == mtf_filter
    return mtf["direction"] == mtf_filter and mtf["tfs_aligned"] >= min_aligned


# ───────────────────────── core/regime.py ─────────────────────────

def detect_regime(candles_1h: list[dict]) -> dict:
    a = adx(candles_1h, 14)
    if a is None:
        return {"regime": "unknown", "adx": None}
    if a > 35:
        regime = "trend"
    elif a < 20:
        regime = "range"
    else:
        regime = "transition"
    return {"regime": regime, "adx": a}


# ───────────────────────── core/strategy.py ─────────────────────────

BARS_7D = 2016
RET_7D_THRESHOLD = 1.0  # synced to live 2026-08-17 (config C; было 0.5)
HIST = 240

# config C — deployed 2026-08-17 (per-coin PUT gates, RESEARCH_LOG Phase 7).
# PUT_GEN оставлен как legacy-«общий» дефолт для старых скриптов и равен
# ETH-конфигу; скрипты, которым нужен live-паритет по монетам, должны брать
# PUT_GEN_BY_COIN[coin] (как это делает live core/strategy.py).
PUT_GEN_BY_COIN = {
    "ETH": {"vol_threshold": 0.60, "regime_filter": ("range",),
            "mtf_direction_filter": "up", "mtf_anchor_tf": None, "bull_market_ratio_max": None},
    "BTC": {"vol_threshold": 0.40, "regime_filter": ("range", "transition"),
            "mtf_direction_filter": "up", "mtf_anchor_tf": None, "bull_market_ratio_max": None},
}
PUT_GEN = PUT_GEN_BY_COIN["ETH"]
CALL_GEN = {"vol_threshold": 0.45, "regime_filter": ("range", "transition", "trend"),
           "mtf_direction_filter": "down", "mtf_anchor_tf": "1h", "bull_market_ratio_max": 1.05}
# exit tune deployed 2026-08-01 (was PUT sl_pct=2.00/hold_h=96, CALL
# tp2_pct=0.80; see sweep_exits.py/validate_combined.py for the backtest).
PUT_EXIT = {"tp2_pct": 0.70, "sl_pct": 1.75, "hold_h": 120}
CALL_EXIT = {"tp2_pct": 0.70, "sl_pct": 0.75, "hold_h": 24}
COIN_SIDES = {"ETH": ("P", "C"), "BTC": ("C", "P")}  # BTC:P включён 2026-08-14

# CALIB markIv-сигма (fit 2026-08-02, sweep_sigma_calibration.py) — единая
# копия для всех research-скриптов вместо 8 разбросанных дублей (ревью 2026-08-17)
CALIB = {"b0": 0.3487, "b1": 0.2646, "floor": 0.25, "ceiling": 1.05}


def fixed_lot_pnl(t: dict) -> float:
    """Каноническая копия (была продублирована в 8 скриптах — ревью 2026-08-17)."""
    qty = t["lot"]
    notional = t["strike"] * qty
    premium_total = t["entry_credit"] * qty
    fee_open = fee_usd(notional, premium_total)
    exit_credit = t["entry_credit"] * (1 - t["pnl_pct"])
    fee_close = fee_usd(notional, exit_credit * qty)
    return (t["entry_credit"] - exit_credit) * qty - fee_open - fee_close


def gen_kwargs(side: str) -> dict:
    return PUT_GEN if side == "P" else CALL_GEN


def exit_params(side: str) -> dict:
    return PUT_EXIT if side == "P" else CALL_EXIT


def compute_ret_7d(k5: list, idx: int) -> float:
    if idx < BARS_7D:
        return 0.0
    prev_close = k5[idx - BARS_7D]["close"]
    if prev_close <= 0:
        return 0.0
    return (k5[idx]["close"] - prev_close) / prev_close * 100


def allowed_sides(coin: str, ret_7d: float) -> list[str]:
    if ret_7d > RET_7D_THRESHOLD:
        sides = ["P"]
    elif ret_7d < -RET_7D_THRESHOLD:
        sides = ["C"]
    else:
        sides = ["P", "C"]
    return [s for s in sides if s in COIN_SIDES[coin]]


def _evaluate_side(side: str, mtf: dict, regime: str, rolling_vols: list[float], closes_1h: list[float]) -> dict:
    kw = gen_kwargs(side)
    out = {"vol_high": False, "regime_ok": False, "mtf_direction_ok": False, "bull_filter_ok": True, "ready": False}
    if len(rolling_vols) >= 30:
        current_vol = rolling_vols[-1]
        sorted_vols = sorted(rolling_vols)
        threshold_idx = int(len(sorted_vols) * kw["vol_threshold"])
        threshold = sorted_vols[threshold_idx]
        out["vol_high"] = current_vol >= threshold
    out["regime_ok"] = regime in kw["regime_filter"]
    out["mtf_direction_ok"] = direction_filter_ok(mtf, kw["mtf_direction_filter"], kw["mtf_anchor_tf"])
    bull_max = kw["bull_market_ratio_max"]
    if bull_max is not None and len(closes_1h) >= 200:
        e50 = ema(closes_1h, 50)
        e200 = ema(closes_1h, 200)
        if e50 is not None and e200 not in (None, 0):
            out["bull_filter_ok"] = (e50 / e200) <= bull_max
    out["ready"] = out["vol_high"] and out["regime_ok"] and out["mtf_direction_ok"] and out["bull_filter_ok"]
    return out


def evaluate_conditions(coin: str, k5: list, k15: list, k1h: list) -> dict:
    """k5/k15/k1h are already-trimmed windows ending at the evaluation bar
    (caller passes k5[:idx+1] etc. — matches live's 'most recent N bars')."""
    out = {"ready": False, "active_side": None}
    if len(k5) < BARS_7D or len(k15) < 50 or len(k1h) < 200:
        return out
    idx = len(k5) - 1
    ret_7d = compute_ret_7d(k5, idx)
    sides = allowed_sides(coin, ret_7d)
    if not sides:
        return out
    s5, s15, s1h = k5[-HIST:], k15[-HIST:], k1h[-HIST:]
    mtf = consensus(analyze_tf(s5), analyze_tf(s15), analyze_tf(s1h))
    closes_1h = [c["close"] for c in s1h]
    rolling_vols = []
    for j in range(20, len(closes_1h)):
        rv = realized_vol(closes_1h[:j + 1], lookback=24)
        if rv is not None:
            rolling_vols.append(rv)
    regime = detect_regime(s1h).get("regime", "unknown")
    side_results = {s: _evaluate_side(s, mtf, regime, rolling_vols, closes_1h) for s in sides}
    active_side = next((s for s in sides if side_results[s]["ready"]), None)
    out["active_side"] = active_side
    out["ready"] = active_side is not None
    out["spot"] = k5[idx]["close"]
    return out


# ───────────────────────── services/config.py + portfolio.py ─────────────────────────

START_EQUITY_USD = 800.0
MARGIN_PCT_PER_TRADE = 0.15
MAX_OPEN_POSITIONS = 10  # bumped 6->10 2026-08-02, see sweep_position_caps.py
PER_COIN_CAP = 6         # bumped 4->6 2026-08-02, see sweep_position_caps.py
PORT_MARGIN_CAP = 0.80
IM_RATE = 0.10
DYN_SIZE_WR_FLOOR = 0.40
CB_CONSEC_LIMIT = 1
CB_PAUSE_HOURS = 8
COOLDOWN_BARS = 6  # 30 min at 5m bars
TARGET_EXPIRY_H = 168.0  # weekly option — the REAL tenor priced at entry,
                         # independent of how long we plan to hold it (hold_h)
FEE_RATE = 0.0003
FEE_CAP_OF_PREMIUM = 0.125
STRIKE_ROUND = {"ETH": 25.0, "BTC": 500.0}


def fee_usd(notional: float, premium_total: float) -> float:
    return min(notional * FEE_RATE, abs(premium_total) * FEE_CAP_OF_PREMIUM)


def dyn_size_factor(recent_pnls: list[float]) -> float:
    if len(recent_pnls) >= 10:
        wr = sum(1 for p in recent_pnls[-10:] if p > 0) / 10.0
        if wr < DYN_SIZE_WR_FLOOR:
            return 0.5
    return 1.0


def margin_per_lot(strike: float, premium: float, lot: float) -> float:
    return (IM_RATE * strike + premium) * lot


def size_position(equity: float, used_margin: float, recent_pnls: list[float],
                  strike: float, premium: float, lot: float, size_mult: float = 1.0) -> tuple[float, float]:
    """size_mult: research-only extra scale on top of dyn_size_factor (default
    1.0 = live behavior unchanged) — used to test regime-aware sizing (e.g.
    smaller CALL positions specifically when regime=='trend') without
    touching the live dyn_size_factor loss-streak logic."""
    free = max(0.0, equity * PORT_MARGIN_CAP - used_margin)
    dyn = dyn_size_factor(recent_pnls)
    budget = min(equity * MARGIN_PCT_PER_TRADE * dyn, free) * size_mult
    m_lot = margin_per_lot(strike, premium, lot)
    if m_lot <= 0:
        return 0.0, 0.0
    n_lots = int(budget // m_lot)
    if n_lots < 1:
        return 0.0, 0.0
    return n_lots * lot, n_lots * m_lot
