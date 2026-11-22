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


def collect(root, tests, requested, indexed, jobs, budget, timeout, batches):
    deadline = time.monotonic() + budget
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0", PYTHONHASHSEED="0",
               OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
               MKL_NUM_THREADS="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        flags = []
        if requested is not None:
            targets = directory / "sites.json"
            targets.write_text(json.dumps(sorted(indexed)))
            flags = [str(targets)]

        work = []
        for index, batch in enumerate(batches):
            manifest = directory / f"batch-{index}-input.json"
            manifest.write_text(json.dumps(batch))
            work.append(("batch", index, str(manifest)))
        work.extend(("test", test["number"], test["id"]) for test in tests)

        def run(item):
            mode, key, target = item
            report = None
            if time.monotonic() >= deadline:
                return mode, key, report
            result = directory / f"{mode}-{key}.json"
            argv = [sys.executable, "-m", "strategies.trace_operator_outcomes",
                    mode, str(root), target, str(result), *flags]
            try:
                trace_env = env if mode == "batch" else dict(env, PYTHON_CPU_COUNT="2")
                with subprocess.Popen(argv, cwd=root, env=trace_env, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True) as child:
                    try:
                        code = child.wait(timeout=max(0, min(deadline - time.monotonic(), timeout)))
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        return mode, key, report
                if code == 0:
                    report = json.loads(result.read_text())
            except (OSError, ValueError):
                pass
            return mode, key, report

        completed, skipped, rows, verified = [], set(), {}, []
        with ThreadPoolExecutor(jobs) as pool:
            for mode, number, report in pool.map(run, work):
                if report is None:
                    continue
                skipped.update(report["skipped"])
                observed = {key: None if values is None else [json.loads(value) for value in values]
                            for key, values in report["sites"].items() if key in indexed}
                if mode == "batch":
                    verified.append({**batches[number], "rows": observed})
                else:
                    completed.append(number)
                    for key, values in observed.items():
                        rows.setdefault(key, {})[number] = values
    return completed, sorted(skipped), rows, verified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("/opt/sg"))
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--site", action="append", help="path:line, or exact path:line:column:link")
    scope.add_argument("--all-sites", action="store_true")
    parser.add_argument("--test-number", type=int, action="append")
    parser.add_argument("--batch-file", type=Path, help="ordered verification batches from a shadow run")
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
    batches = [] if args.batch_file is None else json.loads(args.batch_file.read_text())
    ids = {test["number"]: test["id"] for test in catalog}
    for batch in batches:
        cases = batch["tests"]
        if (not cases or any(ids.get(number) != node or type(limit) not in (int, float)
                             or not 0 < limit < float("inf") for number, node, limit in cases)
                or len({case[0] for case in cases}) != len(cases)
                or any(type(module) is not str for module in batch["imports"])):
            parser.error("invalid verification batch")
    completed, skipped, rows, verified = collect(root, tests, args.site, indexed, args.jobs,
                                                 args.budget_seconds, args.timeout_seconds, batches)
    payload = {"identity": identity(), "tree": tree_digest(root),
               "catalog": catalog_sha256(catalog_records(catalog)), "complete": completed,
               "skipped": skipped, "sites": indexed, "rows": rows, "batches": verified}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), mtime=0))
    print(f"Comparison map: {len(completed)}/{len(tests)} complete, "
          f"{len(tests) - len(completed)} unknown, {len(indexed)} sites "
          f"({len(skipped)} excluded by the rewriter), {len(verified)}/{len(batches)} batches complete, "
          f"{args.output.stat().st_size} bytes, "
          f"{time.monotonic() - start:.2f} s")


if __name__ == "__main__":
    main()
