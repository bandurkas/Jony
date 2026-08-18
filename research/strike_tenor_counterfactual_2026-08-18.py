"""Контрфактуал выбора страйка/тенора на живой истории (2026-08-18).

Вопрос: если бы советник влиял на выбор страйка и экспирации, было бы это
профитнее текущего ATM/nearest-to-168h? LLM задним числом не моделируем
(знает историю) — моделируем СЕМЕЙСТВО правил, доступных советнику,
модель-к-модели (методика Phase 9: BS на 1h свечах, сигма = implied из
РЕАЛЬНОГО entry_credit каждой сделки, те же комиссии; baseline в той же
модели воспроизводит факт с ошибкой ~+0.76$ на 61 сделке).

Фиксировано: моменты входов = реальные 63 входа бота, qty = фактическая,
механические выходы tp2/sl/hold из строки БД. Варьируем ТОЛЬКО контракт:
  страйк: ATM / 1% / 2% / 3% OTM (в безопасную сторону), округление
          к STRIKE_ROUND (ETH 25, BTC 500)
  тенор:  ближайшая пятница к цели 72ч / 168ч (live) / 336ч
Ограничение честно: плоская вола (без skew) — OTM-путы в реальности дают
БОЛЬШЕ премии, чем даёт плоский BS (skew), но и хвосты фактических путей
уже в данных; направление смещения указано в выводах.
"""
from __future__ import annotations

import bisect
import json
import math
import sqlite3
import time

HS = 0.03
FEE_RATE, FEE_CAP = 3e-4, 0.125
STRIKE_ROUND = {"ETH": 25.0, "BTC": 500.0}
DB = "live_dump/jony_2026-08-18.db"
NOW = int(time.time() * 1000)


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(side, S, K, T, sig):
    if T <= 0:
        return max(0.0, (S - K) if side == "C" else (K - S))
    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return S * ncdf(d1) - K * ncdf(d2) if side == "C" else K * ncdf(-d2) - S * ncdf(-d1)


def implied(side, S, K, T, target):
    lo, hi = 0.05, 3.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs(side, S, K, T, mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


KL = {}
for c in ("ETH", "BTC"):
    rows = json.load(open(f"fresh_data/{c.lower()}_1h.json"))
    KL[c] = ([r["start_ms"] for r in rows], [r["close"] for r in rows])


def spot_at(c, ts):
    t, cl = KL[c]
    i = bisect.bisect_right(t, ts) - 1
    return cl[max(0, min(i, len(cl) - 1))]


yf = lambda ms: ms / (1000 * 3600 * 24 * 365)


def fridays_0800(after_ms):
    """Ближайшие пятницы 08:00 UTC после after_ms (сетка недельных Bybit)."""
    t = after_ms
    out = []
    for _ in range(40):
        d = time.gmtime(t / 1000)
        days_ahead = (4 - d.tm_wday) % 7
        cand = time.mktime((d.tm_year, d.tm_mon, d.tm_mday + days_ahead,
                            8, 0, 0, 0, 0, 0)) - time.timezone
        cand_ms = int(cand * 1000)
        if cand_ms > after_ms and (not out or cand_ms != out[-1]):
            out.append(cand_ms)
        t += 86_400_000 * (days_ahead + 1 if days_ahead else 1)
        if len(out) >= 5:
            break
    return out


def fee(notional, premium_total):
    return min(notional * FEE_RATE, abs(premium_total) * FEE_CAP)


def sim(side, coin, open_ms, K, exp_ms, sigma, qty, tp2, sl, hold_h):
    """Механический выход на часовой сетке; экспирация раньше hold —
    settlement по intrinsic. Возврат (pnl_usd, resolution)."""
    S0 = spot_at(coin, open_ms)
    T0 = yf(exp_ms - open_ms)
    mid0 = bs(side, S0, K, T0, sigma)
    if mid0 <= 0.5:
        return None  # премия мусорная — такой контракт бот бы не продал
    credit = mid0 * (1 - HS)
    notional = K * qty
    fees0 = fee(notional, credit * qty)
    tp2_mid = credit * (1 - tp2) / (1 + HS)
    sl_mid = credit * (1 + sl) / (1 + HS)
    end = min(open_ms + int(hold_h * 3600_000), exp_ms, NOW)
    t = open_ms + 3600_000
    while t < end:
        m = bs(side, spot_at(coin, t), K, yf(max(0, exp_ms - t)), sigma)
        if m >= sl_mid:
            return (-sl * credit * qty - fees0 - fee(notional, sl_mid * (1 + HS) * qty), "sl")
        if m <= tp2_mid:
            return (tp2 * credit * qty - fees0 - fee(notional, tp2_mid * (1 + HS) * qty), "tp2")
        t += 3600_000
    if end == exp_ms:  # дожили до экспирации — интринсик, без спреда/комиссии выкупа
        S = spot_at(coin, end)
        intr = max(0.0, (S - K) if side == "C" else (K - S))
        return ((credit - intr) * qty - fees0, "expiry")
    m = bs(side, spot_at(coin, end), K, yf(max(0, exp_ms - end)), sigma)
    tag = "time_stop" if end < NOW else "mtm_now"
    return ((credit - m * (1 + HS)) * qty - fees0 - fee(notional, m * (1 + HS) * qty), tag)


conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
POS = [dict(r) for r in conn.execute(
    "SELECT * FROM positions ORDER BY opened_at_ms")]
print(f"входов: {len(POS)}, период "
      f"{time.strftime('%d.%m', time.gmtime(POS[0]['opened_at_ms'] / 1000))}–"
      f"{time.strftime('%d.%m', time.gmtime(POS[-1]['opened_at_ms'] / 1000))}, "
      f"факт реализовано: ${sum(p['pnl_usd'] or 0 for p in POS):+.2f}")

OFFSETS = (0.0, 0.01, 0.02, 0.03)
TARGETS = (72, 168, 336)
per_trade: dict[tuple, list] = {}
for p in POS:
    S0 = p["underlying_at_open"]
    T_act = yf(p["expiry_ms"] - p["opened_at_ms"])
    sigma = implied(p["side"], S0, p["strike"], T_act, p["entry_credit"] / (1 - HS))
    exps = fridays_0800(p["opened_at_ms"] + 6 * 3600_000)
    for off in OFFSETS:
        raw = S0 * (1 - off) if p["side"] == "P" else S0 * (1 + off)
        K = round(raw / STRIKE_ROUND[p["coin"]]) * STRIKE_ROUND[p["coin"]]
        for tgt in TARGETS:
            exp_ms = min(exps, key=lambda e: abs(e - p["opened_at_ms"] - tgt * 3600_000))
            r = sim(p["side"], p["coin"], p["opened_at_ms"], K, exp_ms, sigma,
                    p["qty"], p["tp2_pct"], p["sl_pct"], p["hold_h"])
            per_trade.setdefault((off, tgt), []).append(
                {"id": p["id"], "side": p["side"], "sigma": sigma, "r": r})

print(f"\n{'вариант':24s} {'n':>3s} {'skip':>4s} {'total $':>9s} "
      f"{'sl':>3s} {'tp2':>4s} {'expiry':>6s} {'wr%':>5s}")
base_key = (0.0, 168)
for (off, tgt), rows in sorted(per_trade.items(), key=lambda kv: (kv[0][1], kv[0][0])):
    done = [x for x in rows if x["r"] is not None]
    tot = sum(x["r"][0] for x in done)
    kinds = [x["r"][1] for x in done]
    wins = sum(1 for x in done if x["r"][0] > 0)
    mark = " <= LIVE" if (off, tgt) == base_key else ""
    print(f"OTM {off * 100:3.0f}% / цель {tgt:3d}ч   {len(done):3d} "
          f"{len(rows) - len(done):4d} {tot:+9.2f} {kinds.count('sl'):3d} "
          f"{kinds.count('tp2'):4d} {kinds.count('expiry'):6d} "
          f"{wins / len(done) * 100 if done else 0:5.1f}{mark}")

# условные правила «советника»: выбор per-trade из статических вариантов
med_sig = sorted(x["sigma"] for x in per_trade[base_key])[len(POS) // 2]


def rule_total(name, pick):
    tot, n = 0.0, 0
    for i in range(len(POS)):
        key = pick(per_trade[base_key][i])
        r = per_trade[key][i]["r"]
        if r is not None:
            tot += r[0]
            n += 1
    print(f"  {name:44s} n={n:3d} ${tot:+9.2f}")


print("\nусловные правила (выбор контракта per-trade):")
rule_total("vol-aware: sigma>медианы -> 2% OTM, иначе ATM",
           lambda x: (0.02, 168) if x["sigma"] > med_sig else (0.0, 168))
rule_total("путы 2% OTM, коллы ATM (тенор live)",
           lambda x: (0.02, 168) if x["side"] == "P" else (0.0, 168))
rule_total("короткий тенор при высокой воле (72ч), иначе live",
           lambda x: (0.0, 72) if x["sigma"] > med_sig else (0.0, 168))
rule_total("длинный тенор при высокой воле (336ч), иначе live",
           lambda x: (0.0, 336) if x["sigma"] > med_sig else (0.0, 168))
