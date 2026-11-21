"""Assign selected tests to workers independently of the selection policy."""

from collections.abc import Mapping, Sequence

WORKERS = 3


def schedule(selected: Sequence[int], durations: Mapping[int, float]) -> list[list[int]]:
    """Put the longest remaining test on the lightest worker; input numbers must be unique."""
    workers: list[list[int]] = [[] for _ in range(WORKERS)]
    loads = [0.0] * WORKERS
    for number in sorted(selected, key=durations.__getitem__, reverse=True):
        lightest = loads.index(min(loads))
        workers[lightest].append(number)
        loads[lightest] += durations[number]
    return [worker for worker in workers if worker]
