import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import call, patch

import selector
from scheduler import schedule

CATALOG = [
    {"number": 0, "id": "tests/test_a.py::test_a", "file": "tests/test_a.py", "seconds": 1.0},
    {"number": 1, "id": "tests/test_b.py::test_b", "file": "tests/test_b.py", "seconds": 2.0},
]


class InterfaceTests(unittest.TestCase):
    def test_starter_selects_every_catalog_number(self):
        self.assertEqual(
            selector.select("diff --git a/sqlglot/a.py b/sqlglot/a.py", CATALOG), [0, 1]
        )

    def test_scheduler_uses_only_selected_numbers_and_preserves_ties(self):
        durations = {0: 1.0, 1: 4.0, 2: 2.0, 3: 3.0, 4: 4.0, 5: 100.0}
        self.assertEqual(schedule([2, 4, 1, 3, 0], durations), [[4, 0], [1], [3, 2]])

    def test_scheduler_omits_empty_workers(self):
        self.assertEqual(schedule([], {}), [])
        self.assertEqual(schedule([1], {1: 2.0}), [[1]])

    def test_entrypoint_writes_one_schedule_per_patch(self):
        first_diff = "diff --git a/sqlglot/a.py b/sqlglot/a.py\n"
        second_diff = "diff --git a/sqlglot/b.py b/sqlglot/b.py\n"
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / "commits").mkdir()
            (data / "commits" / "b.patch").write_text(second_diff)
            (data / "commits" / "a.patch").write_text(first_diff)
            (data / "tests.json").write_text(json.dumps({"tests": CATALOG}))

            with (
                patch.object(selector, "DATA", data),
                patch.object(selector, "select", side_effect=[[1], []]) as select,
                redirect_stdout(io.StringIO()) as stdout,
                redirect_stderr(io.StringIO()),
            ):
                selector.main()

        self.assertEqual(select.call_args_list, [call(first_diff, CATALOG), call(second_diff, CATALOG)])
        self.assertEqual(
            [json.loads(line) for line in stdout.getvalue().splitlines()],
            [{"commit": "a", "workers": [[1]]}, {"commit": "b", "workers": []}],
        )
