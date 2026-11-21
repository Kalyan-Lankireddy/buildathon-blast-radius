"""Unit checks for the selector's own code. Run: python -m pytest checks -q"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pytest

from br.packing import pack
from br.patchparse import PatchError, apply_hunks, parse_patch
from br.pytree import PyTree

PATCH = """diff --git a/sqlglot/x.py b/sqlglot/x.py
index 1..2 100644
--- a/sqlglot/x.py
+++ b/sqlglot/x.py
@@ -2,3 +2,3 @@ def f():
     a = 1
-    b = a and 2
+    b = a or 2
     return b
"""
OLD = "def f():\n    a = 1\n    b = a and 2\n    return b\n"


def test_parse_and_apply_a_one_line_edit():
    (fp,) = parse_patch(PATCH)
    assert fp.path == "sqlglot/x.py" and len(fp.hunks) == 1
    new, edit = apply_hunks(OLD, fp.hunks)
    assert "b = a or 2" in new and edit.removed == [3] and [a for a, _, _ in edit.added] == [3]


def test_context_mismatch_is_an_error_so_the_caller_runs_everything():
    (fp,) = parse_patch(PATCH)
    with pytest.raises(PatchError):
        apply_hunks("def f():\n    a = 2\n    b = a and 2\n    return b\n", fp.hunks)


def test_new_and_deleted_files_and_binary_are_recognised():
    text = ("diff --git a/n.txt b/n.txt\nnew file mode 100644\n--- /dev/null\n+++ b/n.txt\n@@ -0,0 +1 @@\n+hi\n"
            "diff --git a/d.txt b/d.txt\ndeleted file mode 100644\n--- a/d.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
            "diff --git a/i.png b/i.png\nBinary files a/i.png and b/i.png differ\n")
    new, deleted, binary = parse_patch(text)
    assert new.old_path is None and deleted.new_path is None and binary.binary


def test_packing_never_exceeds_eight_workers_and_empty_is_free():
    ms = {i: 1000 for i in range(40)}
    assert pack([], ms) == []
    workers = pack(list(ms), ms)
    assert 1 <= len(workers) <= 8 and sorted(n for w in workers for n in w) == list(ms)


def test_packing_picks_the_cheapest_worker_count():
    ms = {0: 10000, 1: 10000}
    assert len(pack([0, 1], ms)) == 2  # 10 s + 4 s beats 20 s + 2 s
    assert len(pack([0], ms)) == 1


def test_scopes_distinguish_function_class_and_module_lines():
    tree = PyTree("X = 1\n\nclass C:\n    A = 2\n\n    def m(self):\n        return self.A\n")
    assert tree.scope_of(1).kind == "module"
    assert tree.scope_of(4).kind == "classbody" and tree.scope_of(4).cls == "C"
    assert tree.scope_of(7).kind == "runtime"
    assert tree.scope_of(6).kind == "funcheader"


def test_uncommenting_code_is_not_treated_as_a_replacement():
    from br.analyze import Analyzer

    old = PyTree("def f():\n    # x = g()\n    return 1\n")
    new = PyTree("def f():\n    x = g()\n    return 1\n")
    (fp,) = parse_patch(
        "diff --git a/sqlglot/x.py b/sqlglot/x.py\n--- a/sqlglot/x.py\n+++ b/sqlglot/x.py\n"
        "@@ -1,3 +1,3 @@\n def f():\n-    # x = g()\n+    x = g()\n     return 1\n")
    _, edit = apply_hunks("def f():\n    # x = g()\n    return 1\n", fp.hunks)
    added = [(a, p, n) for a, p, n in edit.added]
    slots, paired = Analyzer.replacement_slots(old, new, edit, added)
    assert slots == set() and not paired


def test_literal_only_separates_safe_import_time_statements():
    from br.analyze import Analyzer

    parse = lambda src: PyTree(src).module.body[0]  # noqa: E731
    assert Analyzer.literal_only(parse("X = {'a': (1, 2), 'b': [True, None, -3]}"))
    assert not Analyzer.literal_only(parse("X = Y - {1}"))
    assert not Analyzer.literal_only(parse("X = [p + q for q in Q]"))
    assert not Analyzer.literal_only(parse("def f(): pass"))


def test_a_changed_literal_inside_a_closure_builder_cannot_break_an_import():
    from br.analyze import Analyzer

    one = lambda src: PyTree(src).module.body[0]  # noqa: E731
    same_shape = Analyzer.only_literals_changed
    assert same_shape(one('T = {k: rename_func("A")}'), one('T = {k: rename_func("AZ")}'))
    assert same_shape(one("T = {k: lambda s, e: f'x {s}'}"), one("T = {k: lambda s, e: f'xy {s}'}"))
    # a different name, or a parse call that reads its string at import, is not safe
    assert not same_shape(one('T = {k: rename_func("A")}'), one('T = {j: rename_func("A")}'))
    assert not same_shape(one('T = maybe_parse("a")'), one('T = maybe_parse("ab")'))


def test_fixture_edit_is_attributed_to_one_half_or_to_none():
    from br.fixtures import changed_field

    text = "# title: a\nSELECT 1;\nSELECT 2;\n\n-- note\nSELECT 3\n  FROM t;\nSELECT 4;\n"
    def edit(old_lines, new_text, removed, added):
        return changed_field(text, new_text, removed, added)

    # an input line, an expected line
    assert edit(None, text.replace("SELECT 3", "SELECT 33"), [6], [6]) == "sql"
    assert edit(None, text.replace("SELECT 2;", "SELECT 22;"), [3], [3]) == "expected"
    assert edit(None, text.replace("SELECT 4;", "SELECT 44;"), [8], [8]) == "expected"
    # a meta line, a comment line, a blank line: not sure, so None
    assert edit(None, text.replace("# title: a", "# title: b"), [1], [1]) is None
    assert edit(None, text.replace("-- note", "-- other"), [5], [5]) is None
    assert edit(None, text.replace("\n\n", "\n"), [4], []) is None
    # both halves, a moved boundary, two statements on a line
    assert changed_field(text, text.replace("SELECT 1;", "SELECT 1").replace("SELECT 2;", "SELECT 2;;"), [2, 3], [2, 3]) is None
    assert changed_field("A;B;\n", "A;C;\n", [1], [1]) is None
