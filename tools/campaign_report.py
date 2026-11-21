"""Tally the heavy-test campaign (evaluation only): hidden failures, fidelity direction, CPU, and the priced policy."""
import collections, glob, json, os, statistics, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from br.analyze import Analyzer, FullSuite, Maps  # noqa: E402
from br.packing import pack  # noqa: E402
from br.patchparse import parse_patch  # noqa: E402

D = "work/mut10"
root, M = sys.argv[1], Maps(sys.argv[2])
TESTS = json.load(open("sample/tests.json"))["tests"]
NUM = {t["id"]: t["number"] for t in TESTS}
MS = {t["number"]: round(t["seconds"] * 1000) for t in TESTS}
SEC = {t["number"]: t["seconds"] for t in TESTS}
IDX2NUM = [NUM.get(n) for n in M.tests]
index = json.load(open(f"{D}/index.json"))
cheap_sets = json.load(open(f"{D}/cheap_sets.json"))


def numbers(mask):
    out, i = set(), 0
    while mask:
        if mask & 1 and IDX2NUM[i] is not None:
            out.add(IDX2NUM[i])
        mask >>= 1
        i += 1
    return out


def cost(ns):
    w = [x for x in pack(sorted(ns), MS) if x]
    return 0 if not w else (max(sum(MS[n] for n in x) for x in w) + 2000 * len(w)) / 1000


done = sorted(os.path.basename(f)[:-10] for f in glob.glob(f"{D}/h*.camp.json"))
rows, per_heavy = [], collections.defaultdict(lambda: collections.Counter())
direction = {"pass_local_fail_truth": [], "fail_local_pass_truth": []}
for name in done:
    r = json.load(open(f"{D}/{name}.camp.json"))
    cs = cheap_sets[name]
    an = Analyzer(M, root)
    try:
        sel = numbers(an.select(parse_patch(open(f"{D}/{name}.patch").read())))
    except FullSuite:
        continue
    heavy = {NUM[i] for i in cs["heavy"]}
    cheap = {NUM[i] for i in cs["cheap"]}
    base = cost(sel)
    if r["local_failing"]:
        policy, kind = base + r["cheap_cpu"], "cheap failed locally"
    else:
        policy, kind = cost(sel - heavy - cheap) + r["cheap_cpu"], "verified"
    rows.append({"name": name, "scope": index[name]["scope"], "kind": index[name]["kind"], "r": r, "base": base, "policy": policy,
                 "outcome": kind, "gate": r["cheap_cpu"] <= 5.0, "big": any(SEC[h] >= 20 for h in heavy)})
    for h in cs["heavy"]:
        c = per_heavy[h.split("::", 1)[1]]
        c["selected"] += 1
        c["failed"] += h in r["heavy_failed"]
        c["hidden"] += (h in r["heavy_failed"]) and not r["local_failing"]
    for t in r["pass_locally_fail_in_truth"]:
        direction["pass_local_fail_truth"].append((name, index[name]["scope"], t))
    for t in r["fail_locally_pass_in_truth"]:
        direction["fail_local_pass_truth"].append((name, index[name]["scope"], t))

n = len(rows)
hf = [x for x in rows if x["r"]["heavy_failed"]]
hid = [x for x in rows if x["r"]["hidden"]]
print(f"mutants finished: {n} of {len(index)}   (time box stops the runner at 1:30 PM)")
print(f"commits where a heavy test really failed: {len(hf)}   hidden (heavy failed, local cheap set all passed): {len(hid)}")
for x in hid:
    print("   HIDDEN:", x["name"], x["kind"], x["scope"], x["r"]["heavy_failed"])
print(f"   cheap tests also failed in ground truth for: {sum(1 for x in hf if x['r']['truth_failing_cheap'])} of {len(hf)}")
print("\nper heavy test (selected / failed / hidden):")
for h, c in sorted(per_heavy.items(), key=lambda kv: -kv[1]["selected"]):
    print(f"   {h:55} {c['selected']:4d} {c['failed']:4d} {c['hidden']:4d}")
print("\nfidelity direction (cheap tests, local isolated run vs ground truth):")
print(f"   PASSED locally but FAILED in ground truth: {len(direction['pass_local_fail_truth'])} tests in {len({d[0] for d in direction['pass_local_fail_truth']})} mutants")
for name, scope, t in direction["pass_local_fail_truth"][:8]:
    print(f"      HARD STOP candidate: {name} ({scope}) {t}")
print(f"   FAILED locally but PASSED in ground truth: {len(direction['fail_local_pass_truth'])} tests in {len({d[0] for d in direction['fail_local_pass_truth']})} mutants (safe direction)")
cpus = sorted(x["r"]["cheap_cpu"] for x in rows)
if cpus:
    print(f"\ncheap-set CPU: median {statistics.median(cpus):.1f} s, p90 {cpus[int(len(cpus) * .9)]:.1f} s, max {cpus[-1]:.1f} s; under 5 s: {sum(c <= 5 for c in cpus)} of {n}")
print(f"unreadable failure entries: {sum(x['r']['unread'] for x in rows)}; crashes or timeouts seen: {sum(x['r']['crashed_or_timeout'] for x in rows)}")
for label, pick in (("all mutants", lambda x: True), ("gated: cheap CPU <= 5 s", lambda x: x["gate"]),
                    ("gated and a 20 s+ heavy test selected", lambda x: x["gate"] and x["big"])):
    sub = [x for x in rows if pick(x)]
    if sub:
        net = sum(x["base"] - x["policy"] for x in sub)
        print(f"policy, {label}: {len(sub)} commits, CI now {sum(x['base'] for x in sub):.1f} s, with policy {sum(x['policy'] for x in sub):.1f} s, net {net:+.1f} s ({net / len(sub):+.1f} s per applied commit)")
print("by scope of the edited line:", dict(collections.Counter(x["scope"] for x in rows)))
