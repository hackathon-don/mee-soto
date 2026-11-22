"""First-failure table selector that reruns only the fixture entries a code edit can reach.

Fixture-driven tests (tests/test_optimizer.py, test_transpile.py, test_serde.py)
loop over hundreds of entries from tests/fixtures. tools/record_fixture_entries.py
runs each such test once per entry in isolation at the pinned SQLGlot commit and
records which code objects (and the lines each runs itself) each entry entered, plus the code the test runs with
no entries at all (setUp, code outside the loop), its class setup, and its
module's import. When a commit only changes the run-time body of existing
functions in sqlglot/, a checked fixture test runs just the entries that entered
a changed function.

Anything else runs the whole test, as the base selector would: a missing or
stale map, definition-time or import-time edits, edits outside sqlglot/, fixture
or test edits, a changed function the test reaches outside its entries or in
class/module setup, and a test the map does not cover or disagrees with.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
from bisect import bisect_right
from collections.abc import Sequence
from pathlib import Path

from .impact import runner, selector as impact_selector
from .impact.diffparse import parse_patch
from .impact_first_failure_tables_free_cpu import FirstFailureTablesFreeCpuImpact
from .map_format import SOURCE, catalog_records, catalog_sha256

MAP_PATH = Path(__file__).with_name("fixture_entries.json.gz")
FORMAT = 1
LOADERS = ("load_sql_fixture_pairs", "load_sql_fixtures")
MIN_ENTRY_CPU_MS = 30

# Set in the parent before each verification child forks; read in the child.
_ACTIVE: dict[int, dict[str, tuple[int, frozenset[int]]]] = {}
_CURRENT: dict[str, tuple[int, frozenset[int]]] | None = None


class EntryMap:
    def __init__(self, data: dict) -> None:
        self.sets = data["sets"]
        self.tests = data["tests"]
        self.modules = data["modules"]
        by_path: dict[str, list[tuple[int, int, int]]] = {}
        for cid, (path, ranges) in enumerate(data["codes"]):
            for lo, hi in ranges:
                by_path.setdefault(path, []).append((lo, hi, cid))
        self.by_path = {}
        for path, items in by_path.items():
            items.sort()
            self.by_path[path] = (items, [lo for lo, _, _ in items], max(hi - lo for lo, hi, _ in items))

    def _hits(self, path: str, lo: int, hi: int) -> list[int]:
        entry = self.by_path.get(path)
        if entry is None:
            return []
        items, starts, width = entry
        out = []
        for i in range(bisect_right(starts, lo - width - 1), bisect_right(starts, hi)):
            c_lo, c_hi, cid = items[i]
            if c_hi >= lo and c_lo <= hi:
                out.append(cid)
        return sorted(set(out))

    def keep(self, number: int, spans: list[tuple[str, int, int]]) -> tuple[dict[str, tuple[int, frozenset[int]]], int, int] | None:
        """{file: (entry count, kept indices)}, kept and total entries; None runs the whole test."""
        test = self.tests.get(str(number))
        if test is None:
            return None
        whole = set(test["whole"])
        whole.update(self.modules[test["module"]]["import"])
        entries = test["entries"]
        mask = 0
        for span in spans:
            hits = self._hits(*span)
            seen = False
            for cid in hits:
                if cid in whole:
                    return None
                sid = entries.get(str(cid))
                if sid is not None:
                    mask |= int(self.sets[sid], 16)
                    seen = True
            if not seen:
                return None  # the static map says the test runs this code; the record never saw it
        out = {}
        offset = 0
        total = 0
        for name, count, _ in test["files"]:
            part = (mask >> offset) & ((1 << count) - 1)
            out[name] = (count, frozenset(i for i in range(count) if part >> i & 1))
            offset += count
            total += count
        return out, mask.bit_count(), total


def _file_sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load_entry_map(catalog: Sequence, path: Path = MAP_PATH) -> EntryMap | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        if data["format"] != FORMAT or data["source"] != SOURCE:
            return None
        if data["python"] != list(sys.version_info[:2]):
            return None
        if data["catalog_sha256"] != catalog_sha256(catalog_records(catalog)):
            return None
        digest = hashlib.sha256()
        for rel in sorted(data["tree_files"]):
            digest.update(rel.encode() + b"\0" + _file_sha(os.path.join(impact_selector.SG, rel)).encode() + b"\n")
        if digest.hexdigest() != data["tree_sha256"]:
            return None
        return EntryMap(data)
    except Exception:  # noqa: BLE001 - an unusable map means whole tests
        return None


def _narrowable(text: str) -> bool:
    """Only edits to existing sqlglot/*.py files; anything else keeps whole tests."""
    patches = parse_patch(text)
    return bool(patches) and all(
        fp.old_path and fp.old_path == fp.new_path and fp.old_path.startswith("sqlglot/") and fp.old_path.endswith(".py")
        for fp in patches
    )


def _filtered(orig):
    def load(filename):
        keep = _CURRENT
        if keep is None or filename not in keep:
            return orig(filename)
        count, kept = keep[filename]
        items = list(orig(filename))
        if len(items) != count:
            return iter(items)  # not the recorded fixture: run every entry
        return iter([item for i, item in enumerate(items) if i in kept])

    load.__wrapped_fixture_loader__ = orig
    return load


def _install_filter(module) -> None:
    helpers = sys.modules.get("tests.helpers")
    if helpers is None or getattr(module, "__fixture_entry_filter__", False):
        return
    for name in LOADERS:
        orig = getattr(helpers, name, None)
        if orig is not None and getattr(module, name, None) is orig:
            setattr(module, name, _filtered(orig))
    module.__fixture_entry_filter__ = True


class FixtureEntryImpact(FirstFailureTablesFreeCpuImpact):
    def __init__(self, catalog, config):
        super().__init__(catalog, config)
        self.entries = load_entry_map(list(catalog)) if self.ctx is not None else None
        if self.ctx is not None and self.entries is None:
            print("fixture entry map unusable: running whole tests", file=sys.stderr, flush=True)
        self._captures: list[tuple[int, list | None, int]] = []
        self._keeps: dict[str, dict[int, dict]] = {}
        if self.entries is not None:
            self._wrap_analyzer()

    def _wrap_analyzer(self) -> None:
        an = self.ctx.an
        ix = an.ix
        diff_mask = an._diff_mask
        captures = self._captures

        def wrapped(path, mod, d):
            spans: list = []
            deftime = []
            run_mask = type(ix).run_mask
            deftime_mask = type(an)._deftime_mask

            def capture_run(p, lo, hi):
                m = run_mask(ix, p, lo, hi)
                spans.append((p, lo, hi, m))
                return m

            def capture_deftime(*args):
                deftime.append(True)
                return deftime_mask(an, *args)

            ix.run_mask = capture_run
            an._deftime_mask = capture_deftime
            try:
                m = diff_mask(path, mod, d)
            finally:
                del ix.run_mask
                del an._deftime_mask
            explained = 0
            for *_, sm in spans:
                explained |= sm
            captures.append((m, None if deftime else spans, explained))
            return m

        an._diff_mask = wrapped

    def _narrow(self, a, text: str) -> None:
        imp = a.imp
        captures = list(self._captures)
        if not captures or imp.fixture_edits or imp.must_fail or not _narrowable(text):
            return
        from_diffs = 0
        for m, _, _ in captures:
            from_diffs |= m
        if imp.candidates & ~from_diffs:
            return
        keeps = {}
        cpu = None
        for n in a.cand:
            mod = self.ids[n].split("::")[0][:-3].replace("/", ".")
            if mod not in runner.FIXTURE_SUBSET_MODULES:
                continue
            spans = []
            ok = True
            for m, cap_spans, explained in captures:
                if not m >> n & 1:
                    continue
                if cap_spans is None or not explained >> n & 1:
                    ok = False
                    break
                spans.extend((p, lo, hi) for p, lo, hi, sm in cap_spans if sm >> n & 1)
            if not ok or not spans:
                continue
            res = self.entries.keep(n, spans)
            if res is None:
                continue
            keep, kept, total = res
            if kept >= total:
                continue
            keeps[n] = keep
            if cpu is None:
                cpu = dict(a.cpu)
            cpu[n] = min(cpu[n], max(MIN_ENTRY_CPU_MS, round(cpu[n] * kept / max(total, 1)) + MIN_ENTRY_CPU_MS))
        if keeps:
            a.cpu = cpu
            self._keeps[a.plan.cid] = keeps
            summary = sum(sum(len(k) for _, k in keep.values()) for keep in keeps.values())
            a.plan.notes.append(f"fixture entries: {len(keeps)} tests narrowed to {summary} entries")

    def select(self, commits):
        if self.entries is None:
            return super().select(commits)
        orig_analyze = impact_selector.analyze_commit
        orig_spawn = impact_selector.spawn_child
        orig_start, orig_stop = runner._Result.startTest, runner._Result.stopTest
        keeps = self._keeps
        captures = self._captures

        def analyze_commit(ctx, cid, text):
            captures.clear()
            a = orig_analyze(ctx, cid, text)
            try:
                self._narrow(a, text)
            except Exception as e:  # noqa: BLE001 - never lose a commit to a narrowing bug
                keeps.pop(cid, None)
                print(f"{cid}: fixture entry narrowing failed: {e!r}", file=sys.stderr, flush=True)
            captures.clear()
            return a

        def spawn_child(plan, tests, ids, limits):
            global _ACTIVE
            _ACTIVE = keeps.get(plan.cid, {})
            try:
                return orig_spawn(plan, tests, ids, limits)
            finally:
                _ACTIVE = {}

        def start_test(result, test):
            global _CURRENT
            orig_start(result, test)
            keep = _ACTIVE.get(result._key(test))
            if keep is not None:
                _install_filter(sys.modules[type(test).__module__])
            _CURRENT = keep

        def stop_test(result, test):
            global _CURRENT
            _CURRENT = None
            orig_stop(result, test)

        impact_selector.analyze_commit = analyze_commit
        impact_selector.spawn_child = spawn_child
        runner._Result.startTest, runner._Result.stopTest = start_test, stop_test
        try:
            return super().select(commits)
        finally:
            impact_selector.analyze_commit = orig_analyze
            impact_selector.spawn_child = orig_spawn
            runner._Result.startTest, runner._Result.stopTest = orig_start, orig_stop
