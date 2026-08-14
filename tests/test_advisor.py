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
        # prev said WATCH (historical rows pre-2026-08-15) → still not enough
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

    def test_hold_never_executes(self):
        self.assertEqual(advisor.decide_executions(
            _advice({1: "HOLD", 3: "HOLD"}, "lockdown"), POS, None,
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
                         [{"id": 1, "action": "CLOSE", "reason": "x",
                           "brief": ""}])

    def test_bad_posture_defaults_normal(self):
        a = advisor._normalize_advice({"risk_posture": "panic", "positions": []})
        self.assertEqual(a["risk_posture"], "normal")

    def test_non_dict_advice(self):
        a = advisor._normalize_advice("garbage")
        self.assertEqual(a["positions"], [])
        self.assertEqual(a["risk_posture"], "normal")

    def test_fail_closed_keeps_current_posture(self):
        # malformed advice must NOT relax tight/lockdown back to normal
        a = advisor._normalize_advice("garbage", cur_posture="tight")
        self.assertEqual(a["risk_posture"], "tight")
        a = advisor._normalize_advice({"risk_posture": "panic", "positions": []},
                                      cur_posture="lockdown")
        self.assertEqual(a["risk_posture"], "lockdown")
        # invalid cur_posture itself falls back to normal
        a = advisor._normalize_advice("garbage", cur_posture="???")
        self.assertEqual(a["risk_posture"], "normal")


class TestFormatTg(unittest.TestCase):
    ADVICE = {"market_risk": "high", "risk_posture": "tight",
              "tg_summary": "BTC у страйка 63k, фиксируем колл; ETH put держим",
              "positions": [
                  {"id": 60, "action": "CLOSE", "reason": "long text " * 20,
                   "brief": "touch 92%"},
                  {"id": 50, "action": "CLOSE", "reason": "r", "brief": "ITM, режь"},
                  {"id": 49, "action": "HOLD", "reason": "r", "brief": "у страйка"},
                  {"id": 46, "action": "HOLD", "reason": "r", "brief": "ок"}]}
    POS = {60: {"symbol": "BTC-21AUG26-63000-C-USDT", "unrealized_usd": 0.1},
           50: {"symbol": "ETH-21AUG26-1925-P-USDT", "unrealized_usd": -4.3},
           49: {"symbol": "ETH-21AUG26-1900-P-USDT", "unrealized_usd": 1.6},
           46: {"symbol": "ETH-21AUG26-1875-P-USDT", "unrealized_usd": 2.5}}

    def test_compact_structure(self):
        msg = advisor.format_tg(self.ADVICE, self.POS, [60], ("normal", "tight"),
                                866.07, 800.0)
        lines = msg.splitlines()
        self.assertLessEqual(len(lines), 6)          # header+summary+3 pos+foot
        self.assertLess(len(msg), 350)               # ~3x shorter than old style
        self.assertIn("🔴 Jony · high · tight  (normal→tight)", lines[0])
        self.assertIn("🤖 закрыл BTC C63000 +0.1$ · touch 92%", msg)
        self.assertIn("❗ ETH P1925 -4.3$ · ITM, режь — закрой сам", msg)
        self.assertNotIn("long text", msg)           # full reasons never pushed
        self.assertNotIn("P1875", msg)               # HOLD omitted
        self.assertNotIn("P1900", msg)               # HOLD omitted
        self.assertIn("💰 $866 (+8.3%) · позиций 4 (hold 2)", msg)

    def test_short_symbol(self):
        self.assertEqual(advisor._short_symbol("ETH-21AUG26-1875-P-USDT"),
                         "ETH P1875")
        self.assertEqual(advisor._short_symbol("weird"), "weird")


def _entry_advice(coin="BTC", side="P", conf=0.8):
    return {"entry_proposal": {"coin": coin, "side": side, "confidence": conf,
                               "reason": "r", "brief": "b"}}


GOOD_MKT = {"BTC": {"iv_minus_rv24": 0.05, "vol_accelerating": False},
            "ETH": {"iv_minus_rv24": 0.05, "vol_accelerating": False}}


class TestDecideEntry(unittest.TestCase):
    def test_good_proposal_passes(self):
        p = advisor.decide_entry(_entry_advice(), GOOD_MKT, [], "normal", [], 0)
        self.assertIsNotNone(p)
        self.assertEqual((p["coin"], p["side"]), ("BTC", "P"))

    def test_null_and_low_confidence_rejected(self):
        self.assertIsNone(advisor.decide_entry(
            {"entry_proposal": None}, GOOD_MKT, [], "normal", [], 0))
        self.assertIsNone(advisor.decide_entry(
            _entry_advice(conf=0.4), GOOD_MKT, [], "normal", [], 0))

    def test_posture_must_be_normal(self):
        for posture in ("tight", "lockdown"):
            self.assertIsNone(advisor.decide_entry(
                _entry_advice(), GOOD_MKT, [], posture, [], 0))

    def test_key_already_taken_rejected(self):
        pos = [{"coin": "BTC", "side": "P"}]
        self.assertIsNone(advisor.decide_entry(
            _entry_advice(), GOOD_MKT, pos, "normal", [], 0))
        # other key still fine
        self.assertIsNotNone(advisor.decide_entry(
            _entry_advice("ETH", "P"), GOOD_MKT, pos, "normal", [], 0))

    def test_vrp_and_vol_guard(self):
        bad_vrp = {"BTC": {"iv_minus_rv24": -0.02, "vol_accelerating": False}}
        self.assertIsNone(advisor.decide_entry(
            _entry_advice(), bad_vrp, [], "normal", [], 0))
        accel = {"BTC": {"iv_minus_rv24": 0.05, "vol_accelerating": True}}
        self.assertIsNone(advisor.decide_entry(
            _entry_advice(), accel, [], "normal", [], 0))

    def test_revenge_window(self):
        now = 100 * 3_600_000
        loss_2h_ago = now - 2 * 3_600_000       # inside 4h window → blocked
        self.assertIsNone(advisor.decide_entry(
            _entry_advice(), GOOD_MKT, [], "normal", [], now, loss_2h_ago))
        loss_6h_ago = now - 6 * 3_600_000       # window passed → allowed
        self.assertIsNotNone(advisor.decide_entry(
            _entry_advice(), GOOD_MKT, [], "normal", [], now, loss_6h_ago))
        self.assertIsNotNone(advisor.decide_entry(   # no losses at all
            _entry_advice(), GOOD_MKT, [], "normal", [], now, None))

    def test_daily_rate_and_gap(self):
        now = 100 * 3_600_000
        two_today = [now - 5 * 3_600_000, now - 10 * 3_600_000]
        self.assertIsNone(advisor.decide_entry(
            _entry_advice(), GOOD_MKT, [], "normal", two_today, now))
        recent_one = [now - 3_600_000]  # 1h ago < 4h gap
        self.assertIsNone(advisor.decide_entry(
            _entry_advice(), GOOD_MKT, [], "normal", recent_one, now))
        old_one = [now - 6 * 3_600_000]
        self.assertIsNotNone(advisor.decide_entry(
            _entry_advice(), GOOD_MKT, [], "normal", old_one, now))


if __name__ == "__main__":
    unittest.main()
