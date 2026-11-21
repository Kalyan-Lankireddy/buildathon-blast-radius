"""Mutants aimed at code that heavy tests execute, where a gated 'verify cheap tests, drop heavy' policy would apply.

A mutant is kept when its selection holds test_execute_tpcds or test_generator_memory, it has a cheap covering set, and
that set has no TestOptimizer test (its class setup costs 13 to 14 s of CPU, so the policy would not
apply). Evaluation only. Usage: mutate_heavy.py <checkout> <maps> <out> <count> <seed>
"""
import ast, collections, difflib, importlib.util, json, os, random, sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, ".."))
from br.analyze import Analyzer, FullSuite, Maps  # noqa: E402
from br.patchparse import parse_patch  # noqa: E402
from br.pytree import PyTree  # noqa: E402

spec = importlib.util.spec_from_file_location("mutate_ops", os.path.join(here, "mutate.py"))
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)

root, maps_path, out, count, seed = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
rng = random.Random(seed)
M = Maps(maps_path)
TESTS = json.load(open("sample/tests.json"))["tests"]
NUM = {t["id"]: t["number"] for t in TESTS}
SEC = {t["number"]: t["seconds"] for t in TESTS}
IDX2NUM = [NUM.get(n) for n in M.tests]
TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
MEM = "tests/test_generator.py::TestGenerator::test_generator_memory"
HEAVY_IDX = {"tpcds": M.tests.index(TPCDS), "memory": M.tests.index(MEM)}
HEAVY_SETUP = ("TestOptimizer",)  # its class setup costs 13 to 14 s of CPU; TestExecutor only about 1.5 s


def numbers(mask):
    res, i = set(), 0
    while mask:
        if mask & 1 and IDX2NUM[i] is not None:
            res.add(IDX2NUM[i])
        mask >>= 1
        i += 1
    return res


class Tracing(Analyzer):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.cover = 0

    def old_line_mask(self, rel, otree, line):
        m = super().old_line_mask(rel, otree, line)
        self.cover |= m
        return m

    def new_line_mask(self, *a):
        m = super().new_line_mask(*a)
        self.cover |= m
        return m


def group(rel):
    if "/executor/" in rel:
        return "executor"
    if rel.endswith(("generator.py",)) or "/generators/" in rel:
        return "generator"
    if "/optimizer/" in rel:
        return "optimizer"
    if rel.endswith("dialects/dialect.py") or "/dialects/" in rel:
        return "dialect"
    return "core"


pool = {"tpcds": collections.defaultdict(list), "memory": collections.defaultdict(list)}
eligible = {}


def usable(sid):
    """A line the policy could apply to: a heavy test, a cheap test, and no cheap test from a heavy-setup class."""
    if sid not in eligible:
        nums = numbers(M.mask(sid))
        cheap = [n for n in nums if SEC[n] < 0.5]
        eligible[sid] = bool(cheap) and not any(TESTS[n]["id"].split("::")[1] in HEAVY_SETUP for n in cheap)
    return eligible[sid]


for rel, entry in M._files.items():
    if not rel.startswith("sqlglot/") or "/typing/" in rel:
        continue
    for line, sid in M.line_table(rel).items():
        mask = M.mask(sid)
        if not usable(sid):
            continue
        for name, idx in HEAVY_IDX.items():
            if (mask >> idx) & 1:
                pool[name][group(rel)].append((rel, line))
print({k: {g: len(v) for g, v in d.items()} for k, d in pool.items()})

quota = ([("tpcds", "executor", .50), ("tpcds", "core", .30), ("tpcds", "generator", .20)] if os.environ.get("TOPUP") else [("tpcds", "executor", .25), ("tpcds", "generator", .15), ("tpcds", "optimizer", .10), ("tpcds", "dialect", .05),
         ("tpcds", "core", .05), ("memory", "generator", .15), ("memory", "dialect", .15), ("memory", "core", .10)])
os.makedirs(out, exist_ok=True)
index, cheap_sets, seen = {}, {}, set()
if os.path.exists(os.path.join(out, "index.json")):  # top-up run: keep what exists and continue numbering
    index = json.load(open(os.path.join(out, "index.json")))
    cheap_sets = json.load(open(os.path.join(out, "cheap_sets.json")))
    seen = {(v["file"], v["line"]) for v in index.values()}
for heavy, grp, share in quota:
    want = round(count * share)
    got, tries = 0, 0
    cands = pool[heavy][grp]
    while got < want and cands and tries < 600:
        tries += 1
        rel, line = rng.choice(cands)
        if (rel, line) in seen:
            continue
        old = open(os.path.join(root, rel), encoding="utf-8").read()
        lines = old.split("\n")
        tree = PyTree(old)
        if tree.is_comment_or_blank(line) or tree.in_multiline_string(line):
            continue
        mutated = ops.mutate_line(lines[line - 1], rng)
        if not mutated or mutated == lines[line - 1]:
            continue
        new_lines = lines[:]
        new_lines[line - 1] = mutated
        new = "\n".join(new_lines)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        patch = ops.diff(rel, old, new)
        an = Tracing(M, root)
        try:
            mask = an.select(parse_patch(patch))
        except FullSuite:
            continue
        sel, cover = numbers(mask), numbers(an.cover & mask)
        heavy_ids = [TESTS[n]["id"] for n in sel if SEC[n] >= 3.0]
        cheap = sorted(n for n in sel & cover if SEC[n] < 0.5)
        if not heavy_ids or not cheap or any(TESTS[n]["id"].split("::")[1] in HEAVY_SETUP for n in cheap):
            continue
        name = f"h{len(index) + 1:03d}"
        open(os.path.join(out, name + ".patch"), "w").write(patch)
        scope = tree.scope_of(line).kind
        index[name] = {"file": rel, "line": line, "kind": f"{heavy}-{grp}", "scope": scope}
        cheap_sets[name] = {"heavy": heavy_ids, "cheap": [TESTS[n]["id"] for n in cheap]}
        seen.add((rel, line))
        got += 1
    print(f"{heavy}/{grp}: {got} of {want}")
json.dump(index, open(os.path.join(out, "index.json"), "w"), indent=1)
json.dump(cheap_sets, open(os.path.join(out, "cheap_sets.json"), "w"))
print(len(index), "mutants;", "scopes:", dict(collections.Counter(v["scope"] for v in index.values())),
      "| cheap-set sizes median", sorted(len(v["cheap"]) for v in cheap_sets.values())[len(cheap_sets) // 2])
