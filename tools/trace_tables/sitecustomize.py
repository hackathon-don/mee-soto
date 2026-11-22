"""Per-process table-read recorder used only while building maps (never at run time).

Loaded via PYTHONPATH in every Python process of a map-building run, including
multiprocessing spawn workers and `python -c` subprocesses. When BR_TRACE_DIR is
set it rewrites each sqlglot module as it is imported so that every dict literal
assigned to a name directly in a module or class body becomes a `TracedTable`,
then records per table which literal entries had their *value* read.

Only value reads matter: the map answers "which tests can observe a changed value
of an existing entry". Membership tests, `len()` and key iteration never see a
value, so they are left alone. Anything that reads every value at once (`items()`,
`copy()`, comparison, pickling, mutation) marks the whole table as read.

Dumps <BR_TRACE_DIR>/<BR_TEST>.<pid>.json at interpreter exit.
"""

import os
import sys

_OUT = os.environ.get("BR_TRACE_DIR")
_SRC = os.environ.get("BR_SRC", "/src/")

if _OUT and sys.version_info >= (3, 12):
    import ast
    import atexit
    import builtins
    import hashlib
    import importlib.abc
    import importlib.machinery
    import json
    import marshal

    _CACHE = os.environ.get("BR_TABLE_CACHE", "/tmp/br-table-cache")

    class _Rec:
        """Reads of one table. Values are recorded by identity: a lookup that
        misses reads no value, so only hits count, and the set stays as small
        as the table however many keys are probed."""

        __slots__ = ("vals", "whole", "index", "n")

        def __init__(self, n=0):
            self.vals = set()  # id() of every value handed out
            self.whole = False
            self.index = {}  # id(value) -> literal entries holding that object
            self.n = n

    class _Sink:
        def add(self, value_id):
            pass

    _NULL = _Rec()  # recorder of copies nobody tracks
    _NULL.vals = _Sink()
    _MISSING = object()
    _TABLES = {}  # table id -> [_Rec]
    _SPREAD = object()

    class TracedTable(dict):
        __slots__ = ("_br",)

        def __init__(self, *args, **kwargs):
            dict.__init__(self, *args, **kwargs)
            self._br = _NULL

        def __getitem__(self, key):
            value = dict.__getitem__(self, key)
            self._br.vals.add(id(value))
            return value

        def get(self, key, default=None):
            value = dict.get(self, key, _MISSING)
            if value is _MISSING:
                return default
            self._br.vals.add(id(value))
            return value

        # Overriding __iter__ sends dict(d), {**d} and other.update(d) down
        # CPython's generic path, which fetches values through __getitem__.
        def __iter__(self):
            return dict.__iter__(self)

        def items(self):
            self._br.whole = True
            return dict.items(self)

        def values(self):
            self._br.whole = True
            return dict.values(self)

        def copy(self):
            self._br.whole = True
            return dict(dict.items(self))

        def __eq__(self, other):
            self._br.whole = True
            return dict.__eq__(self, other)

        def __ne__(self, other):
            self._br.whole = True
            return dict.__ne__(self, other)

        __hash__ = None

        def __repr__(self):
            self._br.whole = True
            return dict.__repr__(self)

        def __or__(self, other):
            self._br.whole = True
            return dict.__or__(self, other)

        def __ror__(self, other):
            self._br.whole = True
            return dict.__ror__(self, other)

        def __ior__(self, other):
            self._br.whole = True
            return dict.__ior__(self, other)

        def __reduce_ex__(self, protocol):
            self._br.whole = True
            return (dict, (dict(dict.items(self)),))

        def __setitem__(self, key, value):
            self._br.whole = True
            dict.__setitem__(self, key, value)

        def __delitem__(self, key):
            self._br.whole = True
            dict.__delitem__(self, key)

        def pop(self, *args):
            self._br.whole = True
            return dict.pop(self, *args)

        def popitem(self):
            self._br.whole = True
            return dict.popitem(self)

        def setdefault(self, key, default=None):
            self._br.whole = True
            return dict.setdefault(self, key, default)

        def update(self, *args, **kwargs):
            self._br.whole = True
            dict.update(self, *args, **kwargs)

        def clear(self):
            self._br.whole = True
            dict.clear(self)

    def _table(tid, flat):
        """Build the table from (key, value, key, value, ...) in source order."""
        rec = _Rec(len(flat) // 2)
        out = TracedTable()
        for i in range(0, len(flat), 2):
            key, value = flat[i], flat[i + 1]
            if key is _SPREAD:
                dict.update(out, value)  # a traced parent records these reads itself
            else:
                dict.__setitem__(out, key, value)
                rec.index.setdefault(id(value), []).append(i // 2)
        out._br = rec
        _TABLES.setdefault(tid, []).append(rec)
        return out

    builtins.__br_table__ = _table
    builtins.__br_spread__ = _SPREAD

    def _target(stmt):
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            return stmt.targets[0].id
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            return stmt.target.id
        return None

    def _rewrite(tree, rel):
        """Wrap `NAME = {...}` statements that sit directly in a module or class body."""

        def body(node, stack):
            for stmt in node.body:
                if isinstance(stmt, ast.ClassDef):
                    body(stmt, stack + [stmt.name])
                    continue
                name = _target(stmt)
                value = getattr(stmt, "value", None)
                if not name or not isinstance(value, ast.Dict) or not value.keys:
                    continue
                flat = []
                for key, val in zip(value.keys, value.values):
                    flat.append(ast.Name("__br_spread__", ast.Load()) if key is None else key)
                    flat.append(val)
                tid = f"{rel}:{stmt.lineno}:{'.'.join(stack + [name])}"
                call = ast.Call(
                    ast.Name("__br_table__", ast.Load()),
                    [ast.Constant(tid), ast.Tuple(flat, ast.Load())],
                    [],
                )
                stmt.value = ast.copy_location(call, value)

        body(tree, [])
        ast.fix_missing_locations(tree)
        return tree

    class _Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            path = self.get_filename(fullname)
            data = self.get_data(path)
            digest = hashlib.sha1(path.encode() + b"\0" + data).hexdigest()
            cached = os.path.join(_CACHE, digest)
            try:
                with open(cached, "rb") as fh:
                    return marshal.loads(fh.read())
            except (OSError, EOFError, ValueError, TypeError):
                pass
            tree = _rewrite(ast.parse(data, path), path[len(_SRC):])
            code = compile(tree, path, "exec", dont_inherit=True)
            try:
                os.makedirs(_CACHE, exist_ok=True)
                tmp = f"{cached}.{os.getpid()}"
                with open(tmp, "wb") as fh:
                    fh.write(marshal.dumps(code))
                os.replace(tmp, cached)
            except OSError:
                pass
            return code

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name != "sqlglot" and not name.startswith("sqlglot."):
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path, target)
            if (
                spec is not None
                and type(spec.loader) is importlib.machinery.SourceFileLoader
                and spec.origin
                and spec.origin.startswith(_SRC)
            ):
                spec.loader = _Loader(spec.loader.name, spec.loader.path)
            return spec

    sys.meta_path.insert(0, _Finder())

    def _dump():
        tables = {}
        wrapped = []
        for tid, recs in _TABLES.items():
            n = recs[0].n
            wrapped.append([tid, n])
            whole = any(r.n != n for r in recs)
            reads = set()
            entries = set()
            for rec in recs:
                whole = whole or rec.whole
                for value_id, indexes in rec.index.items():
                    entries.update(indexes)
                    if value_id in rec.vals:
                        reads.update(indexes)
            if whole or (reads and reads == entries):
                tables[tid] = [n, 1, []]
            elif reads:
                tables[tid] = [n, 0, sorted(reads)]
        wrapped.sort()
        defs = json.dumps(wrapped, separators=(",", ":"))
        defs_id = hashlib.sha1(defs.encode()).hexdigest()
        tag = os.environ.get("BR_TEST", "x")
        try:
            os.makedirs(_OUT, exist_ok=True)
            defs_path = os.path.join(_OUT, f"defs.{defs_id}.json")
            if not os.path.exists(defs_path):
                tmp = f"{defs_path}.{os.getpid()}"
                with open(tmp, "w") as fh:
                    fh.write(defs)
                os.replace(tmp, defs_path)
            with open(os.path.join(_OUT, f"{tag}.{os.getpid()}.json"), "w") as fh:
                json.dump({"defs": defs_id, "tables": tables, "argv": sys.argv[:3]}, fh, separators=(",", ":"))
        except Exception:
            pass

    atexit.register(_dump)
