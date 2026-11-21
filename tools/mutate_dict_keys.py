"""Key-edit and entry-deletion mutants for generator TRANSFORMS tables (evaluation only).

Usage: python tools/mutate_dict_keys.py <checkout> <out> <count> <seed>
"""
import ast, difflib, json, os, random, re, sys

root, out, count, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
os.makedirs(out, exist_ok=True)
NEW_KEYS = ["Limit", "Boolean", "Alias", "Not", "Between", "Cast"]
files = [f"sqlglot/generators/{n}" for n in sorted(os.listdir(os.path.join(root, "sqlglot/generators"))) if n.endswith(".py") and n != "__init__.py"]
index, tries = {}, 0


def diff(rel, old, new):
    return f"diff --git a/{rel} b/{rel}\n" + "\n".join(difflib.unified_diff(old.split("\n"), new.split("\n"), f"a/{rel}", f"b/{rel}", lineterm="", n=3)) + "\n"


while len(index) < count and tries < 2000:
    tries += 1
    rel = rng.choice(files)
    old = open(os.path.join(root, rel), encoding="utf-8").read()
    tree = ast.parse(old)
    lines = old.split("\n")
    cands = []
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for st in cls.body:
            tgt = st.targets[0] if isinstance(st, ast.Assign) and len(st.targets) == 1 else getattr(st, "target", None)
            if isinstance(tgt, ast.Name) and tgt.id == "TRANSFORMS" and isinstance(st.value, ast.Dict):
                for k, v in zip(st.value.keys, st.value.values):
                    if k is not None and isinstance(k, ast.Attribute) and k.lineno == v.end_lineno and lines[k.lineno - 1].strip().endswith(","):
                        cands.append((k, v))
    if not cands:
        continue
    k, v = rng.choice(cands)
    ln = k.lineno - 1
    mode = "key" if len(index) % 2 == 0 else "delete"
    if mode == "key":
        new_key = rng.choice([x for x in NEW_KEYS if x != k.attr])
        lines2 = lines[:]
        lines2[ln] = lines[ln][:k.col_offset] + f"exp.{new_key}" + lines[ln][k.end_col_offset:]
    else:
        lines2 = lines[:ln] + lines[ln + 1:]
    new = "\n".join(lines2)
    try:
        ast.parse(new)
    except SyntaxError:
        continue
    key = f"k{len(index) + 1:03d}"
    open(os.path.join(out, key + ".patch"), "w").write(diff(rel, old, new))
    index[key] = {"file": rel, "line": ln + 1, "kind": f"transforms-{mode}", "scope": "classbody"}
json.dump(index, open(os.path.join(out, "index.json"), "w"), indent=1)
print([(k, v["kind"], v["file"].split("/")[-1]) for k, v in index.items()])
