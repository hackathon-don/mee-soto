import contextlib
from decimal import Decimal
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.ci_selection_report import selection_errors
from tools import ci_score


class SelectionReportTests(unittest.TestCase):
    def test_selection_errors_use_public_labels_on_host(self):
        with tempfile.TemporaryDirectory() as directory:
            answer = Path(directory) / "public.json"
            answer.write_text('{"commits":{"a":{"failing":[1,2]},"b":{"failing":[]}}}')
            report = selection_errors({"a": {1, 3}, "b": {4}}, answer)
            self.assertIn("Missed patches:** 1/2", report)
            self.assertIn("Failing tests omitted:** 1", report)
            self.assertIn("Scheduled without a recorded failure:** 2", report)
            self.assertIn("- `a`: 2", report)

    def test_multi_experiment_ci_retains_score_sections(self):
        Score = ci_score.Score
        Run = ci_score.DiagnosticRun
        first = Run(Score(Decimal(11), Decimal(8), 0), None, {"p": set()})
        mid = Run(Score(Decimal(10), Decimal(5), 0), None, {"p": {1}})
        last = Run(Score(Decimal(10), Decimal(1), 0), None, {"p": set()})
        baseline = Score(Decimal(100), Decimal(1), 0)
        with tempfile.TemporaryDirectory() as directory:
            response = io.StringIO()
            with (
                patch.object(sys, "argv", ["ci_score", "a" * 40, "--selection-errors"]),
                patch.object(ci_score, "added_experiments", return_value=["a", "b"]),
                patch.object(ci_score.subprocess, "check_output", return_value="c" * 40),
                patch.object(ci_score, "score_with_schedule", side_effect=[first, last, mid] * 2),
                patch.object(ci_score, "score", return_value=baseline),
                patch.object(ci_score, "selection_errors", return_value="#### Selection errors (median run)"),
                contextlib.redirect_stdout(response),
            ):
                ci_score.main()
            score = response.getvalue()
            self.assertEqual(score.count("| baseline | 100.00 s | 1.00 s | 0 | 101.00 s |"), 2)
            self.assertEqual(score.count("#### Selection errors (median run)"), 2)
            for name in ("a", "b"):
                self.assertIn(f'Experiment: "{name}"', score)
            self.assertIn("Machine: linux/amd64  \nCommit hash: " + "c" * 40, score)


if __name__ == "__main__":
    unittest.main()
