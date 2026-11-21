"""Labeling plugin: which FIELDS of each SQL fixture entry does a test actually read?

load_sql_fixture_pairs yields (meta, sql, expected). Each field is wrapped in a tracking object that records the test
that touches it at Python level (comparisons, formatting, indexing, any method, being pickled). Fields that only get bound
to a name and ignored (`_, sql, _ = row`) record nothing.

Blind spot, stated plainly: a C extension that reads a str buffer directly (duckdb.execute(sql)) does not call back into
Python, so such a read is invisible. Evaluation only; output goes to $BR_OUT/fields-<worker>.jsonl.
"""
import json
import os
import sys

import pytest

OUT = os.environ.get("BR_OUT", "/out")
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
STATE = {"test": None, "phase": None, "reads": {}, "setup_reads": set()}


def _note(file, field):
    if STATE["test"] is None:
        return
    key = (file, field)
    if STATE["phase"] == "setup":
        STATE["setup_reads"].add(key)
    STATE["reads"][key] = STATE["reads"].get(key, 0) + 1


class Tracked(str):
    """A str that reports Python-level use."""

    def __new__(cls, value, file, field):
        obj = super().__new__(cls, value)
        object.__setattr__(obj, "_loc", (file, field))
        return obj

    def _seen(self):
        _note(*object.__getattribute__(self, "_loc"))

    def __getattribute__(self, name):
        if name not in ("_seen", "_loc", "__class__", "__reduce__", "__reduce_ex__"):
            object.__getattribute__(self, "_seen")()
        return object.__getattribute__(self, name)

    def __eq__(self, other):
        self._seen()
        return str.__eq__(self, other)

    def __ne__(self, other):
        self._seen()
        return str.__ne__(self, other)

    def __hash__(self):
        self._seen()
        return str.__hash__(self)

    def __str__(self):
        self._seen()
        return str.__str__(self)

    def __repr__(self):
        self._seen()
        return str.__repr__(self)

    def __format__(self, spec):
        self._seen()
        return str.__format__(self, spec)

    def __len__(self):
        self._seen()
        return str.__len__(self)

    def __getitem__(self, i):
        self._seen()
        return str.__getitem__(self, i)

    def __iter__(self):
        self._seen()
        return str.__iter__(self)

    def __contains__(self, x):
        self._seen()
        return str.__contains__(self, x)

    def __add__(self, o):
        self._seen()
        return str.__add__(self, o)

    def __radd__(self, o):
        self._seen()
        return o + str(str.__str__(self))

    def __mod__(self, o):
        self._seen()
        return str.__mod__(self, o)

    def __bool__(self):
        self._seen()
        return len(str.__str__(self)) > 0

    def __reduce__(self):  # being pickled to a worker is a read
        self._seen()
        return (str, (str.__str__(self),))

    def __lt__(self, o):
        self._seen()
        return str.__lt__(self, o)


class TrackedMeta(dict):
    def __init__(self, value, file):
        super().__init__(value)
        self._file = file

    def _seen(self):
        _note(self._file, "meta")

    def get(self, *a):
        self._seen()
        return dict.get(self, *a)

    def __getitem__(self, k):
        self._seen()
        return dict.__getitem__(self, k)

    def __contains__(self, k):
        self._seen()
        return dict.__contains__(self, k)

    def items(self):
        self._seen()
        return dict.items(self)

    def keys(self):
        self._seen()
        return dict.keys(self)

    def __iter__(self):
        self._seen()
        return dict.__iter__(self)

    def __reduce__(self):
        self._seen()
        return (dict, (dict(dict.items(self)),))


class TrackedRow(tuple):
    """One (meta, sql, expected) row. Unpacking or indexing it is recorded as touching the fixture's rows."""

    def __new__(cls, values, file):
        obj = super().__new__(cls, values)
        object.__setattr__(obj, "_file", file)
        return obj

    def _seen(self):
        _note(object.__getattribute__(self, "_file"), "row")

    def __iter__(self):
        self._seen()
        return tuple.__iter__(self)

    def __getitem__(self, i):
        self._seen()
        return tuple.__getitem__(self, i)

    def __len__(self):
        self._seen()
        return tuple.__len__(self)

    def __reduce__(self):
        self._seen()
        return (tuple, (tuple(tuple.__iter__(self)),))


def pytest_collection_finish(session):
    import tests.helpers as helpers

    original = helpers.load_sql_fixture_pairs

    def tracked(filename):
        for meta, sql, expected in original(filename):
            yield TrackedRow((TrackedMeta(meta, filename), Tracked(sql, filename, "sql"),
                              Tracked(expected, filename, "expected")), filename)

    helpers.load_sql_fixture_pairs = tracked
    for name, module in list(sys.modules.items()):
        if module is not None and getattr(module, "load_sql_fixture_pairs", None) is original:
            module.load_sql_fixture_pairs = tracked


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    STATE["phase"] = "setup"
    yield
    STATE["phase"] = "run"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    STATE["test"], STATE["reads"], STATE["setup_reads"], STATE["phase"] = item.nodeid, {}, set(), "run"
    yield
    rec = {"id": item.nodeid, "reads": sorted([f, k, n] for (f, k), n in STATE["reads"].items()),
           "setup": sorted([f, k] for f, k in STATE["setup_reads"])}
    STATE["test"] = None
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"fields-{WORKER}.jsonl"), "a") as fh:
        fh.write(json.dumps(rec) + "\n")
