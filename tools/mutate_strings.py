"""f-string and multi-line string literal mutants (evaluation only). Usage: mutate_strings.py <checkout> <out> <seed>"""
import ast, difflib, json, os, random, sys

root, out, seed = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(seed)
os.makedirs(out, exist_ok=True)
files = []
for d, _, names in os.walk(os.path.join(root, "sqlglot")):
    files += [os.path.relpath(os.path.join(d, n), root) for n in names if n.endswith(".py")]
files.sort()
index, made = {}, 0


def diff(rel, old, new):
    return f"diff --git a/{rel} b/{rel}\n" + "\n".join(difflib.unified_diff(old.split("\n"), new.split("\n"), f"a/{rel}", f"b/{rel}", lineterm="", n=3)) + "\n"


def emit(kind, rel, old, new):
    global made
    try:
        ast.parse(new)
    except SyntaxError:
        return False
    made += 1
    name = f"g{made:03d}"
    open(os.path.join(out, name + ".patch"), "w").write(diff(rel, old, new))
    index[name] = {"file": rel, "line": 0, "kind": kind, "scope": kind}
    return True


want = {"fstring": 4, "multiline": 3}
tries = 0
while any(v > 0 for v in want.values()) and tries < 3000:
    tries += 1
    rel = rng.choice(files)
    old = open(os.path.join(root, rel), encoding="utf-8").read()
    tree = ast.parse(old)
    lines = old.split("\n")
    doc = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef)) and n.body and isinstance(n.body[0], ast.Expr):
            doc.add(id(n.body[0].value))
    if want["fstring"] > 0:
        cands = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) and n.lineno == n.end_lineno
                 and any(isinstance(v, ast.Constant) and len(v.value) >= 3 for v in n.values)]
        if cands:
            n = rng.choice(cands)
            part = next(v for v in n.values if isinstance(v, ast.Constant) and len(v.value) >= 3)
            line = lines[n.lineno - 1]
            seg = line[n.col_offset:n.end_col_offset]
            pos = seg.find(part.value)
            if pos >= 0:
                new_seg = seg[:pos + 1] + "Z" + seg[pos + 1:]
                lines2 = lines[:]
                lines2[n.lineno - 1] = line[:n.col_offset] + new_seg + line[n.end_col_offset:]
                if emit("fstring", rel, old, "\n".join(lines2)):
                    want["fstring"] -= 1
                continue
    if want["multiline"] > 0:
        cands = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and n.end_lineno - n.lineno >= 2 and id(n) not in doc]
        if cands:
            n = rng.choice(cands)
            ln = rng.randrange(n.lineno + 1, n.end_lineno)
            if lines[ln - 1].strip():
                lines2 = lines[:]
                lines2[ln - 1] = lines[ln - 1] + "Z"
                if emit("multiline", rel, old, "\n".join(lines2)):
                    want["multiline"] -= 1
json.dump(index, open(os.path.join(out, "index.json"), "w"), indent=1)
print(len(index), {k: v["kind"] for k, v in index.items()}, [v["file"] for v in index.values()])
