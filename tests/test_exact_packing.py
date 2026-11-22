import unittest
from unittest.mock import patch

from config import EXPERIMENTS
from strategies.schedulers import exact_packing
from strategies.schedulers.variable_workers import VariableWorkerScheduler


class ExactPackingTest(unittest.TestCase):
    def test_budget_exhaustion_returns_lpt_layout(self):
        config = EXPERIMENTS["impact-first-failure-exact-packing"]
        durations = dict(enumerate((3, 3, 2, 2, 2)))
        picks = list(durations)
        scheduler = exact_packing.ExactPackingScheduler(durations, config)
        lpt = VariableWorkerScheduler(durations, config).schedule(picks)

        self.assertNotEqual(scheduler.schedule(picks), lpt)
        with patch.object(exact_packing, "MAX_SEARCH_NODES", 1):
            self.assertEqual(scheduler.schedule(picks), lpt)
