import gzip
import json
import tempfile
import unittest
from pathlib import Path

from strategies.selectors.impact_fixture_entries import EntryMap, _narrowable, load_entry_map

DATA = {
    "codes": [
        ["sqlglot/a.py", [[1, 1], [40, 40]]],  # module body: def lines only
        ["sqlglot/a.py", [[2, 5]]],  # f: entered by entry 1 of the test
        ["sqlglot/a.py", [[10, 12]]],  # g: runs for every entry (setUp)
        ["sqlglot/a.py", [[20, 22]]],  # h: never recorded
    ],
    "sets": ["2"],
    "modules": {"tests.test_x": {"import": [0], "shared_loads": []}},
    "tests": {
        "7": {
            "module": "tests.test_x",
            "files": [["x.sql", 3, "sha"]],
            "whole": [2],
            "entries": {"1": 0},
        }
    },
}


class EntryMapTest(unittest.TestCase):
    def setUp(self):
        self.map = EntryMap(DATA)

    def test_entry_only_code_keeps_reaching_entries(self):
        files, kept, total = self.map.keep(7, [("sqlglot/a.py", 3, 4)])
        self.assertEqual(files, {"x.sql": (3, frozenset({1}))})
        self.assertEqual((kept, total), (1, 3))

    def test_module_body_lines_do_not_cover_function_bodies(self):
        self.assertIsNotNone(self.map.keep(7, [("sqlglot/a.py", 2, 5)]))
        self.assertIsNone(self.map.keep(7, [("sqlglot/a.py", 1, 1)]))

    def test_whole_test_code_runs_everything(self):
        self.assertIsNone(self.map.keep(7, [("sqlglot/a.py", 11, 11)]))

    def test_unseen_code_runs_everything(self):
        self.assertIsNone(self.map.keep(7, [("sqlglot/a.py", 21, 21)]))
        self.assertIsNone(self.map.keep(7, [("sqlglot/b.py", 3, 3)]))

    def test_unrecorded_test_runs_everything(self):
        self.assertIsNone(self.map.keep(8, [("sqlglot/a.py", 3, 4)]))


class LoadTest(unittest.TestCase):
    def test_stale_or_missing_map_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json.gz"
            self.assertIsNone(load_entry_map([], path))
            with gzip.open(path, "wt") as fh:
                json.dump({**DATA, "format": -1}, fh)
            self.assertIsNone(load_entry_map([], path))


class NarrowableTest(unittest.TestCase):
    def test_only_existing_sqlglot_sources(self):
        edit = "diff --git a/sqlglot/a.py b/sqlglot/a.py\n--- a/sqlglot/a.py\n+++ b/sqlglot/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        self.assertTrue(_narrowable(edit))
        self.assertFalse(_narrowable(edit.replace("sqlglot/a.py", "tests/fixtures/a.sql")))
        new = "diff --git a/sqlglot/n.py b/sqlglot/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/sqlglot/n.py\n@@ -0,0 +1 @@\n+y\n"
        self.assertFalse(_narrowable(new))


if __name__ == "__main__":
    unittest.main()
