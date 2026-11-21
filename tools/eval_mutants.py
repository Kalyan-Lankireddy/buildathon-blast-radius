"""Score the selector on mutant patches whose real test outcome is known (evaluation only).

Usage: python tools/eval_mutants.py <mutant dir with *.patch and *.truth.json> <tests.json> <checkout> <maps>
"""
import json
import os
import subprocess
import sys
import tempfile

mut, tests_json, checkout, maps = sys.argv[1:5]
here = os.path.dirname(os.path.abspath(__file__))
tests = json.load(open(tests_json))["tests"]
number = {t["id"]: t["number"] for t in tests}
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
names = sorted(f[:-11] for f in os.listdir(mut) if f.endswith(".truth.json"))
index = json.load(open(os.path.join(mut, "index.json")))
with tempfile.TemporaryDirectory() as data:
    os.makedirs(os.path.join(data, "commits"))
    for n in names:
        with open(os.path.join(data, "commits", n + ".patch"), "w") as f:
            f.write(open(os.path.join(mut, n + ".patch")).read())
    with open(os.path.join(data, "tests.json"), "w") as f:
        json.dump({"tests": tests}, f)
    env = dict(os.environ, BR_DATA=data, BR_SRC=checkout, BR_MAPS=maps)
    out = subprocess.run([sys.executable, "-I", os.path.join(here, "..", "selector.py")], env=env,
                         capture_output=True, text=True).stdout
chosen = {json.loads(l)["commit"]: json.loads(l)["workers"] for l in out.splitlines()}


def cost(workers):
    w = [x for x in workers if x]
    return 0 if not w else max(sum(ms[n] for n in x) for x in w) + 2000 * len(w)


misses, total, rows = 0, 0, []
for n in names:
    truth = json.load(open(os.path.join(mut, n + ".truth.json")))
    failing = {number[i] for i in truth["failing"] if i in number}
    sel = {x for w in chosen[n] for x in w}
    missed = sorted(failing - sel)
    misses += bool(missed)
    total += cost(chosen[n])
    rows.append((bool(missed), n, index[n]["kind"], index[n]["file"], index[n]["line"], len(failing), len(sel), cost(chosen[n]) / 1000, missed[:3]))
rows.sort(reverse=True)
for miss, n, kind, f, line, nf, ns, c, m in rows:
    print(f"{'MISS' if miss else 'ok  '} {n} {kind:10} {f}:{line:<5} failing {nf:3d} selected {ns:4d} cost {c:5.1f}s {m if miss else ''}")
print(f"mutants {len(names)}  misses {misses}  total cost {total / 1000:.1f}s  (a miss adds 300 s)")
