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
from services.config import FIVE_MIN
from services.telegram_notify import notify


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


def acct_breaker(conn, state: dict, now_ms: int) -> str | None:
    # 30d fetch >> 7d sum window so the streak rule still sees the last N
    # trades across long trading gaps (pause / CB stretches)
    return portfolio.account_breaker(
        repo.closed_pnls(conn, now_ms - 30 * 24 * 3_600_000),
        state.get("equity_usd") or 0.0, now_ms)


def manage_exits(conn, state: dict, now_ms: int,
                 stuck_alerted: set[int] | None = None) -> dict:
    """TP2 / SL / time-stop / expiry settlement for every open position.
    Decision on mark price (Sniper1 convention), fill at ask (fallback mark+1%).

    stuck_alerted: position ids already alerted for failing to settle past
    expiry (Bybit outage) — process-lifetime set, passed in by main() so a
    stuck position pages once, not every minute it stays stuck; retry
    continues unconditionally either way. None (e.g. in tests) = alerting
    disabled, matching the prior unbounded-silent-retry behavior."""
    open_pos = repo.open_positions(conn)
    if not open_pos:
        if stuck_alerted:
            stuck_alerted.clear()
        return state
    posture = portfolio.effective_posture(*repo.get_risk_posture(conn), now_ms)
    if posture == "normal" and acct_breaker(conn, state, now_ms):
        posture = "tight"                       # breaker floors exits, no force-close
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
                    overdue_min = (now_ms - p["expiry_ms"]) / 60_000
                    if (stuck_alerted is not None
                            and overdue_min >= config.STUCK_SETTLEMENT_ALERT_MIN
                            and p["id"] not in stuck_alerted):
                        stuck_alerted.add(p["id"])
                        notify(f"STUCK {p['coin']} {p['side']} {p['option_symbol']} — "
                              f"{overdue_min:.0f}min past expiry, no quote/spot to "
                              f"settle (Bybit outage?). Retrying every tick.")
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

        # Peak tracking runs in EVERY posture (history must already exist the
        # moment the advisor flips to tight); persisted so a loop restart
        # can't forget a peak. The 0.005 epsilon avoids a DB write per tick.
        peak = max(p["peak_profit_pct"] or 0.0, pnl_pct_mark)
        if peak > (p["peak_profit_pct"] or 0.0) + 0.005:
            repo.update_peak_profit(conn, p["id"], peak)

        reason = None
        status = None
        if pnl_pct_mark >= p["tp2_pct"]:
            reason, status = "tp2", "closed_tp2"
        elif pnl_pct_mark <= -p["sl_pct"]:
            reason, status = "sl", "closed_sl"
        elif held_h >= p["hold_h"]:
            reason, status = "time_stop", "closed_time"
        elif posture == "lockdown" and pnl_pct_mark > 0:
            # lockdown: harvest every profitable position immediately
            reason, status = "lockdown_profit_lock", "closed_trail"
        elif posture in ("tight", "lockdown") and \
                portfolio.trail_exit_due(peak, pnl_pct_mark):
            reason, status = "trail_lock", "closed_trail"
        if reason:
            # posture exits lock in profit — they are protective harvests,
            # not strategy losses, so they never arm the circuit breaker
            arm = status != "closed_trail"
            _close(conn, state, p, now_ms, close_fill_price(m), reason, status,
                   arm_cb=arm)
            state = repo.get_state(conn)

    # Prune any id that resolved via a path other than expiry-settle (normal
    # TP2/SL/time_stop above, or a manual close-all between ticks — that one
    # doesn't route through this function at all) — checked generically
    # against what's still open, rather than a discard() at every closing
    # branch, so no resolution path can leave a stale id alerted forever.
    if stuck_alerted:
        stuck_alerted &= {p["id"] for p in repo.open_positions(conn)}
    return state


def close_all_now(conn, state: dict, now_ms: int) -> dict:
    """Manual close-all (Mission Control button): buy back every open position
    at the live ask/mark. Does NOT arm the circuit breaker — a manual stop is
    an operator decision, not a strategy loss streak."""
    open_pos = repo.open_positions(conn)
    if not open_pos:
        return state
    marks_by_coin = {c: bybit_client.get_option_marks(c)
                     for c in {p["coin"] for p in open_pos}}
    for p in open_pos:
        m = marks_by_coin.get(p["coin"], {}).get(p["option_symbol"])
        if m is None or not (m.get("mark") or m.get("ask")):
            print(f"[jony] close_all: no quote for {p['option_symbol']}, "
                  f"skipped (retry next tick)", flush=True)
            continue
        _close(conn, state, p, now_ms, close_fill_price(m),
               "manual_close_all", "closed_manual", arm_cb=False)
        state = repo.get_state(conn)
    return state


def close_position_now(conn, state: dict, pos_id: int, now_ms: int) -> dict:
    """Manual single-position close (Mission Control partial-close button):
    buy back exactly one open position at the live ask/mark. Same pricing/
    accounting path as close_all_now, does NOT arm the circuit breaker (a
    manual close is an operator decision, not a strategy loss streak), and
    does NOT pause the bot -- entries on other coins/sides continue.

    The position may already be closed by the time the loop picks up the
    request (a TP2/SL/expiry could have resolved it in the same tick window
    the API queued this in) -- that's a no-op, not an error, since the
    request has already been consumed by pop_close_requests()."""
    p = repo.get_open_position(conn, pos_id)
    if p is None:
        return state
    m = bybit_client.get_option_marks(p["coin"]).get(p["option_symbol"])
    if m is None or not (m.get("mark") or m.get("ask")):
        print(f"[jony] close_position {pos_id}: no quote for {p['option_symbol']}, "
              f"skipped (will retry on next manual request)", flush=True)
        return state
    _close(conn, state, p, now_ms, close_fill_price(m),
           "manual_close_one", "closed_manual", arm_cb=False)
    return repo.get_state(conn)


def _close(conn, state: dict, p: dict, now_ms: int, exit_debit: float,
           reason: str, status: str, arm_cb: bool = True) -> None:
    entry = p["entry_credit"]
    qty = p["qty"]
    pnl_pct = (entry - exit_debit) / entry if entry > 0 else 0.0
    fee_close = portfolio.fee_usd(p["strike"] * qty, exit_debit * qty)
    pnl_usd = (entry - exit_debit) * qty - p["fee_open_usd"] - fee_close
    # Both writes commit together (commit=False here, one conn.commit() at
    # the end) — closing a position and NOT absorbing its pnl into
    # equity/recent_pnls/CB state must never happen as two separate
    # commits: a crash between them would leave the position permanently
    # closed_* while equity/CB/dyn-size never see the pnl (manage_exits only
    # revisits status='open' rows, so this wouldn't self-heal). The whole
    # block is also wrapped in try/rollback: an in-process exception here
    # (e.g. malformed JSON in state) must not leave a committed-later,
    # semantically-stale close_position write pending on `conn` for some
    # unrelated future commit() to silently flush — main()'s outer
    # try/except logs and continues on the SAME connection, so without an
    # explicit rollback the pending write would outlive the exception that
    # should have discarded it.
    try:
        repo.close_position(conn, p["id"], status=status, closed_at_ms=now_ms,
                            exit_debit=exit_debit, exit_reason=reason,
                            pnl_pct=round(pnl_pct * 100, 2),
                            pnl_usd=round(pnl_usd, 4), commit=False)

        equity = state["equity_usd"] + pnl_usd
        pnls = json.loads(state["recent_pnls_json"])
        pnls = (pnls + [pnl_pct])[-50:]
        # CB is per (coin,side) — a losing streak on one leg must not pause
        # entries on the others (backtest 2026-08-01: isolating this vs. the
        # old single global cb_cooldown_until_ms raised trades/day ~25% and
        # improved holdout return/maxDD together, not a tradeoff).
        cb_key = f"{p['coin']}:{p['side']}"
        cb_by_key = json.loads(state["cb_until_json"])
        if pnl_pct <= 0 and arm_cb:
            cb_by_key[cb_key] = now_ms + config.CB_PAUSE_HOURS * 3_600_000
        repo.update_state(conn, equity_usd=equity,
                          recent_pnls_json=json.dumps(pnls),
                          cb_until_json=json.dumps(cb_by_key), commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    notify(f"CLOSE {p['coin']} {p['side']} {p['option_symbol']} {reason} "
           f"pnl ${pnl_usd:+.2f} ({pnl_pct*100:+.1f}% of premium) | "
           f"equity ${equity:.2f}"
           + (f" | CB until +{config.CB_PAUSE_HOURS}h"
              if pnl_pct <= 0 and arm_cb else ""))


def try_fire(conn, state: dict, coin: str, ev: dict, now_ms: int) -> None:
    side = ev.get("active_side")
    spot = ev.get("spot") or 0.0
    if side is None or side not in COIN_SIDES[coin] or spot <= 0:
        repo.insert_signal_audit(conn, now_ms, coin, side, False, "no_signal",
                                 spot, ev)
        return

    # CB is checked BEFORE cooldown is consumed — while CB is active for this
    # key, last_fired[key] is never advanced (the backtest's replay_account
    # applies CB as a separate filter AFTER cooldown has already advanced
    # unconditionally at event-generation time, a structurally different
    # order). This is safe ONLY because CB_PAUSE_HOURS is always far longer
    # than one cooldown window (guarded by
    # test_portfolio.py::test_cb_pause_dominates_cooldown) — by the time CB
    # clears, cooldown against the stale last_fired[key] is trivially
    # satisfied, so it can never be the actual blocking constraint. If
    # CB_PAUSE_HOURS is ever shortened toward COOLDOWN_BARS*5min, this
    # ordering would need to change too (e.g. advance cooldown on every
    # ready signal regardless of CB, matching the backtest).
    cb_key = f"{coin}:{side}"
    cb_by_key = json.loads(state["cb_until_json"])
    if portfolio.cb_active(cb_by_key.get(cb_key, 0), now_ms):
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
    # Consume the cooldown NOW, before CB/slot/margin checks — the backtest's
    # events_for_variant advances the cooldown for every debounced fire, so a
    # blocked signal must not let the next window re-fire 5 minutes later.
    last_fired[key] = now_ms
    repo.update_state(conn, last_fired_json=json.dumps(last_fired))

    if portfolio.effective_posture(*repo.get_risk_posture(conn),
                                   now_ms) == "lockdown":
        repo.insert_signal_audit(conn, now_ms, coin, side, False, "lockdown",
                                 spot, ev)
        return

    acct_cb = acct_breaker(conn, state, now_ms)
    if acct_cb:
        repo.insert_signal_audit(conn, now_ms, coin, side, False, acct_cb,
                                 spot, ev)
        return

    open_pos = repo.open_positions(conn)
    block = portfolio.can_open(open_pos, coin, side)
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
    repo.insert_signal_audit(conn, now_ms, coin, side, True, None, spot, ev)
    notify(f"OPEN {coin} {side} {pick['symbol']} qty {qty:g} "
           f"credit ${credit:.2f}/ct (src {source}) margin ${margin:.2f} | "
           f"TP2 {ex['tp2_pct']:.0%} SL {ex['sl_pct']:.0%} hold {ex['hold_h']}h")


def process_entry_requests(conn, state: dict, now_ms: int) -> dict:
    """Advisor-proposed entries. Stricter than mechanical signals: they run
    ONLY in posture 'normal' (an elevated-risk regime must not add exposure),
    and only while not paused (checked by the caller's placement). Everything
    else — CB, cooldown, caps incl. per-key, margin sizing, contract pick —
    is re-checked by routing through the SAME try_fire path; the advisor
    cannot bypass a single risk limit. source=advisor rides in the signal
    payload for per-source scoring."""
    for req in repo.pop_entry_requests(conn):
        coin, side = req["coin"], req["side"]
        if coin not in config.COIN_SPEC or side not in COIN_SIDES.get(coin, ()):
            continue
        if portfolio.effective_posture(*repo.get_risk_posture(conn),
                                       now_ms) != "normal":
            repo.insert_signal_audit(conn, now_ms, coin, side, False,
                                     "advisor_entry_posture", None,
                                     {"source": "advisor"})
            continue
        k5 = bybit_client.get_klines(config.COIN_SPEC[coin]["symbol"], "5", 1)
        if not k5:
            repo.insert_signal_audit(conn, now_ms, coin, side, False,
                                     "advisor_entry_no_spot", None,
                                     {"source": "advisor"})
            continue
        try:
            rationale = json.loads(req["payload"] or "{}")
        except ValueError:
            rationale = {}
        ev = {"active_side": side, "spot": k5[-1]["close"],
              "source": "advisor", **rationale}
        notify(f"ADVISOR ENTRY {coin} {side} — проверяю лимиты и открываю")
        try_fire(conn, state, coin, ev, now_ms)
        state = repo.get_state(conn)
    return state


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
    last_exit_min = -1
    stuck_alerted: set[int] = set()

    while True:
        try:
            now = time.time()
            now_ms = int(now * 1000)
            epoch_min = int(now // 60)
            second = int(now % 60)
            wid = epoch_min // FIVE_MIN
            min_in_window = epoch_min % FIVE_MIN

            state = repo.get_state(conn)

            # Exits run every minute UNCONDITIONALLY — pausing the bot stops
            # new entries, never risk management of what is already open.
            if last_exit_min != epoch_min:
                last_exit_min = epoch_min
                state = manage_exits(conn, state, now_ms, stuck_alerted)

            # Mission Control "close all": API sets the flag, loop executes
            # (position writes stay with the single writer). Runs even when
            # paused — request_close_all pauses the bot as its first step.
            if repo.pop_close_all(conn):
                notify("CLOSE ALL requested — buying back all open positions")
                state = close_all_now(conn, state, now_ms)

            # Mission Control partial close: one or more single-position
            # requests queued via POST /close_position/{id}. Runs even when
            # paused, same reasoning as close-all above — but unlike
            # close-all, this does NOT pause the bot itself.
            for pos_id in repo.pop_close_requests(conn):
                notify(f"CLOSE position {pos_id} requested (manual)")
                state = close_position_now(conn, state, pos_id, now_ms)

            if repo.is_paused(conn):
                time.sleep(config.LOOP_SLEEP_S)
                continue

            # Advisor entry proposals — after the pause check on purpose:
            # a paused bot takes no new positions from anyone.
            state = process_entry_requests(conn, state, now_ms)

            for coin, w in win.items():
                if w["wid"] != wid:
                    w.update(wid=wid, fails=0, disq=False, fired=False,
                             audited=False, last_min=-1, ev=None)

                # per-minute gate check (once per distinct minute)
                if w["last_min"] != epoch_min:
                    w["last_min"] = epoch_min
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
                    # For the dashboard entry-proximity gauge (core/proximity.py)
                    # — the API process can't see this in-memory `win` dict.
                    repo.upsert_window_status(
                        conn, coin, wid=wid, min_in_window=min_in_window,
                        disqualified=w["disq"], ev=ev, checked_at_ms=now_ms)

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
