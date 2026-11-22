import contextlib
from decimal import Decimal
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.ci_cpu_report import PatchCpu, parse_cpu, render_cpu
from tools import ci_score


class CpuReportTests(unittest.TestCase):
    def test_keep_or_omit_uses_final_schedule_and_no_double_count(self):
        cpu = parse_cpu(
            'ignored\nRWX_CPU_V1 {"commit":"p","child_cpu":3.00,'
            '"timed":[[11,1.25],[22,0.75]],"incomplete_children":0}\n'
            'RWX_CPU_V1 {"commit":"partial","child_cpu":0,'
            '"timed":[],"incomplete_children":1}\n'
        )
        report = render_cpu("exp", Decimal(5), cpu, {"p": {22, 33}, "partial": set(), "absent": {99}})
        self.assertIn('<summary>Experiment: "exp"<br>CPU: 5.00 s total (median-score run)</summary>', report)
        self.assertIn("0.750s   15.0%", report)
        self.assertIn("1.250s   25.0%", report)
        self.assertIn("2.000s   40.0%", report)
        self.assertIn("| `p` | 2 | 2 | 0.750s | 1.250s | 1.000s | 0 |", report)
        self.assertIn("| `partial` | 0 | 0 | — | — | — | 1 |", report)
        self.assertIn("| `absent` | — | 1 | — | — | — | — |", report)
        self.assertIn("### Per-patch analysis", report)

    def test_missing_or_conflicting_telemetry_is_not_zero(self):
        self.assertIn("unavailable", render_cpu("x", Decimal(1), {}, {"p": set()}))
        cpu = {"p": PatchCpu(Decimal(3), (), 1)}
        self.assertIn("exceed", render_cpu("x", Decimal(1), cpu, {"p": set()}))
        with self.assertRaises(ValueError):
            parse_cpu('RWX_CPU_V1 {"commit":"p","child_cpu":-1,"timed":[],"incomplete_children":0}')

    def test_multi_experiment_ci_retains_score_sections(self):
        Score = ci_score.Score
        Run = ci_score.DiagnosticRun
        first = Run(Score(Decimal(11), Decimal(8), 0), {}, {"p": set()})
        mid = Run(Score(Decimal(10), Decimal(5), 0),
                  {"p": PatchCpu(Decimal(3), ((1, Decimal(2)),), 0)}, {"p": {1}})
        last = Run(Score(Decimal(10), Decimal(1), 0), {}, {"p": set()})
        baseline = Score(Decimal(100), Decimal(1), 0)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cpu.md"
            response = io.StringIO()
            with (
                patch.object(sys, "argv", ["ci_score", "a" * 40, "--cpu-report", str(output)]),
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
                self.assertIn(f'<summary>Experiment: "{name}"<br>CPU: 5.00 s total (median-score run)</summary>', output.read_text())
            self.assertIn("Machine: linux/amd64  \nCommit hash: " + "c" * 40, score)


if __name__ == "__main__":
    unittest.main()
