"""Advisor execution policy: decide_executions guardrails (pure function)."""
import unittest

import advisor


def _advice(actions: dict[int, str], posture: str = "tight") -> dict:
    return {"risk_posture": posture,
            "positions": [{"id": i, "action": a, "reason": "t"}
                          for i, a in actions.items()]}


def _prev(actions: dict[int, str]) -> dict:
    return {"positions": [{"id": i, "action": a, "reason": "t"}
                          for i, a in actions.items()]}


POS = {1: {"id": 1, "unrealized_usd": 5.0},
       2: {"id": 2, "unrealized_usd": -3.0},
       3: {"id": 3, "unrealized_usd": 0.8}}


class TestDecideExecutions(unittest.TestCase):
    def test_off_mode_never_executes(self):
        out = advisor.decide_executions(
            _advice({1: "CLOSE"}, "lockdown"), POS, _prev({1: "CLOSE"}),
            "off", [], 0)
        self.assertEqual(out, [])

    def test_persistence_required(self):
        # first-ever CLOSE (no prev) does not execute
        self.assertEqual(advisor.decide_executions(
            _advice({1: "CLOSE"}), POS, None, "profit_only", [], 0), [])
        # prev said WATCH → still not enough
        self.assertEqual(advisor.decide_executions(
            _advice({1: "CLOSE"}), POS, _prev({1: "WATCH"}),
            "profit_only", [], 0), [])
        # two consecutive CLOSE → executes
        self.assertEqual(advisor.decide_executions(
            _advice({1: "CLOSE"}), POS, _prev({1: "CLOSE"}),
            "profit_only", [], 0), [1])

    def test_lockdown_is_urgent_no_persistence_needed(self):
        self.assertEqual(advisor.decide_executions(
            _advice({1: "CLOSE"}, "lockdown"), POS, None,
            "profit_only", [], 0), [1])

    def test_profit_only_skips_losing(self):
        out = advisor.decide_executions(
            _advice({1: "CLOSE", 2: "CLOSE"}, "lockdown"), POS, None,
            "profit_only", [], 0)
        self.assertEqual(out, [1])

    def test_full_mode_allows_losing(self):
        out = advisor.decide_executions(
            _advice({2: "CLOSE"}, "lockdown"), POS, None, "full", [], 0)
        self.assertEqual(out, [2])

    def test_rate_limit(self):
        now = 10_000_000
        recent = [now - 60_000] * advisor.MAX_EXEC_PER_HOUR  # budget used up
        self.assertEqual(advisor.decide_executions(
            _advice({1: "CLOSE"}, "lockdown"), POS, None,
            "profit_only", recent, now), [])
        old = [now - 2 * 3_600_000] * 10  # outside the rolling hour
        self.assertEqual(advisor.decide_executions(
            _advice({1: "CLOSE"}, "lockdown"), POS, None,
            "profit_only", old, now), [1])

    def test_hold_and_watch_never_execute(self):
        self.assertEqual(advisor.decide_executions(
            _advice({1: "HOLD", 3: "WATCH"}, "lockdown"), POS, None,
            "profit_only", [], 0), [])

    def test_unknown_position_skipped(self):
        # advice references a position that closed between snapshot and now
        self.assertEqual(advisor.decide_executions(
            _advice({99: "CLOSE"}, "lockdown"), POS, None,
            "profit_only", [], 0), [])


class TestStrikeRisk(unittest.TestCase):
    """Reversal-vs-expiry math: buffer measured in remaining expected move."""

    def test_atm_is_maximal_risk(self):
        r = advisor.strike_risk("P", 100.0, 100.0, 0.5, 7 / 365)
        self.assertEqual(r["prob_touch_pct"], 100.0)
        self.assertEqual(r["prob_itm_pct"], 50.0)
        self.assertEqual(r["z_buffer"], 0.0)

    def test_itm_short_put_reports_touch_certain(self):
        r = advisor.strike_risk("P", 95.0, 100.0, 0.5, 7 / 365)
        self.assertLess(r["strike_buffer_pct"], 0)
        self.assertEqual(r["prob_touch_pct"], 100.0)
        self.assertGreater(r["prob_itm_pct"], 50.0)

    def test_far_otm_near_expiry_is_safe(self):
        # 10% buffer, 50% IV, 6 hours left: sigma_t ~ 0.5*sqrt(6/8760) ~ 1.3%
        r = advisor.strike_risk("P", 100.0, 90.0, 0.5, 6 / 8760)
        self.assertGreater(r["z_buffer"], 2)
        self.assertLess(r["prob_touch_pct"], 5)

    def test_same_buffer_more_time_is_riskier(self):
        near = advisor.strike_risk("P", 100.0, 95.0, 0.5, 1 / 365)
        far = advisor.strike_risk("P", 100.0, 95.0, 0.5, 30 / 365)
        self.assertGreater(far["prob_touch_pct"], near["prob_touch_pct"])
        self.assertLess(far["z_buffer"], near["z_buffer"])

    def test_call_side_buffer_direction(self):
        # short call: danger is ABOVE — spot below strike = positive buffer
        r = advisor.strike_risk("C", 100.0, 110.0, 0.5, 7 / 365)
        self.assertGreater(r["strike_buffer_pct"], 0)
        r_itm = advisor.strike_risk("C", 115.0, 110.0, 0.5, 7 / 365)
        self.assertEqual(r_itm["prob_touch_pct"], 100.0)

    def test_degenerate_inputs_empty(self):
        self.assertEqual(advisor.strike_risk("P", 100.0, 100.0, 0.0, 0.1), {})
        self.assertEqual(advisor.strike_risk("P", 100.0, 100.0, 0.5, 0.0), {})


class TestNormalizeAdvice(unittest.TestCase):
    def test_string_rows_dropped(self):
        # exact live failure 2026-08-14: strings inside positions array
        a = advisor._normalize_advice({
            "market_risk": "high", "risk_posture": "tight",
            "positions": ["HOLD", {"id": 1, "action": "CLOSE", "reason": "x"},
                          {"id": "2", "action": "CLOSE"},
                          {"id": 3, "action": "SELL"}],
            "summary": "s"})
        self.assertEqual(a["positions"],
                         [{"id": 1, "action": "CLOSE", "reason": "x"}])

    def test_bad_posture_defaults_normal(self):
        a = advisor._normalize_advice({"risk_posture": "panic", "positions": []})
        self.assertEqual(a["risk_posture"], "normal")

    def test_non_dict_advice(self):
        a = advisor._normalize_advice("garbage")
        self.assertEqual(a["positions"], [])
        self.assertEqual(a["risk_posture"], "normal")


if __name__ == "__main__":
    unittest.main()
