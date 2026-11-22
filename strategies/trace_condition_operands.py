"""Record the truth value of every condition operand a test evaluates in SQLGlot.

A condition operand is an expression whose truth value Python tests: an ``if``,
``while``, conditional-expression or ``assert`` test, a comprehension filter,
each operand of ``and``/``or`` and the operand of ``not``. Each one is wrapped
in a hook that records ``bool(value)`` and returns the value unchanged, so the
traced program keeps its short-circuit order and its effects.
"""

import ast
import json
import os
from pathlib import Path
import sys

from . import trace_operator_outcomes as base

TRUTH = "Truth"
CONDITION_TRACER_VERSION = 1


def condition_id(path, node):
    return f"{path}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}:C"


def truth_positions(tree):
    """Yield (parent, field, index, node) for every condition operand in a module."""
    for parent in ast.walk(tree):
        if isinstance(parent, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            yield parent, "test", None, parent.test
        elif isinstance(parent, ast.comprehension):
            for i, node in enumerate(parent.ifs):
                yield parent, "ifs", i, node
        elif isinstance(parent, ast.BoolOp):
            for i, node in enumerate(parent.values):
                yield parent, "values", i, node
        elif isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
            yield parent, "operand", None, parent.operand


def condition_sites(path, tree):
    """Map each condition site of one module to its line span; duplicate spans are ambiguous."""
    found, duplicate = {}, set()
    for _, _, _, node in truth_positions(tree):
        key = condition_id(path, node)
        if key in found:
            duplicate.add(key)
        found[key] = [node.lineno, node.end_lineno]
    return found, duplicate


class ConditionRewrite(ast.NodeTransformer):
    def __init__(self, path, names, targets):
        self.path, self.targets = path, targets
        self.skipped = set()

    def visit_Module(self, tree):
        _, duplicate = condition_sites(self.path, tree)
        self.skipped.update(duplicate)
        wraps = []
        for parent, field, index, node in truth_positions(tree):
            key = condition_id(self.path, node)
            if key in duplicate or (self.targets is not None and key not in self.targets):
                continue
            wraps.append((parent, field, index, node, key))
        for parent, field, index, node, key in wraps:
            call = ast.copy_location(ast.Call(ast.Name(base.HOOK, ast.Load()),
                                              [ast.Constant(key), ast.Constant(TRUTH), node,
                                               ast.Constant(None), ast.Constant(False)], []), node)
            if index is None:
                setattr(parent, field, call)
            else:
                getattr(parent, field)[index] = call
        return tree


class ConditionRecorder(base.Recorder):
    def observe(self, key, op, value, _right, _chain):
        if not self.unknown:
            counts = self.rows.get(key, ())
            if counts is not None:
                try:
                    truth = bool(value)
                except Exception:
                    self.rows[key] = None
                else:
                    if not counts:
                        counts = self.rows[key] = [0, 0]
                    counts[0 if truth else 1] += 1
        return value

    def report(self):
        data = {"pid": os.getpid(), "sites": dict(self.rows), "skipped": sorted(self.skipped)}
        if self.import_sites is not None:
            data["import_sites"] = sorted(self.import_sites)
        return data


def trace(root, node, result):
    os.chdir(root)
    base.Rewrite = ConditionRewrite
    base.OPERATORS[TRUTH] = lambda value, _right: value
    recorder = ConditionRecorder(root, None)
    recorder.isolate(result, collecting=True)
    recorder.install()
    # Collection-time evaluations never back a drop, so the safety profiler only needs to watch test runs;
    # profiling every import call would cost about 20x on each process start.
    sys.setprofile(None)
    workers = base.Workers(root, result, None, True)
    import pytest

    class Outcome:
        phases = set()
        bad = False

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item):
            recorder.collecting = False
            sys.setprofile(recorder.profile)
            try:
                yield
            finally:
                recorder.collecting = True

        def pytest_runtest_logreport(self, report):
            if report.nodeid == node and report.passed and not getattr(report, "wasxfail", None):
                self.phases.add(report.when)
            elif not report.passed:
                self.bad = True

    outcome = Outcome()
    code = pytest.main([node, "-q", "-p", "no:cacheprovider"], plugins=[outcome])
    # Worker processes would run unrecorded SQLGlot code, so a test that starts one stays unknown.
    complete = (not code and not outcome.bad and outcome.phases == {"setup", "call", "teardown"}
                and not workers.expected and not recorder.unknown and sys.getprofile() == recorder.profile)
    data = recorder.report() if complete else {"import_sites": sorted(recorder.import_sites)}
    data["complete"] = complete
    result.write_text(json.dumps(data, separators=(",", ":")))


if __name__ == "__main__":
    root, node, result = sys.argv[1:]
    trace(Path(root), node, Path(result))
