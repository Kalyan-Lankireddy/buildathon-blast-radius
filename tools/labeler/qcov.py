"""Measurement plugin: split test_execute_tpcds into setup, per-query work and comparison, with per-query coverage.

Evaluation only. Each query gets its own coverage context (q000, q001, ...), switched inside the test's own calls.
"""
import collections
import sys
import time

import coverage

TIMES = collections.defaultdict(float)
COUNT = collections.defaultdict(int)
STATE = {"q": 0}


def _timed(name, fn):
    def wrapper(*a, **k):
        t = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            TIMES[name] += time.perf_counter() - t
            COUNT[name] += 1
    wrapper.__module__, wrapper.__qualname__ = fn.__module__, fn.__qualname__
    return wrapper


def pytest_collection_finish(session):
    import tests.test_executor as te

    original_mp = te.mp_execute

    def mp_execute(*a, **k):
        cov = coverage.Coverage.current()
        if cov:
            cov.switch_context(f"q{STATE['q']:03d}")
        STATE["q"] += 1
        t = time.perf_counter()
        try:
            return original_mp(*a, **k)
        finally:
            TIMES["executor, per query"] += time.perf_counter() - t

    mp_execute.__module__, mp_execute.__qualname__ = "tests.test_executor", "mp_execute"
    te.mp_execute = mp_execute
    te.initializer = _timed("pool initializer (tables to executor)", te.initializer)

    original_helper = te.TestExecutor.subtestHelper

    def subtestHelper(self, i, *a, **k):
        cov = coverage.Coverage.current()
        if cov:
            cov.switch_context(f"q{i:03d}")
        t = time.perf_counter()
        try:
            return original_helper(self, i, *a, **k)
        finally:
            TIMES["comparison with duckdb, per query"] += time.perf_counter() - t

    te.TestExecutor.subtestHelper = subtestHelper
    te.TestExecutor.setUpClass = classmethod(_timed("setUpClass", te.TestExecutor.setUpClass.__func__))


def pytest_sessionfinish(session):
    print("\nQCOV TIMES", {k: round(v, 2) for k, v in TIMES.items()}, dict(COUNT), file=sys.stderr)
