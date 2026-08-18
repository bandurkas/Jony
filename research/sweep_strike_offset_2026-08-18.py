"""OTM strike offset — полный протокол (2026-08-18).

Идея из Phase 11 (live-контрфакт): 1-2% OTM путы дали +WR и +$ на 42 днях.
Здесь — 2-летний честный тест на задеплоенной книге C (per-coin гейты):
  BTC:P ret7d 1.0, vol 0.40, range+transition
  ETH:P ret7d 1.0, vol 0.60, range

Протокол: выбор offset ТОЛЬКО на train/CALIB (fixed-lot); валидация
выживших отдельно — holdout, CLAMP, кварталы, портфельный replay_v2 pkc=1
против ATM-базы. Консервативность модели: плоская сигма БЕЗ skew занижает
OTM-путовую премию — реальный кандидат богаче модельного.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je
from replay_account_v2 import replay_v2

CALIB = {"b0": 0.3487, "b1": 0.2646, "floor": 0.25, "ceiling": 1.05}
MO, CAP, PKC = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP, 1
OFFSETS = (0.0, 0.005, 0.01, 0.015, 0.02, 0.03)


def gen(vol, regimes):
    g = dict(jc.PUT_GEN)
    g["vol_threshold"] = vol
    g["regime_filter"] = regimes
    return g


BOOK_C = {"BTC": {"ret7d": 1.0, "gen": gen(0.40, ("range", "transition"))},
          "ETH": {"ret7d": 1.0, "gen": gen(0.60, ("range",))}}


def put_trades(coin, spec, calib, offset):
    jc.RET_7D_THRESHOLD = spec["ret7d"]
    je._BASE_CACHE.pop(coin, None)
    t = je.coin_trades(coin, sides_enabled=("P",), put_gen=spec["gen"],
                       sigma_calib=calib, strike_offset=offset)
    je._BASE_CACHE.pop(coin, None)
    return t


def fixed_lot_pnl(t):
    qty = t["lot"]
    notional = t["strike"] * qty
    fee_open = jc.fee_usd(notional, t["entry_credit"] * qty)
    exit_credit = t["entry_credit"] * (1 - t["pnl_pct"])
    fee_close = jc.fee_usd(notional, exit_credit * qty)
    return (t["entry_credit"] - exit_credit) * qty - fee_open - fee_close


def seg_stats(seg):
    n = len(seg)
    if not n:
        return "n=0"
    usd = sum(fixed_lot_pnl(t) for t in seg)
    wr = sum(1 for t in seg if t["pnl_pct"] > 0) / n * 100
    cred = sum(t["entry_credit"] for t in seg) / n
    return f"n={n:4d} WR {wr:4.1f}% credit~{cred:5.1f} ${usd:+9.2f}"


print("=" * 100)
print("PHASE 1: выбор offset на TRAIN/CALIB (fixed-lot, книга C, puts-only)")
print("=" * 100)
train_usd = {}
for coin, spec in BOOK_C.items():
    print(f"\n{coin}:P")
    for off in OFFSETS:
        trades = put_trades(coin, spec, CALIB, off)
        tr, _ = je.split(trades, 0.70)
        usd = sum(fixed_lot_pnl(t) for t in tr)
        train_usd[(coin, off)] = usd
        print(f"  off {off * 100:3.1f}%: train {seg_stats(tr)}")

best = {c: max(OFFSETS, key=lambda o: train_usd[(c, o)]) for c in BOOK_C}
print(f"\nвыбор по train: {best} (ATM base: "
      + ", ".join(f"{c} ${train_usd[(c, 0.0)]:+.0f}" for c in BOOK_C) + ")")

print()
print("=" * 100)
print("PHASE 2: валидация выбранных offset (holdout, CLAMP, кварталы)")
print("=" * 100)
for coin, spec in BOOK_C.items():
    off = best[coin]
    if off == 0.0:
        print(f"{coin}:P — train выбрал ATM, кандидата нет")
        continue
    print(f"\n{coin}:P off {off * 100:.1f}% vs ATM:")
    for sig_name, calib in (("CALIB", CALIB), ("CLAMP", None)):
        for o in (0.0, off):
            trades = put_trades(coin, spec, calib, o)
            tr, ho = je.split(trades, 0.70)
            print(f"  {sig_name} off {o * 100:3.1f}%: train {seg_stats(tr)} | "
                  f"holdout {seg_stats(ho)}")
        # кварталы кандидата против ATM
        t_atm = put_trades(coin, spec, calib, 0.0)
        t_off = put_trades(coin, spec, calib, off)
        qa = {q[0]["entry_ts"] // (86_400_000 * 91): sum(fixed_lot_pnl(t) for t in q)
              for q in je.quarters(t_atm) if q}
        qo = {q[0]["entry_ts"] // (86_400_000 * 91): sum(fixed_lot_pnl(t) for t in q)
              for q in je.quarters(t_off) if q}
        worse = [k for k in qo if qo[k] < qa.get(k, 0) - 1e-6 and qo[k] < 0]
        neg_atm = sum(1 for v in qa.values() if v < 0)
        neg_off = sum(1 for v in qo.values() if v < 0)
        print(f"  {sig_name} кварталы: ATM neg {neg_atm}, off neg {neg_off}, "
              f"хуже-и-в-минусе {len(worse)}")

print()
print("=" * 100)
print("PHASE 3: портфельный replay_v2 pkc=1 — книга C ATM vs книга C OTM")
print("=" * 100)
for sig_name, calib in (("CALIB", CALIB), ("CLAMP", None)):
    for label, offs in (("ATM", {c: 0.0 for c in BOOK_C}),
                        ("OTM best", best)):
        book = []
        for coin, spec in BOOK_C.items():
            book += put_trades(coin, spec, calib, offs[coin])
        book.sort(key=lambda t: t["entry_ts"])
        tr, ho = je.split(book, 0.70)
        rt = replay_v2(tr, MO, CAP, per_key_cap=PKC)
        rh = replay_v2(ho, MO, CAP, per_key_cap=PKC)
        print(f"  {sig_name} {label:8s} train {rt['return_pct']:+7.1f}% "
              f"dd {rt['max_dd']:4.1f}% | holdout {rh['return_pct']:+7.1f}% "
              f"dd {rh['max_dd']:4.1f}%")
