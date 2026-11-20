"""v0 selector: run every test on every commit, packed as cheaply as possible.

It never misses a failing test. The only saving is the packing: pick the number of workers k
(1 to 8) and the split that minimize  busiest worker's seconds + 2 s * k.
"""

import json
import sys
from pathlib import Path

DATA = Path("/data")
MAX_WORKERS = 8
WORKER_COST_MS = 2000


def duration_ms(test: dict) -> int:
    # The scorer's rule: round(seconds * 1000), halves up.
    return int(test["seconds"] * 1000 + 0.5)


def pack(numbers: list[int], ms: dict[int, int]) -> list[list[int]]:
    """Split `numbers` into at most 8 workers, minimizing makespan + 2 s per worker.

    Longest-first greedy for each worker count, keep the cheapest count. Empty input is no workers.
    """
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


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: duration_ms(t) for t in tests}
    workers = pack(sorted(ms), ms)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    print(f"v0: all {len(tests)} tests on every commit, {len(workers)} workers", file=sys.stderr)


if __name__ == "__main__":
    main()
