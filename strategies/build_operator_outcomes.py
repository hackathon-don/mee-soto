"""Build a comparison trace map on the exact Linux verifier image."""

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
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
from .trace_operator_outcomes import BATCH_REJECTION, CAP, rewrite_code


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


def build_code_cache(root, cache, targets, indexed):
    paths = (sorted((root / "sqlglot").rglob("*.py")) if targets is None else
             [root / rel for rel in sorted({site.split(":", 1)[0] for site in targets})])
    skipped_sites = set()
    for path in paths:
        rel = path.relative_to(root).as_posix()
        try:
            source = path.read_bytes()
            code, skipped, collision = rewrite_code(source, str(path), rel, targets)
            skipped_sites.update(skipped)
            if collision:
                skipped_sites.update(site for site in indexed if site.split(":", 1)[0] == rel)
            if cache is not None:
                dest = cache / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(marshal.dumps((sha256(source).digest(), sys.flags.optimize, code, skipped, collision)))
        except (OSError, SyntaxError, ValueError):
            skipped_sites.update(site for site in indexed if site.split(":", 1)[0] == rel)
    return skipped_sites


def cgroup_cpu_seconds():
    try:
        stat = Path("/sys/fs/cgroup/cpu.stat").read_text()
        for line in stat.splitlines():
            if line.startswith("usage_usec "):
                return int(line.split()[1]) / 1_000_000
    except (OSError, ValueError):
        pass
    return None


def collect(root, tests, requested, indexed, jobs, budget, timeout, batches, compact, cache_code, profile,
            persistent, fast_builtins, encounter_budget, fast_profile, checkpoints=None):
    deadline = time.monotonic() + budget
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0", PYTHONHASHSEED="0",
               OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
               MKL_NUM_THREADS="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    env.update(BR_OUTCOME_COMPACT="1" if compact else "0", BR_OUTCOME_PROFILE=profile or "",
               BR_OUTCOME_CODE_CACHE="", BR_OUTCOME_FAST_BUILTINS="1" if fast_builtins else "0",
               BR_OUTCOME_ENCOUNTER_BUDGET=str(encounter_budget),
               BR_OUTCOME_FAST_PROFILE="1" if fast_profile else "0")
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        targets = None if requested is None else set(indexed)
        flags = []
        if targets is not None:
            sites_path = directory / "sites.json"
            sites_path.write_text(json.dumps(sorted(targets)))
            flags = [str(sites_path)]
        cache = directory / "bytecode" if cache_code else None
        statically_skipped = (build_code_cache(root, cache, targets, indexed)
                              if cache_code or not (persistent or profile) else set())
        if cache is not None:
            env["BR_OUTCOME_CODE_CACHE"] = str(cache)

        work = []
        for index, batch in enumerate(batches):
            manifest = directory / f"batch-{index}-input.json"
            manifest.write_text(json.dumps(batch))
            work.append(("batch", index, str(manifest)))
        if persistent:
            shards = [[] for _ in range(min(jobs, len(tests)))]
            for index, test in enumerate(tests):
                shards[index % len(shards)].append((test["number"], test["id"]))
            work = [("shard", index, sorted(shard, key=lambda case: case[1]))
                    for index, shard in enumerate(shards)] + work
        else:
            work.extend(("test", test["number"], test["id"]) for test in tests)

        def run_shard(index, cases):
            started = time.monotonic()
            attempted, restart = 0, 0
            stalled = False
            while cases and time.monotonic() < deadline:
                base = directory / f"shard-{index}-{restart}"
                manifest, result = base.with_suffix(".input"), base.with_suffix(".json")
                manifest.write_text(json.dumps(cases))
                argv = [sys.executable, "-m", "strategies.trace_operator_persistent",
                        str(root), str(manifest), str(result), *flags]
                timed_out = False
                with subprocess.Popen(argv, cwd=root, env=dict(env, PYTHON_CPU_COUNT="2"),
                                      stdout=subprocess.DEVNULL, start_new_session=True) as child:
                    try:
                        code = child.wait(timeout=max(0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        timed_out, code = True, None
                    finally:
                        if child.returncode != 0:
                            try:
                                os.killpg(child.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                progress = result.with_suffix(".started")
                seen = progress.read_text().splitlines() if progress.is_file() else []
                if seen != [str(number) for number, _ in cases[:len(seen)]]:
                    break
                attempted += len(seen)
                if code == 0 and result.is_file() and len(seen) == len(cases):
                    try:
                        report = json.loads(result.read_text())
                    except (OSError, ValueError):
                        break
                    return "shard", attempted, report, time.monotonic() - started
                if code == 0 or timed_out or (not seen and stalled):
                    break
                print(f"Outcome shard {index}: worker exited with status {code}; "
                      f"{len(seen)} attempted tests remain unknown, restarting the unstarted suffix",
                      file=sys.stderr, flush=True)
                cases = cases[len(seen):]
                stalled = not seen
                restart += 1
            return "shard", attempted, None, time.monotonic() - started

        def run(item):
            mode, key, target = item
            if mode == "shard":
                return (*run_shard(key, target), False)
            started = time.monotonic()
            report = None
            batch = batches[key] if mode == "batch" else None

            def rejected(reason):
                name = batch.get("commit", f"#{key + 1}")
                print(f"Outcome batch {name} ({len(batch['tests'])} checks): {reason}",
                      file=sys.stderr, flush=True)

            if checkpoints is not None:
                saved = checkpoints.get(key, target)
                if saved is not None:
                    report, capped, source = saved
                    print(f"RWX_OUTCOME_PROGRESS {key} reused-{source}", file=sys.stderr, flush=True)
                    return mode, key, report, 0.0, capped
            if time.monotonic() >= deadline:
                if batch is not None:
                    rejected("build budget expired before batch started")
                return mode, key, report, None, False
            result = directory / f"{mode}-{key}.json"
            argv = [sys.executable, "-m", "strategies.trace_operator_outcomes",
                    mode, str(root), target, str(result), *flags]
            with (tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
                  if batch is not None else nullcontext(subprocess.DEVNULL)) as stderr:
                reason = "trace did not produce a complete report"
                retry = False
                try:
                    trace_env = env if batch is not None else dict(env, PYTHON_CPU_COUNT="2")
                    with subprocess.Popen(argv, cwd=root, env=trace_env, stdout=subprocess.DEVNULL,
                                          stderr=stderr, start_new_session=True) as child:
                        wait_started = time.monotonic()
                        remaining = deadline - wait_started
                        limit = timeout if checkpoints is not None else max(0, min(remaining, timeout))
                        try:
                            code = child.wait(timeout=limit)
                        except subprocess.TimeoutExpired:
                            kind = "remaining build budget" if limit < timeout else "trace process timeout"
                            reason = (f"trace process timed out after {time.monotonic() - wait_started:.2f}s "
                                      f"({limit:.2f}s {kind})")
                            try:
                                os.killpg(child.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            code = None
                    if code == 0 and result.is_file():
                        report = json.loads(result.read_text())
                    elif code not in (None, 0):
                        reason = f"trace exited with status {code}"
                except (OSError, ValueError) as error:
                    reason = f"trace error: {error}"
                    retry = True
                if batch is not None:
                    stderr.seek(0)
                    if report is None:
                        rejected(reason)
                    for line in stderr:
                        if line.startswith(BATCH_REJECTION):
                            rejected(line.removeprefix(BATCH_REJECTION).rstrip())
                        else:
                            print(line, file=sys.stderr, end="", flush=True)
            capped = mode == "test" and (result.with_suffix(".capped").exists()
                                         or any(directory.glob(f"{result.stem}.worker-*.capped")))
            seconds = time.monotonic() - started
            if checkpoints is not None:
                if retry or report is not None and not valid_report(report):
                    print(f"RWX_OUTCOME_PROGRESS {key} retry {seconds:.2f}s {reason}", file=sys.stderr, flush=True)
                    return mode, key, None, None, False
                checkpoints.put(key, target, report, capped)
                state = ("capped" if capped else "complete" if report and report["complete"]
                         else "unknown")
                detail = f" {reason}" if state == "unknown" else ""
                print(f"RWX_OUTCOME_PROGRESS {key} {state} {seconds:.2f}s{detail}",
                      file=sys.stderr, flush=True)
            return mode, key, report, seconds, capped

        completed, skipped, rows, verified, global_rows = [], set(), {}, [], {}
        import_sites, capped_tests = set(), []
        attempted, child_seconds = 0, 0.0
        with ThreadPoolExecutor(jobs) as pool:
            for mode, number, report, seconds, capped in pool.map(run, work):
                if mode == "shard":
                    attempted += number
                    child_seconds += seconds
                    if report is None:
                        continue
                    completed.extend(report["complete"])
                    if profile:
                        continue
                    skipped.update(report["skipped"])
                    for case, sites_seen in report["rows"].items():
                        for site, values in sites_seen.items():
                            if site in indexed:
                                rows.setdefault(site, {})[case] = (None if values is None else
                                                                    [json.loads(value) for value in values])
                    for site, values in report["global"].items():
                        if site not in indexed or global_rows.get(site, ()) is None:
                            continue
                        if values is None:
                            global_rows[site] = None
                        else:
                            seen = global_rows.setdefault(site, set())
                            seen.update(values)
                            if len(seen) > CAP:
                                global_rows[site] = None
                    continue
                if mode == "test" and seconds is not None:
                    attempted += 1
                    child_seconds += seconds
                if capped:
                    capped_tests.append(number)
                if report is None:
                    continue
                if profile:
                    completed.append(number)
                    continue
                if mode == "test":
                    import_sites.update(set(report["import_sites"]) & indexed.keys())
                    if capped or not report["complete"]:
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
    global_rows = {site: None if values is None else [json.loads(value) for value in sorted(values)]
                   for site, values in global_rows.items()}
    return (completed, sorted(skipped | statically_skipped), rows, verified, attempted, child_seconds, global_rows,
            sorted(import_sites), sorted(capped_tests))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("/opt/sg"))
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--committed-map", action="store_true",
                        help="require an all-site, full-catalog, patch-independent map")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--site", action="append", help="path:line, or exact path:line:column:link")
    scope.add_argument("--all-sites", action="store_true")
    parser.add_argument("--test-number", type=int, action="append")
    parser.add_argument("--batch-file", type=Path, help="ordered verification batches from a shadow run")
    parser.add_argument("--compact-records", action="store_true", help="dedupe type, identity and outcome vectors")
    parser.add_argument("--fast-builtins", action="store_true",
                        help="use prebuilt compact rows after evaluating exact builtin operands")
    parser.add_argument("--fast-profile", action="store_true",
                        help="prefilter known harmless builtin calls in the safety profiler")
    parser.add_argument("--encounter-budget", type=int, default=0,
                        help="mark a test unknown above this many comparisons in a recorder process (0: unlimited)")
    parser.add_argument("--cache-rewrites", action="store_true", help="compile each rewritten module once per build")
    parser.add_argument("--profile-mode", choices=("plain", "rewrite"),
                        help="time plain tests or rewritten tests without recording; never write a map")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--workers-mode", choices=("isolated", "persistent"), default="isolated")
    parser.add_argument("--budget-seconds", type=float, default=540,
                        help="recording budget; 0 means no suite deadline for checkpointed recording")
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--checkpoint-dir", type=Path, help="resume or persist isolated committed-map progress")
    parser.add_argument("--resume-from", type=Path, help="seed checkpoints from verified 5fa9aa5 raw reports")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    if args.fast_builtins and not args.compact_records:
        parser.error("--fast-builtins requires --compact-records")
    if args.encounter_budget < 0 or (args.encounter_budget and args.profile_mode):
        parser.error("--encounter-budget must be nonnegative and requires recording")
    if args.profile_mode:
        if args.output or args.batch_file or args.compact_records or (args.profile_mode == "plain" and args.cache_rewrites):
            parser.error("profile modes do not accept map output, batches or records; only rewrite mode can cache")
    elif args.output is None:
        parser.error("--output is required to build a map")
    if args.committed_map and (not args.all_sites or args.test_number or args.batch_file or args.profile_mode):
        parser.error("--committed-map requires all sites and tests, recording, and no patch batches")
    if ((args.checkpoint_dir or args.resume_from)
            and (not args.committed_map or args.workers_mode != "isolated")):
        parser.error("checkpointing requires isolated committed-map recording")
    if args.resume_from is not None and args.checkpoint_dir is None:
        parser.error("--resume-from requires --checkpoint-dir")
    if (not math.isfinite(args.budget_seconds) or args.budget_seconds < 0
            or args.budget_seconds == 0 and args.checkpoint_dir is None
            or not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0):
        parser.error("budgets and per-test timeouts must be positive and finite (checkpointed budget may be 0)")
    start = time.monotonic()
    cpu_start = cgroup_cpu_seconds()
    root = args.source_root.resolve()
    catalog = json.loads(args.catalog.read_text())["tests"]
    header = None if args.profile_mode else trace_header(root, catalog)
    if args.committed_map and not header["sqlglot_revision"]:
        parser.error("--committed-map requires a source revision marker from the pinned Docker image")
    checkpoints = None
    if args.checkpoint_dir is not None:
        if args.output.resolve().is_relative_to(args.checkpoint_dir.resolve()):
            parser.error("final output must not be inside the checkpoint directory")
        settings = {key: getattr(args, key) for key in (
            "workers_mode", "all_sites", "timeout_seconds", "encounter_budget",
            "compact_records", "cache_rewrites", "fast_builtins", "fast_profile")}
        try:
            checkpoints = OutcomeCheckpoints(args.checkpoint_dir, header, settings, args.resume_from)
        except (OSError, ValueError) as error:
            parser.error(str(error))
    tests = [test for test in catalog if not args.test_number or test["number"] in args.test_number]
    tests.sort(key=lambda test: (-test["seconds"], test["number"]))
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
    persistent = args.workers_mode == "persistent"
    completed, skipped, rows, verified, attempted, child_seconds, global_rows, import_sites, capped_tests = collect(
        root, tests, args.site, indexed, args.jobs, args.budget_seconds or math.inf, args.timeout_seconds,
        batches, args.compact_records, args.cache_rewrites, args.profile_mode, persistent, args.fast_builtins,
        args.encounter_budget, args.fast_profile, checkpoints)
    elapsed = time.monotonic() - start
    cpu_end = cgroup_cpu_seconds()
    cpu = cpu_end - cpu_start if cpu_start is not None and cpu_end is not None else None
    utilization = f"; cgroup CPU {cpu:.2f}s ({cpu / elapsed:.2f} cores average)" if cpu is not None else ""
    print(f"Trace work ({args.profile_mode or 'record'}): {attempted}/{len(tests)} tests attempted, "
          f"{len(completed)} complete, {child_seconds:.2f}s summed child wall, "
          f"{elapsed:.2f}s elapsed with {args.jobs} jobs{utilization}")
    if args.profile_mode:
        return
    if checkpoints is not None and attempted != len(tests):
        parser.error(f"{len(tests) - attempted} tests still need recording; resume with the same --checkpoint-dir")
    if args.committed_map and checkpoints is None and (attempted != len(tests) or elapsed >= args.budget_seconds):
        parser.error("committed map recording did not run the full catalog within the deadline")
    if args.committed_map and header != trace_header(root, json.loads(args.catalog.read_text())["tests"]):
        parser.error("committed map source or catalog changed during recording")
    payload = {"header": header, "complete": completed,
               "skipped": skipped, "sites": indexed, "rows": rows, "batches": [] if persistent else verified,
               "compact": args.compact_records, "selected": sorted(test["number"] for test in tests)}
    if args.committed_map:
        payload["committed"] = True
    if persistent:
        payload.update(workers_mode="persistent", global_rows=global_rows, shadow_batches=verified)
    else:
        payload.update(workers_mode="isolated", import_sites=import_sites, capped=capped_tests)
        print(f"Encounter cap: {len(capped_tests)} tests unknown at all {len(indexed)} indexed sites"
              f" ({len(capped_tests) * len(indexed)} test/site pairs): {capped_tests}")
        blocked = set(skipped) | set(import_sites)
        observed = sum(site not in blocked and any(by_test.values()) for site, by_test in rows.items())
        print(f"Isolated coverage: {observed}/{len(indexed)} sites with usable observed rows; "
              f"{len(import_sites)} import/collection sites blocked")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(args.output, gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), mtime=0), 0o644)
    print(f"Comparison map: {len(completed)}/{len(tests)} complete, "
          f"{len(tests) - len(completed)} unknown, {len(indexed)} sites "
          f"({len(skipped)} excluded by the rewriter), {len(verified)}/{len(batches)} batches complete, "
          f"{args.output.stat().st_size} bytes, "
          f"{time.monotonic() - start:.2f} s")


if __name__ == "__main__":
    main()
