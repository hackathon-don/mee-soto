"""Search for the minimum variable-worker cost on small selections."""

from collections.abc import Sequence
from dataclasses import dataclass

from ..selectors.impact.pack import MAX_WORKERS, WORKER_MS, pack
from .variable_workers import VariableWorkerScheduler

MAX_EXACT_PICKS = 24
MAX_SEARCH_NODES = 20_000


class _SearchBudgetExceeded(Exception):
    pass


@dataclass
class _SearchBudget:
    remaining: int


def _fit(
    items: list[tuple[int, int]], k: int, capacity: int, budget: _SearchBudget
) -> list[list[int]] | None:
    remaining = [capacity] * k
    workers: list[list[int]] = [[] for _ in range(k)]
    infeasible: set[tuple[int, tuple[int, ...]]] = set()

    def assign(i: int) -> bool:
        if not budget.remaining:
            raise _SearchBudgetExceeded
        budget.remaining -= 1
        if i == len(items):
            return True
        state = i, tuple(sorted(remaining))
        if state in infeasible:
            return False
        number, duration = items[i]
        tried: set[int] = set()
        for j, space in enumerate(remaining):
            if space < duration or space in tried:
                continue
            tried.add(space)
            workers[j].append(number)
            remaining[j] -= duration
            if assign(i + 1):
                return True
            remaining[j] += duration
            workers[j].pop()
        infeasible.add(state)
        return False

    return [worker for worker in workers if worker] if assign(0) else None


class ExactPackingScheduler(VariableWorkerScheduler):
    def schedule(self, selected: Sequence[int]) -> list[list[int]]:
        cost, lpt = pack(list(selected), self.durations_ms)
        if len(selected) < 2 or len(selected) > MAX_EXACT_PICKS:
            return lpt

        best = lpt
        budget = _SearchBudget(MAX_SEARCH_NODES)
        items = sorted(((n, self.durations_ms[n]) for n in selected), key=lambda x: (-x[1], x[0]))
        total = sum(duration for _, duration in items)
        longest = items[0][1]
        for k in range(2, min(MAX_WORKERS, len(items)) + 1):
            fee = k * WORKER_MS
            lower = max(longest, (total + k - 1) // k)
            upper = cost - fee - 1
            while lower <= upper:
                middle = (lower + upper) // 2
                try:
                    workers = _fit(items, k, middle, budget)
                except _SearchBudgetExceeded:
                    return lpt
                if workers is None:
                    lower = middle + 1
                else:
                    best = workers
                    cost = fee + max(sum(self.durations_ms[n] for n in worker) for worker in best)
                    upper = cost - fee - 1
        return best
