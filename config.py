"""Selector and scheduler experiment configuration."""

from dataclasses import dataclass
from pathlib import Path

from strategies.schedulers import Scheduler
from strategies.schedulers.lpt import LptScheduler
from strategies.selectors import Selector
from strategies.selectors.all_tests import AllTestsSelector
from strategies.selectors.map_narrow import NarrowMapSelector
from strategies.selectors.map_only import MapSelector

MAX_WORKERS = 8


@dataclass(frozen=True)
class Config:
    selector_cls: type[Selector] = AllTestsSelector
    scheduler_cls: type[Scheduler] = LptScheduler
    workers: int = 3
    data_dir: Path = Path("/data")

    def __post_init__(self) -> None:
        if not 1 <= self.workers <= MAX_WORKERS:
            raise ValueError(f"workers must be between 1 and {MAX_WORKERS}, got {self.workers}")


EXPERIMENTS: dict[str, Config] = {
    "baseline": Config(),
    "map": Config(selector_cls=MapSelector),
    "map-narrow": Config(selector_cls=NarrowMapSelector),
}

DEFAULT_CONFIG = EXPERIMENTS["baseline"]
