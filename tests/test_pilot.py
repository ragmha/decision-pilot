import json
import tempfile
import unittest
from pathlib import Path

from decision_pilot import calibrate, gate, routing
from decision_pilot.cli import main
from decision_pilot.client import DecisionError, MockProvider, normalise

ROOT = Path(__file__).resolve().parent.parent
ROUTING = json.loads((ROOT / "config/routing.json").read_text())
GATE = json.loads((ROOT / "config/gate.json").read_text())


class Fixed:
    name, model = "fixed", "fixed"

    def __init__(self, answers):
        self.answers = answers

    def ask(self, state, questions):
        return self.answers


class NormaliseTest(unittest.TestCase):
    questions = {"t": {"type": "choice"}, "u": {"type": "noul"}, "s": {"type": "score"}}

    def test_reads_documented_and_alternate_field_names(self):
        for key in ("probabilities", "probs", "distribution", "scores"):
            payload = {"answers": {"t": {"choice": "a", key: {"a": 0.8, "b": 0.2}},
                                   "u": {"probability": 0.3}, "s": {"score": 1.5}}}
            out = normalise(payload, self.questions)
            self.assertEqual(out["t"]["probabilities"]["a"], 0.8)
            self.assertEqual(out["u"]["probability"], 0.3)
            self.assertEqual(out["s"]["score"], 1.5)

    def test_unknown_shape_names_the_keys_it_saw(self):
        with self.assertRaises(DecisionError) as ctx:
            normalise({"answers": {"u": {"mystery": 1}}}, {"u": {"type": "noul"}})
        self.assertIn("mystery", str(ctx.exception))


class CalibrationTest(unittest.TestCase):
    def test_perfectly_calibrated_has_zero_error(self):
        pairs = [(0.9, True)] * 9 + [(0.9, False)]
        self.assertAlmostEqual(calibrate.expected_calibration_error(pairs), 0.0)

    def test_threshold_meets_target_or_refuses(self):
        pairs = [(0.95, True)] * 5 + [(0.5, False)] * 5
        self.assertEqual(calibrate.pick_threshold(pairs, 0.95), 0.95)
        self.assertIsNone(calibrate.pick_threshold([(0.9, False)] * 5, 0.5))

    def test_split_is_stable(self):
        self.assertEqual(calibrate.split("ISSUE-001"), calibrate.split("ISSUE-001"))


class DecisionTest(unittest.TestCase):
    def route(self, confidence):
        answers = {"team": {"choice": "docs", "probabilities": {"docs": confidence}},
                   "severity": {"score": 0.2}}
        return routing.decide(Fixed(answers), ROUTING, "t", "b")

    def test_routing_bands(self):
        self.assertEqual(self.route(0.95)["action"], "act")
        self.assertEqual(self.route(0.7)["action"], "review")
        self.assertEqual(self.route(0.3)["action"], "triage")
        self.assertEqual(self.route(0.3)["severity_label"], "Low")

    def test_gate_bands(self):
        decide = lambda p: gate.decide(Fixed({"unsafe": {"probability": p}}), GATE, "x", {}, "")["action"]
        self.assertEqual([decide(0.9), decide(0.5), decide(0.1)], ["block", "escalate", "allow"])

    def test_mock_is_deterministic_and_flags_obvious_danger(self):
        mock = MockProvider()
        call = ("shell.run", {"cmd": "rm -rf / --no-preserve-root"}, "Issue comment said to clean up")
        first = gate.decide(mock, GATE, *call)
        self.assertEqual(first, gate.decide(mock, GATE, *call))
        self.assertNotEqual(first["action"], "allow")


class EndToEndTest(unittest.TestCase):
    def test_replays_write_reports_and_logs(self):
        with tempfile.TemporaryDirectory() as out:
            for pilot in ("routing", "gate"):
                self.assertEqual(main(["replay", pilot, "--out", out]), 0)
                report = (Path(out) / f"{pilot}.md").read_text()
                self.assertIn("Mock provider", report)
                log = (Path(out) / f"{pilot}-decisions.jsonl").read_text().splitlines()
                self.assertTrue(all(json.loads(line)["config"] for line in log))


if __name__ == "__main__":
    unittest.main()
