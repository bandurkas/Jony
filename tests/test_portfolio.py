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
        pos = [{"coin": "ETH"}] * 3
        self.assertEqual(portfolio.can_open(pos, "ETH"), "per_coin_cap")
        self.assertIsNone(portfolio.can_open(pos, "BTC"))
        pos4 = [{"coin": "ETH"}] * 2 + [{"coin": "BTC"}] * 2
        self.assertEqual(portfolio.can_open(pos4, "BTC"), "max_open_positions")

    def test_dyn_size(self):
        self.assertEqual(portfolio.dyn_size_factor([0.1] * 10), 1.0)
        self.assertEqual(portfolio.dyn_size_factor([-0.1] * 10), 0.5)
        self.assertEqual(portfolio.dyn_size_factor([-0.1] * 9), 1.0)  # <10 trades

    def test_fee_cap(self):
        # fee = min(notional*3bp, premium*12.5%)
        self.assertAlmostEqual(portfolio.fee_usd(10_000, 100), 3.0)
        self.assertAlmostEqual(portfolio.fee_usd(1_000_000, 10), 1.25)


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
        self.assertEqual(config.MAX_OPEN_POSITIONS, 4)
        self.assertEqual(config.PER_COIN_CAP, 3)
        self.assertEqual(config.MARGIN_PCT_PER_TRADE, 0.15)
        self.assertEqual(config.CB_CONSEC_LIMIT, 1)
        self.assertEqual(config.CB_PAUSE_HOURS, 8)
        self.assertEqual(config.COOLDOWN_BARS, 6)
        self.assertEqual(config.FLICKER_TOLERANCE, 1)


if __name__ == "__main__":
    unittest.main()
