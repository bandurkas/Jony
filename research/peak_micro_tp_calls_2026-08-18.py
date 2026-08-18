"""Пики MTM-профита CALL-сделок + свип микро-TP (2026-08-18).

Директива: анализировать не финальный PnL коллов, а ПИКОВЫЙ временный профит;
если сделки частые и микро-профит можно забирать часто — это валидная
стратегия вместо ожидания редких качественных путов.

Part A: распределение пиков. Live-гейты CALL_GEN, экзиты выключены
(tp2=9.9/sl=99), ход до hold_h; пик = max по бару mark-to-buyback
(favorable интрабарный экстремум — верхняя оценка забираемого).

Part B: свип микро-TP на TRAIN/CALIB only (протокол sweep_exits_v2):
tp2 0.10-0.35 x sl 0.30-0.75 x hold 12/24 по ETH:C и BTC:C. Выжившие —
отдельная валидация: holdout, CLAMP, кварталы, replay_v2 pkc=1 поверх
путовой книги C (дают ли коллы денег ПОРТФЕЛЮ, а не только фикс-лоту).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je
from replay_account_v2 import replay_v2

CALIB = {"b0": 0.3487, "b1": 0.2646, "floor": 0.25, "ceiling": 1.05}
MO, CAP, PKC = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP, 1
jc.COIN_SIDES = {"ETH": ("P", "C"), "BTC": ("C", "P")}


def fixed_lot_pnl(t: dict) -> float:
    qty = t["lot"]
    notional = t["strike"] * qty
    premium_total = t["entry_credit"] * qty
    fee_open = jc.fee_usd(notional, premium_total)
    exit_credit = t["entry_credit"] * (1 - t["pnl_pct"])
    fee_close = jc.fee_usd(notional, exit_credit * qty)
    return (t["entry_credit"] - exit_credit) * qty - fee_open - fee_close


def span_days(trades):
    if not trades:
        return 0.0
    return (trades[-1]["entry_ts"] - trades[0]["entry_ts"]) / 86_400_000 or 1.0


# ── Part A: peak distribution ──
_orig_sim = je.simulate_option_exit
PEAKS: list[dict] = []


def _sim_with_peak(side, entry_idx, close, high, low, start_ms, sigma,
                   tp2_pct, sl_pct, hold_h, strike_round,
                   expiry_h=jc.TARGET_EXPIRY_H, **kw):
    out = _orig_sim(side, entry_idx, close, high, low, start_ms, sigma,
                    tp2_pct, sl_pct, hold_h, strike_round,
                    expiry_h=expiry_h, **kw)
    if out is None:
        return None
    entry_credit, strike = out["entry_credit"], out["strike"]
    lo, hi = entry_idx + 1, min(entry_idx + 1 + int(hold_h * 12), len(close))
    m = hi - lo
    el = np.arange(1, m + 1) * 5 / 60
    T = np.maximum(0.0, (expiry_h - el) / (24 * 365))
    fav = low[lo:hi] if side == "C" else high[lo:hi]
    prem = je._vec_bs_price(side, fav, strike, T, sigma)
    path = (entry_credit - prem * (1 + je.HALF_SPREAD)) / entry_credit
    k = int(np.argmax(path))
    PEAKS.append({"entry_ts": int(start_ms[entry_idx]),
                  "peak_pct": float(path[k]), "t_peak_h": (k + 1) * 5 / 60,
                  "final_pct": out["pnl_pct"]})
    return out


def peak_report(coin: str, hold_h: int):
    PEAKS.clear()
    je.simulate_option_exit = _sim_with_peak
    try:
        trades = je.coin_trades(coin, sides_enabled=("C",), sigma_calib=CALIB,
                                call_exit={"tp2_pct": 9.9, "sl_pct": 99.0,
                                           "hold_h": hold_h})
    finally:
        je.simulate_option_exit = _orig_sim
    peaks = sorted(PEAKS, key=lambda p: p["entry_ts"])
    tr, ho = je.split(peaks, 0.70)
    for name, seg in (("train", tr), ("holdout", ho)):
        if not seg:
            continue
        n = len(seg)
        pk = sorted(p["peak_pct"] for p in seg)
        fin = sorted(p["final_pct"] for p in seg)
        tp = sorted(p["t_peak_h"] for p in seg)
        share = lambda x: sum(1 for p in seg if p["peak_pct"] >= x) / n * 100
        print(f"  {coin}:C hold={hold_h}h {name:8s} n={n:4d} "
              f"({n / (span_days(seg)):.2f}/д) | пик>=10% {share(0.10):4.0f}% "
              f">=20% {share(0.20):4.0f}% >=30% {share(0.30):4.0f}% "
              f">=50% {share(0.50):4.0f}% | медиана пика {pk[n // 2] * 100:+5.1f}% "
              f"t_peak {tp[n // 2]:4.1f}ч | медиана финала {fin[n // 2] * 100:+5.1f}% "
              f"средний финал {sum(fin) / n * 100:+5.1f}%")


print("=" * 100)
print("PART A: распределение пиков CALL-сделок (экзиты выключены, CALIB)")
print("=" * 100)
for coin in ("ETH", "BTC"):
    for hold in (24, 120):
        peak_report(coin, hold)

# ── Part B: micro-TP sweep, TRAIN/CALIB only ──
print()
print("=" * 100)
print("PART B: свип микро-TP по CALL (выбор только TRAIN/CALIB, fixed-lot)")
print("=" * 100)
TP2S = (0.10, 0.15, 0.20, 0.25, 0.30)
SLS = (0.30, 0.50, 0.75)
HOLDS = (12, 24)
results = {}
for coin in ("ETH", "BTC"):
    rows = []
    for tp2 in TP2S:
        for sl in SLS:
            for hold in HOLDS:
                ex = {"tp2_pct": tp2, "sl_pct": sl, "hold_h": hold}
                trades = je.coin_trades(coin, sides_enabled=("C",),
                                        sigma_calib=CALIB, call_exit=ex)
                tr, _ = je.split(trades, 0.70)
                if len(tr) < 50:
                    continue
                usd = sum(fixed_lot_pnl(t) for t in tr)
                wr = sum(1 for t in tr if t["pnl_pct"] > 0) / len(tr)
                rows.append({"ex": ex, "n": len(tr), "wr": wr, "usd": usd,
                             "per_day": len(tr) / span_days(tr)})
    rows.sort(key=lambda r: -r["usd"])
    results[coin] = rows
    print(f"\n{coin}:C top-8 по train fixed-lot $ (из {len(rows)}):")
    for r in rows[:8]:
        e = r["ex"]
        print(f"  tp{e['tp2_pct']:.2f}/sl{e['sl_pct']:.2f}/h{e['hold_h']:3d} "
              f"n={r['n']:4d} ({r['per_day']:.2f}/д) WR {r['wr'] * 100:4.1f}% "
              f"train ${r['usd']:+8.2f}")

# ── Part C: validate the best survivor per coin (holdout, CLAMP, quarters) ──
print()
print("=" * 100)
print("PART C: валидация выживших (holdout/CLAMP/кварталы) + портфель")
print("=" * 100)
SURVIVORS = {c: rows[0]["ex"] for c, rows in results.items()
             if rows and rows[0]["usd"] > 0}
if not SURVIVORS:
    print("Выживших нет: ни один микро-TP конфиг не положителен на train — "
          "GATE: REJECTED")
    sys.exit(0)

for coin, ex in SURVIVORS.items():
    print(f"\n{coin}:C кандидат {ex}")
    for sig_name, calib in (("CALIB", CALIB), ("CLAMP", None)):
        trades = je.coin_trades(coin, sides_enabled=("C",),
                                sigma_calib=calib, call_exit=ex)
        tr, ho = je.split(trades, 0.70)
        for seg_name, seg in (("train", tr), ("holdout", ho)):
            usd = sum(fixed_lot_pnl(t) for t in seg)
            wr = (sum(1 for t in seg if t["pnl_pct"] > 0) / len(seg) * 100
                  if seg else 0)
            print(f"  {sig_name} {seg_name:8s} n={len(seg):4d} "
                  f"WR {wr:4.1f}% ${usd:+8.2f}")
        qs = je.quarters(trades)
        qneg = [(q[0]["entry_ts"], sum(fixed_lot_pnl(t) for t in q))
                for q in qs if q and sum(fixed_lot_pnl(t) for t in q) < 0]
        print(f"  {sig_name} кварталы: {len(qs)} всего, отрицательных "
              f"{len(qneg)}: " + ", ".join(f"${v:+.0f}" for _, v in qneg))

# Portfolio: does adding micro-TP calls help the Phase-7 put book C?
print("\nПортфель replay_v2 pkc=1: путовая книга C ± micro-TP коллы")


def gen(vol, regimes):
    g = dict(jc.PUT_GEN)
    g["vol_threshold"] = vol
    g["regime_filter"] = regimes
    return g


BOOK_C = {"BTC": {"ret7d": 1.0, "gen": gen(0.40, ("range", "transition"))},
          "ETH": {"ret7d": 1.0, "gen": gen(0.60, ("range",))}}


def put_trades(coin, spec, calib):
    jc.RET_7D_THRESHOLD = spec["ret7d"]
    je._BASE_CACHE.pop(coin, None)
    t = je.coin_trades(coin, sides_enabled=("P",), put_gen=spec["gen"],
                       sigma_calib=calib)
    je._BASE_CACHE.pop(coin, None)
    return t


RET7D_DEFAULT = jc.RET_7D_THRESHOLD
for sig_name, calib in (("CALIB", CALIB), ("CLAMP", None)):
    puts = []
    for coin, spec in BOOK_C.items():
        puts += put_trades(coin, spec, calib)
    jc.RET_7D_THRESHOLD = RET7D_DEFAULT
    calls = []
    for coin, ex in SURVIVORS.items():
        calls += je.coin_trades(coin, sides_enabled=("C",),
                                sigma_calib=calib, call_exit=ex)
    for label, book in (("puts-only C", puts), ("C + micro-TP calls",
                                                sorted(puts + calls,
                                                       key=lambda t: t["entry_ts"]))):
        tr, ho = je.split(sorted(book, key=lambda t: t["entry_ts"]), 0.70)
        rt = replay_v2(tr, MO, CAP, per_key_cap=PKC)
        rh = replay_v2(ho, MO, CAP, per_key_cap=PKC)
        print(f"  {sig_name} {label:22s} train {rt['return_pct']:+7.1f}% "
              f"dd {rt['max_dd']:4.1f}% | holdout {rh['return_pct']:+7.1f}% "
              f"dd {rh['max_dd']:4.1f}%")
