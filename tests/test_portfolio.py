"""Unit tests: sizing/caps/CB/fees mirror the basket backtest's engine."""
import unittest

from services import config, portfolio
from services.bybit_client import pick_atm_option


class TestSizing(unittest.TestCase):
    def test_eth_sizing_matches_engine(self):
        # equity 800, no open margin, ETH strike 2500 credit 30, lot 0.1:
        # m_per_lot = (0.10*2500 + 30)*0.1 = 28; budget = 800*0.15 = 120 → 4 lots
        qty, margin = portfolio.size_position(800, 0, [], 2500, 30, 0.1)
        self.assertAlmostEqual(qty, 0.4)
        self.assertAlmostEqual(margin, 112.0)

    def test_margin_block_when_budget_below_lot(self):
        # BTC strike 100000 credit 800, lot 0.01 → m_per_lot = (10000+800)*0.01=108
        # budget 800*0.15=120 → 1 lot ok; with dyn 0.5 → 60 < 108 → blocked
        qty, _ = portfolio.size_position(800, 0, [], 100_000, 800, 0.01)
        self.assertAlmostEqual(qty, 0.01)
        losing = [-0.5] * 10
        qty, _ = portfolio.size_position(800, 0, losing, 100_000, 800, 0.01)
        self.assertEqual(qty, 0.0)

    def test_portfolio_margin_cap(self):
        # used margin 600 of 800*0.8=640 → free 40 < one ETH lot (28 ok!)
        qty, margin = portfolio.size_position(800, 600, [], 2500, 30, 0.1)
        self.assertAlmostEqual(qty, 0.1)   # 40 // 28 = 1 lot
        qty, _ = portfolio.size_position(800, 630, [], 2500, 30, 0.1)
        self.assertEqual(qty, 0.0)         # free 10 < 28 → blocked

    def test_caps(self):
        pos = [{"coin": "ETH", "side": "P"}] * 3 + [{"coin": "ETH", "side": "C"}] * 3
        self.assertEqual(portfolio.can_open(pos, "ETH", "P"), "per_coin_cap")
        self.assertIsNone(portfolio.can_open(pos, "BTC", "C"))
        pos10 = ([{"coin": "ETH", "side": "P"}] * 5
                 + [{"coin": "BTC", "side": "C"}] * 5)
        self.assertEqual(portfolio.can_open(pos10, "BTC", "C"),
                         "max_open_positions")

    def test_per_key_cap(self):
        # one open ETH:P blocks a second ETH:P but not ETH:C or BTC:P
        pos = [{"coin": "ETH", "side": "P"}]
        self.assertEqual(portfolio.can_open(pos, "ETH", "P"), "per_key_cap")
        self.assertIsNone(portfolio.can_open(pos, "ETH", "C"))
        self.assertIsNone(portfolio.can_open(pos, "BTC", "P"))

    def test_trail_exit_due(self):
        # defaults: arm 0.20, giveback 0.10
        self.assertFalse(portfolio.trail_exit_due(0.15, 0.05))   # never armed
        self.assertFalse(portfolio.trail_exit_due(0.25, 0.18))   # retrace 0.07
        self.assertTrue(portfolio.trail_exit_due(0.25, 0.15))    # retrace 0.10
        self.assertTrue(portfolio.trail_exit_due(0.40, 0.05))
        # boundary: peak exactly at arm
        self.assertTrue(portfolio.trail_exit_due(0.20, 0.10))

    def test_effective_posture(self):
        h = 3_600_000
        now = 100 * h
        self.assertEqual(portfolio.effective_posture("normal", 0, now), "normal")
        # tight is fail-safe and never degrades on staleness
        self.assertEqual(portfolio.effective_posture("tight", 0, now), "tight")
        # fresh lockdown holds; stale (> LOCKDOWN_STALE_H=4h) degrades to tight
        self.assertEqual(portfolio.effective_posture("lockdown", now - h, now),
                         "lockdown")
        self.assertEqual(portfolio.effective_posture("lockdown", now - 10 * h, now),
                         "tight")
        # unknown value never reaches the loop as-is
        self.assertEqual(portfolio.effective_posture("garbage", now, now), "normal")

    def test_dyn_size(self):
        self.assertEqual(portfolio.dyn_size_factor([0.1] * 10), 1.0)
        self.assertEqual(portfolio.dyn_size_factor([-0.1] * 10), 0.5)
        self.assertEqual(portfolio.dyn_size_factor([-0.1] * 9), 1.0)  # <10 trades

    def test_fee_cap(self):
        # fee = min(notional*3bp, premium*12.5%)
        self.assertAlmostEqual(portfolio.fee_usd(10_000, 100), 3.0)
        self.assertAlmostEqual(portfolio.fee_usd(1_000_000, 10), 1.25)


class TestAccountBreaker(unittest.TestCase):
    H = 3_600_000
    NOW = 1000 * 3_600_000
    EQ = 800.0  # daily limit 2.5% = $20, weekly 5% = $40

    def test_quiet_history_allows(self):
        closed = [(self.NOW - 2 * self.H, +3.0), (self.NOW - 5 * self.H, -2.0)]
        self.assertIsNone(portfolio.account_breaker(closed, self.EQ, self.NOW))

    def test_daily_loss_trips(self):
        closed = [(self.NOW - i * self.H, -7.0) for i in (1, 2, 3)]  # -21 < -20
        self.assertEqual(portfolio.account_breaker(closed, self.EQ, self.NOW),
                         "acct_cb_daily")
        # same losses spread beyond 24h → daily ok (streak also expired)
        spread = [(self.NOW - i * self.H, -7.0) for i in (30, 60, 90)]
        self.assertIsNone(portfolio.account_breaker(spread, self.EQ, self.NOW))

    def test_weekly_loss_trips(self):
        closed = [(self.NOW - i * 24 * self.H, -15.0) for i in (2, 4, 6)]  # -45 < -40
        self.assertEqual(portfolio.account_breaker(closed, self.EQ, self.NOW),
                         "acct_cb_weekly")

    def test_losing_streak_trips_then_expires(self):
        streak = [(self.NOW - 3 * self.H, -1.0), (self.NOW - 2 * self.H, -1.0),
                  (self.NOW - 1 * self.H, -1.0)]
        self.assertEqual(portfolio.account_breaker(streak, self.EQ, self.NOW),
                         "acct_cb_streak")
        # a win inside the last N breaks the streak
        mixed = streak[:-1] + [(self.NOW - self.H, +1.0)]
        self.assertIsNone(portfolio.account_breaker(mixed, self.EQ, self.NOW))
        # last loss older than the block window → released
        old = [(self.NOW - (25 + i) * self.H, -1.0) for i in (0, 1, 2)]
        self.assertIsNone(portfolio.account_breaker(old, self.EQ, self.NOW))

    def test_bad_equity_fails_closed(self):
        self.assertEqual(portfolio.account_breaker([], 0.0, self.NOW),
                         "acct_cb_bad_equity")

    def test_empty_history_allows(self):
        self.assertIsNone(portfolio.account_breaker([], self.EQ, self.NOW))


class TestPickAtm(unittest.TestCase):
    CHAIN = [
        {"symbol": "E-1", "side": "C", "strike": 2500, "expiry_ms": 170 * 3_600_000},
        {"symbol": "E-2", "side": "C", "strike": 2600, "expiry_ms": 170 * 3_600_000},
        {"symbol": "E-3", "side": "C", "strike": 2500, "expiry_ms": 30 * 3_600_000},
        {"symbol": "E-4", "side": "C", "strike": 2500, "expiry_ms": 2 * 3_600_000},
        {"symbol": "E-5", "side": "P", "strike": 2500, "expiry_ms": 170 * 3_600_000},
    ]

    def test_picks_weekly_atm(self):
        pick = pick_atm_option(self.CHAIN, spot=2510, side="C",
                               target_expiry_h=168, min_expiry_h=6, now_ms=0)
        self.assertEqual(pick["symbol"], "E-1")

    def test_min_expiry_excludes_dying(self):
        pick = pick_atm_option(self.CHAIN, spot=2510, side="C",
                               target_expiry_h=1, min_expiry_h=6, now_ms=0)
        self.assertEqual(pick["symbol"], "E-3")

    def test_side_filter(self):
        pick = pick_atm_option(self.CHAIN, spot=2510, side="P",
                               target_expiry_h=168, min_expiry_h=6, now_ms=0)
        self.assertEqual(pick["symbol"], "E-5")


class TestConfigLocked(unittest.TestCase):
    def test_account_engine_constants(self):
        self.assertEqual(config.MAX_OPEN_POSITIONS, 10)
        self.assertEqual(config.PER_COIN_CAP, 6)
        self.assertEqual(config.MARGIN_PCT_PER_TRADE, 0.15)
        self.assertEqual(config.CB_PAUSE_HOURS, 8)
        self.assertEqual(config.COOLDOWN_BARS, 6)
        self.assertEqual(config.FLICKER_TOLERANCE, 1)

    def test_cb_pause_dominates_cooldown(self):
        # loop.py::try_fire checks CB before consuming cooldown (see comment
        # there) — safe only as long as a CB pause always outlasts a
        # cooldown window, so cooldown against a stale last_fired[key] can
        # never be the binding constraint right after CB clears. If this
        # ever fails, try_fire's CB/cooldown ordering needs revisiting.
        cooldown_ms = config.COOLDOWN_BARS * 300_000
        cb_pause_ms = config.CB_PAUSE_HOURS * 3_600_000
        self.assertGreater(cb_pause_ms, cooldown_ms)


if __name__ == "__main__":
    unittest.main()
