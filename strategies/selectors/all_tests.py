"""Select every catalog test for every patch."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from . import CatalogTest, Patch

if TYPE_CHECKING:
    from config import Config


class AllTestsSelector:
    def __init__(self, catalog: Sequence[CatalogTest], config: Config) -> None:
        self.numbers = [test["number"] for test in catalog]

    def select(self, commits: Sequence[Patch]) -> dict[str, list[int]]:
        return {patch.commit: list(self.numbers) for patch in commits}
