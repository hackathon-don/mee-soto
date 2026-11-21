"""Container entrypoint: select and schedule tests for each patch, emit JSONL."""

import json
import sys
from functools import lru_cache
from pathlib import Path

from config import DEFAULT_CONFIG, Config
from strategies.selectors import CatalogTest, Patch


def main(config: Config = DEFAULT_CONFIG) -> None:
    data = Path(config.data_dir)
    catalog: list[CatalogTest] = json.loads((data / "tests.json").read_text())["tests"]
    durations = {test["number"]: test["seconds"] for test in catalog}
    selector = config.selector_cls(catalog, config)
    scheduler = config.scheduler_cls(durations, config)

    @lru_cache(maxsize=1)
    def schedule_selection(selected: tuple[int, ...]) -> list[list[int]]:
        return scheduler.schedule(selected)

    patches = sorted((data / "commits").glob("*.patch"))
    commits = [
        Patch(patch.stem, patch.read_text(encoding="utf-8", errors="surrogateescape"))
        for patch in patches
    ]
    selections = selector.select(commits)
    total_selected = 0
    for patch in commits:
        selected = selections[patch.commit]
        workers = schedule_selection(tuple(selected))
        print(json.dumps({"commit": patch.commit, "workers": workers}))
        total_selected += len(selected)
    print(f"selected {total_selected} tests across {len(patches)} commits", file=sys.stderr)


if __name__ == "__main__":
    main()
