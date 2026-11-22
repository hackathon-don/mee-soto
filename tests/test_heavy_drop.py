import unittest
from types import SimpleNamespace
from unittest.mock import patch

from strategies.selectors.impact import selector
from strategies.selectors.impact_heavy_drop import HeavyDropImpact
from strategies.selectors.impact_stacked_maps import StackedMapsImpact
from strategies.selectors.impact_verify_heavy import VerifyHeavyImpact


class HeavyDropTest(unittest.TestCase):
    def test_only_direct_failure_evidence_keeps_heavy_tests(self):
        selector = object.__new__(HeavyDropImpact)
        selector._must_fail = {"patch": {1224}}
        plans = [SimpleNamespace(failed={1106}), SimpleNamespace(failed=set())]
        picks = [("patch", [1, 1106, 1224, 1490]), ("fallback", [1, 1106, 1224, 1490])]
        with patch.object(StackedMapsImpact, "_selected", return_value=iter(picks)):
            self.assertEqual(list(selector._selected(plans)), [("patch", [1, 1106, 1224]), ("fallback", [1])])


class VerifyHeavyTest(unittest.TestCase):
    def test_only_a_completed_passing_subset_can_remove_a_heavy_pick(self):
        experiment = object.__new__(VerifyHeavyImpact)
        for verdict in ("pass", "fail", "incomplete", "no subset", "must fail"):
            with self.subTest(verdict=verdict):
                plan = selector.Plan(verdict)
                if verdict != "no subset":
                    plan.subsets[1106] = [0]
                must = [1106] if verdict == "must fail" else []

                def choose(*args):
                    plan.include = {1106, 1490}
                    plan.deferred = {1224}

                def run_all(*args):
                    if plan.run and verdict in ("pass", "fail"):
                        plan.diagnostics["timed"].append((1106, 0.5))
                    if verdict == "fail":
                        plan.failed.add(1106)

                def select(*args):
                    selector.choose(plan, [], must, set(), {}, {1106: 500}, {}, set())
                    self.assertEqual(plan.run, [] if verdict in ("no subset", "must fail") else [1106])
                    self.assertEqual(plan.deferred, {1224})
                    selector.run_all([plan], {}, {}, 0)
                    return plan.include | plan.failed

                with patch.object(selector, "choose", choose), patch.object(selector, "run_all", run_all):
                    with patch.object(StackedMapsImpact, "select", side_effect=select):
                        picks = experiment.select([])
                    self.assertIs(selector.choose, choose)
                    self.assertIs(selector.run_all, run_all)
                self.assertEqual(picks, {1490} if verdict == "pass" else {1106, 1490})
