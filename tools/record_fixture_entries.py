"""Record which fixture entries each fixture-driven test reaches (pinned SQLGlot).

Run inside the runtime image (SQLGlot at /opt/sg); see "Fixture entry map" in WORKSPACE.md.
For every catalog test in a module that uses tests.helpers.load_sql_fixture(s|_pairs),
the test module is imported once and its class fixtures run once in a parent
process. Each test then runs in forked children: one child with every fixture
entry filtered out (the test's own setup and code outside the loop), and one
child per fixture entry with only that entry kept. sys.monitoring PY_START
events give the code objects each run entered. Nothing here reads a patch or
a label.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import select
import signal
import sys
import unittest

SG = "/opt/sg"
FORMAT = 1
TOOL = 4
LOADERS = ("load_sql_fixture_pairs", "load_sql_fixtures")

_seen: set = set()
_keys: dict = {}


def _on_start(code, offset):
    _seen.add(code)
    return sys.monitoring.DISABLE


def _key(code):
    k = _keys.get(code)
    if k is None:
        path = code.co_filename
        if not path.startswith(SG + "/"):
            k = False
        else:
            rel = path[len(SG) + 1 :]
            if not (rel.startswith("sqlglot/") or rel.startswith("tests/")):
                k = False
            else:
                # The lines this code object itself runs, as ranges; a nested
                # function's body belongs to the nested code object.
                lines = sorted({line for _, _, line in code.co_lines() if line is not None} | {code.co_firstlineno})
                ranges = []
                for line in lines:
                    if ranges and ranges[-1][1] + 1 == line:
                        ranges[-1][1] = line
                    else:
                        ranges.append([line, line])
                k = (rel, tuple(tuple(r) for r in ranges))
        _keys[code] = k
    return k


def _norm(k):
    return (k[0], tuple(tuple(r) for r in k[1]))


def _drain() -> set:
    seen = list(_seen)
    _seen.clear()
    out = {k for k in map(_key, seen) if k}
    return out


def sha256_file(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class Filter:
    """Loader wrapper: keep is None (all entries) or a set of (file, index)."""

    def __init__(self):
        self.keep = None
        self.calls: dict[str, int] = {}

    def install(self, module, helpers):
        for name in LOADERS:
            orig = getattr(helpers, name)

            def wrapped(filename, _orig=orig):
                items = list(_orig(filename))
                self.calls[filename] = len(items)
                if self.keep is None:
                    return iter(items)
                return iter([item for i, item in enumerate(items) if (filename, i) in self.keep])

            setattr(helpers, name, wrapped)
            if getattr(module, name, None) is orig:
                setattr(module, name, wrapped)


def run_child(case_cls, meth, flt, keep, exclude, timeout):
    """Fork; run class setup (unrecorded, it is charged to the whole test) then the test."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        code = 1
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            signal.alarm(int(timeout))
            flt.keep = keep
            sys.monitoring.restart_events()
            sys.monitoring.set_events(TOOL, sys.monitoring.events.PY_START)
            case_cls.setUpClass()
            class_codes = _drain()
            class_calls, flt.calls = flt.calls, {}
            result = unittest.TestResult()
            if meth is not None:
                case_cls(meth).run(result)
            sys.monitoring.set_events(TOOL, 0)
            ok = result.wasSuccessful() and not result.skipped
            codes = sorted((class_codes if meth is None else _drain()) - exclude)
            calls = class_calls if meth is None else flt.calls
            payload = json.dumps({"ok": ok or meth is None, "calls": calls, "codes": codes}).encode()
            with os.fdopen(w, "wb") as out:
                out.write(payload)
            code = 0
        finally:
            os._exit(code)
    os.close(w)
    return pid, r


def pool(jobs, tasks, on_done):
    """tasks: iterator of (tag, start) where start() -> (pid, fd)."""
    active = {}
    tasks = iter(tasks)
    exhausted = False
    while active or not exhausted:
        while not exhausted and len(active) < jobs:
            try:
                tag, start = next(tasks)
            except StopIteration:
                exhausted = True
                break
            pid, fd = start()
            active[fd] = (pid, tag, [])
        if not active:
            break
        ready, _, _ = select.select(list(active), [], [])
        for fd in ready:
            pid, tag, chunks = active[fd]
            data = os.read(fd, 1 << 20)
            if data:
                chunks.append(data)
                continue
            os.close(fd)
            _, status = os.waitpid(pid, 0)
            del active[fd]
            ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
            on_done(tag, json.loads(b"".join(chunks)) if ok and chunks else None)


def record_module(mod_name, tests, jobs, timeout):
    """tests: [(number, cls, meth)] -> (import codes, {number: record})."""
    os.chdir(SG)
    sys.path[:0] = [SG]
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.dont_write_bytecode = True
    sys.monitoring.use_tool_id(TOOL, "fixture-entries")
    sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_START, _on_start)
    sys.monitoring.set_events(TOOL, sys.monitoring.events.PY_START)
    import concurrent.futures

    from strategies.selectors.impact.runner import InlineExecutor

    # As in the verification child: the tests' process pools run inline.
    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    import tests.helpers as helpers

    module = __import__(mod_name, fromlist=["_"])
    flt = Filter()
    flt.install(module, helpers)
    setup_module = getattr(module, "setUpModule", None)
    if setup_module:
        setup_module()
    import_codes = _drain()
    sys.monitoring.set_events(TOOL, 0)
    if flt.calls:
        # A loader ran at import: every test shares those entries.
        return sorted(import_codes), {}, sorted(flt.calls)
    class_codes = {}
    shared = []

    def class_done(tag, res):
        class_codes[tag] = set(map(_norm, res["codes"])) if res else None
        if res and res["calls"]:
            shared.append(tag)

    pool(jobs, ((c, lambda c=c: run_child(getattr(module, c), None, flt, None, set(import_codes), timeout))
                for c in sorted({c for _, c, _ in tests})), class_done)
    # A class whose setup failed or loads fixtures itself: its tests are not mapped.
    tests = [t for t in tests if class_codes.get(t[1]) is not None and t[1] not in shared]
    parent = set(import_codes)

    bases = {}

    def base_done(tag, res):
        bases[tag] = res

    by_number = {n: (getattr(module, c), c, m) for n, c, m in tests}
    pool(jobs, ((n, lambda n=n: run_child(by_number[n][0], by_number[n][2], flt, set(), parent, timeout)) for n in by_number), base_done)
    for n, res in bases.items():
        if res is not None:
            res["codes"] = sorted(set(map(_norm, res["codes"])) - class_codes[by_number[n][1]])

    records = {}
    work = {}
    for n, res in bases.items():
        if res is None or not res["calls"]:
            continue
        files = sorted(res["calls"])
        entries = [(f, i) for f in files for i in range(res["calls"][f])]
        records[n] = {
            "cls": by_number[n][1],
            "files": [[f, res["calls"][f], sha256_file(os.path.join(SG, "tests", "fixtures", f))] for f in files],
            "base_ok": res["ok"],
            "whole": sorted(set(map(_norm, res["codes"])) | class_codes[by_number[n][1]]),
            "entries": [None] * len(entries),
        }
        work[n] = (entries, parent | set(records[n]["whole"]))

    def entry_done(tag, res):
        n, j = tag
        records[n]["entries"][j] = res

    def entry_tasks():
        for n, (entries, exclude) in work.items():
            cls, _, meth = by_number[n]
            for j, e in enumerate(entries):
                yield (n, j), (lambda cls=cls, meth=meth, e=e, exclude=exclude: run_child(cls, meth, flt, {e}, exclude, timeout))

    pool(jobs, entry_tasks(), entry_done)
    return sorted(import_codes), records, shared


def tree_digest(paths):
    h = hashlib.sha256()
    for rel in sorted(paths):
        h.update(rel.encode() + b"\0" + sha256_file(os.path.join(SG, rel)).encode() + b"\n")
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--module", action="append", help="limit to these modules (debugging)")
    args = ap.parse_args()

    sys.path[:0] = ["/app", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    from strategies.selectors.map_format import SOURCE, catalog_records, catalog_sha256

    with open(args.catalog) as fh:
        catalog = json.load(fh)["tests"]
    by_module: dict[str, list] = {}
    for t in catalog:
        path, cls, meth = t["id"].split("::")
        with open(os.path.join(SG, path), encoding="utf-8") as fh:
            text = fh.read()
            if not any(name in text for name in LOADERS):
                continue
        by_module.setdefault(path[:-3].replace("/", "."), []).append((t["number"], cls, meth))
    if args.module:
        by_module = {m: v for m, v in by_module.items() if m in args.module}

    codes: dict[tuple, int] = {}
    sets: dict[int, int] = {}
    tests_out = {}
    modules_out = {}

    def cid(k):
        return codes.setdefault(_norm(k), len(codes))

    def sid(mask):
        return sets.setdefault(mask, len(sets))

    for mod_name, tests in sorted(by_module.items()):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                out = record_module(mod_name, tests, args.jobs, args.timeout)
                with os.fdopen(w, "w") as fh:
                    json.dump(out, fh)
            finally:
                os._exit(0)
        os.close(w)
        with os.fdopen(r) as fh:
            raw = fh.read()
        os.waitpid(pid, 0)
        import_codes, records, shared = json.loads(raw)
        modules_out[mod_name] = {"import": [cid(k) for k in import_codes], "shared_loads": shared}
        skipped = 0
        for n, rec in records.items():
            n = int(n)
            if not rec["base_ok"] or any(e is None or not e["ok"] for e in rec["entries"]):
                skipped += 1
                continue
            masks: dict[int, int] = {}
            for j, e in enumerate(rec["entries"]):
                if e["calls"] != {f: c for f, c, _ in rec["files"]}:
                    break
                for k in e["codes"]:
                    c = cid(k)
                    masks[c] = masks.get(c, 0) | 1 << j
            else:
                tests_out[str(n)] = {
                    "module": mod_name,
                    "files": rec["files"],
                    "whole": sorted(cid(k) for k in rec["whole"]),
                    "entries": {str(c): sid(m) for c, m in sorted(masks.items())},
                }
                continue
            skipped += 1
        print(f"{mod_name}: {len(tests)} tests, {len(records)} fixture-driven, {skipped} not mapped", file=sys.stderr, flush=True)

    used = {f for t in tests_out.values() for f, _, _ in t["files"]}
    test_files = {m.replace(".", "/") + ".py" for m in modules_out} | {"tests/helpers.py"}
    data = {
        "format": FORMAT,
        "source": SOURCE,
        "catalog_sha256": catalog_sha256(catalog_records(catalog)),
        "python": list(sys.version_info[:2]),
        "tree_sha256": tree_digest(test_files | {f"tests/fixtures/{f}" for f in used}),
        "tree_files": sorted(test_files | {f"tests/fixtures/{f}" for f in used}),
        "codes": [[k[0], [list(r) for r in k[1]]] for k, _ in sorted(codes.items(), key=lambda kv: kv[1])],
        "sets": [format(m, "x") for m, _ in sorted(sets.items(), key=lambda kv: kv[1])],
        "modules": modules_out,
        "tests": tests_out,
    }
    with gzip.open(args.output, "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump(data, fh, separators=(",", ":"), sort_keys=True)


if __name__ == "__main__":
    main()
