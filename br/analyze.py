"""Turn one patch into the set of tests that could fail because of it.

Everything here rests on maps measured on sqlglot at the pinned commit (see tools/build_maps.py):

  per-test line coverage     which tests executed which line
  class usage                which tests built an instance of which class (through the MRO)
  opened files / dirs        which tests read which file or listed which directory
  class owners, importers    which dialect a Tokenizer/Parser/Generator belongs to; who imports whom

and on the patch itself, never on any record of what a patch broke.

Rules, most precise first. "Tests" always means a set of test indexes (a bitmask):

  line inside a function body   the tests that executed the changed statement's own lines
  function header / decorator   the tests that executed any line of that function
  class body statement          the tests that built that class (or a subclass), plus its owner dialect
  module level statement        the tests that ran any line of the module, built any class defined in
                                it, or did the same for the modules that import it
  comment or blank lines only   the tests that read that file as text
  data file                     the tests that opened it
  anything we cannot read       the full suite, which can never miss
"""
from __future__ import annotations

import ast
import gzip
import json
import os
from collections import deque

from .patchparse import FilePatch, PatchError, apply_hunks
from .pytree import PyTree

CONFIG_NAMES = {"pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py",
                "requirements.txt", "requirements.lock"}


class FullSuite(Exception):
    """Raised when the only safe answer is every test."""


def module_of(rel: str) -> str:
    name = rel[:-3] if rel.endswith(".py") else rel
    parts = name.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class Maps:
    def __init__(self, path: str):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            raw = json.load(f)
        self.tests: list[str] = raw["tests"]
        self.full = (1 << len(self.tests)) - 1
        self._sets = raw["sets"]
        self._masks: dict[int, int] = {}
        self._files = raw["files"]
        self._lines: dict[str, dict[int, int]] = {}
        self._classes = raw["classes"]
        self._opens = raw["opens"]
        self._listdirs = raw["listdirs"]
        self.class_owners: dict[str, list[str]] = raw.get("class_owners", {})
        self.importers: dict[str, list[str]] = raw.get("importers", {})
        self.local_importers: dict[str, list[list]] = raw.get("local_importers", {})
        self.classes_by_module: dict[str, list[str]] = {}
        for full in self._classes:
            self.classes_by_module.setdefault(full.split(":", 1)[0], []).append(full)

    # ---------------------------------------------------------------- set helpers
    def mask(self, set_id) -> int:
        if set_id is None:
            return 0
        if set_id not in self._masks:
            m = 0
            for i in self._sets[set_id]:
                m |= 1 << i
            self._masks[set_id] = m
        return self._masks[set_id]

    def has_file(self, rel: str) -> bool:
        return rel in self._files

    def has_import_time(self, rel: str) -> bool:
        entry = self._files.get(rel)
        return bool(entry and entry["import"])

    def line_table(self, rel: str) -> dict[int, int]:
        if rel not in self._lines:
            entry = self._files.get(rel)
            self._lines[rel] = {int(k): v for k, v in entry["lines"].items()} if entry else {}
        return self._lines[rel]

    def lines_mask(self, rel: str, lines) -> int:
        table = self.line_table(rel)
        m = 0
        for ln in lines:
            sid = table.get(ln)
            if sid is not None:
                m |= self.mask(sid)
        return m

    def any_mask(self, rel: str) -> int:
        entry = self._files.get(rel)
        return self.mask(entry["any"]) if entry else 0

    def class_mask(self, full: str) -> int:
        return self.mask(self._classes.get(full))

    def opens_mask(self, rel: str) -> int:
        return self.mask(self._opens.get(rel))

    def listdir_mask(self, rel: str) -> int:
        m = 0
        parent = rel
        while True:
            parent = os.path.dirname(parent)
            m |= self.mask(self._listdirs.get(parent))
            if not parent:
                return m


class Analyzer:
    def __init__(self, maps: Maps, src_root: str):
        self.maps = maps
        self.root = src_root
        self.reasons: list[str] = []
        self._trees: dict[str, PyTree] = {}

    def note(self, text: str):
        self.reasons.append(text)

    def read(self, rel: str) -> str:
        path = os.path.join(self.root, rel)
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except (OSError, UnicodeDecodeError):
            raise FullSuite(f"cannot read {rel}")

    def old_tree(self, rel: str) -> PyTree:
        if rel not in self._trees:
            self._trees[rel] = PyTree(self.read(rel))
        return self._trees[rel]

    # ---------------------------------------------------------------- entry point
    def select(self, patches: list[FilePatch]) -> int:
        mask = 0
        for fp in patches:
            mask |= self.file_mask(fp)
            if mask == self.maps.full:
                break
        return mask

    def file_mask(self, fp: FilePatch) -> int:
        maps = self.maps
        path = fp.path
        if fp.binary:
            self.note(f"{path}: binary patch -> full suite")
            return maps.full
        base = os.path.basename(path)
        if base in CONFIG_NAMES or (fp.old_path and os.path.basename(fp.old_path) in CONFIG_NAMES):
            self.note(f"{path}: build or test configuration -> full suite")
            return maps.full
        is_py = path.endswith(".py") and path.startswith(("sqlglot/", "tests/"))
        if fp.old_path is None:  # a new file
            m = maps.listdir_mask(path)
            self.note(f"{path}: new file, tests that list its directory: {bin(m).count('1')}")
            return m
        if fp.new_path is None or fp.renamed:  # deleted or moved
            if is_py and path.startswith("sqlglot/"):
                self.note(f"{path}: package module removed -> full suite")
                return maps.full
            m = maps.opens_mask(fp.old_path) | maps.any_mask(fp.old_path) | maps.listdir_mask(fp.old_path)
            if is_py:
                m |= self.module_mask(fp.old_path)
            self.note(f"{fp.old_path}: removed or moved, tests {bin(m).count('1')}")
            return m
        if not fp.hunks:
            return 0  # mode change only
        if not is_py:
            m = maps.opens_mask(path)
            self.note(f"{path}: data file, tests that opened it: {bin(m).count('1')}")
            return m
        return self.python_mask(fp)

    # ---------------------------------------------------------------- python files
    def python_mask(self, fp: FilePatch) -> int:
        maps = self.maps
        rel = fp.path
        old_text = self.read(rel)
        try:
            new_text, edit = apply_hunks(old_text, fp.hunks)
        except PatchError as error:
            raise FullSuite(f"{rel}: {error}")
        otree = self.old_tree(rel)
        try:
            ntree = PyTree(new_text)
        except (SyntaxError, ValueError):
            if rel.startswith("sqlglot/"):
                if maps.has_import_time(rel):
                    self.note(f"{rel}: new source does not parse, module is imported eagerly -> full suite")
                    return maps.full
                m = self.module_mask(rel) | maps.opens_mask(rel)
            else:
                m = maps.any_mask(rel) | maps.opens_mask(rel)
            self.note(f"{rel}: new source does not parse, tests {bin(m).count('1')}")
            return m

        removed = [r for r in edit.removed if not otree.is_comment_or_blank(r)]
        added = [(a, p, n) for (a, p, n) in edit.added if not ntree.is_comment_or_blank(a)]
        if not removed and not added:
            m = maps.opens_mask(rel)
            self.note(f"{rel}: comments or blank lines only, tests that read the file: {bin(m).count('1')}")
            return m

        mask = 0
        for r in removed:
            mask |= self.old_line_mask(rel, otree, r)
        for a, prev_old, next_old in added:
            mask |= self.new_line_mask(rel, otree, ntree, a, prev_old, next_old)
        return mask

    def old_line_mask(self, rel: str, otree: PyTree, line: int) -> int:
        scope = otree.scope_of(line)
        m = self.scope_mask(rel, otree, scope, line, in_old=True)
        self.note(f"{rel}:{line} removed/changed ({scope.kind}) -> {bin(m).count('1')} tests")
        return m

    def new_line_mask(self, rel, otree, ntree, line, prev_old, next_old) -> int:
        scope = ntree.scope_of(line)
        if scope.kind in ("runtime", "funcheader"):
            qual = ntree.qualname(scope.func)
            old_fn = otree.functions.get(qual)
            while old_fn is None:
                # the function is new: fall back to the nearest enclosing function that already existed
                parent = next((a for a in ntree.enclosing(scope.func) if hasattr(a, "name")
                               and not isinstance(a, ast.Module)), None)
                if parent is None:
                    break
                if isinstance(parent, ast.ClassDef):
                    cls = ntree.qualname(parent)
                    m = self.class_mask(rel, cls)
                    self.note(f"{rel}:{line} new method in {cls} -> {bin(m).count('1')} tests")
                    return m
                qual = ntree.qualname(parent)
                old_fn = otree.functions.get(qual)
                scope.func = parent
            if old_fn is None:
                m = self.module_mask(rel)
                self.note(f"{rel}:{line} new top-level code -> {bin(m).count('1')} tests")
                return m
            m = self.neighbour_mask(rel, otree, old_fn, prev_old, next_old)
            self.note(f"{rel}:{line} added in {qual} ({scope.kind}) -> {bin(m).count('1')} tests")
            return m
        m = self.scope_mask(rel, ntree, scope, line, in_old=False)
        self.note(f"{rel}:{line} added ({scope.kind}) -> {bin(m).count('1')} tests")
        return m

    def neighbour_mask(self, rel, otree: PyTree, old_fn, prev_old: int, next_old: int) -> int:
        """Tests that reach an inserted line: those that executed a statement next to it."""
        maps = self.maps
        lo, hi = otree.start(old_fn), old_fn.end_lineno
        mask, found = 0, False
        for ln in (prev_old, next_old):
            stmt = otree.innermost(ln) if ln >= 1 else None
            if stmt is None or stmt is old_fn or not (lo <= otree.start(stmt) and stmt.end_lineno <= hi):
                continue
            found = True
            mask |= maps.lines_mask(rel, otree.own_lines(stmt))
        if not found:
            mask = maps.lines_mask(rel, range(lo, hi + 1))
        return mask

    # ---------------------------------------------------------------- scope rules
    def scope_mask(self, rel: str, tree: PyTree, scope, line: int, in_old: bool) -> int:
        maps = self.maps
        stmt = scope.stmt
        if self.type_checking_only(tree, stmt):
            return 0
        if scope.kind == "runtime":
            if in_old:
                return maps.lines_mask(rel, tree.own_lines(stmt))
            return maps.lines_mask(rel, range(PyTree.start(scope.func), scope.func.end_lineno + 1))
        if scope.kind == "funcheader":
            fn = scope.func
            return maps.lines_mask(rel, range(PyTree.start(fn), fn.end_lineno + 1))
        if scope.kind == "classheader":
            return self.class_mask(rel, scope.cls)
        if scope.kind == "classbody":
            if stmt is not None and tree.lambda_body_line(stmt, line):
                return maps.lines_mask(rel, range(PyTree.start(stmt), stmt.end_lineno + 1))
            m = self.class_mask(rel, scope.cls)
            if stmt is not None:
                m |= maps.lines_mask(rel, range(PyTree.start(stmt), stmt.end_lineno + 1))
            return m
        # module level
        if stmt is not None and isinstance(stmt, (ast.Import, ast.ImportFrom)) and maps.has_import_time(rel):
            raise FullSuite(f"{rel}:{line} import statement changed in an eagerly imported module")
        if stmt is not None and tree.lambda_body_line(stmt, line):
            return maps.lines_mask(rel, range(PyTree.start(stmt), stmt.end_lineno + 1))
        return self.module_mask(rel)

    @staticmethod
    def type_checking_only(tree: PyTree, stmt) -> bool:
        if stmt is None:
            return False
        for anc in [stmt, *tree.enclosing(stmt)]:
            if isinstance(anc, ast.If):
                test = anc.test
                name = test.attr if isinstance(test, ast.Attribute) else getattr(test, "id", "")
                if name == "TYPE_CHECKING" and stmt is not anc:
                    return True
        return False

    def class_mask(self, rel: str, qual: str) -> int:
        """Tests that built `qual` (or a subclass), its outer classes, and the dialects that own them."""
        maps = self.maps
        module = module_of(rel)
        parts = qual.split(".")
        mask = 0
        for i in range(1, len(parts) + 1):
            full = f"{module}:{'.'.join(parts[:i])}"
            mask |= maps.class_mask(full)
            for owner in maps.class_owners.get(full, []):
                mask |= maps.class_mask(owner)
        return mask

    def module_mask(self, rel: str) -> int:
        """Tests that ran code in the module, built its classes, or did so for its near importers."""
        maps = self.maps
        seen = {rel}
        queue = deque([(rel, 0)])
        mask = 0
        while queue:
            cur, depth = queue.popleft()
            mask |= maps.any_mask(cur)
            for full in maps.classes_by_module.get(module_of(cur), []):
                mask |= maps.class_mask(full)
                for owner in maps.class_owners.get(full, []):
                    mask |= maps.class_mask(owner)
            # a function that imports the module inside its body only matters to the tests that ran it
            for path, first, last in maps.local_importers.get(cur, []):
                mask |= maps.lines_mask(path, range(first, last + 1))
            if depth >= 2:
                continue
            for importer in maps.importers.get(cur, []):
                if importer not in seen and not importer.endswith("__init__.py"):
                    seen.add(importer)
                    queue.append((importer, depth + 1))
        return mask
