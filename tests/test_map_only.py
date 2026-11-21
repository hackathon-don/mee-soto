from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config import Config
from strategies.selectors import Patch
from strategies.selectors.all_tests import AllTestsSelector
from strategies.selectors import map_format, map_only
from tools import build_file_map


def catalog_fixture():
    return [
        {"number": 0, "id": "tests/test_a.py::test_a", "file": "tests/test_a.py", "seconds": 1.0},
        {"number": 1, "id": "tests/test_b.py::test_b", "file": "tests/test_b.py", "seconds": 2.0},
        {"number": 2, "id": "tests/test_c.py::test_c", "file": "tests/test_c.py", "seconds": 3.0},
    ]


def map_payload(
    catalog,
    files,
    directories=None,
    baseline_fail=None,
    missing=None,
    incomplete=None,
    schema=None,
    source=None,
):
    records = map_format.catalog_records(catalog)
    digest = map_format.catalog_sha256(records)
    baseline_fail = [] if baseline_fail is None else baseline_fail
    missing = [] if missing is None else missing
    incomplete = [] if incomplete is None else incomplete
    always = set(baseline_fail) | set(missing) | set(incomplete)
    results = {
        "passed": [record["number"] for record in records if record["number"] not in always],
        "baseline_fail": baseline_fail,
        "missing": missing,
        "incomplete": incomplete,
    }
    return {
        "schema": map_format.SCHEMA_VERSION if schema is None else schema,
        "source": dict(map_format.SOURCE) if source is None else source,
        "catalog": records,
        "catalog_sha256": digest,
        "counts": {
            "catalog": len(records),
            **{key: len(values) for key, values in results.items()},
        },
        "results": results,
        "files": files,
        "directories": {} if directories is None else directories,
    }


class MapSelectorTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog_fixture()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.map_path = Path(self.tmp.name) / "file_map.json.gz"

    def write_map(self, payload):
        build_file_map.write_map(self.map_path, payload)

    def selector(
        self,
        files,
        directories=None,
        baseline_fail=None,
        missing=None,
        incomplete=None,
        schema=None,
        catalog=None,
        source=None,
    ):
        catalog = self.catalog if catalog is None else catalog
        payload = map_payload(
            catalog,
            files,
            directories,
            baseline_fail,
            missing,
            incomplete,
            schema,
            source,
        )
        self.write_map(payload)
        patcher = mock.patch.object(map_only, "MAP_PATH", self.map_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return map_only.MapSelector(catalog, Config())

    def test_known_python_fixture_and_hunk_header_union(self):
        selector = self.selector(
            {"sqlglot/parser.py": [0], "tests/fixtures/example.sql": [1], "fake.py": [2]}
        )
        diff = (
            "diff --git a/sqlglot/parser.py b/sqlglot/parser.py\n"
            "index 1234567..7654321 100644\n"
            "--- a/sqlglot/parser.py\n"
            "+++ b/sqlglot/parser.py\n"
            "@@ -1,1 +1,1 @@\n"
            "--- removed SQL comment\n"
            "+++ added SQL comment\n"
            "diff --git a/tests/fixtures/example.sql b/tests/fixtures/example.sql\n"
            "index 1111111..2222222 100644\n"
            "--- a/tests/fixtures/example.sql\n"
            "+++ b/tests/fixtures/example.sql\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        self.assertEqual(
            map_only.changed_paths(diff),
            ["sqlglot/parser.py", "tests/fixtures/example.sql"],
        )
        self.assertEqual(selector.select([Patch("known", diff)])["known"], [0, 1])

    def test_pure_rename_and_delete(self):
        selector = self.selector({"old.py": [0], "new.py": [1], "deleted.py": [2]})
        rename = (
            "diff --git a/old.py b/new.py\n"
            "similarity index 100%\n"
            "rename from old.py\n"
            "rename to new.py\n"
        )
        delete = (
            "diff --git a/deleted.py b/deleted.py\n"
            "deleted file mode 100644\n"
            "index 1234567..0000000\n"
            "--- a/deleted.py\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-deleted\n"
        )
        self.assertEqual(map_only.changed_paths(rename), ["new.py", "old.py"])
        self.assertEqual(map_only.changed_paths(delete), ["deleted.py"])
        self.assertEqual(
            selector.select([Patch("rename", rename), Patch("delete", delete)]),
            {"rename": [0, 1], "delete": [2]},
        )

    def test_parent_directory_dependencies_are_immediate_only(self):
        selector = self.selector(
            {"pkg/sub/file.py": [0], "top.py": [1]},
            directories={"pkg": [1], "pkg/sub": [2], ".": [2]},
        )
        nested = (
            "diff --git a/pkg/sub/file.py b/pkg/sub/file.py\n"
            "--- a/pkg/sub/file.py\n+++ b/pkg/sub/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        top_level = (
            "diff --git a/top.py b/top.py\n"
            "--- a/top.py\n+++ b/top.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        self.assertEqual(selector.select([Patch("nested", nested)])["nested"], [0, 2])
        self.assertEqual(selector.select([Patch("top", top_level)])["top"], [1, 2])

    def test_unmapped_and_empty_entries_fall_back(self):
        selector = self.selector({"known.py": [0], "empty.py": []})
        unknown = (
            "diff --git a/new.py b/new.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new.py\n"
            "@@ -0,0 +1 @@\n"
            "+new\n"
        )
        known = (
            "diff --git a/known.py b/known.py\n"
            "--- a/known.py\n+++ b/known.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        empty = (
            "diff --git a/empty.py b/empty.py\n"
            "--- a/empty.py\n+++ b/empty.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        self.assertEqual(selector.select([Patch("unknown", unknown)])["unknown"], [0, 1, 2])
        self.assertEqual(selector.select([Patch("mixed", known + unknown)])["mixed"], [0, 1, 2])
        self.assertEqual(selector.select([Patch("empty", empty)])["empty"], [0, 1, 2])

    def test_empty_quoted_binary_and_malformed_diffs_fall_back(self):
        selector = self.selector({"file.py": [0]})
        long_count = "9" * 5000
        oversized_hunk = (
            "diff --git a/file.py b/file.py\n"
            "--- a/file.py\n+++ b/file.py\n"
            f"@@ -1,{long_count} +1 @@\n-old\n+new\n"
        )
        diffs = [
            "",
            "not a git diff",
            'diff --git "a/file name.py" "b/file name.py"\n',
            "diff --git a/file.py b/file.py\nBinary files a/file.py and b/file.py differ\n",
            "diff --cc file.py\n",
            "diff --git a/file.py b/file.py\nindex 123..456\n",
            "diff --git a/file.py b/file.py\n--- a/file.py\n+++ b/file.py\n@@ malformed @@\n",
            "diff --git a/file.py b/file.py\n--- a/file.py\n+++ b/file.py\n@@ -1,3 +1,1 @@\n-old\n+new\n",
            oversized_hunk,
            (
                "diff --git a/file.py b/file.py\n"
                "rename from wrong.py\nrename to other.py\n"
                "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
            ),
        ]
        self.assertIsNone(map_only.changed_paths(oversized_hunk))
        results = selector.select([Patch(str(i), diff) for i, diff in enumerate(diffs)])
        self.assertTrue(all(selected == [0, 1, 2] for selected in results.values()))

    def test_always_selected_ids_and_runtime_starts_no_processes(self):
        selector = self.selector(
            {"file.py": [1]}, baseline_fail=[2], missing=[0], incomplete=[]
        )
        diff = (
            "diff --git a/file.py b/file.py\n"
            "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("runtime subprocess")):
            self.assertEqual(selector.select([Patch("known", diff)])["known"], [0, 1, 2])

    def test_catalog_identity_is_exact_but_durations_may_change(self):
        selector = self.selector({"file.py": [1]})
        changed_duration = [dict(test, seconds=99.0) for test in self.catalog]
        duration_records = map_format.catalog_records(changed_duration)
        duration_selector = map_only.MapSelector(changed_duration, Config())
        diff = (
            "diff --git a/file.py b/file.py\n"
            "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        self.assertEqual(selector.select([Patch("known", diff)])["known"], [1])
        self.assertEqual(duration_selector.select([Patch("duration", diff)])["duration"], [1])
        self.assertEqual(duration_records, map_format.catalog_records(self.catalog))
        for field, value in (
            ("number", 9),
            ("id", "tests/test_b.py::renamed"),
            ("file", "tests/other.py"),
        ):
            changed_identity = [dict(test) for test in self.catalog]
            changed_identity[1][field] = value
            identity_selector = map_only.MapSelector(changed_identity, Config())
            self.assertEqual(
                identity_selector.select([Patch("mismatch", diff)])["mismatch"],
                identity_selector.numbers,
            )

    def test_invalid_map_metadata_and_all_tests_default(self):
        diff = (
            "diff --git a/file.py b/file.py\n"
            "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        invalid_schema = self.selector({"file.py": [0]}, schema=1)
        invalid_source = self.selector({"file.py": [0]}, source={"repository": "untrusted"})
        bad_status = map_payload(self.catalog, {"file.py": [0]})
        bad_status["results"]["missing"] = [999]
        self.write_map(bad_status)
        with mock.patch.object(map_only, "MAP_PATH", self.map_path):
            invalid_status = map_only.MapSelector(self.catalog, Config())
        for selector in (invalid_schema, invalid_source, invalid_status):
            self.assertEqual(selector.select([Patch("invalid", diff)])["invalid"], [0, 1, 2])
        invalid_file_root = self.selector({"file.py": [0], ".": [1]})
        self.assertEqual(
            invalid_file_root.select([Patch("invalid-root", diff)])["invalid-root"],
            [0, 1, 2],
        )
        self.assertIs(Config().selector_cls, AllTestsSelector)

    def test_missing_and_corrupt_maps_fall_back(self):
        diff = (
            "diff --git a/file.py b/file.py\n"
            "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        with mock.patch.object(map_only, "MAP_PATH", self.map_path):
            missing = map_only.MapSelector(self.catalog, Config())
            self.assertEqual(missing.select([Patch("missing", diff)])["missing"], [0, 1, 2])
            self.map_path.write_bytes(b"not a gzip map")
            corrupt = map_only.MapSelector(self.catalog, Config())
            self.assertEqual(corrupt.select([Patch("corrupt", diff)])["corrupt"], [0, 1, 2])

    def test_schema2_write_is_deterministic(self):
        data = map_payload(self.catalog, {"file.py": [0]})
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.gz"
            second = Path(directory) / "second.gz"
            build_file_map.write_map(first, data)
            build_file_map.write_map(second, data)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_bad_child_result_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            result_path.write_text("{", encoding="utf-8")
            self.assertIsNone(build_file_map._load_child_result(result_path, 0))


_PYTEST_AVAILABLE = importlib.util.find_spec("pytest") is not None
_MONITORING_AVAILABLE = sys.version_info >= (3, 12) and hasattr(sys, "monitoring")


@unittest.skipUnless(_PYTEST_AVAILABLE and _MONITORING_AVAILABLE, "requires Python 3.12+ and pytest")
class FileMapCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "sqlglot"
        (self.root / "tests" / "fixtures").mkdir(parents=True)
        (self.root / "sqlglot").mkdir()
        (self.root / "sqlglot" / "__init__.py").write_text("", encoding="utf-8")

    def catalog(self, *tests):
        return [
            {
                "number": number,
                "id": f"tests/test_sample.py::{test_name}",
                "file": "tests/test_sample.py",
                "seconds": seconds,
            }
            for number, test_name, seconds in tests
        ]

    def build(self, catalog, **kwargs):
        options = {"jobs": 1, "budget_seconds": 20, "timeout_seconds": 5}
        options.update(kwargs)
        return build_file_map.build_map(catalog, self.root, **options)

    def test_import_function_fixture_and_directory_reads(self):
        (self.root / "sqlglot" / "helper.py").write_text(
            "VALUE = 40\n\ndef value():\n    return VALUE + 2\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "fixtures" / "payload.txt").write_text("fixture", encoding="utf-8")
        (self.root / "tests" / "test_sample.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "from sqlglot.helper import value\n"
            "def test_sample():\n"
            "    fixture = Path(__file__).parent / 'fixtures' / 'payload.txt'\n"
            "    os.chdir(fixture.parent)\n"
            "    assert value() == 42\n"
            "    assert Path('payload.txt').read_text() == 'fixture'\n"
            "    assert list(Path('.').iterdir())\n",
            encoding="utf-8",
        )
        data = self.build(self.catalog((0, "test_sample", 1.0)))
        self.assertEqual(data["results"]["passed"], [0])
        self.assertIn("tests/test_sample.py", data["files"])
        self.assertIn("sqlglot/helper.py", data["files"])
        self.assertIn("tests/fixtures/payload.txt", data["files"])
        self.assertIn("tests/fixtures", data["directories"])
        self.assertEqual(data["counts"]["catalog"], 1)

    def test_child_process_failure_timeout_and_missing_are_always_selected(self):
        (self.root / "tests" / "test_sample.py").write_text(
            "import subprocess, sys, time\n"
            "def test_child():\n"
            "    subprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
            "def test_failure():\n"
            "    assert False\n"
            "def test_slow():\n"
            "    time.sleep(2)\n",
            encoding="utf-8",
        )
        child = self.build(self.catalog((0, "test_child", 1.0)))
        self.assertEqual(child["results"]["incomplete"], [0])
        failure = self.build(self.catalog((1, "test_failure", 1.0)))
        self.assertEqual(failure["results"]["baseline_fail"], [1])
        timeout = self.build(self.catalog((2, "test_slow", 1.0)), timeout_seconds=0.1)
        self.assertEqual(timeout["results"]["incomplete"], [2])
        limited = self.build(
            self.catalog((3, "test_failure", 2.0), (4, "test_slow", 1.0)),
            limit=1,
        )
        self.assertEqual(limited["results"]["missing"], [4])
        self.assertEqual(limited["counts"]["catalog"], 2)

    def test_skipped_and_uncollected_tests_are_incomplete(self):
        (self.root / "tests" / "test_sample.py").write_text(
            "import pytest\n"
            "def test_skipped():\n"
            "    pytest.skip('not measured')\n"
            "def test_other():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        skipped = self.build(self.catalog((0, "test_skipped", 1.0)))
        self.assertEqual(skipped["results"]["incomplete"], [0])
        self.assertEqual(skipped["reason_counts"]["incomplete:skipped"], 1)
        uncollected = self.build(self.catalog((1, "test_missing", 1.0)))
        self.assertEqual(uncollected["results"]["incomplete"], [1])
        self.assertEqual(uncollected["reason_counts"]["incomplete:not_collected"], 1)


if __name__ == "__main__":
    unittest.main()
