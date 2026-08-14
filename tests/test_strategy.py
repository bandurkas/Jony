"""Unit tests: gate mechanics + the backtest-locked exit/entry constants."""
import unittest

from core.strategy import (
    CALL_EXIT, CALL_GEN, COIN_SIDES, PUT_EXIT, PUT_GEN,
    allowed_sides, compute_ret_7d, window_fail_step,
)


class TestBacktestLockedParams(unittest.TestCase):
    """If any of these fail, someone tuned constants without a backtest."""

    def test_exits_match_backtest(self):
        self.assertEqual(PUT_EXIT, {"tp2_pct": 0.70, "sl_pct": 1.75, "hold_h": 120})
        self.assertEqual(CALL_EXIT, {"tp2_pct": 0.70, "sl_pct": 0.75, "hold_h": 24})

    def test_gates_match_backtest(self):
        self.assertEqual(PUT_GEN["vol_threshold"], 0.50)
        self.assertEqual(PUT_GEN["regime_filter"], ("range", "transition"))
        self.assertIsNone(PUT_GEN["mtf_anchor_tf"])
        self.assertEqual(CALL_GEN["vol_threshold"], 0.45)
        self.assertEqual(CALL_GEN["regime_filter"], ("range", "transition", "trend"))
        self.assertEqual(CALL_GEN["mtf_anchor_tf"], "1h")
        self.assertEqual(CALL_GEN["bull_market_ratio_max"], 1.05)

    def test_btc_put_enabled(self):
        # BTC:P re-enabled 2026-08-14 (honest v2 verdict reversed the old
        # clairvoyant-harness rejection — RESEARCH_LOG_2026-08-14.md Phase 6)
        self.assertEqual(COIN_SIDES["BTC"], ("C", "P"))
        self.assertEqual(allowed_sides("BTC", ret_7d=2.0), ["P"])
        self.assertEqual(allowed_sides("BTC", ret_7d=-2.0), ["C"])
        self.assertEqual(allowed_sides("BTC", ret_7d=0.0), ["P", "C"])

    def test_eth_v2_zones(self):
        self.assertEqual(allowed_sides("ETH", 2.0), ["P"])
        self.assertEqual(allowed_sides("ETH", -2.0), ["C"])
        self.assertEqual(allowed_sides("ETH", 0.0), ["P", "C"])

    def test_disabled_keys_filter(self):
        import core.strategy as st
        old = st.DISABLED_KEYS
        try:
            st.DISABLED_KEYS = frozenset({"ETH:C", "BTC:P"})
            self.assertEqual(st.allowed_sides("ETH", 0.0), ["P"])
            self.assertEqual(st.allowed_sides("ETH", -2.0), [])
            self.assertEqual(st.allowed_sides("BTC", 2.0), [])
            self.assertEqual(st.allowed_sides("BTC", 0.0), ["C"])
        finally:
            st.DISABLED_KEYS = old


class TestWindowDebounce(unittest.TestCase):
    def test_tol1(self):
        # 1 failure tolerated
        fails, disq = window_fail_step(0, minute_ready=False)
        self.assertEqual((fails, disq), (1, False))
        # 2nd failure disqualifies
        fails, disq = window_fail_step(fails, minute_ready=False)
        self.assertEqual((fails, disq), (2, True))
        # ready minutes never add failures
        fails, disq = window_fail_step(0, minute_ready=True)
        self.assertEqual((fails, disq), (0, False))


class TestRet7d(unittest.TestCase):
    def test_ret(self):
        k5 = [{"close": 100.0}] * 2016 + [{"close": 103.0}]
        self.assertAlmostEqual(compute_ret_7d(k5, 2016), 3.0)
        self.assertEqual(compute_ret_7d(k5, 100), 0.0)  # not enough history


if __name__ == "__main__":
    unittest.main()
