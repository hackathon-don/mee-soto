"""ImpactSelector that narrows table-entry edits to the tests that read the entry.

SQLGlot keeps most dialect rules in dict literals assigned in class bodies.
ImpactSelector treats an edit to one entry as an edit to the whole statement,
so every test that touches the class and the table's name is a candidate. A
build-time trace (tools/trace_tables) records which entries each test read the
value of; when a patch only changes the value of an existing entry, tests that
never read that value cannot observe the change and are dropped.

Anything else falls back to ImpactSelector's answer: added, removed or renamed
entries, values that call something, tables the trace did not wrap, tests the
trace did not cover, and a missing or mismatched map.
"""

from __future__ import annotations

import ast
import gzip
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from . import CatalogTest
from .impact.impact import FUNC_NODES, SCOPE_NODES, Analyzer, Index, _Diff
from .impact.selector import DECISION_LOG, SG, ImpactSelector
from .map_format import SOURCE, catalog_records, catalog_sha256

if TYPE_CHECKING:
    from config import Config

MAP_PATH = Path(__file__).with_name("impact_tables.json.gz")
SCHEMA_VERSION = 1

# Evaluating one of these while the class body runs could do more than produce
# the entry's value, so an edit that involves one is not narrowed.
_IMPURE = (ast.Call, ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def table_target(stmt: ast.AST) -> str | None:
    """The name a `NAME = {...}` statement binds, as tools/trace_tables wraps it."""
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        name = stmt.targets[0].id
    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        name = stmt.target.id
    else:
        return None
    value = stmt.value
    return name if isinstance(value, ast.Dict) and value.keys else None


def _evaluated(node: ast.AST):
    """Nodes evaluated when `node` is; a lambda's body runs only when called."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, ast.Lambda):
            stack.extend(d for d in current.args.defaults if d is not None)
            stack.extend(d for d in current.args.kw_defaults if d is not None)
        else:
            stack.extend(ast.iter_child_nodes(current))


def changed_entry(path: str, d: _Diff) -> tuple[str, int, int] | None:
    """(table id, entry index, entry count) when `d` only changes one entry's value."""
    chain = [node for node, _ in d.path]
    fields = [field for _, field in d.path]
    at = next((i for i, node in enumerate(chain) if isinstance(node, (ast.Assign, ast.AnnAssign))), None)
    if at is None or at == 0 or fields[at] != "value" or table_target(chain[at]) is None:
        return None
    # The statement must sit directly in a module or class body, as the trace wraps it.
    if any(not isinstance(node, (ast.Module, ast.ClassDef)) or field != "body" for node, field in d.path[:at]):
        return None
    table = chain[at].value
    if len(chain) <= at + 1 or chain[at + 1] is not table or fields[at + 1] != "values":
        return None
    # A difference inside a function-like body is a run-time edit; the base rule is already exact.
    for node, field in d.path[at + 2 :]:
        if isinstance(node, SCOPE_NODES) and (not isinstance(node, FUNC_NODES + (ast.Lambda,)) or field == "body"):
            return None
    if len(chain) > at + 2:
        old_value = chain[at + 2]
    elif d.kind == "node" and len(d.old) == 1:
        old_value = d.old[0]
    else:
        return None  # entries added or removed
    index = next((i for i, value in enumerate(table.values) if value is old_value), None)
    if index is None or table.keys[index] is None:
        return None  # not an entry of this table, or a ** spread
    involved = [old_value, *(node for node in d.new if isinstance(node, ast.AST))]
    if any(isinstance(node, _IMPURE) for root in involved for node in _evaluated(root)):
        return None
    classes = [node.name for node in chain[:at] if isinstance(node, ast.ClassDef)]
    tid = f"{path}:{chain[at].lineno}:{'.'.join(classes + [table_target(chain[at])])}"
    return tid, index, len(table.keys)


class TableIndex:
    """Which tests read each table entry's value, as bitmasks over test numbers."""

    def __init__(self, tables: dict[str, tuple[int, int, dict[int, int]]], untraced: int) -> None:
        self.tables = tables
        self.untraced = untraced

    def readers(self, tid: str, index: int, count: int) -> int | None:
        """Tests that may observe a new value for the entry, or None when unknown."""
        table = self.tables.get(tid)
        if table is None or table[0] != count:
            return None
        return table[1] | table[2].get(index, 0) | self.untraced


def load_tables(catalog: Sequence[CatalogTest], path: Path = MAP_PATH) -> TableIndex | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            data = json.load(stream)
        if data["schema"] != SCHEMA_VERSION or data["source"] != SOURCE:
            return None
        if data["catalog_sha256"] != catalog_sha256(catalog_records(catalog)):
            return None
        sets = [int(mask, 16) for mask in data["sets"]]
        tables = {
            tid: (entry["n"], sets[entry["whole"]], {int(i): sets[s] for i, s in entry["reads"].items()})
            for tid, entry in data["tables"].items()
        }
        return TableIndex(tables, sets[data["untraced"]])
    except Exception:  # noqa: BLE001 - an unusable map means no narrowing
        return None


class TableKeyAnalyzer(Analyzer):
    def __init__(self, index: Index, base_root: str, tables: TableIndex) -> None:
        super().__init__(index, base_root)
        self.tables = tables

    def _diff_mask(self, path: str, mod: str, d: _Diff) -> int:
        base = super()._diff_mask(path, mod, d)
        if not base:
            return base
        try:
            entry = changed_entry(path, d)
            keep = self.tables.readers(*entry) if entry else None
        except Exception:  # noqa: BLE001 - never lose a commit to a narrowing bug
            keep = None
        if keep is None:
            return base
        # Pseudo-tests split one real test; they follow it.
        for pseudo in self.ix.pseudo.values():
            if keep >> pseudo["test"] & 1:
                keep |= 1 << pseudo["setup"]
                for bit in pseudo["queries"].values():
                    keep |= 1 << bit
        narrowed = base & keep
        if DECISION_LOG:
            self.decision_narrowed |= base & ~narrowed
        print(
            f"table entry {entry[0]}[{entry[1]}]: {base.bit_count()} -> {narrowed.bit_count()} candidates",
            file=sys.stderr,
            flush=True,
        )
        return narrowed


class TableKeySelector(ImpactSelector):
    def __init__(self, catalog: Sequence[CatalogTest], config: Config) -> None:
        super().__init__(catalog, config)
        if self.ctx is None:
            return
        tables = load_tables(catalog)
        if tables is None:
            print("table map unusable: selecting as ImpactSelector", file=sys.stderr, flush=True)
            return
        self.ctx.an = TableKeyAnalyzer(self.ctx.ix, SG, tables)
