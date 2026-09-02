import json
import os
import unittest

os.environ.setdefault("JONY_DB_PATH", ":memory:")
import api  # noqa: E402


def _pos(source, conf, pnl, status="closed_tp2"):
    sp = {"active_side": "P", "spot": 1.0}
    if source == "advisor":
        sp.update({"source": "advisor", "confidence": conf})
    return {"status": status, "pnl_usd": pnl, "signal_payload": json.dumps(sp)}


class TestConfidenceCalibration(unittest.TestCase):
    def test_empty_and_filters(self):
        self.assertIsNone(api.confidence_calibration([]))
        # bot entries, open advisor entries and unclosed pnl are ignored
        rows = [_pos("bot", None, 5.0), _pos("advisor", 0.8, 1.0, status="open"),
                _pos("advisor", 0.8, None)]
        self.assertIsNone(api.confidence_calibration(rows))

    def test_brier_and_distinct(self):
        rows = [_pos("advisor", 0.72, 5.0), _pos("advisor", 0.72, -3.0),
                _pos("advisor", 0.72, 2.0)]
        c = api.confidence_calibration(rows)
        self.assertEqual(c["n"], 3)
        self.assertEqual(c["distinct_conf"], 1)          # константа = нет информации
        self.assertAlmostEqual(c["win_rate"], 0.667, places=3)
        # brier = mean((0.72-1)^2, (0.72-0)^2, (0.72-1)^2)
        self.assertAlmostEqual(c["brier"], round(((0.28 ** 2) * 2 + 0.72 ** 2) / 3, 3), places=3)
        rows.append(_pos("advisor", 0.55, -1.0))
        self.assertEqual(api.confidence_calibration(rows)["distinct_conf"], 2)


if __name__ == "__main__":
    unittest.main()
