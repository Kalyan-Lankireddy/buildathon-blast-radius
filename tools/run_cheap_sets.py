"""Run each commit's cheap covering set in one pytest process on the patched sqlglot; record CPU and what failed.
Evaluation only. Inputs: /sets.json (from heavy_hidden.py), patches under /sample and /work.
"""
import json, os, re, resource, shutil, subprocess, sys, time

sys.path.insert(0, "/repo")
from br.patchparse import apply_hunks, parse_patch  # noqa: E402

ALL_IDS = [l.strip() for l in open("/ids.txt") if "::" in l]
sets = json.load(open("/sets.json"))
results = {}


def failed_ids(text):
    entries, current, on = [], None, False
    for line in text.splitlines():
        if "short test summary info" in line:
            on = True
            continue
        if not on:
            continue
        if re.match(r"^(FAILED|ERROR|SUBFAILED)\b", line):
            current = [line]
            entries.append(current)
        elif current is not None and not re.match(r"^=+ .* =+$|^\d+ (failed|passed|error)", line):
            current.append(line)
    found, unread = set(), 0
    for e in entries:
        j = " ".join(e)
        m = (re.search(r"[\]\)]\s+(tests/\S+)", j) if j.startswith("SUBFAILED") else re.match(r"^(?:FAILED|ERROR)\s+(tests/\S+)", j))
        if not m:
            unread += 1
            continue
        f = m.group(1)
        base = re.sub(r"\[.*$", "", f).rstrip(":")
        found.update(i for i in ALL_IDS if i == f or (("::" in f) and (i == base or i.startswith(base + "::"))) or (("::" not in f) and i.startswith(f + "::")))
    return found, unread


for s in sets:
    work = "/tmp/cheap"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree("/src/sqlglot", work, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    for fp in parse_patch(open(s["patch"], encoding="utf-8").read()):
        path = os.path.join(work, fp.path)
        new, _ = apply_hunks(open(path, encoding="utf-8").read(), fp.hunks)
        open(path, "w", encoding="utf-8").write(new)
    env = dict(os.environ, PYTHONPATH=work, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.time()
    proc = subprocess.run(["pytest", "-p", "no:cacheprovider", "-p", "no:xdist", "-q", "--tb=no", "-rfE"] + s["ids"],
                          cwd=work, env=env, capture_output=True, text=True)
    wall = time.time() - t0
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = after.ru_utime - before.ru_utime + after.ru_stime - before.ru_stime
    fails, unread = failed_ids(proc.stdout)
    fails = {f for f in fails if f in set(s["ids"])}
    results[s["name"]] = {"n": len(s["ids"]), "cpu": round(cpu, 2), "wall": round(wall, 2),
                          "local_failing": sorted(fails), "true_failing": s["true_failing_cheap"], "unread": unread,
                          "match": fails == set(s["true_failing_cheap"])}
    print(s["name"], len(s["ids"]), "cpu", round(cpu, 1), "match", results[s["name"]]["match"], flush=True)
    json.dump(results, open("/out/cheap_results.json", "w"), indent=1)
shutil.rmtree("/tmp/cheap", ignore_errors=True)
