"""Select tests for each patch and emit the worker schedule as JSONL."""

import json
import sys
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

from scheduler import schedule

DATA = Path("/data")


class CatalogTest(TypedDict):
    number: int
    id: str
    file: str
    seconds: float


def select(diff: str, catalog: Sequence[CatalogTest]) -> list[int]:
    """Return the test numbers to run for one unified diff."""
    return [test["number"] for test in catalog]


def main() -> None:
    catalog: list[CatalogTest] = json.loads((DATA / "tests.json").read_text())["tests"]
    durations = {test["number"]: test["seconds"] for test in catalog}

    @lru_cache(maxsize=1)
    def schedule_selection(selected: tuple[int, ...]) -> list[list[int]]:
        return schedule(selected, durations)

    patches = sorted((DATA / "commits").glob("*.patch"))
    total_selected = 0
    for patch in patches:
        diff = patch.read_text(encoding="utf-8", errors="surrogateescape")
        selected = select(diff, catalog)
        workers = schedule_selection(tuple(selected))
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        total_selected += len(selected)
    print(f"selected {total_selected} tests across {len(patches)} commits", file=sys.stderr)


if __name__ == "__main__":
    main()
