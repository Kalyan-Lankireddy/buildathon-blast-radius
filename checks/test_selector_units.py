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


def test_derivation_follows_a_constant_through_locals_branches_and_loops():
    import ast
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
    from derive import trace

    fn = ast.parse('''
def new(klass):
    escapes = klass.tokenizer_class.NUMERIC_ESCAPES
    if "x" in escapes:
        fmt = "a"
    else:
        fmt = ""
    for code in range(3):
        if code:
            klass.ESCAPED.setdefault(chr(code), fmt.format(code))
    klass.TRIE = new_trie(klass.TIME_MAPPING)
    klass.FLAG = "\\\\" in klass.tokenizer_class.STRING_ESCAPES
''').body[0]
    derived, failed = trace(fn)
    assert "NUMERIC_ESCAPES" in derived["ESCAPED"] and derived["TRIE"] == {"TIME_MAPPING"} and derived["FLAG"] == {"STRING_ESCAPES"}
    assert not failed


def test_derivation_gives_up_on_what_it_cannot_follow():
    import ast
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
    from derive import trace

    fn = ast.parse('''
def new(klass):
    log.warning(klass.tokenizer_class.NUMERIC_ESCAPES)
    other = mystery(klass.TIME_MAPPING)
    return klass.STRING_ESCAPES
''').body[0]
    derived, failed = trace(fn)
    assert failed == {"NUMERIC_ESCAPES", "TIME_MAPPING", "STRING_ESCAPES"}


def test_import_check_accepts_a_clean_patch_and_rejects_one_that_crashes_the_import(tmp_path):
    from br.importcheck import imports_cleanly

    pkg = tmp_path / "sqlglot"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("TABLE = {1: 'a'}\nOTHER = TABLE\n")
    path = str(pkg / "mod.py")
    assert imports_cleanly(str(tmp_path), {})[0] is True
    assert imports_cleanly(str(tmp_path), {path: "TABLE = {1: 'b'}\nOTHER = TABLE\n"})[0] is True
    clean, detail = imports_cleanly(str(tmp_path), {path: "TABLE = {1: 'a'}\nOTHER = TABLE - {1}\n"})
    assert clean is False and "TypeError" in detail
    assert imports_cleanly(str(tmp_path), {path: "def broken(:\n"})[0] is False


CACHED = '''_CACHE = {}


def build(cls):
    return cls


def plain(x):
    return x


class G:
    def __init__(self):
        hit = _CACHE.get(type(self))
        if hit is None:
            hit = _CACHE[type(self)] = build(type(self))
'''


def test_code_that_fills_a_process_wide_cache_is_found_with_what_it_calls():
    from br.analyze import Analyzer
    tree = PyTree(CACHED)
    spans = Analyzer.cache_fill_spans(tree)
    inside = lambda line: any(a <= line <= b for a, b in spans)
    assert inside(1) and inside(5) and inside(14)   # the variable, a function it calls, the function that fills it
    assert not inside(9)                             # an unrelated function


def test_verify_says_no_to_anything_but_a_clean_pass(tmp_path):
    from br.verify import all_passed
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_ok():\n    assert True\n\ndef test_bad():\n    assert False\n")
    ok, _, _ = all_passed(str(tmp_path), {}, ["tests/test_a.py::test_ok"], 20, 30)
    assert ok
    ok, detail, _ = all_passed(str(tmp_path), {}, ["tests/test_a.py::test_bad"], 20, 30)
    assert not ok
    ok, _, _ = all_passed(str(tmp_path), {}, ["tests/test_a.py::test_missing"], 20, 30)
    assert not ok


def test_a_provable_operator_swap_is_recognised_and_nothing_else_is():
    from br.analyze import Analyzer

    class Stub:
        def __init__(self, sites):
            self.sites = sites

        def comparison_sites(self, rel):
            return self.sites

    old = "def f(x):\n    if x is None:\n        return 1\n    return (x < 3)\n"
    an = Analyzer(Stub({(2, 7)}), "/nonexistent")
    swap = lambda new: an.provable_swap("sqlglot/m.py", PyTree(old), PyTree(new), 2)
    assert swap(old.replace("x is None", "x == None"))                # a swap at a profiled site
    assert swap(old.replace("x is None", "x  ==  None"))              # spacing does not matter
    assert not swap(old.replace("x is None", "x is not None"))        # not one of the paired swaps
    assert not swap(old.replace("x is None", "y == None"))            # the swap plus another change
    assert not Analyzer(Stub(set()), "/x").provable_swap("sqlglot/m.py", PyTree(old), PyTree(old.replace("is", "==")), 2)
    assert not Analyzer(Stub({(4, 12)}), "/x").provable_swap("sqlglot/m.py", PyTree(old), PyTree(old.replace("is", "==")), 2)
