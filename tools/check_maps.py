"""Flag where per-test coverage cannot be trusted for class-body lines.

Class-body statements run once, at import. Dialect modules are imported lazily inside the first test that
uses them, so coverage credits their class-body lines to that test alone (a few tests), while the class
may have hundreds of users. The selector therefore never uses coverage for class-body or module-level
lines: it classifies each line from the AST of the pinned file and widens to the class's users (the class
map) or the module's importers. This script lists every class-body line where coverage disagrees with the
class map, so the difference is visible and checked, not assumed.

Usage: python tools/check_maps.py <maps.json.gz> <sqlglot checkout>
"""
from __future__ import annotations

import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from br.analyze import Maps, module_of  # noqa: E402
from br.pytree import PyTree  # noqa: E402


def popcount(mask: int) -> int:
    return bin(mask).count("1")


def main(maps_path: str, root: str) -> int:
    maps = Maps(maps_path)
    flagged: dict[str, list[tuple[int, int, int, str]]] = {}
    checked = 0
    for rel in sorted(maps._files):
        if not rel.startswith("sqlglot/") or not rel.endswith(".py"):
            continue
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            tree = PyTree(f.read())
        module = module_of(rel)
        table = maps.line_table(rel)
        for _, _, _, node in tree.stmts:
            if not isinstance(node, ast.ClassDef):
                continue
            qual = tree.qualname(node)
            users = 0
            parts = qual.split(".")
            for i in range(1, len(parts) + 1):
                full = f"{module}:{'.'.join(parts[:i])}"
                users |= maps.class_mask(full)
                for owner in maps.class_owners.get(full, []):
                    users |= maps.class_mask(owner)
            n_users = popcount(users)
            if n_users == 0:
                continue
            for child in tree.child_stmts(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue  # method bodies run at test time; nested classes are checked on their own
                for line in tree.own_lines(child):
                    sid = table.get(line)
                    if sid is None:
                        continue
                    checked += 1
                    cov = popcount(maps.mask(sid))
                    if cov < n_users:
                        flagged.setdefault(rel, []).append((line, cov, n_users, qual))
    total = sum(len(v) for v in flagged.values())
    print(f"class-body lines with test coverage: {checked}")
    print(f"flagged (coverage credits fewer tests than the class has users): {total} lines in {len(flagged)} files")
    for rel, items in sorted(flagged.items(), key=lambda kv: -len(kv[1]))[:12]:
        line, cov, users, qual = items[0]
        print(f"  {rel}: {len(items)} lines, e.g. {qual} line {line}: coverage {cov} tests, class users {users}")
    print("The selector does not use coverage for these lines; it uses the class users above.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
