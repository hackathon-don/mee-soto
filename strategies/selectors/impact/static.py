"""Build-time name index: for each identifier, the tests that executed code reading it.

Run during `docker build` (not charged):
  python -m strategies.selectors.impact.static <sqlglot root> <maps/index.pkl.gz> <out static.pkl.gz>

A "unit" is a function body, lambda or generator expression (counted with the
lines it ran at call time), or a module/class-level statement (counted with the
tests that ran it at import). Every Name, Attribute and identifier-like string
constant in a unit adds that unit's tests to the name's mask.
"""

from __future__ import annotations

import ast
import gzip
import os
import pickle
import sys

from .impact import Index

SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.GeneratorExp)


def _is_class_name(name: str) -> bool:
    return name[:1].isupper() and not name.isupper()


def _names_shallow(node, out: set[str], nested: list, isinst: set[str] | None = None):
    """Collect names in node, stopping at nested scopes (queued in `nested`).

    Class names used only as the type argument of isinstance/issubclass go to
    `isinst` instead: such a check reads nothing from the class.
    """
    stack = [node]
    while stack:
        n = stack.pop()
        if (
            isinst is not None
            and isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in ("isinstance", "issubclass")
            and len(n.args) == 2
        ):
            stack.append(n.args[0])
            types = n.args[1].elts if isinstance(n.args[1], ast.Tuple) else [n.args[1]]
            for ty in types:
                if isinstance(ty, ast.Attribute) and _is_class_name(ty.attr):
                    isinst.add(ty.attr)
                    stack.append(ty.value)
                elif isinstance(ty, ast.Name) and _is_class_name(ty.id):
                    isinst.add(ty.id)
                else:
                    stack.append(ty)
            continue
        if n is not node and isinstance(n, SCOPES):
            nested.append(n)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # decorators/defaults/annotations run in the enclosing unit
                stack.extend(n.decorator_list)
                stack.extend(d for d in n.args.defaults if d is not None)
                stack.extend(d for d in n.args.kw_defaults if d is not None)
            elif isinstance(n, ast.Lambda):
                stack.extend(d for d in n.args.defaults if d is not None)
            continue
        if n is not node and isinstance(n, ast.ClassDef):
            nested.append(n)
            stack.extend(n.bases)
            stack.extend(k.value for k in n.keywords)
            stack.extend(n.decorator_list)
            continue
        if isinstance(n, ast.Name):
            if not isinstance(n.ctx, ast.Store):
                out.add(n.id)
        elif isinstance(n, ast.Attribute):
            if not isinstance(n.ctx, ast.Store):
                out.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            out.add(n.value)
        elif isinstance(n, ast.keyword) and n.arg:
            out.add(n.arg)
        stack.extend(ast.iter_child_nodes(n))


_BLOCKS = ("body", "orelse", "finalbody")
_COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match) + (
    (ast.TryStar,) if hasattr(ast, "TryStar") else ()
)


def _stmt_pieces(stmts):
    """Yield (nodes, lo, hi): each statement's own expressions with the lines
    that run them. Compound statements yield their header, then their blocks."""
    for st in stmts:
        if isinstance(st, _COMPOUND):
            header = []
            for f, v in ast.iter_fields(st):
                if f in _BLOCKS or f in ("handlers", "cases"):
                    continue
                if isinstance(v, list):
                    header.extend(x for x in v if isinstance(x, ast.AST))
                elif isinstance(v, ast.AST):
                    header.append(v)
            first = None
            for f in ("body", "cases", "handlers"):
                v = getattr(st, f, None)
                if v:
                    first = v[0]
                    break
            hi = max(st.lineno, (first.lineno - 1) if first is not None else st.lineno)
            if header:
                yield header, st.lineno, hi
            for f in _BLOCKS:
                yield from _stmt_pieces(getattr(st, f, None) or [])
            for h in getattr(st, "handlers", None) or []:
                if h.type is not None:
                    yield [h.type], h.lineno, h.lineno
                yield from _stmt_pieces(h.body)
            for c in getattr(st, "cases", None) or []:
                parts = [c.pattern] + ([c.guard] if c.guard is not None else [])
                yield parts, c.pattern.lineno, (c.guard.end_lineno if c.guard is not None else c.pattern.end_lineno)
                yield from _stmt_pieces(c.body)
        else:
            yield [st], st.lineno, st.end_lineno or st.lineno


def build(root: str, ix: Index) -> tuple[dict[str, int], dict[str, int]]:
    names_run: dict[str, int] = {}
    names_def: dict[str, int] = {}
    names_isinst: dict[str, int] = {}
    class_counts: dict[str, int] = {}

    def add(found: set[str], mask: int, table=None):
        if not mask:
            return
        table = names_def if table is None else table
        for name in found:
            table[name] = table.get(name, 0) | mask

    for top in ("sqlglot", "tests"):
        for dirpath, _, filenames in os.walk(os.path.join(root, top)):
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root)
                try:
                    tree = ast.parse(open(full, encoding="utf-8").read())
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef):
                        class_counts[node.name] = class_counts.get(node.name, 0) + 1
                queue: list[tuple[ast.AST, str]] = [(tree, "def")]
                while queue:
                    node, kind = queue.pop()
                    if isinstance(node, (ast.Module, ast.ClassDef)):
                        for stmt in node.body:
                            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                                # header parts belong to the enclosing definition-level unit
                                found: set[str] = set()
                                nested: list = []
                                lo = min([stmt.lineno] + [d.lineno for d in stmt.decorator_list])
                                hdr: list = list(stmt.decorator_list)
                                if isinstance(stmt, ast.ClassDef):
                                    hdr += stmt.bases + [k.value for k in stmt.keywords]
                                    queue.append((stmt, "def"))
                                else:
                                    hdr += [d for d in stmt.args.defaults if d is not None]
                                    hdr += [d for d in stmt.args.kw_defaults if d is not None]
                                    queue.append((stmt, "run"))
                                for h in hdr:
                                    _names_shallow(h, found, nested)
                                add(found, ix.def_mask(rel, lo, stmt.lineno))
                                for n in nested:
                                    queue.append((n, "run" if not isinstance(n, ast.ClassDef) else "def"))
                                continue
                            found = set()
                            nested = []
                            _names_shallow(stmt, found, nested)
                            add(found, ix.def_mask(rel, stmt.lineno, stmt.end_lineno or stmt.lineno))
                            for n in nested:
                                queue.append((n, "run" if not isinstance(n, ast.ClassDef) else "def"))
                    else:
                        # function-like scope: names in its body, tests that ran it
                        # Statement by statement: a name is read only when the
                        # statement holding it runs.
                        nested = []
                        if isinstance(node, ast.GeneratorExp):
                            pieces = [([node.elt] + node.generators, node.lineno, node.end_lineno or node.lineno)]
                        elif isinstance(node, ast.Lambda):
                            pieces = [([node.body], node.lineno, node.end_lineno or node.lineno)]
                        else:
                            pieces = list(_stmt_pieces(node.body))
                        for parts, lo, hi in pieces:
                            found = set()
                            isinst: set[str] = set()
                            for b in parts:
                                _names_shallow(b, found, nested, isinst)
                            mask = ix.run_mask(rel, lo, hi)
                            add(found, mask, names_run)
                            add(isinst - found, mask, names_isinst)
                        for n in nested:
                            queue.append((n, "run" if not isinstance(n, ast.ClassDef) else "def"))
    return names_run, names_def, names_isinst, class_counts


def main():
    root, index_path, out = sys.argv[1:4]
    ix = Index(index_path)
    names_run, names_def, names_isinst, class_counts = build(root, ix)
    with gzip.open(out, "wb", compresslevel=6) as fh:
        pickle.dump(
            {
                "names_run": names_run,
                "names_def": names_def,
                "names_isinst": names_isinst,
                "class_counts": class_counts,
            },
            fh,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    print(f"names_run={len(names_run)} names_def={len(names_def)} size={os.path.getsize(out)}", file=sys.stderr)


if __name__ == "__main__":
    main()
