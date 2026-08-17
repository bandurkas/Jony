"""Контрфактуал: «а если бы advisor работал с первого дня Jony?»

Три мира на РЕАЛЬНЫХ 61 сделках из live-БД (research/live_dump/jony.db):
  W0  факт: +$65.18 (advisor жил только 14-16.08, закрыл 11 позиций)
  W1  advisor v1 с 7 июля: к 50 нетронутым сделкам применяется его
      эмпирическое правило закрытий (из 11 реальных закрытий 14-15.08):
        harvest  pnl>=+30% кредита         (реально брал 33-36%)
        danger   |spot/K-1|<=0.5%          (46-48 @0.0%, 60 @0.36%, 61 @0.24%)
        losscut  ITM и pnl<=-25%           (50: советы с -20%, исполнение -33%)
      персистентность 2 часовых тика (его CLOSE-persistence=2).
      Entry-вклад = 0: за 96 тиков жизни он не предложил НИ ОДНОГО входа.
  W2  совсем без советника: его 11 закрытий держим до механического выхода
      (tp2/sl/time_stop с параметрами из строки БД).

Модель марок: BS на 1h-свечах, сигма = implied из РЕАЛЬНОГО entry_credit
(модель совпадает с реальностью в точке входа; сигма фиксирована на жизнь
позиции). Комиссии = фактические комиссии той же сделки. Buyback = mid*(1+hs).
Калибровка: механическая симуляция 50 нетронутых сделок против их
фактического pnl -> оценка модельной ошибки.
"""
from __future__ import annotations
import json, math, sqlite3, datetime, itertools

HS = 0.01  # half-spread (engine SPREAD_PCT=2.0)
DATA = "/Users/styserg/Desktop/Jony/research/fresh_data"
DB = "/Users/styserg/Desktop/Jony/research/live_dump/jony.db"


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(side: str, S: float, K: float, T: float, sigma: float) -> float:
    if T <= 0:
        return max(0.0, S - K) if side == "C" else max(0.0, K - S)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / sq
    d2 = d1 - sq
    if side == "C":
        return S * norm_cdf(d1) - K * norm_cdf(d2)
    return K * norm_cdf(-d2) - S * norm_cdf(-d1)


def implied_sigma(side: str, S: float, K: float, T: float, target_mid: float) -> float:
    lo, hi = 0.03, 4.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_price(side, S, K, T, mid) < target_mid:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def load_klines(coin: str) -> tuple[list[int], list[float]]:
    rows = json.load(open(f"{DATA}/{coin.lower()}_1h.json"))
    return [r["start_ms"] for r in rows], [r["close"] for r in rows]


KL = {c: load_klines(c) for c in ("ETH", "BTC")}


def spot_at(coin: str, ts_ms: int) -> float:
    """close последнего часа <= ts; за пределами данных — последний бар."""
    ts_list, cl = KL[coin]
    lo, hi = 0, len(ts_list) - 1
    if ts_ms <= ts_list[0]:
        return cl[0]
    if ts_ms >= ts_list[-1]:
        return cl[-1]
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ts_list[mid] <= ts_ms:
            lo = mid
        else:
            hi = mid - 1
    return cl[lo]


def year_frac(ms: float) -> float:
    return ms / (1000 * 3600 * 24 * 365)


class Pos:
    def __init__(self, r: dict):
        self.__dict__.update(r)
        self.fee_total = (r["entry_credit"] - r["exit_debit"]) * r["qty"] - r["pnl_usd"]
        mid0 = r["entry_credit"] / (1 - HS)  # entry по bid
        T0 = year_frac(r["expiry_ms"] - r["opened_at_ms"])
        self.sigma = implied_sigma(r["side"], r["underlying_at_open"], r["strike"], T0, mid0)

    def mark(self, ts_ms: int) -> float:
        S = spot_at(self.coin, ts_ms)
        T = year_frac(max(0, self.expiry_ms - ts_ms))
        return bs_price(self.side, S, self.strike, T, self.sigma)

    def pnl_at(self, ts_ms: int) -> float:
        buyback = self.mark(ts_ms) * (1 + HS)
        return (self.entry_credit - buyback) * self.qty - self.fee_total


def sim_mechanical(p: Pos) -> tuple[float, int, str]:
    """tp2/sl/time_stop на часовой сетке от открытия (параметры из строки БД)."""
    end_ms = p.opened_at_ms + int(p.hold_h * 3600_000)
    tp2_mid = p.entry_credit * (1 - p.tp2_pct) / (1 + HS)
    sl_mid = p.entry_credit * (1 + p.sl_pct) / (1 + HS)
    t = p.opened_at_ms + 3600_000
    while t < end_ms:
        m = p.mark(t)
        if m >= sl_mid:
            return (-p.sl_pct * p.entry_credit * p.qty - p.fee_total, t, "sl")
        if m <= tp2_mid:
            return (p.tp2_pct * p.entry_credit * p.qty - p.fee_total, t, "tp2")
        t += 3600_000
    return (p.pnl_at(end_ms), end_ms, "time_stop")


def sim_advisor(p: Pos, harvest: float, buffer_pct: float, persist: int,
                user_close_ms: int | None) -> tuple[float, int, str]:
    """Механика + advisor-proxy поверх. user_close_ms: фактический ручной
    close-all юзера — если советник не сработал раньше, выходим как в факте."""
    end_ms = p.opened_at_ms + int(p.hold_h * 3600_000)
    if user_close_ms:
        end_ms = min(end_ms, user_close_ms)
    tp2_mid = p.entry_credit * (1 - p.tp2_pct) / (1 + HS)
    sl_mid = p.entry_credit * (1 + p.sl_pct) / (1 + HS)
    streak = 0
    t = p.opened_at_ms + 3600_000
    while t < end_ms:
        m = p.mark(t)
        if m >= sl_mid:
            return (-p.sl_pct * p.entry_credit * p.qty - p.fee_total, t, "sl")
        if m <= tp2_mid:
            return (p.tp2_pct * p.entry_credit * p.qty - p.fee_total, t, "tp2")
        S = spot_at(p.coin, t)
        pnl_pct = (p.entry_credit - m) / p.entry_credit
        itm = (S < p.strike) if p.side == "P" else (S > p.strike)
        cond = (pnl_pct >= harvest
                or abs(S / p.strike - 1) <= buffer_pct
                or (itm and pnl_pct <= -0.25))
        streak = streak + 1 if cond else 0
        if streak >= persist:
            return (p.pnl_at(t), t, "advisor")
        t += 3600_000
    if user_close_ms and end_ms == user_close_ms:
        return (p.pnl_at(end_ms), end_ms, "user_close")
    return (p.pnl_at(end_ms), end_ms, "time_stop")


def d(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%m-%d %H:%M")


conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = [dict(r) for r in conn.execute("SELECT * FROM positions ORDER BY id")]
pos = [Pos(r) for r in rows]
ADVISOR_IDS = {46, 47, 48, 49, 50, 56, 57, 58, 59, 60, 61}
CLOSE_ALL = {4, 5, 6, 19, 21}  # ручные close-all юзера (07-14, 07-30)

w0 = sum(r["pnl_usd"] for r in rows)
print(f"W0 факт: {w0:+.2f}\n")

# ── Калибровка: механика по модели vs факт на нетронутых механических сделках
print("── Калибровка модели (нетронутые механические сделки) ──")
errs = []
for p in pos:
    if p.id in ADVISOR_IDS or p.id in CLOSE_ALL or p.status == "closed_manual":
        continue
    mpnl, mts, mkind = sim_mechanical(p)
    errs.append(mpnl - p.pnl_usd)
n = len(errs)
tot_err = sum(errs)
mae = sum(abs(e) for e in errs) / n
print(f"n={n}  Σмодель-Σфакт={tot_err:+.2f}$  MAE={mae:.2f}$/сделку\n")

# ── W2: совсем без советника (11 его закрытий -> механика)
print("── W2: без советника вовсе (его 11 закрытий держим до механики) ──")
delta2 = 0.0
for p in pos:
    if p.id not in ADVISOR_IDS:
        continue
    mpnl, mts, mkind = sim_mechanical(p)
    delta2 += mpnl - p.pnl_usd
    print(f"  id{p.id:2d} {p.coin}:{p.side} факт {p.pnl_usd:+7.2f} (advisor) -> механика {mpnl:+7.2f} [{mkind} @ {d(mts)}]")
print(f"W2 = {w0 + delta2:+.2f}  (закрытия советника стоили {-delta2:+.2f}$)\n")

# ── W1: advisor v1 с первого дня — сетка чувствительности
print("── W1: advisor v1 с 7 июля (сетка параметров правила) ──")
results = {}
for harvest, buf, persist in itertools.product((0.30, 0.40), (0.0, 0.003, 0.005), (2, 3)):
    total = 0.0
    n_adv = 0
    for p in pos:
        if p.id in ADVISOR_IDS:
            total += p.pnl_usd  # уже мир советника
            continue
        uc = p.closed_at_ms if p.id in CLOSE_ALL else None
        apnl, ats, akind = sim_advisor(p, harvest, buf, persist, uc)
        total += apnl
        if akind == "advisor":
            n_adv += 1
    results[(harvest, buf, persist)] = (total, n_adv)
    print(f"  harvest>={harvest:.0%} buffer<={buf:.1%} persist={persist}:  W1={total:+8.2f}  (advisor-закрытий {n_adv}/50)")

vals = [v[0] for v in results.values()]
print(f"\nW1 диапазон: {min(vals):+.2f} … {max(vals):+.2f}  медиана {sorted(vals)[len(vals)//2]:+.2f}")

# ── Базовый вариант подробно (эмпирически ближайший к реальному поведению)
print("\n── W1 базовый (harvest 30%, buffer 0.5%, persist 2) по сделкам ──")
base_total = 0.0
for p in pos:
    if p.id in ADVISOR_IDS:
        base_total += p.pnl_usd
        continue
    uc = p.closed_at_ms if p.id in CLOSE_ALL else None
    apnl, ats, akind = sim_advisor(p, 0.30, 0.005, 2, uc)
    base_total += apnl
    tag = "" if akind != "advisor" else "  <== advisor режет"
    if akind == "advisor" or abs(apnl - p.pnl_usd) > 2:
        print(f"  id{p.id:2d} {p.coin}:{p.side} факт {p.pnl_usd:+7.2f} -> {apnl:+7.2f} [{akind} @ {d(ats)}, age {(ats-p.opened_at_ms)/3600_000:.0f}h]{tag}")
print(f"\nИТОГО: W0={w0:+.2f}  W1_база={base_total:+.2f}  W2={w0+delta2:+.2f}")
