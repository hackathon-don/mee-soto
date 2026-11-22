"""Select tests for a batch of patches using impact analysis and verification."""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import sys
import time
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING

from .. import CatalogTest, Patch

if TYPE_CHECKING:
    from config import Config

HERE = os.path.dirname(os.path.abspath(__file__))
SG = os.environ.get("BR_SG", "/opt/sg")
MAPS = os.environ.get("BR_MAPS", os.path.join(HERE, "maps"))
WORK = os.environ.get("BR_WORK", "/tmp/br")
PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
# No new verification child starts after WALL_BUDGET seconds; at
# WALL_BUDGET + HARD_GRACE every running child is killed and its remaining tests
# are included unrun, so the answers always print inside the 180 s limit.
WALL_BUDGET = float(os.environ.get("BR_WALL_BUDGET", "140"))
HARD_GRACE = float(os.environ.get("BR_HARD_GRACE", "20"))
IMPORT_PROBE_TIMEOUT = 2.0

# Tests we never run in-process: they spawn process pools of their own or are
# too slow to be worth verifying. They are included whenever they are candidates.
NEVER_RUN_IDS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
}
TPCDS_SETUP_MS = int(os.environ.get("BR_TPCDS_SETUP_MS", "100"))  # class fixture counted separately

# Fork + fresh sqlglot/test-module imports for one verification child, and the
# class fixtures that run once per child (measured natively, scaled below).
COMMIT_OVERHEAD_MS = int(os.environ.get("BR_COMMIT_OVERHEAD_MS", "100"))
# Longest a child may go without reporting while outside a test (imports, class
# fixtures; the slowest legitimate one is ~1.4 s).
IDLE_LIMIT = float(os.environ.get("BR_IDLE_LIMIT", "12"))
CLASS_SETUP_MS = {"tests/test_executor.py::TestExecutor": 1000}
FIXTURE_SUBSET_MODULES = {"tests.test_optimizer", "tests.test_transpile", "tests.test_serde"}
# maps/cpu.json: in-process CPU per test on the dev VM (children run with a
# raised GC threshold). Under default GC the VM matched the recorded seconds, so
# assume the scorer runs at the same speed until the board says otherwise.
CPU_FACTOR = float(os.environ.get("BR_CPU_FACTOR", "1.0"))
# Running costs a little more than the per-test sum (imports, contention), and
# failures it finds still have to be included afterwards.
# Tuned with tools/simulate.py on 119 mutants + the 30 sample commits.
RUN_SCALE = float(os.environ.get("BR_RUN_SCALE", "1.6"))
FAIL_MS = int(os.environ.get("BR_FAIL_MS", "1000"))
# Failures cluster: once a commit has this many, stop verifying it and include the rest.
STOP_AFTER = int(os.environ.get("BR_STOP_AFTER", "5"))
# Heavy tests decided after the run (see choose()).
DEFERRABLE_IDS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_parser.py::TestParser::test_max_nodes",
}
MIN_EVIDENCE = int(os.environ.get("BR_MIN_EVIDENCE", "3"))
DEFER_FAIL_P = float(os.environ.get("BR_DEFER_FAIL_P", "0.2"))


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def bits(m: int) -> list[int]:
    out = []
    while m:
        low = m & -m
        out.append(low.bit_length() - 1)
        m ^= low
    return out


def _filter_import_masks(import_checks: list[str], masks: dict[str, int], catalog_numbers) -> dict[str, int]:
    catalog_mask = sum(1 << n for n in catalog_numbers)
    filtered = {}
    for module in import_checks:
        mask = masks.get(module, 0) & catalog_mask
        if mask:
            filtered[module] = mask
    return filtered


# ---------------------------------------------------------------- overlay


def build_overlay(base: str, changes: dict[str, str | None], work: str) -> str:
    """A tree under `work` that mirrors `base` via symlinks except changed files."""
    os.makedirs(work, exist_ok=True)
    real_dirs = set()
    for rel in changes:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            real_dirs.add("/".join(parts[:i]))

    def populate(reldir: str):
        src = os.path.join(base, reldir) if reldir else base
        dst = os.path.join(work, reldir) if reldir else work
        os.makedirs(dst, exist_ok=True)
        try:
            names = os.listdir(src)
        except FileNotFoundError:
            names = []
        present = set(names)
        for name in names:
            rel = f"{reldir}/{name}" if reldir else name
            if rel in real_dirs:
                populate(rel)
            elif rel in changes:
                text = changes[rel]
                if text is not None:
                    with open(os.path.join(dst, name), "w", encoding="utf-8") as fh:
                        fh.write(text)
            else:
                os.symlink(os.path.join(src, name), os.path.join(dst, name))
        for rel, text in changes.items():
            if os.path.dirname(rel) == reldir and os.path.basename(rel) not in present and text is not None:
                with open(os.path.join(dst, os.path.basename(rel)), "w", encoding="utf-8") as fh:
                    fh.write(text)
        for rel in real_dirs:
            if os.path.dirname(rel) == reldir and os.path.basename(rel) not in present:
                populate(rel)

    populate("")
    return work


# ---------------------------------------------------------------- planning


class Plan:
    __slots__ = (
        "cid", "include", "run", "notes", "failed", "root", "error", "cpu", "subsets", "fixture_edits",
        "est_run_ms", "wall", "deferred", "child_cpu", "pending_imports", "failed_imports",
        "diagnostics",
    )

    def __init__(self, cid):
        self.cid = cid
        self.include: set[int] = set()
        self.run: list[int] = []
        self.notes: list[str] = []
        self.failed: set[int] = set()
        self.root: str | None = None
        self.error: str | None = None
        self.cpu: dict[int, float] = {}
        self.subsets: dict[int, list[int]] = {}
        self.fixture_edits: list[str] = []
        self.est_run_ms = 0
        self.wall = 0.0
        self.deferred: set[int] = set()
        self.child_cpu = 0.0
        self.pending_imports: dict[str, int] = {}
        self.failed_imports: set[int] = set()
        self.diagnostics: dict | None = None


def choose(
    plan: Plan,
    cand: list[int],
    must: list[int],
    never: set[int],
    ms: dict[int, int],
    cpu: dict[int, int],
    ids: dict[int, str],
    deferrable: set[int] = frozenset(),  # type: ignore[assignment]
):
    """Split candidates into include-unrun and run, minimising expected cost.

    Deferrable tests (the two heaviest) can instead be decided after the run:
    they are included only if another candidate fails. On 28 commits where one
    was a candidate it never failed while every other candidate passed.
    """
    # Selection uses the original cost model, not the experiment's final scheduler.
    from .pack import pack

    def setup_cost(tests):
        classes = {ids[n].rsplit("::", 1)[0] for n in tests}
        return sum(int(CLASS_SETUP_MS.get(c, 0) * CPU_FACTOR) for c in classes)

    def best_split(pool: list[int], defer: list[int]):
        include = set(must) | {n for n in pool if n in never}
        runnable = [n for n in pool if n not in include]
        # Include first the tests that cost the most to run relative to their recorded time.
        runnable.sort(key=lambda n: -(cpu[n] / max(ms[n], 1)))
        ks = sorted(set([0, len(runnable)] + [k for k in range(1, len(runnable)) if k <= 12 or k % max(1, len(runnable) // 40) == 0]))
        suffix = [0] * (len(runnable) + 1)
        for i in range(len(runnable) - 1, -1, -1):
            suffix[i] = suffix[i + 1] + cpu[runnable[i]]
        best = None
        for k in ks:
            inc = include | set(runnable[:k])
            run_n = len(runnable) - k
            # The rule is "include the heavy test iff another candidate fails",
            # so every other candidate must actually run.
            evidence = k == 0 and not include and run_n >= MIN_EVIDENCE
            if defer and evidence:
                base, _ = pack(sorted(inc), ms)
                with_h, _ = pack(sorted(inc | set(defer)), ms)
                static_cost = base + int(DEFER_FAIL_P * (with_h - base))
            else:
                static_cost, _ = pack(sorted(inc | set(defer)), ms)
            run_cost = (
                int(RUN_SCALE * (COMMIT_OVERHEAD_MS + suffix[k] + setup_cost(runnable[k:]))) + FAIL_MS
                if k < len(runnable)
                else 0
            )
            cost = static_cost + run_cost
            if best is None or cost < best[0]:
                best = (cost, k, bool(defer and evidence))
        cost, k, deferred = best
        return cost, include | set(runnable[:k]), runnable[k:], deferred, suffix[k] + setup_cost(runnable[k:])

    options = [best_split(cand, [])]
    defer = [n for n in cand if n in deferrable and n not in must]
    if defer:
        options.append(best_split([n for n in cand if n not in defer], defer))
    cost, include, run, deferred, est = min(options, key=lambda o: o[0])
    if len(options) > 1 and options.index(min(options, key=lambda o: o[0])) == 1:
        if deferred:
            plan.deferred = set(defer)
            plan.notes.append(f"deferred {sorted(defer)}")
        else:
            include = include | set(defer)
    plan.include = include
    # Cheapest first, so early failures are found cheaply (see STOP_AFTER).
    plan.run = sorted(run, key=lambda n: (cpu[n], n))
    plan.est_run_ms = (COMMIT_OVERHEAD_MS + est) if plan.run else 0


def settle_deferred(plan: Plan) -> None:
    """Include deferred heavy tests unless the run gave clean evidence."""
    if not plan.deferred:
        return
    incomplete = bool(set(plan.run) & plan.include) or plan.error is not None
    if plan.failed or incomplete:
        plan.include |= plan.deferred
        plan.notes.append("deferred tests included")
    else:
        plan.notes.append("deferred tests skipped: all other candidates passed")


def _fixture_pairs(text: str) -> list[tuple[dict, str, str]]:
    """Same parsing as tests.helpers.load_sql_fixture_pairs."""
    lines = [l for l in text.splitlines() if l and not l.startswith("--")]
    statements = "\n".join(lines).split(";")
    out = []
    for i in range(0, len(statements), 2):
        if i + 1 < len(statements):
            sql = statements[i].strip()
            meta = {}
            sql_lines = sql.split("\n")
            j = 0
            while j < len(sql_lines) and sql_lines[j].startswith("#"):
                if ":" not in sql_lines[j]:
                    return None  # the real loader raises here: let the full test decide
                key, val = sql_lines[j].split(":", maxsplit=1)
                meta[key.lstrip("#").strip()] = val.strip()
                j += 1
            out.append((meta, "\n".join(sql_lines[j:]), statements[i + 1].strip()))
    return out


def tpcds_fixture_queries(new_text: str | None) -> list[int] | None:
    """Query indices whose SQL or metadata changed in an edit of tpc-ds.sql, or
    None when the edit shifts the pairing (then the whole test is needed)."""
    if new_text is None:
        return None
    try:
        old_pairs = _fixture_pairs(open(os.path.join(SG, "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql")).read())
        new_pairs = _fixture_pairs(new_text)
    except Exception:  # noqa: BLE001
        return None
    if old_pairs is None or new_pairs is None or len(old_pairs) != len(new_pairs):
        return None
    # test_execute_tpcds reads each query's metadata and SQL, never the expected output.
    return [i for i, (o, n) in enumerate(zip(old_pairs, new_pairs)) if o[:2] != n[:2]]


# ---------------------------------------------------------------- running


def preimport():
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    import unittest  # noqa: F401
    import unittest.mock  # noqa: F401
    import concurrent.futures  # noqa: F401
    import concurrent.futures.process  # noqa: F401
    import multiprocessing  # noqa: F401
    import csv, decimal, datetime, pickle, json as _j, typing, weakref, doctest, tempfile, ast  # noqa: F401,E401

    for name in ("numpy", "pandas", "pandas.testing", "duckdb", "pytz", "dateutil"):
        try:
            __import__(name)
        except Exception as e:  # noqa: BLE001
            log("preimport failed", name, e)


class Child:
    __slots__ = ("pid", "fd", "buf", "plan", "pending", "current", "last", "limit", "started",
                 "stopped", "diagnostics")

    def __init__(self, pid, fd, plan, pending):
        self.pid = pid
        self.fd = fd
        self.buf = b""
        self.plan = plan
        self.pending = list(pending)  # tests not yet reported done
        self.current = None
        self.last = time.time()
        self.limit = IDLE_LIMIT
        self.started = time.time()
        self.stopped = False
        self.diagnostics = ({"timed": [], "total": False, "uncertain": False}
                            if plan.diagnostics is not None else None)


def spawn_child(plan: Plan, tests: list[int], ids: dict[int, str], limits: dict[int, float]) -> Child:
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            devnull = os.open(os.devnull, os.O_WRONLY)
            debug = os.environ.get("BR_DEBUG_CHILD")
            os.dup2(2 if debug else devnull, 1)
            if not debug:
                os.dup2(devnull, 2)
            from .runner import child_main

            child_main(
                plan.root,
                [(n, ids[n], limits[n], plan.subsets.get(n)) for n in tests],
                w,
                base=SG,
                fixture_edits=plan.fixture_edits,
                import_checks=list(plan.pending_imports),
                import_timeout=IMPORT_PROBE_TIMEOUT,
            )
        except BaseException:  # noqa: BLE001
            pass
        finally:
            os._exit(0)
    os.close(w)
    os.set_blocking(r, False)
    return Child(pid, r, plan, tests)


def run_all(plans: list[Plan], ids, limits, deadline: float):
    queue: list[tuple[Plan, list[int]]] = [(p, p.run) for p in plans if p.run or p.pending_imports]
    active: dict[int, Child] = {}
    while queue or active:
        while queue and len(active) < PARALLEL:
            plan, tests = queue.pop(0)
            if time.time() > deadline:
                plan.include |= set(tests)
                plan.notes.append("out of time: included unrun")
                continue
            ch = spawn_child(plan, tests, ids, limits)
            active[ch.fd] = ch
        if not active:
            continue
        ready, _, _ = select.select(list(active), [], [], 0.5)
        now = time.time()
        for fd in ready:
            ch = active[fd]
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if data:
                ch.buf += data
                ch.last = now
                *lines, ch.buf = ch.buf.split(b"\n")
                for line in lines:
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        if ch.diagnostics is not None:
                            ch.diagnostics["uncertain"] = True
                        continue
                    if "cpu_total" in msg:
                        if ch.diagnostics is not None:
                            ch.diagnostics["total"] = True
                        ch.plan.child_cpu += msg["cpu_total"]
                    elif "i" in msg:
                        module = msg["i"]
                        mask = ch.plan.pending_imports.pop(module, None)
                        if mask is not None and not msg.get("ok", False):
                            ch.plan.failed_imports.update(bits(mask))
                    elif "s" in msg:
                        ch.current = msg["s"]
                        ch.limit = limits.get(msg["s"], 30.0) + 5.0
                    elif "d" in msg:
                        n = msg["d"]
                        if ch.diagnostics is not None:
                            if "cpu" in msg:
                                ch.diagnostics["timed"].append((n, msg["cpu"]))
                            else:
                                ch.diagnostics["uncertain"] = True
                        if "cpu" in msg:
                            ch.plan.cpu[n] = msg["cpu"]
                        if not msg["ok"]:
                            ch.plan.failed.add(n)
                        if n in ch.pending:
                            ch.pending.remove(n)
                        if ch.current == n:
                            ch.current = None
                            ch.limit = IDLE_LIMIT
                        if not msg["ok"] and len(ch.plan.failed) >= STOP_AFTER and ch.pending:
                            ch.stopped = True
                continue
            # EOF: the child finished or died.
            finish(ch, active, queue, now)
        if now > deadline + HARD_GRACE:
            for fd, ch in list(active.items()):
                try:
                    os.kill(ch.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if ch.current is not None:
                    ch.plan.include.add(ch.current)
                ch.current = None
                ch.plan.notes.append("hard deadline: included unrun")
                finish(ch, active, queue, now)
            for plan, tests in queue:
                plan.include |= set(tests)
            queue.clear()
            continue
        for fd, ch in list(active.items()):
            if ch.stopped:
                try:
                    os.kill(ch.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                ch.current = None
                ch.plan.notes.append(f"stopped after {len(ch.plan.failed)} failures")
                finish(ch, active, queue, now)
                continue
            if now - ch.last > ch.limit:
                log(f"{ch.plan.cid}: child stuck on {ch.current}, killing")
                try:
                    os.kill(ch.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                finish(ch, active, queue, now)


    for plan in plans:
        for mask in plan.pending_imports.values():
            plan.include.update(bits(mask))


def finish(ch: Child, active, queue, now):
    active.pop(ch.fd, None)
    ch.plan.wall += now - ch.started
    try:
        os.close(ch.fd)
    except OSError:
        pass
    status = None
    try:
        _, status = os.waitpid(ch.pid, 0)
    except ChildProcessError:
        pass
    if ch.diagnostics is not None:
        complete = (
            status == 0 and not ch.pending and not ch.buf and not ch.stopped
            and ch.diagnostics["total"] and not ch.diagnostics["uncertain"]
        )
        if complete:
            ch.plan.diagnostics["timed"].extend(ch.diagnostics["timed"])
        else:
            ch.plan.diagnostics["incomplete_children"] += 1
    if not ch.pending:
        return
    if ch.current is not None:
        # Crashed or hung inside a test: that test fails; carry on with the rest.
        ch.plan.failed.add(ch.current)
        rest = [n for n in ch.pending if n != ch.current]
        ch.plan.notes.append(f"child died in {ch.current}")
        if rest:
            queue.insert(0, (ch.plan, rest))
    else:
        # Died outside a test (import, fixtures): include what is left unrun.
        ch.plan.include |= set(ch.pending)
        ch.plan.notes.append(f"child died outside a test; {len(ch.pending)} included unrun")


# ---------------------------------------------------------------- main


class Context:
    """Per-run inputs shared by every commit's planning."""

    def __init__(self, tests: list[CatalogTest]):
        from .impact import Analyzer, Index

        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        self.ids = {t["number"]: t["id"] for t in tests}
        self.cpu = dict(self.ms)
        inproc_fail: set[int] = set()
        cpu_path = os.path.join(MAPS, "cpu.json")
        if os.path.exists(cpu_path):
            data = json.load(open(cpu_path))
            measured = {int(k): v for k, v in data["cpu"].items()}
            inproc_fail = set(data.get("fail", []))
            for n in self.cpu:
                if n in measured:
                    self.cpu[n] = int(measured[n] * 1000 * CPU_FACTOR)
        self.limits = {n: max(8.0, 8.0 * self.cpu[n] / 1000.0) for n in self.cpu}
        self.ix = Index(os.path.join(MAPS, "index.pkl.gz"), os.path.join(MAPS, "static.pkl.gz"))
        self.never = {n for n, i in self.ids.items() if i in NEVER_RUN_IDS} | set(self.ix.baseline_fail) | inproc_fail
        self.an = Analyzer(self.ix, SG)
        # Per-query TPC-DS costs were traced under default GC; rescale them so
        # they sum to the whole test's measured in-process cost.
        tp = self.ix.pseudo.get("tpcds")
        if tp and tp["cpu"]:
            total = sum(tp["cpu"].values())
            if total > 0 and tp["test"] in self.cpu:
                f = (self.cpu[tp["test"]] / 1000.0) / total
                tp["cpu"] = {i: v * f for i, v in tp["cpu"].items()}


class Analysis:
    __slots__ = ("plan", "imp", "cand", "must", "never", "cpu", "deferrable")


def analyze_commit(ctx: Context, cid: str, text: str) -> Analysis:
    """Candidates and per-commit cost inputs; no run/include decision yet."""
    from .diffparse import parse_patch

    ms, ids, cpu, never, ix = ctx.ms, ctx.ids, ctx.cpu, ctx.never, ctx.ix
    plan = Plan(cid)
    imp = ctx.an.analyze(parse_patch(text))
    plan.notes.extend(imp.notes)
    candidate_mask = imp.candidates
    cand = [n for n in bits(candidate_mask) if n in ms]
    plan.pending_imports = _filter_import_masks(imp.import_checks, imp.import_fail_masks, ms)
    must = [n for n in bits(imp.must_fail) if n in ms]
    commit_never, commit_cpu = never, cpu
    tp = ix.pseudo.get("tpcds")
    if tp and tp["test"] in cand:
        qs = sorted(i for i, n in tp["queries"].items() if imp.candidates >> n & 1)
        if qs and not imp.candidates >> tp["setup"] & 1:
            plan.subsets[tp["test"]] = qs
            commit_never = never - {tp["test"]}
            commit_cpu = dict(cpu)
            commit_cpu[tp["test"]] = int(TPCDS_SETUP_MS * CPU_FACTOR + 1000 * sum(tp["cpu"].get(i, 1.0) for i in qs))
            plan.notes.append(f"tpcds subset {len(qs)} queries")
    if tp and tp["test"] in cand and "optimizer/tpc-ds/tpc-ds.sql" in imp.fixture_edits and not imp.code_effect:
        qs = tpcds_fixture_queries(imp.new_files.get("tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"))
        other_files = [f for f in imp.fixture_edits if f != "optimizer/tpc-ds/tpc-ds.sql"]
        if qs is not None and not other_files:
            if qs:
                plan.subsets[tp["test"]] = qs
                commit_never = never - {tp["test"]}
                commit_cpu = dict(commit_cpu)
                commit_cpu[tp["test"]] = int(TPCDS_SETUP_MS * CPU_FACTOR + 1000 * sum(tp["cpu"].get(i, 1.0) for i in qs))
            else:
                cand = [n for n in cand if n != tp["test"]]
            plan.notes.append(f"tpc-ds fixture edit: queries {qs}")
    if imp.fixture_edits and not imp.code_effect:
        # Only fixture items changed: re-check just those items.
        plan.fixture_edits = imp.fixture_edits
        commit_cpu = dict(commit_cpu)
        for n in cand:
            if ids[n].split("::")[0][:-3].replace("/", ".") in FIXTURE_SUBSET_MODULES:
                commit_cpu[n] = min(commit_cpu[n], 150)
    a = Analysis()
    a.plan, a.imp, a.cand, a.must, a.never, a.cpu = plan, imp, cand, must, commit_never, commit_cpu
    # A TPC-DS fixture edit is invisible to every other test: verify it directly.
    tpcds_fixture = "optimizer/tpc-ds/tpc-ds.sql" in imp.fixture_edits
    a.deferrable = {n for n in cand if ids[n] in DEFERRABLE_IDS and not (tpcds_fixture and n == (tp or {}).get("test"))}
    return a


class ImpactSelector:
    def __init__(self, catalog: Sequence[CatalogTest], config: Config):
        self.start_wall = time.time()
        tests = list(catalog)
        self.ctx: Context | None
        try:
            self.ctx = Context(tests)
        except Exception as e:  # noqa: BLE001
            log("index load failed:", repr(e))
            self.ctx = None
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        self.ids = {t["number"]: t["id"] for t in tests}

    def select(self, commits: Sequence[Patch]) -> dict[str, list[int]]:
        ctx, ms, ids = self.ctx, self.ms, self.ids
        limits = ctx.limits if ctx else {n: 30.0 for n in ms}
        plans: list[Plan] = []
        diagnostics = os.environ.get("BR_CPU_DIAGNOSTICS") == "1"
        for patch in commits:
            cid = patch.commit
            try:
                if ctx is None:
                    raise RuntimeError("no index")
                a = analyze_commit(ctx, cid, patch.diff)
                plan = a.plan
                choose(plan, a.cand, a.must, a.never, ms, a.cpu, ids, a.deferrable)
                tp = ctx.ix.pseudo.get("tpcds")
                if tp and tp["test"] in plan.subsets and tp["test"] not in plan.run:
                    del plan.subsets[tp["test"]]
                if plan.run or plan.pending_imports:
                    plan.root = build_overlay(SG, {k: v for k, v in a.imp.new_files.items() if k}, os.path.join(WORK, cid))
                log(f"{cid}: cand={len(a.cand)} must={len(a.must)} include={len(plan.include)} run={len(plan.run)} {plan.notes[:3]}")
            except Exception as e:  # noqa: BLE001
                plan = Plan(cid)
                plan.error = repr(e)
                plan.include = set(ms)
                log(f"{cid}: analysis failed, running everything: {e!r}")
            if diagnostics:
                plan.diagnostics = {"timed": [], "incomplete_children": 0}
            plans.append(plan)

        if any(p.run or p.pending_imports for p in plans):
            preimport()
            run_all(plans, ids, limits, self.start_wall + WALL_BUDGET)

        selected = dict(self._selected(plans))
        if diagnostics:
            for plan in plans:
                print(
                    "RWX_CPU_V1 " + json.dumps({
                        "commit": plan.cid,
                        "child_cpu": round(plan.child_cpu, 6),
                        **plan.diagnostics,
                    }, separators=(",", ":")),
                    file=sys.stderr,
                )
        return selected

    @staticmethod
    def _selected(plans: list[Plan]) -> Iterator[tuple[str, list[int]]]:
        for plan in plans:
            settle_deferred(plan)
            selected = sorted(plan.include | plan.failed | plan.failed_imports)
            yield plan.cid, selected
            log(
                f"{plan.cid}: selected={len(selected)} failed_run={sorted(plan.failed)[:10]} "
                f"run={len(plan.run)} est={plan.est_run_ms/1000:.1f}s testcpu={sum(plan.cpu.values()):.1f}s "
                f"childcpu={plan.child_cpu:.2f}s wall={plan.wall:.1f}s "
                f"{plan.notes[-2:] if plan.notes else ''}"
            )
        shutil.rmtree(WORK, ignore_errors=True)
