"""Split selected tests over workers to minimise busiest load + 2 s per worker."""

from __future__ import annotations

WORKER_MS = 2000
MAX_WORKERS = 8


def _lpt(items: list[tuple[int, int]], k: int) -> tuple[int, list[list[int]]]:
    loads = [0] * k
    bins: list[list[int]] = [[] for _ in range(k)]
    for number, ms in items:
        i = loads.index(min(loads))
        loads[i] += ms
        bins[i].append(number)
    return max(loads), bins


def pack(numbers: list[int], ms_of: dict[int, int]) -> tuple[int, list[list[int]]]:
    """Return (cost in ms, workers). Empty selection costs 0."""
    if not numbers:
        return 0, []
    items = sorted(((n, ms_of[n]) for n in numbers), key=lambda x: (-x[1], x[0]))
    best = None
    for k in range(1, min(MAX_WORKERS, len(items)) + 1):
        span, bins = _lpt(items, k)
        cost = span + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, [b for b in bins if b])
    return best  # type: ignore[return-value]
