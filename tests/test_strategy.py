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
        # config C, 2026-08-17 (RESEARCH_LOG Phase 7): per-coin PUT gates
        from core.strategy import PUT_GEN_BY_COIN, RET_7D_THRESHOLD
        self.assertEqual(RET_7D_THRESHOLD, 1.0)
        self.assertEqual(PUT_GEN_BY_COIN["ETH"]["vol_threshold"], 0.60)
        self.assertEqual(PUT_GEN_BY_COIN["ETH"]["regime_filter"], ("range",))
        self.assertEqual(PUT_GEN_BY_COIN["BTC"]["vol_threshold"], 0.40)
        self.assertEqual(PUT_GEN_BY_COIN["BTC"]["regime_filter"], ("range", "transition"))
        self.assertIs(PUT_GEN, PUT_GEN_BY_COIN["ETH"])  # legacy alias
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


class TestConfigC(unittest.TestCase):
    """Config C (2026-08-17): per-coin PUT gates + no_side_allowed flag."""

    def test_gen_kwargs_per_coin(self):
        from core.strategy import gen_kwargs
        self.assertEqual(gen_kwargs("P", "ETH")["vol_threshold"], 0.60)
        self.assertEqual(gen_kwargs("P", "BTC")["vol_threshold"], 0.40)
        self.assertEqual(gen_kwargs("C", "ETH"), CALL_GEN)
        self.assertEqual(gen_kwargs("C", "BTC"), CALL_GEN)

    def test_ret7d_boundary_widened(self):
        # порог 0.5 -> 1.0: P теперь допустим при ret_7d = -0.9
        self.assertIn("P", allowed_sides("ETH", -0.9))
        self.assertNotIn("P", allowed_sides("ETH", -1.1))

    def test_no_side_allowed_flag(self):
        import core.strategy as st
        old = st.DISABLED_KEYS
        try:
            st.DISABLED_KEYS = frozenset({"ETH:C", "BTC:C"})
            bar = {"open": 100.0, "high": 100.0, "low": 100.0,
                   "close": 100.0, "volume": 1.0}
            bars5 = [dict(bar)] * (st.BARS_7D + 1)
            k5 = list(bars5)
            k5[-1] = {"open": 90.0, "high": 90.0, "low": 90.0,
                      "close": 90.0, "volume": 1.0}  # ret_7d=-10% -> C-зона -> пусто
            ev = st.evaluate_conditions("ETH", k5, [dict(bar)] * 60,
                                        [dict(bar)] * 250)
            self.assertTrue(ev["no_side_allowed"])
            self.assertIsNone(ev["active_side"])
            ev2 = st.evaluate_conditions("ETH", bars5, [dict(bar)] * 60,
                                         [dict(bar)] * 250)
            self.assertFalse(ev2["no_side_allowed"])  # ret 0 -> P допустим
        finally:
            st.DISABLED_KEYS = old


class TestAdvisorOnlyKeys(unittest.TestCase):
    """2026-08-22: коллы — advisor-only: механика не торгует, советник может."""

    def test_mechanics_exclude_advisor_only_keys(self):
        import core.strategy as st
        import advisor
        old_d, old_a, old_adv0 = st.DISABLED_KEYS, st.ADVISOR_ONLY_KEYS, advisor.DISABLED_KEYS
        try:
            st.DISABLED_KEYS = frozenset(); advisor.DISABLED_KEYS = frozenset()
            st.ADVISOR_ONLY_KEYS = frozenset({"ETH:C", "BTC:C"})
            self.assertNotIn("C", st.allowed_sides("ETH", -5.0))   # C-зона, но advisor-only
            self.assertEqual(st.allowed_sides("BTC", 0.0), ["P"])
            # советник видит ключ как доступный (DISABLED_KEYS пуст)
            self.assertIn("ETH:C", advisor.enabled_free_keys([]))
            old_adv = advisor.DISABLED_KEYS
            advisor.DISABLED_KEYS = frozenset({"ETH:C"})  # advisor импортирует по имени
            try:
                self.assertNotIn("ETH:C", advisor.enabled_free_keys([]))  # kill-switch сильнее
            finally:
                advisor.DISABLED_KEYS = old_adv
        finally:
            st.DISABLED_KEYS, st.ADVISOR_ONLY_KEYS, advisor.DISABLED_KEYS = old_d, old_a, old_adv0
