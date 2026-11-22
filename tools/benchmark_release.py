"""Score all registered experiments, keeping release decisions on the CI host."""

import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import re
import subprocess
import sys

if __package__:
    from .ci_score import experiment_names, median_run, print_experiment, print_selection_errors, score_with_schedule
else:
    from ci_score import experiment_names, median_run, print_experiment, print_selection_errors, score_with_schedule


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


def make_record(commit, results):
    all_complete = BASELINE in results and all(completed(rows) for rows in results.values())
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_sha) or commit != args.expected_sha:
        parser.error("the checkout does not match the measured full SHA")
    results = collect(registered_at(commit))
    record = make_record(commit, results)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "results.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    (args.output_dir / "report.md").write_text(render_markdown(record, results), encoding="utf-8")


if __name__ == "__main__":
    main()
