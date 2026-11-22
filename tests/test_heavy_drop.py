import unittest
from types import SimpleNamespace
from unittest.mock import patch

from strategies.selectors.impact_heavy_drop import HeavyDropImpact
from strategies.selectors.impact_stacked_maps import StackedMapsImpact


class HeavyDropTest(unittest.TestCase):
    def test_only_direct_failure_evidence_keeps_heavy_tests(self):
        selector = object.__new__(HeavyDropImpact)
        selector._must_fail = {"patch": {1224}}
        plans = [SimpleNamespace(failed={1106}), SimpleNamespace(failed=set())]
        picks = [("patch", [1, 1106, 1224, 1490]), ("fallback", [1, 1106, 1224, 1490])]
        with patch.object(StackedMapsImpact, "_selected", return_value=iter(picks)):
            self.assertEqual(list(selector._selected(plans)), [("patch", [1, 1106, 1224]), ("fallback", [1])])
