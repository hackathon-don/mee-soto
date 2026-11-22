"""Skip import checks for patches that cannot change any module's post-import state.

The base selector imports every changed module in a child process, so an
import-time failure pulls in the module's importers. When a commit has no
tests to run, that child exists only for the import check.

`tools/record_import_state.py` records, at the pinned SQLGlot commit, every
function whose code ran while all `sqlglot/` and `tests/` modules were
imported. If each changed statement of a module sits inside a function body
that never ran at import or collection time (no decorator, default,
signature, docstring or class-body edit, no `super`/`__class__` change), the
module's state after import is the pinned state except for that function's
code, so importing it cannot fail differently. Only then, and only when the
patched module still compiles, is its import check dropped. A file whose
pinned source hash differs from the record, a module the map does not list,
or a map from another schema, commit or Python version keeps the normal check.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

from .impact import selector as impact_selector
from .impact.impact import FUNC_NODES
from .impact_first_failure_tables_free_cpu import FirstFailureTablesFreeCpuImpact

MAP_PATH = Path(__file__).with_name("impact_import_state.json")
SCHEMA_VERSION = 1
SQLGLOT_COMMIT = "eb11aec4be8cb2122afe713a4816626c3f0fb2fa"
_BODY_SCOPES = FUNC_NODES + (ast.Lambda,)
_SCOPES = _BODY_SCOPES + (ast.GeneratorExp,)


def load_import_state(path: Path = MAP_PATH) -> dict[str, tuple[str, frozenset[int]]] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            data["schema"] != SCHEMA_VERSION
            or data["sqlglot_commit"] != SQLGLOT_COMMIT
            or tuple(data["python"]) != tuple(sys.version_info[:2])
        ):
            return None
        return {p: (e["sha256"], frozenset(e["import_time"])) for p, e in data["files"].items()}
    except Exception:  # noqa: BLE001 - an unusable map keeps every import check
        return None


def _starts(node: ast.AST) -> set[int]:
    lines = {node.lineno}
    for dec in getattr(node, "decorator_list", ()):
        lines.add(dec.lineno)
    lines.add(min(lines))
    return lines


def _touches_state(nodes) -> bool:
    for node in nodes:
        if not isinstance(node, ast.AST):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id in ("super", "__class__"):
                return True
            if isinstance(sub, ast.Expr) and isinstance(sub.value, ast.Constant) and isinstance(sub.value.value, str):
                return True
    return False


def state_preserving(d, import_time: frozenset[int]) -> bool:
    """True when the difference only changes code that never ran at import time."""
    chain = [node for node, _ in d.path]
    fields = [f for _, f in d.path]
    outer = next(
        (i for i, node in enumerate(chain) if isinstance(node, _BODY_SCOPES) and fields[i] == "body"),
        None,
    )
    if outer is None:
        return False
    for node in chain[outer:]:
        if isinstance(node, _SCOPES) and _starts(node) & import_time:
            return False
    if _touches_state(chain[outer + 1:]) or _touches_state(d.old) or _touches_state(d.new):
        return False
    return True


class _ImportStateAnalyzer:
    """Mixin over the analyzer in use; records which import checks are provably moot."""

    import_state: dict[str, tuple[str, frozenset[int]]]

    def analyze(self, patches):
        self.state_proofs: dict[str, tuple[str, str]] = {}
        self.state_unproven: set[str] = set()
        return super().analyze(patches)

    def _diff_mask(self, path, mod, d):
        if self._state_diffs is not None:
            self._state_diffs.append(d)
        return super()._diff_mask(path, mod, d)

    def _analyze_file(self, fp, imp):
        self._state_diffs = []
        before = len(imp.import_checks)
        try:
            super()._analyze_file(fp, imp)
        finally:
            diffs, self._state_diffs = self._state_diffs, None
        added = imp.import_checks[before:]
        if not added:
            return
        proven = None
        path = fp.path
        if (
            len(added) == 1
            and diffs
            and fp.old_path == fp.new_path == path
            and path.startswith("sqlglot/")
            and isinstance(imp.new_files.get(path), str)
        ):
            try:
                rec = self.import_state.get(path)
                old = self.read_old(path) if rec else None
                if (
                    old is not None
                    and hashlib.sha256(old.encode()).hexdigest() == rec[0]
                    and all(state_preserving(d, rec[1]) for d in diffs)
                ):
                    proven = (path, imp.new_files[path])
            except Exception:  # noqa: BLE001 - no proof, keep the normal check
                proven = None
        for mod in added:
            if proven is None or mod in self.state_unproven:
                self.state_unproven.add(mod)
                self.state_proofs.pop(mod, None)
            else:
                self.state_proofs[mod] = proven


class ImportStateImpact(FirstFailureTablesFreeCpuImpact):
    """`impact-first-failure-tables-free-cpu` without import checks the import-state map proves moot."""

    def __init__(self, catalog, config):
        super().__init__(catalog, config)
        self._import_state_on = False
        if self.ctx is None:
            return
        state = load_import_state()
        if state is None:
            print("import state map unusable: keeping every import check", file=sys.stderr, flush=True)
            return
        an = self.ctx.an
        an.__class__ = type(f"ImportState{type(an).__name__}", (_ImportStateAnalyzer, type(an)), {})
        an.import_state = state
        an._state_diffs = None
        self._import_state_on = True

    def select(self, commits):
        if not self._import_state_on:
            return super().select(commits)
        choose, an = impact_selector.choose, self.ctx.an

        def choose_without_moot_imports(plan, *args, **kwargs):
            choose(plan, *args, **kwargs)
            if plan.run or not plan.pending_imports:
                return
            for mod in list(plan.pending_imports):
                proof = an.state_proofs.get(mod)
                if proof is None or mod in an.state_unproven:
                    continue
                try:
                    compile(proof[1], proof[0], "exec", dont_inherit=True)
                except Exception:  # noqa: BLE001 - keep the normal check
                    continue
                del plan.pending_imports[mod]
                plan.notes.append(f"import state unchanged {mod}")

        impact_selector.choose = choose_without_moot_imports
        try:
            return super().select(commits)
        finally:
            impact_selector.choose = choose
