from __future__ import annotations

import gzip
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config import Config
from strategies.selectors import Patch
from strategies.selectors.all_tests import AllTestsSelector
from strategies.selectors import map_only
from tools import export_file_map


def catalog_fixture():
    return [
        {"number": 0, "id": "tests/test_a.py::test_a", "file": "tests/test_a.py", "seconds": 1.0},
        {"number": 1, "id": "tests/test_b.py::test_b", "file": "tests/test_b.py", "seconds": 2.0},
        {"number": 2, "id": "tests/test_c.py::test_c", "file": "tests/test_c.py", "seconds": 3.0},
    ]


def map_payload(catalog, files, baseline_fail=None, missing=None, schema=None):
    records = map_only.catalog_records(catalog)
    digest = map_only.catalog_sha256(records)
    source = {
        "repository": map_only.DONOR_REPOSITORY,
        "commit": map_only.DONOR_COMMIT,
        "index_path": map_only.DONOR_INDEX_PATH,
        "index_sha256": map_only.DONOR_INDEX_SHA256,
        "sqlglot_commit": map_only.SQLGLOT_COMMIT,
        "catalog_sha256": digest,
    }
    return {
        "schema": map_only.MAP_SCHEMA if schema is None else schema,
        "source": source,
        "catalog": records,
        "catalog_sha256": digest,
        "baseline_fail": [] if baseline_fail is None else baseline_fail,
        "missing": [] if missing is None else missing,
        "files": files,
    }


class MapSelectorTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog_fixture()
        self.digest = map_only.catalog_sha256(map_only.catalog_records(self.catalog))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.map_path = Path(self.tmp.name) / "file_map.json.gz"

    def selector(
        self, files, baseline_fail=None, missing=None, schema=None, catalog=None, source=None
    ):
        catalog = self.catalog if catalog is None else catalog
        payload = map_payload(catalog, files, baseline_fail, missing, schema)
        if source is not None:
            payload["source"] = source
        with gzip.open(self.map_path, "wt", encoding="utf-8") as stream:
            json.dump(payload, stream)
        patcher = mock.patch.object(map_only, "MAP_PATH", self.map_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        digest_patcher = mock.patch.object(map_only, "DONOR_CATALOG_SHA256", self.digest)
        digest_patcher.start()
        self.addCleanup(digest_patcher.stop)
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
            "-old\n"
        )
        self.assertEqual(selector.select([Patch("rename", rename)])["rename"], [0, 1])
        self.assertEqual(selector.select([Patch("delete", delete)])["delete"], [2])

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
        self.assertEqual(map_only.changed_paths(unknown), ["new.py"])
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
        selector = self.selector({"file.py": [1]}, baseline_fail=[2], missing=[0])
        diff = (
            "diff --git a/file.py b/file.py\n"
            "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("runtime subprocess")):
            self.assertEqual(selector.select([Patch("known", diff)])["known"], [0, 1, 2])

    def test_catalog_identity_is_exact_but_durations_may_change(self):
        selector = self.selector({"file.py": [1]})
        changed_duration = [dict(test, seconds=99.0) for test in self.catalog]
        duration_selector = self.selector({"file.py": [1]}, catalog=changed_duration)
        identity_selectors = []
        for field, value in (
            ("number", 9),
            ("id", "tests/test_b.py::renamed"),
            ("file", "tests/other.py"),
        ):
            changed_identity = [dict(test) for test in self.catalog]
            changed_identity[1][field] = value
            identity_selectors.append(
                self.selector({"file.py": [1]}, catalog=changed_identity)
            )
        diff = (
            "diff --git a/file.py b/file.py\n"
            "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        patch = [Patch("known", diff)]
        self.assertEqual(selector.select(patch)["known"], [1])
        self.assertEqual(duration_selector.select(patch)["known"], [1])
        for identity_selector in identity_selectors:
            self.assertEqual(
                identity_selector.select(patch)["known"], identity_selector.numbers
            )

    def test_invalid_schema_falls_back_and_all_tests_remains_default(self):
        selector = self.selector({"file.py": [0]}, schema=2)
        source_selector = self.selector({"file.py": [0]}, source={"repository": "untrusted"})
        diff = (
            "diff --git a/file.py b/file.py\n"
            "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        self.assertEqual(selector.select([Patch("invalid-map", diff)])["invalid-map"], [0, 1, 2])
        self.assertEqual(
            source_selector.select([Patch("invalid-source", diff)])["invalid-source"],
            source_selector.numbers,
        )
        self.assertIs(Config().selector_cls, AllTestsSelector)

    def test_missing_and_corrupt_maps_fall_back(self):
        diff = (
            "diff --git a/file.py b/file.py\n"
            "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        for corrupt in (False, True):
            if corrupt:
                self.map_path.write_bytes(b"not a gzip map")
            with mock.patch.object(
                map_only, "MAP_PATH", self.map_path
            ), mock.patch.object(map_only, "DONOR_CATALOG_SHA256", self.digest):
                selector = map_only.MapSelector(self.catalog, Config())
            self.assertIsNone(selector.files)
            self.assertEqual(
                selector.select([Patch(str(corrupt), diff)])[str(corrupt)],
                selector.numbers,
            )
            self.map_path.unlink(missing_ok=True)


class MapExporterTests(unittest.TestCase):
    def test_generated_map_loads_with_full_catalog(self):
        root = Path(__file__).resolve().parents[1]
        catalog = json.loads((root / "sample/tests.json").read_text(encoding="utf-8"))["tests"]
        selector = map_only.MapSelector(catalog, Config())
        self.assertIsNotNone(selector.files)
        self.assertEqual(len(selector.numbers), 1620)

    def test_export_unions_run_deft_files_and_collapses_pseudo_ids(self):
        catalog = catalog_fixture()
        digest = map_only.catalog_sha256(map_only.catalog_records(catalog))
        index = {
            "n_tests": 3,
            "sets": [(1 << 0) | (1 << 1), 1 << 2, 1 << 3, 1 << 4],
            "run": {"sqlglotc/../sqlglot/a.py": {1: 0}},
            "deft": {"sqlglot/a.py": {2: 1}},
            "files": {"tests/fixtures/a.sql": 2},
            "baseline_fail": [1],
            "missing": [0],
            "pseudo": {"tpcds": {"test": 2, "setup": 4, "queries": {0: 3}}},
        }
        with mock.patch.object(export_file_map, "DONOR_CATALOG_SHA256", digest):
            exported = export_file_map.build_map(index, catalog)
        self.assertEqual(exported["files"]["sqlglot/a.py"], [0, 1, 2])
        self.assertEqual(exported["files"]["tests/fixtures/a.sql"], [2])
        self.assertEqual(exported["baseline_fail"], [1])
        self.assertEqual(exported["missing"], [0])

    def test_wrong_index_hash_is_rejected_before_pickle_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "untrusted.pkl.gz"
            path.write_bytes(b"not the pinned donor index")
            with mock.patch.object(export_file_map.pickle, "load") as load:
                with self.assertRaisesRegex(ValueError, "unexpected donor index SHA-256"):
                    export_file_map.load_trusted_index(path)
                load.assert_not_called()

    def test_catalog_mismatch_and_gzip_output_are_rejected_or_deterministic(self):
        catalog = catalog_fixture()
        index = {
            "n_tests": 3,
            "sets": [1, 2, 4],
            "run": {"a.py": {1: 0}},
            "deft": {},
            "files": {},
            "baseline_fail": [],
            "missing": [],
            "pseudo": {},
        }
        with self.assertRaisesRegex(ValueError, "catalog does not match donor"):
            export_file_map.build_map(index, catalog)

        digest = map_only.catalog_sha256(map_only.catalog_records(catalog))
        with mock.patch.object(export_file_map, "DONOR_CATALOG_SHA256", digest):
            exported = export_file_map.build_map(index, catalog)
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.gz"
            second = Path(directory) / "second.gz"
            export_file_map.write_map(first, exported)
            export_file_map.write_map(second, exported)
            self.assertEqual(first.read_bytes(), second.read_bytes())


if __name__ == "__main__":
    unittest.main()
