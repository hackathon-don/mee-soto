"""Score all registered experiments, keeping release decisions on the CI host."""

import argparse
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import re
import subprocess
import sys

from decimal import Decimal

if __package__:
    from .ci_score import (DiagnosticRun, Score, experiment_names, median_run, print_experiment,
                           print_selection_errors, score_with_schedule)
    from .ci_selection_report import FalseChecks, FalseScheduling
else:
    from ci_score import (DiagnosticRun, Score, experiment_names, median_run, print_experiment,
                          print_selection_errors, score_with_schedule)
    from ci_selection_report import FalseChecks, FalseScheduling


REPEATS = 3
BASELINE = "baseline"


def registered_at(commit):
    names = experiment_names(commit)
    if (len(names) != len(set(names)) or BASELINE not in names
            or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) for name in names)):
        raise ValueError("config.py must contain unique safe experiment names and a baseline")
    return names


def collect(names, run_score=score_with_schedule):
    results = {}
    for name in names:
        rows = []
        for i in range(REPEATS):
            print(f"Benchmarking {name}: {i + 1}/{REPEATS}", file=sys.stderr)
            try:
                rows.append(run_score(name))
            except (OSError, ValueError, subprocess.CalledProcessError):
                # Logs show the underlying problem, not untrusted/raw inputs in the artifact.
                print(f"Run failed for {name}: {i + 1}/{REPEATS}", file=sys.stderr)
                rows.append(None)
        results[name] = rows
    return results


def completed(rows):
    return len(rows) == REPEATS and all(row is not None for row in rows)


def rank(results):
    """Rank with exact Decimal scores, not the displayed rounded score."""
    eligible = [
        (median_run(rows).score.total, name)
        for name, rows in results.items()
        if name != BASELINE and completed(rows) and all(row.score.missed == 0 for row in rows)
    ]
    return [name for _, name in sorted(eligible)]


def score_record(run):
    if run is None:
        return {"status": "failed"}
    score = run.score
    # Decimal strings preserve all reported digits without float round trips.
    return {
        "status": "completed",
        "simulated_seconds": str(score.simulated),
        "cpu_seconds": str(score.cpu),
        "missed_patches": score.missed,
        "total_seconds": str(score.total),
    }


def make_record(commit, results, *, jobs_succeeded=True, map_digests=None):
    """A batch is complete only if every job succeeded and all experiments used one file map."""
    digests = set((map_digests or {}).values())
    same_map = map_digests is None or (len(digests) == 1 and map_digests.keys() == results.keys())
    all_complete = (jobs_succeeded and same_map and BASELINE in results
                    and all(completed(rows) for rows in results.values()))
    ranking = rank(results)
    experiments = {}
    for name, rows in results.items():
        middle = median_run(rows) if completed(rows) else None
        experiments[name] = {
            "runs": [score_record(run) for run in rows],
            "median_total_seconds": str(middle.score.total) if middle else None,
            "eligible": name in ranking,
        }
    return {
        "schema": 1,
        "commit": commit,
        "machine": "linux/amd64",
        "sample": "public",
        "repeats": REPEATS,
        "complete": all_complete,
        "jobs_succeeded": jobs_succeeded,
        "same_map": same_map,
        "map_digest": next(iter(digests)) if same_map and digests else None,
        "baseline": BASELINE,
        "ranking": ranking,
        "winner": ranking[0] if all_complete and ranking else None,
        "experiments": experiments,
    }


def render_scores(name, rows, baseline):
    if name != BASELINE and completed(rows) and baseline is not None:
        print_experiment(name, [row.score for row in rows], baseline)
        return
    print(f'Experiment: "{name}"  ')
    if completed(rows):
        print(f"Score: {median_run(rows).score.total:.1f} s (median)\n")
    else:
        print("Score: unavailable (incomplete runs)\n")
    print("| Run | Simulated | CPU | Missed | Total |")
    print("| --- | --- | --- | --- | --- |")
    for i, run in enumerate(rows, 1):
        if run is None:
            print(f"| {i} of {REPEATS} | — | — | — | unavailable |")
            continue
        s = run.score
        print(f"| {i} of {REPEATS} | {s.simulated:.2f} s | {s.cpu:.2f} s | {s.missed} | {s.total:.2f} s |")


def render_markdown(record, results):
    output = StringIO()
    with redirect_stdout(output):
        print("# RWX public-sample benchmark")
        print()
        print("Machine: linux/amd64  ")
        print(f"Commit hash: {record['commit']}")
        print()
        print("## Result")
        if record["winner"] is not None:
            winner = record["winner"]
            median = record["experiments"][winner]["median_total_seconds"]
            print(f"Winner: `{winner}` with an unrounded median total of {median} s.")
        elif not record["jobs_succeeded"]:
            print("No release: an experiment job failed in the latest attempt.")
        elif not record["same_map"]:
            print("No release: experiments were measured with different file maps.")
        elif not record["complete"]:
            print("No release: the benchmark batch is incomplete.")
        else:
            print("No release: no non-baseline experiment completed three zero-miss runs.")
        print()
        print("Ranking excludes the baseline and any experiment with a missing run or a miss.")
        print()
        for i, name in enumerate(record["ranking"], 1):
            print(f"{i}. `{name}` ({record['experiments'][name]['median_total_seconds']} s)")
        if record["ranking"]:
            print()
        base_rows = results.get(BASELINE, [])
        base = median_run(base_rows).score if completed(base_rows) else None
        names = [BASELINE, *(n for n in results if n != BASELINE)]
        for name in names:
            if name not in results:
                continue
            print("## Baseline reference" if name == BASELINE else f"## {name}")
            print()
            rows = results[name]
            render_scores(name, rows, base)
            print()
            if completed(rows):
                run = median_run(rows)
                print_selection_errors(run)
            else:
                print("Median-run selection diagnostics unavailable.")
            print()
    return output.getvalue()


def trial_record(run):
    if run is None:
        return None
    s = run.score
    schedule = None if run.schedule is None else {p: sorted(t) for p, t in run.schedule.items()}
    w = run.scheduling
    scheduling = None if w is None else {"false": w.false, "total": w.total, "simulated_ms": w.simulated_ms}
    return {"simulated": str(s.simulated), "cpu": str(s.cpu), "missed": s.missed, "schedule": schedule,
            "scheduling": scheduling}


def checks_record(checks):
    return None if checks is None else {"false": checks.false, "total": checks.total, "cpu": str(checks.cpu)}


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid count")
    return value


def load_checks(row):
    if row is None:
        return None
    cpu = Decimal(row["cpu"])
    if not cpu.is_finite() or cpu < 0 or _count(row["false"]) > _count(row["total"]):
        raise ValueError("invalid false checks")
    return FalseChecks(row["false"], row["total"], cpu)


def load_scheduling(row):
    if row is None:
        return None
    if _count(row["false"]) > _count(row["total"]) or type(row["simulated_ms"]) is not int:
        raise ValueError("invalid false scheduling")
    return FalseScheduling(row["false"], row["total"], row["simulated_ms"])


def diagnostic_checks(name, run_score=score_with_schedule):
    """One extra unscored run with CPU diagnostics, so logging overhead stays out of the ranking."""
    print(f"Diagnostics run for {name}", file=sys.stderr)
    try:
        return run_score(name, cpu_diagnostics=True).checks
    except (OSError, ValueError, subprocess.CalledProcessError):
        print(f"Diagnostics run failed for {name}", file=sys.stderr)
        return None


def load_trial(row):
    if row is None:
        return None
    simulated, cpu, missed = Decimal(row["simulated"]), Decimal(row["cpu"]), row["missed"]
    if (not simulated.is_finite() or not cpu.is_finite() or simulated < 0 or cpu < 0
            or type(missed) is not int or missed < 0):
        raise ValueError("invalid trial score")
    schedule = row["schedule"]
    if schedule is not None:
        if any(not re.fullmatch(r"[a-zA-Z0-9_-]+", p) or any(type(t) is not int for t in tests)
               for p, tests in schedule.items()):
            raise ValueError("invalid trial schedule")
        schedule = {p: set(tests) for p, tests in schedule.items()}
    return DiagnosticRun(Score(simulated, cpu, missed), None, schedule,
                         scheduling=load_scheduling(row.get("scheduling")))


def load_trials(directory, names):
    """Keep each experiment's latest attempt; a missing or invalid file counts as failed runs."""
    latest = {}
    for path in sorted(directory.rglob("*.json")):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            name, attempt = obj["experiment"], obj["attempt"]
            if name not in names or type(attempt) is not int:
                continue
            checks = load_checks(obj.get("false_checks"))
            rows = [None if row is None else replace(row, checks=checks)
                    for row in map(load_trial, obj["runs"])]
            digest = obj.get("map_digest")
            if len(rows) != REPEATS or not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
                continue
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        if name not in latest or attempt > latest[name][0]:
            latest[name] = (attempt, rows, digest)
    results = {name: latest[name][1] if name in latest else [None] * REPEATS for name in names}
    return results, {name: latest[name][2] for name in names if name in latest}


def checked_commit(parser, expected):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", expected) or commit != expected:
        parser.error("the checkout does not match the measured full SHA")
    return commit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="print registered experiment names as JSON")
    measure = commands.add_parser("measure", help="run one experiment's trials")
    measure.add_argument("--experiment", required=True)
    measure.add_argument("--attempt", type=int, required=True)
    measure.add_argument("--output", type=Path, required=True)
    report = commands.add_parser("report", help="combine trial files into the reports")
    report.add_argument("--trials-dir", type=Path, required=True)
    report.add_argument("--output-dir", type=Path, required=True)
    report.add_argument("--jobs-failed", action="store_true",
                        help="an experiment job failed in the latest attempt, so nothing can release")
    args = parser.parse_args()
    commit = checked_commit(parser, args.expected_sha)
    names = registered_at(commit)
    if args.command == "list":
        print(json.dumps(names))
    elif args.command == "measure":
        if args.experiment not in names:
            parser.error("experiment is not registered at the measured commit")
        rows = collect([args.experiment])[args.experiment]
        checks = diagnostic_checks(args.experiment)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({
            "experiment": args.experiment, "attempt": args.attempt,
            "runs": [trial_record(run) for run in rows],
            "false_checks": checks_record(checks),
        }) + "\n", encoding="utf-8")
    else:
        results, digests = load_trials(args.trials_dir, names)
        record = make_record(commit, results, jobs_succeeded=not args.jobs_failed, map_digests=digests)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "results.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        (args.output_dir / "report.md").write_text(render_markdown(record, results), encoding="utf-8")


if __name__ == "__main__":
    main()
