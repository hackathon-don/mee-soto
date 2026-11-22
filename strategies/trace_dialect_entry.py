"""Trace one catalog test in an otherwise fresh interpreter."""

import ast
import atexit
import importlib.abc
import importlib.machinery
from itertools import count
import json
from multiprocessing import spawn
from multiprocessing.context import SpawnProcess
from multiprocessing.process import BaseProcess
import os
from pathlib import Path
import sys
from threading import local

from strategies.selectors.dialect_entry_format import dialect_table_path, table_assignments


def untraced_process(*args, **kwargs):
    os._exit(125)


class Recorder(importlib.abc.MetaPathFinder):
    def __init__(self, root):
        self.root = root
        self.tables = {}
        self.raw_files = set()
        self.loading = None
        self.unknown = False

    def install(self):
        if any(name == "sqlglot" or name.startswith("sqlglot.") for name in sys.modules):
            untraced_process()
        sys.path.insert(0, str(self.root))
        sys.meta_path.insert(0, self)
        sys.addaudithook(self.audit)

    def merge(self, report):
        self.raw_files.update(report["raw_files"])
        for path, name, whole, keys in report["tables"]:
            row = self.tables.setdefault((path, name), [False, set()])
            row[0] |= whole
            row[1].update(keys)

    def report(self):
        return {
            "tables": [[path, name, whole, sorted(keys)]
                       for (path, name), (whole, keys) in self.tables.items()],
            "raw_files": sorted(self.raw_files),
        }

    def audit(self, event, args):
        if event in ("subprocess.Popen", "os.fork", "os.forkpty", "os.posix_spawn",
                     "os.spawn", "os.exec", "os.system", "cpython.PyInterpreterState_New"):
            untraced_process()
        if event == "open" and isinstance(args[0], (str, bytes)):
            path = os.fsdecode(args[0])
            if not path.endswith(".py"):
                return
            path = os.path.abspath(path)
            if path != self.loading and path.startswith(str(self.root) + os.sep):
                rel = os.path.relpath(path, self.root)
                if dialect_table_path(rel):
                    self.raw_files.add(rel)

    def wrap(self, path, name, value):
        row = self.tables.setdefault((path, name), [False, set()])
        return ObservedDict(value, row)

    def find_spec(self, fullname, path, target=None):
        if not fullname.startswith(("sqlglot.dialects.", "sqlglot.parsers.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if not spec or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            self.unknown = True
            return spec
        rel = os.path.relpath(spec.origin, self.root)
        if not dialect_table_path(rel):
            self.unknown = True
            return spec
        spec.loader = Loader(fullname, spec.origin, self, rel)
        return spec


class Loader(importlib.machinery.SourceFileLoader):
    def __init__(self, name, path, recorder, rel):
        super().__init__(name, path)
        self.recorder, self.rel = recorder, rel

    def get_code(self, fullname):
        self.recorder.loading = self.path
        try:
            source = self.get_data(self.path)
        finally:
            self.recorder.loading = None
        tree = ast.parse(source, self.path)
        for name, stmt in table_assignments(tree):
            call = ast.Call(ast.Name("__rwx_observe_dialect_table__", ast.Load()),
                            [ast.Constant(self.rel), ast.Constant(name), stmt.value], [])
            stmt.value = ast.copy_location(call, stmt.value)
        return compile(ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True)

    def exec_module(self, module):
        module.__dict__["__rwx_observe_dialect_table__"] = self.recorder.wrap
        super().exec_module(module)


class ObservedDict(dict):
    def __init__(self, value, row):
        dict.__init__(self, value)
        self.row = row

    def key(self, key):
        if type(key) is str:
            self.row[1].add(key)
        else:
            self.row[0] = True

    def __getitem__(self, key):
        self.key(key)
        return dict.__getitem__(self, key)

    def __contains__(self, key):
        self.key(key)
        return dict.__contains__(self, key)

    def get(self, key, default=None):
        self.key(key)
        return dict.get(self, key, default)


def _observe_whole(method):
    def wrapped(self, *args, **kwargs):
        self.row[0] = True
        return method(self, *args, **kwargs)
    return wrapped


# Copies, merges, bulk reads and mutations are charged at their origin, so
# subsequent consumers of an ordinary dict, view, trie or serialized copy
# cannot evade the dependency, including when it was built during import.
for _name in (
    "__iter__", "__reversed__", "__len__", "keys", "values", "items", "copy",
    "__eq__", "__ne__", "__repr__", "__or__", "__ror__", "__ior__", "__reduce_ex__",
    "__setitem__", "__delitem__", "update", "pop", "popitem", "setdefault", "clear",
):
    setattr(ObservedDict, _name, _observe_whole(getattr(dict, _name)))


class SpawnWorkers:
    def __init__(self, root, result):
        self.root = root
        self.result = result
        self.expected = []
        self.current = local()
        self.numbers = count()

    def install(self):
        original_start = BaseProcess.start
        original_command = spawn.get_command_line

        def start(process):
            if type(process) is not SpawnProcess or getattr(self.current, "record", None) is not None:
                untraced_process()
            record = [None, None]
            self.expected.append(record)
            self.current.record = record
            try:
                original_start(process)
                record[1] = process._popen
            finally:
                del self.current.record

        def command(**kwargs):
            record = getattr(self.current, "record", None)
            argv = original_command(**kwargs)
            if (record is None or record[0] is not None or
                    argv[-3] != "-c" or argv[-1] != "--multiprocessing-fork"):
                untraced_process()
            path = self.result.with_suffix(f".worker-{next(self.numbers)}.json")
            record[0] = path
            argv[-2] = ("from strategies.trace_dialect_entry import trace_worker; "
                        f"trace_worker({str(self.root)!r}, {str(path)!r}, {kwargs!r})")
            return argv

        BaseProcess.start = start
        spawn.get_command_line = command

    def merge_into(self, recorder):
        for path, process in self.expected:
            if path is None or process is None or process.poll() != 0:
                return False
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                if report["pid"] != process.pid:
                    return False
                recorder.merge(report)
            except (OSError, ValueError, KeyError, TypeError):
                return False
        return True


def trace_worker(root, result, kwargs):
    recorder = Recorder(Path(root))
    recorder.install()
    BaseProcess.start = untraced_process
    original_main = spawn._main
    complete = False

    def main(fd, parent_sentinel):
        nonlocal complete
        code = original_main(fd, parent_sentinel)
        complete = code == 0
        return code

    @atexit.register
    def report():
        if complete and not recorder.unknown:
            Path(result).write_text(json.dumps({"pid": os.getpid(), **recorder.report()}),
                                    encoding="utf-8")

    spawn._main = main
    spawn.spawn_main(**kwargs)


def trace(root, node, result):
    os.chdir(root)
    recorder = Recorder(root)
    recorder.install()
    workers = SpawnWorkers(root, result)
    workers.install()
    import pytest

    class Outcome:
        phases = set()
        bad = False

        def pytest_runtest_logreport(self, report):
            if report.nodeid == node and report.passed and not getattr(report, "wasxfail", None):
                self.phases.add(report.when)
            elif not report.passed:
                self.bad = True

    outcome = Outcome()
    code = pytest.main([node, "-q", "-p", "no:cacheprovider"], plugins=[outcome])
    if (not code and not recorder.unknown and not outcome.bad and
            outcome.phases == {"setup", "call", "teardown"} and workers.merge_into(recorder)):
        result.write_text(json.dumps(recorder.report()), encoding="utf-8")


if __name__ == "__main__":
    trace(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))
