"""Report public-sample selection errors on the host after scoring."""

from dataclasses import dataclass
from decimal import Decimal
import json
import math
from pathlib import Path
import re


PATCH_ID = re.compile(r"[a-zA-Z0-9_-]+")


# Matches score.py's per-worker cost.
WORKER_COST_MS = 2_000


def read_workers(text: str) -> dict[str, list[list[int]]]:
    workers = {}
    for line in text.splitlines():
        row = json.loads(line)
        patch = row["commit"]
        if not PATCH_ID.fullmatch(patch) or patch in workers:
            raise ValueError("invalid or duplicate patch in schedule")
        if any(type(test) is not int for worker in row["workers"] for test in worker):
            raise ValueError("invalid test number in schedule")
        workers[patch] = [list(worker) for worker in row["workers"]]
    return workers


def _failures(answers_path: Path) -> dict[str, set[int]]:
    answers = json.loads(answers_path.read_text(encoding="utf-8"))["commits"]
    return {patch: set(row["failing"]) for patch, row in answers.items()}


@dataclass(frozen=True)
class FalseChecks:
    """Verifier checks that completed for a test with no recorded failure on that patch."""
    false: int
    total: int
    cpu: Decimal


@dataclass(frozen=True)
class FalseScheduling:
    """Scheduled tests with no recorded failure, and the simulated time above an ideal replay."""
    false: int
    total: int
    simulated_ms: int


def false_checks(cpu, answers_path: Path) -> FalseChecks:
    failures = _failures(answers_path)
    if cpu.keys() - failures.keys():
        raise ValueError("CPU diagnostics cover unknown patches")
    false = [(number, seconds) for patch, data in cpu.items()
             for number, seconds in data.timed if number not in failures[patch]]
    # Only completed checks count; tests left unrun after an early stop are not checks.
    total = sum(len(data.timed) for data in cpu.values())
    return FalseChecks(len(false), total, sum((seconds for _, seconds in false), Decimal(0)))


def _simulated_ms(workers: list[list[int]], durations: dict[int, int]) -> int:
    occupied = [worker for worker in workers if worker]
    if not occupied:
        return 0
    return max(sum(durations[test] for test in worker) for worker in occupied) + WORKER_COST_MS * len(occupied)


def false_scheduling(workers: dict[str, list[list[int]]], ideal: dict[str, list[list[int]]],
                     answers_path: Path, tests_path: Path) -> FalseScheduling:
    """Compare each patch with the same scheduler replaying only its known failures."""
    failures = _failures(answers_path)
    if workers.keys() != failures.keys() or ideal.keys() != failures.keys():
        raise ValueError("public labels and schedules cover different patches")
    # Same rounding as score.py.
    durations = {test["number"]: math.floor(test["seconds"] * 1000 + 0.5)
                 for test in json.loads(tests_path.read_text(encoding="utf-8"))["tests"]}
    false = total = extra_ms = 0
    for patch, rows in workers.items():
        sent = {test for worker in rows for test in worker}
        total += len(sent)
        false += len(sent - failures[patch])
        if failures[patch] <= sent:
            extra_ms += _simulated_ms(rows, durations) - _simulated_ms(ideal[patch], durations)
    return FalseScheduling(false, total, extra_ms)


def render_false_work(checks: FalseChecks | None, scheduling: FalseScheduling | None) -> str:
    """Counts use public-sample labels; a false item here can still fail on a hidden patch."""
    if checks is None:
        check_line = "**False checks:** unavailable (no per-check diagnostics)."
    else:
        check_line = f"**False checks:** {checks.false} of {checks.total}, {checks.cpu:.2f} s CPU."
    if scheduling is None:
        schedule_line = "**False scheduling:** unavailable."
    else:
        schedule_line = (f"**False scheduling:** {scheduling.false} of {scheduling.total}, "
                         f"{scheduling.simulated_ms / 1000:.2f} s simulated.")
    return f"{check_line}  \n{schedule_line}"


def selection_errors(schedule: dict[str, set[int]], answers_path: Path) -> str:
    """Use the public answer file only here, after the container has finished."""
    answers = json.loads(answers_path.read_text(encoding="utf-8"))["commits"]
    if answers.keys() != schedule.keys():
        raise ValueError("public labels and schedule cover different patches")
    failures = {patch: set(row["failing"]) for patch, row in answers.items()}
    misses = {patch: sorted(failures[patch] - sent) for patch, sent in schedule.items()}
    missing_ids = sum(map(len, misses.values()))
    extra = sum(len(sent - failures[patch]) for patch, sent in schedule.items())
    lines = [
        "#### Selection errors (median run)",
        "",
        f"**Missed patches:** {sum(bool(ids) for ids in misses.values())}/{len(schedule)}. "
        f"**Failing tests omitted:** {missing_ids}. **Scheduled without a recorded failure:** {extra}.",
    ]
    if missing_ids:
        lines += ["", "Omitted failing test IDs by patch:"]
        lines += [f"- `{patch}`: {', '.join(map(str, ids))}" for patch, ids in sorted(misses.items()) if ids]
    return "\n".join(lines)
