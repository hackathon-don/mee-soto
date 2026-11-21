import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from config import Config
from main import main
from strategies.schedulers.lpt import LptScheduler

CATALOG = [
    {"number": 1, "id": "test_a", "file": "test.py", "seconds": 8.0},
    {"number": 2, "id": "test_b", "file": "test.py", "seconds": 5.0},
    {"number": 3, "id": "test_c", "file": "test.py", "seconds": 3.0},
]


class ModeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        (self.data / "commits").mkdir()
        for commit in ("foo", "bar"):
            (self.data / "commits" / f"{commit}.patch").write_text(commit)
        (self.data / "tests.json").write_text(json.dumps({"tests": CATALOG}))
        self.events = []
        events = self.events

        class Selector:
            def __init__(self, catalog, config):
                events.append("selector")

            def select(self, commits):
                events.append(("select", [patch.commit for patch in commits]))
                return {"bar": [1, 3, 2], "foo": []}

        class Scheduler(LptScheduler):
            def __init__(self, durations, config):
                events.append("scheduler")
                super().__init__(durations, config)

        self.config = Config(Selector, Scheduler, workers=2, data_dir=Path("/nonexistent"))

    def invoke(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            main(self.config, ["--data", str(self.data), *argv])
        return out.getvalue()

    def write(self, name, rows):
        path = self.data / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def test_e2e_is_the_default_mode(self):
        out = self.invoke()
        self.assertEqual(out, '{"commit": "bar", "workers": [[1], [2, 3]]}\n'
                              '{"commit": "foo", "workers": []}\n')
        self.assertEqual(self.events, ["selector", "scheduler", ("select", ["bar", "foo"])])

    def test_select_runs_only_the_selector(self):
        out = self.invoke("select")
        self.assertEqual(out, '{"commit": "bar", "selected": [1, 3, 2]}\n'
                              '{"commit": "foo", "selected": []}\n')
        self.assertEqual(self.events, ["selector", ("select", ["bar", "foo"])])

    def test_replaying_saved_picks_matches_e2e_without_selecting(self):
        picks = self.data / "picks.jsonl"
        picks.write_text(self.invoke("select"))
        self.events.clear()
        replay = self.invoke("schedule", "--picks", str(picks))
        self.assertEqual(self.events, ["scheduler"])
        self.assertEqual(replay, self.invoke())

    def test_ideal_schedules_the_failing_tests(self):
        answers = self.data / "answers.json"
        answers.write_text(json.dumps({"commits": {
            "foo": {"failing": [], "kind": "code"},
            "bar": {"failing": [2, 3], "kind": "code"},
        }}))
        out = self.invoke("schedule", "--ideal", str(answers))
        self.assertEqual(out, '{"commit": "bar", "workers": [[2], [3]]}\n'
                              '{"commit": "foo", "workers": []}\n')
        self.assertEqual(self.events, ["scheduler"])

    def test_invalid_or_incomplete_picks_are_rejected_before_scheduling(self):
        bar = {"commit": "bar", "selected": [1]}
        foo = {"commit": "foo", "selected": []}
        cases = [
            [bar],
            [bar, bar, foo],
            [bar, {**foo, "commit": "unknown"}],
            [bar, {**foo, "selected": [4]}],
            [bar, {**foo, "selected": [1, 1]}],
            [bar, {**foo, "selected": [True]}],
            [bar, {"commit": "foo"}],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                picks = self.write("picks.jsonl", rows)
                with self.assertRaises(SystemExit) as raised:
                    self.invoke("schedule", "--picks", str(picks))
                self.assertEqual(raised.exception.code, 2)
        with self.assertRaises(SystemExit):
            self.invoke("schedule", "--picks", str(self.data / "missing.jsonl"))
        self.assertEqual(self.events, ["scheduler"] * (len(cases) + 1))

    def test_schedule_requires_exactly_one_source(self):
        for argv in (["schedule"], ["schedule", "--picks", "a", "--ideal", "b"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.invoke(*argv)


if __name__ == "__main__":
    unittest.main()
