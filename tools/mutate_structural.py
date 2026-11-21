"""Structural and non-operator mutants against the pinned sqlglot, for evaluation only.

Kinds: docstring edits, SQL fixture comment and meta-comment edits, string-constant edits, default-argument
edits, new methods that override a parent, new dict entries, base-class edits, decorator removal, annotation
edits, new files, and two-file patches.  Usage: python tools/mutate_structural.py <checkout> <out> <seed>
"""
from __future__ import annotations

import ast
import difflib
import json
import os
import random
import re
import sys

root, out, seed = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(seed)
os.makedirs(out, exist_ok=True)


def read(rel):
    return open(os.path.join(root, rel), encoding="utf-8").read()


def diff(rel, old, new):
    body = difflib.unified_diff(old.split("\n"), new.split("\n"), f"a/{rel}", f"b/{rel}", lineterm="", n=3)
    return f"diff --git a/{rel} b/{rel}\n" + "\n".join(body) + "\n"


def py_files(top):
    found = []
    for d, _, names in os.walk(os.path.join(root, top)):
        found += [os.path.relpath(os.path.join(d, n), root) for n in names if n.endswith(".py")]
    return sorted(found)


SRC = py_files("sqlglot")
index, patches = {}, {}


def add(kind, text, note):
    name = f"s{len(patches) + 1:03d}"
    patches[name] = text
    index[name] = {"file": note, "line": 0, "kind": kind, "scope": kind}


def edit_lines(rel, fn):
    old = read(rel)
    lines = old.split("\n")
    new = fn(lines)
    if new is None:
        return None
    new_text = "\n".join(new)
    try:
        ast.parse(new_text)
    except SyntaxError:
        return None
    return old, new_text


def try_many(kind, count, make):
    done, tries = 0, 0
    while done < count and tries < 400:
        tries += 1
        r = make()
        if r:
            add(kind, r[0], r[1])
            done += 1


def docstring():
    rel = rng.choice(SRC)
    old = read(rel)
    tree = ast.parse(old)
    nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef))
             and n.body and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)
             and isinstance(n.body[0].value.value, str)]
    if not nodes:
        return None
    node = rng.choice(nodes).body[0]
    lines = old.split("\n")
    ln = node.end_lineno - 1
    if node.end_lineno == node.lineno:
        lines[ln] = lines[ln].replace('"""', ' EDITED """', 1) if lines[ln].count('"""') >= 2 else lines[ln]
    else:
        lines[ln] = lines[ln].replace('"""', 'EDITED """', 1)
    if lines[ln] == old.split("\n")[ln]:
        return None
    new = "\n".join(lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return diff(rel, old, new), rel


def fixture_comment():
    rel = rng.choice([f"tests/fixtures/optimizer/{n}" for n in sorted(os.listdir(os.path.join(root, "tests/fixtures/optimizer"))) if n.endswith(".sql")])
    old = read(rel)
    lines = old.split("\n")
    cands = [i for i, l in enumerate(lines) if l.startswith("#") or l.startswith("--")]
    if not cands:
        return None
    i = rng.choice(cands)
    l = lines[i]
    if "execute: true" in l:
        lines[i] = l.replace("true", "false")
    elif "execute: false" in l:
        lines[i] = l.replace("false", "true")
    else:
        lines[i] = l + " edited"
    return diff(rel, old, "\n".join(lines)), rel


def string_constant():
    rel = rng.choice([f for f in SRC if "/generators/" in f or "/parsers/" in f or "/optimizer/" in f or f.endswith("generator.py")])
    old = read(rel)
    tree = ast.parse(old)
    lines = old.split("\n")
    doc_lines = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef)) and n.body and isinstance(n.body[0], ast.Expr):
            doc_lines.update(range(n.body[0].lineno, n.body[0].end_lineno + 1))
    cands = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and n.lineno == n.end_lineno and 2 <= len(n.value) < 30 and n.lineno not in doc_lines
             and lines[n.lineno - 1][n.col_offset] in "\"'" and not lines[n.lineno - 1].lstrip().startswith("#")]
    if not cands:
        return None
    n = rng.choice(cands)
    line = lines[n.lineno - 1]
    q = line[n.col_offset]
    end = n.end_col_offset - 1
    if line[end] != q:
        return None
    lines[n.lineno - 1] = line[:end] + "Z" + line[end:]
    new = "\n".join(lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return diff(rel, old, new), rel


def default_arg():
    rel = rng.choice(SRC)
    old = read(rel)
    lines = old.split("\n")
    cands = [i for i, l in enumerate(lines) if l.lstrip().startswith("def ") and re.search(r"=(True|False|None|\d+)[,)]", l)]
    if not cands:
        return None
    i = rng.choice(cands)
    m = rng.choice(list(re.finditer(r"=(True|False|None|\d+)(?=[,)])", lines[i])))
    swap = {"True": "False", "False": "True", "None": "False"}
    val = m.group(1)
    repl = swap.get(val, str(int(val) + 1) if val.isdigit() else val)
    lines[i] = lines[i][: m.start(1)] + repl + lines[i][m.end(1):]
    new = "\n".join(lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return diff(rel, old, new), rel


def new_method():
    rel = rng.choice([f for f in SRC if "/generators/" in f and not f.endswith("__init__.py")])
    old = read(rel)
    tree = ast.parse(old)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name.endswith("Generator")]
    if not classes:
        return None
    cls = classes[0]
    first = cls.body[0]
    at = first.end_lineno if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) else cls.lineno
    lines = old.split("\n")
    method = ["", "    def limit_sql(self, expression, top=False):", "        return \"LIMIT 1\"", ""]
    lines[at:at] = method
    new = "\n".join(lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return diff(rel, old, new), rel


def new_dict_entry():
    rel = rng.choice([f for f in SRC if "/parsers/" in f and not f.endswith("__init__.py")])
    old = read(rel)
    lines = old.split("\n")
    cands = [i for i, l in enumerate(lines) if re.match(r"^    FUNCTIONS = \{\s*$", l) or re.match(r"^    FUNCTIONS = \{\*\*", l)]
    if not cands:
        return None
    i = cands[0]
    if lines[i].rstrip().endswith("{"):
        lines[i + 1:i + 1] = ['        "ZZZ_FN": lambda args: exp.Anonymous(this="ZZZ_FN", expressions=args),']
    else:
        return None
    new = "\n".join(lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return diff(rel, old, new), rel


def base_class():
    rel = rng.choice([f for f in SRC if "/dialects/" in f and not f.endswith("__init__.py")])
    old = read(rel)
    lines = old.split("\n")
    cands = [i for i, l in enumerate(lines) if re.match(r"^class \w+\(\w+\):\s*$", l)]
    if not cands:
        return None
    i = rng.choice(cands)
    lines[i] = re.sub(r"\((\w+)\):", r"(\1, object):", lines[i])
    new = "\n".join(lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return diff(rel, old, new), rel


def drop_decorator():
    rel = rng.choice(SRC)
    old = read(rel)
    lines = old.split("\n")
    cands = [i for i, l in enumerate(lines) if l.strip() in ("@classmethod", "@staticmethod")]
    if not cands:
        return None
    i = rng.choice(cands)
    del lines[i]
    new = "\n".join(lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return diff(rel, old, new), rel


def annotation():
    rel = rng.choice(SRC)
    old = read(rel)
    lines = old.split("\n")
    cands = [i for i, l in enumerate(lines) if l.lstrip().startswith("def ") and l.rstrip().endswith("-> bool:")]
    if not cands:
        return None
    i = rng.choice(cands)
    lines[i] = lines[i].replace("-> bool:", "-> int:")
    return diff(rel, "\n".join(old.split("\n")), "\n".join(lines)), rel


try_many("docstring", 3, docstring)
try_many("fixture_comment", 3, fixture_comment)
try_many("string_constant", 4, string_constant)
try_many("default_arg", 3, default_arg)
try_many("new_method", 2, new_method)
try_many("new_dict_entry", 2, new_dict_entry)
try_many("base_class", 2, base_class)
try_many("drop_decorator", 2, drop_decorator)
try_many("annotation", 2, annotation)
# new files (inert on their own) and a two-file patch
add("new_file_pkg", "diff --git a/sqlglot/brand_new.py b/sqlglot/brand_new.py\nnew file mode 100644\n--- /dev/null\n+++ b/sqlglot/brand_new.py\n@@ -0,0 +1,2 @@\n+def f():\n+    return 1\n", "sqlglot/brand_new.py")
a, b = patches["s004"], patches["s001"]
add("multi_file", a + b, "two files")
for name, text in patches.items():
    open(os.path.join(out, name + ".patch"), "w", encoding="utf-8").write(text)
json.dump(index, open(os.path.join(out, "index.json"), "w"), indent=1)
print(len(patches), "structural mutants:", {k: v["kind"] for k, v in index.items()})
