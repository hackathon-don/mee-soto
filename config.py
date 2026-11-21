"""Baseline experiment configuration."""

from dataclasses import dataclass
from pathlib import Path

from strategies.schedulers import Scheduler
from strategies.schedulers.lpt import LptScheduler
from strategies.selectors import Selector
from strategies.selectors.all_tests import AllTestsSelector


@dataclass(frozen=True)
class Config:
    selector_cls: type[Selector] = AllTestsSelector
    scheduler_cls: type[Scheduler] = LptScheduler
    workers: int = 3
    data_dir: Path = Path("/data")


DEFAULT_CONFIG = Config()
