"""Калиброванная политика закрытий (Phase 9, 2026-08-17,
research/counterfactual_advisor_2026-08-17.py + RESEARCH_LOG): профиту дают
дозреть. На 61 live-сделке дельта политики к чистой механике +$33…52 по всей
сетке параметров; прежнее поведение советника (ранний харвест 30-35%, страх
спота у страйка) дало бы −$103…−144. Единый источник порогов для advisor.py
(вето авто-CLOSE) и loop.py (lockdown-жатва) — чтобы её нельзя было обойти
сменой канала (ревью 2026-08-17, находка «lockdown bypass»).

Разрешено закрывать досрочно РОВНО два случая:
  1) эндшпиль: прошло >= ENDGAME_FRAC планового hold_h И профит >=
     ENDGAME_MIN_PROFIT кредита — снять зрелый профит до позднего отката;
  2) аварийный стоп: позиция ITM И убыток <= EMERGENCY_LOSS кредита
     (механический SL дальше и в гэпе проскальзывает).
Пороги здесь чуть шире промптовых (75%/30%/−60%) — это предохранитель
вокруг LLM-решения, а не его дублёр."""
from __future__ import annotations

ENDGAME_FRAC = 0.70          # раньше этой доли hold_h профит не трогаем
ENDGAME_MIN_PROFIT = 0.25    # доля кредита
EMERGENCY_LOSS = -0.55       # доля кредита; применять только вместе с ITM


def endgame_ok(pnl_frac: float | None, held_h: float | None,
               hold_h: float | None) -> bool:
    """Эндшпиль-ветка. Все величины — ДОЛИ/часы (не проценты). Fail-closed."""
    if pnl_frac is None or held_h is None or hold_h is None or hold_h <= 0:
        return False
    return pnl_frac >= ENDGAME_MIN_PROFIT and held_h >= ENDGAME_FRAC * hold_h


def emergency_ok(pnl_frac: float | None, itm: bool | None) -> bool:
    """Аварийная ветка: глубокий убыток строго вместе с ITM. Fail-closed."""
    if pnl_frac is None or itm is None:
        return False
    return bool(itm) and pnl_frac <= EMERGENCY_LOSS
