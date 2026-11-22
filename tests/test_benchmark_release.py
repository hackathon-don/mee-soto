from decimal import Decimal
from pathlib import Path
import io
from contextlib import redirect_stderr
import subprocess
import unittest
from unittest.mock import patch

from tools import benchmark_release as release
from tools.ci_score import DiagnosticRun, Score


def row(total, misses=0):
    return DiagnosticRun(Score(Decimal(total), Decimal(0), misses), None, None)


class BenchmarkReleaseTests(unittest.TestCase):
    def test_enumerates_every_registered_name_and_runs_baseline_only_three_times(self):
        names = ["baseline", "new-but-not-in-pr", "other"]
        with patch.object(release, "experiment_names", return_value=names) as source:
            self.assertEqual(release.registered_at("a" * 40), names)
        source.assert_called_once_with("a" * 40)
        calls = []

        def fake(name, *, cpu_diagnostics):
            calls.append((name, cpu_diagnostics))
            return row("1.0")

        with redirect_stderr(io.StringIO()):
            result = release.collect(names, fake)
        self.assertEqual(calls, [(name, True) for name in names for _ in range(3)])
        self.assertEqual(list(result), names)
        for invalid in (["map"], ["baseline", "same", "same"], ["baseline", "bad name"]):
            with patch.object(release, "experiment_names", return_value=invalid):
                with self.assertRaises(ValueError):
                    release.registered_at("b" * 40)

    def test_rank_is_exact_and_breaks_ties_by_name(self):
        results = {
            "beta": [row("1.0011"), row("2"), row("1")],
            "worse": [row("1.0012")] * 3,
            "alpha": [row("1.0011")] * 3,
            "baseline": [row("0.0")] * 3,
        }
        self.assertEqual(release.rank(results), ["alpha", "beta", "worse"])
        record = release.make_record("a" * 40, results)
        self.assertTrue(record["complete"])
        self.assertEqual(record["winner"], "alpha")
        self.assertEqual(record["experiments"]["alpha"]["median_total_seconds"], "1.0011")
        self.assertFalse(record["experiments"]["baseline"]["eligible"])

    def test_no_release_when_no_eligible_experiment(self):
        results = {"baseline": [row("100")] * 3, "missed": [row("1"), row("1"), row("301", 1)]}
        record = release.make_record("a" * 40, results)
        self.assertTrue(record["complete"])
        self.assertEqual(record["ranking"], [])
        self.assertIsNone(record["winner"])
        self.assertIn("no non-baseline experiment", release.render_markdown(record, results))

    def test_any_incomplete_batch_blocks_release_but_keeps_successful_rows(self):
        results = {
            "baseline": [row("100")] * 3, "eligible": [row("2")] * 3,
            "interrupted": [None, row("1"), row("1")],
        }
        record = release.make_record("a" * 40, results)
        self.assertFalse(record["complete"])
        self.assertIsNone(record["winner"])
        self.assertEqual(record["ranking"], ["eligible"])
        self.assertEqual(record["experiments"]["interrupted"]["runs"][0], {"status": "failed"})
        self.assertIn("No release: the benchmark batch is incomplete.", release.render_markdown(record, results))

    def test_all_three_attempts_are_recorded_after_failure(self):
        with patch("tools.benchmark_release.print", create=True):
            from unittest.mock import Mock
            scorer = Mock(side_effect=[subprocess.CalledProcessError(1, "make"), row("2"), row("3")])
            result = release.collect(["failed"], scorer)
        self.assertEqual(scorer.call_count, 3)
        self.assertIsNone(result["failed"][0])
        self.assertEqual(result["failed"][2].score.total, Decimal(3))

    def test_report_reuses_ci_format_and_has_separate_baseline(self):
        results = {"baseline": [row("100")] * 3, "map": [row("1"), row("2"), row("3")]}
        record = release.make_record("a" * 40, results)
        report = release.render_markdown(record, results)
        self.assertIn("## Baseline reference", report)
        self.assertIn('Experiment: "map"', report)
        self.assertIn("| Run | Simulated | CPU | Missed | Total |", report)
        self.assertIn("| 2 of 3 | 2.00 s | 0.00 s | 0 | 2.00 s |", report)
        self.assertIn("| baseline | 100.00 s | 0.00 s | 0 | 100.00 s |", report)
        self.assertIn("#### Selection errors (median run)", report)
        self.assertIn("CPU: 0.00 s total (median-score run)", report)
        self.assertIn("Commit hash: " + "a" * 40, report)
        self.assertEqual(set(record["experiments"]["map"]["runs"][0]),
                         {"status", "simulated_seconds", "cpu_seconds", "missed_patches", "total_seconds"})

    def test_workflow_is_manual_main_only_and_nonpublishing_by_default(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/benchmark-release.yml").read_text()
        benchmark, publish = workflow.split("\n  publish:", 1)
        self.assertIn("workflow_dispatch:", benchmark)
        self.assertIn("type: boolean\n        default: false", benchmark)
        self.assertIn("github.ref == 'refs/heads/main'", benchmark)
        self.assertNotIn("contents: write", benchmark)
        self.assertIn("inputs.publish_release && needs.benchmark.outputs.eligible == 'true'", publish)
        self.assertIn("contents: write", publish)
        self.assertNotIn("actions/checkout", publish)
        self.assertNotIn("make e2e", publish)
        self.assertIn('-f "sha=$GITHUB_SHA"', publish)
        self.assertIn("--verify-tag", publish)


if __name__ == "__main__":
    unittest.main()
