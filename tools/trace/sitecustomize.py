"""Per-process coverage recorder used only while building maps (never at run time).

Loaded via PYTHONPATH in every Python process of a map-building run, including
multiprocessing spawn workers and `python -c` subprocesses. When BR_TRACE_DIR is
set it records, for files under BR_SRC:
  run  - lines executed inside functions, lambdas and generator expressions
  def  - lines executed in module or class bodies (import / definition time)
  inits - sqlglot classes (with their sqlglot MRO) whose instances were created
  files - non-Python files under BR_SRC that were opened or listed
and dumps them as JSON at interpreter exit.
"""

import os
import sys

_OUT = os.environ.get("BR_TRACE_DIR")
_SRC = os.environ.get("BR_SRC", "/src/")

if _OUT and sys.version_info >= (3, 12):
    import atexit
    import json

    _mon = sys.monitoring
    _TOOL = 4
    _run = {}
    _def = {}
    _codes = {}
    _init_types = set()
    _files = set()
    _getframe = sys._getframe

    _keep = []

    def _line(code, line):
        # Keyed by id(): hashing a code object walks all nested code, which
        # made every first-hit line event O(module size).
        info = _codes.get(id(code))
        if info is None:
            fn = code.co_filename
            if fn.startswith(_SRC):
                info = (fn[len(_SRC):], bool(code.co_flags & 0x1))
            else:
                info = False
            _codes[id(code)] = info
            _keep.append(code)
        if info:
            (_run if info[1] else _def).setdefault(info[0], set()).add(line)
        return _mon.DISABLE

    def _start(code, offset):
        if code.co_name != "__init__" or not code.co_filename.startswith(_SRC):
            return _mon.DISABLE
        if not code.co_argcount:
            return _mon.DISABLE
        try:
            frame = _getframe(1)
            obj = frame.f_locals.get(code.co_varnames[0])
        except Exception:
            return None
        if obj is not None:
            _init_types.add(type(obj))
        return None

    def _audit(event, args):
        if event == "open":
            path = args[0]
        elif event in ("os.listdir", "os.scandir"):
            path = args[0]
        else:
            return
        if isinstance(path, bytes):
            try:
                path = path.decode()
            except Exception:
                return
        if not isinstance(path, str):
            return
        if not path.startswith("/"):
            try:
                path = os.path.abspath(path)
            except Exception:
                return
        if path.startswith(_SRC) and not path.endswith((".py", ".pyc")):
            _files.add(path[len(_SRC):])

    def _dump():
        try:
            _mon.set_events(_TOOL, 0)
        except Exception:
            pass
        inits = set()
        for klass in list(_init_types):
            for base in getattr(klass, "__mro__", ()):
                mod = getattr(base, "__module__", "") or ""
                if mod.startswith("sqlglot") or mod.startswith("tests"):
                    inits.add(f"{mod}:{base.__qualname__}")
        data = {
            "run": {k: sorted(v) for k, v in _run.items()},
            "def": {k: sorted(v) for k, v in _def.items()},
            "inits": sorted(inits),
            "files": sorted(_files),
            "argv": sys.argv[:3],
        }
        tag = os.environ.get("BR_TEST", "x")
        path = os.path.join(_OUT, f"{tag}.{os.getpid()}.json")
        try:
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    _mon.use_tool_id(_TOOL, "blast-radius")
    _mon.register_callback(_TOOL, _mon.events.LINE, _line)
    _mon.register_callback(_TOOL, _mon.events.PY_START, _start)
    _mon.set_events(_TOOL, _mon.events.LINE | _mon.events.PY_START)
    sys.addaudithook(_audit)
    atexit.register(_dump)
