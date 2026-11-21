"""Measure what running a commit's selected tests ourselves would cost. Evaluation only, never shipped.

For each commit with a non-empty selection: copy sqlglot, apply the patch, run exactly the selected tests with one
pytest process, and record wall time, CPU seconds (user + system, children included), and which tests failed. The
local failures are compared with the real answers to see whether a local run is a faithful stand-in for CI.
Runs inside the labeler image: python /repo/tools/estimate_local_run.py
"""
import json, os, re, resource, shutil, subprocess, sys, time

sys.path.insert(0, "/repo")
from br.patchparse import apply_hunks, parse_patch  # noqa: E402

SAMPLE, SELECTION, OUT = "/sample", "/selection.jsonl", "/out/local_run.json"
tests = json.load(open(f"{SAMPLE}/tests.json"))["tests"]
by_number = {t["number"]: t for t in tests}
answers = json.load(open(f"{SAMPLE}/answers.json"))["commits"]
ids_all = [t["id"] for t in tests]
results = {}
only = set(sys.argv[1:])


def parse_failures(text):
    entries, current, in_summary = [], None, False
    for line in text.splitlines():
        if "short test summary info" in line:
            in_summary = True
            continue
        if not in_summary:
            continue
        if re.match(r"^(FAILED|ERROR|SUBFAILED)\b", line):
            current = [line]
            entries.append(current)
        elif current is not None and not re.match(r"^=+ .* =+$|^\d+ (failed|passed|error)", line):
            current.append(line)
    found, unread = set(), 0
    for e in entries:
        joined = " ".join(e)
        m = (re.search(r"[\]\)]\s+(tests/\S+)", joined) if joined.startswith("SUBFAILED")
             else re.match(r"^(?:FAILED|ERROR)\s+(tests/\S+)", joined))
        if not m:
            unread += 1
            continue
        f = m.group(1)
        base = re.sub(r"\[.*$", "", f).rstrip(":")
        if "::" in f:
            found.update(i for i in ids_all if i == base or i.startswith(base + "::") or i == f)
        else:
            found.update(i for i in ids_all if i.startswith(f + "::"))
    return found, unread


for line in open(SELECTION):
    rec = json.loads(line)
    name = rec["commit"]
    if only and name not in only:
        continue
    numbers = sorted(n for w in rec["workers"] for n in w)
    if not numbers:
        results[name] = {"selected": 0}
        continue
    work = f"/tmp/t_{name}"
    shutil.rmtree(work, ignore_errors=True)
    t0 = time.time()
    shutil.copytree("/src/sqlglot", work, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    for fp in parse_patch(open(f"{SAMPLE}/commits/{name}.patch", encoding="utf-8").read()):
        path = os.path.join(work, fp.path)
        new, _ = apply_hunks(open(path, encoding="utf-8").read(), fp.hunks)
        open(path, "w", encoding="utf-8").write(new)
    prep = time.time() - t0
    ids = [by_number[n]["id"] for n in numbers]
    env = dict(os.environ, PYTHONPATH=work, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    w0 = time.time()
    proc = subprocess.run(["pytest", "-p", "no:cacheprovider", "-q", "--tb=no", "-rfE", "-p", "no:xdist"] + ids,
                          cwd=work, env=env, capture_output=True, text=True)
    wall = time.time() - w0
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    failing, unread = parse_failures(proc.stdout)
    truth = {by_number[n]["id"] for n in answers[name]["failing"]}
    results[name] = {
        "selected": len(numbers), "recorded_s": round(sum(by_number[n]["seconds"] for n in numbers), 2),
        "wall": round(wall, 2), "cpu": round(cpu, 2), "prep_copy_s": round(prep, 2),
        "local_failing": sorted(failing), "true_failing": sorted(truth), "unread": unread,
        "match": failing == truth, "summary": (proc.stdout.strip().splitlines() or [""])[-1],
    }
    print(name, results[name]["selected"], "cpu", results[name]["cpu"], "wall", results[name]["wall"], "match", results[name]["match"], flush=True)
    shutil.rmtree(work, ignore_errors=True)
    json.dump(results, open(OUT, "w"), indent=1)
json.dump(results, open(OUT, "w"), indent=1)
