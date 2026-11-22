"""Render the public-sample CI report without changing the scorer's CLI."""

import argparse
import ast
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import re
from statistics import median
import subprocess
import sys
from tempfile import TemporaryDirectory

if __package__:
    from .ci_selection_report import read_schedule, selection_errors
else:
    from ci_selection_report import read_schedule, selection_errors


def experiment_names(revision: str) -> list[str]:
    source = subprocess.check_output(["git", "show", f"{revision}:config.py"], text=True)
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        elif isinstance(statement, ast.Assign):
            targets = statement.targets
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "EXPERIMENTS" for target in targets):
            mapping = statement.value
            if isinstance(mapping, ast.Dict) and all(
                isinstance(key, ast.Constant) and isinstance(key.value, str) for key in mapping.keys
            ):
                return [key.value for key in mapping.keys]
            break
    raise ValueError(f"{revision}: config.py must define EXPERIMENTS as a dict with literal names")


def added_experiments(base: str) -> list[str]:
    if base == "0" * 40:
        base = subprocess.check_output(["git", "merge-base", "HEAD", "origin/main"], text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise ValueError("expected a full base commit hash")
    existing = set(experiment_names(base))
    added = dict.fromkeys(name for name in experiment_names("HEAD") if name not in existing)
    if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name) for name in added):
        raise ValueError("experiment names must be safe to pass to make e2e")
    return list(added)


@dataclass(frozen=True)
class Score:
    simulated: Decimal
    cpu: Decimal
    missed: int

    @property
    def total(self) -> Decimal:
        return self.simulated + self.cpu


def parse_score(output: str) -> Score:
    def read(label: str, pattern: str) -> str:
        matches = re.findall(rf"^{re.escape(label)}:[ \t]+({pattern})(?=[ \t]|$)", output, re.M)
        if len(matches) != 1:
            raise ValueError(f"expected one {label} value in make e2e output")
        return matches[0]

    seconds = r"[0-9]+\.[0-9]+(?= s\b)"
    return Score(
        Decimal(read("simulated total", seconds)),
        Decimal(read("selector CPU (local)", seconds)),
        int(read("Misses", r"[0-9]+")),
    )


def score(experiment: str, *, log_score: bool = True, options=()) -> Score:
    result = subprocess.run(
        ["make", "e2e", f"EXPERIMENT={experiment}", *options], stdout=subprocess.PIPE, text=True
    )
    if log_score or result.returncode:
        sys.stderr.write(result.stdout)
    result.check_returncode()
    try:
        return parse_score(result.stdout)
    except ValueError as error:
        raise ValueError(f"{experiment}: {error}") from error


@dataclass(frozen=True)
class DiagnosticRun:
    score: Score
    schedule: dict[str, set[int]] | None


def score_with_schedule(experiment: str) -> DiagnosticRun:
    # Each run gets new paths; a missing diagnostic cannot reuse a previous run.
    with TemporaryDirectory(prefix="rwx-ci-") as out:
        score_row = score(experiment, options=(f"OUT={out}",))
        try:
            schedule = read_schedule(Path(out) / "e2e.jsonl")
        except (OSError, ValueError, KeyError, TypeError):
            schedule = None
        return DiagnosticRun(score_row, schedule)


def median_run(runs: list[DiagnosticRun]) -> DiagnosticRun:
    return sorted(runs, key=lambda run: run.score.total)[len(runs) // 2]


def print_selection_errors(run: DiagnosticRun) -> None:
    if run.schedule is not None:
        try:
            print(selection_errors(run.schedule, Path("sample/answers.json")))
            return
        except (OSError, ValueError, KeyError, TypeError):
            pass
    print("#### Selection errors (median run)\n\nPublic-sample selection errors unavailable for the median-total run.")


def print_experiment(experiment: str, runs: list[Score], baseline: Score) -> None:
    print(f'Experiment: "{experiment}"  ')
    print(f"Score: {median(run.total for run in runs):.1f} s (median)\n")
    print("| Run | Simulated | CPU | Missed | Total |")
    print("| --- | --- | --- | --- | --- |")
    for label, run in zip(("1 of 3", "2 of 3", "3 of 3", "baseline"), [*runs, baseline]):
        print(f"| {label} | {run.simulated:.2f} s | {run.cpu:.2f} s | {run.missed} | {run.total:.2f} s |")


def print_scoring_check(commit: str, passed: bool) -> None:
    print("No new experiments in this change, so there is no score to report.\n")
    print(f"Scoring check: {'passed' if passed else 'failed'}\n")
    print("To get a score, add a new experiment name to config.py.\n")
    print(f"Commit hash: {commit}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", help="Previous branch tip or PR base commit SHA")
    parser.add_argument("--selection-errors", action="store_true", help="Report median-run selection errors for PR CI")
    args = parser.parse_args()
    try:
        experiments = added_experiments(args.base)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    try:
        if not experiments:
            score("map", log_score=False)
            print_scoring_check(commit, passed=True)
            return
        if args.selection_errors:
            diagnostics = {
                experiment: [score_with_schedule(experiment) for _ in range(3)]
                for experiment in experiments
            }
            results = {name: [run.score for run in runs] for name, runs in diagnostics.items()}
        else:
            diagnostics = {}
            results = {experiment: [score(experiment) for _ in range(3)] for experiment in experiments}
        baseline = score("baseline")
    except (ValueError, subprocess.CalledProcessError) as error:
        if not experiments:
            print_scoring_check(commit, passed=False)
        parser.exit(1, f"{error}\n")

    for experiment, runs in results.items():
        print_experiment(experiment, runs, baseline)
        if experiment in diagnostics:
            run = median_run(diagnostics[experiment])
            print()
            print_selection_errors(run)
        print()
    print(f"Machine: linux/amd64  \nCommit hash: {commit}")


if __name__ == "__main__":
    main()
