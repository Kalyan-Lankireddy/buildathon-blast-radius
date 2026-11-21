"""Where does our excess cost come from? Evaluation only, never shipped.

For every commit (sample answers, or mutant ground truth) this runs the real analyzer with tracing, so each part of the
selection is labelled with the rule that produced it. Excess = our cost minus the perfect cost; it is split over the
rules in proportion to the recorded seconds of the unneeded tests each rule contributed.
"""
import collections, json, os, re, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from br.analyze import Analyzer, FullSuite, Maps  # noqa: E402
from br.packing import pack  # noqa: E402
from br.patchparse import parse_patch  # noqa: E402

CHECKOUT = sys.argv[1]
MAPS = Maps(sys.argv[2])
TESTS = json.load(open("sample/tests.json"))["tests"]
NUMBER = {t["id"]: t["number"] for t in TESTS}
SEC = {t["number"]: t["seconds"] for t in TESTS}
MS = {n: round(s * 1000) for n, s in SEC.items()}
IDX2NUM = [NUMBER.get(n) for n in MAPS.tests]


def numbers(mask):
    out, i = set(), 0
    while mask:
        if mask & 1 and IDX2NUM[i] is not None:
            out.add(IDX2NUM[i])
        mask >>= 1
        i += 1
    return out


def cost(workers):
    w = [x for x in workers if x]
    return 0 if not w else max(sum(MS[n] for n in x) for x in w) + 2000 * len(w)


def ideal(failing):
    return cost(pack(sorted(failing), MS)) if failing else 0


class Tracing(Analyzer):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.parts = []  # (rule label, mask)

    def _scope_label(self, tree, scope, line, rel, in_old):
        stmt = scope.stmt
        if self.bare_string(stmt):
            return "docstring (read as text)"
        if scope.kind == "runtime":
            lines = tree.own_lines(stmt) if in_old else []
            if in_old and self.maps.import_lines(rel).intersection(lines):
                return "line coverage + import-time line widening"
            return "line coverage (function body)"
        if scope.kind == "funcheader":
            return "function header (whole function)"
        if scope.kind == "classheader":
            return "class header (class users)"
        if scope.kind == "classbody":
            if stmt is not None and tree.lambda_body_line(stmt, line):
                return "line coverage (lambda body)"
            narrowed = self.attribute_readers(tree, stmt) if stmt is not None else None
            entry = self.dict_entry_readers(rel, tree, stmt, line, scope.cls) if stmt is not None else None
            if entry is not None and (narrowed is not None or (self.maps.dispatch_recorded and self.is_transforms(stmt))):
                return "dict entry (key lookups)"
            return "class attribute read sites" if narrowed is not None else "class users (class body)"
        return "module-level (module + importers)"

    def old_line_mask(self, rel, otree, line):
        m = super().old_line_mask(rel, otree, line)
        self.parts.append((self._scope_label(otree, otree.scope_of(line), line, rel, True), m))
        return m

    def new_line_mask(self, rel, otree, ntree, line, prev_old, next_old):
        m = super().new_line_mask(rel, otree, ntree, line, prev_old, next_old)
        self.parts.append((self._scope_label(ntree, ntree.scope_of(line), line, rel, False) + " [added line]", m))
        return m

    def module_mask(self, rel, depth_limit=2):
        m = super().module_mask(rel, depth_limit)
        if depth_limit is None:
            self.parts.append(("import-crash widening (non-literal import-time statement)", m))
        return m

    def file_mask(self, fp):
        before = len(self.parts)
        n_notes = len(self.reasons)
        m = super().file_mask(fp)
        notes = " ".join(self.reasons[n_notes:])
        if len(self.parts) == before:
            label = ("fixture/data file (tests that opened it, class setup shared)" if "data file" in notes
                     else "comment-only (read as text)" if "comments or blank" in notes
                     else "new/removed file or config" if re.search(r"new file|removed|moved|configuration|binary", notes)
                     else "other")
            self.parts.append((label, m))
        return m


def analyse(patch_text, failing_numbers):
    an = Tracing(MAPS, CHECKOUT)
    try:
        files = parse_patch(patch_text)
        mask = an.select(files)
        parts = an.parts
        full_reason = None
    except FullSuite as e:
        mask, parts, full_reason = MAPS.full, [(f"FULL SUITE: {e}", MAPS.full)], str(e)
    sel = numbers(mask)
    workers = pack(sorted(sel), MS)
    ours, perfect = cost(workers), ideal(failing_numbers)
    excess_tests = sel - set(failing_numbers)
    # who brought each unneeded test in?
    weight = collections.defaultdict(float)
    for n in excess_tests:
        owners = [lab for lab, pm in parts if n in numbers(pm)] or ["(unattributed)"]
        for lab in set(owners):
            weight[lab] += SEC[n] / len(set(owners))
    total_w = sum(weight.values())
    share = {lab: (ours - perfect) / 1000 * (w / total_w if total_w else 0) for lab, w in weight.items()}
    heavy = sorted(((SEC[n], n) for n in sel), reverse=True)[:2]
    return {"sel_set": sel, "fail_set": set(failing_numbers), "ours": ours / 1000, "perfect": perfect / 1000, "selected": len(sel), "failing": len(failing_numbers),
            "share": share, "heavy": [(s, TESTS[n]["id"].split("::", 1)[1] if "::" in TESTS[n]["id"] else TESTS[n]["id"], n in failing_numbers) for s, n in heavy],
            "full": full_reason}


rows = []
mode = sys.argv[3]
if mode == "sample":
    ans = json.load(open("sample/answers.json"))["commits"]
    for name in sorted(ans):
        r = analyse(open(f"sample/commits/{name}.patch").read(), set(ans[name]["failing"]))
        r["name"] = name
        rows.append(r)
else:
    for d in sys.argv[4:]:
        idx = json.load(open(f"work/{d}/index.json"))
        for n in sorted(f[:-11] for f in os.listdir(f"work/{d}") if f.endswith(".truth.json")):
            truth = json.load(open(f"work/{d}/{n}.truth.json"))
            failing = {NUMBER[i] for i in truth["failing"] if i in NUMBER
                       and not any(i in l and re.search(r"worker 'gw\d", l) for l in truth.get("failed_lines", []))}
            r = analyse(open(f"work/{d}/{n}.patch").read(), failing)
            r["name"] = f"{d}/{n}"
            rows.append(r)

rows.sort(key=lambda r: -(r["ours"] - r["perfect"]))
print(f"{'commit':12} {'ours':>6} {'perfect':>7} {'excess':>6} {'sel':>5} {'fail':>4}  rules (share of excess) | busiest tests")
for r in rows[: int(os.environ.get("TOP", "40"))]:
    ex = r["ours"] - r["perfect"]
    rules = "; ".join(f"{l} {v:.1f}" for l, v in sorted(r["share"].items(), key=lambda kv: -kv[1])[:2]) or "-"
    heavy = ", ".join(f"{t.split('::')[-1]}{'*' if f else ''} {s:.1f}s" for s, t, f in r["heavy"][:2])
    print(f"{r['name']:12} {r['ours']:6.1f} {r['perfect']:7.1f} {ex:6.1f} {r['selected']:5d} {r['failing']:4d}  {rules} | {heavy}")
tot = collections.defaultdict(float)
for r in rows:
    for l, v in r["share"].items():
        tot[re.sub(r" \[added line\]", "", l)] += v
print(f"\ncommits {len(rows)}  ours {sum(r['ours'] for r in rows):.1f}  perfect {sum(r['perfect'] for r in rows):.1f}  excess {sum(r['ours'] - r['perfect'] for r in rows):.1f}")
print("excess by rule:")
for l, v in sorted(tot.items(), key=lambda kv: -kv[1]):
    print(f"  {v:7.1f}  {l}")


# ---- which single tests set the busiest worker, and how often are they actually needed?
by_test = collections.defaultdict(lambda: [0, 0, 0.0])  # selected, failed, seconds saved if dropped when not failing
for r in rows:
    sel, fail = r["sel_set"], r["fail_set"]
    base = cost(pack(sorted(sel), MS))
    for n in sel:
        entry = by_test[n]
        entry[0] += 1
        if n in fail:
            entry[1] += 1
        else:
            entry[2] += (base - cost(pack(sorted(sel - {n}), MS))) / 1000
print("\nsingle tests ranked by seconds saved if dropped from the commits where they were not failing:")
print(f"{'seconds saved':>13} {'selected':>8} {'failed':>6}  test")
for n, (sel_n, fail_n, saved) in sorted(by_test.items(), key=lambda kv: -kv[1][2])[:8]:
    print(f"{saved:13.1f} {sel_n:8d} {fail_n:6d}  {TESTS[n]['id'].split('::', 1)[1]} ({SEC[n]:.1f}s)")
