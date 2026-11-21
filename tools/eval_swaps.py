"""Score the selector on the operator-swap mutants against their ground truth (evaluation only).
Usage: eval_swaps.py <mutant dir> <tests.json> [image]   (image: run the submission image; else the local selector)"""
import json, os, subprocess, sys, tempfile
mut, tests_json = sys.argv[1:3]
image = sys.argv[3] if len(sys.argv) > 3 else None
T = json.load(open(tests_json))["tests"]
NUM = {t["id"]: t["number"] for t in T}
here = os.path.dirname(os.path.abspath(__file__))
names = sorted(f[:-8] for f in os.listdir(mut) if f.endswith(".gt.json"))
index = json.load(open(os.path.join(mut, "index.json")))
with tempfile.TemporaryDirectory() as data:
    os.makedirs(os.path.join(data, "commits"))
    for n in names:
        open(os.path.join(data, "commits", n + ".patch"), "w").write(open(os.path.join(mut, n + ".patch")).read())
    json.dump({"tests": T}, open(os.path.join(data, "tests.json"), "w"))
    cmd = (["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777",
            "--user", "65534:65534", "-e", "HOME=/tmp", "-e", "BR_EXPLAIN=1", "-v", f"{data}:/data:ro", image] if image else
           [sys.executable, "-I", os.path.join(here, "..", "selector.py")])
    env = dict(os.environ, BR_DATA=data, BR_EXPLAIN="1", BR_SRC=os.environ.get("BR_SRC", "/opt/sqlglot"), BR_MAPS=os.environ.get("BR_MAPS", ""))
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
out = {json.loads(l)["commit"]: {n for w in json.loads(l)["workers"] for n in w} for l in proc.stdout.splitlines()}
free = {b.split("\n")[0] for b in ("\n" + proc.stderr).split("\n== ")[1:] if "proves harmless" in b}
misses, by_kind = [], {}
for n in names:
    gt = json.load(open(os.path.join(mut, n + ".gt.json")))
    fail = {NUM[i] for i in gt["failing"] if i in NUM}
    k = index[n]["kind"]
    s = by_kind.setdefault(k, {"n": 0, "free": 0, "failing": 0, "miss": 0})
    s["n"] += 1
    s["free"] += n in free
    s["failing"] += bool(fail)
    if fail - out[n]:
        s["miss"] += 1
        misses.append((n, index[n], sorted(fail - out[n])[:5], n in free))
print(json.dumps(by_kind, indent=1))
print("MISSES:", misses)
print("crashes/unread in ground truth:", sum(1 for n in names if (lambda g: g["crashed_or_timeout"] or g["unread"])(json.load(open(os.path.join(mut, n + ".gt.json"))))))
