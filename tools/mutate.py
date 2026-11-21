"""Generate mutation patches against sqlglot at the pinned commit, for evaluating the selector.

These patches are for evaluation only. They are never shipped and never become a lookup table: the
selector sees only a patch, and the harness compares its choice with the real test outcome.

Usage: python tools/mutate.py <checkout> <out_dir> <count> <seed>
Writes <out_dir>/m001.patch ... plus <out_dir>/index.json (kind of each mutant).
"""
from __future__ import annotations

import ast
import difflib
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from br.pytree import PyTree  # noqa: E402

OPERATORS = [
    (" == ", " != "), (" != ", " == "), (" and ", " or "), (" or ", " and "), (" is not ", " is "),
    (" is ", " is not "), (" < ", " <= "), (" > ", " >= "), (" <= ", " < "), (" >= ", " > "),
    (" + ", " - "), (" - ", " + "), ("True", "False"), ("False", "True"), (" in ", " not in "),
    (".lower()", ".upper()"), (".upper()", ".lower()"), ("not ", ""),
]


def mutate_line(line: str, rng: random.Random):
    code = line.split("#")[0]
    options = [(a, b) for a, b in OPERATORS if a in code]
    number = re.search(r"(?<![\w.\"'])(\d+)(?![\w.\"'])", code)
    choices = [("op", o) for o in options] + ([("num", number)] if number else [])
    if not choices:
        return None
    kind, value = rng.choice(choices)
    if kind == "op":
        a, b = value
        positions = [m.start() for m in re.finditer(re.escape(a), line)]
        pos = rng.choice(positions)
        return line[:pos] + b + line[pos + len(a):]
    n = value
    return line[:n.start()] + str(int(n.group(1)) + 1) + line[n.end():]


def diff(rel: str, old: str, new: str) -> str:
    body = difflib.unified_diff(old.split("\n"), new.split("\n"), f"a/{rel}", f"b/{rel}", lineterm="", n=3)
    return f"diff --git a/{rel} b/{rel}\n" + "\n".join(body) + "\n"


def mutate_fixture_line(line: str, rng: random.Random):
    """A small edit to one SQL fixture line: bump a number, swap AND/OR, or glue a stray letter on a word."""
    if not line.strip() or line.startswith(("--", "#")):
        return None
    options = []
    if re.search(r"\d", line):
        options.append("digit")
    if " AND " in line or " OR " in line:
        options.append("logic")
    words = [m for m in re.finditer(r"\b[a-z_]{2,}\b", line)]
    if words:
        options.append("word")
    if not options:
        return None
    pick = rng.choice(options)
    if pick == "digit":
        m = rng.choice(list(re.finditer(r"\d", line)))
        return line[:m.start()] + str((int(m.group()) + 1) % 10) + line[m.end():]
    if pick == "logic":
        return line.replace(" AND ", " OR ", 1) if " AND " in line else line.replace(" OR ", " AND ", 1)
    m = rng.choice(words)
    return line[:m.end()] + "x" + line[m.end():]


def main_extra(root: str, out: str, count: int, seed: int, mode: str):
    """mode 'tests': operator edits in tests/*.py. mode 'fixtures': edits to tests/fixtures/optimizer/*.sql."""
    rng = random.Random(seed)
    os.makedirs(out, exist_ok=True)
    top = "tests" if mode == "tests" else os.path.join("tests", "fixtures", "optimizer")
    files = []
    for dirpath, _, names in os.walk(os.path.join(root, top)):
        files += [os.path.relpath(os.path.join(dirpath, n), root) for n in names
                  if n.endswith(".py" if mode == "tests" else ".sql") and "__pycache__" not in dirpath]
    files.sort()
    index, attempts = {}, 0
    prefix = "t" if mode == "tests" else "f"
    while len(index) < count and attempts < count * 300:
        attempts += 1
        rel = rng.choice(files)
        old = open(os.path.join(root, rel), encoding="utf-8").read()
        lines = old.split("\n")
        n = rng.randrange(1, len(lines) + 1)
        if mode == "tests":
            tree = PyTree(old)
            if tree.is_comment_or_blank(n) or tree.in_multiline_string(n):
                continue
            mutated = mutate_line(lines[n - 1], rng)
            scope = tree.scope_of(n).kind
        else:
            mutated = mutate_fixture_line(lines[n - 1], rng)
            scope = "data"
        if mutated is None or mutated == lines[n - 1]:
            continue
        new_lines = lines[:]
        new_lines[n - 1] = mutated
        new = "\n".join(new_lines)
        if mode == "tests":
            try:
                ast.parse(new)
            except SyntaxError:
                continue
        name = f"{prefix}{len(index) + 1:03d}"
        with open(os.path.join(out, name + ".patch"), "w", encoding="utf-8") as f:
            f.write(diff(rel, old, new))
        index[name] = {"file": rel, "line": n, "kind": mode, "scope": scope}
    with open(os.path.join(out, "index.json"), "w") as f:
        json.dump(index, f, indent=1)
    print(f"wrote {len(index)} {mode} mutants to {out}")


def main(root: str, out: str, count: int, seed: int):
    rng = random.Random(seed)
    os.makedirs(out, exist_ok=True)
    files = []
    for top in ("sqlglot",):
        for dirpath, _, names in os.walk(os.path.join(root, top)):
            files += [os.path.relpath(os.path.join(dirpath, n), root) for n in names if n.endswith(".py")]
    files.sort()
    index = {}
    attempts = 0
    while len(index) < count and attempts < count * 200:
        attempts += 1
        want = "classlevel" if len(index) % 2 == 0 else "function"
        rel = rng.choice(files)
        old = open(os.path.join(root, rel), encoding="utf-8").read()
        tree = PyTree(old)
        lines = old.split("\n")
        n = rng.randrange(1, len(lines) + 1)
        if tree.is_comment_or_blank(n) or tree.in_multiline_string(n):
            continue
        scope = tree.scope_of(n)
        kind = "function" if scope.kind == "runtime" else "classlevel" if scope.kind in ("classbody", "module") else None
        if kind != want:
            continue
        mutated = mutate_line(lines[n - 1], rng)
        if mutated is None or mutated == lines[n - 1]:
            continue
        new_lines = lines[:]
        new_lines[n - 1] = mutated
        new = "\n".join(new_lines)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        name = f"m{len(index) + 1:03d}"
        with open(os.path.join(out, name + ".patch"), "w", encoding="utf-8") as f:
            f.write(diff(rel, old, new))
        index[name] = {"file": rel, "line": n, "kind": kind, "scope": scope.kind}
    with open(os.path.join(out, "index.json"), "w") as f:
        json.dump(index, f, indent=1)
    print(f"wrote {len(index)} mutants to {out}")


if __name__ == "__main__":
    if len(sys.argv) > 5:
        main_extra(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5])
    else:
        main(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
