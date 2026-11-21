"""Render the public-sample CI report without changing the scorer's CLI."""

import argparse
import ast
from dataclasses import dataclass
from decimal import Decimal
import re
from statistics import median
import subprocess
import sys


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


def score(experiment: str) -> Score:
    result = subprocess.run(
        ["make", "e2e", f"EXPERIMENT={experiment}"], stdout=subprocess.PIPE, text=True
    )
    sys.stderr.write(result.stdout)
    if result.returncode:
        raise SystemExit(result.returncode)
    try:
        return parse_score(result.stdout)
    except ValueError as error:
        raise SystemExit(f"{experiment}: {error}") from error


def print_experiment(experiment: str, runs: list[Score], baseline: Score) -> None:
    print(f'Experiment: "{experiment}"  ')
    print(f"Score: {median(run.total for run in runs):.1f} s (median)\n")
    print("| Run | Simulated | CPU | Missed | Total |")
    print("| --- | --- | --- | --- | --- |")
    for label, run in zip(("1 of 3", "2 of 3", "3 of 3", "baseline"), [*runs, baseline]):
        print(f"| {label} | {run.simulated:.2f} s | {run.cpu:.2f} s | {run.missed} | {run.total:.2f} s |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", help="Previous branch tip or PR base commit SHA")
    try:
        experiments = added_experiments(parser.parse_args().base)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    if not experiments:
        print("No new experiments to score.")
        return

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    results = {experiment: [score(experiment) for _ in range(3)] for experiment in experiments}
    baseline = score("baseline")

    for experiment, runs in results.items():
        print_experiment(experiment, runs, baseline)
        print()
    print(f"Machine: linux/amd64  \nCommit hash: {commit}")


if __name__ == "__main__":
    main()
