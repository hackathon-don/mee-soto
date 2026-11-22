"""Collect operand observations for one catalog test and all its spawn workers."""

import ast
import atexit
import datetime
import enum
from hashlib import sha256
import importlib.abc
import importlib.machinery
import inspect
import json
import marshal
import operator
import os
import sys
import threading
from time import time as wall_time
from types import CodeType
from itertools import count
from multiprocessing import spawn
from multiprocessing.context import SpawnProcess
from multiprocessing.process import BaseProcess
from pathlib import Path

from .selectors.outcome_format import EXECUTOR_CLOCK_PATH, OPS, executor_clock_lines, site_id


HOOK = "__rwx_record_comparison__"
BATCH_REJECTION = "RWX_OUTCOME_BATCH_REJECT "
CAP = 64
UNIXTOTIME_PATH = "sqlglot/executor/env.py"
UNIXTOTIME_HASH = "1c032c459b232a7b426a05fca18bb6007fe7d8d56b73b291630f570b5575b23e"
UNIXTOTIME_LINE = 333
DATETIME_TYPE = datetime.datetime
TIMEZONE_TYPE = datetime.timezone
SIMPLE = (type(None), bool, int, float, str, bytes)
SIMPLE_TAGS = {id(cls): cls.__name__ for cls in SIMPLE}
FAST_SCALARS = (type(None), bool, int, float, str)
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
    if type(cls) is enum.EnumType:
        def method(name):
            for base in cls.__mro__:
                namespace = vars(base)
                if name in namespace:
                    return namespace[name]
            return None

        eq, ne = method("__eq__"), method("__ne__")
        if not any(eq is a and ne is b for a, b in (
                (object.__eq__, object.__ne__), (str.__eq__, str.__ne__), (int.__eq__, int.__ne__))):
            return None
        hash_method, length = method("__hash__"), method("__len__")
        if (not any(hash_method is method for method in
                    (object.__hash__, enum.Enum.__hash__, str.__hash__, int.__hash__))
                or method("__getattribute__") is not object.__getattribute__
                or method("__bool__") is not None
                or (length is not None and length is not str.__len__)):
            return None
        name = inspect.getattr_static(value, "_name_", None)
        if type(name) is str and cls.__members__.get(name) is value:
            scalar = (str.__str__(value) if eq is str.__eq__ else
                      int.__int__(value) if eq is int.__eq__ else None)
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


def operand_tag(value):
    cls = type(value)
    tag = SIMPLE_TAGS.get(id(cls))
    if tag is not None:
        return tag
    if type(cls) is enum.EnumType and describe(value) is not None:
        return f"{cls.__module__}.{cls.__qualname__}"
    return None


def builtin_rows():
    return {
        id(left): {
            id(right): tuple(
                (left.__name__, right.__name__, same, (same, not same, equal, not equal, member))
                for member in (None, False, True)
                for same in (False, True)
                for equal in (False, True)
            )
            for right in FAST_SCALARS
        }
        for left in FAST_SCALARS
    }


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


def batch_rejection(reason):
    print(BATCH_REJECTION + reason, file=sys.stderr, flush=True)


def abort(*args, reason="unsupported process start", **kwargs):
    batch_rejection(reason)
    os._exit(125)


class Recorder(importlib.abc.MetaPathFinder):
    def __init__(self, root, targets, diagnose=False):
        self.root, self.targets = root, targets
        self.paths = None if targets is None else {site.split(":", 1)[0] for site in targets}
        self.rows, self.skipped = {}, set()
        self.unknown = False
        self.diagnose = diagnose
        self.loading = None
        self.executor_clock = None
        self.unix_to_time = None
        self.compact = os.environ.get("BR_OUTCOME_COMPACT") == "1"
        cache = os.environ.get("BR_OUTCOME_CODE_CACHE")
        self.cache = Path(cache) if cache else None
        self.rewrite_only = os.environ.get("BR_OUTCOME_PROFILE") == "rewrite"
        self.encounters = None
        if self.compact and os.environ.get("BR_OUTCOME_FAST_BUILTINS") == "1":
            self.builtins = builtin_rows()
            self.observe = self.observe_builtins

    def reject(self, reason):
        if not self.unknown and self.diagnose:
            batch_rejection(reason)
        self.unknown = True

    def install(self):
        if any(name == "sqlglot" or name.startswith("sqlglot.") for name in sys.modules):
            abort(reason="SQLGlot was imported before the recorder was installed")
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
                self.reject(f"unexpected thread start from {caller}")
            return original(thread, *args, **kwargs)

        threading.Thread.start = start
        sys.setprofile(self.profile)

    def profile(self, frame, event, arg):
        if event == "c_call" and relevant(frame):
            module = getattr(arg, "__module__", None)
            owner = getattr(arg, "__self__", None)
            name = getattr(arg, "__name__", "")
            if (arg is wall_time and self.executor_clock is not None
                    and frame.f_globals.get("__name__") == "sqlglot.executor"
                    and frame.f_code is self.executor_clock[0] and frame.f_lineno in self.executor_clock[1]):
                return
            if (owner is DATETIME_TYPE and name == "fromtimestamp" and frame.f_code is self.unix_to_time
                    and frame.f_lineno == UNIXTOTIME_LINE
                    and frame.f_globals.get("__name__") == "sqlglot.executor.env"
                    and frame.f_globals.get("datetime") is datetime and datetime.timezone is TIMEZONE_TYPE):
                return
            iso_parse = name == "fromisoformat" and (
                owner is datetime.date or owner is datetime.datetime or owner is datetime.time
            )
            if (module in ("time", "_thread", "_random") or
                    module in ("os", "posix") and name in ("urandom", "getrandom") or
                    isinstance(owner, type) and owner.__module__ == "datetime" and not iso_parse or
                    getattr(type(owner), "__module__", "") in ("_random", "numpy.random._generator")):
                self.reject(f"nondeterministic call {module or type(owner).__name__}.{name} "
                            f"at {frame.f_code.co_filename}:{frame.f_lineno}")
        elif event == "call" and frame.f_back and relevant(frame.f_back):
            module = frame.f_globals.get("__name__")
            if (module in ("random", "secrets") or module == "inspect"
                    and frame.f_code.co_name in ("getsource", "getsourcelines", "currentframe")):
                self.reject(f"unsupported call {module}.{frame.f_code.co_name} "
                            f"at {frame.f_back.f_code.co_filename}:{frame.f_back.f_lineno}")

    def audit(self, event, args):
        if event in ("subprocess.Popen", "os.fork", "os.forkpty", "os.posix_spawn",
                     "os.spawn", "os.exec", "os.system", "cpython.PyInterpreterState_New"):
            abort(reason=f"unsupported process event {event}")
        if event == "open" and isinstance(args[0], (str, bytes)):
            path = os.path.abspath(os.fsdecode(args[0]))
            if path == self.loading or not path.startswith(str(self.root / "sqlglot") + os.sep):
                return
            if path.endswith((".py", ".pyc")) and relevant(sys._getframe(1)):
                self.reject(f"direct read of SQLGlot source {path}")

    def find_spec(self, fullname, path, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None:
            return None
        if not isinstance(spec.loader, importlib.machinery.SourceFileLoader) or spec.origin is None:
            self.reject(f"unsupported loader for {fullname}")
            return spec
        origin = Path(spec.origin)
        if not origin.is_relative_to(self.root):
            self.reject(f"unexpected origin for {fullname}: {origin}")
            return spec
        rel = origin.relative_to(self.root).as_posix()
        if self.paths is None or rel in self.paths or rel in (EXECUTOR_CLOCK_PATH, UNIXTOTIME_PATH):
            spec.loader = Loader(fullname, spec.origin, self, rel)
        return spec

    def observe_builtins(self, key, op, left, right, chain):
        if key is None or self.rewrite_only or self.unknown:
            return type(self).observe(self, key, op, left, right, chain)
        seen = self.rows.get(key, ())
        if seen is None and self.encounters is None:
            return OPERATORS[op](left, right)
        lt, rt = type(left), type(right)
        left_rows = self.builtins.get(id(lt))
        templates = left_rows.get(id(rt)) if left_rows else None
        if templates is None:
            return type(self).observe(self, key, op, left, right, chain)
        try:
            same, equal = left is right, left == right
            member = (left in right) if lt is str and rt is str else None
        except Exception:
            return type(self).observe(self, key, op, left, right, chain)
        if self.encounters is not None:
            self.encounters[key] = self.encounters.get(key, 0) + 1
        if seen is not None:
            index = (0 if member is None else 8 if member else 4) + 2 * same + equal
            if seen:
                seen.add(templates[index])
                if len(seen) > CAP:
                    self.rows[key] = None
            else:
                self.rows[key] = {templates[index]}
        if op == "Is":
            return same
        if op == "IsNot":
            return not same
        if op == "Eq":
            return equal
        if op == "NotEq":
            return not equal
        if op == "In" and member is not None:
            return member
        return OPERATORS[op](left, right)

    def observe(self, key, op, left, right, chain):
        if self.encounters is not None and key is not None:
            self.encounters[key] = self.encounters.get(key, 0) + 1
        if not self.rewrite_only and not self.unknown and self.rows.get(key, ()) is not None:
            try:
                describe_operand = operand_tag if self.compact else describe
                a, b = describe_operand(left), describe_operand(right)
                if a is None or b is None:
                    if key is not None:
                        self.rows[key] = None
                    if chain:
                        self.reject(f"unsupported chained comparison operand at {key or op}")
                elif key is not None:
                    same = left is right
                    if self.compact:
                        outcomes = (same, not same, alternate("Eq", left, right),
                                    alternate("NotEq", left, right), alternate("In", left, right))
                        row = (a, b, same, outcomes)
                    else:
                        row = json.dumps([a, b, same, [alternate(o, left, right) for o in OPS]],
                                         separators=(",", ":"))
                    seen = self.rows.setdefault(key, set())
                    seen.add(row)
                    if len(seen) > CAP:
                        self.rows[key] = None
            except Exception:
                if key is not None:
                    self.rows[key] = None
                if chain:
                    self.reject(f"chained comparison observation failed at {key or op}")
        return OPERATORS[op](left, right)

    def report(self):
        def encode(values):
            if values is None:
                return None
            if self.compact:
                return sorted(json.dumps(v, separators=(",", ":")) for v in values)
            return sorted(values)

        return {"pid": os.getpid(), "sites": {k: encode(v) for k, v in self.rows.items()},
                "skipped": sorted(self.skipped)}

    def merge(self, report):
        self.skipped.update(report["skipped"])
        for key, values in report["sites"].items():
            if values is None:
                self.rows[key] = None
            elif key not in self.rows or self.rows[key] is not None:
                seen = self.rows.setdefault(key, set())
                if self.compact:
                    values = [(a, b, same, tuple(outcomes))
                              for a, b, same, outcomes in map(json.loads, values)]
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
        artifact = None
        if self.recorder.cache is not None:
            try:
                digest, optimize, code, skipped, collision = marshal.loads(
                    (self.recorder.cache / self.rel).read_bytes())
                if digest == sha256(source).digest() and optimize == sys.flags.optimize:
                    artifact = code, skipped, collision
            except (OSError, EOFError, ValueError, TypeError):
                pass
        code, skipped, collision = (artifact if artifact is not None else
                                    rewrite_code(source, self.path, self.rel, self.recorder.targets))
        if collision:
            self.recorder.reject(f"comparison observer name collision in {self.rel}")
        if self.rel == EXECUTOR_CLOCK_PATH:
            lines = executor_clock_lines(source)
            functions = [part for part in code.co_consts
                         if isinstance(part, CodeType) and part.co_qualname == "execute"]
            self.recorder.executor_clock = (functions[0], lines) if lines and len(functions) == 1 else None
        elif self.rel == UNIXTOTIME_PATH:
            self.recorder.unix_to_time = None
            if sha256(source).hexdigest() == UNIXTOTIME_HASH:
                functions = [part for part in code.co_consts
                             if isinstance(part, CodeType) and part.co_name == "<lambda>"
                             and part.co_firstlineno == UNIXTOTIME_LINE]
                if len(functions) == 1:
                    self.recorder.unix_to_time = functions[0]
        self.recorder.skipped.update(skipped)
        return code


def rewrite_code(source, path, rel, targets):
    tree = ast.parse(source, path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    if HOOK in names:
        return compile(source, path, "exec", dont_inherit=True), (), True
    rewrite = Rewrite(rel, names, targets)
    tree = rewrite.visit(tree)
    return compile(ast.fix_missing_locations(tree), path, "exec", dont_inherit=True), sorted(rewrite.skipped), False


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
        if (code == 0 and (recorder.rewrite_only or not recorder.unknown)
                and sys.getprofile() == recorder.profile):
            Path(result).write_text(json.dumps(recorder.report()))

    spawn._main = main
    spawn.spawn_main(**kwargs)


def trace(root, node, result, sites):
    os.chdir(root)
    plain = os.environ.get("BR_OUTCOME_PROFILE") == "plain"
    recorder = Recorder(root, None if sites is None else set(json.loads(Path(sites).read_text())))
    if not plain:
        recorder.install()
    workers = None if plain else Workers(root, result, sites)
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
    if not code and not outcome.bad and outcome.phases == {"setup", "call", "teardown"}:
        if plain:
            result.write_text("{}")
        elif workers.merge(recorder) and sys.getprofile() == recorder.profile:
            if recorder.rewrite_only:
                result.write_text("{}")
            elif not recorder.unknown:
                result.write_text(json.dumps(recorder.report()))


def trace_batch(root, manifest, result, sites):
    from .selectors.impact.runner import child_main
    from .selectors.impact.selector import IMPORT_PROBE_TIMEOUT, preimport
    from .trace_operator_batch import batch_diagnostics, replay_limits

    batch = json.loads(Path(manifest).read_text())
    recorder = Recorder(root, None if sites is None else set(json.loads(Path(sites).read_text())),
                        diagnose=True)
    preimport()
    recorder.install()
    workers = Workers(root, result, sites)
    events = result.with_suffix(".events")
    cases = replay_limits(batch["tests"])
    with batch_diagnostics(cases, IMPORT_PROBE_TIMEOUT, batch_rejection):
        child_main(str(root), cases, os.open(events, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                   import_checks=batch["imports"], import_timeout=IMPORT_PROBE_TIMEOUT)
    records = [json.loads(line) for line in events.read_text().splitlines()]
    checked = [event for event in records if "d" in event]
    imported = [event for event in records if "i" in event]
    if recorder.unknown:
        return
    failed = next((event for event in records if event.get("ok") is False), None)
    if failed is not None and "i" in failed:
        batch_rejection(f"unsuccessful import probe {failed['i']}")
    elif failed is not None and "d" in failed:
        batch_rejection(f"unsuccessful verifier result for check {failed['d']}")
    elif not records or "cpu_total" not in records[-1]:
        batch_rejection("verifier did not report normal completion")
    elif sorted(event["d"] for event in checked) != sorted(test[0] for test in batch["tests"]):
        batch_rejection(f"incomplete check results ({len(checked)}/{len(batch['tests'])})")
    elif [event["i"] for event in imported] != batch["imports"]:
        batch_rejection(f"incomplete or out-of-order import probes ({len(imported)}/{len(batch['imports'])})")
    elif sys.getprofile() != recorder.profile:
        batch_rejection("recorder profile hook was replaced")
    elif not workers.merge(recorder):
        batch_rejection("worker report missing, incomplete, or invalid")
    else:
        result.write_text(json.dumps(recorder.report()))


if __name__ == "__main__":
    mode, root, target, result, *flags = sys.argv[1:]
    (trace_batch if mode == "batch" else trace)(
        Path(root), target, Path(result), flags[0] if flags else None)
