"""Jony main loop — multi-asset VRP basket paper bot (ETH P+C, BTC C-only).

Once per minute: (1) manage exits on open positions against live option
marks, (2) run the per-coin gate check inside the current 5-minute window
(tol1 persistence). At the window's last minute (second >= 50) a surviving
window fires: pick the ATM weekly, paper-fill at live bid, size by the
backtest's margin engine, persist + notify.

Restart semantics: cooldowns and CB live in bot_state (survive restarts);
the current 5m window tracker is ephemeral (a restart forfeits at most one
window — same as Sniper1 redeploys).
"""
from __future__ import annotations

import json
import time
import traceback

from core.strategy import (
    COIN_SIDES, evaluate_conditions, exit_params, window_fail_step,
)
from db import repo
from services import config, portfolio
from services.bybit_client import bybit_client, pick_atm_option
from services.telegram_notify import notify

FIVE_MIN = 5


def fetch_klines(coin: str) -> tuple[list, list, list]:
    sym = config.COIN_SPEC[coin]["symbol"]
    k5 = bybit_client.get_klines(sym, "5", config.KLINE_LIMIT_5M)
    k15 = bybit_client.get_klines(sym, "15", config.KLINE_LIMIT_15M)
    k1h = bybit_client.get_klines(sym, "60", config.KLINE_LIMIT_1H)
    return k5, k15, k1h


def close_fill_price(m: dict) -> float:
    """Paper buy-to-close: pay the ask when quoted, else mark +1%."""
    if m.get("ask"):
        return m["ask"]
    return (m.get("mark") or 0.0) * 1.01


def manage_exits(conn, state: dict, now_ms: int) -> dict:
    """TP2 / SL / time-stop / expiry settlement for every open position.
    Decision on mark price (Sniper1 convention), fill at ask (fallback mark+1%)."""
    open_pos = repo.open_positions(conn)
    if not open_pos:
        return state
    marks_by_coin: dict[str, dict] = {}
    for coin in {p["coin"] for p in open_pos}:
        marks_by_coin[coin] = bybit_client.get_option_marks(coin)

    for p in open_pos:
        m = marks_by_coin.get(p["coin"], {}).get(p["option_symbol"])
        entry = p["entry_credit"]

        if m is None or not m.get("mark"):
            if now_ms >= p["expiry_ms"]:
                # Settle at intrinsic — needs spot; approximate with strike-side
                # worst case only if spot is unavailable this tick.
                k5 = bybit_client.get_klines(config.COIN_SPEC[p["coin"]]["symbol"], "5", 1)
                if not k5:
                    continue
                spot = k5[-1]["close"]
                intrinsic = max(0.0, spot - p["strike"]) if p["side"] == "C" \
                    else max(0.0, p["strike"] - spot)
                _close(conn, state, p, now_ms, intrinsic, "expiry_settle",
                       "closed_time")
                state = repo.get_state(conn)
            continue

        mark = m["mark"]
        pnl_pct_mark = (entry - mark) / entry if entry > 0 else 0.0
        held_h = (now_ms - p["opened_at_ms"]) / 3_600_000

        reason = None
        status = None
        if pnl_pct_mark >= p["tp2_pct"]:
            reason, status = "tp2", "closed_tp2"
        elif pnl_pct_mark <= -p["sl_pct"]:
            reason, status = "sl", "closed_sl"
        elif held_h >= p["hold_h"]:
            reason, status = "time_stop", "closed_time"
        if reason:
            _close(conn, state, p, now_ms, close_fill_price(m), reason, status)
            state = repo.get_state(conn)
    return state


def _close(conn, state: dict, p: dict, now_ms: int, exit_debit: float,
           reason: str, status: str) -> None:
    entry = p["entry_credit"]
    qty = p["qty"]
    pnl_pct = (entry - exit_debit) / entry if entry > 0 else 0.0
    fee_close = portfolio.fee_usd(p["strike"] * qty, exit_debit * qty)
    pnl_usd = (entry - exit_debit) * qty - p["fee_open_usd"] - fee_close
    repo.close_position(conn, p["id"], status=status, closed_at_ms=now_ms,
                        exit_debit=exit_debit, exit_reason=reason,
                        pnl_pct=round(pnl_pct * 100, 2),
                        pnl_usd=round(pnl_usd, 4))

    equity = state["equity_usd"] + pnl_usd
    pnls = json.loads(state["recent_pnls_json"])
    pnls = (pnls + [pnl_pct])[-50:]
    cb_until = state["cb_cooldown_until_ms"]
    if pnl_pct <= 0:
        cb_until = now_ms + config.CB_PAUSE_HOURS * 3_600_000
    repo.update_state(conn, equity_usd=equity,
                      recent_pnls_json=json.dumps(pnls),
                      cb_cooldown_until_ms=cb_until)
    notify(f"CLOSE {p['coin']} {p['side']} {p['option_symbol']} {reason} "
           f"pnl ${pnl_usd:+.2f} ({pnl_pct*100:+.1f}% of premium) | "
           f"equity ${equity:.2f}"
           + (f" | CB until +{config.CB_PAUSE_HOURS}h" if pnl_pct <= 0 else ""))


def try_fire(conn, state: dict, coin: str, ev: dict, now_ms: int) -> None:
    side = ev.get("active_side")
    spot = ev.get("spot") or 0.0
    if side is None or side not in COIN_SIDES[coin] or spot <= 0:
        repo.insert_signal_audit(conn, now_ms, coin, side, False, "no_signal",
                                 spot, ev)
        return

    if portfolio.cb_active(state["cb_cooldown_until_ms"], now_ms):
        repo.insert_signal_audit(conn, now_ms, coin, side, False, "cb_active",
                                 spot, ev)
        return

    last_fired = json.loads(state["last_fired_json"])
    key = f"{coin}:{side}"
    cooldown_ms = config.COOLDOWN_BARS * 300_000
    if now_ms - int(last_fired.get(key, 0)) < cooldown_ms:
        repo.insert_signal_audit(conn, now_ms, coin, side, False, "cooldown",
                                 spot, ev)
        return

    open_pos = repo.open_positions(conn)
    block = portfolio.can_open(open_pos, coin)
    if block:
        repo.insert_signal_audit(conn, now_ms, coin, side, False, block, spot, ev)
        return

    chain = bybit_client.get_options_tickers(coin)
    pick = pick_atm_option(chain, spot, side, config.TARGET_EXPIRY_H,
                           config.MIN_EXPIRY_H, now_ms)
    if pick is None:
        repo.insert_signal_audit(conn, now_ms, coin, side, False,
                                 "no_option_contract", spot, ev)
        return

    if pick["bid"] > 0:
        credit, source = pick["bid"], "bid"
    elif pick["mark_price"] > 0:
        credit, source = pick["mark_price"] * 0.99, "mark_fallback"
    else:
        repo.insert_signal_audit(conn, now_ms, coin, side, False,
                                 "no_quote", spot, ev)
        return

    used_margin = sum(p["margin_usd"] for p in open_pos)
    pnls = json.loads(state["recent_pnls_json"])
    qty, margin = portfolio.size_position(
        state["equity_usd"], used_margin, pnls,
        pick["strike"], credit, config.COIN_SPEC[coin]["lot"])
    if qty <= 0:
        repo.insert_signal_audit(conn, now_ms, coin, side, False,
                                 "margin_blocked", spot, ev)
        return

    ex = exit_params(side)
    fee_open = portfolio.fee_usd(pick["strike"] * qty, credit * qty)
    repo.insert_position(conn, {
        "coin": coin, "side": side, "option_symbol": pick["symbol"],
        "strike": pick["strike"], "expiry_ms": pick["expiry_ms"], "qty": qty,
        "opened_at_ms": now_ms, "underlying_at_open": spot,
        "entry_credit": credit, "entry_source": source,
        "margin_usd": margin, "fee_open_usd": fee_open,
        "tp2_pct": ex["tp2_pct"], "sl_pct": ex["sl_pct"], "hold_h": ex["hold_h"],
        "signal_payload": json.dumps(ev),
    })
    last_fired[key] = now_ms
    repo.update_state(conn, last_fired_json=json.dumps(last_fired))
    repo.insert_signal_audit(conn, now_ms, coin, side, True, None, spot, ev)
    notify(f"OPEN {coin} {side} {pick['symbol']} qty {qty:g} "
           f"credit ${credit:.2f}/ct (src {source}) margin ${margin:.2f} | "
           f"TP2 {ex['tp2_pct']:.0%} SL {ex['sl_pct']:.0%} hold {ex['hold_h']}h")


def main() -> None:
    conn = repo.connect()
    repo.apply_schema(conn)
    now_ms = int(time.time() * 1000)
    state = repo.init_state(conn, config.START_EQUITY_USD, now_ms)
    print(f"[jony] started, mode={config.TRADING_MODE}, "
          f"equity=${state['equity_usd']:.2f}", flush=True)
    notify(f"started (mode={config.TRADING_MODE}, "
           f"equity ${state['equity_usd']:.2f})")

    # per-coin ephemeral window trackers
    win: dict[str, dict] = {c: {"wid": -1, "fails": 0, "disq": False,
                                "fired": False, "audited": False,
                                "last_min": -1, "ev": None}
                            for c in config.COIN_SPEC}
    last_snapshot_min = -1

    while True:
        try:
            now = time.time()
            now_ms = int(now * 1000)
            epoch_min = int(now // 60)
            second = int(now % 60)
            wid = epoch_min // FIVE_MIN
            min_in_window = epoch_min % FIVE_MIN

            if repo.is_paused(conn):
                time.sleep(config.LOOP_SLEEP_S)
                continue

            state = repo.get_state(conn)

            for coin, w in win.items():
                if w["wid"] != wid:
                    w.update(wid=wid, fails=0, disq=False, fired=False,
                             audited=False, last_min=-1, ev=None)

                # per-minute gate check (once per distinct minute)
                if w["last_min"] != epoch_min:
                    w["last_min"] = epoch_min
                    if coin == list(config.COIN_SPEC)[0]:
                        state = manage_exits(conn, state, now_ms)
                    k5, k15, k1h = fetch_klines(coin)
                    ev = evaluate_conditions(coin, k5, k15, k1h)
                    w["ev"] = ev
                    w["fails"], w["disq"] = window_fail_step(
                        w["fails"], bool(ev["ready"]), config.FLICKER_TOLERANCE)
                    print(f"[jony] {coin} w{wid} m{min_in_window}: "
                          f"ready={ev['ready']} side={ev['active_side']} "
                          f"regime={ev['regime']} vol={ev['vol_pctile']} "
                          f"fails={w['fails']} disq={w['disq']}", flush=True)
                    if w["disq"] and not w["audited"]:
                        w["audited"] = True
                        repo.insert_signal_audit(
                            conn, now_ms, coin, ev.get("active_side"), None,
                            "disqualified", ev.get("spot"), ev)

                # fire at the window's last minute, near candle close
                if (min_in_window == FIVE_MIN - 1
                        and second >= config.ENTRY_FIRE_SECOND
                        and not w["fired"] and not w["disq"]
                        and w["ev"] is not None):
                    w["fired"] = True
                    try_fire(conn, state, coin, w["ev"], now_ms)
                    state = repo.get_state(conn)

            # equity snapshot cadence
            if (epoch_min % config.EQUITY_SNAPSHOT_EVERY_MIN == 0
                    and epoch_min != last_snapshot_min):
                last_snapshot_min = epoch_min
                open_pos = repo.open_positions(conn)
                unreal = 0.0
                for coin in {p["coin"] for p in open_pos}:
                    marks = bybit_client.get_option_marks(coin)
                    for p in open_pos:
                        if p["coin"] != coin:
                            continue
                        m = marks.get(p["option_symbol"])
                        if m and m.get("mark"):
                            unreal += (p["entry_credit"] - m["mark"]) * p["qty"]
                repo.insert_equity_snapshot(conn, now_ms, state["equity_usd"],
                                            round(unreal, 4), len(open_pos))

            time.sleep(config.LOOP_SLEEP_S)
        except KeyboardInterrupt:
            raise
        except Exception:
            print(f"[jony] loop error:\n{traceback.format_exc()}", flush=True)
            time.sleep(config.LOOP_SLEEP_S)


if __name__ == "__main__":
    main()
