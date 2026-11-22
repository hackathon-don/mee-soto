"""Selector and scheduler experiment configuration."""

from dataclasses import dataclass, replace
from pathlib import Path

from strategies.schedulers import Scheduler
from strategies.schedulers.exact_packing import ExactPackingScheduler
from strategies.schedulers.lpt import LptScheduler
from strategies.schedulers.variable_workers import VariableWorkerScheduler
from strategies.selectors import Selector
from strategies.selectors.all_tests import AllTestsSelector
from strategies.selectors.impact import ImpactSelector
from strategies.selectors.impact_first_failure import FirstFailureImpact
from strategies.selectors.impact_first_failure_tables import FirstFailureTablesImpact
from strategies.selectors.impact_first_failure_tables_free_cpu import FirstFailureTablesFreeCpuImpact
from strategies.selectors.impact_first_failure_tables_free_cpu_outcomes import FirstFailureTablesFreeCpuOutcomesImpact
from strategies.selectors.impact_first_failure_tables_outcomes import FirstFailureTablesOutcomesImpact
from strategies.selectors.impact_fresh_copy import FreshTableCopyImpact
from strategies.selectors.impact_individual_proof import IndividualProofImpact
from strategies.selectors.impact_optimizer_transcript import OptimizerTranscriptImpact
from strategies.selectors.impact_tables import TableKeySelector

MAX_WORKERS = 8


@dataclass(frozen=True)
class Config:
    selector_cls: type[Selector] = AllTestsSelector
    scheduler_cls: type[Scheduler] = LptScheduler
    workers: int = 3
    data_dir: Path = Path("/data")
    exit_after_output: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.workers <= MAX_WORKERS:
            raise ValueError(f"workers must be between 1 and {MAX_WORKERS}, got {self.workers}")


EXPERIMENTS: dict[str, Config] = {
    "baseline": Config(),
    "impact": Config(selector_cls=ImpactSelector),
    "impact-individual-pass": Config(selector_cls=IndividualProofImpact),
    "impact-fresh-table-copy": Config(selector_cls=FreshTableCopyImpact),
    "impact-full-optimizer-proof": Config(selector_cls=OptimizerTranscriptImpact),
    "impact-variable-workers-matched": Config(
        selector_cls=OptimizerTranscriptImpact, scheduler_cls=VariableWorkerScheduler
    ),
    "impact-tables": Config(selector_cls=TableKeySelector),
    "impact-tables-variable-workers": Config(
        selector_cls=TableKeySelector, scheduler_cls=VariableWorkerScheduler
    ),
    "impact-first-failure": Config(
        selector_cls=FirstFailureImpact, scheduler_cls=VariableWorkerScheduler
    ),
    "impact-first-failure-tables": Config(
        selector_cls=FirstFailureTablesImpact, scheduler_cls=VariableWorkerScheduler
    ),
    "impact-first-failure-tables-outcomes": Config(
        selector_cls=FirstFailureTablesOutcomesImpact, scheduler_cls=VariableWorkerScheduler
    ),
    "impact-first-failure-tables-free-cpu": Config(
        selector_cls=FirstFailureTablesFreeCpuImpact,
        scheduler_cls=VariableWorkerScheduler,
        exit_after_output=True,
    ),
    "impact-first-failure-exact-packing": Config(
        selector_cls=FirstFailureImpact, scheduler_cls=ExactPackingScheduler
    ),
}

EXPERIMENTS["impact-first-failure-tables-free-cpu-outcomes"] = replace(
    EXPERIMENTS["impact-first-failure-tables-free-cpu"], selector_cls=FirstFailureTablesFreeCpuOutcomesImpact,
)

DEFAULT_CONFIG = EXPERIMENTS["baseline"]
