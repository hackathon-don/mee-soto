"""Container entrypoint: select and schedule tests for each patch, emit JSONL."""

import json
import sys
from functools import lru_cache
from pathlib import Path

import selector
from scheduler import schedule

DATA = Path("/data")


def main() -> None:
    catalog: list[selector.CatalogTest] = json.loads((DATA / "tests.json").read_text())["tests"]
    durations = {test["number"]: test["seconds"] for test in catalog}

    @lru_cache(maxsize=1)
    def schedule_selection(selected: tuple[int, ...]) -> list[list[int]]:
        return schedule(selected, durations)

    patches = sorted((DATA / "commits").glob("*.patch"))
    total_selected = 0
    for patch in patches:
        diff = patch.read_text(encoding="utf-8", errors="surrogateescape")
        selected = selector.select(diff, catalog)
        workers = schedule_selection(tuple(selected))
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        total_selected += len(selected)
    print(f"selected {total_selected} tests across {len(patches)} commits", file=sys.stderr)


if __name__ == "__main__":
    main()
