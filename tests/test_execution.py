"""Execution state machine (Track B 2026-08-26) against an in-memory fake
exchange: mid→retreat→cancel, partial fills, urgent chase, reconcile halt,
live settlement, SL equity cap, near-high sizing, preflight."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["JONY_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "jony_exec_test.db")

import loop as jony_loop  # noqa: E402
from db import repo  # noqa: E402
from services import config, execution, portfolio  # noqa: E402
from services.bybit_client import round_to_tick, fmt_qty  # noqa: E402

S = 1000  # ms


class FakeClient:
    """Minimal Bybit stand-in. Orders fill only when the test says so."""
    has_key = True

    def __init__(self):
        self.orders = {}          # link -> dict
        self.n = 0
        self.positions = {}       # coin -> {symbol: {...}}
        self.delivery = {}
        self.fail_place = False
        self.fail_cancel = False
        self.fail_amend = False
        self.execs = {}           # order_id -> list of executions
        self.ghost_on_place = False   # place "fails" but the order exists
        self.api_down = False         # get_order/positions return None
        self.resting = []             # extra open orders for reconcile
        self.log = []

    def tick_size(self, symbol):
        return 5.0 if symbol.startswith("BTC") else 0.1

    def place_order(self, symbol, side, qty, price, link_id, reduce_only):
        self.log.append(("place", symbol, side, qty, price, reduce_only))
        self.n += 1
        if self.ghost_on_place:             # accepted by Bybit, response lost
            self.orders[link_id] = {"orderId": f"ex{self.n}", "orderLinkId": link_id,
                                    "orderStatus": "New", "cumExecQty": "0",
                                    "avgPrice": "0", "cumExecFee": "0", "price": price,
                                    "qty": qty, "symbol": symbol}
            return "unknown", None
        if self.fail_place:
            return "rejected", None
        if self.api_down:
            return "unknown", None
        self.orders[link_id] = {"orderId": f"ex{self.n}", "orderLinkId": link_id,
                                "orderStatus": "New", "cumExecQty": "0",
                                "avgPrice": "0", "cumExecFee": "0", "price": price,
                                "qty": qty, "symbol": symbol}
        return "ok", f"ex{self.n}"

    def amend_order(self, symbol, order_id, price):
        self.log.append(("amend", symbol, order_id, price))
        if self.fail_amend:
            return False
        for o in self.orders.values():
            if o["orderId"] == order_id:
                o["price"] = price
                return True
        return False

    def cancel_order(self, symbol, order_id, link_id=None):
        self.log.append(("cancel", symbol, order_id or link_id))
        if self.fail_cancel:
            return False
        for o in self.orders.values():
            if (o["orderId"] == order_id or o["orderLinkId"] == link_id) \
                    and o["orderStatus"] in ("New", "PartiallyFilled"):
                o["orderStatus"] = ("PartiallyFilledCanceled"
                                    if o["orderStatus"] == "PartiallyFilled" else "Cancelled")
                return True
        return False

    def get_order(self, symbol, link_id):
        if self.api_down:
            return None
        return self.orders.get(link_id, {})

    def get_open_orders_all(self):
        if self.api_down:
            return None
        return [{"symbol": o["symbol"], "orderLinkId": o["orderLinkId"], "orderId": o["orderId"]}
                for o in self.orders.values() if o["orderStatus"] in ("New", "PartiallyFilled")] + self.resting

    def get_executions(self, symbol, order_id):
        return self.execs.get(order_id, [])

    def get_option_positions(self, base_coin):
        if self.api_down:
            return None
        return self.positions.get(base_coin, {})

    def get_delivery(self, symbol):
        return self.delivery.get(symbol)

    def key_status(self):
        return {"readOnly": 0, "permissions": {"Options": ["OptionsTrade"]}}

    # test helpers
    def fill(self, link_id, qty, price, fee=0.05, full=True):
        o = self.orders[link_id]
        o["cumExecQty"] = str(qty)
        o["avgPrice"] = str(price)
        o["cumExecFee"] = str(fee)
        o["orderStatus"] = "Filled" if full else "PartiallyFilled"

    def last_link(self):
        return list(self.orders)[-1]


QUOTE = {"bid": 100.0, "ask": 110.0, "mark": 105.0}


def _pos(conn, **kw):
    d = {"coin": "ETH", "side": "P", "option_symbol": "ETH-TEST-P",
         "strike": 2500, "expiry_ms": 168 * 3_600_000, "qty": 0.3,
         "opened_at_ms": 0, "underlying_at_open": 2500, "entry_credit": 120.0,
         "entry_source": "mid", "margin_usd": 100.0, "fee_open_usd": 0.3,
         "tp2_pct": 0.7, "sl_pct": 1.75, "hold_h": 120, "signal_payload": None}
    d.update(kw)
    return repo.insert_position(conn, d)


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = repo.connect()
        repo.apply_schema(self.conn)
        for tb in ("positions", "bot_state", "orders", "signal_audit", "bot_control"):
            self.conn.execute(f"DELETE FROM {tb}")
        self.conn.commit()
        repo.init_state(self.conn, 1500.0, 0)
        self.fake = FakeClient()
        self.ex = execution.LiveExecutor(self.fake)
        self._p = patch.object(jony_loop, "executor", self.ex)
        self._p.start()
        self._n = patch("loop.notify")
        self._n.start()

    def tearDown(self):
        self._p.stop()
        self._n.stop()

    def tick(self, now_ms, quote=QUOTE):
        execution.process_orders(self.conn, self.ex, now_ms, lambda c: {"ETH-TEST-P": quote},
                                 jony_loop._on_open_fill, jony_loop._on_close_fill,
                                 lambda *_: None)


class TestOpenFlow(Base):
    def submit_open(self, now=0, qty=0.3):
        return execution.submit(
            self.conn, self.ex, kind="open", coin="ETH", side="P", symbol="ETH-TEST-P",
            qty=qty, price=105.0, urgent=False, reason="bid",
            payload={"strike": 2500, "expiry_ms": 168 * 3_600_000, "spot": 2500,
                     "source": "bid", "margin_est": 100.0, "tp2_pct": 0.7,
                     "sl_pct": 1.75, "hold_h": 120, "ev": {"x": 1}}, now_ms=now)

    def test_mid_then_retreat_then_fill(self):
        oid = self.submit_open()
        link = self.fake.last_link()
        self.assertEqual(self.fake.log[0][1:], ("ETH-TEST-P", "Sell", 0.3, 105.0, False))
        self.tick(10 * S)                                  # still mid
        self.assertEqual(repo.get_order(self.conn, oid)["stage"], "mid")
        self.tick(config.EXEC_MID_WAIT_S * S + S)          # retreat to bid
        o = repo.get_order(self.conn, oid)
        self.assertEqual(o["stage"], "retreat")
        self.assertEqual(o["price"], 100.0)
        self.assertEqual(self.fake.log[-1][0], "amend")
        self.fake.fill(link, 0.3, 100.0, fee=0.07)
        self.tick(config.EXEC_MID_WAIT_S * S + 5 * S)
        pos = repo.open_positions(self.conn)
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["entry_credit"], 100.0)
        self.assertEqual(pos[0]["entry_source"], "retreat")
        self.assertAlmostEqual(pos[0]["fee_open_usd"], 0.07)
        # SL capped at 2.5% × 1500 = $37.5 over credit 100 × 0.3 = $30 → 1.25
        self.assertAlmostEqual(pos[0]["sl_pct"], 1.25, places=3)
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "filled")

    def test_no_fill_cancels_without_position(self):
        oid = self.submit_open()
        t = (config.EXEC_MID_WAIT_S + 1) * S
        self.tick(t)                                        # retreat
        t += (config.EXEC_RETREAT_WAIT_S + 1) * S
        self.tick(t)                                        # cancel
        self.assertEqual(self.fake.log[-1][0], "cancel")
        self.tick(t + S)                                    # settle cancelled
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "no_fill")
        self.assertEqual(repo.open_positions(self.conn), [])
        self.assertEqual(repo.active_orders(self.conn), [])

    def test_partial_fill_at_cancel_books_partial(self):
        oid = self.submit_open()
        link = self.fake.last_link()
        t = (config.EXEC_MID_WAIT_S + 1) * S
        self.tick(t)
        self.fake.fill(link, 0.1, 100.0, full=False)
        t += (config.EXEC_RETREAT_WAIT_S + 1) * S
        self.tick(t)                                        # cancel remainder
        self.tick(t + S)
        o = repo.get_order(self.conn, oid)
        self.assertEqual(o["status"], "partial")
        pos = repo.open_positions(self.conn)
        self.assertEqual(len(pos), 1)
        self.assertAlmostEqual(pos[0]["qty"], 0.1)
        self.assertAlmostEqual(pos[0]["margin_usd"], 100.0 / 3, places=4)

    def test_placement_rejected_recorded(self):
        self.fake.fail_place = True
        oid = self.submit_open()
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "error")
        self.assertEqual(repo.active_orders(self.conn), [])

    def test_placement_unknown_stays_active_then_retired(self):
        self.fake.api_down = True
        oid = self.submit_open()
        o = repo.get_order(self.conn, oid)
        self.assertEqual((o["status"], o["order_id"]), ("active", None))
        for i in range(3):
            self.tick(i * S)                      # API down: wait, no error
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "active")
        self.fake.api_down = False                # back: exchange says absent
        for i in range(config.EXEC_MAX_ATTEMPTS + 1):
            self.tick(10 * S + i * S)
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "error")
        self.assertFalse(repo.get_exec_halt(self.conn)[0])   # nothing rests: no halt

    def test_link_ids_unique_and_prefixed(self):
        a = repo.get_order(self.conn, self.submit_open(now=1))["order_link_id"]
        b = repo.get_order(self.conn, self.submit_open(now=1))["order_link_id"]
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("jony-o-"))
        self.assertLessEqual(len(a), 36)


class TestCloseFlow(Base):
    def test_urgent_close_goes_to_ask_and_chases(self):
        pid = _pos(self.conn)
        self.fake.positions = {"ETH": {"ETH-TEST-P": {"size": 0.3, "side": "Sell"}}}
        p = repo.get_open_position(self.conn, pid)
        jony_loop._request_close(self.conn, repo.get_state(self.conn), p, 0, 110.0,
                                 "sl", "closed_sl", True, True, QUOTE)
        self.assertEqual(self.fake.log[0][1:], ("ETH-TEST-P", "Buy", 0.3, 110.0, True))
        self.assertIsNotNone(repo.get_open_position(self.conn, pid)["closing_order_id"])
        wide = {"bid": 100.0, "ask": 130.0, "mark": 105.0}   # cap 110.25 < ask
        self.tick((config.EXEC_URGENT_WAIT_S + 1) * S, quote=wide)
        o = repo.active_orders(self.conn)[0]
        self.assertEqual(o["stage"], "urgent")
        self.assertAlmostEqual(o["price"], 110.0 * 1.02, places=6)   # +2%, still < ask
        self.tick((2 * config.EXEC_URGENT_WAIT_S + 3) * S, quote=QUOTE)  # ask 110 → never above ask
        self.assertAlmostEqual(repo.active_orders(self.conn)[0]["price"], 110.0 * 1.02, places=6)
        self.fake.fill(self.fake.last_link(), 0.3, 112.0, fee=0.09)
        self.tick((config.EXEC_URGENT_WAIT_S + 2) * S)
        row = dict(self.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        self.assertEqual(row["status"], "closed_sl")
        self.assertEqual(row["exit_debit"], 112.0)
        # pnl = (120-112)*0.3 - 0.3 - 0.09 = 2.01
        self.assertAlmostEqual(row["pnl_usd"], 2.01, places=3)
        st = repo.get_state(self.conn)
        self.assertAlmostEqual(st["equity_usd"], 1502.01, places=3)

    def test_patient_close_uses_mid_then_ask(self):
        pid = _pos(self.conn)
        p = repo.get_open_position(self.conn, pid)
        jony_loop._request_close(self.conn, repo.get_state(self.conn), p, 0, 110.0,
                                 "tp2", "closed_tp2", True, False, QUOTE)
        self.assertEqual(self.fake.log[0][4], 105.0)      # mid
        self.tick((config.EXEC_MID_WAIT_S + 1) * S)
        self.assertEqual(repo.active_orders(self.conn)[0]["price"], 110.0)  # ask

    def test_no_fill_clears_closing_and_escalates_next_time(self):
        pid = _pos(self.conn)
        self.fake.positions = {"ETH": {"ETH-TEST-P": {"size": 0.3, "side": "Sell"}}}
        p = repo.get_open_position(self.conn, pid)
        jony_loop._request_close(self.conn, repo.get_state(self.conn), p, 0, 110.0,
                                 "tp2", "closed_tp2", True, False, QUOTE)
        t = (config.EXEC_MID_WAIT_S + 1) * S
        self.tick(t)
        t += (config.EXEC_RETREAT_WAIT_S + 1) * S
        self.tick(t)
        self.tick(t + S)
        p = repo.get_open_position(self.conn, pid)
        self.assertIsNone(p["closing_order_id"])
        self.assertEqual(p["close_attempts"], 1)
        self.assertEqual(p["status"], "open")
        jony_loop._request_close(self.conn, repo.get_state(self.conn), p, t + 2 * S, 110.0,
                                 "tp2", "closed_tp2", True, False, QUOTE)
        self.assertEqual(repo.active_orders(self.conn)[0]["stage"], "urgent")

    def test_partial_close_books_child_and_keeps_remainder(self):
        pid = _pos(self.conn, qty=0.3)
        p = repo.get_open_position(self.conn, pid)
        jony_loop._request_close(self.conn, repo.get_state(self.conn), p, 0, 110.0,
                                 "tp2", "closed_tp2", True, False, QUOTE)
        link = self.fake.last_link()
        t = (config.EXEC_MID_WAIT_S + 1) * S
        self.tick(t)
        self.fake.fill(link, 0.1, 105.0, fee=0.03, full=False)
        t += (config.EXEC_RETREAT_WAIT_S + 1) * S
        self.tick(t)
        self.tick(t + S)
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM positions ORDER BY id")]
        self.assertEqual(len(rows), 2)
        rem, child = rows[0], rows[1]
        self.assertEqual(rem["status"], "open")
        self.assertAlmostEqual(rem["qty"], 0.2)
        self.assertIsNone(rem["closing_order_id"])
        self.assertEqual(child["status"], "closed_tp2")
        self.assertAlmostEqual(child["qty"], 0.1)
        self.assertAlmostEqual(child["fee_open_usd"], 0.1)
        # pnl = (120-105)*0.1 - 0.1 - 0.03 = 1.37
        self.assertAlmostEqual(child["pnl_usd"], 1.37, places=3)

    def test_manage_exits_skips_position_with_inflight_close(self):
        pid = _pos(self.conn)
        repo.set_closing(self.conn, pid, 999)
        with patch("loop.bybit_client.get_option_marks",
                   return_value={"ETH-TEST-P": {"mark": 5.0, "bid": 4, "ask": 6}}):
            jony_loop.manage_exits(self.conn, repo.get_state(self.conn), 10 * S)
        self.assertEqual(repo.get_open_position(self.conn, pid)["status"], "open")
        self.assertEqual(self.fake.log, [])


class TestReconcile(Base):
    def test_match_and_mismatch_toggle_halt(self):
        pid = _pos(self.conn, qty=0.3)
        self.fake.positions = {"ETH": {"ETH-TEST-P": {"size": 0.3, "side": "Sell", "im": 40.0}}, "BTC": {}}
        ok = execution.reconcile(self.conn, self.ex, repo.open_positions(self.conn), 0, lambda *_: None)
        self.assertTrue(ok)
        self.assertEqual(repo.get_exec_halt(self.conn), (False, None))
        self.fake.positions["ETH"]["ETH-TEST-P"]["size"] = 0.2
        ok = execution.reconcile(self.conn, self.ex, repo.open_positions(self.conn), 0, lambda *_: None)
        self.assertFalse(ok)
        halted, why = repo.get_exec_halt(self.conn)
        self.assertTrue(halted)
        self.assertIn("ETH-TEST-P", why)
        self.fake.positions["ETH"]["ETH-TEST-P"]["size"] = 0.3
        ok = execution.reconcile(self.conn, self.ex, repo.open_positions(self.conn), 0, lambda *_: None)
        self.assertTrue(ok)
        self.assertEqual(repo.get_exec_halt(self.conn)[0], False)

    def test_unknown_exchange_position_and_api_failure_halt(self):
        self.fake.positions = {"ETH": {"ETH-OTHER-P": {"size": 0.1, "side": "Sell"}}, "BTC": {}}
        self.assertFalse(execution.reconcile(self.conn, self.ex, [], 0, lambda *_: None))
        self.assertTrue(repo.get_exec_halt(self.conn)[0])
        self.fake.positions = {"ETH": None}
        self.assertFalse(execution.reconcile(self.conn, self.ex, [], 0, lambda *_: None))

    def test_inflight_symbol_excluded(self):
        execution.submit(self.conn, self.ex, kind="open", coin="ETH", side="P",
                         symbol="ETH-TEST-P", qty=0.3, price=105.0, urgent=False,
                         reason="bid", payload={}, now_ms=0)
        self.fake.positions = {"ETH": {}, "BTC": {}}
        self.assertTrue(execution.reconcile(self.conn, self.ex, [], 0, lambda *_: None))

    def test_paper_executor_never_halts(self):
        self.assertTrue(execution.reconcile(self.conn, execution.PaperExecutor(), [], 0, lambda *_: None))


class TestTryFireLive(Base):
    def _fire(self, now=10_000_000):
        ev = {"active_side": "P", "spot": 2500.0, "ready": True, "dist_7d_high_pct": -5.0}
        pick = {"symbol": "ETH-TEST-P", "strike": 2500.0, "expiry_ms": 168 * 3_600_000,
                "side": "P", "bid": 100.0, "ask": 110.0, "mark_price": 105.0}
        with patch("loop.bybit_client.get_options_tickers", return_value=[pick]), \
                patch("loop.pick_atm_option", return_value=pick):
            jony_loop.try_fire(self.conn, repo.get_state(self.conn), "ETH", ev, now)

    def _last_audit(self):
        return dict(self.conn.execute(
            "SELECT accepted, reject_reason FROM signal_audit ORDER BY id DESC LIMIT 1").fetchone())

    def test_live_open_places_mid_and_no_position_until_fill(self):
        self._fire()
        self.assertEqual(self._last_audit()["accepted"], 1)
        self.assertEqual(self.fake.log[0][4], 105.0)
        self.assertEqual(repo.open_positions(self.conn), [])
        self.assertEqual(len(repo.active_orders(self.conn)), 1)
        # second signal on the same key while in flight: the in-flight order
        # already counts as a position for the caps
        self._fire(now=10_000_000 + 60 * 60 * S)
        self.assertEqual(self._last_audit()["reject_reason"], "per_key_cap")

    def test_exec_halt_blocks_entries(self):
        repo.set_exec_halt(self.conn, True, "reconcile mismatch: x")
        self._fire()
        self.assertEqual(self._last_audit()["reject_reason"], "exec_halt")
        self.assertEqual(self.fake.log, [])

    def test_near_high_mult_applied(self):
        ev = {"active_side": "P", "spot": 2500.0, "ready": True, "dist_7d_high_pct": -0.5}
        pick = {"symbol": "ETH-TEST-P", "strike": 2500.0, "expiry_ms": 168 * 3_600_000,
                "side": "P", "bid": 100.0, "ask": 110.0, "mark_price": 105.0}
        with patch("loop.bybit_client.get_options_tickers", return_value=[pick]), \
                patch("loop.pick_atm_option", return_value=pick), \
                patch.object(config, "NEAR_HIGH_PCT", 1.5), \
                patch.object(portfolio.config, "NEAR_HIGH_PCT", 1.5):
            jony_loop.try_fire(self.conn, repo.get_state(self.conn), "ETH", ev, 10_000_000)
        o = repo.active_orders(self.conn)[0]
        self.assertEqual(json.loads(o["payload"])["size_mult"], 0.5)
        # budget 1500×0.15×0.5 = 112.5; lot margin (0.1×2500+100)×0.1 = 35 → 3 lots = 0.3
        self.assertAlmostEqual(o["qty"], 0.3)


class TestSettlementLive(Base):
    def test_expired_position_settled_from_delivery_record(self):
        pid = _pos(self.conn, expiry_ms=1000, strike=2500)
        self.fake.positions = {"ETH": {}, "BTC": {}}
        self.fake.delivery = {"ETH-TEST-P": {"deliveryPrice": "2400", "fee": "0"}}
        p = repo.get_open_position(self.conn, pid)
        with patch("loop.bybit_client.get_delivery", side_effect=self.fake.get_delivery, create=True):
            ok = jony_loop._settle_expired_live(self.conn, repo.get_state(self.conn), p, 5000, set())
        self.assertTrue(ok)
        row = dict(self.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        self.assertEqual(row["status"], "closed_time")
        self.assertEqual(row["exit_debit"], 100.0)          # 2500-2400 intrinsic
        self.assertEqual(row["exit_reason"], "expiry_settle")
        # fee "0" from the delivery record is real (OTM), not "unknown" (r2 F8)
        self.assertAlmostEqual(row["pnl_usd"], (120 - 100) * 0.3 - 0.3, places=4)

    def test_still_on_exchange_waits(self):
        pid = _pos(self.conn, expiry_ms=1000)
        self.fake.positions = {"ETH": {"ETH-TEST-P": {"size": 0.3, "side": "Sell"}}}
        p = repo.get_open_position(self.conn, pid)
        self.assertFalse(jony_loop._settle_expired_live(self.conn, repo.get_state(self.conn), p, 5000, set()))
        self.assertEqual(repo.get_open_position(self.conn, pid)["status"], "open")


class TestPureRules(unittest.TestCase):
    def test_sl_equity_cap(self):
        self.assertAlmostEqual(portfolio.sl_pct_effective(1.75, 1500, 100, 0.3, 0.025), 1.25)
        self.assertEqual(portfolio.sl_pct_effective(1.75, 1500, 10, 0.3, 0.025), 1.75)  # cap looser → unchanged
        self.assertEqual(portfolio.sl_pct_effective(1.75, 1500, 100, 0.3, 0.0), 1.75)   # off
        self.assertEqual(portfolio.sl_pct_effective(1.75, 0, 100, 0.3, 0.025), 1.75)    # bad equity

    def test_near_high_mult(self):
        self.assertEqual(portfolio.near_high_mult("P", -0.5, 1.5, 0.5), 0.5)
        self.assertEqual(portfolio.near_high_mult("P", -3.0, 1.5, 0.5), 1.0)
        self.assertEqual(portfolio.near_high_mult("C", -0.5, 1.5, 0.5), 1.0)
        self.assertEqual(portfolio.near_high_mult("P", None, 1.5, 0.5), 1.0)
        self.assertEqual(portfolio.near_high_mult("P", -0.5, 0, 0.5), 1.0)

    def test_dist_7d_high(self):
        k = [{"high": 100 + i} for i in range(200)]
        self.assertAlmostEqual(jony_loop.dist_7d_high_pct(k, 269.01), (269.01 / 299 - 1) * 100)
        self.assertIsNone(jony_loop.dist_7d_high_pct(k[:10], 100))

    def test_round_to_tick_and_fmt(self):
        self.assertEqual(round_to_tick(103.2, 5, up=True), 105.0)
        self.assertEqual(round_to_tick(103.2, 5, up=False), 100.0)
        self.assertEqual(round_to_tick(2.34, 0.1, up=False), 2.3)
        self.assertEqual(fmt_qty(0.1), "0.1")
        self.assertEqual(fmt_qty(0.01), "0.01")

    def test_preflight(self):
        class C:
            has_key = True
            def key_status(self):
                return {"readOnly": 1, "permissions": {"Options": ["OptionsTrade"]}}
        with patch.object(config, "TRADING_MODE", "live"):
            self.assertIn("read-only", execution.live_preflight(C()))
            C.key_status = lambda self: {"readOnly": 0, "permissions": {"Options": []}}
            self.assertIn("OptionsTrade", execution.live_preflight(C()))
            C.key_status = lambda self: {"readOnly": 0, "permissions": {"Options": ["OptionsTrade"]}}
            self.assertIsNone(execution.live_preflight(C()))
            C.has_key = False
            self.assertIn("without", execution.live_preflight(C()))
        with patch.object(config, "TRADING_MODE", "paper"):
            self.assertIsNone(execution.live_preflight(C()))

    def test_make_executor(self):
        with patch.object(config, "TRADING_MODE", "live"):
            self.assertTrue(execution.make_executor(FakeClient()).live)
        with patch.object(config, "TRADING_MODE", "paper"):
            self.assertFalse(execution.make_executor(FakeClient()).live)


if __name__ == "__main__":
    unittest.main()


class TestReviewFixes(Base):
    """Scenarios from the 2026-08-26 review round 1."""

    def _open(self, now=0):
        return execution.submit(
            self.conn, self.ex, kind="open", coin="ETH", side="P", symbol="ETH-TEST-P",
            qty=0.3, price=105.0, urgent=False, reason="bid",
            payload={"strike": 2500, "expiry_ms": 168 * 3_600_000, "spot": 2500,
                     "source": "bid", "margin_est": 100.0, "tp2_pct": 0.7,
                     "sl_pct": 1.75, "hold_h": 120, "ev": {}}, now_ms=now)

    def test_abnormal_book_urgent_close_is_capped(self):
        # pos #65 replay: mark 130, ask 1045 → live urgent close ≤ 136.5, and the
        # chase never climbs past mark×1.25 however long the phantom ask stays
        pid = _pos(self.conn)
        self.fake.positions = {"ETH": {"ETH-TEST-P": {"size": 0.3, "side": "Sell"}}}
        p = repo.get_open_position(self.conn, pid)
        bad = {"bid": 30.0, "ask": 1045.0, "mark": 130.0}
        jony_loop._request_close(self.conn, repo.get_state(self.conn), p, 0,
                                 jony_loop.close_fill_price(bad, cap_slip=True),
                                 "sl", "closed_sl", True, True, bad)
        self.assertAlmostEqual(self.fake.log[0][4], 130.0 * 1.05, places=1)
        for i in range(1, 80):
            self.tick(i * (config.EXEC_URGENT_WAIT_S + 1) * S, quote=bad)
        px = repo.active_orders(self.conn)[0]["price"]
        self.assertLessEqual(px, 130.0 * 1.25 + 0.1)
        self.assertGreater(px, 130.0 * 1.05 * 1.02)

    def test_patient_close_mid_capped_and_open_floor(self):
        bad = {"bid": 30.0, "ask": 1045.0, "mark": 130.0}
        self.assertAlmostEqual(jony_loop._quote_px(bad, "close", False, 1.0), 136.5)
        self.assertAlmostEqual(jony_loop._quote_px(bad, "open", False, 1.0), 130.0 * 1.05)  # mid 537 clamped
        self.assertAlmostEqual(jony_loop._quote_px({"bid": 30.0, "ask": 40.0, "mark": 130.0}, "open", False, 1.0), 130.0 * 0.95)

    def test_partially_filled_canceled_terminates(self):
        oid = self._open()
        link = self.fake.last_link()
        t = (config.EXEC_MID_WAIT_S + 1) * S
        self.tick(t)
        self.fake.fill(link, 0.1, 100.0, full=False)
        t += (config.EXEC_RETREAT_WAIT_S + 1) * S
        self.tick(t)
        self.assertEqual(self.fake.orders[link]["orderStatus"], "PartiallyFilledCanceled")
        self.tick(t + S)
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "partial")
        self.assertEqual(repo.active_orders(self.conn), [])

    def test_cancel_failure_is_retried_until_it_works(self):
        oid = self._open()
        self.fake.fail_cancel = True
        t = (config.EXEC_MID_WAIT_S + 1) * S
        self.tick(t)
        t += (config.EXEC_RETREAT_WAIT_S + 1) * S
        self.tick(t)                                 # cancel fails
        self.assertEqual(repo.get_order(self.conn, oid)["stage"], "cancelled")
        self.tick(t + S)                             # retried
        self.assertEqual(self.fake.log[-1][0], "cancel")
        self.fake.fail_cancel = False
        self.tick(t + 2 * S)                         # succeeds
        self.tick(t + 3 * S)
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "no_fill")

    def test_cancel_keeps_failing_never_abandons(self):
        # r2 F3: a live order must never be dropped while it may rest on the book
        oid = self._open()
        self.fake.fail_cancel = True
        t = (config.EXEC_MID_WAIT_S + config.EXEC_RETREAT_WAIT_S + 2) * S
        self.tick((config.EXEC_MID_WAIT_S + 1) * S)
        for i in range(config.EXEC_MAX_ATTEMPTS + 30):
            self.tick(t + i * S)
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "active")
        self.assertFalse(repo.get_exec_halt(self.conn)[0])
        self.fake.fail_cancel = False
        self.tick(t + 100 * S); self.tick(t + 101 * S)
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "no_fill")

    def test_ghost_placement_is_adopted(self):
        self.fake.ghost_on_place = True
        oid = self._open()
        o = repo.get_order(self.conn, oid)
        self.assertEqual((o["status"], o["order_id"]), ("active", None))
        self.tick(S)                              # adopted by orderLinkId
        o = repo.get_order(self.conn, oid)
        self.assertEqual((o["status"], o["order_id"]), ("active", "ex1"))
        self.fake.fill(self.fake.last_link(), 0.3, 100.0)
        self.tick(2 * S)
        self.assertEqual(len(repo.open_positions(self.conn)), 1)

    def test_null_order_id_row_retired_when_absent(self):
        # crash between insert and place: row with no order_id, exchange never saw it
        oid = repo.insert_order(self.conn, {
            "kind": "open", "pos_id": None, "coin": "ETH", "side": "P",
            "option_symbol": "ETH-TEST-P", "qty": 0.3, "price": 105.0, "stage": "mid",
            "urgent": 0, "order_link_id": "jony-o-ghost-1", "placed_at_ms": 0,
            "status": "active", "reason": "bid", "payload": "{}",
            "created_at_ms": 0, "updated_at_ms": 0})
        for i in range(config.EXEC_MAX_ATTEMPTS + 1):
            self.tick(i * S)
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "error")
        self.assertFalse(repo.get_exec_halt(self.conn)[0])

    def test_outage_during_live_order_never_drops_fill(self):
        # r2 F3: 60 s+ API outage while an order fills → booked when API returns
        oid = self._open()
        link = self.fake.last_link()
        self.fake.fill(link, 0.3, 100.0)
        self.fake.api_down = True
        for i in range(config.EXEC_MAX_ATTEMPTS + 20):
            self.tick(i * S)
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "active")
        self.assertEqual(repo.open_positions(self.conn), [])
        self.fake.api_down = False
        self.tick(999 * S)
        self.assertEqual(len(repo.open_positions(self.conn)), 1)
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "filled")

    def test_patient_deadline_without_quotes(self):
        oid = self._open()
        self.tick((config.EXEC_MID_WAIT_S + 1) * S, quote=None)      # no quote → stays mid
        self.assertEqual(repo.get_order(self.conn, oid)["stage"], "mid")
        self.tick((config.EXEC_DEADLINE_S + 1) * S, quote=None)      # hard deadline cancels
        self.assertEqual(repo.get_order(self.conn, oid)["stage"], "cancelled")
        self.tick((config.EXEC_DEADLINE_S + 2) * S, quote=None)
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "no_fill")

    def test_fill_without_price_waits(self):
        oid = self._open()
        link = self.fake.last_link()
        o = self.fake.orders[link]
        o["cumExecQty"] = "0.3"; o["orderStatus"] = "Filled"; o["avgPrice"] = "0"
        for i in range(config.EXEC_MAX_ATTEMPTS + 5):
            self.tick(S + i * S)              # never abandoned, however long
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "active")
        self.assertEqual(repo.open_positions(self.conn), [])
        self.fake.execs["ex1"] = [{"execQty": "0.2", "execPrice": "100", "execFee": "0.02"},
                                  {"execQty": "0.1", "execPrice": "103", "execFee": "0.01"}]
        self.tick(2 * S)
        pos = repo.open_positions(self.conn)
        self.assertEqual(len(pos), 1)
        self.assertAlmostEqual(pos[0]["entry_credit"], 101.0)
        self.assertAlmostEqual(pos[0]["fee_open_usd"], 0.03)

    def test_callback_exception_keeps_order_active_no_orphan(self):
        oid = self._open()
        self.fake.fill(self.fake.last_link(), 0.3, 100.0)
        with patch.object(jony_loop, "_on_open_fill", side_effect=RuntimeError("boom")):
            for i in range(config.EXEC_MAX_ATTEMPTS + 2):
                self.tick(S + i * S)          # isolated per order, never abandoned
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "active")
        self.assertEqual(repo.open_positions(self.conn), [])
        self.tick(2 * S)                      # next tick books it normally
        self.assertEqual(len(repo.open_positions(self.conn)), 1)
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "filled")

    def test_close_fill_uses_tick_time_and_clears_closing(self):
        pid = _pos(self.conn)
        self.fake.positions = {"ETH": {"ETH-TEST-P": {"size": 0.3, "side": "Sell"}}}
        p = repo.get_open_position(self.conn, pid)
        jony_loop._request_close(self.conn, repo.get_state(self.conn), p, 7_000_000, 110.0,
                                 "sl", "closed_sl", True, True, QUOTE)
        self.fake.fill(self.fake.last_link(), 0.3, 130.0)      # loss → CB arms
        self.tick(7_000_000 + 5 * S)
        row = repo.position_row(self.conn, pid)
        self.assertEqual(row["closed_at_ms"], 7_000_000 + 5 * S)
        self.assertIsNone(row["closing_order_id"])
        cb = json.loads(repo.get_state(self.conn)["cb_until_json"])
        self.assertEqual(cb["ETH:P"], 7_000_000 + 5 * S + config.CB_PAUSE_HOURS * 3_600_000)

    def test_close_fill_for_already_closed_position_is_idempotent(self):
        pid = _pos(self.conn)
        p = repo.get_open_position(self.conn, pid)
        jony_loop._request_close(self.conn, repo.get_state(self.conn), p, 0, 110.0,
                                 "tp2", "closed_tp2", True, False, QUOTE)
        oid = repo.active_orders(self.conn)[0]["id"]
        # simulate: THIS order booked the close earlier, order row still active
        jony_loop._close(self.conn, repo.get_state(self.conn), p, 100, 110.0, "tp2", "closed_tp2",
                         order_id=oid)
        self.fake.fill(self.fake.last_link(), 0.3, 110.0)
        self.tick(200)
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "filled")
        self.assertFalse(repo.get_exec_halt(self.conn)[0])

    def test_close_fill_from_other_order_halts(self):
        # r2 F5: position was closed by a DIFFERENT order → this fill is unattributable
        pid = _pos(self.conn)
        p = repo.get_open_position(self.conn, pid)
        jony_loop._request_close(self.conn, repo.get_state(self.conn), p, 0, 110.0,
                                 "tp2", "closed_tp2", True, False, QUOTE)
        oid = repo.active_orders(self.conn)[0]["id"]
        jony_loop._close(self.conn, repo.get_state(self.conn), p, 100, 110.0, "tp2", "closed_tp2",
                         order_id=oid + 500)
        self.fake.fill(self.fake.last_link(), 0.3, 110.0)
        self.tick(200)
        self.assertEqual(repo.get_order(self.conn, oid)["status"], "filled")
        self.assertTrue(repo.get_exec_halt(self.conn)[0])

    def test_close_attempts_cap_halts_but_manual_bypasses(self):
        pid = _pos(self.conn)
        self.fake.positions = {"ETH": {"ETH-TEST-P": {"size": 0.3, "side": "Sell"}}}
        self.conn.execute("UPDATE positions SET close_attempts=? WHERE id=?",
                          (config.CLOSE_MAX_ATTEMPTS, pid)); self.conn.commit()
        p = repo.get_open_position(self.conn, pid)
        jony_loop._request_close(self.conn, repo.get_state(self.conn), p, 0, 110.0,
                                 "tp2", "closed_tp2", True, False, QUOTE)
        self.assertEqual(self.fake.log, [])
        self.assertTrue(repo.get_exec_halt(self.conn)[0])
        # operator override (r2 F4): manual close still places, urgent
        jony_loop._request_close(self.conn, repo.get_state(self.conn), p, 60_000, 110.0,
                                 "manual_close_one", "closed_manual", False, True, QUOTE)
        self.assertEqual(self.fake.log[-1][0], "place")
        self.assertEqual(repo.active_orders(self.conn)[0]["stage"], "urgent")

    def test_rejected_close_counts_attempts_and_halts(self):
        # r2 F6: placement rejections count; after CLOSE_MAX_ATTEMPTS → halt, no spam
        pid = _pos(self.conn)
        self.fake.positions = {"ETH": {"ETH-TEST-P": {"size": 0.3, "side": "Sell"}}}
        self.fake.fail_place = True
        for i in range(config.CLOSE_MAX_ATTEMPTS + 2):
            p = repo.get_open_position(self.conn, pid)
            jony_loop._request_close(self.conn, repo.get_state(self.conn), p, i * 60_000, 110.0,
                                     "tp2", "closed_tp2", True, False, QUOTE)
        p = repo.get_open_position(self.conn, pid)
        self.assertEqual(p["close_attempts"], config.CLOSE_MAX_ATTEMPTS)
        self.assertIsNone(p["closing_order_id"])
        self.assertTrue(repo.get_exec_halt(self.conn)[0])
        self.assertEqual(len([l for l in self.fake.log if l[0] == "place"]), config.CLOSE_MAX_ATTEMPTS)

    def test_exchange_flat_blocks_reduce_only_close(self):
        # r2 F10: exchange no longer has the position → no reduce-only buy, halt
        pid = _pos(self.conn)
        self.fake.positions = {"ETH": {}, "BTC": {}}
        p = repo.get_open_position(self.conn, pid)
        jony_loop._request_close(self.conn, repo.get_state(self.conn), p, 0, 110.0,
                                 "sl", "closed_sl", True, True, QUOTE)
        self.assertEqual(self.fake.log, [])
        self.assertTrue(repo.get_exec_halt(self.conn)[0])

    def test_reconcile_flags_foreign_resting_order(self):
        self.fake.positions = {"ETH": {}, "BTC": {}}
        self.fake.resting = [{"symbol": "ETH-X-P", "orderLinkId": "manual-ui", "orderId": "z1"}]
        self.assertFalse(execution.reconcile(self.conn, self.ex, [], 0, lambda *_: None))
        self.assertIn("zombie", repo.get_exec_halt(self.conn)[1])

    def test_reconcile_halt_does_not_mask_manual_halt(self):
        repo.set_exec_halt(self.conn, True, "pos 1 X: exchange size < db qty")
        self.fake.positions = {"ETH": {}, "BTC": {}}
        execution.reconcile(self.conn, self.ex, [], 0, lambda *_: None)
        self.assertEqual(repo.get_exec_halt(self.conn)[1], "pos 1 X: exchange size < db qty")

    def test_stale_closing_marker_self_heals_in_manage_exits(self):
        pid = _pos(self.conn, sl_pct=0.5)
        self.fake.positions = {"ETH": {"ETH-TEST-P": {"size": 0.3, "side": "Sell"}}}
        repo.set_closing(self.conn, pid, 4242)      # order row does not exist
        with patch("loop.bybit_client.get_option_marks",
                   return_value={"ETH-TEST-P": {"mark": 200.0, "bid": 195, "ask": 205}}):
            jony_loop.manage_exits(self.conn, repo.get_state(self.conn), 60 * S)
        self.assertEqual(self.fake.log[0][0], "place")   # SL close placed

    def test_reconcile_skips_expired_db_position(self):
        _pos(self.conn, expiry_ms=1000)
        self.fake.positions = {"ETH": {}, "BTC": {}}
        self.assertTrue(execution.reconcile(self.conn, self.ex, repo.open_positions(self.conn),
                                            5000, lambda *_: None))

    def test_inflight_open_counts_for_margin(self):
        # equity 1500, cap 0.8 → 1200 free; in-flight order reserves 1190 → <1 lot left
        execution.submit(self.conn, self.ex, kind="open", coin="BTC", side="P",
                         symbol="BTC-TEST-P", qty=0.01, price=2000.0, urgent=False,
                         reason="bid", payload={"margin_est": 1190.0}, now_ms=0)
        ev = {"active_side": "P", "spot": 2500.0, "ready": True, "dist_7d_high_pct": -5.0}
        pick = {"symbol": "ETH-TEST-P", "strike": 2500.0, "expiry_ms": 168 * 3_600_000,
                "side": "P", "bid": 100.0, "ask": 110.0, "mark_price": 105.0}
        with patch("loop.bybit_client.get_options_tickers", return_value=[pick]), \
                patch("loop.pick_atm_option", return_value=pick):
            jony_loop.try_fire(self.conn, repo.get_state(self.conn), "ETH", ev, 10_000_000)
        last = dict(self.conn.execute(
            "SELECT reject_reason FROM signal_audit ORDER BY id DESC LIMIT 1").fetchone())
        self.assertEqual(last["reject_reason"], "margin_blocked")

    def test_settlement_waits_for_delivery_record(self):
        pid = _pos(self.conn, expiry_ms=1000, strike=2500)
        self.fake.positions = {"ETH": {}, "BTC": {}}
        p = repo.get_open_position(self.conn, pid)
        with patch("loop.bybit_client.get_delivery", return_value=None, create=True), \
                patch("loop.bybit_client.get_klines", return_value=[{"close": 2400}]):
            # 5 min past expiry, no record yet → wait
            self.assertFalse(jony_loop._settle_expired_live(self.conn, repo.get_state(self.conn), p, 1000 + 5 * 60_000, set()))
            # 31 min → intrinsic fallback
            self.assertTrue(jony_loop._settle_expired_live(self.conn, repo.get_state(self.conn), p, 1000 + 31 * 60_000, set()))
        self.assertEqual(repo.position_row(self.conn, pid)["exit_debit"], 100.0)


class TestPaperResilience(unittest.TestCase):
    """r2 F1: a paper close that fails once must be retried, never frozen."""
    def setUp(self):
        self.conn = repo.connect()
        repo.apply_schema(self.conn)
        for tb in ("positions", "bot_state", "orders", "signal_audit", "bot_control"):
            self.conn.execute(f"DELETE FROM {tb}")
        self.conn.commit()
        repo.init_state(self.conn, 800.0, 0)

    def test_close_exception_then_retry_closes(self):
        pid = _pos(self.conn, entry_credit=30.0, qty=0.4, sl_pct=0.75)
        marks = {"ETH-TEST-P": {"mark": 60.0, "bid": 58.0, "ask": 61.0}}
        with patch("loop.bybit_client.get_option_marks", return_value=marks), patch("loop.notify"):
            with patch.object(jony_loop, "_close", side_effect=RuntimeError("db locked")):
                jony_loop.manage_exits(self.conn, repo.get_state(self.conn), 60 * S)
            row = repo.position_row(self.conn, pid)
            self.assertEqual(row["status"], "open")
            self.assertIsNotNone(row["closing_order_id"])
            # next tick: process_orders (both modes) finalizes the stranded paper order
            execution.process_orders(self.conn, execution.PaperExecutor(), 120 * S, lambda c: {},
                                     jony_loop._on_open_fill, jony_loop._on_close_fill, lambda *_: None)
        row = repo.position_row(self.conn, pid)
        self.assertEqual(row["status"], "closed_sl")
        self.assertEqual(row["exit_debit"], 61.0)
        self.assertIsNone(row["closing_order_id"])


class TestPaperParity(unittest.TestCase):
    """Paper mode through manage_exits: same tick timestamps, CB, prices."""
    def setUp(self):
        self.conn = repo.connect()
        repo.apply_schema(self.conn)
        for tb in ("positions", "bot_state", "orders", "signal_audit", "bot_control"):
            self.conn.execute(f"DELETE FROM {tb}")
        self.conn.commit()
        repo.init_state(self.conn, 800.0, 0)

    def test_sl_close_same_tick_prices_and_times(self):
        pid = _pos(self.conn, entry_credit=30.0, qty=0.4, sl_pct=0.75, tp2_pct=0.8)
        now = 5_000_000
        with patch("loop.bybit_client.get_option_marks",
                   return_value={"ETH-TEST-P": {"mark": 60.0, "bid": 58.0, "ask": 61.0}}), \
                patch("loop.notify"):
            jony_loop.manage_exits(self.conn, repo.get_state(self.conn), now)
        row = repo.position_row(self.conn, pid)
        self.assertEqual(row["status"], "closed_sl")
        self.assertEqual(row["exit_debit"], 61.0)            # paper pays the ask
        self.assertEqual(row["closed_at_ms"], now)
        self.assertIsNone(row["closing_order_id"])
        st = repo.get_state(self.conn)
        self.assertEqual(json.loads(st["cb_until_json"])["ETH:P"], now + config.CB_PAUSE_HOURS * 3_600_000)
        o = repo.recent_orders(self.conn, 1)[0]
        self.assertEqual((o["status"], o["price"], o["avg_price"]), ("filled", 61.0, 61.0))
