from .impact_first_failure import FirstFailureImpact
from .impact_tables import TableKeySelector


class FirstFailureTablesImpact(FirstFailureImpact, TableKeySelector):
    """Use table-key candidates with first-failure verification and proofs."""
