"""Select and schedule tests for each patch, or run either stage on its own.

    main.py [--experiment NAME] [--data DIR] [select | schedule --picks FILE | schedule --ideal FILE]

With no subcommand, select, schedule and emit worker JSONL. Select emits
{"commit", "selected"} JSONL; schedule replays saved picks or known failures.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

if __name__ == "__main__" and os.environ.get("PYTHONHASHSEED") != "0":
    os.execve(sys.executable, [sys.executable, *sys.argv], {**os.environ, "PYTHONHASHSEED": "0"})

from config import DEFAULT_CONFIG, EXPERIMENTS, Config
from strategies.schedulers import Scheduler
from strategies.selectors import CatalogTest, Patch

Selections = list[tuple[str, list[int]]]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select and schedule tests for each patch.")
    parser.add_argument("--experiment", choices=EXPERIMENTS, default=None, help="named configuration")
    parser.add_argument("--data", type=Path, help="directory with commits/ and tests.json")
    modes = parser.add_subparsers(dest="mode")
    modes.add_parser("select", help="emit selected test numbers without scheduling")
    scheduling = modes.add_parser("schedule", help="schedule fixed test numbers without selecting")
    source = scheduling.add_mutually_exclusive_group(required=True)
    source.add_argument("--picks", type=Path, help="JSONL written by the select command")
    source.add_argument("--ideal", type=Path, help="sample answers.json; schedules its failing tests")
    args = parser.parse_args(argv)
    args.error = scheduling.error if args.mode == "schedule" else parser.error
    return args


def read_selections(args: argparse.Namespace, commits: Sequence[str], numbers: set[int]) -> Selections:
    if args.ideal is not None:
        answers = json.loads(args.ideal.read_text(encoding="utf-8"))["commits"]
        rows = [(commit, answer["failing"]) for commit, answer in answers.items()]
    else:
        with args.picks.open(encoding="utf-8") as stream:
            rows = [(row["commit"], row["selected"]) for row in map(json.loads, stream)]

    selected_by_commit: dict[str, list[int]] = {}
    for commit, selected in rows:
        if commit not in commits or commit in selected_by_commit:
            raise ValueError(f"unknown or duplicate commit: {commit!r}")
        if not isinstance(selected, list) or any(type(n) is not int or n not in numbers for n in selected):
            raise ValueError(f"{commit}: selected values must be catalog test numbers")
        if len(set(selected)) != len(selected):
            raise ValueError(f"{commit}: duplicate selected test numbers")
        selected_by_commit[commit] = selected
    missing = [commit for commit in commits if commit not in selected_by_commit]
    if missing:
        raise ValueError(f"missing selections for: {', '.join(missing)}")
    return [(commit, selected_by_commit[commit]) for commit in commits]


def write_schedule(selections: Selections, scheduler: Scheduler) -> None:
    @lru_cache(maxsize=1)
    def schedule_selection(selected: tuple[int, ...]) -> list[list[int]]:
        return scheduler.schedule(selected)

    for commit, selected in selections:
        print(json.dumps({"commit": commit, "workers": schedule_selection(tuple(selected))}))


def main(config: Config = DEFAULT_CONFIG, argv: Sequence[str] = ()) -> None:
    args = parse_args(argv)
    if args.experiment is not None:
        config = EXPERIMENTS[args.experiment]
    if args.data is not None:
        config = dataclasses.replace(config, data_dir=args.data)
    data = Path(config.data_dir)
    catalog: list[CatalogTest] = json.loads((data / "tests.json").read_text())["tests"]
    durations = {test["number"]: test["seconds"] for test in catalog}
    patches = sorted((data / "commits").glob("*.patch"))

    selector = config.selector_cls(catalog, config) if args.mode != "schedule" else None
    scheduler = config.scheduler_cls(durations, config) if args.mode != "select" else None
    if selector is None:
        try:
            selections = read_selections(
                args, [patch.stem for patch in patches], {test["number"] for test in catalog}
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            args.error(str(error))
    else:
        commits = [
            Patch(patch.stem, patch.read_text(encoding="utf-8", errors="surrogateescape"))
            for patch in patches
        ]
        chosen = selector.select(commits)
        selections = [(patch.commit, list(chosen[patch.commit])) for patch in commits]

    if scheduler is None:
        for commit, selected in selections:
            print(json.dumps({"commit": commit, "selected": selected}))
    else:
        write_schedule(selections, scheduler)
    total = sum(len(selected) for _, selected in selections)
    print(f"selected {total} tests across {len(patches)} commits", file=sys.stderr)
    if __name__ == "__main__" and config.exit_after_output and args.mode is None:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


if __name__ == "__main__":
    main(argv=sys.argv[1:])
