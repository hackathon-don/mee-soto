"""Build a comparison trace map on the exact Linux verifier image."""

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from .selectors.map_format import catalog_records, catalog_sha256
from .selectors.outcome_format import OPS, identity, site_id, tree_digest


def sites(root, requested):
    found = {}
    for path in sorted((root / "sqlglot").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(ast.parse(path.read_bytes(), filename=rel)):
            if isinstance(node, ast.Compare):
                for i, op in enumerate(node.ops):
                    if type(op).__name__ in OPS:
                        key = site_id(rel, node, i)
                        found[key] = [type(op).__name__, node.lineno, node.end_lineno]
    if requested is None:
        return found
    selected = {}
    for target in requested:
        matches = ([target] if target in found else
                   [key for key, (_, lo, hi) in found.items()
                    if any(target == f"{key.split(':', 1)[0]}:{line}" for line in range(lo, hi + 1))])
        if not matches:
            raise ValueError(f"unknown site: {target}")
        selected.update((key, found[key]) for key in matches)
    return selected


def collect(root, tests, requested, indexed, jobs, budget, timeout):
    deadline = time.monotonic() + budget
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0", PYTHONHASHSEED="0",
               PYTHON_CPU_COUNT="2", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
               MKL_NUM_THREADS="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        flags = []
        if requested is not None:
            targets = directory / "sites.json"
            targets.write_text(json.dumps(sorted(indexed)))
            flags = [str(targets)]

        def run(test):
            if time.monotonic() >= deadline:
                return test["number"], None
            result = directory / f"{test['number']}.json"
            argv = [sys.executable, "-m", "strategies.trace_operator_outcomes",
                    str(root), test["id"], str(result), *flags]
            try:
                with subprocess.Popen(argv, cwd=root, env=env, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True) as child:
                    try:
                        code = child.wait(timeout=max(0, min(deadline - time.monotonic(), timeout)))
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        return test["number"], None
                if code == 0:
                    return test["number"], json.loads(result.read_text())
            except (OSError, ValueError):
                pass
            return test["number"], None

        completed, skipped, rows = [], set(), {}
        with ThreadPoolExecutor(jobs) as pool:
            for number, report in pool.map(run, tests):
                if report is None:
                    continue
                completed.append(number)
                skipped.update(report["skipped"])
                for key, values in report["sites"].items():
                    if key in indexed:
                        rows.setdefault(key, {})[number] = (
                            None if values is None else [json.loads(value) for value in values])
    return completed, sorted(skipped), rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("/opt/sg"))
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--site", action="append", help="path:line, or exact path:line:column:link")
    scope.add_argument("--all-sites", action="store_true")
    parser.add_argument("--test-number", type=int, action="append")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--budget-seconds", type=float, default=540)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args()
    start = time.monotonic()
    root = args.source_root.resolve()
    catalog = json.loads(args.catalog.read_text())["tests"]
    tests = [test for test in catalog if not args.test_number or test["number"] in args.test_number]
    tests.sort(key=lambda test: -test["seconds"])
    if not tests:
        parser.error("no catalog tests selected")
    indexed = sites(root, args.site)
    completed, skipped, rows = collect(root, tests, args.site, indexed, args.jobs,
                                       args.budget_seconds, args.timeout_seconds)
    payload = {"identity": identity(), "tree": tree_digest(root),
               "catalog": catalog_sha256(catalog_records(catalog)), "complete": completed,
               "skipped": skipped, "sites": indexed, "rows": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), mtime=0))
    print(f"Comparison map: {len(completed)}/{len(tests)} complete, "
          f"{len(tests) - len(completed)} unknown, {len(indexed)} sites "
          f"({len(skipped)} excluded by the rewriter), {args.output.stat().st_size} bytes, "
          f"{time.monotonic() - start:.2f} s")


if __name__ == "__main__":
    main()
