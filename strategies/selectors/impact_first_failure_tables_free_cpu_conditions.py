import ast
import base64
import gzip
import json
import os
from pathlib import Path

from .impact import selector
from .impact.diffparse import parse_patch
from .impact_first_failure_tables_free_cpu import FirstFailureTablesFreeCpuImpact
from .outcome_format import EXECUTOR_CLOCK_PATH, UNIXTOTIME_PATH, trace_header
from ..trace_condition_operands import CONDITION_TRACER_VERSION, condition_id, condition_sites

CONDITION_MAP = Path(__file__).parent / "impact/maps/condition-operands.json.gz"
UNSUPPORTED_HEADERS = ("old mode ", "new mode ", "rename from ", "rename to ", "copy from ", "copy to ",
                       "Binary files ", "GIT binary patch")


def _same(a, b):
    return ast.dump(a) == ast.dump(b)


def _is_not(node):
    return isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)


def condition_edits(path, before, after):
    """Return the old condition sites a patch flips, or None when it changes anything else.

    Supported edits are ``and`` <-> ``or`` and adding or removing ``not`` on a condition
    operand. Each one changes the result on every evaluation that reaches it, so only
    tests that never evaluate the site keep their behavior.
    """
    old, new = ast.parse(before), ast.parse(after)
    sites, duplicate = condition_sites(path, old)
    edits = []

    def site(node):
        key = condition_id(path, node)
        if key not in sites or key in duplicate:
            raise ValueError(key)
        edits.append(key)

    def visit(a, b):
        if _is_not(a) and not _is_not(b) and _same(a.operand, b):
            site(a.operand)
            return True
        if _is_not(b) and not _is_not(a) and _same(a, b.operand):
            site(a)
            return True
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.BoolOp) and type(a.op) is not type(b.op):
            if len(a.values) != len(b.values):
                return False
            site(a.values[0])
            return all(visit(x, y) for x, y in zip(a.values, b.values))
        if isinstance(a, ast.AST):
            return all(visit(x, y) for (_, x), (_, y) in zip(ast.iter_fields(a), ast.iter_fields(b)))
        if isinstance(a, list):
            return len(a) == len(b) and all(visit(x, y) for x, y in zip(a, b))
        return a == b

    try:
        if not visit(old, new):
            return None
    except ValueError:
        return None
    return edits or None


def patch_condition_edits(root, diff, new_files):
    lines = diff.splitlines()
    if any(line.startswith(UNSUPPORTED_HEADERS) for line in lines):
        return None
    files = parse_patch(diff)
    if not files or len([line for line in lines if line.startswith("diff --git ")]) not in (0, len(files)):
        return None
    edits = []
    for file in files:
        if (not file.old_path or file.old_path != file.new_path or not file.hunks
                or not file.path.startswith("sqlglot/") or not file.path.endswith(".py")
                or file.path in (EXECUTOR_CLOCK_PATH, UNIXTOTIME_PATH)):
            return None
        path = (root / file.path).resolve()
        after = new_files.get(file.path)
        if not path.is_relative_to(root / "sqlglot") or after is None:
            return None
        edited = condition_edits(file.path, path.read_text(encoding="utf-8"), after)
        if edited is None:
            return None
        edits.extend(edited)
    return edits


class FirstFailureTablesFreeCpuConditionsImpact(FirstFailureTablesFreeCpuImpact):
    """Skip candidates that never evaluate a condition the patch flips, per the committed map."""

    def __init__(self, catalog, config):
        super().__init__(catalog, config)
        self.catalog = catalog
        self._conditions = False
        self.condition_diagnostics = os.environ.get("BR_OUTCOME_DIAGNOSTICS") == "1"

    def _condition_map(self):
        if self._conditions is False:
            self._conditions = None
            try:
                with gzip.open(os.environ.get("BR_CONDITION_MAP", CONDITION_MAP), "rt", encoding="utf-8") as stream:
                    data = json.load(stream)
                numbers = {test["number"] for test in self.catalog}
                if (data["condition_tracer_version"] == CONDITION_TRACER_VERSION and data["committed"] is True
                        and set(data["selected"]) == numbers
                        and data["header"] == trace_header(selector.SG, self.catalog)):
                    complete = set(data["complete"]).difference(data["capped"])
                    blocked = set(data["skipped"]) | set(data["import_sites"])
                    self._conditions = data["encountered"], complete, blocked
            except (OSError, EOFError, ValueError, KeyError, TypeError) as error:
                selector.log("condition map unavailable:", repr(error))
        return self._conditions

    def _condition_drops(self, patch, analysis):
        if self.ctx is None or analysis.imp.fixture_edits or not analysis.cand:
            return set()
        try:
            edits = patch_condition_edits(Path(selector.SG).resolve(), patch.diff, analysis.imp.new_files)
        except (OSError, SyntaxError, ValueError, TypeError, UnicodeDecodeError):
            return set()
        if not edits:
            return set()
        conditions = self._condition_map()
        if conditions is None:
            return set()
        encountered, complete, blocked = conditions
        if any(key in blocked for key in edits):
            return set()
        try:
            reached = 0
            for key in edits:
                if key in encountered:
                    reached |= int.from_bytes(base64.b64decode(encountered[key]), "little")
        except (TypeError, ValueError):
            return set()
        return {number for number in analysis.cand
                if number in complete and not reached >> number & 1 and number not in analysis.must
                and number not in self.ctx.never and number not in analysis.plan.subsets}

    def _analyze_commit(self, ctx, patch):
        analysis = super()._analyze_commit(ctx, patch)
        drops = self._condition_drops(patch, analysis)
        if self.condition_diagnostics:
            selector.log(f"RWX_CONDITION_DROPS {patch.commit} {len(drops)}")
        if drops:
            analysis.cand = [number for number in analysis.cand if number not in drops]
            analysis.deferrable.difference_update(drops)
        return analysis
