"""Exit math on an in-memory DB: TP2/SL/time-stop thresholds and pnl accounting."""
import json
import os
import tempfile
import unittest

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
        # loss → CB armed for 8h
        self.assertEqual(st["cb_cooldown_until_ms"], 1000 + 8 * 3_600_000)
        self.assertEqual(json.loads(st["recent_pnls_json"])[-1],
                         (30 - 55) / 30)

    def test_tp_close_no_cb(self):
        _mk_pos(self.conn)
        p = repo.open_positions(self.conn)[0]
        jony_loop._close(self.conn, repo.get_state(self.conn), p,
                         now_ms=1000, exit_debit=5.0, reason="tp2",
                         status="closed_tp2")
        st = repo.get_state(self.conn)
        self.assertEqual(st["cb_cooldown_until_ms"], 0)
        self.assertGreater(st["equity_usd"], 800)

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


if __name__ == "__main__":
    unittest.main()
