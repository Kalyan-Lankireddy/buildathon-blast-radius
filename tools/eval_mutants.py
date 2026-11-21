"""Score the selector on mutant patches whose real test outcome is known (evaluation only).

Usage: python tools/eval_mutants.py <mutant dir with *.patch and *.truth.json> <tests.json> <checkout> <maps>
"""
import json
import os
import re
import subprocess
import sys
import tempfile

mut, tests_json, checkout, maps = sys.argv[1:5]
here = os.path.dirname(os.path.abspath(__file__))
tests = json.load(open(tests_json))["tests"]
number = {t["id"]: t["number"] for t in tests}
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
ALL_IDS = [l.strip() for l in open(os.path.join(here, "..", "work", "out", "collected.txt")) if "::" in l]


NOISE = []


def _crashed(truth, test_id):
    return any(test_id in l and re.search(r"worker 'gw\d", l) for l in truth.get('failed_lines', []))


def reparse(truth):
    """Failing tests from the stored failure lines, with the SUBFAILED[...] and SUBFAILED(...) forms.

    Returns (failing ids, number of entries that could not be read). The runner's own list is kept too.
    """
    import re

    found, bad = {i for i in truth["failing"] if not _crashed(truth, i)}, 0
    for joined in truth.get("failed_lines", []):
        if re.search(r"worker 'gw\d", joined):
            # An xdist worker died under load (the heaviest tests do this when containers share the machine).
            # They pass on an idle machine, so this is noise in the ground truth, not an effect of the patch.
            NOISE.append(joined[:60])
            continue
        m = (re.search(r"[\]\)]\s+(tests/\S+)", joined) if joined.startswith("SUBFAILED")
             else re.match(r"^(?:FAILED|ERROR)\s+(tests/\S+)", joined))
        if not m:
            bad += 1
            continue
        f = m.group(1)
        if "::" in f:
            base = re.sub(r"\[.*$", "", f).rstrip(":")
            found.update(i for i in ALL_IDS if i == base or i.startswith(base + "::") or i == f)
        else:
            found.update(i for i in ALL_IDS if i.startswith(f + "::"))
    return found, bad


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
    if os.environ.get("BR_IMAGE"):  # run the submission image under the scorer's limits instead of the local files
        cmd = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none", "--cpus", "2", "--memory", "4096m",
               "--memory-swap", "4096m", "--pids-limit", "256", "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534", "--env", "HOME=/tmp",
               "-e", "BR_EXPLAIN=1", "-v", f"{data}:/data:ro", os.environ["BR_IMAGE"]]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        open("/tmp/eval_image.err", "w").write(proc.stderr)
        out = proc.stdout
    else:
        out = subprocess.run([sys.executable, "-I", os.path.join(here, "..", "selector.py")], env=env,
                             capture_output=True, text=True).stdout
chosen = {json.loads(l)["commit"]: json.loads(l)["workers"] for l in out.splitlines()}


def cost(workers):
    w = [x for x in workers if x]
    return 0 if not w else max(sum(ms[n] for n in x) for x in w) + 2000 * len(w)


misses, total, rows = 0, 0, []
for n in names:
    truth = json.load(open(os.path.join(mut, n + ".truth.json")))
    parsed, unreadable = reparse(truth)
    if unreadable or (re.search(r"\d+ (failed|error)", truth["summary"]) and not parsed):
        print(f"UNPARSED {n}: {unreadable} failure entries could not be read; rerun it")
    failing = {number[i] for i in parsed if i in number}
    sel = {x for w in chosen[n] for x in w}
    missed = sorted(failing - sel)
    misses += bool(missed)
    total += cost(chosen[n])
    rows.append((bool(missed), n, index[n]["kind"], index[n]["file"], index[n]["line"], len(failing), len(sel), cost(chosen[n]) / 1000, missed[:3]))
rows.sort(reverse=True)
for miss, n, kind, f, line, nf, ns, c, m in rows:
    print(f"{'MISS' if miss else 'ok  '} {n} {kind:10} {f}:{line:<5} failing {nf:3d} selected {ns:4d} cost {c:5.1f}s {m if miss else ''}")
if NOISE:
    print(f"ignored {len(NOISE)} crashed-worker failures (flaky under load): {sorted(set(NOISE))[:3]}")
print(f"mutants {len(names)}  misses {misses}  total cost {total / 1000:.1f}s  (a miss adds 300 s)")
