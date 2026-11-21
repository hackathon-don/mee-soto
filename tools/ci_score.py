"""Render the public-sample CI report without changing the scorer's CLI."""

import argparse
from dataclasses import dataclass
from decimal import Decimal
import re
from statistics import median
import subprocess
import sys


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment")
    experiment = parser.parse_args().experiment
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    runs = [score(experiment) for _ in range(3)]
    baseline = score("baseline")

    print(f'Experiment: "{experiment}"  ')
    print(f"Score: {median(run.total for run in runs):.1f} s (median)\n")
    print("| Run | Simulated | CPU | Missed | Total |")
    print("| --- | --- | --- | --- | --- |")
    for label, run in zip(("1 of 3", "2 of 3", "3 of 3", "baseline"), [*runs, baseline]):
        print(f"| {label} | {run.simulated:.2f} s | {run.cpu:.2f} s | {run.missed} | {run.total:.2f} s |")
    print(f"\nMachine: linux/amd64  \nCommit hash: {commit}")


if __name__ == "__main__":
    main()
