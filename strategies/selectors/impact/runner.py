"""Child-side test execution: runs selected sqlglot tests on a patched tree.

Runs inside a process forked from the selector. The parent pre-imports heavy
third-party modules (numpy, pandas, duckdb) but never sqlglot or tests, so the
child imports those fresh from the overlay root.

Protocol on `out_fd`, one JSON object per line:
  {"s": n}            test n started
  {"d": n, "ok": b}   test n finished
  {"x": "msg"}        child-level error (all unfinished tests count as failed)
"""

from __future__ import annotations

import importlib
import os
import pickle
import signal
import sys
import time
import unittest
from concurrent.futures import Future


class TestTimeout(KeyboardInterrupt):
    """Raised by SIGALRM inside a test that ran past its limit.

    A KeyboardInterrupt subclass so unittest (including subTest blocks) lets it
    through instead of recording it and carrying on with the next subtest.
    """


_RESULT = None


def _on_alarm(signum, frame):
    if _RESULT is not None and _RESULT.current is not None:
        _RESULT.failed.add(_RESULT.current)
    raise TestTimeout()


class InlineExecutor:
    """Stands in for ProcessPoolExecutor: runs work in-process.

    Arguments and results round-trip through pickle so pickling failures still
    surface the way they would with real spawn workers.
    """

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        future: Future = Future()
        try:
            args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            result = fn(*args, **kwargs)
            result = pickle.loads(pickle.dumps(result))
        except TestTimeout:
            raise
        except BaseException as e:  # noqa: BLE001
            future.set_exception(e)
        else:
            future.set_result(result)
        return future

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [self.submit(fn, *args).result() for args in zip(*iterables)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False


def _tpcds_subset(queries: list[int]):
    """test_execute_tpcds restricted to the given query indices, run in-process."""

    def test(self):
        from sqlglot import parse_one
        from tests.helpers import TPCDS_SCHEMA

        mod = sys.modules[type(self).__module__]
        mod.initializer(TPCDS_SCHEMA, self.tpcds_tables)
        for i in queries:
            args, sql, _ = self.tpcds_sqls[i]
            expr = pickle.loads(pickle.dumps(parse_one(sql)))
            table = pickle.loads(pickle.dumps(mod.mp_execute(expr, args)))
            if table is not None:
                self.subtestHelper(i, table, tpch=False)

    return test


# Test modules whose fixture loops treat every item independently, so a fixture
# edit only needs the changed items re-checked.
FIXTURE_SUBSET_MODULES = {"tests.test_optimizer", "tests.test_transpile", "tests.test_serde"}


def _changed_items(new_items: list, old_items: list) -> list:
    if len(new_items) == len(old_items):
        return [n for n, o in zip(new_items, old_items) if n != o]
    first = 0
    while first < min(len(new_items), len(old_items)) and new_items[first] == old_items[first]:
        first += 1
    return new_items[first:]


def _install_fixture_subset(module, base_fixtures: str, edited: set[str]) -> None:
    import tests.helpers as helpers

    for name in ("load_sql_fixture_pairs", "load_sql_fixtures"):
        orig = getattr(helpers, name)
        if getattr(module, name, None) is not orig:
            continue

        def wrapped(filename, _orig=orig):
            if filename not in edited:
                return _orig(filename)
            new_items = list(_orig(filename))
            saved = helpers.FIXTURES_DIR
            helpers.FIXTURES_DIR = base_fixtures
            try:
                old_items = list(_orig(filename))
            finally:
                helpers.FIXTURES_DIR = saved
            return iter(_changed_items(new_items, old_items))

        setattr(module, name, wrapped)


def _module_name(path: str) -> str:
    return path[:-3].replace("/", ".")


class _Result(unittest.TestResult):
    def __init__(self, emit, key_of, limits):
        super().__init__()
        self.emit = emit
        self.key_of = key_of
        self.limits = limits
        self.failed: set[int] = set()
        self.current: int | None = None
        self.t0 = 0.0
        self.done: set[int] = set()

    def _key(self, test):
        return self.key_of.get(test.id())

    def startTest(self, test):
        super().startTest(test)
        key = self._key(test)
        self.current = key
        if key is not None:
            self.emit({"s": key})
            self.t0 = time.process_time()
            signal.setitimer(signal.ITIMER_REAL, self.limits.get(key, 30.0))

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        key = self._key(test)
        if key is not None:
            self.done.add(key)
            self.emit({"d": key, "ok": key not in self.failed, "cpu": round(time.process_time() - self.t0, 4)})
        self.current = None
        super().stopTest(test)

    def _fail(self, test):
        key = self._key(test)
        if key is not None:
            self.failed.add(key)
        else:
            # setUpClass / setUpModule errors arrive as _ErrorHolder objects.
            desc = getattr(test, "description", "") or str(test)
            self.class_errors.append(desc)

    class_errors: list

    def addError(self, test, err):
        self._fail(test)

    def addFailure(self, test, err):
        self._fail(test)

    def addUnexpectedSuccess(self, test):
        self._fail(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._fail(test)


def child_main(
    root: str,
    tests: list[tuple],
    out_fd: int,
    base: str | None = None,
    fixture_edits: list[str] | None = None,
    import_checks: list[str] | None = None,
    import_timeout: float = 2.0,
) -> None:
    """tests: (number, pytest node id, timeout seconds[, tpcds query subset]) in run order."""
    out = os.fdopen(out_fd, "w", buffering=1)

    def emit(obj):
        import json

        out.write(json.dumps(obj) + "\n")

    import gc

    # sqlglot allocates millions of small cyclic objects (nodes point to their
    # parents); with the default gen-0 threshold the collector rescans the heap
    # constantly. A larger threshold cut test_max_nodes from ~28 s to ~16 s.
    gc.set_threshold(50000, 20, 100)
    sys.dont_write_bytecode = True
    os.chdir(root)
    sys.path[:0] = [root]
    os.environ["PYTHONPATH"] = root
    os.environ["SKIP_INTEGRATION"] = "0"
    signal.signal(signal.SIGALRM, _on_alarm)

    for module_name in import_checks or []:
        ok = False
        try:
            signal.setitimer(signal.ITIMER_REAL, import_timeout)
            importlib.import_module(module_name)
            ok = True
        except BaseException:  # noqa: BLE001 - inconclusive imports are conservative failures
            pass
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        emit({"i": module_name, "ok": ok})

    import concurrent.futures

    concurrent.futures.ProcessPoolExecutor = InlineExecutor  # type: ignore[misc]

    key_of: dict[str, int] = {}
    limits: dict[int, float] = {}
    by_module: dict[str, list[tuple[int, str, str]]] = {}
    subsets: dict[int, list[int]] = {}
    for entry in tests:
        number, node, limit = entry[:3]
        if len(entry) > 3 and entry[3] is not None:
            subsets[number] = entry[3]
        path, cls, meth = node.split("::")
        mod = _module_name(path)
        key_of[f"{mod}.{cls}.{meth}"] = number
        limits[number] = limit
        by_module.setdefault(mod, []).append((number, cls, meth))

    global _RESULT
    result = _Result(emit, key_of, limits)
    result.class_errors = []
    _RESULT = result
    timeouts = 0
    max_timeouts = int(os.environ.get("BR_MAX_TIMEOUTS", "2"))
    for mod_name, items in by_module.items():
        try:
            signal.setitimer(signal.ITIMER_REAL, 60.0)
            module = __import__(mod_name, fromlist=["_"])
            signal.setitimer(signal.ITIMER_REAL, 0)
            if fixture_edits and base and mod_name in FIXTURE_SUBSET_MODULES:
                _install_fixture_subset(module, os.path.join(base, "tests", "fixtures"), set(fixture_edits))
        except BaseException:  # noqa: BLE001 - import failure fails every test in the file
            signal.setitimer(signal.ITIMER_REAL, 0)
            for number, _, _ in items:
                emit({"s": number})
                emit({"d": number, "ok": False})
            continue
        cases = []
        bad_lookup = []
        for number, cls, meth in items:
            try:
                if number in subsets and meth == "test_execute_tpcds":
                    setattr(getattr(module, cls), meth, _tpcds_subset(subsets[number]))
                cases.append((number, cls, meth))
                getattr(module, cls)
            except BaseException:  # noqa: BLE001
                bad_lookup.append(number)
        for number in bad_lookup:
            emit({"s": number})
            emit({"d": number, "ok": False})
        before = len(result.class_errors)
        pending = [c for c in cases if c[0] not in bad_lookup]
        while pending:
            suite = unittest.TestSuite()
            for number, cls, meth in pending:
                try:
                    suite.addTest(getattr(module, cls)(meth))
                except BaseException:  # noqa: BLE001
                    emit({"s": number})
                    emit({"d": number, "ok": False})
                    result.done.add(number)
            try:
                suite.run(result)
                pending = []
            except TestTimeout:
                signal.setitimer(signal.ITIMER_REAL, 0)
                cur = result.current
                if cur is not None and cur not in result.done:
                    result.failed.add(cur)
                    result.done.add(cur)
                    emit({"d": cur, "ok": False})
                result.current = None
                timeouts += 1
                if timeouts >= max_timeouts:
                    # Probably a hang that every remaining test would hit too:
                    # stop here; the parent includes what is left unrun.
                    out.flush()
                    return
                pending = [c for c in pending if c[0] not in result.done]
        if len(result.class_errors) > before:
            # A class or module fixture failed: every selected test in it fails.
            for number, _, _ in items:
                emit({"d": number, "ok": False})
    t = os.times()
    emit({"cpu_total": round(t.user + t.system, 3)})
    out.flush()
