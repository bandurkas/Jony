"""Put credit spread vs голый пут — полный протокол (2026-08-18).

Вторая нога Jony: к каждой короткой ATM-путе покупаем пут на width ниже
спота (та же экспирация, та же сигма). Короткая нога и ВСЕ её выходы
идентичны валидированной стратегии; длинная — оверлей: куплена по ask на
входе, продана по bid в бар выхода короткой. Вопрос гейта: окупает ли
купленный хвост свою цену за 2 года и что он делает с DD/худшими кварталами.

Модельные смещения (в обе стороны, честно):
- SL заполняется ровно по -sl_pct без гэпа => выгода спреда в обвале
  ЗАНИЖЕНА (реальный SL голой путы проскальзывает, у спреда лонг растёт);
- плоская сигма без skew => реальный OTM-пут ДОРОЖЕ модельного, цена
  защиты ЗАНИЖЕНА. Направления противоположны, сальдо неизвестно.

Портфельная капасити: маржа спреда = max loss (width - net credit) через
m_lot override в replay_v2 — сравниваем и без него (чистый drag-тест).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_bs as bs
import jony_core as jc
import jony_engine as je
from replay_account_v2 import replay_v2

CALIB = {"b0": 0.3487, "b1": 0.2646, "floor": 0.25, "ceiling": 1.05}
MO, CAP, PKC = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP, 1
HS = je.HALF_SPREAD
T0_Y = jc.TARGET_EXPIRY_H / (24 * 365)
WIDTHS = (0.03, 0.05, 0.075, 0.10)


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


def spreadify(t: dict, width: float) -> dict | None:
    """Голая сделка -> спредовая. None = защита невозможна (вырожденный K)."""
    K_long = round(t["entry_spot"] * (1 - width) / jc.STRIKE_ROUND[t["coin"]]) \
        * jc.STRIKE_ROUND[t["coin"]]
    if K_long >= t["strike"]:
        return None
    long_cost = bs.price("P", t["entry_spot"], K_long, T0_Y, t["sigma"]) * (1 + HS)
    net_credit = t["entry_credit"] - long_cost
    if net_credit <= 0.01:
        return None
    elapsed_h = (t["exit_ts"] - t["entry_ts"]) / 3.6e6
    T_rem = max(0.0, (jc.TARGET_EXPIRY_H - elapsed_h)) / (24 * 365)
    long_exit = bs.price("P", t["exit_spot"], K_long, T_rem, t["sigma"]) * (1 - HS)
    short_exit = t["entry_credit"] * (1 - t["pnl_pct"])
    # комиссии длинной ноги (обе стороны) складываем в exit-долг
    lf = (jc.fee_usd(K_long * t["lot"], long_cost * t["lot"])
          + jc.fee_usd(K_long * t["lot"], long_exit * t["lot"])) / t["lot"]
    net_exit = short_exit - long_exit + lf
    s = dict(t)
    s["entry_credit"] = net_credit
    s["pnl_pct"] = (net_credit - net_exit) / net_credit
    s["m_lot"] = max(0.01 * t["strike"],
                     (t["strike"] - K_long - net_credit)) * t["lot"]
    return s


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
    worst = min(fixed_lot_pnl(t) for t in seg)
    return f"n={n:4d} WR {wr:4.1f}% ${usd:+9.2f} worst1 {worst:+7.2f}"


print("=" * 100)
print("PHASE 1: выбор width на TRAIN/CALIB (fixed-lot, книга C)")
print("=" * 100)
naked = {}
train_usd = {}
for coin, spec in BOOK_C.items():
    naked[coin] = put_trades(coin, spec, CALIB)
    tr, _ = je.split(naked[coin], 0.70)
    print(f"\n{coin}:P голая:      train {seg_stats(tr)}")
    for w in WIDTHS:
        sp = [s for s in (spreadify(t, w) for t in naked[coin]) if s]
        tr_s, _ = je.split(sp, 0.70)
        usd = sum(fixed_lot_pnl(t) for t in tr_s)
        train_usd[(coin, w)] = usd
        print(f"{coin}:P width {w * 100:4.1f}%: train {seg_stats(tr_s)}")

best = {c: max(WIDTHS, key=lambda w: train_usd[(c, w)]) for c in BOOK_C}
print(f"\nвыбор по train: { {c: f'{best[c]*100:.1f}%' for c in BOOK_C} }")

print()
print("=" * 100)
print("PHASE 2: holdout/CLAMP/кварталы для выбранных width")
print("=" * 100)
for coin, spec in BOOK_C.items():
    w = best[coin]
    for sig_name, calib in (("CALIB", CALIB), ("CLAMP", None)):
        nk = put_trades(coin, spec, calib)
        sp = [s for s in (spreadify(t, w) for t in nk) if s]
        for label, trades in ((f"голая", nk), (f"spread {w * 100:.0f}%", sp)):
            tr, ho = je.split(trades, 0.70)
            print(f"  {coin} {sig_name} {label:11s} train {seg_stats(tr)} | "
                  f"holdout {seg_stats(ho)}")
        qn = [(q[0]["entry_ts"], sum(fixed_lot_pnl(t) for t in q))
              for q in je.quarters(nk) if q]
        qs = [(q[0]["entry_ts"], sum(fixed_lot_pnl(t) for t in q))
              for q in je.quarters(sp) if q]
        worst_n = sorted(v for _, v in qn)[:3]
        worst_s = sorted(v for _, v in qs)[:3]
        print(f"  {coin} {sig_name} худшие кварталы: голая "
              f"{[f'{v:+.0f}' for v in worst_n]} vs spread "
              f"{[f'{v:+.0f}' for v in worst_s]}")

print()
print("=" * 100)
print("PHASE 3: портфель replay_v2 pkc=1 — голая vs spread (drag) vs spread (маржа max-loss)")
print("=" * 100)
for sig_name, calib in (("CALIB", CALIB), ("CLAMP", None)):
    nk_book, sp_book = [], []
    for coin, spec in BOOK_C.items():
        nk = put_trades(coin, spec, calib)
        nk_book += nk
        sp_book += [s for s in (spreadify(t, best[coin]) for t in nk) if s]
    variants = [("голая", sorted(nk_book, key=lambda t: t["entry_ts"]))]
    drag = [dict(s) for s in sp_book]
    for d in drag:
        d.pop("m_lot", None)  # маржа как у голой — чистый тест цены защиты
    variants.append(("spread drag", sorted(drag, key=lambda t: t["entry_ts"])))
    variants.append(("spread m-loss", sorted(sp_book, key=lambda t: t["entry_ts"])))
    for label, book in variants:
        tr, ho = je.split(book, 0.70)
        rt = replay_v2(tr, MO, CAP, per_key_cap=PKC)
        rh = replay_v2(ho, MO, CAP, per_key_cap=PKC)
        print(f"  {sig_name} {label:14s} train {rt['return_pct']:+7.1f}% "
              f"dd {rt['max_dd']:4.1f}% conc~{rt['avg_concurrent']:.1f} | "
              f"holdout {rh['return_pct']:+7.1f}% dd {rh['max_dd']:4.1f}%")
