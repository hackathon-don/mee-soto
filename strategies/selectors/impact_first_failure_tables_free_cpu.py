import gzip
from pathlib import Path

from . import impact_tables
from .impact import impact
from .impact_first_failure_tables import FirstFailureTablesImpact


class _PlainMapReader:
    @staticmethod
    def open(filename, mode, **kwargs):
        raw = Path(filename).with_suffix("")
        try:
            return raw.open(mode, **kwargs)
        except FileNotFoundError:
            return gzip.open(filename, mode, **kwargs)


class FirstFailureTablesFreeCpuImpact(FirstFailureTablesImpact):
    def __init__(self, catalog, config):
        impact_gzip, tables_gzip = impact.gzip, impact_tables.gzip
        impact.gzip = impact_tables.gzip = _PlainMapReader
        try:
            super().__init__(catalog, config)
        finally:
            impact.gzip, impact_tables.gzip = impact_gzip, tables_gzip
