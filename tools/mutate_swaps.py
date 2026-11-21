"""Operator-swap mutants (evaluation only; never shipped): is<->==, is not<->!=, <-><=, >->>=.

Usage: python tools/mutate_swaps.py <checkout> <profile dump dir> <out> <provable> <unprovable> <unexecuted> <seed>
The profile decides only which sites to sample, so both provable and unprovable ones are tested.
"""
import ast, difflib, glob, json, os, random, re, sys

root, dumps, out = sys.argv[1:4]
want = {"provable": int(sys.argv[4]), "bad": int(sys.argv[5]), "unexecuted": int(sys.argv[6])}
rng = random.Random(int(sys.argv[7]))
os.makedirs(out, exist_ok=True)
total = {}
for p in glob.glob(os.path.join(dumps, "cmp.*.json")):
    for k, (n, bad) in json.load(open(p)).items():
        t = total.setdefault(k, [0, 0]); t[0] += n; t[1] += bad
SWAP = {"is not": "!=", "is": "==", "==": "is", "!=": "is not", "<=": "<", "<": "<=", ">=": ">", ">": ">="}
OPS = {ast.Is: "is", ast.IsNot: "is not", ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}
PAT = re.compile(r"\bis\s+not\b|\bis\b|==|!=|<=|>=|<|>")
pool = {"provable": [], "bad": [], "unexecuted": []}
for dp, _, names in os.walk(os.path.join(root, "sqlglot")):
    for n in names:
        if not n.endswith(".py"):
            continue
        rel = os.path.relpath(os.path.join(dp, n), root)
        text = open(os.path.join(root, rel), encoding="utf-8").read()
        lines = text.split("\n")
        for c in ast.walk(ast.parse(text)):
            if not (isinstance(c, ast.Compare) and len(c.ops) == 1 and type(c.ops[0]) in OPS):
                continue
            l, r = c.left, c.comparators[0]
            if l.end_lineno != r.lineno:
                continue
            seg = lines[l.end_lineno - 1][l.end_col_offset:r.col_offset]
            m = PAT.search(seg)
            if not m or re.sub(r"[\s()]", "", seg) != re.sub(r"[\s()]", "", m.group(0)):
                continue
            key = f"{rel}:{c.lineno}:{c.col_offset}"
            rec = total.get(key)
            kind = "unexecuted" if rec is None or rec[0] == 0 else ("provable" if rec[1] == 0 else "bad")
            start = l.end_col_offset + m.start()
            pool[kind].append((rel, l.end_lineno, start, m.group(0), key))
index = {}
for kind, count in want.items():
    for rel, line, start, op, key in rng.sample(pool[kind], min(count, len(pool[kind]))):
        old = open(os.path.join(root, rel), encoding="utf-8").read().split("\n")
        new = list(old)
        new[line - 1] = old[line - 1][:start] + SWAP[re.sub(r"\s+", " ", op)] + old[line - 1][start + len(op):]
        name = f"w{len(index) + 1:03d}"
        body = f"diff --git a/{rel} b/{rel}\n" + "\n".join(difflib.unified_diff(old, new, f"a/{rel}", f"b/{rel}", lineterm="", n=3)) + "\n"
        open(os.path.join(out, name + ".patch"), "w").write(body)
        index[name] = {"file": rel, "line": line, "kind": kind, "op": op, "key": key}
json.dump(index, open(os.path.join(out, "index.json"), "w"), indent=1)
print({k: len(v) for k, v in pool.items()}, "->", len(index), "mutants")
