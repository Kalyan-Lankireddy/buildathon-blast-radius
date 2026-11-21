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

_state = {"dict_keys": {}, "dict_iter": {}, "active": False, "classes": set(), "opens": set(), "listdirs": set(), "spawns": False}
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


def _norm_key(key):
    """A lookup key in the form the AST of a dict literal gives: a class or enum member by its last name."""
    import enum

    if isinstance(key, str):
        return "S:" + key
    if isinstance(key, type):
        return key.__name__
    if isinstance(key, enum.Enum):
        return key.name
    return None  # a key we cannot name; callers treat it as "any key"


class _RecordingDict(dict):
    """A class-level dict that notes which keys tests look up, and when a test walks the whole thing."""

    __slots__ = ("owner", "attr")

    def _note_key(self, key):
        if _state["active"]:
            norm = _norm_key(key)
            if norm is None:
                _state["dict_iter"].setdefault((self.owner, self.attr), True)
            else:
                _state["dict_keys"].setdefault((self.owner, self.attr), set()).add(norm)

    def _note_walk(self):
        if _state["active"]:
            _state["dict_iter"][(self.owner, self.attr)] = True

    def __getitem__(self, key):
        self._note_key(key)
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        self._note_key(key)
        return dict.get(self, key, default)

    def __contains__(self, key):
        self._note_key(key)
        return dict.__contains__(self, key)

    def pop(self, key, *default):
        self._note_key(key)
        return dict.pop(self, key, *default)

    def setdefault(self, key, default=None):
        self._note_key(key)
        return dict.setdefault(self, key, default)

    def __iter__(self):
        self._note_walk()
        return dict.__iter__(self)

    def keys(self):
        self._note_walk()
        return dict.keys(self)

    def values(self):
        self._note_walk()
        return dict.values(self)

    def items(self):
        self._note_walk()
        return dict.items(self)

    def __len__(self):
        self._note_walk()
        return dict.__len__(self)

    def copy(self):
        self._note_walk()
        return dict(dict.items(self))

    def update(self, *args, **kwargs):
        self._note_walk()
        return dict.update(self, *args, **kwargs)

    def __or__(self, other):
        self._note_walk()
        return dict(dict.items(self)) | other

    def __ror__(self, other):
        self._note_walk()
        return other | dict(dict.items(self))

    def __reduce__(self):
        return (dict, (dict(dict.items(self)),))


def _wrap_class_dicts():
    """Replace every ALL_CAPS plain-dict class attribute of a Tokenizer, Parser, Generator or Dialect."""
    import re

    from sqlglot import generator, parser, tokens
    from sqlglot.dialects.dialect import Dialect

    caps = re.compile(r"^[A-Z][A-Z0-9_]*$")
    seen = set()
    for base in (tokens.Tokenizer, parser.Parser, generator.Generator, Dialect):
        for cls in [base, *_all_subclasses(base)]:
            if cls in seen:
                continue
            seen.add(cls)
            for name, value in list(vars(cls).items()):
                if type(value) is dict and caps.match(name):
                    rec = _RecordingDict(value)
                    rec.owner = f"{cls.__module__}:{cls.__qualname__}"
                    rec.attr = name
                    setattr(cls, name, rec)


def _record_dispatch_tables():
    """Record lookups on each generator class's cached dispatch table.

    Generator.__init__ copies TRANSFORMS (plus the *_sql methods) into one dict per class, cached for the
    process, and real lookups go through that copy (`self._dispatch.get(expression.__class__)`), not
    through TRANSFORMS. The copy is keyed like TRANSFORMS, so its lookups are lookups of TRANSFORMS keys.
    """
    from sqlglot import generator

    original = generator._build_dispatch

    def build(cls):
        rec = _RecordingDict(original(cls))
        rec.owner = f"{cls.__module__}:{cls.__qualname__}"
        rec.attr = "TRANSFORMS"
        return rec

    generator._build_dispatch = build


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
    _wrap_class_dicts()
    _record_dispatch_tables()
    sys.addaudithook(_audit)
    print(f"br_plugin: process pools run in process in {patched}", file=sys.stderr)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    """What the setup phase did. Class-level setup (setUpClass, class fixtures) runs once, inside the
    first test of the class, but every test in the class depends on it; the map builder shares it out."""
    yield
    _state["setup"] = {"classes": set(_state["classes"]), "opens": set(_state["opens"]),
                       "listdirs": set(_state["listdirs"]),
                       "dict_keys": {k: set(v) for k, v in _state["dict_keys"].items()},
                       "dict_iter": dict(_state["dict_iter"])}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _state["classes"] = set()
    _state["opens"] = set()
    _state["listdirs"] = set()
    _state["spawns"] = False
    _state["setup"] = {"classes": set(), "opens": set(), "listdirs": set(), "dict_keys": {}, "dict_iter": {}}
    _state["dict_keys"] = {}
    _state["dict_iter"] = {}
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
            "dict_keys": [[o, a, sorted(k)] for (o, a), k in sorted(_state["dict_keys"].items())],
            "dict_iter": [[o, a] for (o, a) in sorted(_state["dict_iter"])],
            "setup_dict_keys": [[o, a, sorted(k)] for (o, a), k in sorted(_state["setup"]["dict_keys"].items())],
            "setup_dict_iter": [[o, a] for (o, a) in sorted(_state["setup"]["dict_iter"])],
            "setup_classes": _class_names(_state["setup"]["classes"]),
            "setup_opens": sorted(_state["setup"]["opens"]),
            "setup_listdirs": sorted(_state["setup"]["listdirs"]),
        }
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, f"meta-{WORKER}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
