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


# Снапшоты проходят калиброванную политику закрытий (2026-08-17): 1 и 3 —
# эндшпиль (профит >= 25% кредита, возраст >= 70% hold_h), 2 — аварийный
# ITM-убыток. Существующие тесты проверяют ДРУГИЕ гейты (mode/persistence/
# rate-limit) поверх позиций, которым политика закрываться разрешает;
# сама политика тестируется в TestClosePolicy.
POS = {1: {"id": 1, "unrealized_usd": 5.0, "profit_pct_of_credit": 40.0,
           "age_h": 100.0, "hold_h": 120, "strike_buffer_pct": 3.0},
       2: {"id": 2, "unrealized_usd": -3.0, "profit_pct_of_credit": -60.0,
           "age_h": 30.0, "hold_h": 120, "strike_buffer_pct": -1.5},
       3: {"id": 3, "unrealized_usd": 0.8, "profit_pct_of_credit": 26.0,
           "age_h": 20.0, "hold_h": 24, "strike_buffer_pct": 2.0}}


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


def _snap(**kw) -> dict:
    base = {"id": 7, "unrealized_usd": 1.0, "profit_pct_of_credit": 10.0,
            "age_h": 10.0, "hold_h": 120, "strike_buffer_pct": 2.0}
    base.update(kw)
    return base


class TestClosePolicy(unittest.TestCase):
    """Калиброванная политика закрытий (Phase 9): профиту дают дозреть.
    Разрешено ровно два случая — эндшпиль и аварийный ITM-убыток."""

    def test_endgame_allowed(self):
        self.assertTrue(advisor.close_policy_allows(
            _snap(profit_pct_of_credit=30.0, age_h=90.0, hold_h=120)))

    def test_young_winner_vetoed(self):
        # кейс id56-59 истории: BTC-коллы +33-36% в возрасте 8-10ч из 24ч —
        # раньше советник их резал; 8h < 70% * 24h => вето
        self.assertFalse(advisor.close_policy_allows(
            _snap(profit_pct_of_credit=35.0, age_h=8.0, hold_h=24)))

    def test_mature_but_small_profit_vetoed(self):
        self.assertFalse(advisor.close_policy_allows(
            _snap(profit_pct_of_credit=10.0, age_h=110.0, hold_h=120)))

    def test_atm_fear_vetoed(self):
        # кейс id60/61: свежая ATM-позиция у страйка — норма, не риск
        self.assertFalse(advisor.close_policy_allows(
            _snap(profit_pct_of_credit=0.5, age_h=2.0, hold_h=24,
                  strike_buffer_pct=0.2)))

    def test_breathing_drawdown_vetoed(self):
        # кейс id1-6: просадка -25..-50% ITM регулярно возвращается в плюс
        self.assertFalse(advisor.close_policy_allows(
            _snap(profit_pct_of_credit=-30.0, age_h=20.0, hold_h=96,
                  strike_buffer_pct=-1.0)))

    def test_emergency_deep_itm_loss_allowed(self):
        self.assertTrue(advisor.close_policy_allows(
            _snap(profit_pct_of_credit=-70.0, strike_buffer_pct=-2.0)))

    def test_deep_loss_but_otm_vetoed(self):
        # глубокий минус БЕЗ ITM (вола раздула премию) — не аварийный кейс
        self.assertFalse(advisor.close_policy_allows(
            _snap(profit_pct_of_credit=-70.0, strike_buffer_pct=1.0)))

    def test_zero_hold_fail_closed(self):
        self.assertFalse(advisor.close_policy_allows(
            _snap(profit_pct_of_credit=40.0, age_h=10.0, hold_h=0)))

    def test_missing_data_fail_closed(self):
        self.assertFalse(advisor.close_policy_allows(
            _snap(profit_pct_of_credit=None)))
        self.assertFalse(advisor.close_policy_allows(
            _snap(age_h=None)))
        self.assertFalse(advisor.close_policy_allows(
            _snap(hold_h=None)))
        self.assertFalse(advisor.close_policy_allows(
            _snap(profit_pct_of_credit=-70.0, strike_buffer_pct=None)))
        self.assertFalse(advisor.close_policy_allows({}))

    def test_lockdown_does_not_bypass_policy(self):
        # даже lockdown (urgent, без persistence) не может резать молодой
        # профит — паническая фиксация и была измеренным источником убытка
        young = {5: _snap(id=5, unrealized_usd=2.0,
                          profit_pct_of_credit=35.0, age_h=8.0, hold_h=24)}
        self.assertEqual(advisor.decide_executions(
            _advice({5: "CLOSE"}, "lockdown"), young, None,
            "profit_only", [], 0), [])

    def test_endgame_executes_through_decide_executions(self):
        ripe = {6: _snap(id=6, unrealized_usd=2.0,
                         profit_pct_of_credit=30.0, age_h=100.0, hold_h=120)}
        self.assertEqual(advisor.decide_executions(
            _advice({6: "CLOSE"}), ripe, _prev({6: "CLOSE"}),
            "profit_only", [], 0), [6])


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
    POS = {60: {"symbol": "BTC-21AUG26-63000-C-USDT", "unrealized_usd": 0.1,
                "profit_pct_of_credit": 1.0, "age_h": 3.0, "hold_h": 24,
                "tp2_pct": 0.7, "sl_pct": 0.75, "strike_buffer_pct": 0.4},
           50: {"symbol": "ETH-21AUG26-1925-P-USDT", "unrealized_usd": -4.3,
                "profit_pct_of_credit": -62.0, "age_h": 10.0, "hold_h": 120,
                "tp2_pct": 0.7, "sl_pct": 1.75, "strike_buffer_pct": -2.1},
           49: {"symbol": "ETH-21AUG26-1900-P-USDT", "unrealized_usd": 1.6,
                "profit_pct_of_credit": 8.0, "age_h": 40.0, "hold_h": 120,
                "tp2_pct": 0.7, "sl_pct": 1.75, "strike_buffer_pct": 1.7},
           46: {"symbol": "ETH-21AUG26-1875-P-USDT", "unrealized_usd": 2.5,
                "profit_pct_of_credit": 30.0, "age_h": 60.0, "hold_h": 120,
                "tp2_pct": 0.7, "sl_pct": 1.75}}
    MARKET = {"BTC": {"spot": 63398.6, "chg_24h_pct": -0.41},
              "ETH": {"spot": 1886.09, "chg_24h_pct": -0.29}}

    def test_compact_structure(self):
        msg = advisor.format_tg(self.ADVICE, self.POS, [60], ("normal", "tight"),
                                866.07, 800.0, market=self.MARKET)
        lines = msg.splitlines()
        self.assertLessEqual(len(lines), 9)          # head+market+summary+4 pos(+1 detail)
        self.assertLess(len(msg), 700)
        self.assertEqual(lines[0], "🔴 Jony · риск high · tight (normal→tight) · 💰 $866 (+8.3%)")
        self.assertEqual(lines[1], "📊 BTC 63 399 (-0.4%) · ETH 1 886 (-0.3%)")
        self.assertTrue(lines[2].startswith("💬 BTC у страйка"))
        self.assertIn("🤖 закрыл BTC C63000 +0.1$ (+1%) · touch 92%", msg)
        # неветированный CLOSE — императив + что сделает механика без человека
        self.assertIn("🔴 ETH P1925 -4.3$ (-62%) · 10/120ч · ITM -2.1%", msg)
        self.assertIn("❗ ITM, режь → закрой сам (бот убыточные не закрывает) · иначе механика: SL −175% / time-stop 110ч", msg)
        # HOLD-позиции показаны одной строкой с механикой
        self.assertIn("🟡 ETH P1900 +1.6$ (+8%) · 40/120ч · до страйка 1.7% · у страйка · TP2 +70% / time-stop 80ч", msg)
        self.assertIn("🟢 ETH P1875 +2.5$ (+30%) · 60/120ч · ок · TP2 +70% / time-stop 60ч", msg)
        self.assertNotIn("long text", msg)           # full reasons never pushed

    def test_short_symbol(self):
        self.assertEqual(advisor._short_symbol("ETH-21AUG26-1875-P-USDT"),
                         "ETH P1875")
        self.assertEqual(advisor._short_symbol("weird"), "weird")

    def test_vetoed_close_is_not_an_imperative(self):
        # CLOSE, отклонённый политикой закрытий, — «держим», а не «закрой
        # сам»: иначе ветированный ранний харвест исполнялся бы руками
        # человека (ревью 2026-08-17)
        msg = advisor.format_tg(self.ADVICE, self.POS, [], None,
                                866.07, 800.0, vetoed={60})
        self.assertIn("🔒 CLOSE отклонён политикой · touch 92% · механика: TP2 +70% / time-stop 21ч", msg)
        self.assertNotIn("touch 92% → закрой сам", msg)
        # неветированный CLOSE (аварийный кейс) сохраняет императив человеку
        self.assertIn("❗ ITM, режь → закрой сам", msg)


def _entry_advice(coin="BTC", side="P", conf=0.8):
    return {"entry_proposal": {"coin": coin, "side": side, "confidence": conf,
                               "reason": "r", "brief": "b"}}


GOOD_MKT = {"BTC": {"iv_minus_rv24": 0.05, "vol_accelerating": False},
            "ETH": {"iv_minus_rv24": 0.05, "vol_accelerating": False}}


def _de_test(*args, **kw):
    kw.setdefault("mech_gates", FRESH_GATES)
    return advisor.decide_entry(*args, **kw)

advisor._de_test = _de_test

FRESH_GATES = {"ETH": {"no_side_allowed": False, "ret_7d": 0.2, "stale": False},
               "BTC": {"no_side_allowed": False, "ret_7d": 0.2, "stale": False}}


class TestDecideEntry(unittest.TestCase):
    def test_good_proposal_passes(self):
        p = advisor._de_test(_entry_advice(), GOOD_MKT, [], "normal", [], 0)
        self.assertIsNotNone(p)
        self.assertEqual((p["coin"], p["side"]), ("BTC", "P"))

    def test_null_and_low_confidence_rejected(self):
        self.assertIsNone(advisor._de_test(
            {"entry_proposal": None}, GOOD_MKT, [], "normal", [], 0))
        self.assertIsNone(advisor._de_test(
            _entry_advice(conf=0.4), GOOD_MKT, [], "normal", [], 0))

    def test_posture_gating(self):
        # self-lock fix 2026-08-26: lockdown blocks; tight blocks only when
        # market_risk == 'high'; tight + medium risk passes
        self.assertIsNone(advisor._de_test(
            _entry_advice(), GOOD_MKT, [], "lockdown", [], 0))
        hi = dict(_entry_advice(), market_risk="high")
        self.assertIsNone(advisor._de_test(hi, GOOD_MKT, [], "tight", [], 0))
        med = dict(_entry_advice(), market_risk="medium")
        self.assertIsNotNone(advisor._de_test(med, GOOD_MKT, [], "tight", [], 0))

    def test_key_already_taken_rejected(self):
        pos = [{"coin": "BTC", "side": "P"}]
        self.assertIsNone(advisor._de_test(
            _entry_advice(), GOOD_MKT, pos, "normal", [], 0))
        # other key still fine
        self.assertIsNotNone(advisor._de_test(
            _entry_advice("ETH", "P"), GOOD_MKT, pos, "normal", [], 0))

    def test_vrp_and_vol_guard(self):
        bad_vrp = {"BTC": {"iv_minus_rv24": -0.02, "vol_accelerating": False}}
        self.assertIsNone(advisor._de_test(
            _entry_advice(), bad_vrp, [], "normal", [], 0))
        accel = {"BTC": {"iv_minus_rv24": 0.05, "vol_accelerating": True}}
        self.assertIsNone(advisor._de_test(
            _entry_advice(), accel, [], "normal", [], 0))

    def test_revenge_window(self):
        now = 100 * 3_600_000
        loss_2h_ago = now - 2 * 3_600_000       # inside 4h window → blocked
        self.assertIsNone(advisor._de_test(
            _entry_advice(), GOOD_MKT, [], "normal", [], now, loss_2h_ago))
        loss_6h_ago = now - 6 * 3_600_000       # window passed → allowed
        self.assertIsNotNone(advisor._de_test(
            _entry_advice(), GOOD_MKT, [], "normal", [], now, loss_6h_ago))
        self.assertIsNotNone(advisor._de_test(   # no losses at all
            _entry_advice(), GOOD_MKT, [], "normal", [], now, None))

    def test_daily_rate_and_gap(self):
        now = 100 * 3_600_000
        two_today = [now - 5 * 3_600_000, now - 10 * 3_600_000]
        self.assertIsNone(advisor._de_test(
            _entry_advice(), GOOD_MKT, [], "normal", two_today, now))
        recent_one = [now - 3_600_000]  # 1h ago < 4h gap
        self.assertIsNone(advisor._de_test(
            _entry_advice(), GOOD_MKT, [], "normal", recent_one, now))
        old_one = [now - 6 * 3_600_000]
        self.assertIsNotNone(advisor._de_test(
            _entry_advice(), GOOD_MKT, [], "normal", old_one, now))


class TestP2InputBlocks(unittest.TestCase):
    """P2 2026-08-17: mechanical_gates + track_record во входе советника."""

    def test_mechanical_gates_block_reads_window_status(self):
        import advisor
        from db import repo as _repo
        conn = _repo.connect()
        _repo.apply_schema(conn)
        _repo.upsert_window_status(conn, "ETH", wid=1, min_in_window=0,
                                   disqualified=0,
                                   ev={"no_side_allowed": True, "ret_7d": -2.4},
                                   checked_at_ms=1)
        _repo.upsert_window_status(conn, "BTC", wid=1, min_in_window=0,
                                   disqualified=0,
                                   ev={"no_side_allowed": False, "ret_7d": 1.2},
                                   checked_at_ms=1)
        out = advisor.mechanical_gates_block(conn)
        conn.close()
        self.assertTrue(out["ETH"]["no_side_allowed"])
        self.assertEqual(out["ETH"]["ret_7d"], -2.4)
        self.assertFalse(out["BTC"]["no_side_allowed"])

    def test_mechanical_gates_block_empty_db(self):
        import advisor
        from db import repo as _repo
        conn = _repo.connect()
        _repo.apply_schema(conn)
        conn.execute("DELETE FROM window_status")
        conn.commit()
        out = advisor.mechanical_gates_block(conn)
        conn.close()
        self.assertFalse(out["ETH"]["no_side_allowed"])
        self.assertIsNone(out["ETH"]["ret_7d"])

    def test_track_record_none_on_api_error(self):
        import advisor
        from unittest.mock import patch as _patch
        with _patch.object(advisor.requests, "get",
                           side_effect=OSError("down")):
            self.assertIsNone(advisor.track_record_block())


class TestCliBackend(unittest.TestCase):
    """Подписочный CLI-бэкенд (2026-08-17)."""

    def _cli_result(self, result_text, rc=0):
        import json as _json
        from unittest.mock import MagicMock
        m = MagicMock()
        m.returncode = rc
        m.stdout = _json.dumps({"result": result_text})
        m.stderr = ""
        return m

    def test_parses_clean_json(self):
        import advisor
        from unittest.mock import patch as _patch
        good = ('{"market_risk":"low","risk_posture":"normal","market_view":"x",'
                '"tg_summary":"t","positions":[],"summary":"s"}')
        with _patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}), \
             _patch("subprocess.run", return_value=self._cli_result(good)) as mrun:
            out = advisor.call_claude_cli({"a": 1})
        self.assertEqual(out["risk_posture"], "normal")
        # харденинг: чистое окружение без ANTHROPIC_API_KEY/биржевых секретов
        env = mrun.call_args.kwargs["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("BYBIT_API_KEY", env)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok")
        self.assertIn("--disallowedTools", mrun.call_args.args[0])

    def test_strips_code_fences_and_retries_once(self):
        import advisor
        from unittest.mock import patch as _patch
        fenced = ('```json\n{"market_risk":"low","risk_posture":"tight",'
                  '"market_view":"","tg_summary":"","positions":[],"summary":""}\n```')
        bad = self._cli_result("не json")
        ok = self._cli_result(fenced)
        with _patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}), \
             _patch("subprocess.run", side_effect=[bad, ok]) as mrun:
            out = advisor.call_claude_cli({}, cur_posture="tight")
        self.assertEqual(mrun.call_count, 2)
        self.assertEqual(out["risk_posture"], "tight")

    def test_raises_after_two_failures(self):
        import advisor
        from unittest.mock import patch as _patch
        bad = self._cli_result("мусор")
        with _patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}), \
             _patch("subprocess.run", side_effect=[bad, bad]):
            with self.assertRaises(RuntimeError):
                advisor.call_claude_cli({})

    def test_rc_error_retries_once_and_reports_stdout(self):
        import advisor
        from unittest.mock import patch as _patch, MagicMock
        m = MagicMock(); m.returncode = 1
        m.stdout = '{"is_error":true,"result":"overloaded"}'; m.stderr = ""
        with _patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}), \
             _patch("subprocess.run", return_value=m) as mrun:
            with self.assertRaises(RuntimeError) as cm:
                advisor.call_claude_cli({})
        self.assertEqual(mrun.call_count, 2)
        # rc!=0: CLI кладёт текст ошибки в stdout, он должен попасть в сообщение
        self.assertIn("overloaded", str(cm.exception))

    def test_missing_token_fails_loud(self):
        import advisor, os as _os
        from unittest.mock import patch as _patch
        env = {k: v for k, v in _os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
        with _patch.dict("os.environ", env, clear=True):
            with self.assertRaises(RuntimeError):
                advisor.call_claude_cli({})

    def test_stale_gates_reject_entry(self):
        import advisor
        stale = {"BTC": {"no_side_allowed": True, "ret_7d": -3.0, "stale": True},
                 "ETH": {"no_side_allowed": False, "ret_7d": 0.0, "stale": True}}
        self.assertIsNone(advisor.decide_entry(
            _entry_advice(), GOOD_MKT, [], "normal", [], 0, mech_gates=stale))

    def test_countertrend_put_needs_stabilization(self):
        import advisor
        down = {"BTC": {"no_side_allowed": True, "ret_7d": -3.0, "stale": False},
                "ETH": {"no_side_allowed": False, "ret_7d": 0.0, "stale": False}}
        mkt_bad = {"BTC": {**GOOD_MKT["BTC"], "chg_24h_pct": -2.0,
                           "dist_from_7d_low_pct": 0.3}}
        self.assertIsNone(advisor.decide_entry(
            _entry_advice(), mkt_bad, [], "normal", [], 0, mech_gates=down))
        mkt_ok = {"BTC": {**GOOD_MKT["BTC"], "chg_24h_pct": 0.1,
                          "dist_from_7d_low_pct": 2.5}}
        self.assertIsNotNone(advisor.decide_entry(
            _entry_advice(), mkt_ok, [], "normal", [], 0, mech_gates=down))


class TestWakeCooldown(unittest.TestCase):
    """2026-08-22: один триггер (позиция у страйка) будил модель каждые 10 мин
    сутками — ~200 вызовов за 2 дня; теперь один ключ ≤ 1 побудки в 30 мин."""

    def setUp(self):
        advisor._wake_last.clear()

    @staticmethod
    def _kl(first, last):
        return [{"close": first}, {"close": first}, {"close": first}, {"close": last}]

    def test_triggers_keys(self):
        pos = [{"id": 64, "coin": "ETH", "strike": 1900.0}]
        kl = {"BTC": self._kl(64000, 65500), "ETH": self._kl(1910, 1912)}
        t = advisor._wake_triggers(pos, kl)
        self.assertEqual([k for k, _ in t], ["move:BTC", "prox:64"])
        self.assertEqual(advisor._wake_triggers(pos, {"ETH": self._kl(2100, 2101)}), [])

    def test_same_key_cooldown_other_key_passes(self):
        t = [("prox:64", "ETH у страйка")]
        m = 60_000
        self.assertIsNotNone(advisor.pick_wake(t, 0))
        self.assertIsNone(advisor.pick_wake(t, 10 * m))
        self.assertIsNone(advisor.pick_wake(t, 29 * m))
        self.assertIsNotNone(advisor.pick_wake(t, 30 * m))
        # другой триггер в кулдаун первого не попадает
        self.assertEqual(advisor.pick_wake([("move:BTC", "BTC +2.6%")], 31 * m), "BTC +2.6%")
        # оба в кулдауне → тишина, следующий плановый тик всё равно придёт
        self.assertIsNone(advisor.pick_wake(t + [("move:BTC", "x")], 40 * m))

    def test_wake_stamps_all_triggered_keys(self):
        m = 60_000
        a, b = ("move:BTC", "BTC +2%"), ("prox:64", "ETH у страйка")
        self.assertEqual(advisor.pick_wake([a, b], 0), "BTC +2%")
        self.assertIsNone(advisor.pick_wake([b], 10 * m))  # b видел модель вместе с a
        self.assertIsNotNone(advisor.pick_wake([b], 30 * m))


CALL_MKT_OK = {"iv_minus_rv24": 0.05, "vol_accelerating": False, "chg_24h_pct": -1.2,
               "chg_1h_pct": 0.2, "chg_7d_pct": 4.0, "dist_from_7d_high_pct": -3.1,
               "funding_rate_pct": 0.01}


class TestCallGuard(unittest.TestCase):
    """2026-08-22: advisor-only коллы — код-дубль условий промпта + kill."""

    def _p(self, conf=0.8):
        return {"coin": "ETH", "side": "C", "confidence": conf}

    def test_ok_market_passes(self):
        self.assertIsNone(advisor.call_guard(self._p(), CALL_MKT_OK, [], 0, by_key={}))
        self.assertIn("unavailable", advisor.call_guard(self._p(), CALL_MKT_OK, [], 0, by_key=None))

    def test_each_condition(self):
        cases = {
            "chg_24h_pct": (0.6, "chg_24h"), "chg_1h_pct": (1.5, "chg_1h"),
            "chg_7d_pct": (15.0, "parabolic"), "dist_from_7d_high_pct": (-0.8, "7d_high"),
            "funding_rate_pct": (0.08, "funding"),
        }
        for k, (bad, word) in cases.items():
            m = dict(CALL_MKT_OK, **{k: bad})
            why = advisor.call_guard(self._p(), m, [], 0, by_key={})
            self.assertIsNotNone(why, k); self.assertIn(word, why, k)
        # крах-день → squeeze-риск
        self.assertIn("squeeze", advisor.call_guard(self._p(), dict(CALL_MKT_OK, chg_24h_pct=-8.0), [], 0, by_key={}))
        # отрицательный funding (шорты перегружены)
        self.assertIn("funding", advisor.call_guard(self._p(), dict(CALL_MKT_OK, funding_rate_pct=-0.03), [], 0, by_key={}))
        m = dict(CALL_MKT_OK); del m["funding_rate_pct"]
        self.assertIn("funding missing", advisor.call_guard(self._p(), m, [], 0, by_key={}))
        # нет полей → отказ (fail closed)
        self.assertIn("missing", advisor.call_guard(self._p(), {"iv_minus_rv24": 0.05}, [], 0, by_key={}))

    def test_confidence_and_daily_cap(self):
        self.assertIn("confidence", advisor.call_guard(self._p(0.65), CALL_MKT_OK, [], 0, by_key={}))
        h = 3_600_000
        self.assertIn("daily cap", advisor.call_guard(self._p(), CALL_MKT_OK, [5 * h], 20 * h, by_key={}))
        self.assertIsNone(advisor.call_guard(self._p(), CALL_MKT_OK, [5 * h], 30 * h, by_key={}))

    def test_kill_switch(self):
        ok = {"ETH:C": {"advisor": {"n": 4, "wins": 1, "pnl_usd": -10.0}},
              "BTC:C": {"advisor": {"n": 3, "wins": 0, "pnl_usd": -8.0}}}
        self.assertIsNone(advisor.call_kill_active(ok))  # n=7 < 8 — ещё рано
        bad = {"ETH:C": {"advisor": {"n": 5, "wins": 1, "pnl_usd": -10.0}},
               "BTC:C": {"advisor": {"n": 3, "wins": 2, "pnl_usd": -5.0}}}
        self.assertIn("kill-switch", advisor.call_kill_active(bad))  # WR 3/8=37.5%
        fine = {"ETH:C": {"advisor": {"n": 8, "wins": 5, "pnl_usd": 12.0}, "bot": {"n": 11, "wins": 2, "pnl_usd": -41.0}}}
        self.assertIsNone(advisor.call_kill_active(fine))  # bot-история не считается
        self.assertIsNone(advisor.call_kill_active(None))
        self.assertIn("kill-switch", advisor.call_guard(self._p(), CALL_MKT_OK, [], 0, by_key=bad))

    def test_decide_entry_routes_calls_through_guard(self):
        mkt = {"ETH": dict(CALL_MKT_OK), "BTC": dict(CALL_MKT_OK)}
        self.assertIsNotNone(advisor._de_test(_entry_advice("ETH", "C", 0.8), mkt, [], "normal", [], 0, by_key={}))
        self.assertIsNone(advisor._de_test(_entry_advice("ETH", "C", 0.65), mkt, [], "normal", [], 0, by_key={}))
        mkt["ETH"]["dist_from_7d_high_pct"] = -0.3
        self.assertIsNone(advisor._de_test(_entry_advice("ETH", "C", 0.8), mkt, [], "normal", [], 0, by_key={}))
        # пут теми же guard'ами не ограничен
        self.assertIsNotNone(advisor._de_test(_entry_advice("ETH", "P", 0.65), mkt, [], "normal", [], 0))
        # плумбинг kwargs: daily cap и kill-switch доходят до call_guard через decide_entry
        mkt["ETH"]["dist_from_7d_high_pct"] = -3.1
        h = 3_600_000
        self.assertIsNone(advisor._de_test(_entry_advice("ETH", "C", 0.8), mkt, [], "normal", [], 20 * h,
                                           recent_call_ts=[5 * h], by_key={}))
        bad = {"ETH:C": {"advisor": {"n": 8, "wins": 2, "pnl_usd": -5.0}}}
        self.assertIsNone(advisor._de_test(_entry_advice("ETH", "C", 0.8), mkt, [], "normal", [], 0, by_key=bad))
        # API недоступен (by_key=None) → fail-closed для коллов, путы не затронуты
        self.assertIsNone(advisor._de_test(_entry_advice("ETH", "C", 0.8), mkt, [], "normal", [], 0, by_key=None))
        self.assertIsNotNone(advisor._de_test(_entry_advice("ETH", "P", 0.8), mkt, [], "normal", [], 0, by_key=None))


if __name__ == "__main__":
    unittest.main()
