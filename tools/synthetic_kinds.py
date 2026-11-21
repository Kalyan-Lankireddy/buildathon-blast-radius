"""Synthetic patches of kinds the sample never had, to see how the selector reacts (evaluation only)."""
import difflib
import json
import os
import subprocess
import sys

root, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)


def read(rel):
    return open(os.path.join(root, rel), encoding="utf-8").read()


def diff(rel, old, new, header=""):
    body = "\n".join(difflib.unified_diff(old.split("\n"), new.split("\n"), f"a/{rel}", f"b/{rel}", lineterm="", n=3))
    return f"diff --git a/{rel} b/{rel}\n{header}{body}\n"


patches = {}
# 1. a new function appended to a module
old = read("sqlglot/helper.py")
patches["new_function"] = diff("sqlglot/helper.py", old, old.rstrip("\n") + "\n\n\ndef brand_new_helper(x):\n    return x\n")
# 2. a function body replaced by pass-through (delete lines inside a function)
old = read("sqlglot/serde.py")
patches["delete_lines"] = diff("sqlglot/serde.py", old, old.replace("            if node.comments:\n                payload[COMMENTS] = node.comments\n", ""))
# 3. an added import line
old = read("sqlglot/dialects/tsql.py")
patches["add_import"] = diff("sqlglot/dialects/tsql.py", old, old.replace("class TSQL(Dialect):", "import json\n\n\nclass TSQL(Dialect):", 1))
# 4. docstring edit
old = read("sqlglot/helper.py")
patches["docstring"] = diff("sqlglot/helper.py", old, old.replace('"""', '"""EDITED ', 1))
# 5. pyproject change
old = read("pyproject.toml") if os.path.exists(os.path.join(root, "pyproject.toml")) else "x\n"
patches["pyproject"] = diff("pyproject.toml", old, old.replace("sqlglot", "sqlglotx", 1))
# 6. a deleted fixture file
old = read("tests/fixtures/optimizer/simplify.sql")
patches["delete_fixture"] = f"diff --git a/tests/fixtures/optimizer/simplify.sql b/tests/fixtures/optimizer/simplify.sql\ndeleted file mode 100644\n--- a/tests/fixtures/optimizer/simplify.sql\n+++ /dev/null\n@@ -1,{len(old.split(chr(10)))} +0,0 @@\n" + "\n".join("-" + l for l in old.split("\n")) + "\n"
# 7. a new test file
patches["new_test_file"] = "diff --git a/tests/test_new.py b/tests/test_new.py\nnew file mode 100644\n--- /dev/null\n+++ b/tests/test_new.py\n@@ -0,0 +1,2 @@\n+def test_x():\n+    assert 1\n"
# 8. a rename of a source module
patches["rename_module"] = "diff --git a/sqlglot/trie.py b/sqlglot/trie2.py\nsimilarity index 100%\nrename from sqlglot/trie.py\nrename to sqlglot/trie2.py\n"
# 9. a README change
patches["readme"] = diff("README.md", read("README.md"), read("README.md") + "\nmore docs\n")
# 10. a benchmark file edit
old = read("benchmarks/bench.py") if os.path.exists(os.path.join(root, "benchmarks/bench.py")) else None
if old:
    patches["benchmark"] = diff("benchmarks/bench.py", old, old.replace("import", "import  ", 1))
# 11. syntax error in a lazily imported dialect module
old = read("sqlglot/dialects/materialize.py")
patches["syntax_error_lazy"] = diff("sqlglot/dialects/materialize.py", old, old.replace("class Materialize", "class Materialize(", 1))
# 12. syntax error in a core module
old = read("sqlglot/helper.py")
patches["syntax_error_core"] = diff("sqlglot/helper.py", old, old.replace("import", "imprt", 1))
# 13. a type annotation only change
old = read("sqlglot/helper.py")
patches["annotation"] = diff("sqlglot/helper.py", old, old.replace("-> str:", "-> str | None:", 1))
for name, text in patches.items():
    open(os.path.join(out, name + ".patch"), "w", encoding="utf-8").write(text)
print(sorted(patches))
