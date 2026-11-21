"""Hidden-failure measurement (evaluation only).

For each commit whose selection holds a heavy test (3 s or more): the cheap tests (under 0.5 s) in the selection that
cover the changed code, and whether a heavy test failed while every one of those cheap tests passed.
Writes work/heavy/cheap_sets.json for the CPU measurement.
"""
import collections, json, os, re, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from br.analyze import Analyzer, FullSuite, Maps  # noqa: E402
from br.patchparse import parse_patch  # noqa: E402

CHECKOUT, MAPS = sys.argv[1], Maps(sys.argv[2])
TESTS = json.load(open("sample/tests.json"))["tests"]
NUM = {t["id"]: t["number"] for t in TESTS}
SEC = {t["number"]: t["seconds"] for t in TESTS}
NAME = {t["number"]: t["id"].split("::", 1)[-1] for t in TESTS}
IDX2NUM = [NUM.get(n) for n in MAPS.tests]
HEAVY, CHEAP = 3.0, 0.5


def numbers(mask):
    out, i = set(), 0
    while mask:
        if mask & 1 and IDX2NUM[i] is not None:
            out.add(IDX2NUM[i])
        mask >>= 1
        i += 1
    return out


class Tracing(Analyzer):
    """Records which parts of the selection come from rules that say 'these tests run the changed code'."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.cover = 0

    def old_line_mask(self, rel, otree, line):
        m = super().old_line_mask(rel, otree, line)
        self.cover |= m
        return m

    def new_line_mask(self, rel, otree, ntree, line, prev_old, next_old):
        m = super().new_line_mask(rel, otree, ntree, line, prev_old, next_old)
        self.cover |= m
        return m

    def file_mask(self, fp):
        n = len(self.reasons)
        m = super().file_mask(fp)
        if any(k in " ".join(self.reasons[n:]) for k in ("data file", "comments or blank")):
            self.cover |= m
        return m


def analyse(text):
    an = Tracing(MAPS, CHECKOUT)
    full = False
    try:
        mask = an.select(parse_patch(text))
        cover = numbers(an.cover & mask)
    except FullSuite:
        mask, cover, full = MAPS.full, set(), True
    return numbers(mask), cover, full


items = []  # (label, patch path, selection, covering, failing)
ans = json.load(open("sample/answers.json"))["commits"]
for name in sorted(ans):
    sel, cover, full = analyse(open(f"sample/commits/{name}.patch").read())
    items.append((f"sample/{name}", f"/sample/commits/{name}.patch", sel, cover, set(ans[name]["failing"]), full))
for d in ("mut", "mut2", "mut3", "mut6", "mut8", "mut9"):
    for n in sorted(f[:-11] for f in os.listdir(f"work/{d}") if f.endswith(".truth.json")):
        t = json.load(open(f"work/{d}/{n}.truth.json"))
        fail = {NUM[i] for i in t["failing"] if i in NUM
                and not any(i in l and re.search(r"worker 'gw\d", l) for l in t.get("failed_lines", []))}
        sel, cover, full = analyse(open(f"work/{d}/{n}.patch").read())
        items.append((f"{d}/{n}", f"/work/{d}/{n}.patch", sel, cover, fail, full))

DETAIL = {}
rows, per_heavy = [], collections.defaultdict(lambda: {"n": 0, "failed": 0, "hidden": 0, "no_cheap": 0})
sets = []
for label, patch, sel, cover, fail, full in items:
    heavy = sorted((n for n in sel if SEC[n] >= HEAVY), key=lambda n: -SEC[n])
    if not heavy:
        continue
    cheap = sorted(n for n in sel & cover if SEC[n] < CHEAP)
    cheap_failed = bool(set(cheap) & fail)
    heavy_failed = [n for n in heavy if n in fail]
    for h in heavy:
        e = per_heavy[h]
        e["n"] += 1
        if not cheap:
            e["no_cheap"] += 1
        else:
            e["failed"] += h in fail
            e["hidden"] += (h in fail) and not cheap_failed
    rows.append((label, len(sel), [NAME[h].split("::")[-1] for h in heavy], len(cheap), cheap_failed,
                 [NAME[h].split("::")[-1] for h in heavy_failed], full))
    DETAIL[label] = {"sel": sorted(sel), "heavy": heavy, "cheap": cheap, "fail": sorted(fail)}
    if cheap:
        sets.append({"name": label, "patch": patch, "ids": [TESTS[n]["id"] for n in cheap],
                     "true_failing_cheap": [TESTS[n]["id"] for n in cheap if n in fail]})

with_heavy_fail = [r for r in rows if r[5] and r[3] > 0]
print("commits where a heavy test really failed (and a cheap set existed):", len(with_heavy_fail), [r[0] for r in with_heavy_fail])
print("   cheap covering tests failed too in:", sum(1 for r in with_heavy_fail if r[4]), "of", len(with_heavy_fail))
print(f"commits with a heavy test (>= {HEAVY}s) in the selection: {len(rows)} of {len(items)}")
print(f"  (b) no cheap covering test at all: {sum(1 for r in rows if r[3] == 0)} ({sum(1 for r in rows if r[6])} are full-suite fallbacks)")
verifiable = [r for r in rows if r[3] > 0]
hidden = [r for r in verifiable if r[5] and not r[4]]
print(f"  with a cheap covering set: {len(verifiable)}; heavy failed while all cheap passed (hidden): {len(hidden)}")
for r in hidden:
    print("    HIDDEN:", r[0], "heavy failed:", r[5], "cheap tests:", r[3])
print("\n(a) per heavy test, over commits where it was selected and a cheap covering set existed:")
print(f"{'heavy test':44} {'selected':>8} {'no cheap':>8} {'failed':>6} {'hidden':>6}")
for h, e in sorted(per_heavy.items(), key=lambda kv: -kv[1]["n"]):
    print(f"{NAME[h][:44]:44} {e['n']:8d} {e['no_cheap']:8d} {e['failed']:6d} {e['hidden']:6d}   ({SEC[h]:.1f}s)")
os.makedirs("work/heavy", exist_ok=True)
json.dump(sets, open("work/heavy/cheap_sets.json", "w"))
json.dump(DETAIL, open("work/heavy/detail.json", "w"))
print(f"\nwrote {len(sets)} cheap sets; sizes: min {min(len(s['ids']) for s in sets)}, median {sorted(len(s['ids']) for s in sets)[len(sets)//2]}, max {max(len(s['ids']) for s in sets)}")
