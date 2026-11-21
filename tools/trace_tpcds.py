"""Per-query coverage for tests/test_executor.py::TestExecutor::test_execute_tpcds (offline).

Run in the map image: python /tools/trace_tpcds.py /out/tpcds.json
Contexts: "setup" (imports, setUpClass, initializer) and one per query index.
Also records each query's CPU seconds with tracing off.
"""

import json
import os
import pickle
import sys
import time

SRC = "/src/"
sys.path.insert(0, "/src")
os.environ["SKIP_INTEGRATION"] = "0"
os.chdir("/src")

mon = sys.monitoring
TOOL = 4
ctx = {"name": "setup"}
data = {}  # ctx -> {"run": {f: set}, "def": {f: set}, "inits": set}
codes = {}
keep = []


def bucket():
    b = data.get(ctx["name"])
    if b is None:
        b = data[ctx["name"]] = {"run": {}, "def": {}, "inits": set()}
    return b


def on_line(code, line):
    info = codes.get(id(code))
    if info is None:
        fn = code.co_filename
        info = (fn[len(SRC):], bool(code.co_flags & 1)) if fn.startswith(SRC) else False
        codes[id(code)] = info
        keep.append(code)
    if info:
        b = bucket()
        b["run" if info[1] else "def"].setdefault(info[0], set()).add(line)
    return mon.DISABLE


def on_start(code, offset):
    if code.co_name != "__init__" or not code.co_filename.startswith(SRC) or not code.co_argcount:
        return mon.DISABLE
    try:
        obj = sys._getframe(1).f_locals.get(code.co_varnames[0])
    except Exception:
        return None
    if obj is not None:
        bucket()["inits"].add(type(obj))
    return None


mon.use_tool_id(TOOL, "tpcds")
mon.register_callback(TOOL, mon.events.LINE, on_line)
mon.register_callback(TOOL, mon.events.PY_START, on_start)
mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)

import tests.test_executor as te  # noqa: E402
from sqlglot import parse_one  # noqa: E402
from tests.helpers import TPCDS_SCHEMA  # noqa: E402

te.TestExecutor.setUpClass()
case = te.TestExecutor("test_execute_tpcds")
te.initializer(*pickle.loads(pickle.dumps((TPCDS_SCHEMA, case.tpcds_tables))))
sqls = case.tpcds_sqls


def run_query(i):
    args, sql, _ = sqls[i]
    expr = pickle.loads(pickle.dumps(parse_one(sql)))
    table = te.mp_execute(expr, args)
    table = pickle.loads(pickle.dumps(table))
    if table is not None:
        case.subtestHelper(i, table, tpch=False)


for i in range(len(sqls)):
    ctx["name"] = str(i)
    mon.restart_events()
    run_query(i)

mon.set_events(TOOL, 0)
# CPU per query without tracing (duckdb results are cached now, as in a second run;
# add the duckdb side separately below).
cpu = {}
case.cache.clear()
for i in range(len(sqls)):
    t0 = time.process_time()
    run_query(i)
    cpu[str(i)] = round(time.process_time() - t0, 4)


def ser(b):
    inits = set()
    for k in b["inits"]:
        for base in getattr(k, "__mro__", ()):
            mod = getattr(base, "__module__", "") or ""
            if mod.startswith(("sqlglot", "tests")):
                inits.add(f"{mod}:{base.__qualname__}")
    return {
        "run": {f: sorted(v) for f, v in b["run"].items()},
        "def": {f: sorted(v) for f, v in b["def"].items()},
        "inits": sorted(inits),
    }


json.dump(
    {"n_queries": len(sqls), "ctx": {k: ser(v) for k, v in data.items()}, "cpu": cpu},
    open(sys.argv[1], "w"),
)
print("queries", len(sqls), "total cpu", round(sum(cpu.values()), 1), file=sys.stderr)
