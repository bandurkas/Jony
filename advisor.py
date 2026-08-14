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

from db import repo
from services.bybit_client import BybitClient, pick_atm_option
from services.telegram_notify import notify

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.getenv("ADVISOR_MODEL", "claude-sonnet-5")
INTERVAL_S = int(os.getenv("ADVISOR_INTERVAL_MIN", "60")) * 60
NOTIFY_MODE = os.getenv("ADVISOR_NOTIFY_MODE", "actionable")
ADVICE_PATH = Path(os.getenv("ADVICE_PATH", "data/advice.jsonl"))

SYSTEM = """Ты — риск-советник по проданным (short) крипто-опционам на Bybit.
Счёт продаёт недельные ATM опционы (short put / short call) и зарабатывает
тету; главный риск — резкое движение базового актива, сжигающее накопленную
прибыль. Задача: защитить УЖЕ НАКОПЛЕННУЮ марк-ту-маркет прибыль открытых
позиций. Не жадничать: если позиция набрала заметный профит и рыночный фон
ухудшается — рекомендуй забирать. Учитывай: короткие путы страдают при
падении, короткие коллы — при росте; ускорение волатильности вредит обоим.
Подсказки по данным: iv_minus_rv24 > 0 — продажа волы оплачивается (фон для
удержания лучше); funding_rate экстремумы и резкий рост OI — признак
перегретого позиционирования; dist_from_7d_high/low — где спот в диапазоне.
your_previous_advice — твоя рекомендация в прошлый раз: сохраняй
преемственность (не дёргай HOLD↔CLOSE и posture без причины), но признавай
смену обстановки. wake_reason в запросе означает срочный вызов по рыночному
триггеру, а не плановый часовой.

Твои решения ИСПОЛНЯЮТСЯ автоматически:
- risk_posture пишется в бота: tight включает трейлинг-фиксацию профита,
  lockdown снимает весь профит и блокирует входы. Это твой главный рычаг
  при ухудшении фона.
- CLOSE по профитной позиции закрывает её реально (после двух согласных
  вызовов подряд, либо сразу при lockdown). CLOSE по убыточной — только
  уведомление человеку.
Ответ давай вызовом tool give_advice: market_view/summary/reason — кратко и
по-русски; по каждой открытой позиции ровно одна запись в positions.
CLOSE — забрать профит/резать риск сейчас; WATCH — граница, проверить позже.
"""


def pct(a: float, b: float) -> float | None:
    return round((a - b) / b * 100, 2) if (a and b) else None


def market_block(client: BybitClient) -> dict:
    out = {}
    now_ms = int(time.time() * 1000)
    for coin, sym in (("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")):
        kl = client.get_klines(sym, "60", 168)  # 7d of 1h bars, oldest-first
        closes = [k["close"] for k in kl]
        if len(closes) < 25:
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
    marks_by_coin = {}
    for coin in {p["coin"] for p in open_pos}:
        try:
            marks_by_coin[coin] = client.get_option_marks(coin)
        except Exception:
            marks_by_coin[coin] = {}
    out = []
    for p in open_pos:
        m = marks_by_coin.get(p["coin"], {}).get(p["option_symbol"]) or {}
        mark = m.get("mark")
        unreal = round((p["entry_credit"] - mark) * p["qty"], 2) if mark else None
        credit_total = p["entry_credit"] * p["qty"]
        out.append({
            "id": p["id"], "symbol": p["option_symbol"], "side": p["side"],
            "qty": p["qty"], "entry_credit": p["entry_credit"],
            "current_mark": mark, "unrealized_usd": unreal,
            "profit_pct_of_credit": round(unreal / credit_total * 100, 1)
            if (unreal is not None and credit_total) else None,
            "age_h": round((now_ms - p["opened_at_ms"]) / 3.6e6, 1),
            "expires_in_h": round((p["expiry_ms"] - now_ms) / 3.6e6, 1),
            "tp2_pct": p["tp2_pct"], "sl_pct": p["sl_pct"], "hold_h": p["hold_h"],
        })
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
            "positions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "action": {"type": "string",
                                   "enum": ["HOLD", "CLOSE", "WATCH"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "action", "reason"],
                },
            },
            "summary": {"type": "string"},
        },
        "required": ["market_risk", "risk_posture", "market_view", "positions",
                     "summary"],
    },
}


def call_claude(payload: dict) -> dict:
    key = os.environ["ANTHROPIC_API_KEY"]
    body = {
        "model": MODEL,
        "max_tokens": 1500,
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
            return block["input"]
    raise RuntimeError("no tool_use block in Claude response")


EXECUTE_MODE = os.getenv("ADVISOR_EXECUTE", "profit_only")  # off|profit_only|full
MAX_EXEC_PER_HOUR = int(os.getenv("ADVISOR_MAX_EXEC_PER_HOUR", "4"))
WAKE_MIN_GAP_MIN = int(os.getenv("ADVISOR_WAKE_MIN_GAP_MIN", "10"))
WAKE_SPOT_MOVE_PCT = float(os.getenv("ADVISOR_WAKE_SPOT_MOVE_PCT", "2.0"))
WAKE_STRIKE_PROX_PCT = float(os.getenv("ADVISOR_WAKE_STRIKE_PROX_PCT", "1.5"))


def decide_executions(advice: dict, pos_by_id: dict, prev_advice: dict | None,
                      mode: str, recent_exec_ts: list[int],
                      now_ms: int) -> list[int]:
    """Which CLOSE recommendations actually execute. Guardrails:
    - mode off => nothing; profit_only => only positions in MTM profit;
      full => losing positions too.
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
        if not urgent and prev_actions.get(rec["id"]) != "CLOSE":
            continue
        out.append(rec["id"])
        if len(out) >= budget:
            break
    return out


def check_wake(client: BybitClient, now_ms: int) -> str | None:
    """Cheap market triggers that justify calling the model off-schedule.
    Runs every poll (60s), no LLM cost."""
    try:
        conn = repo.connect()
        try:
            open_pos = repo.open_positions(conn)
        finally:
            conn.close()
        for coin, sym in (("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")):
            kl = client.get_klines(sym, "5", 4)  # last 15 min
            if len(kl) < 4:
                continue
            spot = kl[-1]["close"]
            move = (spot - kl[0]["close"]) / kl[0]["close"] * 100
            if abs(move) >= WAKE_SPOT_MOVE_PCT:
                return f"{coin} двинулся {move:+.1f}% за 15 минут"
            for p in open_pos:
                if p["coin"] != coin:
                    continue
                dist = abs(spot - p["strike"]) / p["strike"] * 100
                if dist <= WAKE_STRIKE_PROX_PCT:
                    return (f"{coin} спот {spot:.0f} в {dist:.1f}% от страйка "
                            f"{p['strike']:.0f} (позиция #{p['id']})")
    except Exception as e:
        print(f"[advisor] wake check failed: {e}", flush=True)
    return None


def format_tg(advice: dict, n_open: int) -> str:
    risk = advice.get("market_risk", "?")
    icon = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(risk, "❔")
    lines = [f"{icon} Jony advisor — риск {risk}, позиций {n_open}",
             advice.get("market_view", "")]
    for rec in advice.get("positions", []):
        if rec.get("action") in ("CLOSE", "WATCH"):
            act = "❗CLOSE" if rec["action"] == "CLOSE" else "👀 WATCH"
            lines.append(f"{act} #{rec['id']}: {rec.get('reason', '')}")
    lines.append(advice.get("summary", ""))
    return "\n".join(x for x in lines if x)


def _load_history(now_ms: int) -> tuple[dict | None, list[int]]:
    """(previous advice record, timestamps of auto-closes in the last hour)."""
    prev = None
    exec_ts: list[int] = []
    if not ADVICE_PATH.exists():
        return prev, exec_ts
    try:
        lines = ADVICE_PATH.read_text().strip().splitlines()
        if lines:
            prev_rec = json.loads(lines[-1])
            prev = {"hours_ago": round((now_ms - prev_rec["ts_ms"]) / 3.6e6, 1),
                    "advice": prev_rec["advice"]}
        for ln in lines[-30:]:
            rec = json.loads(ln)
            if now_ms - rec["ts_ms"] < 3_600_000:
                exec_ts += [rec["ts_ms"]] * len(rec.get("executed", []))
    except Exception as e:
        print(f"[advisor] history read failed: {e}", flush=True)
    return prev, exec_ts


def tick(client: BybitClient, wake_reason: str | None = None) -> dict | None:
    now_ms = int(time.time() * 1000)
    pos = positions_block(client, now_ms)
    market = market_block(client)
    conn = repo.connect()
    try:
        state = repo.try_get_state(conn) or {}
        cur_posture, _ = repo.get_risk_posture(conn)
    finally:
        conn.close()
    prev, recent_exec_ts = _load_history(now_ms)
    payload = {
        "now_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
        "wake_reason": wake_reason,
        "current_risk_posture": cur_posture,
        "account": {"equity_usd": state.get("equity_usd"),
                    "start_equity_usd": state.get("start_equity_usd"),
                    "open_positions": len(pos)},
        "market": market,
        "open_positions": pos,
        "your_previous_advice": prev,
    }
    advice = call_claude(payload)

    # 1) risk posture -> bot_control (loop reads it every exit/entry tick)
    new_posture = advice.get("risk_posture", "normal")
    if new_posture != cur_posture:
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

    record = {"ts_ms": now_ms, "model": MODEL, "wake_reason": wake_reason,
              "posture": new_posture, "executed": exec_ids,
              "input": payload, "advice": advice}
    ADVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ADVICE_PATH.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    actionable = (advice.get("market_risk") == "high" or exec_ids
                  or new_posture != cur_posture
                  or any(r.get("action") in ("CLOSE", "WATCH")
                         for r in advice.get("positions", [])))
    if NOTIFY_MODE == "all" or (NOTIFY_MODE == "actionable" and actionable and pos):
        try:
            msg = format_tg(advice, len(pos))
            if new_posture != cur_posture:
                msg = f"⚙️ posture {cur_posture} → {new_posture}\n" + msg
            if exec_ids:
                msg += "\n" + "\n".join(
                    f"🤖 АВТО-ЗАКРЫТИЕ #{pid} (профит "
                    f"${pos_by_id[pid].get('unrealized_usd', 0):+.2f})"
                    for pid in exec_ids)
            notify(msg)
        except Exception as e:
            print(f"[advisor] telegram failed: {e}", flush=True)
    print(f"[advisor] tick ok: risk={advice.get('market_risk')} "
          f"posture={new_posture} executed={exec_ids} n_pos={len(pos)} "
          f"wake={wake_reason!r}", flush=True)
    return advice


def main() -> None:
    client = BybitClient()
    if "--once" in sys.argv:
        advice = tick(client)
        print(json.dumps(advice, ensure_ascii=False, indent=2))
        return
    last_llm_ms = 0
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
        except Exception as e:
            print(f"[advisor] tick failed: {e}", flush=True)
            # retry in ~5 min: no hot-loop, but a transient failure must not
            # push the next scheduled call out by a whole hour
            last_llm_ms = int(time.time() * 1000) - (INTERVAL_S - 300) * 1000
        time.sleep(60)


if __name__ == "__main__":
    main()
