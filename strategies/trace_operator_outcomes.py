"""Collect operand observations for one catalog test and all its spawn workers."""

import ast
import atexit
import enum
import importlib.abc
import importlib.machinery
import json
import operator
import os
import sys
import threading
from itertools import count
from multiprocessing import spawn
from multiprocessing.context import SpawnProcess
from multiprocessing.process import BaseProcess
from pathlib import Path

from .selectors.outcome_format import OPS, site_id


HOOK = "__rwx_record_comparison__"
CAP = 64
SIMPLE = (type(None), bool, int, float, str, bytes)
OPERATORS = {
    "Is": operator.is_, "IsNot": operator.is_not, "Eq": operator.eq, "NotEq": operator.ne,
    "In": lambda a, b: operator.contains(b, a),
    "NotIn": lambda a, b: not operator.contains(b, a),
    "Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge,
}


def describe(value):
    cls = type(value)
    if any(cls is plain for plain in SIMPLE):
        if cls is str and len(value) <= 256:
            return ["str", value]
        if cls is bytes and len(value) <= 128:
            return ["bytes", value.hex()]
        if cls is float:
            return ["float", value.hex()]
        if cls is int and value.bit_length() <= 256:
            return ["int", str(value)]
        if cls in (bool, type(None)):
            return [cls.__name__, value]
        return None
    if type(cls) is enum.EnumType and (cls.__eq__, cls.__ne__) in (
            (object.__eq__, object.__ne__), (str.__eq__, str.__ne__), (int.__eq__, int.__ne__)):
        name = object.__getattribute__(value, "_name_")
        if type(name) is str and cls.__members__.get(name) is value:
            scalar = (str.__str__(value) if cls.__eq__ is str.__eq__ else
                      int.__int__(value) if cls.__eq__ is int.__eq__ else None)
            return ["enum", cls.__module__, cls.__qualname__, name, describe(scalar)]
    return None


def alternate(op, left, right):
    if op == "In" and type(right) not in (str, bytes):
        return None
    try:
        value = OPERATORS[op](left, right)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if type(value) is bool else None


class Rewrite(ast.NodeTransformer):
    def __init__(self, path, names, targets):
        self.path, self.names, self.targets = path, names, targets
        self.serial = count()
        self.comprehensions = 0
        self.skipped = set()

    def visit_listcomp(self, node):
        self.comprehensions += 1
        try:
            return self.generic_visit(node)
        finally:
            self.comprehensions -= 1

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_listcomp

    def visit_Compare(self, node):
        self.generic_visit(node)
        ids = [site_id(self.path, node, i) for i in range(len(node.ops))]
        selected = [type(op).__name__ in OPS and (self.targets is None or key in self.targets)
                    for op, key in zip(node.ops, ids)]
        if not any(selected):
            return node
        if len(ids) > 1 and self.comprehensions:
            self.skipped.update(key for key, yes in zip(ids, selected) if yes)
            return node
        left, links = node.left, []
        for i, (op, right) in enumerate(zip(node.ops, node.comparators)):
            next_left = None
            if i < len(ids) - 1:
                name = f"__rwx_compare_tmp_{next(self.serial)}__"
                while name in self.names:
                    name = f"__rwx_compare_tmp_{next(self.serial)}__"
                right = ast.NamedExpr(ast.Name(name, ast.Store()), right)
                next_left = ast.Name(name, ast.Load())
            if selected[i] or len(ids) > 1:
                link = ast.Call(ast.Name(HOOK, ast.Load()),
                                [ast.Constant(ids[i] if selected[i] else None),
                                 ast.Constant(type(op).__name__), left, right,
                                 ast.Constant(len(ids) > 1)], [])
            else:
                link = ast.Compare(left, [op], [right])
            links.append(ast.copy_location(link, node))
            left = next_left
        return links[0] if len(links) == 1 else ast.copy_location(ast.BoolOp(ast.And(), links), node)


def relevant(frame):
    module = frame.f_globals.get("__name__", "")
    return module in ("sqlglot", "tests") or module.startswith(("sqlglot.", "tests."))


def abort(*args, **kwargs):
    os._exit(125)


class Recorder(importlib.abc.MetaPathFinder):
    def __init__(self, root, targets):
        self.root, self.targets = root, targets
        self.paths = None if targets is None else {site.split(":", 1)[0] for site in targets}
        self.rows, self.skipped = {}, set()
        self.unknown = False
        self.loading = None

    def install(self):
        if any(name == "sqlglot" or name.startswith("sqlglot.") for name in sys.modules):
            abort()
        sys.path.insert(0, str(self.root))
        sys.meta_path.insert(0, self)
        sys.addaudithook(self.audit)
        original = threading.Thread.start

        def start(thread, *args, **kwargs):
            cls = type(thread)
            caller = sys._getframe(1).f_globals.get("__name__")
            pool_admin = (
                caller == "concurrent.futures.process" and cls.__module__ == caller
                and cls.__name__ == "_ExecutorManagerThread"
            ) or (caller == "multiprocessing.queues" and cls is threading.Thread
                  and thread.name == "QueueFeederThread")
            if not pool_admin:
                self.unknown = True
            return original(thread, *args, **kwargs)

        threading.Thread.start = start
        sys.setprofile(self.profile)

    def profile(self, frame, event, arg):
        if event == "c_call" and relevant(frame):
            module = getattr(arg, "__module__", None)
            owner = getattr(arg, "__self__", None)
            if (module in ("time", "_thread", "_random") or
                    module in ("os", "posix") and getattr(arg, "__name__", "") in ("urandom", "getrandom") or
                    isinstance(owner, type) and owner.__module__ == "datetime" or
                    getattr(type(owner), "__module__", "") in ("_random", "numpy.random._generator")):
                self.unknown = True
        elif event == "call" and frame.f_back and relevant(frame.f_back):
            module = frame.f_globals.get("__name__")
            if (module in ("random", "secrets") or module == "inspect"
                    and frame.f_code.co_name in ("getsource", "getsourcelines", "currentframe")):
                self.unknown = True

    def audit(self, event, args):
        if event in ("subprocess.Popen", "os.fork", "os.forkpty", "os.posix_spawn",
                     "os.spawn", "os.exec", "os.system", "cpython.PyInterpreterState_New"):
            abort()
        if event == "open" and isinstance(args[0], (str, bytes)):
            path = os.path.abspath(os.fsdecode(args[0]))
            if path == self.loading or not path.startswith(str(self.root / "sqlglot") + os.sep):
                return
            if path.endswith((".py", ".pyc")) and relevant(sys._getframe(1)):
                self.unknown = True

    def find_spec(self, fullname, path, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if not spec or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            self.unknown = True
            return spec
        origin = Path(spec.origin)
        if not origin.is_relative_to(self.root):
            self.unknown = True
            return spec
        rel = origin.relative_to(self.root).as_posix()
        if self.paths is None or rel in self.paths:
            spec.loader = Loader(fullname, spec.origin, self, rel)
        return spec

    def observe(self, key, op, left, right, chain):
        if not self.unknown and (key not in self.rows or self.rows[key] is not None):
            try:
                a, b = describe(left), describe(right)
                if a is None or b is None:
                    if key is not None:
                        self.rows[key] = None
                    if chain:
                        self.unknown = True
                elif key is not None:
                    row = json.dumps([a, b, left is right, [alternate(o, left, right) for o in OPS]],
                                     separators=(",", ":"))
                    seen = self.rows.setdefault(key, set())
                    seen.add(row)
                    if len(seen) > CAP:
                        self.rows[key] = None
            except Exception:
                if key is not None:
                    self.rows[key] = None
                if chain:
                    self.unknown = True
        return OPERATORS[op](left, right)

    def report(self):
        return {"pid": os.getpid(), "sites": {k: None if v is None else sorted(v)
                                             for k, v in self.rows.items()},
                "skipped": sorted(self.skipped)}

    def merge(self, report):
        self.skipped.update(report["skipped"])
        for key, values in report["sites"].items():
            if values is None:
                self.rows[key] = None
            elif key not in self.rows or self.rows[key] is not None:
                seen = self.rows.setdefault(key, set())
                seen.update(values)
                if len(seen) > CAP:
                    self.rows[key] = None


class Loader(importlib.machinery.SourceFileLoader):
    def __init__(self, name, path, recorder, rel):
        super().__init__(name, path)
        self.recorder, self.rel = recorder, rel

    def exec_module(self, module):
        module.__dict__[HOOK] = self.recorder.observe
        super().exec_module(module)

    def get_code(self, fullname):
        self.recorder.loading = self.path
        try:
            source = self.get_data(self.path)
        finally:
            self.recorder.loading = None
        tree = ast.parse(source, self.path)
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names.update(node.arg for node in ast.walk(tree) if isinstance(node, ast.arg))
        if HOOK in names:
            self.recorder.unknown = True
            return compile(source, self.path, "exec", dont_inherit=True)
        rewrite = Rewrite(self.rel, names, self.recorder.targets)
        tree = rewrite.visit(tree)
        self.recorder.skipped.update(rewrite.skipped)
        return compile(ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True)


class Workers:
    def __init__(self, root, result, sites):
        expected, serial, current = [], count(), threading.local()
        self.expected = expected
        original_start, original_command = BaseProcess.start, spawn.get_command_line

        def start(process):
            if type(process) is not SpawnProcess or getattr(current, "record", None) is not None:
                abort()
            record = [None, None]
            expected.append(record)
            current.record = record
            try:
                original_start(process)
                record[1] = process._popen
            finally:
                del current.record

        def command(**kwargs):
            record = getattr(current, "record", None)
            argv = original_command(**kwargs)
            if record is None or record[0] is not None or argv[-3] != "-c" or argv[-1] != "--multiprocessing-fork":
                abort()
            path = result.with_suffix(f".worker-{next(serial)}.json")
            record[0] = path
            argv[-2] = ("from strategies.trace_operator_outcomes import worker; "
                        f"worker({str(root)!r}, {str(path)!r}, {sites!r}, {kwargs!r})")
            return argv

        BaseProcess.start = start
        spawn.get_command_line = command

    def merge(self, recorder):
        for path, process in self.expected:
            if path is None or process is None or process.poll() != 0:
                return False
            try:
                report = json.loads(path.read_text())
                if report["pid"] != process.pid:
                    return False
                recorder.merge(report)
            except (OSError, ValueError, KeyError, TypeError):
                return False
        return True


def worker(root, result, sites, kwargs):
    recorder = Recorder(Path(root), None if sites is None else set(json.loads(Path(sites).read_text())))
    recorder.install()
    BaseProcess.start = abort
    original = spawn._main
    code = None

    def main(fd, sentinel):
        nonlocal code
        code = original(fd, sentinel)
        return code

    @atexit.register
    def report():
        if code == 0 and not recorder.unknown and sys.getprofile() == recorder.profile:
            Path(result).write_text(json.dumps(recorder.report()))

    spawn._main = main
    spawn.spawn_main(**kwargs)


def trace(root, node, result, sites):
    os.chdir(root)
    recorder = Recorder(root, None if sites is None else set(json.loads(Path(sites).read_text())))
    recorder.install()
    workers = Workers(root, result, sites)
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
    if (not code and not outcome.bad and outcome.phases == {"setup", "call", "teardown"}
            and workers.merge(recorder) and not recorder.unknown and sys.getprofile() == recorder.profile):
        result.write_text(json.dumps(recorder.report()))


if __name__ == "__main__":
    trace(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), sys.argv[4] if len(sys.argv) > 4 else None)
