"""Batch-level test selection interface."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypedDict

if TYPE_CHECKING:
    from config import Config


class CatalogTest(TypedDict):
    number: int
    id: str
    file: str
    seconds: float


@dataclass(frozen=True)
class Patch:
    commit: str
    diff: str


class Selector(Protocol):
    def __init__(self, catalog: Sequence[CatalogTest], config: Config) -> None: ...

    def select(self, commits: Sequence[Patch]) -> Mapping[str, list[int]]: ...
