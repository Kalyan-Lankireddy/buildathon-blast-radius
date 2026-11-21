"""Price the 'verify cheap covering tests locally, drop heavy tests if they pass' policy (evaluation only)."""
import json, sys
sys.path.insert(0, ".")
from br.packing import pack

tests = json.load(open("sample/tests.json"))["tests"]
MS = {t["number"]: round(t["seconds"] * 1000) for t in tests}
detail = json.load(open("work/heavy/detail.json"))
runs = json.load(open("work/heavy/cheap_results.json"))


def cost(numbers):
    w = pack(sorted(numbers), MS)
    w = [x for x in w if x]
    return 0 if not w else (max(sum(MS[n] for n in x) for x in w) + 2000 * len(w)) / 1000


rows, tot = [], {"base": 0.0, "policy": 0.0, "fallback_cpu": 0.0, "saved": 0.0}
for name, r in sorted(runs.items()):
    d = detail[name]
    sel, heavy, cheap = set(d["sel"]), set(d["heavy"]), set(d["cheap"])
    base = cost(sel)
    if r["local_failing"]:  # a cheap test failed locally: normal selection, and the CPU was wasted
        policy, kind = base + r["cpu"], "cheap failed -> normal selection"
    else:
        policy, kind = cost(sel - heavy - cheap) + r["cpu"], "verified, heavy dropped"
    rows.append((name, kind, len(cheap), r["cpu"], base, policy, base - policy, r["match"]))
    tot["base"] += base
    tot["policy"] += policy
print(f"{'commit':14} {'outcome':34} {'cheap':>5} {'CPU':>6} {'CI now':>7} {'policy':>7} {'saving':>7} same-as-CI")
for row in sorted(rows, key=lambda r: -r[6]):
    print(f"{row[0]:14} {row[1]:34} {row[2]:5d} {row[3]:6.1f} {row[4]:7.1f} {row[5]:7.1f} {row[6]:7.1f}  {'yes' if row[7] else 'NO'}")
print(f"\ncommits priced: {len(rows)}  CI now {tot['base']:.1f} s  with the policy {tot['policy']:.1f} s  net saving {tot['base'] - tot['policy']:.1f} s")
won = [r for r in rows if r[6] > 0]
print(f"wins: {len(won)} commits, {sum(r[6] for r in won):.1f} s; losses: {len(rows) - len(won)} commits, {sum(r[6] for r in rows if r[6] <= 0):.1f} s")
sample = [r for r in rows if r[0].startswith('sample/')]
print(f"sample only: {len(sample)} commits priced, net {sum(r[6] for r in sample):.1f} s")
