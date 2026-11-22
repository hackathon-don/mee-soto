"""Report public-sample selection errors on the host after scoring."""

import json
from pathlib import Path
import re


PATCH_ID = re.compile(r"[a-zA-Z0-9_-]+")


def read_schedule(path: Path) -> dict[str, set[int]]:
    schedule = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        patch = row["commit"]
        if not PATCH_ID.fullmatch(patch) or patch in schedule:
            raise ValueError("invalid or duplicate patch in schedule")
        schedule[patch] = {test for worker in row["workers"] for test in worker}
    return schedule


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
