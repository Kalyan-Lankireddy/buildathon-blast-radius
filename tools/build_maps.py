"""Build the committed maps from a labeling run. Runs inside the labeler image (needs coverage).

Inputs   $BR_OUT/.coverage      per-test line coverage (pytest-cov --cov-context=test)
         $BR_OUT/meta-*.jsonl   per-test class usage, opened files, listed directories (br_plugin)
         $BR_ROOT               the sqlglot checkout at the pinned commit
Output   $BR_MAPS               maps.json.gz

Everything here is a fact about sqlglot at the pinned commit: which tests ran which lines, built which
classes, opened which files. Nothing is derived from any commit's answers.
"""
import ast
import glob
import gzip
import json
import os
import re
import sys

import coverage

ROOT = os.environ.get("BR_ROOT", "/src/sqlglot")
OUT = os.environ.get("BR_OUT", "/out")
MAPS = os.environ.get("BR_MAPS", os.path.join(OUT, "maps.json.gz"))


def test_id(context):
    """'tests/x.py::T::t|run' -> 'tests/x.py::T::t'; '' stays ''."""
    return context.split("|", 1)[0]


def phase(context):
    return context.split("|", 1)[1] if "|" in context else ""


def class_of(name):
    """'tests/x.py::T::t' -> 'tests/x.py::T'; a plain function test has no class and shares nothing."""
    parts = name.split("::")
    return "::".join(parts[:-1]) if len(parts) >= 3 else None


def module_of(rel):
    name = rel[:-3] if rel.endswith(".py") else rel
    parts = name.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def import_graph():
    """Who imports whom, from a static scan of sqlglot/ and tests/.

    top[file]   = files that import it at module level (the name is bound when the importer loads)
    local[file] = (file, first line, last line) of functions that import it inside their body
    """
    rels = []
    for top_dir in ("sqlglot", "tests"):
        for dirpath, _, names in os.walk(os.path.join(ROOT, top_dir)):
            for name in names:
                if name.endswith(".py"):
                    rels.append(os.path.relpath(os.path.join(dirpath, name), ROOT))
    module_file = {module_of(r): r for r in rels}
    top, local = {}, {}

    def edge(target_module, importer, func_range):
        target = module_file.get(target_module)
        if not target or target == importer:
            return
        if func_range is None:
            top.setdefault(target, set()).add(importer)
        else:
            local.setdefault(target, set()).add((importer, *func_range))

    for rel in rels:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        package = module_of(rel).split(".") if rel.endswith("__init__.py") else module_of(rel).split(".")[:-1]

        def visit(node, func_range):
            for child in ast.iter_child_nodes(node):
                inner = func_range
                if func_range is None and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    first = min([child.lineno] + [d.lineno for d in child.decorator_list])
                    inner = (first, child.end_lineno)
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        edge(alias.name, rel, inner)
                elif isinstance(child, ast.ImportFrom):
                    if child.level:
                        base_parts = package[: len(package) - child.level + 1]
                        base = ".".join(base_parts + ([child.module] if child.module else []))
                    else:
                        base = child.module or ""
                    edge(base, rel, inner)
                    for alias in child.names:
                        edge(f"{base}.{alias.name}", rel, inner)
                        # `from sqlglot.dialects import BigQuery` loads sqlglot/dialects/bigquery.py lazily
                        edge(f"{base}.{alias.name.lower()}", rel, inner)
                visit(child, inner)

        visit(tree, None)
    return (
        {k: sorted(v) for k, v in sorted(top.items())},
        {k: [list(t) for t in sorted(v)] for k, v in sorted(local.items())},
    )


CONSTANT = re.compile(r"^[A-Z][A-Z0-9_]*$")
IMPORT_TIME_FUNCTIONS = {"__new__", "__init_subclass__", "__set_name__", "__class_getitem__"}


def attribute_reads(files, intern, sets):
    """For each ALL_CAPS name, the tests that ran a line that reads it, if every read happens at test time.

    A class constant such as WEEK_OFFSET only matters to a test that reads it. A read counts when it is
    `x.NAME` or the string "NAME" (getattr, __dict__) inside a function body. If any read happens at import
    time (class body, module level, a metaclass __new__), the value may be folded into other attributes
    before any test runs, so the name is left out and callers fall back to the class's users.
    """
    reads = {}
    for top_dir in ("sqlglot", "tests"):
        for dirpath, _, names in os.walk(os.path.join(ROOT, top_dir)):
            for name in names:
                if not name.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), ROOT)
                with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                parent = {}
                for node in ast.walk(tree):
                    for child in ast.iter_child_nodes(node):
                        parent[child] = node

                def climb(node):
                    func = stmt = None
                    cur = node
                    while cur is not None:
                        if stmt is None and isinstance(cur, ast.stmt):
                            stmt = cur
                        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                            func = func or cur
                        cur = parent.get(cur)
                    return func, stmt

                for node in ast.walk(tree):
                    found = None
                    if isinstance(node, ast.Attribute) and CONSTANT.match(node.attr) and isinstance(node.ctx, ast.Load):
                        found = node.attr
                    elif isinstance(node, ast.Constant) and isinstance(node.value, str) and CONSTANT.match(node.value):
                        found = node.value
                    elif isinstance(node, ast.Name) and CONSTANT.match(node.id) and isinstance(node.ctx, ast.Load):
                        found = node.id  # only a class attribute when read inside a class body (import time)
                    if found is None:
                        continue
                    func, stmt = climb(node)
                    entry = reads.setdefault(found, {"unsafe": False, "lines": set()})
                    if isinstance(node, ast.Name) and func is not None:
                        continue  # a bare global inside a function is not a class attribute
                    if func is None or getattr(func, "name", "") in IMPORT_TIME_FUNCTIONS:
                        entry["unsafe"] = True
                        continue
                    first = min([stmt.lineno] + [d.lineno for d in getattr(stmt, "decorator_list", [])])
                    entry["lines"].update((rel, ln) for ln in range(first, stmt.end_lineno + 1))
    out = {}
    for name, entry in reads.items():
        if entry["unsafe"] or not entry["lines"]:
            continue
        members = set()
        for rel, ln in entry["lines"]:
            sid = files.get(rel, {}).get("lines", {}).get(str(ln))
            if sid is not None:
                members.update(sets[sid])
        out[name] = intern(members)
    return out


def wrapped_dicts():
    """The ALL_CAPS plain-dict class attributes that br_plugin wraps, as "module:Class|NAME"."""
    sys.path.insert(0, ROOT)
    from sqlglot import generator, parser, tokens
    from sqlglot.dialects.dialect import Dialect

    seen, out = set(), []
    for base in (tokens.Tokenizer, parser.Parser, generator.Generator, Dialect):
        stack = [base]
        while stack:
            cls = stack.pop()
            if cls in seen:
                continue
            seen.add(cls)
            stack.extend(cls.__subclasses__())
            for name, value in vars(cls).items():
                if type(value) is dict and re.match(r"^[A-Z][A-Z0-9_]*$", name):
                    out.append(f"{cls.__module__}:{cls.__qualname__}|{name}")
    return sorted(out)


def descendants():
    """class -> itself and every subclass, for the Tokenizer / Parser / Generator / Dialect families."""
    sys.path.insert(0, ROOT)
    from sqlglot import generator, parser, tokens
    from sqlglot.dialects.dialect import Dialect

    out = {}

    def walk(cls, acc):
        acc.append(cls)
        for sub in cls.__subclasses__():
            walk(sub, acc)

    for base in (tokens.Tokenizer, parser.Parser, generator.Generator, Dialect):
        everyone = []
        walk(base, everyone)
        for cls in everyone:
            subs = []
            walk(cls, subs)
            out[f"{cls.__module__}:{cls.__qualname__}"] = sorted({f"{k.__module__}:{k.__qualname__}" for k in subs})
    return out


def core_modules():
    """Files of the modules a bare `import sqlglot` loads. A subprocess test can only be hurt by these."""
    import subprocess

    out = subprocess.run(
        [sys.executable, "-c", "import sqlglot, sys; print('\\n'.join(sorted(m for m in sys.modules if m.startswith('sqlglot'))))"],
        env=dict(os.environ, PYTHONPATH=ROOT), capture_output=True, text=True, check=True).stdout.split()
    files = set()
    for module in out:
        base = os.path.join(*module.split("."))
        for candidate in (base + ".py", os.path.join(base, "__init__.py")):
            if os.path.exists(os.path.join(ROOT, candidate)):
                files.add(candidate)
    return sorted(files)


def class_owners():
    """A dialect-specific Tokenizer/Parser/Generator class belongs to the dialects that use it."""
    sys.path.insert(0, ROOT)
    from sqlglot.dialects import dialect as dialect_module

    for name in sorted(os.listdir(os.path.join(ROOT, "sqlglot", "dialects"))):
        if name.endswith(".py") and name not in ("__init__.py", "dialect.py"):
            __import__("sqlglot.dialects." + name[:-3])
    owners = {}
    base_modules = {"sqlglot.tokens", "sqlglot.parser", "sqlglot.generator", "sqlglot.jsonpath"}
    for cls in set(dialect_module.Dialect.classes.values()):
        owner = f"{cls.__module__}:{cls.__qualname__}"
        for attr in ("tokenizer_class", "jsonpath_tokenizer_class", "parser_class", "generator_class"):
            klass = getattr(cls, attr, None)
            if klass is None or klass.__module__ in base_modules:
                continue
            owners.setdefault(f"{klass.__module__}:{klass.__qualname__}", set()).add(owner)
    return {k: sorted(v) for k, v in sorted(owners.items())}


def main():
    ids = [l.strip() for l in open(os.path.join(OUT, "collected.txt")) if "::" in l]
    index = {name: i for i, name in enumerate(ids)}
    siblings = {}
    for name, i in index.items():
        cls = class_of(name)
        if cls:
            siblings.setdefault(cls, []).append(i)

    def sharers(name):
        """The tests that depend on what `name`'s setup did: its whole class, or just itself."""
        cls = class_of(name)
        return siblings[cls] if cls else [index[name]]

    sets, set_ids = [], {}

    def intern(members):
        key = tuple(sorted(members))
        if key not in set_ids:
            set_ids[key] = len(sets)
            sets.append(list(key))
        return set_ids[key]

    data = coverage.CoverageData(basename=os.path.join(OUT, ".coverage"))
    data.read()
    files = {}          # rel path -> {"lines": {lineno: set_id}, "import": [lineno...], "any": set_id}
    unknown_contexts = set()
    for path in sorted(data.measured_files()):
        rel = os.path.relpath(path, ROOT)
        if rel.startswith(".."):
            continue
        by_line = data.contexts_by_lineno(path)
        lines, import_lines, union = {}, [], set()
        for lineno, contexts in by_line.items():
            members = set()
            at_import = False
            for ctx in contexts:
                name = test_id(ctx)
                if name == "":
                    at_import = True
                elif name in index:
                    if phase(ctx) in ("setup", "teardown"):
                        members.update(sharers(name))  # class-level setup serves every test in the class
                    else:
                        members.add(index[name])
                else:
                    unknown_contexts.add(name)
            if at_import:
                import_lines.append(lineno)
            if members:
                lines[lineno] = intern(members)
                union |= members
        files[rel] = {
            "lines": {str(k): v for k, v in sorted(lines.items())},
            "import": sorted(import_lines),
            "any": intern(union) if union else None,
        }

    classes, opens, listdirs, spawners = {}, {}, {}, set()
    dict_keys, dict_iter = {}, {}
    seen_tests = set()
    for meta in glob.glob(os.path.join(OUT, "meta-*.jsonl")):
        for line in open(meta, encoding="utf-8"):
            rec = json.loads(line)
            if rec["id"] not in index:
                continue
            t = index[rec["id"]]
            seen_tests.add(t)
            if rec.get("spawns"):
                spawners.add(t)
            for c in rec["classes"]:
                classes.setdefault(c, set()).add(t)
            for o in rec["opens"]:
                opens.setdefault(o, set()).add(t)
            for d in rec["listdirs"]:
                listdirs.setdefault(d, set()).add(t)
            shared = sharers(rec["id"])
            for o, a, keys in rec.get("dict_keys", []):
                for k in keys:
                    dict_keys.setdefault(f"{o}|{a}|{k}", set()).add(t)
            for o, a in rec.get("dict_iter", []):
                dict_iter.setdefault(f"{o}|{a}", set()).add(t)
            for o, a, keys in rec.get("setup_dict_keys", []):
                for k in keys:
                    dict_keys.setdefault(f"{o}|{a}|{k}", set()).update(shared)
            for o, a in rec.get("setup_dict_iter", []):
                dict_iter.setdefault(f"{o}|{a}", set()).update(shared)
            for c in rec.get("setup_classes", []):
                classes.setdefault(c, set()).update(shared)
            for o in rec.get("setup_opens", []):
                opens.setdefault(o, set()).update(shared)
            for d in rec.get("setup_listdirs", []):
                listdirs.setdefault(d, set()).update(shared)

    importers_top, importers_local = import_graph()
    attribute_tests = attribute_reads(files, intern, sets)
    maps = {
        "tests": ids,
        "sets": sets,
        "files": files,
        "classes": {k: intern(v) for k, v in sorted(classes.items())},
        "opens": {k: intern(v) for k, v in sorted(opens.items())},
        "listdirs": {k: intern(v) for k, v in sorted(listdirs.items())},
        "class_owners": class_owners(),
        "attr_tests": attribute_tests,
        "dict_keys": {k: intern(v) for k, v in sorted(dict_keys.items())},
        "dict_iter": {k: intern(v) for k, v in sorted(dict_iter.items())},
        "descendants": descendants(),
        "wrapped_dicts": wrapped_dicts(),
        "spawn_tests": intern(spawners),
        "core_modules": core_modules(),
        "importers": importers_top,
        "local_importers": importers_local,
    }
    os.makedirs(os.path.dirname(MAPS), exist_ok=True)
    with gzip.open(MAPS, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(maps, f, separators=(",", ":"))
    print(f"tests {len(ids)} (with meta {len(seen_tests)}), files {len(files)}, sets {len(sets)}, "
          f"classes {len(classes)}, opens {len(opens)}, listdirs {len(listdirs)}")
    print(f"unknown contexts: {len(unknown_contexts)} {sorted(unknown_contexts)[:3]}")
    print(f"maps: {os.path.getsize(MAPS) / 1e6:.2f} MB")


if __name__ == "__main__":
    sys.exit(main())
