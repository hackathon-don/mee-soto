import ast
import difflib
import gzip
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from strategies.selectors.impact.diffparse import parse_patch
from strategies.selectors.impact.impact import _ast_diff
from strategies.selectors.impact_tables import TableIndex, TableKeyAnalyzer, changed_entry, load_tables
from strategies.selectors.map_format import SOURCE, catalog_records, catalog_sha256

ROOT = Path(__file__).resolve().parents[1]
PATH = "sqlglot/dialects/demo.py"

OLD = textwrap.dedent(
    '''
    BASE = {"k": 1}


    class Demo:
        class Tokenizer:
            ESCAPES = {"x": (16, 2), "u": (16, 4), **BASE, "f": lambda n=1: n + 1, "c": make(1)}
            LIMIT: int = 3

        if True:
            GUARDED = {"a": 1}

        def method(self):
            local = {"a": 1}
            return local
    '''
)


def entries(new_source, old_source=OLD):
    diffs = []
    every_line = set(range(1, 100))  # treat every statement as touched by the patch
    _ast_diff(ast.parse(old_source), ast.parse(new_source), [], diffs, (every_line, every_line))
    return [changed_entry(PATH, d) for d in diffs]


class ChangedEntryTest(unittest.TestCase):
    def test_value_edit_names_the_table_and_entry(self):
        self.assertEqual(entries(OLD.replace('"u": (16, 4)', '"u": (17, 4)')), [(f"{PATH}:7:Demo.Tokenizer.ESCAPES", 1, 5)])

    def test_replaced_value_and_lambda_default_are_entries(self):
        self.assertEqual(entries(OLD.replace('"x": (16, 2)', '"x": None')), [(f"{PATH}:7:Demo.Tokenizer.ESCAPES", 0, 5)])
        self.assertEqual(entries(OLD.replace("lambda n=1", "lambda n=2")), [(f"{PATH}:7:Demo.Tokenizer.ESCAPES", 3, 5)])

    def test_module_level_table(self):
        self.assertEqual(entries(OLD.replace('BASE = {"k": 1}', 'BASE = {"k": 2}')), [(f"{PATH}:2:BASE", 0, 1)])

    def test_edits_that_are_not_one_value_fall_back(self):
        for old, new in (
            ('"u": (16, 4)', '"u": (16, 4), "z": (8, 1)'),  # entry added
            ('"x": (16, 2), ', ""),  # entry removed
            ('"u": (16, 4)', '"w": (16, 4)'),  # key renamed
            ("**BASE", "**OTHER"),  # spread changed
            ('"u": (16, 4)', '"u": pick(16)'),  # new value calls something
            ("make(1)", "make(2)"),  # old value calls something
            ("n + 1", "n + 2"),  # lambda body runs later
            ("LIMIT: int = 3", "LIMIT: int = 4"),  # not a table
            ('GUARDED = {"a": 1}', 'GUARDED = {"a": 2}'),  # not directly in the class body
            ('local = {"a": 1}', 'local = {"a": 2}'),  # inside a function
        ):
            with self.subTest(new=new):
                result = entries(OLD.replace(old, new))
                self.assertTrue(result)
                self.assertEqual(set(result), {None})


class TableIndexTest(unittest.TestCase):
    def test_readers_are_whole_readers_entry_readers_and_untraced_tests(self):
        index = TableIndex({"t": (2, 0b0001, {1: 0b0010})}, untraced=0b1000)
        self.assertEqual(index.readers("t", 1, 2), 0b1011)
        self.assertEqual(index.readers("t", 0, 2), 0b1001)

    def test_unknown_table_or_changed_shape_is_unknown(self):
        index = TableIndex({"t": (2, 0, {})}, untraced=0)
        self.assertIsNone(index.readers("other", 0, 2))
        self.assertIsNone(index.readers("t", 0, 3))


class StubIndex:
    """Every lookup answers with the same candidates, as a coarse base rule would."""

    class_counts: dict = {}

    def __init__(self, mask, pseudo=None):
        self.mask = mask
        self.pseudo = pseudo or {}

    def __getattr__(self, name):
        return lambda *args: self.mask


class AnalyzerTest(unittest.TestCase):
    def analyze(self, new_source, tables, mask=0b1111, pseudo=None):
        with tempfile.TemporaryDirectory() as base:
            target = Path(base) / PATH
            target.parent.mkdir(parents=True)
            target.write_text(OLD)
            diff = "".join(
                difflib.unified_diff(OLD.splitlines(True), new_source.splitlines(True), f"a/{PATH}", f"b/{PATH}")
            )
            analyzer = TableKeyAnalyzer(StubIndex(mask, pseudo), base, tables)
            return analyzer.analyze(parse_patch(f"diff --git a/{PATH} b/{PATH}\n{diff}")).candidates

    def test_value_edit_keeps_only_tests_that_read_the_entry(self):
        tables = TableIndex({f"{PATH}:7:Demo.Tokenizer.ESCAPES": (5, 0b0001, {1: 0b0100})}, untraced=0)
        self.assertEqual(self.analyze(OLD.replace('"u": (16, 4)', '"u": (17, 4)'), tables), 0b0101)

    def test_untraced_tests_and_unknown_tables_are_kept(self):
        edit = OLD.replace('"u": (16, 4)', '"u": (17, 4)')
        tables = TableIndex({f"{PATH}:7:Demo.Tokenizer.ESCAPES": (5, 0, {})}, untraced=0b1000)
        self.assertEqual(self.analyze(edit, tables), 0b1000)
        self.assertEqual(self.analyze(edit, TableIndex({}, untraced=0)), 0b1111)

    def test_other_edits_keep_the_base_candidates(self):
        tables = TableIndex({f"{PATH}:7:Demo.Tokenizer.ESCAPES": (5, 0, {})}, untraced=0)
        self.assertEqual(self.analyze(OLD.replace('"u": (16, 4)', '"w": (16, 4)'), tables), 0b1111)

    def test_pseudo_tests_follow_their_real_test(self):
        pseudo = {"tpcds": {"test": 2, "setup": 5, "queries": {0: 4}}}
        edit = OLD.replace('"u": (16, 4)', '"u": (17, 4)')
        kept = TableIndex({f"{PATH}:7:Demo.Tokenizer.ESCAPES": (5, 0, {1: 0b000100})}, untraced=0)
        dropped = TableIndex({f"{PATH}:7:Demo.Tokenizer.ESCAPES": (5, 0, {1: 0b000001})}, untraced=0)
        self.assertEqual(self.analyze(edit, kept, mask=0b111111, pseudo=pseudo), 0b110100)
        self.assertEqual(self.analyze(edit, dropped, mask=0b111111, pseudo=pseudo), 0b000001)


CATALOG = [{"number": n, "id": f"tests/test_demo.py::test_{n}", "file": "tests/test_demo.py", "seconds": 0.1} for n in range(3)]

PACKAGE = textwrap.dedent(
    '''
    BASE = {"k": 1, "j": 2}


    class Demo:
        ESCAPES = {"x": 16, "u": 17, **BASE}
        KEYWORDS = {"a": 1, "b": 2}
        COPIED = {"a": 1, "b": 2}
        UNTOUCHED = {"a": 1}

        def nested(self):
            return {"local": 1}
    '''
)

SCRIPTS = {
    0: "from sqlglot import Demo\nDemo.ESCAPES['u']; Demo.KEYWORDS.get('b'); 'a' in Demo.KEYWORDS; len(Demo.COPIED); list(Demo.COPIED)",
    1: "from sqlglot import Demo\ndict(Demo.COPIED); Demo.KEYWORDS.get('missing'); assert Demo().nested() == {'local': 1}",
    2: "import sqlglot\nraise SystemExit(3)",
}


@unittest.skipUnless(sys.version_info >= (3, 12), "the tracer needs Python 3.12+")
class TraceToMapTest(unittest.TestCase):
    """Runs the real tracer on a tiny package and folds its dumps into a map."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        base = Path(cls.temp.name)
        (base / "src" / "sqlglot").mkdir(parents=True)
        (base / "src" / "sqlglot" / "__init__.py").write_text(PACKAGE)
        run = base / "run"
        run.mkdir()
        with open(run / "status.jsonl", "w") as status:
            for number, script in SCRIPTS.items():
                env = dict(
                    os.environ,
                    PYTHONPATH=f"{base / 'src'}{os.pathsep}{ROOT / 'tools' / 'trace_tables'}",
                    BR_TRACE_DIR=str(run / "trace"),
                    BR_SRC=f"{base / 'src'}{os.sep}",
                    BR_TEST=str(number),
                    BR_TABLE_CACHE=str(base / "cache"),
                )
                rc = subprocess.run([sys.executable, "-c", script], env=env, cwd=base).returncode
                status.write(json.dumps({"number": number, "rc": rc}) + "\n")
        (base / "tests.json").write_text(json.dumps({"tests": CATALOG}))
        cls.map_path = base / "impact_tables.json.gz"
        subprocess.run(
            [sys.executable, str(ROOT / "tools" / "build_table_index.py"), str(run), str(base / "tests.json"), str(cls.map_path)],
            check=True,
            capture_output=True,
        )
        cls.tables = load_tables(CATALOG, cls.map_path)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def readers(self, name, index, count, line):
        return self.tables.readers(f"sqlglot/__init__.py:{line}:{name}", index, count)

    def test_a_failed_test_is_untraced_and_always_kept(self):
        self.assertEqual(self.tables.untraced, 0b100)

    def test_value_reads_are_recorded_per_entry(self):
        self.assertEqual(self.readers("Demo.ESCAPES", 1, 3, 6), 0b101)  # test 0 read "u"
        self.assertEqual(self.readers("Demo.ESCAPES", 0, 3, 6), 0b100)  # nobody read "x"
        self.assertEqual(self.readers("Demo.KEYWORDS", 1, 2, 7), 0b101)  # .get("b") reads; "a" in does not
        self.assertEqual(self.readers("Demo.KEYWORDS", 0, 2, 7), 0b100)

    def test_copying_reads_every_value_but_len_and_key_iteration_read_none(self):
        self.assertEqual(self.readers("Demo.COPIED", 0, 2, 8), 0b110)  # only test 1 copied it
        self.assertEqual(self.readers("Demo.COPIED", 1, 2, 8), 0b110)

    def test_a_spread_reads_the_parent_table_at_import(self):
        self.assertEqual(self.readers("BASE", 0, 2, 2), 0b111)

    def test_unread_and_unwrapped_tables(self):
        self.assertEqual(self.readers("Demo.UNTOUCHED", 0, 1, 9), 0b100)
        self.assertIsNone(self.tables.readers("sqlglot/__init__.py:12:Demo.nested.local", 0, 1))

    def test_map_is_rejected_for_another_catalog_or_source(self):
        other = [dict(test, id=test["id"] + "x") for test in CATALOG]
        self.assertIsNone(load_tables(other, self.map_path))
        data = json.load(gzip.open(self.map_path))
        self.assertEqual(data["source"], SOURCE)
        self.assertEqual(data["catalog_sha256"], catalog_sha256(catalog_records(CATALOG)))
        self.assertIsNone(load_tables(CATALOG, Path(self.temp.name) / "missing.json.gz"))


if __name__ == "__main__":
    unittest.main()
