"""Minimal Black-Scholes pricer. No scipy — uses math.erf for the standard normal CDF.

Crypto context: r ≈ 0 by default, q (dividend yield) = 0. Inputs:
  S = spot, K = strike, T = years to expiry, sigma = annualized vol, r = risk-free.
"""
from __future__ import annotations

import math


def _N(x: float) -> float:
    """Standard normal CDF using math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(S: float, K: float, T: float, sigma: float, r: float = 0.0) -> tuple[float, float]:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return float("nan"), float("nan")
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def price(side: str, S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    """Return option premium in USDT (assuming underlying is in USDT). `side` is 'C' or 'P'."""
    if T <= 0:
        # at expiry: intrinsic value
        if side == "C":
            return max(0.0, S - K)
        return max(0.0, K - S)
    d1, d2 = _d1_d2(S, K, T, sigma, r)
    if math.isnan(d1):
        return 0.0
    if side == "C":
        return S * _N(d1) - K * math.exp(-r * T) * _N(d2)
    return K * math.exp(-r * T) * _N(-d2) - S * _N(-d1)


def delta(side: str, S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    if T <= 0:
        if side == "C":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1, _ = _d1_d2(S, K, T, sigma, r)
    if math.isnan(d1):
        return 0.0
    return _N(d1) if side == "C" else _N(d1) - 1.0


def theta_per_day(side: str, S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    """Theta in premium-USDT per calendar day."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1, d2 = _d1_d2(S, K, T, sigma, r)
    pdf_d1 = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    first = -(S * pdf_d1 * sigma) / (2 * math.sqrt(T))
    if side == "C":
        theta_y = first - r * K * math.exp(-r * T) * _N(d2)
    else:
        theta_y = first + r * K * math.exp(-r * T) * _N(-d2)
    return theta_y / 365.0


def implied_spot(side: str, target_premium: float, K: float, T: float, sigma: float,
                 r: float = 0.0) -> float | None:
    """Inverse of price(): the spot S that makes price(side, S, K, T, sigma, r)
    equal target_premium, via bisection. price() is monotonic in S (calls
    increasing, puts decreasing), so a fixed-iteration bisection converges.
    Returns None if target_premium is outside the reachable range (e.g. T<=0
    or a premium beyond what any spot in the search window can produce) —
    callers should treat this as "no approx line to draw", not an error.
    """
    if T <= 0 or sigma <= 0 or K <= 0 or target_premium < 0:
        return None
    lo, hi = K * 0.1, K * 10.0
    f_lo = price(side, lo, K, T, sigma, r) - target_premium
    f_hi = price(side, hi, K, T, sigma, r) - target_premium
    if f_lo == 0:
        return lo
    if f_hi == 0:
        return hi
    if (f_lo > 0) == (f_hi > 0):
        return None  # target unreachable within [0.1K, 10K]
    for _ in range(60):
        mid = (lo + hi) / 2.0
        f_mid = price(side, mid, K, T, sigma, r) - target_premium
        if f_mid == 0:
            return mid
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return (lo + hi) / 2.0
