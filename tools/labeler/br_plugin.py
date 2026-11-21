"""pytest plugin used ONLY at map-building time (never shipped in the submission image).

Besides the per-test line coverage that pytest-cov records (--cov-context=test), it records for
each test, as facts about sqlglot at the pinned commit:

  classes  every sqlglot class that had an instance created during the test, expanded through the
           MRO (a test that builds a Snowflake dialect also uses Dialect). Class-body statements run
           only at import time, so line coverage cannot say which tests depend on them; this can.
  opens    every file under the checkout the test opened (fixtures, sources read as text)
  listdirs every directory the test listed

Output: one JSON line per test in $BR_OUT/meta-<worker>.jsonl.
"""
import json
import os
import sys

import pytest

ROOT = os.environ.get("BR_ROOT", "/src/sqlglot") + os.sep
OUT = os.environ.get("BR_OUT", "/out")
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")

_state = {"active": False, "classes": set(), "opens": set(), "listdirs": set(), "spawns": False}
SPAWN_EVENTS = {"subprocess.Popen", "os.system", "os.fork", "os.posix_spawn", "os.exec", "os.spawn"}


def _rel(path):
    try:
        path = os.fspath(path)
    except TypeError:
        return None
    if isinstance(path, bytes):
        return None
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    path = os.path.normpath(path)
    if path.startswith(ROOT):
        return path[len(ROOT):]
    return None


def _audit(event, args):
    if not _state["active"]:
        return
    if event in SPAWN_EVENTS:
        _state["spawns"] = True
    elif event == "open":
        rel = _rel(args[0]) if isinstance(args[0], (str, os.PathLike)) else None
        if rel is not None and not rel.endswith(".pyc"):
            _state["opens"].add(rel)
    elif event in ("os.listdir", "os.scandir"):
        target = args[0] if args else None
        if target is None:
            target = "."
        if isinstance(target, (str, os.PathLike)):
            rel = _rel(target)
            if rel is not None:
                _state["listdirs"].add(rel)


def _wrap_init(cls):
    original = cls.__dict__.get("__init__")
    if original is None or getattr(original, "_br_wrapped", False):
        return

    def __init__(self, *args, **kwargs):
        if _state["active"]:
            _state["classes"].add(type(self))
        return original(self, *args, **kwargs)

    __init__._br_wrapped = True
    __init__.__wrapped__ = original
    cls.__init__ = __init__


def _all_subclasses(cls, seen=None):
    seen = set() if seen is None else seen
    for sub in cls.__subclasses__():
        if sub not in seen:
            seen.add(sub)
            _all_subclasses(sub, seen)
    return seen


def _install():
    from sqlglot import generator, parser, tokens
    from sqlglot import expressions as exp
    from sqlglot.dialects.dialect import Dialect, _Dialect

    # Dialect instances: the metaclass __call__ sees every instantiation, however it is reached.
    original_call = _Dialect.__call__

    def dialect_call(cls, *args, **kwargs):
        if _state["active"]:
            _state["classes"].add(cls)
        return original_call(cls, *args, **kwargs)

    _Dialect.__call__ = dialect_call
    for base in (exp.Expr, tokens.Tokenizer, parser.Parser, generator.Generator, Dialect):
        _wrap_init(base)
        for sub in _all_subclasses(base):
            _wrap_init(sub)


class _InProcessExecutor:
    """Stands in for ProcessPoolExecutor while labeling.

    Some tests (test_optimizer, test_executor) hand their work to a spawn process pool. Coverage does
    not follow children, so the lines that work runs would be attributed to no test, and a mutation in
    them would look harmless to the maps while failing those tests. Running the same callables in
    process, inside the submitting test, records them where they belong.

    A real pool also pickles everything that crosses the process boundary, and sqlglot pickles
    expressions through serde.dump / serde.load. Those lines run in every test that uses the pool, so
    arguments and results make the same round trip here.
    """

    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        from concurrent.futures import Future

        import pickle

        future = Future()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))  # into the "child"
            result = fn(*args, **kwargs)
            future.set_result(pickle.loads(pickle.dumps(result)))  # and back to the parent
        except BaseException as error:  # the test reads it back with future.result()
            future.set_exception(error)
        return future

    def shutdown(self, *args, **kwargs):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _inline_process_pools():
    import concurrent.futures

    real = concurrent.futures.ProcessPoolExecutor
    concurrent.futures.ProcessPoolExecutor = _InProcessExecutor
    patched = []
    for name, module in list(sys.modules.items()):
        if module is not None and getattr(module, "ProcessPoolExecutor", None) is real:
            module.ProcessPoolExecutor = _InProcessExecutor
            patched.append(name)
    return patched


def _import_every_module():
    """Import every dialect, parser, generator and typing module now, outside any test.

    Dialects load lazily inside the first test that needs them, which would credit all of a module's
    import-time lines (class bodies, helpers the metaclass calls) to that one test. Importing them here
    records those lines as import-time, which is what they are.
    """
    import importlib

    root = os.path.join(os.environ.get("BR_ROOT", "/src/sqlglot"), "sqlglot")
    for package in ("dialects", "parsers", "generators", "typing", "optimizer", "executor", "expressions"):
        directory = os.path.join(root, package)
        for name in sorted(os.listdir(directory)):
            if name.endswith(".py") and name != "__init__.py":
                try:
                    importlib.import_module(f"sqlglot.{package}.{name[:-3]}")
                except Exception as error:  # a module that cannot import on its own is simply skipped
                    print(f"br_plugin: could not import sqlglot.{package}.{name[:-3]}: {error}", file=sys.stderr)


def _class_names(classes):
    names = set()
    for cls in classes:
        for klass in cls.__mro__:
            module = getattr(klass, "__module__", "") or ""
            if module == "sqlglot" or module.startswith("sqlglot."):
                names.add(f"{module}:{klass.__qualname__}")
    return sorted(names)


def pytest_collection_finish(session):
    # After collection, so the import-time lines have already been recorded by coverage.
    _import_every_module()
    patched = _inline_process_pools()
    _install()
    sys.addaudithook(_audit)
    print(f"br_plugin: process pools run in process in {patched}", file=sys.stderr)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _state["classes"] = set()
    _state["opens"] = set()
    _state["listdirs"] = set()
    _state["spawns"] = False
    _state["active"] = True
    try:
        yield
    finally:
        _state["active"] = False
        record = {
            "id": item.nodeid,
            "classes": _class_names(_state["classes"]),
            "opens": sorted(_state["opens"]),
            "listdirs": sorted(_state["listdirs"]),
            "spawns": _state["spawns"],
        }
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, f"meta-{WORKER}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
