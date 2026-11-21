"""Tally CI cost against 'run it ourselves and report only real failures' (evaluation only)."""
import json, sys

tests = json.load(open("sample/tests.json"))["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
number_of = {t["id"]: t["number"] for t in tests}
sel = {json.loads(l)["commit"]: json.loads(l)["workers"] for l in open("work/sel_v19.jsonl")}
runs = json.load(open("work/est/local_run.json"))


def cost(workers):
    w = [x for x in workers if x]
    return 0 if not w else max(sum(ms[n] for n in x) for x in w) + 2000 * len(w)


def pack_cost(numbers):
    if not numbers:
        return 0
    items = sorted((ms[n] for n in numbers), reverse=True)
    best = None
    for k in range(1, min(8, len(items)) + 1):
        loads = [0] * k
        for d in items:
            loads[loads.index(min(loads))] += d
        c = max(loads) + 2000 * k
        best = c if best is None else min(best, c)
    return best


rows = []
for name, workers in sel.items():
    ci = cost(workers) / 1000
    r = runs.get(name, {})
    if not r.get("selected"):
        rows.append((name, 0, ci, 0, 0, 0, 0, True, 0, ci))
        continue
    local_numbers = [number_of[i] for i in r["local_failing"]]
    out = pack_cost(local_numbers) / 1000
    total = r["cpu"] + out
    rows.append((name, r["selected"], ci, r["recorded_s"], r["cpu"], r["wall"], out, r["match"], total, ci - total))
rows.sort(key=lambda x: -x[9])
print(f"{'commit':6} {'sel':>4} {'CI cost':>8} {'recorded':>9} {'local CPU':>9} {'wall':>6} {'out cost':>8} {'local total':>11} {'saving':>7}  same-as-CI")
for n, s, ci, rec, cpu, wall, out, ok, tot, win in rows:
    print(f"{n:6} {s:4d} {ci:8.1f} {rec:9.1f} {cpu:9.2f} {wall:6.1f} {out:8.1f} {tot:11.2f} {win:7.1f}  {'yes' if ok else 'NO'}")
ci_all = sum(r[2] for r in rows)
always = sum(r[8] if r[1] else r[2] for r in rows)
best = sum(min(r[2], r[8]) if r[1] else r[2] for r in rows)
valid = [r for r in rows if r[1] and r[7]]
print(f"\nCI cost, all commits: {ci_all:.1f} s")
print(f"always run locally (where a selection exists): {always:.1f} s")
print(f"run locally only where it wins (hindsight): {best:.1f} s   saving {ci_all - best:.1f} s")
print(f"local outcome identical to the real failures: {len(valid)} of {len([r for r in rows if r[1]])} commits")
print(f"total local CPU: {sum(r[4] for r in rows):.1f} s; total wall: {sum(r[5] for r in rows):.1f} s")
