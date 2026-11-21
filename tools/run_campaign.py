"""Campaign runner: for each mutant, (1) run its cheap covering set the way the policy would, in one isolated process,
(2) run the full suite without the heavy tests (xdist), (3) run the heavy tests serially with timeouts.
No failure is dropped as noise: crashes and timeouts count as failures and are flagged. Evaluation only.

Inputs: /mut (patches, index.json, cheap_sets.json), /ids.txt. Writes /mut/<name>.camp.json. Stops at $STOP_AT (HHMM).
"""
import json, os, re, resource, shutil, subprocess, sys, time

sys.path.insert(0, "/repo")
from br.patchparse import apply_hunks, parse_patch  # noqa: E402

MUT = "/mut"
ALL_IDS = [l.strip() for l in open("/ids.txt") if "::" in l]
HEAVY = ["tests/test_executor.py::TestExecutor::test_execute_tpcds", "tests/test_executor.py::TestExecutor::test_execute_tpch",
         "tests/test_executor.py::TestExecutor::test_optimized_tpch", "tests/test_generator.py::TestGenerator::test_generator_memory",
         "tests/test_parser.py::TestParser::test_max_nodes"]
cheap_sets = json.load(open(f"{MUT}/cheap_sets.json"))
STOP = int(os.environ.get("STOP_AT", "9999"))
TREE = "/tmp/camp_tree"


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


def run(cmd, env):
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=TREE, env=env, capture_output=True, text=True)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = after.ru_utime - before.ru_utime + after.ru_stime - before.ru_stime
    return proc.stdout, time.time() - t0, cpu


for name in sorted(cheap_sets):
    out = f"{MUT}/{name}.camp.json"
    if os.path.exists(out):
        continue
    if int(time.strftime("%H%M")) >= STOP:
        print("time box reached, stopping", flush=True)
        break
    shutil.rmtree(TREE, ignore_errors=True)
    shutil.copytree("/src/sqlglot", TREE, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    for fp in parse_patch(open(f"{MUT}/{name}.patch", encoding="utf-8").read()):
        path = os.path.join(TREE, fp.path)
        new, _ = apply_hunks(open(path, encoding="utf-8").read(), fp.hunks)
        open(path, "w", encoding="utf-8").write(new)
    env = dict(os.environ, PYTHONPATH=TREE, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    cheap = cheap_sets[name]["cheap"]
    base = ["pytest", "-q", "--tb=no", "-rfE", "-p", "no:cacheprovider"]
    # 1. what the policy would do: the cheap covering set, alone, one process
    text, cheap_wall, cheap_cpu = run(base + ["-p", "no:xdist", "--timeout=300"] + cheap, env)
    local, unread_l, _ = parse_failures(text)
    local &= set(cheap)
    # 2. ground truth, everything except the heavy tests
    deselect = [a for h in HEAVY for a in ("--deselect", h)]
    text, rest_wall, _ = run(base + ["-n", "8", "--timeout=300", "--continue-on-collection-errors"] + deselect, env)
    rest, unread_r, crashed_r = parse_failures(text)
    # 3. the heavy tests, serially, with generous timeouts
    text, heavy_wall, _ = run(base + ["-p", "no:xdist", "--timeout=900"] + HEAVY, env)
    heavy, unread_h, crashed_h = parse_failures(text)
    truth = rest | heavy
    cheap_truth = truth & set(cheap)
    heavy_failed = sorted(truth & set(HEAVY))
    res = {"cheap_n": len(cheap), "cheap_cpu": round(cheap_cpu, 2), "cheap_wall": round(cheap_wall, 1),
           "local_failing": sorted(local), "truth_failing_cheap": sorted(cheap_truth),
           "pass_locally_fail_in_truth": sorted(cheap_truth - local), "fail_locally_pass_in_truth": sorted(local - cheap_truth),
           "heavy_failed": heavy_failed, "heavy_selected": cheap_sets[name]["heavy"],
           "hidden": bool(heavy_failed) and not local, "hidden_vs_truth_cheap": bool(heavy_failed) and not cheap_truth,
           "unread": unread_l + unread_r + unread_h, "crashed_or_timeout": crashed_r + crashed_h,
           "truth_total_failing": len(truth), "walls": [round(rest_wall), round(heavy_wall)]}
    json.dump(res, open(out, "w"), indent=1)
    print(name, "cheap", len(cheap), "cpu", round(cheap_cpu, 1), "| local fail", len(local), "| heavy failed", len(heavy_failed),
          "| hidden", res["hidden"], "| pass-local/fail-truth", len(res["pass_locally_fail_in_truth"]), flush=True)
shutil.rmtree(TREE, ignore_errors=True)
