"""Record an isolated, full-catalog comparison map on the pinned verifier image."""

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import gzip
from hashlib import sha256
import json
import marshal
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from .outcome_checkpoints import OutcomeCheckpoints, atomic_write, valid_report
from .selectors.outcome_format import OPS, site_id, trace_header
from .trace_operator_outcomes import rewrite_code


def index_sites(root, cache):
    indexed, skipped = {}, set()
    for path in sorted((root / "sqlglot").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        source = path.read_bytes()
        file_sites = {
            site_id(rel, node, i): [type(op).__name__, node.lineno, node.end_lineno]
            for node in ast.walk(ast.parse(source, filename=rel)) if isinstance(node, ast.Compare)
            for i, op in enumerate(node.ops) if type(op).__name__ in OPS
        }
        indexed.update(file_sites)
        try:
            code, excluded, collision = rewrite_code(source, str(path), rel, None)
            skipped.update(excluded)
            if collision:
                skipped.update(file_sites)
            if cache is not None:
                dest = cache / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(marshal.dumps((sha256(source).digest(), sys.flags.optimize, code, excluded, collision)))
        except (OSError, SyntaxError, ValueError):
            skipped.update(file_sites)
    return indexed, skipped


def record_test(root, node, result, env, timeout):
    started = time.monotonic()
    argv = [sys.executable, "-m", "strategies.trace_operator_outcomes", str(root), node, str(result)]
    report, reason = None, "trace did not produce a complete report"
    try:
        with subprocess.Popen(argv, cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True) as child:
            try:
                code = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                reason = f"trace process timeout ({timeout:.2f}s)"
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                code = None
        if code == 0 and result.is_file():
            report = json.loads(result.read_text())
            if not valid_report(report):
                return None, False, f"{time.monotonic() - started:.2f}s invalid trace report"
        elif code is not None:
            reason = f"trace exited with status {code}"
    except (OSError, ValueError) as error:
        return None, False, f"{time.monotonic() - started:.2f}s trace error: {error}"
    capped = result.with_suffix(".capped").exists() or any(result.parent.glob(f"{result.stem}.worker-*.capped"))
    seconds = time.monotonic() - started
    state = ("capped" if capped else "complete" if report and report["complete"] else f"unknown ({reason})")
    return (report, capped), True, f"{state} {seconds:.2f}s"


def collect(root, tests, args, checkpoints):
    deadline = time.monotonic() + (args.budget_seconds or math.inf)
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        cache = directory / "bytecode" if args.cache_rewrites else None
        indexed, skipped = index_sites(root, cache)
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                   PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0", PYTHONHASHSEED="0",
                   OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1",
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHON_CPU_COUNT="2",
                   BR_OUTCOME_COMPACT="1" if args.compact_records else "0",
                   BR_OUTCOME_CODE_CACHE=str(cache) if cache is not None else "",
                   BR_OUTCOME_ENCOUNTER_BUDGET=str(args.encounter_budget),
                   BR_OUTCOME_FAST_BUILTINS="1" if args.fast_builtins else "0",
                   BR_OUTCOME_FAST_PROFILE="1" if args.fast_profile else "0")

        def run(test):
            number, node = test["number"], test["id"]
            saved = checkpoints.get(number, node)
            if saved is not None:
                report, capped, source = saved
                print(f"RWX_OUTCOME_PROGRESS {number} reused-{source}", file=sys.stderr, flush=True)
                return report, capped
            if time.monotonic() >= deadline:
                return None
            terminal, finished, detail = record_test(root, node, directory / f"test-{number}.json",
                                                     env, args.timeout_seconds)
            if finished:
                checkpoints.put(number, node, *terminal)
            print(f"RWX_OUTCOME_PROGRESS {number} {'' if finished else 'retry '}{detail}",
                  file=sys.stderr, flush=True)
            return terminal

        complete, capped_tests, import_sites, rows = [], [], set(), {}
        accounted = 0
        with ThreadPoolExecutor(args.jobs) as pool:
            for test, terminal in zip(tests, pool.map(run, tests)):
                if terminal is None:
                    continue
                accounted += 1
                number = test["number"]
                report, capped = terminal
                if capped:
                    capped_tests.append(number)
                if report is None:
                    continue
                import_sites.update(set(report["import_sites"]) & indexed.keys())
                if capped or not report["complete"]:
                    continue
                complete.append(number)
                skipped.update(report["skipped"])
                for site, values in report["sites"].items():
                    if site in indexed:
                        rows.setdefault(site, {})[number] = (None if values is None else
                                                            [json.loads(value) for value in values])
    return accounted, {
        "complete": complete, "skipped": sorted(skipped), "sites": indexed, "rows": rows,
        "compact": args.compact_records, "selected": sorted(test["number"] for test in tests),
        "committed": True, "workers_mode": "isolated", "import_sites": sorted(import_sites),
        "capped": sorted(capped_tests),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("/opt/sg"))
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkpoint-dir", type=Path, required=True)
    parser.add_argument("--resume-from", type=Path, help="seed checkpoints from verified 5fa9aa5 raw reports")
    parser.add_argument("--committed-map", action="store_true", required=True)
    parser.add_argument("--all-sites", action="store_true", required=True)
    parser.add_argument("--workers-mode", choices=("isolated",), default="isolated")
    parser.add_argument("--compact-records", action="store_true")
    parser.add_argument("--cache-rewrites", action="store_true")
    parser.add_argument("--fast-builtins", action="store_true")
    parser.add_argument("--fast-profile", action="store_true")
    parser.add_argument("--encounter-budget", type=int, default=0)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--budget-seconds", type=float, default=0, help="0 means no overall deadline")
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args()
    if args.jobs < 1 or args.encounter_budget < 0:
        parser.error("jobs must be positive and the encounter budget must be nonnegative")
    if args.fast_builtins and not args.compact_records:
        parser.error("--fast-builtins requires --compact-records")
    if (not math.isfinite(args.budget_seconds) or args.budget_seconds < 0
            or not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0):
        parser.error("the budget must be finite and nonnegative and the per-test timeout finite and positive")
    if args.output.resolve().is_relative_to(args.checkpoint_dir.resolve()):
        parser.error("final output must not be inside the checkpoint directory")
    started = time.monotonic()
    root = args.source_root.resolve()
    tests = json.loads(args.catalog.read_text())["tests"]
    if not tests or len({test["number"] for test in tests}) != len(tests):
        parser.error("the catalog must contain tests with unique numbers")
    header = trace_header(root, tests)
    if not header["sqlglot_revision"]:
        parser.error("a source revision marker from the pinned Docker image is required")
    settings = {key: getattr(args, key) for key in (
        "workers_mode", "all_sites", "timeout_seconds", "encounter_budget",
        "compact_records", "cache_rewrites", "fast_builtins", "fast_profile")}
    try:
        checkpoints = OutcomeCheckpoints(args.checkpoint_dir, header, settings, args.resume_from)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    tests.sort(key=lambda test: (-test["seconds"], test["number"]))
    accounted, data = collect(root, tests, args, checkpoints)
    if accounted != len(tests):
        parser.error(f"{len(tests) - accounted} tests still need recording; resume with the same --checkpoint-dir")
    if header != trace_header(root, json.loads(args.catalog.read_text())["tests"]):
        parser.error("committed map source or catalog changed during recording")
    sites = len(data["sites"])
    capped = data["capped"]
    blocked = set(data["skipped"]) | set(data["import_sites"])
    observed = sum(site not in blocked and any(by_test.values()) for site, by_test in data["rows"].items())
    print(f"Encounter cap: {len(capped)} tests unknown at all {sites} indexed sites "
          f"({len(capped) * sites} test/site pairs): {capped}")
    print(f"Isolated coverage: {observed}/{sites} sites with usable observed rows; "
          f"{len(data['import_sites'])} import/collection sites blocked")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"header": header, **data}, separators=(",", ":")).encode()
    atomic_write(args.output, gzip.compress(payload, mtime=0), 0o644)
    print(f"Comparison map: {len(data['complete'])}/{len(tests)} complete, "
          f"{len(tests) - len(data['complete'])} unknown, {sites} sites, "
          f"{args.output.stat().st_size} bytes, {time.monotonic() - started:.2f} s")


if __name__ == "__main__":
    main()
