from .impact_first_failure_tables_free_cpu_outcomes import FirstFailureTablesFreeCpuOutcomesImpact
from .impact_fixture_entries import FixtureEntryImpact
from .impact_keys import KeyReadImpact


class StackedMapsImpact(FirstFailureTablesFreeCpuOutcomesImpact, FixtureEntryImpact, KeyReadImpact):
    """Install key narrowing before wrapping the analyzer for fixture capture."""
