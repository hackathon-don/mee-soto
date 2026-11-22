from __future__ import annotations

import ast
from functools import lru_cache
from heapq import heapreplace

from .impact import impact, pack
from .impact_first_failure import FirstFailureImpact


def _pack_cost(numbers: list[int], ms_of: dict[int, int]) -> tuple[int, list[list[int]]]:
    if not numbers:
        return 0, []
    durations = sorted((ms_of[n] for n in numbers), reverse=True)
    total = sum(durations)
    best = total + pack.WORKER_MS
    for workers in range(2, min(pack.MAX_WORKERS, len(durations)) + 1):
        overhead = pack.WORKER_MS * workers
        if (total + workers - 1) // workers + overhead >= best:
            continue
        loads = [0] * workers
        for duration in durations:
            heapreplace(loads, loads[0] + duration)
        best = min(best, max(loads) + overhead)
    return best, []


class _CachedAnalyzer(impact.Analyzer):
    def __init__(self, index, base_root):
        super().__init__(index, base_root)
        self._parse = lru_cache(maxsize=8)(ast.parse)

    def analyze(self, patches):
        original = impact.ast
        impact.ast = self
        try:
            return super().analyze(patches)
        finally:
            impact.ast = original

    def __getattr__(self, name):
        return getattr(ast, name)

    def parse(self, source):
        return self._parse(source)


class ParentCpuImpact(FirstFailureImpact):
    def select(self, commits):
        if self.ctx is None:
            return super().select(commits)
        analyzer = self.ctx.an
        original_pack = pack.pack
        self.ctx.an = _CachedAnalyzer(analyzer.ix, analyzer.base)
        pack.pack = _pack_cost
        try:
            return super().select(commits)
        finally:
            self.ctx.an = analyzer
            pack.pack = original_pack
