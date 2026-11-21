"""Ground truth for any mutant patches (evaluation only): all tests but the heavy ones in xdist, then the heavy ones
serially with long timeouts. Nothing is dropped as noise: crashes and timeouts are recorded and flagged.

Inputs: /mut/*.patch, /ids.txt. Env PART=i/n runs every n-th mutant. Writes /mut/<name>.gt.json.
"""
import json, os, re, shutil, subprocess, sys, time

sys.path.insert(0, "/repo")
from br.patchparse import apply_hunks, parse_patch  # noqa: E402

MUT = "/mut"
ALL_IDS = [l.strip() for l in open("/ids.txt") if "::" in l]
HEAVY = ["tests/test_executor.py::TestExecutor::test_execute_tpcds", "tests/test_executor.py::TestExecutor::test_execute_tpch",
         "tests/test_executor.py::TestExecutor::test_optimized_tpch", "tests/test_generator.py::TestGenerator::test_generator_memory",
         "tests/test_parser.py::TestParser::test_max_nodes"]
part, parts = (int(x) for x in os.environ.get("PART", "0/1").split("/"))
TREE = f"/tmp/gt_tree{part}"
sys.path.insert(0, "/repo/tools")


def parse_failures(text):
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
    found, unread, crashed = set(), 0, 0
    for e in entries:
        j = " ".join(e)
        if re.search(r"worker 'gw\d", j) or "Timeout" in j:
            crashed += 1
        m = (re.search(r"[\]\)]\s+(tests/\S+)", j) if j.startswith("SUBFAILED") else re.match(r"^(?:FAILED|ERROR)\s+(tests/\S+)", j))
        if not m:
            unread += 1
            continue
        f = m.group(1)
        base = re.sub(r"\[.*$", "", f).rstrip(":")
        if "::" in f:
            found.update(i for i in ALL_IDS if i == f or i == base or i.startswith(base + "::"))
        else:
            found.update(i for i in ALL_IDS if i.startswith(f + "::"))
    return found, unread, crashed


names = sorted(f[:-6] for f in os.listdir(MUT) if f.endswith(".patch"))
for k, name in enumerate(names):
    if k % parts != part:
        continue
    out = f"{MUT}/{name}.gt.json"
    if os.path.exists(out):
        continue
    shutil.rmtree(TREE, ignore_errors=True)
    shutil.copytree("/src/sqlglot", TREE, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    for fp in parse_patch(open(f"{MUT}/{name}.patch", encoding="utf-8").read()):
        path = os.path.join(TREE, fp.path)
        new, _ = apply_hunks(open(path, encoding="utf-8").read(), fp.hunks)
        open(path, "w", encoding="utf-8").write(new)
    env = dict(os.environ, PYTHONPATH=TREE, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    base = ["pytest", "-q", "--tb=no", "-rfE", "-p", "no:cacheprovider"]
    t0 = time.time()
    deselect = [a for h in HEAVY for a in ("--deselect", h)]
    rest_text = subprocess.run(base + ["-n", "4", "--timeout=300", "--continue-on-collection-errors"] + deselect,
                               cwd=TREE, env=env, capture_output=True, text=True).stdout
    heavy_text = subprocess.run(base + ["-p", "no:xdist", "--timeout=900"] + HEAVY, cwd=TREE, env=env,
                                capture_output=True, text=True).stdout
    rest, u1, c1 = parse_failures(rest_text)
    heavy, u2, c2 = parse_failures(heavy_text)
    json.dump({"failing": sorted(rest | heavy), "unread": u1 + u2, "crashed_or_timeout": c1 + c2,
               "summary": [rest_text.strip().splitlines()[-1:], heavy_text.strip().splitlines()[-1:]],
               "seconds": round(time.time() - t0)}, open(out, "w"))
    print(name, len(rest | heavy), "failing", round(time.time() - t0), "s", flush=True)
shutil.rmtree(TREE, ignore_errors=True)
