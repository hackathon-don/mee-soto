"""First-failure table selector that also narrows key edits and class settings.

TableKeySelector narrows one kind of edit, a new value for an existing entry of a
dict literal in a module or class body. This selector adds two more, using a map
recorded at build time by tools/trace_keys (see tools/build_key_index.py):

* Entries added to, removed from or rekeyed in such a table. A removed or rekeyed
  entry is observable by tests that read its value or found its key with `in`; an
  added key by tests whose lookup of that key missed. Tests that observed the key
  set itself (iteration, `keys()`, `len()`) or the whole table see every key edit.
  A new key equal to a kept key also keeps that entry's readers, since one value
  replaces the other. A new key that is not a str or number literal falls back.
* A new value for a scalar setting `NAME = <expr>` directly in a class body: kept
  are the tests that looked NAME up on a class whose MRO found it there, read that
  class's `__dict__` as a whole, or spell NAME in their test file.

The map loads only when a patch makes one of these edits. Anything else, an edit
that evaluates a call, a setting the class body itself reads at definition time, a
table or setting the map lacks, and a missing or mismatched map, keeps the
first-failure table selector's answer.
"""

from __future__ import annotations

import ast
import gzip
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .impact.impact import FUNC_NODES, SCOPE_NODES, Analyzer, _Diff
from .impact.selector import SG
from .impact_first_failure_tables_free_cpu import FirstFailureTablesFreeCpuImpact
from .impact_tables import _IMPURE, TableKeyAnalyzer, _evaluated, changed_entry, table_target
from .map_format import SOURCE, catalog_records, catalog_sha256

MAP_PATH = Path(__file__).with_name("impact_keys.json.gz")
SCHEMA_VERSION = 1
KEY_SOURCE = {**SOURCE, "collector": "key-read-v1"}


def canonical(node: ast.AST | None) -> str | None:
    """The tracer's spelling of a literal key, or None when it has none."""
    if not isinstance(node, ast.Constant):
        return None
    value = node.value
    if isinstance(value, str):
        return "s:" + value
    if isinstance(value, (bool, int, float)):
        return "n:" + repr(hash(value))
    return None


def _impure(nodes) -> bool:
    return any(isinstance(n, _IMPURE) for root in nodes if isinstance(root, ast.AST) for n in _evaluated(root))


def _statement(d: _Diff):
    """(index of the assignment in the path, its class names) when it sits directly in a module or class body."""
    chain = [node for node, _ in d.path]
    at = next((i for i, node in enumerate(chain) if isinstance(node, (ast.Assign, ast.AnnAssign))), None)
    if at is None or at == 0 or d.path[at][1] != "value":
        return None
    if any(not isinstance(node, (ast.Module, ast.ClassDef)) or field != "body" for node, field in d.path[:at]):
        return None
    for node, field in d.path[at + 1 :]:
        if isinstance(node, SCOPE_NODES) and (not isinstance(node, FUNC_NODES + (ast.Lambda,)) or field == "body"):
            return None
    return at, [node.name for node in chain[:at] if isinstance(node, ast.ClassDef)]


def key_edit(path: str, d: _Diff) -> tuple[str, list[int], list[str]] | None:
    """(table id, old entries removed or rekeyed, canonical new keys) for a key edit."""
    found = _statement(d)
    if found is None:
        return None
    at, classes = found
    chain = [node for node, _ in d.path]
    stmt = chain[at]
    name = table_target(stmt)
    table = stmt.value
    if name is None or len(chain) <= at + 1 or chain[at + 1] is not table:
        return None
    field = d.path[at + 1][1]
    tid = f"{path}:{stmt.lineno}:{'.'.join(classes + [name])}"
    if None in table.keys:
        return None  # a ** spread shifts what the trace saw
    if _impure([*d.old, *d.new]) or (len(chain) > at + 2 and _impure([chain[at + 2]])):
        return None
    if field == "keys":
        if d.kind == "list" and len(chain) == at + 2:
            old_keys, new_keys = list(d.old), list(d.new)
            if any(not isinstance(k, ast.AST) for k in old_keys + new_keys):
                return None
            new = [canonical(k) for k in new_keys]
        else:
            old_keys = [chain[at + 2]] if len(chain) > at + 2 else list(d.old)
            if len(old_keys) != 1:
                return None
            leaf = d.path[-1]
            if len(chain) == at + 2 and d.kind == "node" and isinstance(d.new[0], ast.AST):
                new = [canonical(d.new[0])]
            elif (
                len(chain) == at + 3
                and isinstance(leaf[0], ast.Constant)
                and leaf[1] == "value"
                and len(d.new) == 1
            ):
                new = [canonical(ast.Constant(d.new[0]))]
            else:
                new = [None]
        removed = [next((i for i, k in enumerate(table.keys) if k is old), None) for old in old_keys]
        if None in removed or None in new:
            return None
        # A new key equal to a kept key replaces or shadows that entry's value.
        removed += [i for i, k in enumerate(table.keys) if i not in removed and canonical(k) in new]
        return tid, removed, new
    if field == "values" and d.kind == "list" and len(chain) == at + 2:
        # Entry counts changed, so the keys list has its own difference naming the keys.
        removed = [next((i for i, v in enumerate(table.values) if v is old), None) for old in d.old]
        if None in removed:
            return None
        return tid, removed, []
    return None


def setting_edit(path: str, mod: str, d: _Diff) -> str | None:
    """The map key of a scalar class setting whose value `d` changes."""
    found = _statement(d)
    if found is None:
        return None
    at, classes = found
    chain = [node for node, _ in d.path]
    stmt = chain[at]
    if not classes or not isinstance(chain[at - 1], ast.ClassDef):
        return None
    if isinstance(stmt, ast.Assign):
        if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
            return None
        name = stmt.targets[0].id
    elif isinstance(stmt.target, ast.Name) and stmt.value is not None:
        name = stmt.target.id
    else:
        return None
    if isinstance(stmt.value, ast.Dict) or name.startswith("__"):
        return None
    if _impure([stmt.value, *d.new]):
        return None
    # The class body (outside functions) reading the name runs at definition time.
    pending = [s for s in chain[at - 1].body if s is not stmt]
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                pending.extend(node.decorator_list)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                pending.extend(x for x in node.args.defaults + node.args.kw_defaults if x is not None)
            if isinstance(node, ast.ClassDef):
                pending.extend(node.bases + [k.value for k in node.keywords])
            continue
        if isinstance(node, ast.Name) and node.id == name:
            return None
        pending.extend(ast.iter_child_nodes(node))
    return f"{mod}:{'.'.join(classes)}.{name}"


class KeyIndex:
    """The key-read map, loaded on first use; None from every query when unusable."""

    def __init__(self, catalog, path: Path = MAP_PATH) -> None:
        self.catalog = catalog
        self.path = path
        self.data = None
        self.loaded = False
        self.masks: dict[int, int] = {}

    def _load(self):
        if not self.loaded:
            self.loaded = True
            try:
                with gzip.open(self.path, "rt", encoding="utf-8") as stream:
                    data = json.load(stream)
                if data["schema"] == SCHEMA_VERSION and data["source"] == KEY_SOURCE:
                    if data["catalog_sha256"] == catalog_sha256(catalog_records(self.catalog)):
                        self.data = data
            except Exception:  # noqa: BLE001 - an unusable map means no narrowing
                self.data = None
            if self.data is None:
                print("key map unusable: not narrowing key edits", file=sys.stderr, flush=True)
        return self.data

    def _mask(self, set_id: int) -> int:
        mask = self.masks.get(set_id)
        if mask is None:
            mask = self.masks[set_id] = int(self.data["sets"][set_id], 16)
        return mask

    def key_readers(self, tid: str, count: int, removed: list[int], new: list[str]) -> int | None:
        data = self._load()
        table = data and data["tables"].get(tid)
        if not table or table["n"] != count:
            return None
        m = self._mask
        keep = m(data["untraced"]) | m(table["whole"]) | m(table["shape"])
        for index in removed:
            for kind in ("reads", "hits"):
                set_id = table[kind].get(str(index))
                if set_id is not None:
                    keep |= m(set_id)
        for key in new:
            keep |= m(table["opaque"])
            set_id = table["miss"].get(key)
            if set_id is not None:
                keep |= m(set_id)
        return keep

    def setting_readers(self, key: str) -> int | None:
        data = self._load()
        set_id = data and data["settings"].get(key)
        if set_id is None:
            return None
        return self._mask(data["untraced"]) | self._mask(set_id)


class KeyReadAnalyzer(TableKeyAnalyzer):
    def __init__(self, index, base_root, tables, keys: KeyIndex) -> None:
        super().__init__(index, base_root, tables)
        self.keys = keys

    def _diff_mask(self, path: str, mod: str, d: _Diff) -> int:
        result = super()._diff_mask(path, mod, d)
        if not result:
            return result
        try:
            if changed_entry(path, d) is not None:
                return result
            what, keep = None, None
            edit = key_edit(path, d)
            if edit is not None:
                tid, removed, new = edit
                count = len(next(node for node, _ in d.path if isinstance(node, (ast.Assign, ast.AnnAssign))).value.keys)
                keep = self.keys.key_readers(tid, count, removed, new)
                what = f"table keys {tid} -{removed} +{new}"
            else:
                key = setting_edit(path, mod, d)
                if key is not None:
                    keep = self.keys.setting_readers(key)
                    what = f"setting {key}"
        except Exception:  # noqa: BLE001 - never lose a commit to a narrowing bug
            keep = None
        if keep is None:
            return result
        for pseudo in self.ix.pseudo.values():
            if keep >> pseudo["test"] & 1:
                keep |= 1 << pseudo["setup"]
                for bit in pseudo["queries"].values():
                    keep |= 1 << bit
        narrowed = result & keep
        print(f"{what}: {result.bit_count()} -> {narrowed.bit_count()} candidates", file=sys.stderr, flush=True)
        return narrowed


class KeyReadImpact(FirstFailureTablesFreeCpuImpact):
    """First-failure table selector plus key-edit and class-setting narrowing."""

    def __init__(self, catalog: Sequence, config) -> None:
        super().__init__(catalog, config)
        an = getattr(self.ctx, "an", None)
        if isinstance(an, TableKeyAnalyzer):
            self.ctx.an = KeyReadAnalyzer(self.ctx.ix, SG, an.tables, KeyIndex(catalog))
