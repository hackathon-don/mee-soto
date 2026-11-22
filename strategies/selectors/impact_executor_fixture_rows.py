"""Share immutable CSV conversions, not executor setup or test results."""

import ast
import csv
import hashlib
import locale
from pathlib import Path
import unittest
import zlib

from .impact import runner, selector
from .impact_optimizer_transcript import OptimizerTranscriptImpact

EXECUTOR = "tests/test_executor.py"
EXECUTOR_STAMP = "270a844a7b0b84045efde017135e6208ca07d7d93eba395be17c20c6e918923d"
EXECUTOR_NODE = f"{EXECUTOR}::TestExecutor::"


def _identity(path):
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _prepare(root):
    source = root / EXECUTOR
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != EXECUTOR_STAMP:
        return None
    tree = ast.parse(data, filename=str(source))
    open_file = next(node for node in tree.body if getattr(node, "name", None) == "open_file")
    cls = next(node for node in tree.body if getattr(node, "name", None) == "TestExecutor")
    method = next(node for node in cls.body if getattr(node, "name", None) == "setUpClass")
    setup = next(node for node in method.body if getattr(node, "name", None) == "setup")
    row_block = setup.body[2:-1]

    parser = ast.parse("def read_rows(file_name):\n    tables = {}\n    table = file_name\n    return rows, ctypes\n")
    parser.body[0].body[2:2] = row_block
    parser.body.insert(0, open_file)
    namespace = {"ast": ast, "csv": csv}
    exec(compile(ast.fix_missing_locations(parser), str(source), "exec"), namespace)

    fixtures = root / "tests/fixtures/optimizer"
    cache = {}
    for folder in ("tpc-h", "tpc-ds"):
        for path in (fixtures / folder).glob("*.csv.gz"):
            stamp = _identity(path)
            if stamp is None:
                continue
            rows, types = namespace["read_rows"](str(path))
            if all(t in (str, int, float) for t in types) and stamp == _identity(path):
                cache[stamp] = tuple(rows)
    if not cache:
        return None

    replacement = ast.parse(
        "rows = _cached_fixture_rows(file_name)\n"
        "if rows is None:\n"
        "    pass\n"
        "else:\n"
        "    tables[table] = rows\n"
    ).body
    replacement[1].body = row_block
    setup.body[2:-1] = replacement
    code = compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), "exec")
    return cache, code, _identity(source), method.decorator_list[0].lineno, open_file.lineno


def _with_cached_fixtures(verify, prepared):
    cache, code, source_stamp, setup_line, open_line = prepared
    reader, literal_eval = csv.reader, ast.literal_eval
    field_limit, encoding = csv.field_size_limit(), locale.getencoding()

    def cached_rows(path):
        if (csv.reader is not reader or ast.literal_eval is not literal_eval
                or csv.field_size_limit() != field_limit or locale.getencoding() != encoding):
            return None
        rows = cache.get(_identity(path))
        return list(rows) if rows is not None else None

    def child(root, tests, *args, **kwargs):
        if (source_stamp is None or _identity(Path(root) / EXECUTOR) != source_stamp
                or not any(node.startswith(EXECUTOR_NODE) for _, node, *_ in tests)):
            return verify(root, tests, *args, **kwargs)
        suite = unittest.TestSuite
        original = suite._handleClassSetUp

        def setup(self, test, result):
            cls = type(test)
            if (cls is getattr(result, "_previousTestClass", None)
                    or cls.__module__ != "tests.test_executor" or cls.__name__ != "TestExecutor"):
                return original(self, test, result)
            method = vars(cls).get("setUpClass")
            if not isinstance(method, classmethod):
                return original(self, test, result)
            func = method.__func__
            namespace = func.__globals__
            source = func.__code__
            opener = getattr(namespace.get("open_file"), "__code__", None)
            if (source.co_firstlineno != setup_line or _identity(source.co_filename) != source_stamp
                    or opener is None or opener.co_firstlineno != open_line
                    or _identity(opener.co_filename) != source_stamp
                    or namespace.get("ast") is not ast or namespace.get("csv") is not csv):
                return original(self, test, result)
            namespace = dict(namespace, _cached_fixture_rows=cached_rows)
            exec(code, namespace)
            cls.setUpClass = namespace["setUpClass"]
            try:
                return original(self, test, result)
            finally:
                cls.setUpClass = method

        suite._handleClassSetUp = setup
        try:
            return verify(root, tests, *args, **kwargs)
        finally:
            suite._handleClassSetUp = original

    return child


class ExecutorFixtureRowsImpact(OptimizerTranscriptImpact):
    def select(self, commits):
        run_all = selector.run_all

        def run_with_fixture_rows(plans, ids, limits, deadline):
            if sum(any(ids[n].startswith(EXECUTOR_NODE) for n in plan.run) for plan in plans) < 2:
                return run_all(plans, ids, limits, deadline)
            try:
                prepared = _prepare(Path(selector.SG))
            except (OSError, EOFError, ValueError, StopIteration, csv.Error, SyntaxError, zlib.error):
                prepared = None
            if prepared is None:
                return run_all(plans, ids, limits, deadline)
            verify = runner.child_main
            runner.child_main = _with_cached_fixtures(verify, prepared)
            try:
                return run_all(plans, ids, limits, deadline)
            finally:
                runner.child_main = verify

        selector.run_all = run_with_fixture_rows
        try:
            return super().select(commits)
        finally:
            selector.run_all = run_all
