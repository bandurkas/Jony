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
from services.bybit_client import BybitClient
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
Ответ давай вызовом tool give_advice: market_view/summary/reason — кратко и
по-русски; по каждой открытой позиции ровно одна запись в positions.
CLOSE — забрать профит/резать риск сейчас; WATCH — граница, проверить через час.
"""


def pct(a: float, b: float) -> float | None:
    return round((a - b) / b * 100, 2) if (a and b) else None


def market_block(client: BybitClient) -> dict:
    out = {}
    for coin, sym in (("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")):
        kl = client.get_klines(sym, "60", 168)  # 7d of 1h bars, oldest-first
        closes = [k["close"] for k in kl]
        if len(closes) < 25:
            continue
        last = closes[-1]
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        rv24 = (sum(r * r for r in rets[-24:]) / 24) ** 0.5 * math.sqrt(24 * 365)
        rv24_prev = (sum(r * r for r in rets[-48:-24]) / 24) ** 0.5 * math.sqrt(24 * 365)
        out[coin] = {
            "spot": last,
            "chg_1h_pct": pct(last, closes[-2]),
            "chg_24h_pct": pct(last, closes[-25]),
            "chg_7d_pct": pct(last, closes[0]),
            "rv24_annualized": round(rv24, 3),
            "rv24_prev_day": round(rv24_prev, 3),
            "vol_accelerating": rv24 > rv24_prev * 1.3,
        }
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
        "required": ["market_risk", "market_view", "positions", "summary"],
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


def tick(client: BybitClient) -> dict | None:
    now_ms = int(time.time() * 1000)
    pos = positions_block(client, now_ms)
    market = market_block(client)
    conn = repo.connect()
    try:
        state = repo.try_get_state(conn) or {}
    finally:
        conn.close()
    payload = {
        "now_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
        "account": {"equity_usd": state.get("equity_usd"),
                    "start_equity_usd": state.get("start_equity_usd"),
                    "open_positions": len(pos)},
        "market": market,
        "open_positions": pos,
    }
    advice = call_claude(payload)
    record = {"ts_ms": now_ms, "model": MODEL, "input": payload, "advice": advice}
    ADVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ADVICE_PATH.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    actionable = (advice.get("market_risk") == "high"
                  or any(r.get("action") in ("CLOSE", "WATCH")
                         for r in advice.get("positions", [])))
    if NOTIFY_MODE == "all" or (NOTIFY_MODE == "actionable" and actionable and pos):
        try:
            notify(format_tg(advice, len(pos)))
        except Exception as e:
            print(f"[advisor] telegram failed: {e}", flush=True)
    print(f"[advisor] tick ok: risk={advice.get('market_risk')} "
          f"n_pos={len(pos)} actions="
          f"{[r.get('action') for r in advice.get('positions', [])]}", flush=True)
    return advice


def main() -> None:
    client = BybitClient()
    if "--once" in sys.argv:
        advice = tick(client)
        print(json.dumps(advice, ensure_ascii=False, indent=2))
        return
    while True:
        try:
            tick(client)
        except Exception as e:
            print(f"[advisor] tick failed: {e}", flush=True)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
