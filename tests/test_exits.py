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
            tp2=0.80, sl=0.75, symbol="ETH-TEST"):
    pid = repo.insert_position(conn, {
        "coin": "ETH", "side": side, "option_symbol": symbol,
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


class TestPostureExits(unittest.TestCase):
    """Risk-posture layer: peak tracking, tight-mode trailing lock, lockdown."""

    def setUp(self):
        self.conn = repo.connect()
        repo.apply_schema(self.conn)
        self.conn.execute("DELETE FROM positions")
        self.conn.execute("DELETE FROM bot_state")
        self.conn.execute("DELETE FROM signal_audit")
        self.conn.commit()
        repo.init_state(self.conn, 800.0, 0)
        repo.set_risk_posture(self.conn, "normal", 0)

    def _tick(self, marks, now_ms):
        with patch.object(jony_loop.bybit_client, "get_option_marks",
                          return_value=marks), patch("loop.notify"):
            return jony_loop.manage_exits(self.conn,
                                          repo.get_state(self.conn), now_ms)

    def test_peak_tracked_but_no_trail_in_normal(self):
        pid = _mk_pos(self.conn)  # entry 30
        # mark 21 → profit 30% of credit → peak persists
        self._tick({"ETH-TEST": {"mark": 21.0, "ask": 21.0, "bid": 20.0}}, 1000)
        row = dict(self.conn.execute(
            "SELECT status, peak_profit_pct FROM positions WHERE id=?",
            (pid,)).fetchone())
        self.assertEqual(row["status"], "open")
        self.assertAlmostEqual(row["peak_profit_pct"], 0.30, places=6)
        # retrace to profit 15% (giveback 15pp > 10pp) — normal posture: stays
        self._tick({"ETH-TEST": {"mark": 25.5, "ask": 25.5, "bid": 25.0}}, 2000)
        row = dict(self.conn.execute(
            "SELECT status FROM positions WHERE id=?", (pid,)).fetchone())
        self.assertEqual(row["status"], "open")

    def test_tight_trail_locks_profit_without_cb(self):
        pid = _mk_pos(self.conn)
        self._tick({"ETH-TEST": {"mark": 21.0, "ask": 21.0, "bid": 20.0}}, 1000)
        repo.set_risk_posture(self.conn, "tight", 1500)
        self._tick({"ETH-TEST": {"mark": 25.5, "ask": 25.5, "bid": 25.0}}, 2000)
        row = dict(self.conn.execute(
            "SELECT status, exit_reason, pnl_usd FROM positions WHERE id=?",
            (pid,)).fetchone())
        self.assertEqual(row["status"], "closed_trail")
        self.assertEqual(row["exit_reason"], "trail_lock")
        self.assertGreater(row["pnl_usd"], 0)  # locked in profit
        st = repo.get_state(self.conn)
        self.assertEqual(json.loads(st["cb_until_json"]), {})  # no CB arm

    def test_lockdown_harvests_only_profitable(self):
        pid_win = _mk_pos(self.conn, symbol="ETH-WIN")
        pid_loss = _mk_pos(self.conn, symbol="ETH-LOSS")
        repo.set_risk_posture(self.conn, "lockdown", 500)
        # WIN at profit 10% (below trail arm — lockdown must still harvest);
        # LOSS at −10% (must stay open, loss-cutting is not automated)
        self._tick({"ETH-WIN": {"mark": 27.0, "ask": 27.0, "bid": 26.0},
                    "ETH-LOSS": {"mark": 33.0, "ask": 33.0, "bid": 32.0}}, 1000)
        rows = {r["option_symbol"]: dict(r) for r in self.conn.execute(
            "SELECT option_symbol, status, exit_reason FROM positions")}
        self.assertEqual(rows["ETH-WIN"]["status"], "closed_trail")
        self.assertEqual(rows["ETH-WIN"]["exit_reason"], "lockdown_profit_lock")
        self.assertEqual(rows["ETH-LOSS"]["status"], "open")

    def _insert_closed(self, closed_at_ms, pnl_usd, status="closed_sl"):
        pid = repo.insert_position(self.conn, {
            "coin": "ETH", "side": "C", "option_symbol": "ETH-T-OPT",
            "strike": 2500, "expiry_ms": 1, "qty": 0.1, "opened_at_ms": 0,
            "underlying_at_open": 2500, "entry_credit": 30,
            "entry_source": "bid", "margin_usd": 28, "fee_open_usd": 0.1,
            "tp2_pct": 0.5, "sl_pct": 2.0, "hold_h": 48})
        repo.close_position(self.conn, pid, status=status,
                            closed_at_ms=closed_at_ms, exit_debit=40,
                            exit_reason="x", pnl_pct=-0.3, pnl_usd=pnl_usd)

    def test_protective_closes_do_not_feed_breaker(self):
        # closed_trail / closed_manual are harvests & operator decisions —
        # the account breaker must not read them as strategy losses
        now = 5_000_000_000
        for i, status in ((1, "closed_trail"), (2, "closed_manual"),
                          (3, "closed_trail")):
            self._insert_closed(now - i * 3_600_000, -7.0, status)
        ev = {"active_side": "C", "spot": 2500.0, "ready": True}
        with patch.object(jony_loop.bybit_client, "get_options_tickers",
                          return_value=[]), patch("loop.notify"):
            jony_loop.try_fire(self.conn, repo.get_state(self.conn), "ETH",
                               ev, now_ms=now)
        last = dict(self.conn.execute(
            "SELECT reject_reason FROM signal_audit ORDER BY id DESC LIMIT 1"
        ).fetchone())
        self.assertEqual(last["reject_reason"], "no_option_contract")

    def test_acct_breaker_blocks_new_entries(self):
        now = 5_000_000_000
        for i in (1, 2, 3):  # -21$ realized in 24h > 2.5% of 800
            self._insert_closed(now - i * 3_600_000, -7.0)
        ev = {"active_side": "C", "spot": 2500.0, "ready": True}
        with patch("loop.notify"):
            jony_loop.try_fire(self.conn, repo.get_state(self.conn), "ETH",
                               ev, now_ms=now)
        last = dict(self.conn.execute(
            "SELECT reject_reason FROM signal_audit ORDER BY id DESC LIMIT 1"
        ).fetchone())
        self.assertEqual(last["reject_reason"], "acct_cb_daily")

    def test_acct_breaker_floors_exit_posture_to_tight(self):
        # daily breach + posture normal → trail must arm (tight behavior)
        now = 5_000_000_000
        for i in (1, 2, 3):
            self._insert_closed(now - i * 3_600_000, -7.0)
        pid = repo.insert_position(self.conn, {
            "coin": "ETH", "side": "C", "option_symbol": "ETH-OPEN-OPT",
            "strike": 2500, "expiry_ms": now + 48 * 3_600_000, "qty": 0.1,
            "opened_at_ms": now - 3_600_000, "underlying_at_open": 2500,
            "entry_credit": 30, "entry_source": "bid", "margin_usd": 28,
            "fee_open_usd": 0.1, "tp2_pct": 0.5, "sl_pct": 2.0, "hold_h": 48})
        self.conn.execute("UPDATE positions SET peak_profit_pct=0.30 WHERE id=?",
                          (pid,))
        self.conn.commit()
        # peak 0.30 armed; mark 25.5 → pnl 0.15 <= peak-giveback → trail fires
        marks = {"ETH-OPEN-OPT": {"mark": 25.5, "ask": 25.5}}
        self._tick(marks, now)
        row = dict(self.conn.execute(
            "SELECT status FROM positions WHERE id=?", (pid,)).fetchone())
        self.assertEqual(row["status"], "closed_trail")

    def test_lockdown_blocks_new_entries(self):
        # now_ms must clear the 30-min cooldown vs last_fired=0 default
        now = 5_000_000
        repo.set_risk_posture(self.conn, "lockdown", now - 1000)
        ev = {"active_side": "C", "spot": 2500.0, "ready": True}
        with patch("loop.notify"):
            jony_loop.try_fire(self.conn, repo.get_state(self.conn), "ETH",
                               ev, now_ms=now)
        last = dict(self.conn.execute(
            "SELECT reject_reason FROM signal_audit ORDER BY id DESC LIMIT 1"
        ).fetchone())
        self.assertEqual(last["reject_reason"], "lockdown")

    def test_advisor_entry_request_full_path(self):
        # queue an advisor entry; process_entry_requests must route it through
        # try_fire and open a real position tagged source=advisor
        now = 5_000_000
        repo.request_entry(self.conn, "ETH", "C", now, '{"advisor_reason": "x"}')
        fake_chain = [{"symbol": "ETH-TEST-OPT", "side": "C",
                       "expiry_ms": now + 100 * 3_600_000, "strike": 2500.0,
                       "bid": 30.0, "ask": 31.0, "mark_price": 30.5,
                       "underlying_price": 2500.0, "delta": 0.5, "mark_iv": 0.5}]
        with patch("loop.notify"), \
             patch.object(jony_loop.bybit_client, "get_klines",
                          return_value=[{"close": 2500.0}]), \
             patch.object(jony_loop.bybit_client, "get_options_tickers",
                          return_value=fake_chain):
            jony_loop.process_entry_requests(self.conn,
                                             repo.get_state(self.conn), now)
        pos = repo.open_positions(self.conn)
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["option_symbol"], "ETH-TEST-OPT")
        self.assertIn('"source": "advisor"', pos[0]["signal_payload"])
        # queue must be consumed
        self.assertEqual(repo.pop_entry_requests(self.conn), [])

    def test_advisor_entry_blocked_outside_normal_posture(self):
        now = 5_000_000
        repo.set_risk_posture(self.conn, "tight", now - 1000)
        repo.request_entry(self.conn, "ETH", "C", now, "{}")
        with patch("loop.notify"):
            jony_loop.process_entry_requests(self.conn,
                                             repo.get_state(self.conn), now)
        self.assertEqual(repo.open_positions(self.conn), [])
        last = dict(self.conn.execute(
            "SELECT reject_reason FROM signal_audit ORDER BY id DESC LIMIT 1"
        ).fetchone())
        self.assertEqual(last["reject_reason"], "advisor_entry_posture")

    def test_stale_lockdown_degrades_to_tight_for_entries(self):
        # posture set 10h ago → effective tight → entry NOT blocked by lockdown
        repo.set_risk_posture(self.conn, "lockdown", 0)
        ev = {"active_side": "C", "spot": 2500.0, "ready": True}
        now = 10 * 3_600_000
        with patch("loop.notify"), \
             patch.object(jony_loop.bybit_client, "get_options_tickers",
                          return_value=[]):
            jony_loop.try_fire(self.conn, repo.get_state(self.conn), "ETH",
                               ev, now_ms=now)
        last = dict(self.conn.execute(
            "SELECT reject_reason FROM signal_audit ORDER BY id DESC LIMIT 1"
        ).fetchone())
        self.assertNotEqual(last["reject_reason"], "lockdown")


if __name__ == "__main__":
    unittest.main()


class TestPositionMarkLogging(unittest.TestCase):
    """P1 2026-08-17: manage_exits пишет реальную марку 1 раз/мин/позицию."""

    def setUp(self):
        self.conn = repo.connect()
        repo.apply_schema(self.conn)
        for t in ("positions", "bot_state", "position_marks"):
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.commit()
        repo.init_state(self.conn, 800.0, 0)
        jony_loop._mark_logged_min.clear()

    def _marks(self, mark=25.0):
        return {"ETH-TEST": {"mark": mark, "bid": mark - 0.5, "ask": mark + 0.5,
                             "mark_iv": 0.42, "underlying": 2400.0, "delta": -0.2}}

    def test_logs_once_per_minute_with_full_fields(self):
        _mk_pos(self.conn, entry=30.0, hold_h=999, tp2=9.9, sl=9.9)
        state = repo.get_state(self.conn)
        with patch.object(jony_loop.bybit_client, "get_option_marks",
                          return_value=self._marks()):
            jony_loop.manage_exits(self.conn, state, now_ms=60_000)
            jony_loop.manage_exits(self.conn, state, now_ms=65_000)   # та же минута
            jony_loop.manage_exits(self.conn, state, now_ms=125_000)  # новая минута
        rows = self.conn.execute(
            "SELECT ts_ms, mark, mark_iv, underlying, delta, pnl_pct_mark"
            " FROM position_marks ORDER BY ts_ms").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], 60_000)
        self.assertEqual(rows[1][0], 125_000)
        self.assertEqual(rows[0][1], 25.0)
        self.assertEqual(rows[0][2], 0.42)
        self.assertEqual(rows[0][3], 2400.0)
        self.assertEqual(rows[0][4], -0.2)
        self.assertAlmostEqual(rows[0][5], (30.0 - 25.0) / 30.0)

    def test_no_rows_without_open_positions(self):
        state = repo.get_state(self.conn)
        with patch.object(jony_loop.bybit_client, "get_option_marks",
                          return_value={}):
            jony_loop.manage_exits(self.conn, state, now_ms=60_000)
        n = self.conn.execute("SELECT COUNT(*) FROM position_marks").fetchone()[0]
        self.assertEqual(n, 0)
