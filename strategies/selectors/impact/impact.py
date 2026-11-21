"""Which tests can a patch affect? Maps AST differences to candidate tests.

All masks are Python ints with bit n set for test number n.
"""

from __future__ import annotations

import ast
import gzip
import os
import pickle
from dataclasses import dataclass, field

from .diffparse import FilePatch, apply_patch

FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.GeneratorExp)
# Names too generic to narrow anything; references to these say nothing.
GENERIC = frozenset({"self", "cls", "exp", "t", "typing", "super", "args", "kwargs"})


class Index:
    def __init__(self, index_path: str, static_path: str | None = None):
        with gzip.open(index_path, "rb") as fh:
            data = pickle.load(fh)
        self.n_tests: int = data["n_tests"]
        self.sets: list[int] = data["sets"]
        self.run: dict[str, dict[int, int]] = data["run"]
        self.deft: dict[str, dict[int, int]] = data["deft"]
        self.inits: dict[str, int] = data["inits"]
        self.files: dict[str, int] = data["files"]
        self.baseline_fail: list[int] = data["baseline_fail"]
        self.pseudo: dict = data.get("pseudo", {})
        n_all = self.n_tests
        for p in self.pseudo.values():
            n_all = max(n_all, p["setup"] + 1, max(p["queries"].values(), default=0) + 1)
        self.all_mask = (1 << n_all) - 1
        self.names_run: dict[str, int] = {}
        self.names_def: dict[str, int] = {}
        self.class_counts: dict[str, int] = {}
        self.names_isinst: dict[str, int] = {}
        self.has_static = False
        if static_path and os.path.exists(static_path):
            with gzip.open(static_path, "rb") as fh:
                st = pickle.load(fh)
            self.names_run = st["names_run"]
            self.names_def = st["names_def"]
            self.class_counts = st.get("class_counts", {})
            self.names_isinst = st.get("names_isinst", {})
            self.has_static = True
        self._importers: dict[str, int] = {}

    def _mask(self, table: dict[str, dict[int, int]], path: str, lo: int, hi: int) -> int:
        d = table.get(path)
        if not d:
            return 0
        sets = self.sets
        m = 0
        if hi - lo < len(d):
            for l in range(lo, hi + 1):
                i = d.get(l)
                if i is not None:
                    m |= sets[i]
        else:
            for l, i in d.items():
                if lo <= l <= hi:
                    m |= sets[i]
        return m

    def run_mask(self, path, lo, hi):
        return self._mask(self.run, path, lo, hi)

    def def_mask(self, path, lo, hi):
        return self._mask(self.deft, path, lo, hi)

    def importers(self, path: str) -> int:
        m = self._importers.get(path)
        if m is None:
            m = 0
            for i in set((self.deft.get(path) or {}).values()):
                m |= self.sets[i]
            for i in set((self.run.get(path) or {}).values()):
                m |= self.sets[i]
            self._importers[path] = m
        return m

    def name_mask(self, name: str) -> int:
        """Tests that ran code reading `name` (at call time or import time)."""
        if not self.has_static:
            return self.all_mask
        return self.names_run.get(name, 0) | self.names_def.get(name, 0) | self.names_isinst.get(name, 0)

    def run_name_mask(self, name: str) -> int:
        """Tests that ran function code reading `name`."""
        if not self.has_static:
            return self.all_mask
        return self.names_run.get(name, 0)

    def init_mask(self, key: str) -> int:
        i = self.inits.get(key)
        return self.sets[i] if i is not None else 0

    def file_mask(self, path: str) -> int:
        i = self.files.get(path)
        return self.sets[i] if i is not None else 0


@dataclass
class Impact:
    candidates: int = 0  # tests that may fail; verify by running or include
    must_fail: int = 0  # tests known to fail (e.g. syntax error on import)
    import_checks: list[str] = field(default_factory=list)  # modules to import-check
    import_fail_masks: dict[str, int] = field(default_factory=dict)  # module -> importers
    notes: list[str] = field(default_factory=list)
    new_files: dict[str, str | None] = field(default_factory=dict)
    code_effect: bool = False  # some Python change was not a no-op
    fixture_edits: list[str] = field(default_factory=list)  # edited files under tests/fixtures/


def module_name(path: str) -> str:
    mod = path[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return mod


# ---------------------------------------------------------------- AST helpers


def _start(node) -> int:
    lines = [node.lineno]
    for d in getattr(node, "decorator_list", ()) or ():
        lines.append(d.lineno)
    return min(lines)


def _span(node) -> tuple[int, int]:
    return _start(node), getattr(node, "end_lineno", None) or node.lineno


def _names_in(node) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.add(sub.attr)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value.isidentifier():
            out.add(sub.value)
    return out


def _targets(stmt) -> set[str]:
    out: set[str] = set()
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.add(stmt.name)
    elif isinstance(stmt, ast.Assign):
        for t in stmt.targets:
            for sub in ast.walk(t):
                if isinstance(sub, ast.Name):
                    out.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    out.add(sub.attr)
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        for sub in ast.walk(stmt.target):
            if isinstance(sub, ast.Name):
                out.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                out.add(sub.attr)
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for a in stmt.names:
            out.add((a.asname or a.name).split(".")[0])
    return out


class _Diff:
    """One minimal difference between the old and new AST, seen from the old side."""

    __slots__ = ("path", "old", "new", "kind")

    def __init__(self, path, old, new, kind):
        self.path = path  # list of (old_node, field) from the root down to the difference
        self.old = old  # old-side nodes involved (list)
        self.new = new  # new-side nodes involved (list)
        self.kind = kind  # "node" | "list"


def _key(node, lines, touched_old: bool) -> str:
    return ast.dump(node, annotate_fields=False, include_attributes=False)


def _ast_diff(a, b, path, out, ctx):
    if type(a) is not type(b):
        out.append(_Diff(path, [a], [b], "node"))
        return
    if isinstance(a, ast.AST):
        for f in a._fields:
            va = getattr(a, f, None)
            vb = getattr(b, f, None)
            if isinstance(va, list) and isinstance(vb, list):
                _list_diff(a, f, va, vb, path, out, ctx)
            else:
                _ast_diff(va, vb, path + [(a, f)], out, ctx)
    elif a != b:
        out.append(_Diff(path, [a], [b], "node"))


def _untouched(node, changed: set[int]) -> bool:
    if not isinstance(node, (ast.stmt, ast.expr)) or not hasattr(node, "lineno"):
        return False
    lo, hi = _span(node)
    return not any(lo <= l <= hi for l in changed) if changed else True


def _list_diff(parent, f, va, vb, path, out, ctx):
    old_changed, new_changed = ctx
    if len(va) == len(vb):
        for i, (x, y) in enumerate(zip(va, vb)):
            if _untouched(x, old_changed) and _untouched(y, new_changed):
                continue
            _ast_diff(x, y, path + [(parent, f)], out, ctx)
        return
    import difflib

    ka = [ast.dump(x, include_attributes=False) if isinstance(x, ast.AST) else repr(x) for x in va]
    kb = [ast.dump(y, include_attributes=False) if isinstance(y, ast.AST) else repr(y) for y in vb]
    sm = difflib.SequenceMatcher(a=ka, b=kb, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and i2 - i1 == j2 - j1:
            for x, y in zip(va[i1:i2], vb[j1:j2]):
                _ast_diff(x, y, path + [(parent, f)], out, ctx)
            continue
        out.append(_Diff(path + [(parent, f)], va[i1:i2], vb[j1:j2], "list"))


def _changed_lines(fp: FilePatch) -> tuple[set[int], set[int]]:
    """Old lines touched (removed, or adjacent to an insertion) and new lines added."""
    old_set: set[int] = set()
    new_set: set[int] = set()
    for h in fp.hunks:
        o = h.old_start if h.old_len else h.old_start + 1
        n = h.new_start if h.new_len else h.new_start + 1
        for l in h.lines:
            s = l[0]
            if s == " ":
                o += 1
                n += 1
            elif s == "-":
                old_set.add(o)
                o += 1
            else:
                new_set.add(n)
                old_set.add(o - 1)
                old_set.add(o)
                n += 1
    return old_set, new_set


# ---------------------------------------------------------------- analysis


class Analyzer:
    def __init__(self, index: Index, base_root: str):
        self.ix = index
        self.base = base_root

    def read_old(self, path: str) -> str | None:
        try:
            with open(os.path.join(self.base, path), encoding="utf-8") as fh:
                return fh.read()
        except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
            return None

    def analyze(self, patches: list[FilePatch]) -> Impact:
        imp = Impact()
        for fp in patches:
            try:
                self._analyze_file(fp, imp)
            except Exception as e:  # noqa: BLE001 - never lose a commit to an analysis bug
                imp.notes.append(f"error {fp.path}: {e!r}")
                imp.candidates |= self._fallback(fp.path)
        imp.candidates &= ~imp.must_fail
        return imp

    def _fallback(self, path: str) -> int:
        m = self.ix.importers(path) | self.ix.file_mask(path)
        return m if m else 0

    def _analyze_file(self, fp: FilePatch, imp: Impact) -> None:
        path = fp.path
        if fp.old_path and fp.new_path and fp.old_path != fp.new_path:
            imp.candidates |= self.ix.importers(fp.old_path)
        old = self.read_old(fp.old_path) if fp.old_path else None
        if fp.old_path and old is None:
            # Unknown or binary file: anything that read it is a candidate.
            imp.candidates |= self.ix.file_mask(path)
            imp.notes.append(f"unreadable {path}")
            return
        new = apply_patch(old or "", fp) if fp.new_path else None
        if fp.new_path and fp.old_path and fp.new_path != fp.old_path:
            imp.new_files[fp.old_path] = None
            imp.new_files[fp.new_path] = new
        else:
            imp.new_files[fp.new_path or fp.old_path] = new  # type: ignore[index]

        is_py = path.endswith(".py") and (path.startswith("sqlglot/") or path.startswith("tests/"))
        if not is_py:
            m = self.ix.file_mask(path)
            if fp.old_path is None:
                parent = os.path.dirname(path)
                m |= self.ix.file_mask(parent)
            imp.candidates |= m
            imp.notes.append(f"data {path}: {bin(m).count('1')} tests")
            if path.startswith("tests/fixtures/") and fp.old_path and fp.new_path == fp.old_path:
                imp.fixture_edits.append(path[len("tests/fixtures/"):])
            return

        if fp.old_path is None:
            # A new module nobody imports at the pinned commit.
            imp.notes.append(f"new module {path}")
            return
        importers = self.ix.importers(path)
        if new is None:
            imp.code_effect = True
            imp.must_fail |= importers
            imp.notes.append(f"deleted {path}")
            return
        try:
            new_tree = ast.parse(new)
        except SyntaxError:
            imp.code_effect = True
            imp.must_fail |= importers
            imp.notes.append(f"syntax error {path}: {bin(importers).count('1')} importers fail")
            return
        old_tree = ast.parse(old)
        old_changed, new_changed = _changed_lines(fp)
        diffs: list[_Diff] = []
        _ast_diff(old_tree, new_tree, [], diffs, (old_changed, new_changed))
        if not diffs:
            imp.notes.append(f"no-op {path}")
            return
        imp.code_effect = True
        mod = module_name(path)
        imp.import_checks.append(mod)
        imp.import_fail_masks[mod] = importers
        for d in diffs:
            m = self._diff_mask(path, mod, d)
            imp.candidates |= m

    # A difference inside a function body affects tests that executed the
    # enclosing statement; at definition level it affects users of the class
    # or module that also run code reading the changed names.
    def _diff_mask(self, path: str, mod: str, d: _Diff) -> int:
        ix = self.ix
        chain = [node for node, _ in d.path]
        fields = [f for _, f in d.path]
        # Innermost function-like scope whose *body* contains the difference.
        scope_i = None
        for i in range(len(chain) - 1, -1, -1):
            node = chain[i]
            if isinstance(node, SCOPE_NODES):
                f = fields[i]
                if isinstance(node, FUNC_NODES) and f != "body":
                    continue  # decorators / args / returns are definition-time
                if isinstance(node, ast.Lambda) and f != "body":
                    # Lambda defaults are evaluated where the lambda is created.
                    continue
                scope_i = i
                break
        if scope_i is not None:
            return self._runtime_mask(path, chain, fields, scope_i, d)
        return self._deftime_mask(path, mod, chain, fields, d)

    def _runtime_mask(self, path, chain, fields, scope_i, d) -> int:
        ix = self.ix
        scope = chain[scope_i]
        if isinstance(scope, (ast.Lambda, ast.GeneratorExp)):
            lo, hi = _span(scope)
            return ix.run_mask(path, lo, hi)
        # Deepest statement below the scope that contains the difference.
        stmt_i = None
        for i in range(len(chain) - 1, scope_i, -1):
            if isinstance(chain[i], ast.stmt):
                stmt_i = i
                break
        if stmt_i is None:
            # Statement list of the function body itself changed.
            return ix.run_mask(path, *_span(scope))
        stmt = chain[stmt_i]
        field_below = fields[stmt_i]
        if d.kind == "list" and stmt_i == len(chain) - 1 and field_below in (
            "body",
            "orelse",
            "finalbody",
            "handlers",
            "cases",
        ):
            # Statements inserted/removed in a block of `stmt`: whoever ran the block's neighbourhood.
            return ix.run_mask(path, *_span(stmt))
        lo, hi = _span(stmt)
        if field_below in ("body", "orelse", "finalbody", "handlers", "cases"):
            return ix.run_mask(path, lo, hi)
        # Header of a compound statement or a simple statement.
        first_child = None
        for f in ("body", "handlers", "cases"):
            v = getattr(stmt, f, None)
            if isinstance(v, list) and v and hasattr(v[0], "lineno"):
                first_child = v[0]
                break
        if first_child is not None and isinstance(stmt, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.Match)):
            hi = max(lo, _start(first_child) - 1)
        if isinstance(stmt, FUNC_NODES + (ast.ClassDef,)):
            # Nested def header: it runs where the outer function runs, and
            # its body runs when called.
            return ix.run_mask(path, *_span(stmt))
        return ix.run_mask(path, lo, hi)

    def _class_users(self, mod: str, cls_chain: list[ast.ClassDef], path: str) -> int:
        ix = self.ix
        qual = ".".join(c.name for c in cls_chain)
        cls = cls_chain[-1]
        m = ix.init_mask(f"{mod}:{qual}")
        m |= ix.run_mask(path, *_span(cls))
        # Code that names the class. A name shared by several classes (every
        # dialect has a nested `Tokenizer`) says nothing, so use the innermost
        # enclosing class whose name is unique.
        for c in reversed(cls_chain):
            if ix.class_counts.get(c.name, 1) <= 1:
                m |= ix.run_name_mask(c.name)
                break
        return m

    def _deftime_mask(self, path, mod, chain, fields, d) -> int:
        ix = self.ix
        classes = [n for n in chain if isinstance(n, ast.ClassDef)]
        funcs_i = [i for i, n in enumerate(chain) if isinstance(n, FUNC_NODES)]
        # Definition-time parts of a function (decorators, defaults, signature).
        if funcs_i:
            fn = chain[funcs_i[-1]]
            m = ix.run_mask(path, *_span(fn))
            if fields[funcs_i[-1]] == "decorator_list":
                m |= ix.def_mask(path, *_span(fn)) & ix.name_mask(fn.name)
            return m
        # Find the definition-level statement and its owner (class or module).
        stmt = None
        for i in range(len(chain) - 1, -1, -1):
            if isinstance(chain[i], ast.stmt):
                stmt = chain[i]
                break
        if d.kind == "list" and chain and fields[-1] == "body" and isinstance(chain[-1], (ast.Module, ast.ClassDef)):
            # Statements added or removed at module/class level.
            owner = chain[-1]
            names: set[str] = set()
            for n in d.old + d.new:
                if isinstance(n, ast.stmt):
                    names |= _targets(n)
            removed_funcs = [n for n in d.old if isinstance(n, FUNC_NODES + (ast.ClassDef,))]
            added_defs = [n for n in d.new if isinstance(n, FUNC_NODES + (ast.ClassDef,))]
            if isinstance(owner, ast.ClassDef):
                users = self._class_users(mod, classes, path)
                m = 0
                for n in removed_funcs:
                    m |= ix.run_mask(path, *_span(n))
                if added_defs or not names:
                    m |= users
                else:
                    m |= users & self._names_mask(names)
                return m
            importers = ix.importers(path)
            if not names or added_defs or removed_funcs:
                m = importers if not names else importers & self._names_mask(names)
                for n in removed_funcs:
                    m |= ix.run_mask(path, *_span(n))
                return m
            return importers & self._names_mask(names)
        if stmt is None:
            return ix.importers(path)
        lo, hi = _span(stmt)
        executed = ix.def_mask(path, lo, hi)
        if isinstance(stmt, ast.ClassDef):
            # Class header (bases, keywords, decorators).
            return self._class_users(mod, classes, path) | executed
        names = _targets(stmt)
        if classes:
            users = self._class_users(mod, classes, path)
            if names:
                return users & self._names_mask(names) & executed if executed else users & self._names_mask(names)
            return users
        importers = ix.importers(path)
        if names:
            return importers & self._names_mask(names) & (executed or importers)
        return executed or importers

    def _names_mask(self, names: set[str]) -> int:
        m = 0
        for n in names:
            m |= self.ix.name_mask(n)
        return m
