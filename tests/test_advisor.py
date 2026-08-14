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


if __name__ == "__main__":
    unittest.main()
