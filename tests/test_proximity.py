"""Unit tests for core.proximity.entry_proximity — the Jony dashboard's
entry-proximity gauge. Ported test suite from opt-app's Sniper1
(backend/tests/test_proximity.py), same invariants, factor names updated to
match Jony's actual evaluate_conditions() output (vol_pctile/regime_ok/
tfs_aligned/bull_filter_ok — no adx_score, Jony doesn't use one) and
Jony's window-status shape (core/proximity.py, loop.py's `win[coin]` state).
unittest.TestCase (not the origin's standalone script) to match every other
Jony test file's convention, so `python3 -m unittest discover` picks this up
too instead of silently skipping it.

The key invariant, unchanged from the original: 100 is reserved for `ready`
AND a *confirmed* live debounce window (loop.py's per-coin FLICKER_TOLERANCE
persistence check) that is not disqualified AND at the close-tick minute —
the gauge must never show a full signal the bot wouldn't actually fire on.
"""
from __future__ import annotations

import unittest

from core.proximity import entry_proximity, window_id

# now_ms used throughout: epoch_min = 1_000_500 // 60_000 = 16 -> wid = 16//5 = 3
NOW_MS = 1_000_500
SAME_WID = window_id(NOW_MS // 60_000)
assert SAME_WID == 3, SAME_WID
OTHER_WID = SAME_WID - 1


class TestEntryProximity(unittest.TestCase):
    def test_ready_without_window_status_is_capped_below_100(self):
        p = entry_proximity({"ready": True, "tfs_aligned": 3, "vol_pctile": 1.0,
                             "regime_ok": True, "bull_filter_ok": True})
        self.assertLess(p["proximity_pct"], 100.0)
        self.assertNotEqual(p["zone"], "entry")
        self.assertTrue(p["debounce_unknown"])

    def test_not_ready_is_capped_below_100(self):
        p = entry_proximity({"ready": False, "tfs_aligned": 3, "vol_pctile": 1.0,
                             "regime_ok": True, "bull_filter_ok": True})
        self.assertEqual(p["proximity_pct"], 99.0)
        self.assertEqual(p["zone"], "ready")

    def test_empty_factors_is_waiting(self):
        p = entry_proximity({"ready": False, "tfs_aligned": None, "vol_pctile": None,
                             "regime_ok": False, "bull_filter_ok": False})
        self.assertEqual(p["proximity_pct"], 0.0)
        self.assertEqual(p["zone"], "waiting")

    def test_weighted_blend_value(self):
        # vol=0.5(w.20), regime ok(w.30), mtf=3/3=1(w.30), bull ok(w.20)
        # = 100*(.20*.5 + .30*1 + .30*1 + .20*1) = 100*(.10+.30+.30+.20) = 90.0
        p = entry_proximity({"ready": False, "tfs_aligned": 3, "vol_pctile": 0.5,
                             "regime_ok": True, "bull_filter_ok": True})
        self.assertAlmostEqual(p["proximity_pct"], 90.0, places=1)
        self.assertEqual(p["zone"], "ready")

    def test_clamps_out_of_range_inputs(self):
        # tfs_aligned above 3 and vol_pctile above 1 must clamp, not overflow.
        p = entry_proximity({"ready": False, "tfs_aligned": 9, "vol_pctile": 5.0,
                             "regime_ok": True, "bull_filter_ok": True})
        self.assertEqual(p["proximity_pct"], 99.0)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in p["factors"].values()))

    def test_preparing_zone_midrange(self):
        # vol=0.3(w.20), regime ok(w.30), mtf=3/3=1(w.30), bull not ok(w.20)
        # = 100*(.20*.3 + .30*1 + .30*1 + 0) = 100*(.06+.30+.30) = 66.0
        p = entry_proximity({"ready": False, "tfs_aligned": 3, "vol_pctile": 0.3,
                             "regime_ok": True, "bull_filter_ok": False})
        self.assertTrue(50.0 <= p["proximity_pct"] < 80.0, p)
        self.assertEqual(p["zone"], "preparing")

    def test_regime_failure_measurably_lowers_the_gauge(self):
        with_regime = entry_proximity({"ready": False, "tfs_aligned": 3, "vol_pctile": 1.0,
                                       "regime_ok": True, "bull_filter_ok": True})
        without_regime = entry_proximity({"ready": False, "tfs_aligned": 3, "vol_pctile": 1.0,
                                          "regime_ok": False, "bull_filter_ok": True})
        self.assertGreater(with_regime["proximity_pct"], without_regime["proximity_pct"])

    def test_window_disqualified_blocks_entry_zone_even_when_ready(self):
        fresh_disqualified = {"wid": SAME_WID, "disqualified": True, "checked_at_ms": NOW_MS - 5_000}
        p = entry_proximity({"ready": True, "tfs_aligned": 3, "vol_pctile": 1.0,
                             "regime_ok": True, "bull_filter_ok": True},
                             window_status=fresh_disqualified, now_ms=NOW_MS)
        self.assertLess(p["proximity_pct"], 100.0)
        self.assertNotEqual(p["zone"], "entry")
        self.assertTrue(p["window_disqualified"])
        self.assertFalse(p["debounce_unknown"])

    def test_window_not_disqualified_and_same_window_allows_entry_zone(self):
        # min_in_window=4 -> the close-tick minute (FIVE_MIN - 1), the same
        # one loop.py's fire block actually attempts the open on.
        fresh_ok = {"wid": SAME_WID, "disqualified": False, "checked_at_ms": NOW_MS - 5_000,
                    "min_in_window": 4}
        p = entry_proximity({"ready": True, "tfs_aligned": 3, "vol_pctile": 1.0,
                             "regime_ok": True, "bull_filter_ok": True},
                             window_status=fresh_ok, now_ms=NOW_MS)
        self.assertEqual(p["proximity_pct"], 100.0)
        self.assertEqual(p["zone"], "entry")
        self.assertFalse(p["debounce_unknown"])

    def test_early_minute_not_disqualified_does_not_reach_100(self):
        fresh_but_early = {"wid": SAME_WID, "disqualified": False, "checked_at_ms": NOW_MS - 5_000,
                           "min_in_window": 1}
        p = entry_proximity({"ready": True, "tfs_aligned": 3, "vol_pctile": 1.0,
                             "regime_ok": True, "bull_filter_ok": True},
                             window_status=fresh_but_early, now_ms=NOW_MS)
        self.assertLess(p["proximity_pct"], 100.0)
        self.assertNotEqual(p["zone"], "entry")
        self.assertFalse(p["debounce_unknown"])
        self.assertFalse(p["window_disqualified"])

    def test_every_non_close_tick_minute_capped_below_100(self):
        for m in range(4):
            status = {"wid": SAME_WID, "disqualified": False, "checked_at_ms": NOW_MS - 5_000,
                      "min_in_window": m}
            p = entry_proximity({"ready": True, "tfs_aligned": 3, "vol_pctile": 1.0,
                                 "regime_ok": True, "bull_filter_ok": True},
                                 window_status=status, now_ms=NOW_MS)
            self.assertLess(p["proximity_pct"], 100.0, (m, p))
            self.assertNotEqual(p["zone"], "entry", (m, p))

    def test_stale_window_status_falls_back_to_unconfirmed(self):
        stale = {"wid": SAME_WID, "disqualified": True, "checked_at_ms": 0}
        p = entry_proximity({"ready": True, "tfs_aligned": 3, "vol_pctile": 1.0,
                             "regime_ok": True, "bull_filter_ok": True},
                             window_status=stale, now_ms=1_000_000_000)
        self.assertTrue(p["debounce_unknown"])
        self.assertLess(p["proximity_pct"], 100.0)
        self.assertNotEqual(p["zone"], "entry")

    def test_cross_window_status_is_not_trusted_even_if_fresh(self):
        fresh_but_other_window = {"wid": OTHER_WID, "disqualified": False,
                                  "checked_at_ms": NOW_MS - 5_000}
        p = entry_proximity({"ready": True, "tfs_aligned": 3, "vol_pctile": 1.0,
                             "regime_ok": True, "bull_filter_ok": True},
                             window_status=fresh_but_other_window, now_ms=NOW_MS)
        self.assertTrue(p["debounce_unknown"])
        self.assertLess(p["proximity_pct"], 100.0)
        self.assertNotEqual(p["zone"], "entry")

    def test_bull_defaults_true_when_side_has_no_bull_filter(self):
        # PUT_GEN.bull_market_ratio_max is None -> _evaluate_side leaves
        # bull_filter_ok at its True default; the gauge must not penalize that.
        p = entry_proximity({"ready": False, "tfs_aligned": 3, "vol_pctile": 1.0,
                             "regime_ok": True, "bull_filter_ok": True})
        self.assertEqual(p["factors"]["bull"], 1.0)


if __name__ == "__main__":
    unittest.main()


class TestSideOffZone(unittest.TestCase):
    def test_no_side_allowed_shows_side_off(self):
        from core.proximity import entry_proximity
        p = entry_proximity({"no_side_allowed": True, "bull_filter_ok": True},
                            None, 1_000_000)
        self.assertEqual(p["zone"], "side-off")
        self.assertEqual(p["proximity_pct"], 0.0)
        self.assertFalse(p["debounce_unknown"])

    def test_normal_eval_unaffected(self):
        from core.proximity import entry_proximity
        p = entry_proximity({"no_side_allowed": False, "vol_pctile": 0.7,
                             "regime_ok": True, "tfs_aligned": 3,
                             "bull_filter_ok": True}, None, 1_000_000)
        self.assertNotEqual(p["zone"], "side-off")
        self.assertGreater(p["proximity_pct"], 50)
