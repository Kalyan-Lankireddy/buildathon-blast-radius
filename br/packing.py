"""Pack a set of tests onto at most 8 workers, cheapest by the scorer's cost."""
from __future__ import annotations

MAX_WORKERS = 8
WORKER_COST_MS = 2000


def pack(numbers: list[int], ms: dict[int, int]) -> list[list[int]]:
    """Minimize (busiest worker) + 2 s per worker. Longest-first greedy for each worker count."""
    if not numbers:
        return []
    order = sorted(numbers, key=lambda n: (-ms[n], n))
    best_cost, best = None, []
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        loads = [0] * k
        workers: list[list[int]] = [[] for _ in range(k)]
        for n in order:
            lightest = loads.index(min(loads))
            workers[lightest].append(n)
            loads[lightest] += ms[n]
        cost = max(loads) + WORKER_COST_MS * k
        if best_cost is None or cost < best_cost:
            best_cost, best = cost, workers
    return best
