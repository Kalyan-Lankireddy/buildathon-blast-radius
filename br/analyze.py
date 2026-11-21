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

from .fixtures import changed_field
from .patchparse import FilePatch, PatchError, apply_hunks
from .pytree import PyTree

CONFIG_NAMES = {"pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py",
                "requirements.txt", "requirements.lock"}


MAX_CHANGED_LINES = 60  # changed code lines in one file before we stop analysing and run everything


class _Masker(ast.NodeTransformer):
    """Replace every constant by its type, so two statements compare equal when only literal values differ."""

    def visit_Constant(self, node):
        return ast.copy_location(ast.Constant(value=f"<{type(node.value).__name__}>"), node)


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
        self._index = {name: i for i, name in enumerate(self.tests)}
        fields_path = os.path.join(os.path.dirname(path), "fields.json")
        self._fields: dict[str, dict[str, list[str]]] = {}
        if os.path.exists(fields_path):
            with open(fields_path, encoding="utf-8") as f:
                self._fields = json.load(f).get("files", {})
        self.full = (1 << len(self.tests)) - 1
        self._sets = raw["sets"]
        self._masks: dict[int, int] = {}
        self._files = raw["files"]
        self._lines: dict[str, dict[int, int]] = {}
        self._classes = raw["classes"]
        self._opens = raw["opens"]
        self._listdirs = raw["listdirs"]
        self._attr_tests: dict[str, int] = raw.get("attr_tests", {})
        self._dict_keys: dict[str, int] = raw.get("dict_keys", {})
        self._dict_iter: dict[str, int] = raw.get("dict_iter", {})
        self._wrapped = set(raw.get("wrapped_dicts", []))
        self.dispatch_recorded: bool = bool(raw.get("dispatch_recorded", False))
        self._descendants: dict[str, list[str]] = raw.get("descendants", {})
        self._spawners = self.mask(raw.get("spawn_tests"))  # tests that start a subprocess
        self._core = set(raw.get("core_modules", []))  # files that a bare `import sqlglot` loads
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

    def field_readers(self, path: str, field: str) -> int | None:
        """Tests that read `field` (sql or expected) of a pair-format fixture; None when the map says nothing."""
        entry = self._fields.get(path)
        if entry is None or field not in entry:
            return None
        mask = 0
        for test_id in entry[field]:
            if test_id in self._index:
                mask |= 1 << self._index[test_id]
        return mask

    def has_file(self, rel: str) -> bool:
        return rel in self._files

    def is_core(self, rel: str) -> bool:
        """True when a bare `import sqlglot` loads this module, so a crash in it fails every test."""
        return rel in self._core

    def spawners_for(self, rel: str) -> int:
        """Subprocess tests import sqlglot fresh, so only a change in a module that loads then can hurt them."""
        return self._spawners if rel in self._core else 0

    def import_lines(self, rel: str) -> set[int]:
        entry = self._files.get(rel)
        return set(entry["import"]) if entry else set()

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

    def dict_entry_mask(self, cls_full: str, name: str, key: str) -> int | None:
        """Tests that looked up `key` in dict `name` of the class or a subclass, or walked that dict.

        None when no such dict was recorded, so the caller keeps the wider rule.
        """
        mask, recorded = 0, False
        for owner in self._descendants.get(cls_full, [cls_full]):
            if f"{owner}|{name}" not in self._wrapped:
                continue
            recorded = True
            mask |= self.mask(self._dict_keys.get(f"{owner}|{name}|{key}"))
            mask |= self.mask(self._dict_iter.get(f"{owner}|{name}"))
        return mask if recorded else None

    def attr_mask(self, name: str) -> int | None:
        """Tests that ran a line reading class constant `name`; None when the name cannot be narrowed."""
        set_id = self._attr_tests.get(name)
        return None if set_id is None else self.mask(set_id)

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
            if path.startswith("sqlglot/"):
                # A new package file could be picked up by a directory scan or an import we cannot see.
                self.note(f"{path}: new file in the package -> full suite")
                return maps.full
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
            narrowed = self.fixture_field_mask(fp)
            if narrowed is not None:
                self.note(f"{path}: data file, only the tests that read the edited field: {bin(narrowed).count('1')} "
                          f"of the {bin(m).count('1')} that opened it")
                return narrowed
            self.note(f"{path}: data file, tests that opened it: {bin(m).count('1')}")
            return m
        return self.python_mask(fp)

    def fixture_field_mask(self, fp: FilePatch) -> int | None:
        """For an edit that plainly changes one half of a pair-format fixture: the tests that read that half.

        None (so the caller keeps "every test that opened the file") unless the fixture is in the field map and the
        edit is certain to sit in one half. See br/fixtures.py and tools/build_fields.py.
        """
        path = fp.path
        if not path.endswith(".sql") or path not in self.maps._fields:
            return None
        try:
            old_text = self.read(path)
            new_text, edit = apply_hunks(old_text, fp.hunks)
        except (FullSuite, PatchError):
            return None
        field = changed_field(old_text, new_text, edit.removed, [a for a, _, _ in edit.added])
        return None if field is None else self.maps.field_readers(path, field)

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
                if maps.is_core(rel):
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

        if len(removed) + len(added) > MAX_CHANGED_LINES:
            # A refactor this large will break a lot, and looking up every line costs CPU seconds.
            raise FullSuite(f"{rel}: {len(removed) + len(added)} changed lines")
        mask = 0
        for r in removed:
            mask |= self.old_line_mask(rel, otree, r)
        replaced, pair_of = self.replacement_slots(otree, ntree, edit, added)
        if self.import_time_risk(otree, ntree, removed, added, pair_of):
            # A change that runs while the module loads can crash the import. In a module that
            # `import sqlglot` loads that fails every test; elsewhere it fails the tests that import it.
            if maps.is_core(rel):
                raise FullSuite(f"{rel}: import-time statement that is not a plain literal, in a core module")
            mask |= self.module_mask(rel, depth_limit=None) | maps.spawners_for(rel)
            self.note(f"{rel}: import-time statement that is not a plain literal -> module users")
        for a, prev_old, next_old in added:
            if a in replaced and ntree.scope_of(a).kind != "classbody":
                continue  # takes the place of a removed line: that line's own tests already count
            # (in a class body the new line can name a different dict key than the old one, so it is mapped too)
            mask |= self.new_line_mask(rel, otree, ntree, a, prev_old, next_old)
        return mask

    @staticmethod
    def replacement_slots(otree: PyTree, ntree: PyTree, edit, added) -> tuple[set[int], dict[int, int]]:
        """Added lines that replace a removed line, in the same kind of place.

        A replaced line sits where the old line sat, so the same tests reach it. Only genuine
        insertions (more added than removed lines, or a different kind of place) need the
        neighbour rule, which is wider.
        """
        # only real code can stand in for the line that replaces it: a removed comment selects nothing
        removed = {r for r in edit.removed if not otree.is_comment_or_blank(r)}
        by_slot: dict[int, list[int]] = {}
        for a, prev_old, _ in added:
            by_slot.setdefault(prev_old, []).append(a)
        slots: set[int] = set()
        paired: dict[int, int] = {}  # new line -> the old line it replaces
        for prev_old, new_lines in by_slot.items():
            block, p = [], prev_old
            while p in removed:
                block.append(p)
                p -= 1
            block.reverse()  # the run of removed lines this group of added lines follows
            for old_line, new_line in zip(block, new_lines):
                o, n = otree.scope_of(old_line), ntree.scope_of(new_line)
                if o.kind != n.kind:
                    continue
                if o.kind in ("runtime", "funcheader"):
                    same = otree.qualname(o.func) == ntree.qualname(n.func)
                elif o.kind in ("classbody", "classheader"):
                    same = o.cls == n.cls
                else:
                    same = True
                if same:
                    slots.add(new_line)
                    paired[new_line] = old_line
        return slots, paired

    IMPORT_KINDS = ("classbody", "classheader", "funcheader", "module")

    def import_time_risk(self, otree: PyTree, ntree: PyTree, removed, added, pair_of: dict[int, int]) -> bool:
        """True when a change could make the module fail to import.

        Statements outside function bodies run when the module loads. A plain literal (numbers,
        strings, containers of them) cannot fail. Anything else can: a name that no longer exists,
        arithmetic on the wrong types, a base class that changed. A deleted statement can leave
        a name undefined.
        """
        paired_old = set(pair_of.values())
        for r in removed:
            if r in paired_old:
                continue
            sc = otree.scope_of(r)
            if sc.kind in self.IMPORT_KINDS and not self.harmless_here(otree, sc, r):
                return True
        for a, _, _ in added:
            sc = ntree.scope_of(a)
            if sc.kind not in self.IMPORT_KINDS or self.harmless_here(ntree, sc, a) or self.literal_only(sc.stmt):
                continue
            old_line = pair_of.get(a)
            if old_line is not None:
                old_stmt = otree.scope_of(old_line).stmt
                # In `NAME = {...}` only the entry that changed matters; the other entries are untouched.
                old_node, new_node = self.entry_at(old_stmt, old_line), self.entry_at(sc.stmt, a)
                if old_node is None or new_node is None:
                    old_node, new_node = old_stmt, sc.stmt
                if self.only_literals_changed(old_node, new_node):
                    continue
            return True
        return False

    @staticmethod
    def entry_at(stmt, line: int):
        """The `(key, value)` of the dict-literal entry holding `line`, as one node, or None."""
        value = getattr(stmt, "value", None)
        if not isinstance(value, ast.Dict):
            return None
        for key, val in zip(value.keys, value.values):
            if key is not None and key.lineno <= line <= val.end_lineno:
                return ast.Tuple(elts=[key, val], ctx=ast.Load(), lineno=key.lineno, col_offset=0,
                                 end_lineno=val.end_lineno, end_col_offset=0)
        return None

    CLOSURE_BUILDERS = {"rename_func"}  # calls that only build a function and never look at their argument

    @classmethod
    def only_literals_changed(cls, old_stmt, new_stmt) -> bool:
        """The new statement is the old one with different literal values, and runs no call that could fail.

        Same structure (every constant masked by its type) means no name, attribute or operator changed, so the
        statement cannot start failing on a missing name. Any call it makes while the module loads must be a
        closure builder: `rename_func("X")` ignores its argument, but `maybe_parse("...")` parses it, which is how a
        string edit once broke an import. Calls inside a lambda do not run at import.
        """
        if old_stmt is None or new_stmt is None:
            return False

        def shape(node):
            return ast.dump(_Masker().visit(ast.parse(ast.unparse(node)).body[0]))

        try:
            if shape(old_stmt) != shape(new_stmt):
                return False
        except Exception:
            return False
        stack = [new_stmt]
        while stack:
            node = stack.pop()
            if isinstance(node, ast.Lambda):
                continue
            if isinstance(node, ast.Call):
                f = node.func
                if (f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)) not in cls.CLOSURE_BUILDERS:
                    return False
            stack.extend(ast.iter_child_nodes(node))
        return True

    def harmless_here(self, tree: PyTree, scope, line: int) -> bool:
        stmt = scope.stmt
        if stmt is None:
            return False
        return self.type_checking_only(tree, stmt) or tree.lambda_body_line(stmt, line)

    @staticmethod
    def literal_only(stmt) -> bool:
        """An assignment whose value is built only from constants and containers of constants."""
        if isinstance(stmt, ast.Assign):
            value = stmt.value
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            value = stmt.value
        else:
            return False
        if value is None:
            return True
        allowed = (ast.Constant, ast.Dict, ast.List, ast.Tuple, ast.Set, ast.UnaryOp, ast.UAdd, ast.USub,
                   ast.Not, ast.Invert, ast.Load)
        return all(isinstance(node, allowed) for node in ast.walk(value))

    @staticmethod
    def bare_string(stmt) -> bool:
        return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)

    def old_line_mask(self, rel: str, otree: PyTree, line: int) -> int:
        scope = otree.scope_of(line)
        m = self.scope_mask(rel, otree, scope, line, in_old=True)
        self.note(f"{rel}:{line} removed/changed ({scope.kind}) -> {bin(m).count('1')} tests")
        return m

    def new_line_mask(self, rel, otree, ntree, line, prev_old, next_old) -> int:
        scope = ntree.scope_of(line)
        if self.bare_string(scope.stmt):
            return self.maps.opens_mask(rel)  # see scope_mask: a docstring is read by nothing
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
                    # A method is only called on an instance of its class or a subclass, and every test that
                    # built one is a recorded user. The mutant that suggested widening this (SparkGenerator
                    # limit_sql) failed only tests whose xdist worker crashed under load; its real failures
                    # were all inside the recorded users.
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
        touched = set(range(lo, hi + 1)) if not found else set()
        for ln in (prev_old, next_old):
            stmt = otree.innermost(ln) if ln >= 1 else None
            if stmt is not None and stmt is not old_fn:
                touched.update(otree.own_lines(stmt))
        if maps.import_lines(rel).intersection(touched):
            mask |= self.module_mask(rel) | maps.spawners_for(rel)
        return mask

    # ---------------------------------------------------------------- scope rules
    def scope_mask(self, rel: str, tree: PyTree, scope, line: int, in_old: bool) -> int:
        maps = self.maps
        stmt = scope.stmt
        if self.type_checking_only(tree, stmt):
            return 0
        if self.bare_string(stmt):
            # A docstring or other bare string statement runs no code. Coverage cannot see it, and nothing
            # reads it as data: the suite has no doctests (tests/test_docs.py defines no pytest tests) and
            # `__doc__` is only assigned in sqlglot/expressions/core.py, never inspected. Mutants that edit
            # docstrings failed no test. So it counts like a comment: only tests that read the file as text.
            return maps.opens_mask(rel)
        if scope.kind == "runtime":
            if in_old:
                lines = tree.own_lines(stmt)
                m = maps.lines_mask(rel, lines)
                if maps.import_lines(rel).intersection(lines):
                    # this line also runs while modules load, so what it computes feeds every user of them
                    m |= self.module_mask(rel) | maps.spawners_for(rel)
                return m
            return maps.lines_mask(rel, range(PyTree.start(scope.func), scope.func.end_lineno + 1))
        if scope.kind == "funcheader":
            fn = scope.func
            return maps.lines_mask(rel, range(PyTree.start(fn), fn.end_lineno + 1)) | maps.spawners_for(rel)
        if scope.kind == "classheader":
            return self.class_mask(rel, scope.cls) | maps.spawners_for(rel)
        if scope.kind == "classbody":
            if stmt is not None and tree.lambda_body_line(stmt, line):
                return maps.lines_mask(rel, range(PyTree.start(stmt), stmt.end_lineno + 1))
            m = self.class_mask(rel, scope.cls)
            if stmt is not None:
                narrowed = self.attribute_readers(tree, stmt)
                entry = self.dict_entry_readers(rel, tree, stmt, line, scope.cls)
                if narrowed is not None:
                    # a test must use the class AND read the constant (and, for one dict entry, look it up)
                    m &= narrowed if entry is None else entry
                elif entry is not None and maps.dispatch_recorded and self.is_transforms(stmt):
                    # TRANSFORMS is merged into subclasses at import, so the generic read test leaves it out,
                    # but every lookup goes through a recorded dict: the class table or its cached dispatch copy.
                    m &= entry
                m |= maps.lines_mask(rel, range(PyTree.start(stmt), stmt.end_lineno + 1))
            return m | maps.spawners_for(rel)
        # module level
        if stmt is not None and isinstance(stmt, (ast.Import, ast.ImportFrom)) and maps.is_core(rel):
            raise FullSuite(f"{rel}:{line} import statement changed in a module that `import sqlglot` loads")
        if stmt is not None and tree.lambda_body_line(stmt, line):
            return maps.lines_mask(rel, range(PyTree.start(stmt), stmt.end_lineno + 1))
        return self.module_mask(rel) | maps.spawners_for(rel)

    def attribute_readers(self, tree: PyTree, stmt) -> int | None:
        """For `NAME = ...` directly in a class body: the tests that ran a line reading NAME.

        Only when every name assigned has a map entry, which exists only when all of its reads happen at
        test time (see tools/build_maps.py). Anything else returns None and the caller keeps the wider
        rule of all users of the class.
        """
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            targets = [stmt.target]
        else:
            return None
        if not isinstance(tree.parent.get(id(stmt)), ast.ClassDef):
            return None
        mask = 0
        for target in targets:
            if not isinstance(target, ast.Name):
                return None
            readers = self.maps.attr_mask(target.id)
            if readers is None:
                return None
            mask |= readers
        return mask

    def dict_entry_readers(self, rel: str, tree: PyTree, stmt, line: int, qual: str) -> int | None:
        """For an edit inside one entry of `NAME = {key: value, ...}`: the tests that looked that key up."""
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
        elif isinstance(stmt, ast.AnnAssign):
            target = stmt.target
        else:
            return None
        if not (isinstance(target, ast.Name) and isinstance(stmt.value, ast.Dict)):
            return None
        for key, value in zip(stmt.value.keys, stmt.value.values):
            first = key.lineno if key is not None else value.lineno
            if first <= line <= value.end_lineno:
                break
        else:
            return None  # a brace line, not an entry
        if key is None:
            return None  # `**Base.NAME`: every entry of the base
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            name = "S:" + key.value
        elif isinstance(key, ast.Attribute):
            name = key.attr
        elif isinstance(key, ast.Name):
            name = key.id
        else:
            return None
        return self.maps.dict_entry_mask(f"{module_of(rel)}:{qual}", target.id, name)

    @staticmethod
    def is_transforms(stmt) -> bool:
        target = stmt.targets[0] if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 else getattr(stmt, "target", None)
        return isinstance(target, ast.Name) and target.id == "TRANSFORMS"

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

    def module_mask(self, rel: str, depth_limit: int | None = 2) -> int:
        """Tests that ran code in the module, built its classes, or did so for its importers.

        `depth_limit` is how many import hops to follow; None follows the whole chain, which is what an
        import-time crash needs: everything that imports the module, directly or not, fails with it.
        """
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
            if depth_limit is not None and depth >= depth_limit:
                continue
            for importer in maps.importers.get(cur, []):
                if importer not in seen and not importer.endswith("__init__.py"):
                    seen.add(importer)
                    queue.append((importer, depth + 1))
        return mask
