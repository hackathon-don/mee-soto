"""Scheduling interface independent of selection policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from config import Config


class Scheduler(Protocol):
    def __init__(self, durations: Mapping[int, float], config: Config) -> None: ...

    def schedule(self, selected: Sequence[int]) -> list[list[int]]: ...
