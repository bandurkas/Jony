"""Exit math on an in-memory DB: TP2/SL/time-stop thresholds and pnl accounting."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["JONY_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "jony_test.db")

import loop as jony_loop  # noqa: E402  (env must be set before import)
from db import repo  # noqa: E402


def _mk_pos(conn, side="C", entry=30.0, qty=0.4, opened_at=0, hold_h=24,
            tp2=0.80, sl=0.75):
    pid = repo.insert_position(conn, {
        "coin": "ETH", "side": side, "option_symbol": "ETH-TEST",
        "strike": 2500, "expiry_ms": 168 * 3_600_000, "qty": qty,
        "opened_at_ms": opened_at, "underlying_at_open": 2500,
        "entry_credit": entry, "entry_source": "bid",
        "margin_usd": 112.0, "fee_open_usd": 0.3,
        "tp2_pct": tp2, "sl_pct": sl, "hold_h": hold_h,
        "signal_payload": None,
    })
    return pid


class TestExitMath(unittest.TestCase):
    def setUp(self):
        self.conn = repo.connect()
        repo.apply_schema(self.conn)
        self.conn.execute("DELETE FROM positions")
        self.conn.execute("DELETE FROM bot_state")
        self.conn.commit()
        repo.init_state(self.conn, 800.0, 0)

    def test_close_pnl_and_cb(self):
        pid = _mk_pos(self.conn)
        p = repo.open_positions(self.conn)[0]
        # SL close: exit debit 55 → pnl_pct = (30-55)/30 = -83.3%
        jony_loop._close(self.conn, repo.get_state(self.conn), p,
                         now_ms=1000, exit_debit=55.0, reason="sl",
                         status="closed_sl")
        row = dict(self.conn.execute(
            "SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        self.assertEqual(row["status"], "closed_sl")
        self.assertAlmostEqual(row["pnl_pct"], -83.33, places=1)
        # pnl_usd = (30-55)*0.4 - 0.3 - fee_close; fee_close = min(2500*0.4*3e-4, 55*0.4*0.125)=0.3
        self.assertAlmostEqual(row["pnl_usd"], -10.6, places=1)
        st = repo.get_state(self.conn)
        self.assertAlmostEqual(st["equity_usd"], 800 - 10.6, places=1)
        # loss → CB armed for 8h, on the (coin,side) key only
        cb = json.loads(st["cb_until_json"])
        self.assertEqual(cb["ETH:C"], 1000 + 8 * 3_600_000)
        self.assertEqual(json.loads(st["recent_pnls_json"])[-1],
                         (30 - 55) / 30)

    def test_tp_close_no_cb(self):
        _mk_pos(self.conn)
        p = repo.open_positions(self.conn)[0]
        jony_loop._close(self.conn, repo.get_state(self.conn), p,
                         now_ms=1000, exit_debit=5.0, reason="tp2",
                         status="closed_tp2")
        st = repo.get_state(self.conn)
        self.assertEqual(json.loads(st["cb_until_json"]), {})
        self.assertGreater(st["equity_usd"], 800)

    def test_cb_isolated_per_coin_side(self):
        # a loss on ETH:C must not arm CB on ETH:P
        _mk_pos(self.conn, side="C")
        p = repo.open_positions(self.conn)[0]
        jony_loop._close(self.conn, repo.get_state(self.conn), p,
                         now_ms=1000, exit_debit=55.0, reason="sl",
                         status="closed_sl")
        st = repo.get_state(self.conn)
        cb = json.loads(st["cb_until_json"])
        self.assertIn("ETH:C", cb)
        self.assertNotIn("ETH:P", cb)
        from services import portfolio
        self.assertTrue(portfolio.cb_active(cb.get("ETH:C", 0), 1000))
        self.assertFalse(portfolio.cb_active(cb.get("ETH:P", 0), 1000))

    def test_close_and_state_update_are_one_transaction(self):
        # close_position(commit=False) + update_state(commit=False) must not
        # be visible to another connection until the caller's single
        # conn.commit() — this is what makes _close() atomic (a crash
        # between the two writes must never leave positions.status=closed_*
        # without equity/CB also having absorbed the pnl).
        pid = _mk_pos(self.conn)
        p = repo.open_positions(self.conn)[0]
        other = repo.connect()
        try:
            repo.close_position(self.conn, pid, status="closed_sl",
                                closed_at_ms=1000, exit_debit=55.0,
                                exit_reason="sl", pnl_pct=-83.33,
                                pnl_usd=-10.6, commit=False)
            repo.update_state(self.conn, equity_usd=789.4, commit=False)
            # uncommitted: a second connection must still see the old state
            other_row = dict(other.execute(
                "SELECT status FROM positions WHERE id=?", (pid,)).fetchone())
            self.assertEqual(other_row["status"], "open")
            self.assertEqual(repo.get_state(other)["equity_usd"], 800.0)

            self.conn.commit()
            other_row = dict(other.execute(
                "SELECT status FROM positions WHERE id=?", (pid,)).fetchone())
            self.assertEqual(other_row["status"], "closed_sl")
            self.assertEqual(repo.get_state(other)["equity_usd"], 789.4)
        finally:
            other.close()

    def test_close_rolls_back_on_exception_between_writes(self):
        # An in-process exception between close_position and update_state
        # (both commit=False) must not leave the close_position write
        # pending on `conn` for some later unrelated commit() to silently
        # flush — _close() must roll back and re-raise.
        pid = _mk_pos(self.conn)
        p = repo.open_positions(self.conn)[0]
        with patch.object(repo, "update_state", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                jony_loop._close(self.conn, repo.get_state(self.conn), p,
                                 now_ms=1000, exit_debit=55.0, reason="sl",
                                 status="closed_sl")
        # rolled back: position must still read as open, not closed_sl.
        row = dict(self.conn.execute(
            "SELECT status FROM positions WHERE id=?", (pid,)).fetchone())
        self.assertEqual(row["status"], "open")
        # and a later, totally unrelated commit must not resurrect the
        # rolled-back write.
        repo.update_state(self.conn, equity_usd=750.0)
        row = dict(self.conn.execute(
            "SELECT status FROM positions WHERE id=?", (pid,)).fetchone())
        self.assertEqual(row["status"], "open")

    def test_thresholds(self):
        # mark-based trigger levels for a Call entry=30:
        # TP2 at pnl>=0.80 → mark<=6; SL at pnl<=-0.75 → mark>=52.5
        entry, tp2, sl = 30.0, 0.80, 0.75
        pnl = lambda mark: (entry - mark) / entry
        self.assertGreaterEqual(pnl(6.0), tp2)
        self.assertLess(pnl(6.1), tp2)
        self.assertLessEqual(pnl(52.5), -sl)
        self.assertGreater(pnl(52.4), -sl)
        # Put SL=2.00 → mark>=90 (3x entry)
        self.assertLessEqual(pnl(90.0), -2.00)



class TestControl(unittest.TestCase):
    def setUp(self):
        self.conn = repo.connect()
        repo.apply_schema(self.conn)
        self.conn.execute("DELETE FROM positions")
        self.conn.execute("DELETE FROM bot_state")
        self.conn.execute("DELETE FROM bot_control")
        self.conn.commit()
        repo.init_state(self.conn, 800.0, 0)

    def test_pause_resume(self):
        self.assertFalse(repo.is_paused(self.conn))
        repo.set_paused(self.conn, True)
        self.assertTrue(repo.is_paused(self.conn))
        repo.set_paused(self.conn, False)
        self.assertFalse(repo.is_paused(self.conn))

    def test_close_all_flag_pauses_and_pops_once(self):
        repo.request_close_all(self.conn)
        self.assertTrue(repo.is_paused(self.conn))
        self.assertTrue(repo.pop_close_all(self.conn))
        self.assertFalse(repo.pop_close_all(self.conn))  # reset after pop

    def test_manual_close_does_not_arm_cb(self):
        _mk_pos(self.conn)
        p = repo.open_positions(self.conn)[0]
        jony_loop._close(self.conn, repo.get_state(self.conn), p,
                         now_ms=1000, exit_debit=55.0, reason="manual_close_all",
                         status="closed_manual", arm_cb=False)
        st = repo.get_state(self.conn)
        self.assertEqual(json.loads(st["cb_until_json"]), {})  # loss, but no CB

    def test_close_position_request_queues_and_pops_once(self):
        pid = _mk_pos(self.conn)
        repo.request_close_position(self.conn, pid, now_ms=500)
        # duplicate request for the same id is a no-op (INSERT OR IGNORE) —
        # a double-click on the dashboard button must not queue two closes
        repo.request_close_position(self.conn, pid, now_ms=600)
        self.assertEqual(repo.pop_close_requests(self.conn), [pid])
        self.assertEqual(repo.pop_close_requests(self.conn), [])  # reset after pop
        # does NOT pause the bot, unlike request_close_all
        self.assertFalse(repo.is_paused(self.conn))

    def test_close_position_now_closes_one_leaves_others_open(self):
        pid_a = _mk_pos(self.conn, side="C")
        pid_b = _mk_pos(self.conn, side="P")
        state = repo.get_state(self.conn)
        marks = {"ETH-TEST": {"mark": 5.0, "bid": 4.9, "ask": 5.1}}
        with patch.object(jony_loop.bybit_client, "get_option_marks", return_value=marks):
            jony_loop.close_position_now(self.conn, state, pid_a, now_ms=1000)
        row_a = dict(self.conn.execute(
            "SELECT status, exit_reason FROM positions WHERE id=?", (pid_a,)).fetchone())
        row_b = dict(self.conn.execute(
            "SELECT status FROM positions WHERE id=?", (pid_b,)).fetchone())
        self.assertEqual(row_a["status"], "closed_manual")
        self.assertEqual(row_a["exit_reason"], "manual_close_one")
        self.assertEqual(row_b["status"], "open")  # untouched
        st = repo.get_state(self.conn)
        self.assertEqual(json.loads(st["cb_until_json"]), {})  # no CB from a manual close

    def test_close_position_now_is_noop_if_already_closed(self):
        # the position could resolve via TP2/SL/expiry between the API
        # queuing the request and the loop picking it up next tick — that
        # race must be a harmless no-op, not an error.
        pid = _mk_pos(self.conn)
        state = repo.get_state(self.conn)
        jony_loop._close(self.conn, state, repo.open_positions(self.conn)[0],
                         now_ms=900, exit_debit=5.0, reason="tp2", status="closed_tp2")
        state = repo.get_state(self.conn)
        with patch.object(jony_loop.bybit_client, "get_option_marks") as mock_marks:
            result = jony_loop.close_position_now(self.conn, state, pid, now_ms=1000)
            mock_marks.assert_not_called()
        self.assertEqual(result, state)

    def test_close_position_now_skips_without_quote_and_stays_open(self):
        pid = _mk_pos(self.conn)
        state = repo.get_state(self.conn)
        with patch.object(jony_loop.bybit_client, "get_option_marks", return_value={}):
            jony_loop.close_position_now(self.conn, state, pid, now_ms=1000)
        row = dict(self.conn.execute(
            "SELECT status FROM positions WHERE id=?", (pid,)).fetchone())
        self.assertEqual(row["status"], "open")


class TestStuckSettlement(unittest.TestCase):
    def setUp(self):
        self.conn = repo.connect()
        repo.apply_schema(self.conn)
        self.conn.execute("DELETE FROM positions")
        self.conn.execute("DELETE FROM bot_state")
        self.conn.commit()
        repo.init_state(self.conn, 800.0, 0)

    def test_alerts_once_past_threshold_and_keeps_retrying(self):
        expiry_ms = 1_000_000
        pid = _mk_pos(self.conn, opened_at=0)
        self.conn.execute("UPDATE positions SET expiry_ms=? WHERE id=?",
                          (expiry_ms, pid))
        self.conn.commit()
        state = repo.get_state(self.conn)
        stuck_alerted = set()
        # Bybit totally down: no marks, no klines either.
        with patch.object(jony_loop.bybit_client, "get_option_marks", return_value={}), \
             patch.object(jony_loop.bybit_client, "get_klines", return_value=[]), \
             patch("loop.notify") as mock_notify:
            # 5 min past expiry — below the 15min alert threshold, no alert yet.
            jony_loop.manage_exits(self.conn, state, expiry_ms + 5 * 60_000, stuck_alerted)
            self.assertEqual(stuck_alerted, set())
            mock_notify.assert_not_called()

            # 20 min past expiry — over threshold, alerts once.
            jony_loop.manage_exits(self.conn, state, expiry_ms + 20 * 60_000, stuck_alerted)
            self.assertEqual(stuck_alerted, {pid})
            mock_notify.assert_called_once()
            self.assertIn("STUCK", mock_notify.call_args[0][0])

            # Next tick, still stuck: retried (still open), but not re-alerted.
            jony_loop.manage_exits(self.conn, state, expiry_ms + 21 * 60_000, stuck_alerted)
            mock_notify.assert_called_once()
            row = dict(self.conn.execute(
                "SELECT status FROM positions WHERE id=?", (pid,)).fetchone())
            self.assertEqual(row["status"], "open")

    def test_manual_close_all_clears_stuck_alert_on_next_tick(self):
        # close_all_now doesn't touch stuck_alerted at all (it can't — it's
        # a different function/call path) — verify the generic prune in
        # manage_exits picks it up on the very next tick once the position
        # is simply no longer open, even with Bybit still fully down.
        expiry_ms = 1_000_000
        pid = _mk_pos(self.conn, opened_at=0)
        self.conn.execute("UPDATE positions SET expiry_ms=? WHERE id=?",
                          (expiry_ms, pid))
        self.conn.commit()
        state = repo.get_state(self.conn)
        stuck_alerted = {pid}  # already alerted from a prior outage tick

        marks = {"ETH-TEST": {"mark": 5.0, "bid": 4.9, "ask": 5.1}}
        with patch.object(jony_loop.bybit_client, "get_option_marks", return_value=marks), \
             patch("loop.notify"):
            # operator hits "Close All" (Mission Control) — resolves via a
            # totally different code path than manage_exits' settlement branch.
            jony_loop.close_all_now(self.conn, state, expiry_ms + 20 * 60_000)
            state = repo.get_state(self.conn)
            # next tick: position is gone (no open positions at all now) —
            # manage_exits' early-return path must also clear stuck_alerted,
            # not just the per-position generic prune at the end of the loop.
            jony_loop.manage_exits(self.conn, state, expiry_ms + 21 * 60_000,
                                   stuck_alerted)
        self.assertEqual(stuck_alerted, set())

    def test_settles_and_clears_alert_once_bybit_recovers(self):
        expiry_ms = 1_000_000
        pid = _mk_pos(self.conn, side="C", opened_at=0)
        self.conn.execute("UPDATE positions SET expiry_ms=?, strike=2500 WHERE id=?",
                          (expiry_ms, pid))
        self.conn.commit()
        state = repo.get_state(self.conn)
        stuck_alerted = {pid}  # already alerted from a prior outage tick
        with patch.object(jony_loop.bybit_client, "get_option_marks", return_value={}), \
             patch.object(jony_loop.bybit_client, "get_klines",
                          return_value=[{"close": 2600.0}]), \
             patch("loop.notify"):
            jony_loop.manage_exits(self.conn, state, expiry_ms + 20 * 60_000, stuck_alerted)
        row = dict(self.conn.execute(
            "SELECT status FROM positions WHERE id=?", (pid,)).fetchone())
        self.assertEqual(row["status"], "closed_time")
        self.assertEqual(stuck_alerted, set())  # cleared once it actually settled


if __name__ == "__main__":
    unittest.main()
