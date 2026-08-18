"""Калибровка политики закрытий советника (Phase 9, продолжение
counterfactual_advisor_2026-08-17.py) — по запросу пользователя: «ни в коем
случае не закрывать позиции, которые ещё могут собрать профит; главная
задача — давать профиту расти, уберегая от резких движений; откалибровать
так, чтобы на прожитом отрезке советник давал реальную прибавку в $».

Результат (модель-к-модели, тот же прайсинг в обоих мирах, поэтому
модельные артефакты сокращаются):
  - политика «эндшпиль + аварийный стоп»: дельта к чистой механике
    +$33…+52 по ВСЕЙ сетке 24 вариантов (медиана +$43.5); выбрано
    75%/30% + losscut −60% ITM persist 2 → +$48.6;
  - резка убытков на просадке (−15…−35%) ТОКСИЧНА: итог −$34…−101 против
    +$66 механики (короткая опционная позиция дышит: id1-6 вернулись с
    −25% в +10..12$); допустим только глубокий аварийный стоп (−60%, до
    механического SL с его гэп-проскальзыванием);
  - «страх страйка» (закрытие при |spot/K−1|<=0.5%) убивает каждый свежий
    ATM-вход через ~2ч — любой вариант с ним −$38…−79 итогом.
Честные оговорки: июль −$6 / август +$54.6 — ценность приходит эпизодами
«поздний откат забирает дозревший профит» (кластер id30-35 даёт бОльшую
часть дельты); пороги выбраны in-sample, но знак устойчив по всей сетке.
Пороги деплоя: промпт 75%/30%/−60%; кодовое вето core/close_policy.py чуть
шире (70%/25%/−55%) — предохранитель, не дублёр LLM-решения.
"""
from __future__ import annotations

import itertools
import statistics
import sqlite3

# переиспользуем прайсинг/загрузку из основного контрфактуала
CF = open(__file__.replace("calibrate_close_policy", "counterfactual_advisor")).read()
exec(CF.split("conn = sqlite3.connect")[0])  # noqa: S102 — research-скрипт

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = [dict(r) for r in conn.execute("SELECT * FROM positions ORDER BY id")]
pos = [Pos(r) for r in rows]
CLOSE_ALL = {4, 5, 6, 19, 21}
w0 = sum(r["pnl_usd"] for r in rows)


def sim_policy(p, losscut, ef, eh, persist, uc):
    """Механика + политика: закрыть разрешено только в эндшпиле (elapsed>=ef
    и профит>=eh) или аварийно (ITM и убыток<=losscut, None=выкл)."""
    end_ms = p.opened_at_ms + int(p.hold_h * 3600_000)
    if uc:
        end_ms = min(end_ms, uc)
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
        elapsed = (t - p.opened_at_ms) / (p.hold_h * 3600_000)
        cond = ((losscut is not None and itm and pnl_pct <= losscut)
                or (ef is not None and elapsed >= ef and pnl_pct >= eh))
        streak = streak + 1 if cond else 0
        if streak >= persist:
            return (p.pnl_at(t), t, "advisor")
        t += 3600_000
    if uc and end_ms == uc:
        return (p.pnl_at(end_ms), end_ms, "user_close")
    return (p.pnl_at(end_ms), end_ms, "time_stop")


def world(losscut, ef, eh, ps):
    tot = 0.0
    for p in pos:
        uc = p.closed_at_ms if p.id in CLOSE_ALL else None
        pnl, _, _ = sim_policy(p, losscut, ef, eh, ps, uc)
        tot += pnl
    return tot


base = world(None, None, 0, 2)
print(f"факт {w0:+.2f} | модельная механика {base:+.2f} (ошибка {base - w0:+.2f})\n")
print("дельты политики к модельной механике:")
vals = []
for losscut, ef, eh, ps in itertools.product(
        (None, -0.60, -0.70), (0.70, 0.75), (0.25, 0.30), (2, 3)):
    d = world(losscut, ef, eh, ps) - base
    vals.append(d)
    print(f"  losscut={losscut} endgame {ef:.0%}/{eh:.0%} p{ps}: {d:+7.2f}")
print(f"\nдиапазон {min(vals):+.2f}…{max(vals):+.2f}, "
      f"медиана {statistics.median(vals):+.2f}")
print("\nтоксичные альтернативы (для протокола):")
for losscut, label in ((-0.25, "резка просадки −25%"),):
    d = world(losscut, 0.75, 0.30, 2) - base
    print(f"  {label}: {d:+.2f}")
