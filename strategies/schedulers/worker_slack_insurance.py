"""Use existing worker slack to hedge against test-selection misses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import floor
from pathlib import Path
from typing import TYPE_CHECKING

from ..selectors.impact.diffparse import parse_patch
from ..selectors.impact.impact import Index, _changed_lines
from ..selectors.impact.selector import MAPS
from .variable_workers import VariableWorkerScheduler

if TYPE_CHECKING:
    from config import Config


class WorkerSlackInsuranceScheduler(VariableWorkerScheduler):
    def __init__(self, durations: Mapping[int, float], config: Config) -> None:
        super().__init__(durations, config)
        self.patch_dir = config.data_dir / "commits"
        self.impact_index: Index | None = None
        # Preserve the base layout, but fill against the scorer's half-up rounding.
        self.judge_ms = {number: floor(seconds * 1000 + 0.5) for number, seconds in durations.items()}
        self.by_duration = sorted((ms, number) for number, ms in self.judge_ms.items())

    def _risk_masks(self, commit: str) -> tuple[int, int]:
        patch = (self.patch_dir / f"{commit}.patch").read_text(
            encoding="utf-8", errors="surrogateescape"
        )
        if self.impact_index is None:
            self.impact_index = Index(str(Path(MAPS) / "index.pkl.gz"))
        index = self.impact_index
        lines, files = 0, 0
        for file in parse_patch(patch):
            for path in {file.old_path, file.new_path} - {None}:
                files |= index.file_mask(path) | index.importers(path)
            if file.old_path:
                for line in _changed_lines(file)[0]:
                    lines |= index.run_mask(file.old_path, line, line)
                    lines |= index.def_mask(file.old_path, line, line)
        return lines, files

    def schedule_for_commit(self, commit: str, selected: Sequence[int]) -> list[list[int]]:
        workers = self.schedule(selected)
        if len(workers) < 2:
            return workers

        loads = [sum(self.judge_ms[number] for number in worker) for worker in workers]
        busiest = max(loads)
        slack = [busiest - load for load in loads]
        capacity = max(slack)
        if capacity <= 0:
            return workers

        lines, files = self._risk_masks(commit)
        scheduled = set(selected)
        tiers: list[list[tuple[int, int]]] = [[], [], []]
        for ms, number in self.by_duration:
            if ms > capacity:
                break
            if number not in scheduled:
                bit = 1 << number
                tier = 0 if lines & bit else 1 if files & bit else 2
                tiers[tier].append((ms, number))

        for tier in tiers:
            for ms, number in tier:
                worker = min((i for i, free in enumerate(slack) if free >= ms),
                             key=slack.__getitem__, default=None)
                if worker is None:
                    break
                workers[worker].append(number)
                slack[worker] -= ms
        return workers
