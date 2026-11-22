"""Per-process key and setting recorder used only while building maps (never at run time).

Extends tools/trace_tables with what that trace leaves out, so a patch that adds,
removes or renames a table key, or changes a scalar class setting, can be narrowed:

* Tables (dict literals assigned directly in a module or class body) also record
  which existing keys were probed by membership (`in`), which keys were looked up
  and missed (by `[]`, `get` or `in`), and whether the key set itself was observed
  (iteration, `keys()`, `len()`, truthiness). Missed keys are recorded canonically
  (str and number keys); any other missed key marks the table as missed opaquely.
* Class settings: every `obj.NAME` load, `getattr(obj, name)` and `vars(cls)` /
  `cls.__dict__` in sqlglot code, where NAME is assigned directly in some sqlglot
  class body, records the class in the lookup's MRO that defines NAME.

Reads made by code outside sqlglot (stdlib or C) are not seen, as in trace_tables.

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

    _CACHE = os.environ.get("BR_TABLE_CACHE", "/tmp/br-key-cache")

    class _Rec:
        """Reads of one table. Values are recorded by identity: a lookup that
        misses reads no value, so only hits count, and the set stays as small
        as the table however many keys are probed."""

        __slots__ = ("vals", "whole", "index", "n", "keys", "hits", "shape", "miss", "opaque")

        def __init__(self, n=0):
            self.vals = set()  # id() of every value handed out
            self.whole = False
            self.index = {}  # id(value) -> literal entries holding that object
            self.n = n
            self.keys = {}  # literal key -> literal entries with that key
            self.hits = set()  # literal entries found by a membership test
            self.shape = False  # the key set was observed
            self.miss = set()  # canonical keys looked up and not found
            self.opaque = False  # a non-canonical key was looked up and not found

        def missed(self, key):
            canon = _canon(key)
            if canon is None:
                self.opaque = True
            else:
                self.miss.add(canon)

    def _canon(key):
        """A stable spelling of a str or number key, matching the selector's."""
        if isinstance(key, str):
            return "s:" + str.__str__(key)
        if isinstance(key, (int, float)):
            try:
                return "n:" + repr(hash(key))
            except Exception:
                return None
        return None

    class _Sink:
        def add(self, value_id):
            pass

    class _NullRec(_Rec):
        __slots__ = ()

        def missed(self, key):
            pass

    _NULL = _NullRec()  # recorder of copies nobody tracks
    _NULL.vals = _NULL.hits = _NULL.miss = _Sink()
    _MISSING = object()
    _TABLES = {}  # table id -> [_Rec]
    _SPREAD = object()

    class TracedTable(dict):
        __slots__ = ("_br",)

        def __init__(self, *args, **kwargs):
            dict.__init__(self, *args, **kwargs)
            self._br = _NULL

        def __getitem__(self, key):
            value = dict.get(self, key, _MISSING)
            if value is _MISSING:
                self._br.missed(key)
                return dict.__getitem__(self, key)
            self._br.vals.add(id(value))
            return value

        def get(self, key, default=None):
            value = dict.get(self, key, _MISSING)
            if value is _MISSING:
                self._br.missed(key)
                return default
            self._br.vals.add(id(value))
            return value

        def __contains__(self, key):
            found = dict.__contains__(self, key)
            rec = self._br
            if found:
                if rec is not _NULL:
                    rec.hits.update(rec.keys.get(key, ()))
            else:
                rec.missed(key)
            return found

        # Overriding __iter__ sends dict(d), {**d} and other.update(d) down
        # CPython's generic path, which fetches values through __getitem__.
        def __iter__(self):
            self._br.shape = True
            return dict.__iter__(self)

        def keys(self):
            self._br.shape = True
            return dict.keys(self)

        def __len__(self):
            self._br.shape = True
            return dict.__len__(self)

        def __reversed__(self):
            self._br.shape = True
            return dict.__reversed__(self)

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
                try:
                    rec.keys.setdefault(key, []).append(i // 2)
                except TypeError:
                    rec.shape = True  # unhashable keys cannot be, but stay safe
        out._br = rec
        _TABLES.setdefault(tid, []).append(rec)
        return out

    builtins.__br_table__ = _table
    builtins.__br_spread__ = _SPREAD

    _ATTRS = set()  # (defining class, setting name); name "*" = the class dict as a whole
    _OWNER = {}  # (looked-up type, name) -> defining class or None
    _getattr = getattr
    _vars = vars

    def _owner(mro, name):
        for klass in mro:
            if name in type.__getattribute__(klass, "__dict__"):
                return klass
        return None

    class _ClassDict:
        """A class's __dict__ that records which names are looked up in it."""

        __slots__ = ("_klass", "_raw")

        def __init__(self, klass):
            self._klass = klass
            self._raw = type.__getattribute__(klass, "__dict__")

        def _key(self, key):
            if isinstance(key, str):
                _ATTRS.add((self._klass, key))
            else:
                _ATTRS.add((self._klass, "*"))

        def get(self, key, default=None):
            self._key(key)
            return self._raw.get(key, default)

        def __getitem__(self, key):
            self._key(key)
            return self._raw[key]

        def __contains__(self, key):
            self._key(key)
            return key in self._raw

        def __getattr__(self, name):
            _ATTRS.add((self._klass, "*"))
            return getattr(self._raw, name)

        def __iter__(self):
            _ATTRS.add((self._klass, "*"))
            return iter(self._raw)

        def __len__(self):
            _ATTRS.add((self._klass, "*"))
            return len(self._raw)

    def _note(obj, name):
        try:
            if name == "__dict__":
                return
            if isinstance(obj, super):
                start = obj.__self_class__
                if start is not None:
                    klass = _owner(start.__mro__, name)
                    if klass is not None:
                        _ATTRS.add((klass, name))
                return
            key = (obj if isinstance(obj, type) else type(obj), name)
            klass = _OWNER.get(key, _MISSING)
            if klass is _MISSING:
                if isinstance(obj, type):
                    klass = _owner(obj.__mro__, name) or _owner(type(obj).__mro__, name)
                else:
                    klass = _owner(key[0].__mro__, name)
                _OWNER[key] = klass
            if klass is not None:
                _ATTRS.add((klass, name))
        except Exception:
            pass

    def _attr(obj, name):
        if name == "__dict__" and isinstance(obj, type):
            return _ClassDict(obj)
        _note(obj, name)
        return _getattr(obj, name)

    def _getattr_traced(obj, name, *default):
        if isinstance(name, str):
            _note(obj, name)
        return _getattr(obj, name, *default)

    def _vars_traced(*args):
        if args and isinstance(args[0], type):
            _ATTRS.add((args[0], "*"))
        return _vars(*args)

    def _attr_store(obj, name):
        _note(obj, name)  # `obj.NAME += x` reads before it writes
        return obj

    builtins.__br_attr__ = _attr
    builtins.__br_getattr__ = _getattr_traced
    builtins.__br_vars__ = _vars_traced
    builtins.__br_attr_store__ = _attr_store

    def _setting_names():
        """Names bound by a plain (non-dict) assignment directly in some sqlglot class body."""
        cached = os.path.join(_CACHE, "settings.json")
        try:
            with open(cached) as fh:
                return frozenset(json.load(fh))
        except (OSError, ValueError):
            pass
        names = set()

        def body(node):
            for stmt in node.body:
                if isinstance(stmt, ast.ClassDef):
                    body(stmt)
                    for inner in stmt.body:
                        target = _target(inner)
                        if target and not isinstance(inner.value, ast.Dict):
                            names.add(target)

        for root, _, files in os.walk(os.path.join(_SRC, "sqlglot")):
            for name in files:
                if name.endswith(".py"):
                    try:
                        with open(os.path.join(root, name), "rb") as fh:
                            body(ast.parse(fh.read()))
                    except (OSError, SyntaxError, ValueError):
                        pass
        names = sorted(n for n in names if not (n.startswith("__") and n.endswith("__")))
        try:
            os.makedirs(_CACHE, exist_ok=True)
            tmp = f"{cached}.{os.getpid()}"
            with open(tmp, "w") as fh:
                json.dump(names, fh)
            os.replace(tmp, cached)
        except OSError:
            pass
        return frozenset(names)

    _SETTINGS = None

    class _Attrs(ast.NodeTransformer):
        def __init__(self, names):
            self.names = names

        def visit_Attribute(self, node):
            self.generic_visit(node)
            if isinstance(node.ctx, ast.Load) and (node.attr in self.names or node.attr == "__dict__"):
                call = ast.Call(ast.Name("__br_attr__", ast.Load()), [node.value, ast.Constant(node.attr)], [])
                return ast.copy_location(call, node)
            return node

        def visit_AugAssign(self, node):
            self.generic_visit(node)
            target = node.target
            if isinstance(target, ast.Attribute) and target.attr in self.names:
                target.value = ast.Call(
                    ast.Name("__br_attr_store__", ast.Load()), [target.value, ast.Constant(target.attr)], []
                )
            return node

        def visit_Call(self, node):
            self.generic_visit(node)
            if isinstance(node.func, ast.Name) and node.func.id in ("getattr", "vars"):
                node.func = ast.copy_location(ast.Name(f"__br_{node.func.id}__", ast.Load()), node.func)
            return node

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
            global _SETTINGS
            if _SETTINGS is None:
                _SETTINGS = _setting_names()
            tree = _rewrite(ast.parse(data, path), path[len(_SRC):])
            tree = ast.fix_missing_locations(_Attrs(_SETTINGS).visit(tree))
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
            reads, hits, entries, miss = set(), set(), set(), set()
            shape = opaque = False
            for rec in recs:
                whole = whole or rec.whole
                shape = shape or rec.shape
                opaque = opaque or rec.opaque
                hits.update(rec.hits)
                miss.update(rec.miss)
                for value_id, indexes in rec.index.items():
                    entries.update(indexes)
                    if value_id in rec.vals:
                        reads.update(indexes)
            if whole:
                tables[tid] = {"n": n, "w": 1}
            elif reads or hits or shape or opaque or miss:
                tables[tid] = {
                    "n": n,
                    "r": sorted(reads),
                    "h": sorted(hits),
                    "s": int(shape),
                    "o": int(opaque),
                    "k": sorted(miss),
                }
        attrs = sorted(
            {
                f"{klass.__module__}:{klass.__qualname__}.{name}"
                for klass, name in _ATTRS
                if isinstance(getattr(klass, "__module__", None), str) and klass.__module__.split(".")[0] == "sqlglot"
            }
        )
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
                json.dump({"defs": defs_id, "tables": tables, "attrs": attrs, "argv": sys.argv[:3]}, fh, separators=(",", ":"))
        except Exception:
            pass

    atexit.register(_dump)
