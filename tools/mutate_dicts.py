"""Mutants that edit one entry of a class-level dict (TRANSFORMS, FUNCTIONS, TYPE_MAPPING, ...). Evaluation only.

Usage: python tools/mutate_dicts.py <checkout> <out> <count> <seed>
"""
import ast, difflib, json, os, random, re, sys

root, out, count, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
os.makedirs(out, exist_ok=True)
files = []
for top in ("generators", "parsers", "dialects"):
    d = os.path.join(root, "sqlglot", top)
    files += [f"sqlglot/{top}/{n}" for n in sorted(os.listdir(d)) if n.endswith(".py") and n != "__init__.py"]
files += ["sqlglot/generator.py", "sqlglot/parser.py", "sqlglot/tokens.py"]
index, tries = {}, 0


def diff(rel, old, new):
    return f"diff --git a/{rel} b/{rel}\n" + "\n".join(difflib.unified_diff(old.split("\n"), new.split("\n"), f"a/{rel}", f"b/{rel}", lineterm="", n=3)) + "\n"


while len(index) < count and tries < 4000:
    tries += 1
    rel = rng.choice(files)
    old = open(os.path.join(root, rel), encoding="utf-8").read()
    tree = ast.parse(old)
    lines = old.split("\n")
    entries = []
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for st in cls.body:
            tgt = st.targets[0] if isinstance(st, ast.Assign) and len(st.targets) == 1 else getattr(st, "target", None)
            if isinstance(tgt, ast.Name) and re.match(r"^[A-Z][A-Z0-9_]*$", tgt.id) and isinstance(getattr(st, "value", None), ast.Dict):
                for k, v in zip(st.value.keys, st.value.values):
                    if k is not None and k.lineno == v.end_lineno:
                        entries.append((cls.name, tgt.id, k, v))
    if not entries:
        continue
    cname, name, k, v = rng.choice(entries)
    ln = k.lineno - 1
    line = lines[ln]
    seg_start = v.col_offset
    seg = line[seg_start:v.end_col_offset]
    new_seg = None
    s = re.search(r"([\"'])([^\"']{2,})\1", seg)
    if s and rng.random() < 0.7:
        new_seg = seg[:s.end(2)] + "Z" + seg[s.end(2):]
    else:
        n = re.search(r"(?<![\w.])(\d+)(?![\w.])", seg)
        if n:
            new_seg = seg[:n.start()] + str(int(n.group(1)) + 1) + seg[n.end():]
    if not new_seg or new_seg == seg:
        continue
    lines2 = lines[:]
    lines2[ln] = line[:seg_start] + new_seg + line[v.end_col_offset:]
    new = "\n".join(lines2)
    try:
        ast.parse(new)
    except SyntaxError:
        continue
    key = f"d{len(index) + 1:03d}"
    open(os.path.join(out, key + ".patch"), "w").write(diff(rel, old, new))
    index[key] = {"file": rel, "line": ln + 1, "kind": f"dict:{name}", "scope": "classbody"}
json.dump(index, open(os.path.join(out, "index.json"), "w"), indent=1)
print(len(index), [(k, v["kind"], v["file"].split("/")[-1]) for k, v in index.items()])
