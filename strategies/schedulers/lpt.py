"""Assign the longest remaining test to the lightest worker."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Config


class LptScheduler:
    def __init__(self, durations: Mapping[int, float], config: Config) -> None:
        self.durations = durations
        self.workers = config.workers

    def schedule(self, selected: Sequence[int]) -> list[list[int]]:
        workers: list[list[int]] = [[] for _ in range(self.workers)]
        loads = [0.0] * self.workers
        for number in sorted(selected, key=self.durations.__getitem__, reverse=True):
            lightest = loads.index(min(loads))
            workers[lightest].append(number)
            loads[lightest] += self.durations[number]
        return [worker for worker in workers if worker]
