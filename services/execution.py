"""Execution layer (Track B 2026-08-26, EXECUTION_DESIGN_2026-08-26.md).

One state machine for both modes: an open/close *order* row is created, then
`advance()` moves it mid → retreat → cancel (or urgent chase) on every loop
tick and finalizes into `positions` via callbacks the loop provides.
PaperExecutor fills instantly at the price the loop computed (the pre-Track-B
paper rules, byte-for-byte), so paper keeps its old behaviour while sharing
the code path that live uses. process_orders runs in BOTH modes, so a paper
order that failed to finalize (exception) is retried next tick (review r2 F1).

Live rules (user decisions 2026-08-26): limit at mid, EXEC_MID_WAIT_S, then
one retreat to bid/ask for EXEC_RETREAT_WAIT_S, then cancel; whatever filled
stays (lots 0.01/0.1). Protective closes (sl/trail/lockdown/manual) start at
min(ask, mark×(1+EXIT_MAX_SLIP)) and step +EXEC_URGENT_CHASE_PCT every
EXEC_URGENT_WAIT_S, never above the ask and never above the CEILING
mark_fresh×(1+EXEC_URGENT_MAX_SLIP) — the spread guard that saved pos #65 in
paper applies to live too (review r1 #1, r2 F2).

Invariants (review r1 #2-#6, r2 F3-F6):
- An order row is only ever marked `error` when the exchange DEFINITIVELY does
  not know it (both realtime and history answered "absent"). API failures,
  fills without a price yet, cancel failures and callback exceptions keep the
  order active and the position reserved; they page every EXEC_ALERT_EVERY
  ticks. A parked, paged order beats a duplicate one.
- A placement whose outcome is unknown (transport failure) stays active with
  order_id NULL and is adopted by orderLinkId on the next ticks.
- The close order is recorded on the position (closing_order_id) BEFORE the
  order is sent, so a crash can never produce a second close.
- Finalize runs the booking callback first and commits it together with the
  order row; notifications go out only after the commit.
"""
from __future__ import annotations

import json

from db import repo
from services import config
from services.bybit_client import round_to_tick


# ── Executors ────────────────────────────────────────────────────────────────

class PaperExecutor:
    """Fills every order immediately at o['price'] (loop-computed paper fill)."""
    live = False

    def place(self, o: dict) -> tuple[str, str | None]:
        return "ok", f"paper-{o['id']}"

    def amend(self, o: dict, price: float) -> float | None:
        return price

    def cancel(self, o: dict) -> bool:
        return True

    def poll(self, o: dict) -> dict | None:
        return {"status": "Filled", "filled_qty": o["qty"], "avg_price": o["price"],
                "fee_usd": None, "order_id": o.get("order_id")}

    def positions(self, coin: str) -> dict | None:
        return None

    def open_orders(self) -> list[dict] | None:
        return []

    def position_im(self, symbol: str, qty: float) -> float | None:
        return None


class LiveExecutor:
    live = True

    def __init__(self, client):
        self.c = client

    def _side(self, o: dict) -> str:
        return "Sell" if o["kind"] == "open" else "Buy"

    def _px(self, o: dict, price: float, up: bool | None = None) -> float | None:
        tick = self.c.tick_size(o["option_symbol"])
        if tick <= 0:
            return None                     # unknown tick → don't guess a price
        if up is None:
            up = o["kind"] == "open"
        return round_to_tick(price, tick, up=up)

    def place(self, o: dict) -> tuple[str, str | None]:
        px = self._px(o, o["price"])
        if px is None:
            return "unknown", None          # retry placement next tick
        return self.c.place_order(o["option_symbol"], self._side(o), o["qty"], px,
                                  o["order_link_id"], reduce_only=(o["kind"] == "close"))

    def amend(self, o: dict, price: float) -> float | None:
        """Returns the tick-rounded price actually sent, None on failure."""
        # chase steps round AWAY from the current price so a coarse tick can't
        # stall the chase (review r2 F9); a buy retreat still rounds down
        up = (o["kind"] == "close" and o["stage"] == "urgent") or o["kind"] == "open"
        px = self._px(o, price, up=up)
        if px is None or abs(px - o["price"]) < 1e-12:
            return None
        return px if self.c.amend_order(o["option_symbol"], o["order_id"], px) else None

    def cancel(self, o: dict) -> bool:
        return self.c.cancel_order(o["option_symbol"], o.get("order_id"),
                                   link_id=o.get("order_link_id"))

    def poll(self, o: dict) -> dict | None:
        raw = self.c.get_order(o["option_symbol"], o["order_link_id"])
        if raw is None:
            return None
        if not raw:
            return {"status": "Absent", "filled_qty": 0.0, "avg_price": None,
                    "fee_usd": None, "order_id": None}
        st = raw.get("orderStatus") or ""
        filled = float(raw.get("cumExecQty") or 0)
        avg = float(raw.get("avgPrice") or 0) or None
        fee_raw = raw.get("cumExecFee")
        fee = float(fee_raw) if fee_raw not in (None, "") else None
        if filled > 0 and (avg is None or fee is None):
            ex = self.c.get_executions(o["option_symbol"], raw.get("orderId") or o["order_id"] or "")
            if ex:
                q = sum(float(e.get("execQty") or 0) for e in ex)
                if q > 0:
                    avg = sum(float(e["execPrice"]) * float(e["execQty"]) for e in ex) / q
                    fee = sum(float(e.get("execFee") or 0) for e in ex)
        return {"status": st, "filled_qty": filled, "avg_price": avg, "fee_usd": fee,
                "order_id": raw.get("orderId")}

    def positions(self, coin: str) -> dict | None:
        return self.c.get_option_positions(coin)

    def open_orders(self) -> list[dict] | None:
        return self.c.get_open_orders_all()

    def position_im(self, symbol: str, qty: float) -> float | None:
        """Exchange IM attributable to `qty` of this symbol (review r2 F11)."""
        base = symbol.split("-")[0]
        pos = self.c.get_option_positions(base) or {}
        p = pos.get(symbol)
        if not p or not p.get("im") or not p.get("size"):
            return None
        return p["im"] * min(1.0, qty / abs(p["size"]))


# ── Price rules (shared by loop._quote_px and the state machine) ─────────────

def close_cap(quote: dict | None) -> float | None:
    """Max buy-to-close price under the spread guard: mark×(1+EXIT_MAX_SLIP)."""
    mark = (quote or {}).get("mark") or 0.0
    return mark * (1 + config.EXIT_MAX_SLIP) if mark > 0 else None


def open_floor(quote: dict | None) -> float | None:
    mark = (quote or {}).get("mark") or 0.0
    return mark * (1 - config.EXIT_MAX_SLIP) if mark > 0 else None


def _stage_price(o: dict, quote: dict | None) -> float | None:
    """Price for the next stage from a fresh quote {bid, ask, mark}. None =
    no usable quote (caller applies a blind fallback or waits)."""
    if not quote:
        return None
    bid, ask, mark = quote.get("bid") or 0.0, quote.get("ask") or 0.0, quote.get("mark") or 0.0
    if o["kind"] == "open":                 # seller retreats to bid, never below the floor
        px = bid or (mark * 0.99 if mark else 0.0)
        fl = open_floor(quote)
        return max(px, fl) if px and fl else (px or None)
    cap = close_cap(quote)
    if o["stage"] == "urgent":
        # chase: +CHASE per step, never above the ask, never above the
        # ceiling mark_fresh×(1+URGENT_MAX_SLIP) — a phantom ask is not chased
        step = o["price"] * (1 + config.EXEC_URGENT_CHASE_PCT)
        ceil = mark * (1 + config.EXEC_URGENT_MAX_SLIP) if mark > 0 else None
        px = step
        if ask:
            px = min(px, ask)
        if ceil:
            px = min(px, ceil)
        return px
    px = ask or (mark * 1.01 if mark else 0.0)
    return min(px, cap) if px and cap else (px or None)


# ── Order lifecycle ──────────────────────────────────────────────────────────

TERMINAL_FILLED = ("Filled",)
TERMINAL_DEAD = ("Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled")


def submit(conn, ex, *, kind: str, coin: str, side: str, symbol: str, qty: float,
           price: float, urgent: bool, reason: str | None, payload: dict,
           now_ms: int, pos_id: int | None = None) -> int:
    """Create the order row, reserve the position, place. Returns the row id.
    Outcomes: placed (active, order_id set) | unknown (active, order_id NULL,
    adopted later) | rejected (status=error, position released)."""
    oid = repo.insert_order(conn, {
        "kind": kind, "pos_id": pos_id, "coin": coin, "side": side,
        "option_symbol": symbol, "qty": qty, "price": price,
        "stage": "urgent" if urgent else "mid", "urgent": int(urgent),
        "order_link_id": None, "placed_at_ms": now_ms, "status": "active",
        "reason": reason, "payload": json.dumps(payload),
        "created_at_ms": now_ms, "updated_at_ms": now_ms,
    })
    link = f"jony-{kind[0]}-{oid}-{now_ms}"      # ≤36 chars, unique per row+time
    repo.update_order(conn, oid, order_link_id=link, commit=False)
    if pos_id is not None:                       # reserve BEFORE sending (r2 F6)
        repo.set_closing(conn, pos_id, oid, bump_attempts=True, commit=False)
    conn.commit()
    o = repo.get_order(conn, oid)
    outcome, ex_id = ex.place(o)
    if outcome == "ok":
        repo.update_order(conn, oid, order_id=ex_id, updated_at_ms=now_ms)
    elif outcome == "rejected":
        repo.update_order(conn, oid, status="error", stage="cancelled",
                          updated_at_ms=now_ms, commit=False)
        if pos_id is not None:
            repo.set_closing(conn, pos_id, None, commit=False)
        conn.commit()
    # 'unknown': leave active with order_id NULL — advance() adopts or retires it
    return oid


def _bump(conn, o: dict, now_ms: int) -> int:
    n = (o.get("attempts") or 0) + 1
    repo.update_order(conn, o["id"], attempts=n, updated_at_ms=now_ms)
    o["attempts"] = n
    return n


def _page(conn, o: dict, now_ms: int, why: str, notify) -> None:
    """Bounded alerting for a stuck-but-safe order: every EXEC_ALERT_EVERY ticks."""
    n = _bump(conn, o, now_ms)
    if n % config.EXEC_ALERT_EVERY == 1:
        notify(f"ORDER {o['id']} {o['kind']} {o['option_symbol']} stuck: {why} "
               f"(tick {n}) — holding, no duplicate will be sent")


def _retire_absent(conn, o: dict, now_ms: int, notify) -> None:
    """The exchange definitively never had this order: safe to drop it and
    release the position (nothing rests on the book)."""
    repo.update_order(conn, o["id"], status="error", stage="cancelled",
                      updated_at_ms=now_ms, commit=False)
    if o["pos_id"] is not None:
        repo.set_closing(conn, o["pos_id"], None, commit=False)
    conn.commit()
    notify(f"ORDER {o['id']} {o['kind']} {o['option_symbol']} never reached the "
           f"exchange — dropped, will retry")


def advance(conn, ex, o: dict, now_ms: int, quote: dict | None,
            on_open, on_close, notify) -> None:
    """One tick of the state machine for one active order. on_open(conn, o,
    fill, now_ms, notes) / on_close(conn, o, fill, now_ms, notes) book the
    fill WITHOUT committing; _finalize commits with the order row, then sends
    the notes."""
    if o["status"] != "active":
        return
    st = ex.poll(o)
    if st is None:
        _page(conn, o, now_ms, "exchange unreachable", notify)
        return                              # unknown — never guess (r2 F3)
    if st["status"] == "Absent":
        if not o.get("order_id"):
            # placement outcome was unknown: give Bybit a few ticks, then retire
            if _bump(conn, o, now_ms) >= config.EXEC_MAX_ATTEMPTS:
                _retire_absent(conn, o, now_ms, notify)
            return
        _page(conn, o, now_ms, "known order missing from exchange", notify)
        return
    if st.get("order_id") and not o.get("order_id"):
        repo.update_order(conn, o["id"], order_id=st["order_id"], updated_at_ms=now_ms)
        o["order_id"] = st["order_id"]
    filled, avg = st["filled_qty"], st["avg_price"]
    status = st["status"]
    src = o["stage"]
    if filled > 0 and not avg:
        _page(conn, o, now_ms, "fill reported without price", notify)
        return

    if status in TERMINAL_FILLED or (filled >= o["qty"] - 1e-9 and filled > 0):
        _finalize(conn, o, now_ms, filled, avg, st.get("fee_usd"), src, "filled",
                  on_open, on_close, notify)
        return
    if status in TERMINAL_DEAD:
        _finalize(conn, o, now_ms, filled, avg, st.get("fee_usd"), src,
                  "partial" if filled > 0 else "no_fill", on_open, on_close, notify)
        return

    if o["stage"] == "cancelled":
        if not ex.cancel(o):                # our cancel didn't take: re-issue, page
            _page(conn, o, now_ms, "cancel failing", notify)
        return

    placed = o["placed_at_ms"] if o["placed_at_ms"] is not None else now_ms
    waited = (now_ms - placed) / 1000.0
    age = (now_ms - o["created_at_ms"]) / 1000.0

    if not o["urgent"] and age >= config.EXEC_DEADLINE_S:
        ex.cancel(o)
        repo.update_order(conn, o["id"], stage="cancelled", updated_at_ms=now_ms)
        return
    if o["stage"] == "mid" and waited >= config.EXEC_MID_WAIT_S:
        px = _stage_price(o, quote)
        if px is None:
            return                          # no quote: deadline above will cancel
        sent = ex.amend(o, px)
        if sent is not None:
            repo.update_order(conn, o["id"], stage="retreat", price=sent,
                              placed_at_ms=now_ms, updated_at_ms=now_ms)
            notify(f"ORDER {o['kind']} {o['option_symbol']} retreat → {sent:g}")
        return
    if o["stage"] == "retreat" and waited >= config.EXEC_RETREAT_WAIT_S:
        ex.cancel(o)
        repo.update_order(conn, o["id"], stage="cancelled", updated_at_ms=now_ms)
        return
    if o["stage"] == "urgent" and waited >= config.EXEC_URGENT_WAIT_S:
        px = _stage_price(o, quote)
        if px is None:                      # blind step when quotes are down
            px = o["price"] * (1 + config.EXEC_URGENT_CHASE_PCT)
        if px > o["price"] + 1e-12:
            sent = ex.amend(o, px)
            if sent is not None:
                repo.update_order(conn, o["id"], price=sent, placed_at_ms=now_ms,
                                  updated_at_ms=now_ms)
                return
        # pinned at ceiling/ask (or amend refused): wait, page periodically
        repo.update_order(conn, o["id"], placed_at_ms=now_ms, updated_at_ms=now_ms)
        _page(conn, o, now_ms, f"urgent close pinned at {o['price']:g}", notify)
        return


def _finalize(conn, o: dict, now_ms: int, filled: float, avg: float | None,
              fee: float | None, src: str, outcome: str, on_open, on_close,
              notify) -> None:
    """Book the fill and close the order row in ONE transaction; notify after."""
    fill = {"qty": filled, "avg_price": avg, "fee_usd": fee, "source": src}
    notes: list[str] = []
    booked = True
    try:
        if filled > 0 and avg:
            if o["kind"] == "open":
                on_open(conn, o, fill, now_ms, notes)
            else:
                booked = on_close(conn, o, fill, now_ms, notes)
        elif o["kind"] == "close" and o["pos_id"] is not None:
            repo.set_closing(conn, o["pos_id"], None, commit=False)   # retry next tick
        repo.update_order(conn, o["id"], commit=False,
                          status=(outcome if filled > 0 else "no_fill"),
                          filled_qty=filled, avg_price=avg, fee_usd=fee,
                          stage="done" if filled > 0 else "cancelled",
                          updated_at_ms=now_ms)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    for msg in notes:
        notify(msg)
    if booked is False:
        _halt(conn, now_ms, f"order {o['id']} close fill for pos {o['pos_id']} "
                            f"could not be booked (position closed by another order)", notify)


def process_orders(conn, ex, now_ms: int, quotes_by_coin, on_open, on_close, notify) -> int:
    """Advance every active order (both modes). quotes_by_coin(coin) -> {symbol: quote}."""
    n = 0
    cache: dict[str, dict] = {}
    for o in repo.active_orders(conn):
        q = None
        if ex.live:
            try:
                if o["coin"] not in cache:
                    cache[o["coin"]] = quotes_by_coin(o["coin"]) or {}
                q = cache[o["coin"]].get(o["option_symbol"])
            except Exception as e:      # quote failure must not stall the machine
                print(f"[exec] quote failed for {o['option_symbol']}: {e}", flush=True)
        try:
            advance(conn, ex, o, now_ms, q, on_open, on_close, notify)
        except Exception as e:          # one poisoned order must not stall the tick
            conn.rollback()
            print(f"[exec] order {o['id']} advance failed: {e!r}", flush=True)
            _page(conn, o, now_ms, f"booking error {e!r}"[:100], notify)
        n += 1
    return n


def inflight_opens(conn) -> list[dict]:
    """Active open orders as pseudo-positions for caps/margin (review #15)."""
    out = []
    for o in repo.active_orders(conn):
        if o["kind"] != "open":
            continue
        try:
            pl = json.loads(o["payload"] or "{}")
        except ValueError:
            pl = {}
        out.append({"coin": o["coin"], "side": o["side"],
                    "margin_usd": float(pl.get("margin_est") or 0.0), "inflight": True})
    return out


# ── Reconcile ────────────────────────────────────────────────────────────────

def reconcile(conn, ex, open_positions: list[dict], now_ms: int, notify) -> bool:
    """Exchange book vs DB book by symbol, plus resting orders vs our active
    orders. Mismatch → exec_halt (entries only; exits keep running). Returns
    True when consistent. Positions with an in-flight order and positions
    past expiry (exchange settles first) are excluded from the strict compare."""
    if not ex.live:
        return True
    active = repo.active_orders(conn)
    inflight = {o["option_symbol"] for o in active}
    links = {o["order_link_id"] for o in active}
    db: dict[str, float] = {}
    for p in open_positions:
        if now_ms >= p["expiry_ms"]:
            inflight.add(p["option_symbol"])
            continue
        db[p["option_symbol"]] = db.get(p["option_symbol"], 0.0) + p["qty"]
    exch: dict[str, float] = {}
    for coin in config.COIN_SPEC:
        pos = ex.positions(coin)
        if pos is None:
            return _halt(conn, now_ms, f"reconcile: positions({coin}) unavailable", notify)
        for sym, v in pos.items():
            if v.get("side") == "Sell" or v["size"] < 0:
                exch[sym] = exch.get(sym, 0.0) + abs(v["size"])
            else:
                return _halt(conn, now_ms, f"reconcile: LONG option on exchange {sym}", notify)
    problems = []
    for sym in set(db) | set(exch):
        if sym in inflight:
            continue
        a, b = round(db.get(sym, 0.0), 4), round(exch.get(sym, 0.0), 4)
        if abs(a - b) > 1e-6:
            problems.append(f"{sym}: db {a} vs exch {b}")
    resting = ex.open_orders()
    if resting is None:
        return _halt(conn, now_ms, "reconcile: open orders unavailable", notify)
    for r in resting:
        if r.get("orderLinkId") not in links:
            problems.append(f"foreign/zombie order {r.get('symbol')} {r.get('orderLinkId') or r.get('orderId')}")
    if problems:
        return _halt(conn, now_ms, "reconcile mismatch: " + "; ".join(problems), notify)
    halted, why = repo.get_exec_halt(conn)
    if halted and (why or "").startswith("reconcile"):
        repo.set_exec_halt(conn, False, None)
        notify("reconcile OK again — exec halt lifted")
    return True


def _halt(conn, now_ms: int, reason: str, notify) -> bool:
    halted, why = repo.get_exec_halt(conn)
    if halted and why and not why.startswith("reconcile") and reason.startswith("reconcile"):
        return False                        # never mask a manual-recovery halt (r2 F5)
    if not halted or why != reason:
        repo.set_exec_halt(conn, True, reason)
        notify(f"EXEC HALT: {reason} — new entries blocked, exits continue "
               f"(POST /exec/unhalt after checking the book)")
    return False


def make_executor(client):
    if config.TRADING_MODE == "live":
        return LiveExecutor(client)
    return PaperExecutor()


def live_preflight(client) -> str | None:
    """Fail-closed startup check for live mode. Returns error text or None."""
    if config.TRADING_MODE != "live":
        return None
    if not getattr(client, "has_key", False):
        return "live mode without BYBIT_API_KEY/SECRET"
    ks = client.key_status()
    if not ks:
        return "live mode: query-api failed"
    if str(ks.get("readOnly")) == "1":
        return "live mode: API key is read-only"
    perms = ks.get("permissions") or {}
    if "OptionsTrade" not in (perms.get("Options") or []):
        return "live mode: API key lacks OptionsTrade"
    return None
