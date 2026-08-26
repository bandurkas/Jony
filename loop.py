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
import sys
import time
import traceback

from core import close_policy
from core.strategy import (
    COIN_SIDES, evaluate_conditions, exit_params, window_fail_step,
)
from db import repo
from services import config, execution, portfolio
from services.bybit_client import bybit_client, pick_atm_option
from services.config import FIVE_MIN
from services.telegram_notify import notify


# Executor is module-level so tests keep the synchronous paper path by default;
# main() swaps in LiveExecutor for JONY_TRADING_MODE=live (Track B 2026-08-26).
executor = execution.PaperExecutor()


def dist_7d_high_pct(k1h: list, spot: float) -> float | None:
    """Spot distance from the 7d (168×1h) high in %, negative below it."""
    bars = k1h[-168:] if k1h else []
    if len(bars) < 24 or not spot:
        return None
    hi = max(b["high"] for b in bars)
    return (spot / hi - 1) * 100 if hi else None


def fetch_klines(coin: str) -> tuple[list, list, list]:
    sym = config.COIN_SPEC[coin]["symbol"]
    k5 = bybit_client.get_klines(sym, "5", config.KLINE_LIMIT_5M)
    k15 = bybit_client.get_klines(sym, "15", config.KLINE_LIMIT_15M)
    k1h = bybit_client.get_klines(sym, "60", config.KLINE_LIMIT_1H)
    return k5, k15, k1h


def is_advisor_position(p: dict) -> bool:
    """Позиция открыта по заявке советника (source=advisor в signal_payload)."""
    try:
        d = json.loads(p.get("signal_payload") or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(d, dict) and d.get("source") == "advisor"


def spread_abnormal(m: dict) -> bool:
    """Ask оторван от mark сильнее EXIT_SPREAD_GUARD — пустой стакан/спайк."""
    mark, ask = m.get("mark") or 0.0, m.get("ask") or 0.0
    return mark > 0 and ask > mark * (1 + config.EXIT_SPREAD_GUARD)


def close_fill_price(m: dict, cap_slip: bool = False) -> float:
    """Paper buy-to-close: pay the ask when quoted, else mark +1%.
    cap_slip=True — лимитка у mark*(1+EXIT_MAX_SLIP): для профит-выходов,
    которые уже отложены EXIT_DEFER_MAX_MIN и стакан так и не пришёл в норму."""
    mark = m.get("mark") or 0.0
    if m.get("ask"):
        if cap_slip and mark > 0:
            return min(m["ask"], mark * (1 + config.EXIT_MAX_SLIP))
        return m["ask"]
    return mark * 1.01


def acct_breaker(conn, state: dict, now_ms: int) -> str | None:
    # 30d fetch >> 7d sum window so the streak rule still sees the last N
    # trades across long trading gaps (pause / CB stretches)
    return portfolio.account_breaker(
        repo.closed_pnls(conn, now_ms - 30 * 24 * 3_600_000),
        state.get("equity_usd") or 0.0, now_ms)


_mark_logged_min: dict[int, int] = {}  # pos_id -> последняя минута записи марки (P1)
_exit_deferred_since: dict[int, int] = {}  # pos_id -> ms первого отложенного выхода


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
        _exit_deferred_since.clear()
        return state
    posture = portfolio.effective_posture(*repo.get_risk_posture(conn), now_ms)
    if posture == "normal" and acct_breaker(conn, state, now_ms):
        posture = "tight"                       # breaker floors exits, no force-close
    marks_by_coin: dict[str, dict] = {}
    for coin in {p["coin"] for p in open_pos}:
        marks_by_coin[coin] = bybit_client.get_option_marks(coin)

    for p in open_pos:
        if p.get("closing_order_id"):
            oc = repo.get_order(conn, p["closing_order_id"])
            if oc and oc["status"] == "active":
                continue                    # close order in flight
            repo.set_closing(conn, p["id"], None)   # stale marker: self-heal (r2 F1)
            p["closing_order_id"] = None
        m = marks_by_coin.get(p["coin"], {}).get(p["option_symbol"])
        entry = p["entry_credit"]

        if executor.live and now_ms >= p["expiry_ms"]:
            # Bybit settles expired options itself; we only book the result
            # once the position has left the exchange book.
            if _settle_expired_live(conn, state, p, now_ms, stuck_alerted):
                state = repo.get_state(conn)
            continue

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

        # P1 2026-08-17: реальная история марок, 1 строка/мин/позицию —
        # сырьё для честной калибровки сигмы и ре-тюна выходов
        minute = now_ms // 60_000
        if _mark_logged_min.get(p["id"]) != minute:
            # INSERT OR IGNORE + UNIQUE(pos_id, minute) в БД — рестарты loop
            # не дублируют строки калибровочной выборки (ревью 2026-08-17);
            # флаг ставится ПОСЛЕ записи, чтобы сбой вставки не терял минуту
            repo.insert_position_mark(conn, now_ms, p["id"],
                                      p["option_symbol"], m, pnl_pct_mark)
            _mark_logged_min[p["id"]] = minute

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
        elif posture == "lockdown" and \
                close_policy.endgame_ok(pnl_pct_mark, held_h, p["hold_h"]):
            # lockdown снимает только ЗРЕЛЫЙ профит (политика закрытий,
            # core/close_policy.py — ревью 2026-08-17: иначе советник,
            # которому вето не даёт CLOSE, харвестил бы молодые позиции
            # через lockdown). Молодой профит под lockdown НЕ трогаем
            # (с 2026-08-27 trail-ветка ниже под той же close_policy, т.е.
            # в lockdown недостижима); полная эвакуация — Close All у человека.
            reason, status = "lockdown_profit_lock", "closed_trail"
        elif (posture in ("tight", "lockdown") or is_advisor_position(p)) and \
                portfolio.trail_exit_due(peak, pnl_pct_mark) and \
                (not config.TRAIL_REQUIRE_ENDGAME or
                 close_policy.endgame_ok(pnl_pct_mark, held_h, p["hold_h"])):
            # 2026-08-27: trail только на зрелом профите (close_policy) —
            # единое правило досрочных закрытий для всех каналов
            # advisor-входы (в т.ч. advisor-only коллы) — под трейлингом
            # ВСЕГДА: экспериментальный источник сигнала, профит защищаем
            # механически (решение пользователя 2026-08-22)
            reason, status = "trail_lock", "closed_trail"
        if reason:
            # пустой стакан (bid 30/ask 1045 при mark 130, поз.65 2026-08-19):
            # любой выход платит не больше mark*(1+EXIT_MAX_SLIP); tp2/time_stop
            # до экспирации ещё и ждут нормализации книги (не вечно). trail/
            # lockdown/SL — защитные, ждать им нельзя: фил сразу, капированный.
            # Таймер in-memory: рестарт loop даёт ещё ≤EXIT_DEFER_MAX_MIN —
            # осознанно, персистить ради 10 минут не стоит.
            cap_slip = spread_abnormal(m)
            if cap_slip and reason in ("tp2", "time_stop") \
                    and now_ms < p["expiry_ms"]:
                since = _exit_deferred_since.setdefault(p["id"], now_ms)
                if now_ms - since < config.EXIT_DEFER_MAX_MIN * 60_000:
                    if since == now_ms:
                        print(f"[jony] exit {reason} pos {p['id']} deferred: "
                              f"ask {m['ask']} vs mark {mark:.2f}", flush=True)
                    continue
            # posture exits lock in profit — they are protective harvests,
            # not strategy losses, so they never arm the circuit breaker
            arm = status != "closed_trail"
            urgent = reason in ("sl", "trail_lock", "lockdown_profit_lock")
            _request_close(conn, state, p, now_ms, close_fill_price(m, cap_slip),
                           reason, status, arm, urgent, m)
            _exit_deferred_since.pop(p["id"], None)
            state = repo.get_state(conn)
        else:
            _exit_deferred_since.pop(p["id"], None)

    # Prune any id that resolved via a path other than expiry-settle (normal
    # TP2/SL/time_stop above, or a manual close-all between ticks — that one
    # doesn't route through this function at all) — checked generically
    # against what's still open, rather than a discard() at every closing
    # branch, so no resolution path can leave a stale id alerted forever.
    if stuck_alerted:
        stuck_alerted &= {p["id"] for p in repo.open_positions(conn)}
    still_open = {p["id"] for p in open_pos}
    for pid in list(_mark_logged_min):
        if pid not in still_open:
            _mark_logged_min.pop(pid, None)
            _exit_deferred_since.pop(pid, None)
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
        if p.get("closing_order_id"):
            continue
        _request_close(conn, state, p, now_ms, close_fill_price(m, cap_slip=True),
                       "manual_close_all", "closed_manual", False, True, m)
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
    if p.get("closing_order_id"):
        oc = repo.get_order(conn, p["closing_order_id"])
        if oc and oc["status"] == "active":
            notify(f"CLOSE position {pos_id}: already closing (order {oc['id']}, "
                   f"stage {oc['stage']})")
            return state
        repo.set_closing(conn, p["id"], None)
    _request_close(conn, state, p, now_ms, close_fill_price(m, cap_slip=True),
                   "manual_close_one", "closed_manual", False, True, m)
    return repo.get_state(conn)


def _close(conn, state: dict, p: dict, now_ms: int, exit_debit: float,
           reason: str, status: str, arm_cb: bool = True,
           fee_close: float | None = None, commit: bool = True,
           notes: list | None = None, order_id: int | None = None) -> None:
    entry = p["entry_credit"]
    qty = p["qty"]
    pnl_pct = (entry - exit_debit) / entry if entry > 0 else 0.0
    if fee_close is None:                   # paper / no exchange fee available
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
                            pnl_usd=round(pnl_usd, 4), commit=False,
                            closed_by_order_id=order_id)

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
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    msg = (f"CLOSE {p['coin']} {p['side']} {p['option_symbol']} {reason} "
           f"pnl ${pnl_usd:+.2f} ({pnl_pct*100:+.1f}% of premium) | "
           f"equity ${equity:.2f}"
           + (f" | CB until +{config.CB_PAUSE_HOURS}h"
              if pnl_pct <= 0 and arm_cb else ""))
    if notes is not None:
        notes.append(msg)                   # sent after the caller's commit
    else:
        notify(msg)


# ── Execution glue (Track B 2026-08-26) ──────────────────────────────────────

def _quote_px(m: dict | None, kind: str, urgent: bool, fallback: float) -> float:
    """Live limit price: mid for patient orders, ask for urgent ones — every
    close capped by the spread guard mark×(1+EXIT_MAX_SLIP) (pos #65 rule,
    review 2026-08-26 #1); opens floored at mark×(1−EXIT_MAX_SLIP)."""
    if not executor.live or not m:
        return fallback
    bid, ask, mark = m.get("bid") or 0.0, m.get("ask") or 0.0, m.get("mark") or 0.0
    if kind == "open":
        px = (bid + ask) / 2 if bid and ask else (bid or fallback)
        fl, cap = execution.open_floor(m), execution.close_cap(m)
        return min(max(px, fl), cap) if fl and cap else px
    cap = execution.close_cap(m)
    if urgent:
        px = ask or (mark * 1.01 if mark else fallback)
    else:
        px = (bid + ask) / 2 if bid and ask else (ask or fallback)
    return min(px, cap) if cap else px


_exit_blocked_notified: dict[int, int] = {}   # pos_id -> last alert ms


def _request_close(conn, state: dict, p: dict, now_ms: int, paper_price: float,
                   reason: str, status: str, arm_cb: bool, urgent: bool,
                   m: dict | None) -> None:
    """Route a close through the order state machine. Paper fills on this
    tick (same prices as before); live places mid/ask and finalizes later.
    A second attempt after a no-fill is always urgent; manual closes always
    go through (operator override, r2 F4)."""
    manual = reason.startswith("manual")
    attempts = p.get("close_attempts") or 0
    if attempts >= config.CLOSE_MAX_ATTEMPTS and not manual:
        # keeps failing to fill/being rejected (already flat on exchange?) —
        # stop hammering, page every 10 min, leave it to the operator
        execution._halt(conn, now_ms, f"pos {p['id']} {p['option_symbol']}: "
                                      f"{attempts} close attempts failed", notify)
        if now_ms - _exit_blocked_notified.get(p["id"], -10**12) >= 10 * 60_000:
            _exit_blocked_notified[p["id"]] = now_ms
            notify(f"EXIT BLOCKED pos {p['id']} {p['option_symbol']} ({reason}) after "
                   f"{attempts} attempts — close manually (POST /close_position/{p['id']})")
        return
    if attempts >= 1 or manual:
        urgent = True
    if executor.live and (urgent or manual):
        # never send a reduce-only buy against a position the exchange no
        # longer has (settled/liquidated/closed in UI) — r2 F10
        pos = executor.positions(p["coin"])
        size = abs((pos.get(p["option_symbol"]) or {}).get("size") or 0) if pos is not None else None
        if size is not None and size + 1e-9 < p["qty"]:
            if manual and size < 1e-9:
                # operator closes a row the exchange is already flat on:
                # book it at the current mark, no order (r3 int #1)
                mark = (m or {}).get("mark") or paper_price
                if now_ms >= p["expiry_ms"]:      # exchange settled at intrinsic
                    und = (m or {}).get("underlying") or p["underlying_at_open"]
                    mark = max(0.0, (p["strike"] - und) if p["side"] == "P" else (und - p["strike"]))
                _close(conn, state, p, now_ms, mark, "manual_book_flat", status,
                       arm_cb=False, fee_close=0.0)
                notify(f"CLOSE pos {p['id']} {p['option_symbol']} booked flat @ {mark:g} "
                       f"(exchange had no position, no order sent)")
                return
            repo.set_closing(conn, p["id"], None, bump_attempts=not manual)
            execution._halt(conn, now_ms, f"pos {p['id']} {p['option_symbol']}: exchange "
                                          f"size {size:g} < db qty {p['qty']:g} — closed "
                                          f"outside the bot?", notify)
            return
    px = _quote_px(m, "close", urgent, paper_price)
    oid = execution.submit(
        conn, executor, kind="close", coin=p["coin"], side=p["side"],
        symbol=p["option_symbol"], qty=p["qty"], price=px, urgent=urgent,
        reason=reason, payload={"reason": reason, "status": status, "arm_cb": arm_cb},
        now_ms=now_ms, pos_id=p["id"],
        ceiling=execution.urgent_ceiling(m) if executor.live else None)
    o = repo.get_order(conn, oid)
    if o["status"] != "active":
        n = attempts + 1                    # submit bumped close_attempts (r2 F6)
        if n in (1, config.CLOSE_MAX_ATTEMPTS):
            notify(f"ORDER close {p['option_symbol']} placement REJECTED ({reason}), "
                   f"attempt {n}/{config.CLOSE_MAX_ATTEMPTS}")
        return
    if executor.live:
        notify(f"ORDER close {p['option_symbol']} {reason} qty {p['qty']:g} @ {px:g}"
               f"{' URGENT' if urgent else ' mid'}")
    _advance_safely(conn, o, now_ms, m)


def _advance_safely(conn, o: dict, now_ms: int, quote: dict | None) -> None:
    """First advance right after submit. A booking error here must not abort
    the tick: the order stays active and process_orders retries next tick."""
    try:
        execution.advance(conn, executor, o, now_ms, quote, _on_open_fill, _on_close_fill, notify)
    except Exception as e:
        conn.rollback()
        print(f"[jony] order {o['id']} first advance failed: {e!r} — will retry", flush=True)


def _on_close_fill(conn, o: dict, fill: dict, now_ms: int, notes: list) -> bool:
    """Book a close fill. No commit here — execution._finalize commits the
    position, state and order row together and sends `notes` afterwards.
    Returns False when the fill cannot be attributed (position closed by a
    different order) so the caller halts."""
    p = repo.get_open_position(conn, o["pos_id"])
    if p is None:
        row = repo.position_row(conn, o["pos_id"])
        # booked by THIS order in an earlier finalize that crashed before the
        # order row was updated → idempotent success; any other order → halt
        return bool(row and row["status"] != "open"
                    and row.get("closed_by_order_id") == o["id"])
    pl = json.loads(o["payload"] or "{}")
    qty = fill["qty"]
    if qty < p["qty"] - 1e-9:
        # partial close (live): the filled part becomes its own closed row,
        # the remainder shrinks and is released to the next exit tick —
        # all inside this one transaction (review 2 #11)
        frac = qty / p["qty"]
        child = {k: p[k] for k in p if k not in ("id", "closing_order_id", "close_attempts")}
        child.update(qty=qty, fee_open_usd=p["fee_open_usd"] * frac,
                     margin_usd=p["margin_usd"] * frac, status="open")
        cid = repo.insert_position(conn, child, commit=False)
        conn.execute("UPDATE positions SET qty=?, fee_open_usd=?, margin_usd=?,"
                     " closing_order_id=NULL WHERE id=?",
                     (p["qty"] - qty, p["fee_open_usd"] * (1 - frac),
                      p["margin_usd"] * (1 - frac), p["id"]))
        p = dict(child, id=cid)
    _close(conn, repo.get_state(conn), p, now_ms, fill["avg_price"], pl["reason"],
           pl["status"], arm_cb=pl.get("arm_cb", True), fee_close=fill.get("fee_usd"),
           commit=False, notes=notes, order_id=o["id"])
    return True


def _on_open_fill(conn, o: dict, fill: dict, now_ms: int, notes: list) -> int:
    """Book an open fill (no commit — see _on_close_fill). Network calls
    (exchange IM) happen before the first write so the sqlite lock is short."""
    pl = json.loads(o["payload"] or "{}")
    qty, credit = fill["qty"], fill["avg_price"]
    state = repo.get_state(conn)
    fee_open = fill.get("fee_usd")
    if fee_open is None:
        fee_open = portfolio.fee_usd(pl["strike"] * qty, credit * qty)
    margin = pl["margin_est"] * (qty / o["qty"]) if o["qty"] else pl["margin_est"]
    im = executor.position_im(o["option_symbol"], qty) if executor.live else None
    if im:
        margin = im
    sl_eff = portfolio.sl_pct_effective(pl["sl_pct"], state["equity_usd"], credit, qty)
    pid = repo.insert_position(conn, {
        "coin": o["coin"], "side": o["side"], "option_symbol": o["option_symbol"],
        "strike": pl["strike"], "expiry_ms": pl["expiry_ms"], "qty": qty,
        "opened_at_ms": now_ms, "underlying_at_open": pl["spot"],
        "entry_credit": credit, "entry_source": pl["source"] if not executor.live else fill["source"],
        "margin_usd": margin, "fee_open_usd": fee_open,
        "tp2_pct": pl["tp2_pct"], "sl_pct": round(sl_eff, 4), "hold_h": pl["hold_h"],
        "signal_payload": json.dumps(pl.get("ev") or {}),
        "exchange_im_usd": im,
    }, commit=False)
    notes.append(f"OPEN {o['coin']} {o['side']} {o['option_symbol']} qty {qty:g} "
                 f"credit ${credit:.2f}/ct (src {fill['source'] if executor.live else pl['source']}) "
                 f"margin ${margin:.2f}{' (exch IM)' if im else ''} | "
                 f"TP2 {pl['tp2_pct']:.0%} SL {sl_eff:.0%} hold {pl['hold_h']}h")
    return pid


def _settle_expired_live(conn, state: dict, p: dict, now_ms: int,
                         stuck_alerted: set[int] | None) -> bool:
    """Live: once the expired position is gone from the exchange, book the
    settlement (delivery record → intrinsic fallback). Returns True if closed."""
    pos = executor.positions(p["coin"])
    if pos is None:
        return False                        # API failure: retry next tick
    if p["option_symbol"] in pos:
        overdue_min = (now_ms - p["expiry_ms"]) / 60_000
        if (stuck_alerted is not None and overdue_min >= config.STUCK_SETTLEMENT_ALERT_MIN
                and p["id"] not in stuck_alerted):
            stuck_alerted.add(p["id"])
            notify(f"STUCK {p['option_symbol']} — {overdue_min:.0f}min past expiry, "
                   f"still on exchange book (settlement pending)")
        return False
    exit_debit, fee = None, None
    rec = bybit_client.get_delivery(p["option_symbol"])
    if rec:
        try:
            dp = float(rec.get("deliveryPrice") or 0)
            exit_debit = max(0.0, dp - p["strike"]) if p["side"] == "C" else max(0.0, p["strike"] - dp)
            fee = float(rec.get("fee") or 0)              # Bybit: positive = cost; 0 is real (OTM)
        except (TypeError, ValueError):
            exit_debit = None
    if exit_debit is None:
        # delivery record lags settlement by minutes — wait for the real
        # TWAP price before falling back to spot intrinsic (review #8)
        if (now_ms - p["expiry_ms"]) / 60_000 < config.SETTLE_RECORD_WAIT_MIN:
            return False
        k5 = bybit_client.get_klines(config.COIN_SPEC[p["coin"]]["symbol"], "5", 1)
        if not k5:
            return False
        spot = k5[-1]["close"]
        exit_debit = max(0.0, spot - p["strike"]) if p["side"] == "C" else max(0.0, p["strike"] - spot)
    _close(conn, state, p, now_ms, exit_debit, "expiry_settle", "closed_time", fee_close=fee)
    return True


def log_iv(conn, now_ms: int) -> None:
    for coin in config.COIN_SPEC:
        try:
            chain = bybit_client.get_options_tickers(coin)
            if not chain:
                continue
            spot = next((o["underlying_price"] for o in chain if o.get("underlying_price")), None)
            row = {}
            for side in ("P", "C"):
                atm = pick_atm_option(chain, spot or 0, side, config.TARGET_EXPIRY_H,
                                      config.MIN_EXPIRY_H, now_ms) if spot else None
                row[side] = (atm["symbol"], atm["mark_iv"]) if atm else (None, None)
            repo.insert_iv_log(conn, now_ms, coin, spot, *row["P"], *row["C"])
        except Exception as e:
            print(f"[jony] iv_log {coin} failed: {e}", flush=True)


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
    inflight = execution.inflight_opens(conn) if executor.live else []
    block = portfolio.can_open(open_pos + inflight, coin, side)
    if block:
        repo.insert_signal_audit(conn, now_ms, coin, side, False, block, spot, ev)
        return
    if executor.live:
        halted, why = repo.get_exec_halt(conn)
        if halted:
            repo.insert_signal_audit(conn, now_ms, coin, side, False, "exec_halt", spot, ev)
            return
        if any(o["coin"] == coin and o["side"] == side for o in inflight):
            repo.insert_signal_audit(conn, now_ms, coin, side, False, "open_in_flight", spot, ev)
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

    used_margin = sum(p["margin_usd"] for p in open_pos + inflight)
    pnls = json.loads(state["recent_pnls_json"])
    mult = portfolio.near_high_mult(side, ev.get("dist_7d_high_pct"))
    qty, margin = portfolio.size_position(
        state["equity_usd"], used_margin, pnls,
        pick["strike"], credit, config.COIN_SPEC[coin]["lot"], size_mult=mult)
    if qty <= 0:
        repo.insert_signal_audit(conn, now_ms, coin, side, False,
                                 "margin_blocked", spot, ev)
        return

    ex = exit_params(side)
    quote = {"bid": pick["bid"], "ask": pick["ask"], "mark": pick["mark_price"]}
    px = _quote_px(quote, "open", False, credit)
    oid = execution.submit(
        conn, executor, kind="open", coin=coin, side=side, symbol=pick["symbol"],
        qty=qty, price=px, urgent=False, reason=source,
        payload={"strike": pick["strike"], "expiry_ms": pick["expiry_ms"], "spot": spot,
                 "source": source, "margin_est": margin, "tp2_pct": ex["tp2_pct"],
                 "sl_pct": ex["sl_pct"], "hold_h": ex["hold_h"], "ev": ev,
                 "size_mult": mult},
        now_ms=now_ms)
    o = repo.get_order(conn, oid)
    if o["status"] != "active":
        repo.insert_signal_audit(conn, now_ms, coin, side, False, "order_error", spot, ev)
        notify(f"ORDER open {pick['symbol']} placement FAILED")
        return
    repo.insert_signal_audit(conn, now_ms, coin, side, True, None, spot, ev)
    if executor.live:
        notify(f"ORDER open {coin} {side} {pick['symbol']} qty {qty:g} @ {px:g} mid "
               f"(bid {pick['bid']:g}/ask {pick['ask']:g}) mult {mult:g}")
    _advance_safely(conn, o, now_ms, quote)


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
        # TTL (ревью 2026-08-17): заявка без срока могла бы исполниться после
        # рестарта loop на рынке, которого больше нет
        if now_ms - req["requested_at_ms"] > 15 * 60_000:
            repo.insert_signal_audit(conn, now_ms, coin, side, False,
                                     "advisor_entry_expired", None,
                                     {"source": "advisor",
                                      "age_min": round((now_ms - req["requested_at_ms"]) / 60_000)})
            continue
        # tight no longer blocks advisor entries (self-lock fix 2026-08-26:
        # an ITM position on one coin used to freeze the other coin's best
        # key); advisor.decide_entry already requires market_risk != high,
        # and advisor positions trail unconditionally. Lockdown still blocks.
        if portfolio.effective_posture(*repo.get_risk_posture(conn),
                                       now_ms) == "lockdown":
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
        k1h = bybit_client.get_klines(config.COIN_SPEC[coin]["symbol"], "60", 168)
        ev = {"active_side": side, "spot": k5[-1]["close"],
              "dist_7d_high_pct": dist_7d_high_pct(k1h, k5[-1]["close"]),
              "source": "advisor", **rationale}
        notify(f"ADVISOR ENTRY {coin} {side} — проверяю лимиты и открываю")
        try_fire(conn, state, coin, ev, now_ms)
        state = repo.get_state(conn)
    return state


def main() -> None:
    global executor
    conn = repo.connect()
    repo.apply_schema(conn)
    executor = execution.make_executor(bybit_client)
    err = execution.live_preflight(bybit_client)
    if err:
        print(f"[jony] FATAL: {err}", flush=True)
        notify(f"FATAL: {err} — loop not started (retry in 5 min)")
        time.sleep(300)                     # restart:unless-stopped → no TG spam (r3 int #4)
        sys.exit(1)
    n = repo.sweep_unlinked_orders(conn)
    if n:
        notify(f"startup: {n} active order(s) without link id marked error")
    now_ms = int(time.time() * 1000)
    state = repo.init_state(conn, config.START_EQUITY_USD, now_ms)
    print(f"[jony] started, mode={config.TRADING_MODE}, "
          f"equity=${state['equity_usd']:.2f}", flush=True)
    notify(f"started (mode={config.TRADING_MODE}"
           f"{' TESTNET' if config.BYBIT_TESTNET else ''}, "
           f"equity ${state['equity_usd']:.2f})")

    # per-coin ephemeral window trackers
    win: dict[str, dict] = {c: {"wid": -1, "fails": 0, "disq": False,
                                "fired": False, "audited": False,
                                "last_min": -1, "ev": None}
                            for c in config.COIN_SPEC}
    last_snapshot_min = -1
    last_exit_min = -1
    last_reconcile_min = -1
    last_iv_min = -1
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

            # In-flight orders first (live): fills become positions/closes
            # before this tick's exit logic looks at the book.
            execution.process_orders(conn, executor, now_ms,
                                     bybit_client.get_option_marks,
                                     _on_open_fill, _on_close_fill, notify)
            state = repo.get_state(conn)
            if executor.live:
                if (epoch_min % config.RECONCILE_EVERY_MIN == 0
                        and epoch_min != last_reconcile_min):
                    last_reconcile_min = epoch_min
                    execution.reconcile(conn, executor, repo.open_positions(conn),
                                        now_ms, notify)

            if (epoch_min % config.IV_LOG_EVERY_MIN == 0
                    and epoch_min != last_iv_min):
                last_iv_min = epoch_min
                log_iv(conn, now_ms)

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
                    ev["dist_7d_high_pct"] = dist_7d_high_pct(k1h, ev.get("spot") or 0.0)
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
