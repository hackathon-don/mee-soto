"""Choose a worker count using the benchmark's millisecond cost model."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from ..selectors.impact.pack import pack

if TYPE_CHECKING:
    from config import Config


class VariableWorkerScheduler:
    """Use the least expensive LPT layout over the allowed worker counts."""

    def __init__(self, durations: Mapping[int, float], config: Config) -> None:
        self.durations_ms = {
            number: round(seconds * 1000) for number, seconds in durations.items()
        }

    def schedule(self, selected: Sequence[int]) -> list[list[int]]:
        _, workers = pack(list(selected), self.durations_ms)
        return workers
