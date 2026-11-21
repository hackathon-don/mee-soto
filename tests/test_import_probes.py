import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from strategies.selectors.impact.selector import Plan, run_all


class ImportProbeTests(unittest.TestCase):
    def make_plan(self, source, *, include=(), mask=1 << 4):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "probe_target.py").write_text(source)
        plan = Plan("probe")
        plan.root = directory.name
        plan.include.update(include)
        plan.pending_imports = {"probe_target": mask}
        return plan, root

    def run_plan(self, plan, timeout=2.0, deadline=None):
        with patch("strategies.selectors.impact.selector.IMPORT_PROBE_TIMEOUT", timeout):
            run_all([plan], {}, {}, time.time() + 5 if deadline is None else deadline)

    def test_failing_import_is_selected_without_runtime_candidates(self):
        plan, _ = self.make_plan("raise RuntimeError('broken import')\n")
        self.assertEqual(plan.run, [])
        self.run_plan(plan)
        self.assertFalse(plan.pending_imports)
        self.assertEqual(plan.failed_imports, {4})
        self.assertEqual(sorted(plan.include | plan.failed_imports), [4])

    def test_successful_import_only_probe_preserves_ordinary_selection(self):
        plan, root = self.make_plan(
            "from pathlib import Path; Path('marker').write_text('loaded')\n",
            include={8},
        )
        marker = root / "marker"
        self.run_plan(plan)
        self.assertTrue(marker.exists())
        self.assertFalse(plan.pending_imports)
        self.assertFalse(plan.failed_imports)
        self.assertEqual(plan.include, {8})
        self.assertEqual(plan.run, [])

    def test_timed_out_import_falls_back_to_all_dependents(self):
        plan, _ = self.make_plan("import time; time.sleep(5)\n", mask=(1 << 2) | (1 << 4))
        self.run_plan(plan, timeout=0.05)
        self.assertFalse(plan.pending_imports)
        self.assertEqual(plan.failed_imports, {2, 4})

    def test_child_exit_without_probe_result_keeps_pending_dependents(self):
        plan, _ = self.make_plan("import os; os._exit(7)\n")
        self.run_plan(plan)
        self.assertEqual(plan.pending_imports, {"probe_target": 1 << 4})
        self.assertEqual(plan.include, {4})
        self.assertFalse(plan.failed_imports)

    def test_probe_not_started_before_deadline_keeps_dependents(self):
        plan, root = self.make_plan("from pathlib import Path; Path('marker').write_text('loaded')\n")
        marker = root / "marker"
        self.run_plan(plan, deadline=time.time() - 1)
        self.assertEqual(plan.pending_imports, {"probe_target": 1 << 4})
        self.assertEqual(plan.include, {4})
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
