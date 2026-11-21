"""Per-commit cost of a selector output against the sample answers (evaluation only, never shipped)."""
import json, sys
data, answers_path, out_path = sys.argv[1:4]
tests = json.load(open(f"{data}/tests.json"))["tests"]
ans = json.load(open(answers_path))["commits"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
def cost(workers):
    w = [x for x in workers if x]
    return 0 if not w else max(sum(ms[n] for n in x) for x in w) + 2000 * len(w)
def ideal(fail):
    items = sorted((ms[i] for i in fail), reverse=True); best = 0 if not items else None
    for k in range(1, min(8, len(items)) + 1):
        loads = [0] * k
        for d in items: loads[loads.index(min(loads))] += d
        c = max(loads) + 2000 * k; best = c if best is None else min(best, c)
    return best or 0
rows = []
for line in open(out_path):
    r = json.loads(line); a = ans[r["commit"]]
    sel = {n for x in r["workers"] for n in x}
    rows.append((cost(r["workers"]) - ideal(a["failing"]), r["commit"], a["kind"], len(sel), sum(ms[n] for n in sel) / 1000,
                 cost(r["workers"]) / 1000, ideal(a["failing"]) / 1000, len(a["failing"]), all(f in sel for f in a["failing"])))
rows.sort(reverse=True)
print(f"{'commit':6} {'kind':8} {'sel':>5} {'sel_s':>7} {'cost':>7} {'ideal':>6} {'fail':>4} ok")
for e, c, k, n, s, co, i, f, ok in rows:
    print(f"{c:6} {k:8} {n:5d} {s:7.1f} {co:7.1f} {i:6.1f} {f:4d} {'ok' if ok else 'MISS'}")
print(f"total cost {sum(r[5] for r in rows):.1f}  ideal {sum(r[6] for r in rows):.1f}")
