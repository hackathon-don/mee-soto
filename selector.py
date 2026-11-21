"""Choose which catalog tests to run for one patch."""

from collections.abc import Sequence
from typing import TypedDict


class CatalogTest(TypedDict):
    number: int
    id: str
    file: str
    seconds: float


def select(diff: str, catalog: Sequence[CatalogTest]) -> list[int]:
    """Return a list of unique catalog test numbers to run for one unified diff."""
    return [test["number"] for test in catalog]
