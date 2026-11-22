"""Selector and scheduler experiment configuration."""

from dataclasses import dataclass
from pathlib import Path

from strategies.schedulers import Scheduler
from strategies.schedulers.lpt import LptScheduler
from strategies.schedulers.variable_workers import VariableWorkerScheduler
from strategies.selectors import Selector
from strategies.selectors.all_tests import AllTestsSelector
from strategies.selectors.impact import ImpactSelector
from strategies.selectors.impact_optimizer_transcript import OptimizerTranscriptImpact
from strategies.selectors.impact_tables import TableKeySelector
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
    "impact": Config(selector_cls=ImpactSelector),
    "impact-full-optimizer-proof": Config(selector_cls=OptimizerTranscriptImpact),
    "impact-variable-workers-matched": Config(
        selector_cls=OptimizerTranscriptImpact, scheduler_cls=VariableWorkerScheduler
    ),
    "impact-tables": Config(selector_cls=TableKeySelector),
    "impact-tables-variable-workers": Config(
        selector_cls=TableKeySelector, scheduler_cls=VariableWorkerScheduler
    ),
}

DEFAULT_CONFIG = EXPERIMENTS["baseline"]
