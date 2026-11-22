"""Build the condition operand map on the exact Linux verifier image.

Each catalog test runs alone in a fresh recorder process, once per hash seed,
because the scored verifier runs with hash randomization. The map keeps, per
condition site, the set of completely traced tests that evaluated it under any
seed, as a base64 little-endian bitset over catalog test numbers. The header
fingerprints the interpreter, SQLGlot revision, catalog and source tree, so a
stale map is ignored by the selector.
"""

import argparse
import ast
import base64
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

from . import trace_operator_outcomes as base
from .build_operator_outcomes import build_code_cache
from .selectors.outcome_format import trace_header
from .trace_condition_operands import CONDITION_TRACER_VERSION, ConditionRewrite, condition_sites


def all_sites(root):
    found, duplicate = {}, set()
    for path in sorted((root / "sqlglot").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        sites, dup = condition_sites(rel, ast.parse(path.read_bytes(), filename=rel))
        found.update(sites)
        duplicate.update(dup)
    return found, duplicate


def run_test(root, directory, test, timeout, env, seed):
    env = dict(env, PYTHONHASHSEED=str(seed))
    result = directory / f"test-{test['number']}-{seed}.json"
    argv = [sys.executable, "-m", "strategies.trace_condition_operands", str(root), test["id"], str(result)]
    with subprocess.Popen(argv, cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          start_new_session=True) as child:
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            code = None
        finally:
            if child.returncode != 0:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    capped = result.with_suffix(".capped").exists()
    report = None
    if code == 0 and result.is_file():
        try:
            report = json.loads(result.read_text())
        except ValueError:
            report = None
    result.unlink(missing_ok=True)
    return test["number"], report, capped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("/opt/sg"))
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test-number", type=int, action="append", help="record a subset (never committed)")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--hash-seed", type=int, action="append", help="default: 0 and 1")
    parser.add_argument("--encounter-budget", type=int, default=2_000_000,
                        help="mark a test unknown above this many condition evaluations")
    args = parser.parse_args()
    start = time.monotonic()
    root = args.source_root.resolve()
    catalog = json.loads(args.catalog.read_text())["tests"]
    header = trace_header(root, catalog)
    committed = not args.test_number
    if committed and not header["sqlglot_revision"]:
        parser.error("a committed map requires the source revision marker from the pinned Docker image")
    tests = [test for test in catalog if not args.test_number or test["number"] in args.test_number]
    tests.sort(key=lambda test: (-test["seconds"], test["number"]))
    seeds = args.hash_seed or [0, 1]
    sites, duplicate = all_sites(root)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0",
               OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1",
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHON_CPU_COUNT="2",
               BR_OUTCOME_ENCOUNTER_BUDGET=str(args.encounter_budget), BR_OUTCOME_COMPACT="0",
               BR_OUTCOME_PROFILE="", BR_OUTCOME_FAST_BUILTINS="0", BR_OUTCOME_FAST_PROFILE="1")
    seen, complete, capped, unknown, import_sites, skipped = {}, [], [], [], set(), set(duplicate)
    seed_differences = 0
    with tempfile.TemporaryDirectory() as temp, ThreadPoolExecutor(args.jobs) as pool:
        directory = Path(temp)
        # Compile each rewritten module once; recorder processes reuse it when the source digest matches.
        base.Rewrite = ConditionRewrite
        skipped.update(build_code_cache(root, directory / "bytecode", None, sites))
        env["BR_OUTCOME_CODE_CACHE"] = str(directory / "bytecode")
        jobs = [[pool.submit(run_test, root, directory, test, args.timeout_seconds, env, seed) for seed in seeds]
                for test in tests]
        for runs in jobs:
            results = [job.result() for job in runs]
            number = results[0][0]
            for _, report, _ in results:
                if report is not None:
                    import_sites.update(report["import_sites"])
            if any(was_capped for _, _, was_capped in results):
                capped.append(number)
            if any(report is None or not report["complete"] or was_capped for _, report, was_capped in results):
                unknown.append(number)
                continue
            complete.append(number)
            per_seed = []
            for _, report, _ in results:
                skipped.update(report["skipped"])
                per_seed.append(set(report["sites"]))
            union = set().union(*per_seed)
            seed_differences += len(union) - len(set.intersection(*per_seed))
            for key in union:
                seen[key] = seen.get(key, 0) | 1 << number
    if header != trace_header(root, json.loads(args.catalog.read_text())["tests"]):
        parser.error("source or catalog changed during recording")
    payload = {"header": header, "condition_tracer_version": CONDITION_TRACER_VERSION,
               "committed": committed, "selected": sorted(test["number"] for test in tests),
               "complete": sorted(complete), "capped": sorted(capped),
               "import_sites": sorted(import_sites), "skipped": sorted(skipped),
               "hash_seeds": seeds, "site_count": len(sites),
               "encountered": {key: base64.b64encode(bits.to_bytes((bits.bit_length() + 7) // 8, "little")).decode()
                               for key, bits in sorted(seen.items())}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(),
                                          mtime=0))
    print(f"Condition map: {len(complete)}/{len(tests)} complete, {len(capped)} capped, "
          f"{len(unknown)} unknown, {len(sites)} sites ({len(skipped)} skipped, {len(import_sites)} import-time), "
          f"{len(seen)} encountered, {seed_differences} test/site pairs differ across seeds, "
          f"{args.output.stat().st_size} bytes, "
          f"{time.monotonic() - start:.1f} s")


if __name__ == "__main__":
    main()
