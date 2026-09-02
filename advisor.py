"""Hourly Claude-powered exit advisor (2026-08-14).

Goal (user request): once per hour, look at the market globally + locally and
the current open book, and recommend exits that PROTECT accumulated MTM
profit ("не жадничать"). Advisory only — this service never closes positions
and never touches loop.py state; it writes recommendations to
data/advice.jsonl, and telegram-notifies when something is actionable.

Env:
  ANTHROPIC_API_KEY   required (VPS3 .env only, never committed)
  ADVISOR_MODEL       default claude-sonnet-5
  ADVISOR_INTERVAL_MIN default 60
  ADVISOR_NOTIFY_MODE "actionable" (default: telegram only when action needed
                      or risk high) | "all" | "off"

Run modes: `python advisor.py` (loop) | `python advisor.py --once` (single
tick, prints the advice — used for deploy verification).
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

import requests

from core import close_policy
from core.strategy import COIN_SIDES, DISABLED_KEYS, RET_7D_THRESHOLD
from db import repo
from services.bybit_client import BybitClient, pick_atm_option
from services.telegram_notify import notify

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.getenv("ADVISOR_MODEL", "claude-sonnet-5")
INTERVAL_S = int(os.getenv("ADVISOR_INTERVAL_MIN", "60")) * 60
NOTIFY_MODE = os.getenv("ADVISOR_NOTIFY_MODE", "actionable")
ADVICE_PATH = Path(os.getenv("ADVICE_PATH", "data/advice.jsonl"))
BACKEND = os.getenv("ADVISOR_BACKEND", "api")  # api | cli (MAX-подписка)
FAIL_NOTIFY_STREAK = int(os.getenv("ADVISOR_FAIL_NOTIFY_STREAK", "3"))
API_BASE = os.getenv("JONY_API_BASE", "http://jony_api:8200")
GATES_STALE_MS = 10 * 60_000  # старше -> gate-снапшот считается протухшим

SYSTEM = """Ты — риск-советник по проданным (short) крипто-опционам на Bybit.
Счёт продаёт недельные ATM опционы (short put / short call) и зарабатывает
тету; главный риск — резкое движение базового актива, сжигающее накопленную
прибыль. Учитывай: короткие путы страдают при падении, короткие коллы — при
росте; ускорение волатильности вредит обоим.
Подсказки по данным: iv_minus_rv24 > 0 — продажа волы оплачивается (фон для
удержания лучше); funding_rate экстремумы и резкий рост OI — признак
перегретого позиционирования; dist_from_7d_high/low — где спот в диапазоне.

ГЛАВНОЕ ПРАВИЛО по открытым позициям: ДАЙ ПРОФИТУ ДОЗРЕТЬ. Экспектанса
стратегии живёт в поздней тете; ранний харвест и страх у страйка — твой
главный измеренный источник убытка. Калибровка на всей истории бота
(61 сделка, 07.07-14.08): твоё прежнее поведение (фиксация при 30-35%,
закрытие при споте у страйка) дало бы итог −$38…−79 против +$65 у механики
без тебя; резка просадок −15…−35% дала бы −$34…−101. Политика ниже —
единственная из проверенных, дающая плюс: +$33…52 СВЕРХУ механики.

CLOSE разрешён РОВНО в двух случаях:
1) Эндшпиль: age_h >= 75% hold_h И профит >= 30% кредита — снять ЗРЕЛЫЙ
   профит, пока поздний откат его не забрал (весь измеренный плюс политики
   из этого правила).
2) Аварийный стоп: позиция ITM (strike_buffer_pct < 0) И убыток <= −60%
   кредита — механический SL стоит дальше и в гэпе проскальзывает. (CLOSE
   по убыточной не авто-исполняется — уйдёт срочным уведомлением человеку.)
Всё остальное — HOLD. Особо запрещено:
- закрывать позицию моложе 75% hold_h из-за спота у страйка, низкого
  z_buffer, prob_touch/prob_itm или «экстремального темпа профита»: вход
  ВСЕГДА ATM, молодая позиция у страйка — норма, а не риск. Эти метрики
  имеют вес только в эндшпиле и в аварийном случае.
- резать «дышащую» просадку (−20…−50% кредита): короткая опционная позиция
  регулярно возвращается из неё в плюс, у механики есть свой SL.
- фиксировать профит потому что «премия почти собрана» или «темп
  экстремальный» — низкий remaining_premium_pct сам по себе НЕ причина.
Защита РАСТУЩЕГО профита при ухудшении фона — это posture=tight (включает
механический трейлинг-лок от пика), а не CLOSE. lockdown — только
аккаунт-уровневая авария против всей книги, не инструмент фиксации.
Код дублирует политику вето (эндшпиль от 70%/25%, аварийный от −55% ITM):
CLOSE вне этих рамок физически не исполнится — не трать на него решения.

Справочно: z_buffer (запас до страйка в единицах ожидаемого хода за
оставшееся время), prob_touch_pct, prob_itm_pct, expected_move_pct,
remaining_premium_pct — используй их в эндшпиле (решить, снимать ли при
пограничном профите) и для аварийной оценки убыточной позиции.
your_previous_advice — твоя рекомендация в прошлый раз: сохраняй
преемственность (не дёргай HOLD↔CLOSE и posture без причины), но признавай
смену обстановки. wake_reason в запросе означает срочный вызов по рыночному
триггеру, а не плановый часовой.

Твои решения ИСПОЛНЯЮТСЯ автоматически:
- risk_posture пишется в бота: tight включает трейлинг-фиксацию профита,
  lockdown блокирует входы, снимает ЗРЕЛЫЙ профит (по политике закрытий)
  и держит трейлинг-лок на остальных. Это твой главный рычаг при
  ухудшении фона; обойти политику закрытий через lockdown нельзя.
- CLOSE по профитной позиции закрывает её реально (после двух согласных
  вызовов подряд, либо сразу при lockdown) — но только в рамках политики
  закрытий выше (кодовое вето). CLOSE по убыточной — только уведомление
  человеку.
- entry_proposal открывает РЕАЛЬНУЮ позицию (бот перепроверит лимиты).
  Базовые условия: ключ в keys_available; iv_minus_rv24 > 0 (продажу волы
  платят); волатильность НЕ ускоряется; funding умеренный; спот не у 7д
  экстремума. Особо следи за окном для BTC P — исторически лучший ключ.
  mechanical_gates показывает состояние механики: если по монете
  no_side_allowed=true (side-off: ret_7d ниже −границы тренда) — пут по
  этой монете НЕ предлагай. Контртрендовый пут «на выдохшемся сливе»
  проверен на честном движке (Phase 14, 2026-09-02): минус в обеих монетах
  при любом hold/размере/страйке; код такое предложение отклонит. Твоя
  зона — момент входа ВНУТРИ разрешённого механикой окна (тайминг, отказ
  от входа у 7д-экстремума), не обход трендового фильтра.
  Качество важнее частоты: обычно entry_proposal = null, максимум 1-2
  предложения в сутки.
  confidence — твоя калиброванная вероятность, что позиция закроется в
  плюс; она сверяется с фактом (confidence_calibration в your_track_record,
  Brier). Одно и то же число в каждом предложении = нет информации; не
  подгоняй под порог. Трек-рекорд по ключу с n<10 — не аргумент ни за,
  ни против.

SHORT CALL (ETH:C, BTC:C) — ИСПЫТАТЕЛЬНЫЕ advisor-only ключи. Механика их
не торгует (live ETH:C −$41/WR 18%, бэктест минус во всех конфигах, хвост —
blow-off top); ты — единственный источник колл-сигнала, и это A/B-тест тебя.
Почему колл ≠ пут: хвост вверх в крипте открытый (squeeze, ликвидации
шортов), hold всего 24ч (пут — 120ч), SL ближе. Экспектанса колла — только
если рост ВЫДОХСЯ в ближайшие сутки. Предлагай short call, когда ВСЕ
условия выполнены (они продублированы кодом, нарушение = отказ):
  1. 24ч изменение в [−6%; +0.5%] — не после зелёного дня и не в день краха
     (после краха отскок-squeeze бьёт колл сильнее теты);
  2. |1ч изменение| ≤ 1% — не в момент импульса;
  3. 7д изменение ≤ +12% — не в параболе;
  4. спот ниже 7д-хая ≥ 1.5% — не продавать колл на свежем хае;
  5. iv_minus_rv24 > 0 и вола НЕ ускоряется (rv24 vs rv24_prev_day);
  6. funding в [−0.01%; +0.05%]/8ч — шорты не перегружены (squeeze-риск),
     лонги не в эйфории;
  7. confidence ≥ 0.7 (строже, чем у путов), не больше 1 колла в сутки.
Что усиливает сигнал: откат от 7д-хая 2–6% на падающем rv, IV ATM выше
rv24 заметно, OI не растёт, 1ч/24ч около нуля. Что запрещает даже при
выполнении чисел: известные события в ближайшие 24ч (FOMC/CPI/крупные
разблокировки), резкий рост OI с положительным funding (разгон), новостной
фон «ETF/листинг/апгрейд». Для ETH:C — дополнительно смотри на BTC: колл на
ETH при BTC у хаёв — нет.
Выход: каждая advisor-позиция закрывается трейлингом автоматически (arm 20%
кредита / откат 10 п.п.), плюс штатные TP2 70% / SL / 24ч. Твои HOLD/CLOSE
по коллам — по общей политике выше. Вход на пике ралли трейлинг не спасёт.
Kill-критерий эксперимента (код): ≥8 закрытых advisor-коллов с WR<40% или
PnL<−$20 — коллы выключаются автоматически.
Ответ давай вызовом tool give_advice: market_view/summary/reason — кратко и
по-русски; по каждой открытой позиции ровно одна запись в positions.
tg_summary — одна строка ≤90 символов для мобильного пуша (суть + действие),
brief — 2-4 слова причины на позицию. Без воды: пуш читают с телефона.
Решение по каждой позиции строго бинарное. CLOSE — забрать профит/резать
риск сейчас; HOLD — осознанно держать. Категории «понаблюдать» нет:
сомневаешься — сравни тету с риском разворота и прими решение.
your_track_record — твой собственный измеренный скоринг (сколько $ реально
сохранили/стоили твои прошлые советы против «просто держать»). Калибруйся
по нему: если CLOSE в минусе (total_saved_usd < 0) — ты всё ещё режешь
позиции раньше политики закрытий; сверяйся с её двумя разрешёнными
случаями. Если HOLD в плюсе — тета работает, продолжай не мешать.
"""


def pct(a: float, b: float) -> float | None:
    return round((a - b) / b * 100, 2) if (a and b) else None


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def strike_risk(side: str, spot: float, strike: float, iv: float,
                t_years: float) -> dict:
    """Deterministic reversal-vs-expiry math the model must not eyeball:
    - strike_buffer_pct: distance to the strike on the DANGEROUS side
      (short put hurts below, short call above); negative = already ITM
    - expected_move_pct: 1-sigma underlying move over the REMAINING life
      (iv * sqrt(T)) — the yardstick the buffer is measured against
    - z_buffer: buffer in units of that remaining expected move
    - prob_touch_pct: ~P(spot touches strike before expiry) ≈ 2*N(-z)
    - prob_itm_pct: ~P(ITM at expiry) ≈ N(-z) (driftless approximation)
    A reversal signal only matters if there is enough remaining time for it
    to reach the strike — that is exactly what z_buffer/prob_touch encode."""
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 0:
        return {}
    buffer_frac = (spot - strike) / spot if side == "P" else (strike - spot) / spot
    sigma_t = iv * math.sqrt(t_years)
    z = buffer_frac / sigma_t
    prob_touch = 1.0 if z <= 0 else min(1.0, 2.0 * _norm_cdf(-z))
    prob_itm = _norm_cdf(-z)
    return {
        "strike_buffer_pct": round(buffer_frac * 100, 2),
        "expected_move_pct": round(sigma_t * 100, 2),
        "z_buffer": round(z, 2),
        "prob_touch_pct": round(prob_touch * 100, 1),
        "prob_itm_pct": round(prob_itm * 100, 1),
    }


def market_block(client: BybitClient) -> dict:
    out = {}
    now_ms = int(time.time() * 1000)
    for coin, sym in (("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")):
        kl = client.get_klines(sym, "60", 168)  # 7d of 1h bars, oldest-first
        closes = [k["close"] for k in kl]
        if len(closes) < 168:  # усечённый ответ → 7д-метрики врут; монета fail-closed
            print(f"[advisor] market_block: {coin} skipped, only {len(closes)} bars", flush=True)
            continue
        last = closes[-1]
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        rv24 = (sum(r * r for r in rets[-24:]) / 24) ** 0.5 * math.sqrt(24 * 365)
        rv24_prev = (sum(r * r for r in rets[-48:-24]) / 24) ** 0.5 * math.sqrt(24 * 365)
        hi7, lo7 = max(closes), min(closes)
        row = {
            "spot": last,
            "chg_1h_pct": pct(last, closes[-2]),
            "chg_24h_pct": pct(last, closes[-25]),
            "chg_7d_pct": pct(last, closes[0]),
            "dist_from_7d_high_pct": pct(last, hi7),
            "dist_from_7d_low_pct": pct(last, lo7),
            "rv24_annualized": round(rv24, 3),
            "rv24_prev_day": round(rv24_prev, 3),
            "vol_accelerating": rv24 > rv24_prev * 1.3,
        }
        try:  # funding + open interest (perp ticker)
            t = client.session.get_tickers(category="linear", symbol=sym)["result"]["list"][0]
            row["funding_rate_pct"] = round(float(t.get("fundingRate", 0)) * 100, 4)
            row["open_interest_value_usd"] = round(float(t.get("openInterestValue", 0)))
        except Exception:
            pass
        try:  # ATM weekly IV both sides -> VRP vs realized vol
            chain = client.get_options_tickers(coin)
            ivs = {}
            for opt_side in ("P", "C"):
                atm = pick_atm_option(chain, last, opt_side, 168, 6, now_ms)
                if atm and atm.get("mark_iv"):
                    ivs[opt_side] = round(atm["mark_iv"], 3)
            if ivs:
                row["atm_weekly_iv"] = ivs
                iv_mid = sum(ivs.values()) / len(ivs)
                row["iv_minus_rv24"] = round(iv_mid - rv24, 3)  # >0: selling vol is paid
        except Exception:
            pass
        out[coin] = row
    return out


def positions_block(client: BybitClient, now_ms: int) -> list[dict]:
    conn = repo.connect()
    try:
        open_pos = repo.open_positions(conn)
    finally:
        conn.close()
    chain_by_coin: dict[str, dict] = {}
    for coin in {p["coin"] for p in open_pos}:
        try:
            chain_by_coin[coin] = {
                o["symbol"]: {"mark": o["mark_price"], "iv": o["mark_iv"],
                              "spot": o["underlying_price"]}
                for o in client.get_options_tickers(coin)}
        except Exception:
            chain_by_coin[coin] = {}
    out = []
    for p in open_pos:
        m = chain_by_coin.get(p["coin"], {}).get(p["option_symbol"]) or {}
        mark = m.get("mark")
        unreal = round((p["entry_credit"] - mark) * p["qty"], 2) if mark else None
        credit_total = p["entry_credit"] * p["qty"]
        t_years = max(0.0, (p["expiry_ms"] - now_ms) / (8760 * 3.6e6))
        row = {
            "id": p["id"], "symbol": p["option_symbol"],
            "coin": p["coin"], "side": p["side"],
            "qty": p["qty"], "entry_credit": p["entry_credit"],
            "current_mark": mark, "unrealized_usd": unreal,
            "profit_pct_of_credit": round(unreal / credit_total * 100, 1)
            if (unreal is not None and credit_total) else None,
            "remaining_premium_pct": round(mark / p["entry_credit"] * 100, 1)
            if (mark and p["entry_credit"]) else None,
            "age_h": round((now_ms - p["opened_at_ms"]) / 3.6e6, 1),
            "expires_in_h": round((p["expiry_ms"] - now_ms) / 3.6e6, 1),
            "tp2_pct": p["tp2_pct"], "sl_pct": p["sl_pct"], "hold_h": p["hold_h"],
        }
        if m.get("spot") and m.get("iv"):
            row.update(strike_risk(p["side"], m["spot"], p["strike"],
                                   m["iv"], t_years))
        out.append(row)
    return out


ADVICE_TOOL = {
    "name": "give_advice",
    "description": "Вернуть структурированную рекомендацию по открытым позициям.",
    "input_schema": {
        "type": "object",
        "properties": {
            "market_risk": {"type": "string", "enum": ["low", "medium", "high"]},
            "risk_posture": {
                "type": "string", "enum": ["normal", "tight", "lockdown"],
                "description": ("Режим защиты для бота: normal — обычные выходы; "
                                "tight — включить трейлинг-фиксацию профита "
                                "(риск повышен); lockdown — немедленно снять "
                                "весь профит и заблокировать новые входы "
                                "(острая ситуация). lockdown применяй редко."),
            },
            "market_view": {"type": "string"},
            "tg_summary": {
                "type": "string",
                "description": ("Одна строка ≤90 символов для мобильного "
                                "пуша: суть ситуации и что делаем. Без воды."),
            },
            "positions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "action": {"type": "string",
                                   "enum": ["HOLD", "CLOSE"]},
                        "reason": {"type": "string"},
                        "brief": {"type": "string",
                                  "description": "2-4 слова: почему (для пуша)"},
                    },
                    "required": ["id", "action", "reason", "brief"],
                },
            },
            "summary": {"type": "string"},
            "entry_proposal": {
                "type": ["object", "null"],
                "description": ("Предложение ОДНОГО нового входа, только если "
                                "условия действительно хорошие; обычно null. "
                                "Исполнится лишь после жёстких проверок кода."),
                "properties": {
                    "coin": {"type": "string", "enum": ["ETH", "BTC"]},
                    "side": {"type": "string", "enum": ["P", "C"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                    "brief": {"type": "string"},
                },
                "required": ["coin", "side", "confidence", "reason", "brief"],
            },
        },
        "required": ["market_risk", "risk_posture", "market_view", "tg_summary",
                     "positions", "summary"],
    },
}


def call_claude(payload: dict, cur_posture: str = "normal") -> dict:
    if BACKEND == "cli":
        return call_claude_cli(payload, cur_posture)
    return call_claude_api(payload, cur_posture)


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()


CLI_DISALLOWED_TOOLS = ("Bash,Read,Edit,Write,Glob,Grep,WebFetch,WebSearch,"
                        "Task,TodoWrite,NotebookEdit")


def call_claude_cli(payload: dict, cur_posture: str = "normal") -> dict:
    """Подписочный бэкенд (2026-08-17): headless Claude Code CLI вместо прямого
    API — работает от MAX-подписки (CLAUDE_CODE_OAUTH_TOKEN), не от кредитов.
    Ревью-харденинг: чистое env (иначе CLI молча биллит ANTHROPIC_API_KEY и
    видит биржевые секреты), пустой cwd (никакого контекста репо/CLAUDE.md),
    тулы запрещены, rc!=0 ретраится 1 раз (транзиентные ошибки API тоже дают
    rc=1, текст ошибки CLI кладёт в stdout), таймаут ловится."""
    import subprocess
    import tempfile
    schema = json.dumps(ADVICE_TOOL["input_schema"], ensure_ascii=False)
    prompt = (SYSTEM
              + "\n\n(Здесь нет tool give_advice — вместо вызова инструмента "
                "верни ТОЛЬКО валидный JSON-объект по этой схеме, без code "
                "fence, без пояснений, без текста до/после):\n"
              + schema + "\n\nВходные данные:\n"
              + json.dumps(payload, ensure_ascii=False))
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not tok:
        raise RuntimeError("ADVISOR_BACKEND=cli без CLAUDE_CODE_OAUTH_TOKEN в env")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", "/root"),
           "TERM": "dumb", "CLAUDE_CODE_OAUTH_TOKEN": tok}
    workdir = tempfile.mkdtemp(prefix="advisor_cli_")
    last_err = None
    for _ in range(2):
        try:
            r = subprocess.run(
                ["claude", "-p", "--model", MODEL, "--output-format", "json",
                 "--max-turns", "2", "--disallowedTools", CLI_DISALLOWED_TOOLS],
                input=prompt, capture_output=True, text=True, timeout=180,
                env=env, cwd=workdir)
        except subprocess.TimeoutExpired:
            raise RuntimeError("claude cli timeout (180s)")
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            last_err = RuntimeError(f"claude cli rc={r.returncode}: {err[:300]}")
            continue
        try:
            envelope = json.loads(r.stdout)
            if not isinstance(envelope, dict):
                raise ValueError(f"unexpected envelope type {type(envelope)}")
            if envelope.get("is_error"):
                raise RuntimeError(f"cli is_error: {str(envelope.get('result'))[:300]}")
            advice = json.loads(_strip_fences(envelope.get("result") or ""))
            return _normalize_advice(advice, cur_posture)
        except (ValueError, KeyError, TypeError) as e:
            last_err = RuntimeError(f"cli JSON parse failed: {e}")
    raise last_err


def call_claude_api(payload: dict, cur_posture: str = "normal") -> dict:
    key = os.environ["ANTHROPIC_API_KEY"]
    body = {
        "model": MODEL,
        "max_tokens": 2000,
        "system": SYSTEM,
        "tools": [ADVICE_TOOL],
        "tool_choice": {"type": "tool", "name": "give_advice"},
        "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    }
    r = requests.post(API_URL, json=body, timeout=120, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01",
        "content-type": "application/json"})
    r.raise_for_status()
    for block in r.json()["content"]:
        if block.get("type") == "tool_use" and block.get("name") == "give_advice":
            return _normalize_advice(block["input"], cur_posture)
    raise RuntimeError("no tool_use block in Claude response")


def _normalize_advice(advice: dict, cur_posture: str = "normal") -> dict:
    """Defensive shape repair: the tool schema asks for position OBJECTS, but
    a malformed generation can still slip strings/garbage into the array —
    seen live 2026-08-14. Keep only well-formed {id:int, action:enum} rows so
    every downstream consumer (executions, telegram, scoring) can trust the
    shape; drop the rest rather than crash the tick."""
    # Fail closed: a malformed generation must KEEP the current posture, not
    # relax to 'normal' (which would drop tight/lockdown and re-open entries).
    fallback = cur_posture if cur_posture in ("normal", "tight", "lockdown") \
        else "normal"
    if not isinstance(advice, dict):
        return {"market_risk": "medium", "risk_posture": fallback,
                "market_view": "", "positions": [], "summary": ""}
    clean = []
    for rec in advice.get("positions") or []:
        if (isinstance(rec, dict) and isinstance(rec.get("id"), int)
                and rec.get("action") in ("HOLD", "CLOSE")):
            clean.append({"id": rec["id"], "action": rec["action"],
                          "reason": str(rec.get("reason", "")),
                          "brief": str(rec.get("brief", ""))[:40]})
    advice["positions"] = clean
    if advice.get("risk_posture") not in ("normal", "tight", "lockdown"):
        advice["risk_posture"] = fallback
    ep = advice.get("entry_proposal")
    if not (isinstance(ep, dict) and ep.get("coin") in ("ETH", "BTC")
            and ep.get("side") in ("P", "C")
            and isinstance(ep.get("confidence"), (int, float))):
        advice["entry_proposal"] = None
    for k in ("market_view", "summary"):
        v = advice.get(k)
        if isinstance(v, str) and "</" in v:
            # malformed generation leaks XML-ish tool markup into free text
            advice[k] = v.split("</")[0].strip()
    return advice


EXECUTE_MODE = os.getenv("ADVISOR_EXECUTE", "profit_only")  # off|profit_only|full
MAX_EXEC_PER_HOUR = int(os.getenv("ADVISOR_MAX_EXEC_PER_HOUR", "4"))
ENTRIES_MODE = os.getenv("ADVISOR_ENTRIES", "on")           # on|off
ENTRY_MIN_CONFIDENCE = float(os.getenv("ADVISOR_ENTRY_MIN_CONF", "0.6"))
MAX_ENTRIES_PER_DAY = int(os.getenv("ADVISOR_MAX_ENTRIES_PER_DAY", "2"))
ENTRY_MIN_GAP_H = float(os.getenv("ADVISOR_ENTRY_MIN_GAP_H", "4"))
REVENGE_WINDOW_H = float(os.getenv("ADVISOR_REVENGE_WINDOW_H", "4"))
# CALL-специфичные guardrails (2026-08-22, advisor-only коллы): короткий колл
# в крипте — открытый хвост вверх (squeeze/blow-off), hold всего 24ч, live
# ETH:C −$41/WR 18%. Порог строже, условия «рост выдохся» продублированы кодом.
CALL_MIN_CONF = float(os.getenv("ADVISOR_CALL_MIN_CONF", "0.7"))
CALL_MAX_PER_DAY = int(os.getenv("ADVISOR_CALL_MAX_PER_DAY", "1"))
CALL_MAX_CHG_24H = float(os.getenv("ADVISOR_CALL_MAX_CHG_24H", "0.5"))     # % — не после зелёного дня
CALL_MIN_CHG_24H = float(os.getenv("ADVISOR_CALL_MIN_CHG_24H", "-6.0"))    # % — не в день краха (squeeze)
CALL_MAX_ABS_CHG_1H = float(os.getenv("ADVISOR_CALL_MAX_ABS_CHG_1H", "1.0"))
CALL_MAX_CHG_7D = float(os.getenv("ADVISOR_CALL_MAX_CHG_7D", "12.0"))      # % — не в параболе
CALL_MIN_DIST_7D_HIGH = float(os.getenv("ADVISOR_CALL_MIN_DIST_7D_HIGH", "1.5"))
CALL_FUNDING_MIN = float(os.getenv("ADVISOR_CALL_FUNDING_MIN", "-0.01"))   # %/8ч — шорты не перегружены
CALL_FUNDING_MAX = float(os.getenv("ADVISOR_CALL_FUNDING_MAX", "0.05"))
CALL_KILL_MIN_N = int(os.getenv("ADVISOR_CALL_KILL_MIN_N", "8"))           # предрегистрированный kill
CALL_KILL_WR = float(os.getenv("ADVISOR_CALL_KILL_WR", "0.40"))
CALL_KILL_PNL = float(os.getenv("ADVISOR_CALL_KILL_PNL", "-20.0"))
WAKE_MIN_GAP_MIN = int(os.getenv("ADVISOR_WAKE_MIN_GAP_MIN", "10"))
WAKE_SPOT_MOVE_PCT = float(os.getenv("ADVISOR_WAKE_SPOT_MOVE_PCT", "2.0"))
WAKE_STRIKE_PROX_PCT = float(os.getenv("ADVISOR_WAKE_STRIKE_PROX_PCT", "1.5"))
# 2026-08-22: поз.64 стояла у страйка 32ч → ~200 внеплановых вызовов за 2 дня
# (110 тиков 18.08), все HOLD. Один и тот же триггер (движение монеты /
# близость страйка конкретной позиции) не будит модель чаще раза в N минут.
WAKE_REASON_COOLDOWN_MIN = int(os.getenv("ADVISOR_WAKE_REASON_COOLDOWN_MIN", "30"))
_wake_last: dict[str, int] = {}  # trigger key -> ms последней побудки


def close_policy_allows(snap: dict) -> bool:
    """Разрешён ли CLOSE калиброванной политикой закрытий (пороги и
    обоснование — core/close_policy.py, единые с lockdown-жаткой loop.py).
    Fail-closed: нет данных — вето. snap — строка positions_block
    (profit_pct_of_credit в % кредита, strike_buffer_pct < 0 означает ITM)."""
    profit = snap.get("profit_pct_of_credit")
    pnl_frac = profit / 100.0 if profit is not None else None
    if close_policy.endgame_ok(pnl_frac, snap.get("age_h"), snap.get("hold_h")):
        return True
    buf = snap.get("strike_buffer_pct")
    itm = (buf < 0) if buf is not None else None
    return close_policy.emergency_ok(pnl_frac, itm)


def decide_executions(advice: dict, pos_by_id: dict, prev_advice: dict | None,
                      mode: str, recent_exec_ts: list[int],
                      now_ms: int) -> list[int]:
    """Which CLOSE recommendations actually execute. Guardrails:
    - mode off => nothing; profit_only => only positions in MTM profit;
      full => losing positions too.
    - close_policy_allows: калиброванное вето (эндшпиль или аварийный ITM);
      применяется ВСЕГДА, включая lockdown — паническая фиксация молодого
      профита и была измеренным источником убытка.
    - persistence: a CLOSE fires only if the PREVIOUS advice also said CLOSE
      for that position — unless posture is lockdown (urgent).
    - rate limit: at most MAX_EXEC_PER_HOUR auto-closes per rolling hour.
    Pure function (no I/O) so it is unit-testable."""
    if mode not in ("profit_only", "full"):
        return []
    budget = MAX_EXEC_PER_HOUR - sum(
        1 for t in recent_exec_ts if now_ms - t < 3_600_000)
    if budget <= 0:
        return []
    prev_actions = {}
    if prev_advice:
        prev_actions = {r["id"]: r.get("action")
                        for r in prev_advice.get("positions", [])}
    urgent = advice.get("risk_posture") == "lockdown"
    out = []
    for rec in advice.get("positions", []):
        if rec.get("action") != "CLOSE":
            continue
        snap = pos_by_id.get(rec.get("id"))
        if snap is None or snap.get("unrealized_usd") is None:
            continue
        if mode == "profit_only" and snap["unrealized_usd"] <= 0:
            continue
        if not close_policy_allows(snap):
            continue
        if not urgent and prev_actions.get(rec["id"]) != "CLOSE":
            continue
        out.append(rec["id"])
        if len(out) >= budget:
            break
    return out


def enabled_free_keys(open_positions: list[dict]) -> list[str]:
    """Keys the bot may legally open right now under per_key_cap=1."""
    taken = {f"{p['coin']}:{p['side']}" for p in open_positions
             if p.get("coin") and p.get("side")}
    out = []
    for coin, sides in COIN_SIDES.items():
        for side in sides:
            key = f"{coin}:{side}"
            if key not in DISABLED_KEYS and key not in taken:
                out.append(key)
    return out


def call_kill_active(by_key: dict | None) -> str | None:
    """Предрегистрированный kill-критерий эксперимента advisor-only коллов:
    суммарно по ETH:C+BTC:C advisor-сделкам n>=CALL_KILL_MIN_N и (WR<CALL_KILL_WR
    или PnL<CALL_KILL_PNL) -> коллы запрещены кодом. None = эксперимент жив."""
    n = wins = 0
    pnl = 0.0
    for key, srcs in (by_key or {}).items():
        if key.endswith(":C") and isinstance(srcs, dict) and "advisor" in srcs:
            a = srcs["advisor"]
            n += a.get("n", 0); wins += a.get("wins", 0); pnl += a.get("pnl_usd", 0.0)
    if n < CALL_KILL_MIN_N:
        return None
    wr = wins / n
    if wr < CALL_KILL_WR or pnl < CALL_KILL_PNL:
        return f"call kill-switch: n={n} WR={wr:.0%} PnL={pnl:+.2f}"
    return None


def call_guard(prop: dict, mkt: dict, recent_call_ts: list[int], now_ms: int,
               by_key: dict | None = None) -> str | None:
    """Код-дубль условий промпта для short call. Возвращает причину отказа
    или None. Все пороги — env ADVISOR_CALL_*."""
    if by_key is None:
        return "track record unavailable (fail-closed)"
    kill = call_kill_active(by_key)
    if kill:
        return kill
    if (prop.get("confidence") or 0) < CALL_MIN_CONF:
        return f"confidence<{CALL_MIN_CONF}"
    if sum(1 for t in recent_call_ts if now_ms - t < 24 * 3_600_000) >= CALL_MAX_PER_DAY:
        return "call daily cap"
    c24 = mkt.get("chg_24h_pct")
    c1 = mkt.get("chg_1h_pct")
    c7 = mkt.get("chg_7d_pct")
    dh = mkt.get("dist_from_7d_high_pct")
    if c24 is None or c1 is None or c7 is None or dh is None:
        return "market fields missing"
    if c24 > CALL_MAX_CHG_24H:
        return f"chg_24h {c24:+.1f}% > {CALL_MAX_CHG_24H}"
    if c24 < CALL_MIN_CHG_24H:
        return f"chg_24h {c24:+.1f}% < {CALL_MIN_CHG_24H} (squeeze risk)"
    if abs(c1) > CALL_MAX_ABS_CHG_1H:
        return f"|chg_1h| {c1:+.1f}% > {CALL_MAX_ABS_CHG_1H}"
    if c7 > CALL_MAX_CHG_7D:
        return f"chg_7d {c7:+.1f}% > {CALL_MAX_CHG_7D} (parabolic)"
    if abs(dh) < CALL_MIN_DIST_7D_HIGH:
        return f"dist_from_7d_high {abs(dh):.1f}% < {CALL_MIN_DIST_7D_HIGH}"
    f = mkt.get("funding_rate_pct")
    if f is None:
        return "funding missing"
    if not (CALL_FUNDING_MIN <= f <= CALL_FUNDING_MAX):
        return f"funding {f:+.4f}% outside [{CALL_FUNDING_MIN}, {CALL_FUNDING_MAX}]"
    return None


def decide_entry(advice: dict, market: dict, positions: list[dict],
                 posture: str, recent_entry_ts: list[int],
                 now_ms: int, last_loss_ms: int | None = None,
                 mech_gates: dict | None = None,
                 recent_call_ts: list[int] | None = None,
                 by_key: dict | None = None) -> dict | None:
    """Hard deterministic guardrails for an advisor entry proposal. Returns
    the proposal dict if it may execute, else None. Pure function.
    - ENTRIES_MODE on, confidence floor, valid enabled key
    - posture not lockdown; tight allowed unless market_risk == 'high'
    - key free under per_key_cap (loop re-checks anyway)
    - VRP must be paid (iv_minus_rv24 > 0) and vol must not be accelerating
    - rate: <= MAX_ENTRIES_PER_DAY / rolling 24h, >= ENTRY_MIN_GAP_H apart
    - revenge window: no advisor entries REVENGE_WINDOW_H after a losing
      close (advisor entries only — mechanical entries stay as validated)
    - puts only inside the mechanics' trend window (ret_7d >= -RET_7D_THRESHOLD);
      the counter-trend override was REJECTED on the honest engine (Phase 14)"""
    prop = advice.get("entry_proposal")
    if ENTRIES_MODE != "on" or not isinstance(prop, dict):
        return None
    coin, side = prop.get("coin"), prop.get("side")
    key = f"{coin}:{side}"
    if coin not in ("ETH", "BTC") or side not in ("P", "C"):
        return None
    if side not in COIN_SIDES.get(coin, ()) or key in DISABLED_KEYS:
        return None
    if (prop.get("confidence") or 0) < ENTRY_MIN_CONFIDENCE:
        return None
    # self-lock fix 2026-08-26: tight (typically an ITM position on ONE coin)
    # no longer freezes entries on the other coin; lockdown or market_risk
    # 'high' still do. Advisor positions trail unconditionally either way.
    if posture == "lockdown":
        return None
    if posture == "tight" and advice.get("market_risk") == "high":
        return None
    if last_loss_ms and now_ms - last_loss_ms < REVENGE_WINDOW_H * 3_600_000:
        return None
    if key in {f"{p.get('coin')}:{p.get('side')}" for p in positions}:
        return None
    mkt = market.get(coin) or {}
    if (mkt.get("iv_minus_rv24") or 0) <= 0 or mkt.get("vol_accelerating"):
        return None
    if side == "C":
        why = call_guard(prop, mkt, recent_call_ts or [], now_ms, by_key)
        if why:
            print(f"[advisor] call entry rejected: {why}", flush=True)
            return None
    # Контр-трендовые входы (ревью 2026-08-17): промпт-условия «слив выдохся»
    # продублированы КОДОМ — одна генерация LLM не может быть единственным
    # предохранителем. Fail closed: протухший gate-снапшот = отказ.
    g = (mech_gates or {}).get(coin) or {}
    if g.get("stale", True):
        return None
    r7 = g.get("ret_7d")
    if side == "P" and (r7 is None or r7 < -RET_7D_THRESHOLD):
        # Phase 14 (2026-09-02): override-пут против side-off механики —
        # REJECTED на честном движке (13 вариантов hold/size/OTM/зона).
        # Пут только внутри разрешённого окна; нет ret_7d — отказ.
        print("[advisor] put entry rejected: mechanics side-off", flush=True)
        return None
    if r7 is not None:
        if side == "C" and r7 > RET_7D_THRESHOLD:
            if ((mkt.get("chg_24h_pct") or 99) > 0.5
                    or abs(mkt.get("dist_from_7d_high_pct") or 0) < 1.5):
                return None
    day = [t for t in recent_entry_ts if now_ms - t < 24 * 3_600_000]
    if len(day) >= MAX_ENTRIES_PER_DAY:
        return None
    if day and now_ms - max(day) < ENTRY_MIN_GAP_H * 3_600_000:
        return None
    return prop


def _wake_triggers(open_pos: list[dict], klines: dict[str, list]) -> list[tuple[str, str]]:
    """(key, reason) для всех сработавших триггеров; key — единица кулдауна."""
    out = []
    for coin, kl in klines.items():
        if len(kl) < 4:
            continue
        spot = kl[-1]["close"]
        move = (spot - kl[0]["close"]) / kl[0]["close"] * 100
        if abs(move) >= WAKE_SPOT_MOVE_PCT:
            out.append((f"move:{coin}", f"{coin} двинулся {move:+.1f}% за 15 минут"))
        for p in open_pos:
            if p["coin"] != coin:
                continue
            dist = abs(spot - p["strike"]) / p["strike"] * 100
            if dist <= WAKE_STRIKE_PROX_PCT:
                out.append((f"prox:{p['id']}",
                            f"{coin} спот {spot:.0f} в {dist:.1f}% от страйка "
                            f"{p['strike']:.0f} (позиция #{p['id']})"))
    return out


def pick_wake(triggers: list[tuple[str, str]], now_ms: int) -> str | None:
    """Первый триггер вне кулдауна; фиксирует побудку по его ключу."""
    cd = WAKE_REASON_COOLDOWN_MIN * 60_000
    for key, reason in triggers:
        if now_ms - _wake_last.get(key, -cd) >= cd:
            # модель увидит ВСЕ текущие триггеры в одном вызове — штампуем все
            for k, _ in triggers:
                _wake_last[k] = now_ms
            return reason
    return None


def check_wake(client: BybitClient, now_ms: int,
               stamp_only: bool = False) -> str | None:
    """Cheap market triggers that justify calling the model off-schedule.
    Runs every poll (60s), no LLM cost; per-trigger cooldown (pick_wake).
    stamp_only: после планового тика — модель уже видела текущее состояние,
    активные триггеры уходят в кулдаун без побудки."""
    try:
        conn = repo.connect()
        try:
            open_pos = repo.open_positions(conn)
        finally:
            conn.close()
        klines = {coin: client.get_klines(sym, "5", 4)  # last 15 min
                  for coin, sym in (("BTC", "BTCUSDT"), ("ETH", "ETHUSDT"))}
        live = {f"prox:{p['id']}" for p in open_pos} | {f"move:{c}" for c in klines}
        for k in [k for k in _wake_last if k not in live]:
            _wake_last.pop(k, None)
        triggers = _wake_triggers(open_pos, klines)
        if stamp_only:
            for k, _ in triggers:
                _wake_last[k] = now_ms
            return None
        return pick_wake(triggers, now_ms)
    except Exception as e:
        print(f"[advisor] wake check failed: {e}", flush=True)
    return None


def _short_symbol(symbol: str) -> str:
    # "ETH-21AUG26-1875-P-USDT" -> "ETH P1875"
    parts = symbol.split("-")
    if len(parts) >= 4:
        return f"{parts[0]} {parts[3]}{parts[2]}"
    return symbol


def _pos_icon(pct: float | None, buf: float | None) -> str:
    if pct is None:
        return "⚪"
    if pct <= -50 or (buf is not None and buf < -3):
        return "🔴"
    if pct < -20:
        return "🟠"
    if pct >= 20:
        return "🟢"
    return "🟡"


def _mech_hint(snap: dict) -> str:
    """Что сделает механика дальше — чтобы человек видел альтернативу
    ручному действию: порог TP2/SL и когда time-stop."""
    parts = []
    pct = snap.get("profit_pct_of_credit")
    tp2, sl = snap.get("tp2_pct"), snap.get("sl_pct")
    if pct is not None and pct < 0 and sl is not None:
        parts.append(f"SL −{sl * 100:.0f}%")
    elif tp2 is not None:
        parts.append(f"TP2 +{tp2 * 100:.0f}%")
    age, hold = snap.get("age_h"), snap.get("hold_h")
    if age is not None and hold:
        left = max(0.0, hold - age)
        parts.append(f"time-stop {left:.0f}ч" if left >= 1 else "time-stop <1ч")
    return " / ".join(parts)


def _pos_line(rec: dict, snap: dict, executed: list[int],
              vetoed: set[int] | None) -> str:
    pid = rec.get("id")
    sym = _short_symbol(snap.get("symbol", f"#{pid}"))
    pnl = snap.get("unrealized_usd")
    pct = snap.get("profit_pct_of_credit")
    buf = snap.get("strike_buffer_pct")
    pnl_s = (f" {pnl:+.1f}$" if pnl is not None else "") + \
            (f" ({pct:+.0f}%)" if pct is not None else "")
    age, hold = snap.get("age_h"), snap.get("hold_h")
    age_s = f" · {age:.0f}/{hold:.0f}ч" if (age is not None and hold) else ""
    buf_s = ""
    if buf is not None:
        buf_s = f" · ITM {buf:+.1f}%" if buf < 0 else f" · до страйка {buf:.1f}%"
    brief = (rec.get("brief") or "").strip()
    action = rec.get("action")
    head = f"{_pos_icon(pct, buf)} {sym}{pnl_s}{age_s}{buf_s}"
    if pid in executed:
        return f"🤖 закрыл {sym}{pnl_s} · {brief}"
    if action == "CLOSE" and vetoed and pid in vetoed:
        return f"{head}\n   🔒 CLOSE отклонён политикой · {brief} · механика: {_mech_hint(snap)}"
    if action == "CLOSE":
        return (f"{head}\n   ❗ {brief} → закрой сам (бот убыточные не закрывает)"
                f" · иначе механика: {_mech_hint(snap)}")
    return f"{head} · {brief or 'держим'} · {_mech_hint(snap)}"


def format_tg(advice: dict, pos_by_id: dict, executed: list[int],
              posture_change: tuple[str, str] | None,
              equity: float | None, start_equity: float | None,
              vetoed: set[int] | None = None,
              market: dict | None = None) -> str:
    """Mobile-first push (2026-08-28 redesign по запросу пользователя —
    ручные закрытия по TG стали штатным каналом, значит в сообщении должно
    быть всё для решения): шапка (риск/posture/equity) → рынок одной строкой
    → по одной строке на КАЖДУЮ позицию (PnL, возраст, дистанция до страйка,
    что сделает механика) → действие/вход. Полные reasons — только в
    advice.jsonl. vetoed — CLOSE, отклонённые политикой: «держим», не
    команда человеку (ревью 2026-08-17)."""
    risk = advice.get("market_risk", "?")
    icon = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(risk, "❔")
    posture = advice.get("risk_posture", "?")
    head = f"{icon} Jony · риск {risk} · {posture}"
    if posture_change:
        head += f" ({posture_change[0]}→{posture_change[1]})"
    if equity is not None:
        head += f" · 💰 ${equity:.0f}"
        if start_equity:
            head += f" ({(equity - start_equity) / start_equity * 100:+.1f}%)"
    lines = [head]
    if market:
        mk = []
        for coin in ("BTC", "ETH"):
            m = market.get(coin) or {}
            if m.get("spot"):
                chg = m.get("chg_24h_pct")
                mk.append(f"{coin} {m['spot']:,.0f}".replace(",", " ")
                          + (f" ({chg:+.1f}%)" if chg is not None else ""))
        if mk:
            lines.append("📊 " + " · ".join(mk))
    tg_sum = (advice.get("tg_summary") or "").strip()
    if tg_sum:
        lines.append(f"💬 {tg_sum[:120]}")
    seen = set()
    for rec in advice.get("positions", []):
        pid = rec.get("id")
        if pid in seen:
            continue
        seen.add(pid)
        lines.append(_pos_line(rec, pos_by_id.get(pid, {}), executed, vetoed))
    if not advice.get("positions") and not pos_by_id:
        lines.append("— позиций нет")
    return "\n".join(x for x in lines if x)


def mechanical_gates_block(conn) -> dict:
    """Состояние механических гейтов per coin (P2 2026-08-17): советник должен
    видеть, что в side-off механика не войдёт вообще — его entry_proposal
    в этом состоянии единственный источник входа."""
    now_ms = int(time.time() * 1000)
    out = {}
    for coin in COIN_SIDES:
        ws = repo.get_window_status(conn, coin)
        ev = (ws or {}).get("ev") or {}
        checked = (ws or {}).get("checked_at_ms")
        # staleness по образцу core/proximity (ревью 2026-08-17): протухший
        # снапшот (loop мёртв/завис) нельзя выдавать за живое состояние
        stale = checked is None or now_ms - int(checked) > GATES_STALE_MS
        out[coin] = {"no_side_allowed": bool(ev.get("no_side_allowed")),
                     "ret_7d": ev.get("ret_7d"), "stale": stale,
                     "age_s": round((now_ms - int(checked)) / 1000) if checked else None}
    return out



_kill_notified: list = []  # одноразовый TG при срабатывании kill-switch
_last_by_key: dict | None = None  # per-key скоринг из /advice/score; None = API недоступен (fail-closed для коллов)


def track_record_block() -> dict | None:
    """Собственный скоринг советника из /advice/score (P2 2026-08-17):
    подаётся ему же на вход для самокоррекции. None при недоступности API."""
    global _last_by_key
    try:
        r = requests.get(f"{API_BASE}/advice/score", timeout=5)
        j = r.json()
        _last_by_key = j.get("by_key") or {}
        out = dict(j.get("aggregate") or {})
        for k in ("by_key", "confidence_calibration"):
            if j.get(k):
                out[k] = j[k]
        return out or None
    except Exception as e:
        print(f"[advisor] track_record unavailable: {e}", flush=True)
        _last_by_key = None
        return None



_last_call_ts: list[int] = []  # call-входы <24ч, заполняется _load_history


def _load_history(now_ms: int) -> tuple[dict | None, list[int], list[int]]:
    """(previous advice record, auto-close timestamps <1h,
    advisor-entry timestamps <24h); call-входы <24ч -> _last_call_ts."""
    prev = None
    exec_ts: list[int] = []
    entry_ts: list[int] = []
    call_ts: list[int] = []
    _last_call_ts.clear()
    if not ADVICE_PATH.exists():
        return prev, exec_ts, entry_ts
    try:
        lines = ADVICE_PATH.read_text().strip().splitlines()
        if lines:
            prev_rec = json.loads(lines[-1])
            prev = {"hours_ago": round((now_ms - prev_rec["ts_ms"]) / 3.6e6, 1),
                    "advice": prev_rec["advice"]}
        for ln in lines[-80:]:
            rec = json.loads(ln)
            if now_ms - rec["ts_ms"] < 3_600_000:
                exec_ts += [rec["ts_ms"]] * len(rec.get("executed", []))
            er = rec.get("entry_requested")
            if er and now_ms - rec["ts_ms"] < 24 * 3_600_000:
                entry_ts.append(rec["ts_ms"])
                if isinstance(er, dict) and er.get("side") == "C":
                    call_ts.append(rec["ts_ms"])
    except Exception as e:
        print(f"[advisor] history read failed: {e}", flush=True)
    _last_call_ts.extend(call_ts)
    return prev, exec_ts, entry_ts


def tick(client: BybitClient, wake_reason: str | None = None) -> dict | None:
    now_ms = int(time.time() * 1000)
    pos = positions_block(client, now_ms)
    market = market_block(client)
    conn = repo.connect()
    try:
        state = repo.try_get_state(conn) or {}
        cur_posture, _ = repo.get_risk_posture(conn)
        lookback_h = max(24.0, REVENGE_WINDOW_H)
        closed = repo.closed_pnls(conn, now_ms - int(lookback_h * 3_600_000))
        mech_gates = mechanical_gates_block(conn)
    finally:
        conn.close()
    last_loss_ms = max((ts for ts, p in closed if p < 0), default=None)
    prev, recent_exec_ts, recent_entry_ts = _load_history(now_ms)
    payload = {
        "now_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
        "wake_reason": wake_reason,
        "current_risk_posture": cur_posture,
        "account": {"equity_usd": state.get("equity_usd"),
                    "start_equity_usd": state.get("start_equity_usd"),
                    "open_positions": len(pos)},
        "keys_available": enabled_free_keys(pos),
        "advisor_entries_today": len(recent_entry_ts),
        "market": market,
        "open_positions": pos,
        "your_previous_advice": prev,
        "mechanical_gates": mech_gates,
        "your_track_record": track_record_block(),
    }
    advice = call_claude(payload, cur_posture)
    if pos and not advice.get("positions"):
        # malformed generation dropped every per-position row (seen live
        # 2026-08-14: rows serialized as text into summary). One retry —
        # a fresh sample is usually well-formed; without rows the
        # 2-consecutive-CLOSE persistence chain silently breaks.
        print("[advisor] empty positions with open book — retrying once",
              flush=True)
        advice = call_claude(payload, cur_posture)

    # 1) risk posture -> bot_control (loop reads it every exit/entry tick).
    # Written EVERY tick, even unchanged: a live advisor confirming lockdown
    # must refresh posture_updated_ms, or the dead-advisor staleness guard
    # (effective_posture, LOCKDOWN_STALE_H) would decay it to tight — that
    # guard is for a silent advisor, not a malformed-but-alive one.
    new_posture = advice.get("risk_posture", "normal")
    conn = repo.connect()
    try:
        repo.set_risk_posture(conn, new_posture, now_ms)
    finally:
        conn.close()

    # 2) auto-close via the loop's own close-request queue
    pos_by_id = {p["id"]: p for p in pos}
    exec_ids = decide_executions(advice, pos_by_id,
                                 prev["advice"] if prev else None,
                                 EXECUTE_MODE, recent_exec_ts, now_ms)
    if exec_ids:
        conn = repo.connect()
        try:
            for pid in exec_ids:
                repo.request_close_position(conn, pid, now_ms)
        finally:
            conn.close()

    # 3) entry proposal -> loop's entry queue (guardrails in decide_entry)
    kill = call_kill_active(_last_by_key)
    if kill and not _kill_notified:
        _kill_notified.append(True)
        notify(f"JONY CALL-эксперимент остановлен кодом: {kill}")
    entry = decide_entry(advice, market, pos, new_posture,
                         recent_entry_ts, now_ms, last_loss_ms,
                         mech_gates=mech_gates, recent_call_ts=list(_last_call_ts),
                         by_key=_last_by_key)
    if entry:
        conn = repo.connect()
        try:
            repo.request_entry(conn, entry["coin"], entry["side"], now_ms,
                               json.dumps({"advisor_reason": entry.get("reason", ""),
                                           "confidence": entry.get("confidence")},
                                          ensure_ascii=False))
        finally:
            conn.close()

    # CLOSE-советы, отклонённые политикой закрытий: в пуше — «держим», не
    # «закрой сам», и сами по себе не делают тик actionable (иначе модель,
    # упорно советующая ветированный CLOSE, спамила бы пушами каждый час)
    vetoed = {r.get("id") for r in advice.get("positions", [])
              if r.get("action") == "CLOSE" and r.get("id") in pos_by_id
              and not close_policy_allows(pos_by_id[r.get("id")])}

    record = {"ts_ms": now_ms, "model": MODEL, "wake_reason": wake_reason,
              "posture": new_posture, "executed": exec_ids,
              "policy_vetoed": sorted(vetoed),
              "entry_requested": entry, "input": payload, "advice": advice}
    ADVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ADVICE_PATH.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    actionable = (advice.get("market_risk") == "high" or exec_ids or entry
                  or new_posture != cur_posture
                  or any(r.get("action") == "CLOSE" and r["id"] not in vetoed
                         for r in advice.get("positions", [])))
    if NOTIFY_MODE == "all" or (NOTIFY_MODE == "actionable" and actionable
                                and (pos or entry)):
        try:
            msg = format_tg(
                advice, pos_by_id, exec_ids,
                (cur_posture, new_posture) if new_posture != cur_posture else None,
                state.get("equity_usd"), state.get("start_equity_usd"),
                vetoed=vetoed, market=payload.get("market"))
            if entry:
                msg += (f"\n🚀 Вход {entry['coin']} {entry['side']}"
                        f" (уверенность {entry.get('confidence', 0):.2f})"
                        f" · {entry.get('brief', '')} → бот открывает")
            notify(msg)
        except Exception as e:
            print(f"[advisor] telegram failed: {e}", flush=True)
    print(f"[advisor] tick ok: risk={advice.get('market_risk')} "
          f"posture={new_posture} executed={exec_ids} "
          f"entry={entry and (entry['coin'] + ':' + entry['side'])} "
          f"n_pos={len(pos)} wake={wake_reason!r}", flush=True)
    return advice


def main() -> None:
    client = BybitClient()
    if "--once" in sys.argv:
        advice = tick(client)
        print(json.dumps(advice, ensure_ascii=False, indent=2))
        return
    last_llm_ms = 0
    fail_streak = 0
    while True:
        try:
            now_ms = int(time.time() * 1000)
            due = now_ms - last_llm_ms >= INTERVAL_S * 1000
            wake = None
            if not due and now_ms - last_llm_ms >= WAKE_MIN_GAP_MIN * 60_000:
                wake = check_wake(client, now_ms)
            if due or wake:
                tick(client, wake_reason=wake)
                last_llm_ms = now_ms
                fail_streak = 0
                if due:
                    check_wake(client, now_ms, stamp_only=True)
        except Exception as e:
            fail_streak += 1
            print(f"[advisor] tick failed (streak={fail_streak}): {e}",
                  flush=True)
            # единичные транзиентные сбои в TG не шлём (канал чистый);
            # алерт только когда советник реально лежит: streak-порог,
            # дальше повтор ~раз в 2ч (24 сбоя при 5-мин ретрае)
            if fail_streak == FAIL_NOTIFY_STREAK or (
                    fail_streak > FAIL_NOTIFY_STREAK
                    and fail_streak % 24 == 0):
                try:
                    notify(f"ADVISOR DOWN: {fail_streak} тиков подряд, "
                           f"последняя ошибка: {str(e)[:200]}")
                except Exception:
                    pass
            # retry in ~5 min: no hot-loop, but a transient failure must not
            # push the next scheduled call out by a whole hour
            last_llm_ms = int(time.time() * 1000) - (INTERVAL_S - 300) * 1000
        time.sleep(60)


if __name__ == "__main__":
    main()
