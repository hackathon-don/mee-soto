import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from config import DEFAULT_CONFIG, Config
from main import main
from strategies.schedulers.lpt import LptScheduler
from strategies.selectors import Patch
from strategies.selectors.all_tests import AllTestsSelector


CATALOG = [
    {"number": 4, "id": "test_a", "file": "test.py", "seconds": 8.0},
    {"number": 2, "id": "test_b", "file": "test.py", "seconds": 5.0},
    {"number": 7, "id": "test_c", "file": "test.py", "seconds": 3.0},
    {"number": 1, "id": "test_d", "file": "test.py", "seconds": 3.0},
]


class StrategyTest(unittest.TestCase):
    def test_config_defaults_and_immutability(self):
        self.assertIs(DEFAULT_CONFIG.selector_cls, AllTestsSelector)
        self.assertIs(DEFAULT_CONFIG.scheduler_cls, LptScheduler)
        self.assertEqual(DEFAULT_CONFIG.workers, 3)
        self.assertEqual(DEFAULT_CONFIG.data_dir, Path("/data"))
        with self.assertRaises(FrozenInstanceError):
            DEFAULT_CONFIG.workers = 8

    def test_all_tests_for_each_commit_in_catalog_order(self):
        selector = AllTestsSelector(CATALOG, DEFAULT_CONFIG)
        result = selector.select([Patch("b", "changed"), Patch("a", "")])
        self.assertEqual(result, {"b": [4, 2, 7, 1], "a": [4, 2, 7, 1]})
        self.assertIsNot(result["a"], result["b"])
        self.assertEqual(selector.select([]), {})
        self.assertEqual(AllTestsSelector([], DEFAULT_CONFIG).select([Patch("a", "")]), {"a": []})

    def test_empty_selection_and_empty_workers(self):
        scheduler = LptScheduler({4: 8.0}, DEFAULT_CONFIG)
        self.assertEqual(scheduler.schedule([]), [])
        self.assertEqual(scheduler.schedule([4]), [[4]])

    def test_selected_ids_once_with_configured_worker_limit(self):
        durations = {test["number"]: test["seconds"] for test in CATALOG}
        for workers in (1, 2, 3, 8):
            for selected in ([], [7], [4, 2, 7, 1], [1, 4]):
                with self.subTest(workers=workers, selected=selected):
                    result = LptScheduler(durations, Config(workers=workers)).schedule(selected)
                    self.assertLessEqual(len(result), workers)
                    self.assertTrue(all(result))
                    flattened = [number for worker in result for number in worker]
                    self.assertCountEqual(flattened, selected)
                    self.assertEqual(len(flattened), len(set(flattened)))

    def test_schedule_depends_only_on_selected_ids_and_their_durations(self):
        scheduler = LptScheduler({4: 8.0, 2: 5.0, 7: 3.0, 1: 1000.0}, Config(workers=2))
        expected = [[4], [2, 7]]
        self.assertEqual(scheduler.schedule([4, 2, 7]), expected)
        scheduler.schedule([1])
        self.assertEqual(scheduler.schedule([4, 2, 7]), expected)
        self.assertEqual(LptScheduler({4: 8.0, 2: 5.0, 7: 3.0}, Config(workers=2)).schedule([4, 2, 7]), expected)

    def test_equal_duration_ties_preserve_input_order(self):
        self.assertEqual(LptScheduler({7: 3.0, 1: 3.0}, DEFAULT_CONFIG).schedule([7, 1]), [[7], [1]])

    def test_unknown_selected_id_raises(self):
        with self.assertRaises(KeyError):
            LptScheduler({}, DEFAULT_CONFIG).schedule([99])


class EntrypointTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        (self.data / "commits").mkdir()
        (self.data / "tests.json").write_text(json.dumps({"tests": CATALOG}))

    def invoke(self, config):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            main(config)
        return out.getvalue(), err.getvalue()

    def test_constructs_once_selects_batch_and_caches_identical_selections(self):
        for commit in ("c", "a", "b"):
            (self.data / "commits" / f"{commit}.patch").write_bytes(b"diff\xff")
        events = []

        class Selector:
            def __init__(self, catalog, config):
                events.append(("selector", catalog, config))

            def select(self, commits):
                events.append(("select", commits))
                return {"a": [4, 2], "b": [4, 2], "c": []}

        class Scheduler:
            def __init__(self, durations, config):
                events.append(("scheduler", durations, config))

            def schedule(self, selected):
                events.append(("schedule", selected))
                return [list(selected)] if selected else []

        config = Config(Selector, Scheduler, data_dir=self.data)
        out, err = self.invoke(config)
        self.assertEqual(events, [
            ("selector", CATALOG, config),
            ("scheduler", {4: 8.0, 2: 5.0, 7: 3.0, 1: 3.0}, config),
            ("select", [Patch(commit, "diff\udcff") for commit in ("a", "b", "c")]),
            ("schedule", (4, 2)),
            ("schedule", ()),
        ])
        self.assertEqual(out, '{"commit": "a", "workers": [[4, 2]]}\n'
                              '{"commit": "b", "workers": [[4, 2]]}\n'
                              '{"commit": "c", "workers": []}\n')
        self.assertEqual(err, "selected 4 tests across 3 commits\n")

    def test_empty_batch_constructs_each_strategy_once(self):
        events = []

        class Selector(AllTestsSelector):
            def __init__(self, catalog, config):
                events.append("selector")
                super().__init__(catalog, config)

        class Scheduler(LptScheduler):
            def __init__(self, durations, config):
                events.append("scheduler")
                super().__init__(durations, config)

        self.assertEqual(self.invoke(Config(Selector, Scheduler, data_dir=self.data)),
                         ("", "selected 0 tests across 0 commits\n"))
        self.assertEqual(events, ["selector", "scheduler"])

    def test_default_strategies_and_empty_catalog(self):
        (self.data / "commits" / "a.patch").write_text("")
        out, err = self.invoke(Config(data_dir=self.data))
        self.assertEqual(json.loads(out), {"commit": "a", "workers": [[4], [2], [7, 1]]})
        self.assertEqual(err, "selected 4 tests across 1 commits\n")
        (self.data / "tests.json").write_text('{"tests": []}')
        self.assertEqual(self.invoke(Config(data_dir=self.data)),
                         ('{"commit": "a", "workers": []}\n', "selected 0 tests across 1 commits\n"))

    def test_missing_catalog_raises(self):
        (self.data / "tests.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.invoke(Config(data_dir=self.data))
