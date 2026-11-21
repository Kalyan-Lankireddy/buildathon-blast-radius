"""Ground truth for mutants: apply each patch to a copy of the checkout and run the real test suite.

Runs inside the labeler image. For every <name>.patch in $MUT_DIR it writes <name>.truth.json with the
failing test ids (a collection error fails every test in that file, as the organizers count it).
"""
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, "/repo")
from br.patchparse import apply_hunks, parse_patch  # noqa: E402

SRC = "/src/sqlglot"
MUT = os.environ.get("MUT_DIR", "/mut")
WORK = "/tmp/mutant"


def run(name):
    out = os.path.join(MUT, name + ".truth.json")
    if os.path.exists(out):
        return
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.copytree(SRC, WORK, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    for fp in parse_patch(open(os.path.join(MUT, name + ".patch"), encoding="utf-8").read()):
        path = os.path.join(WORK, fp.path)
        new, _ = apply_hunks(open(path, encoding="utf-8").read(), fp.hunks)
        open(path, "w", encoding="utf-8").write(new)
    env = dict(os.environ, PYTHONPATH=WORK, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        ["pytest", "-n", "8", "-q", "-p", "no:cacheprovider", "--tb=no", "-rfE", "--continue-on-collection-errors"],
        cwd=WORK, env=env, capture_output=True, text=True,
    )
    text = proc.stdout
    failing = set()
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
            current.append(line)  # a subtest title can span several lines
    for entry in entries:
        joined = " ".join(entry)
        if joined.startswith("SUBFAILED"):
            m = re.search(r"[\]\)]\s+(tests/\S+)", joined)  # SUBFAILED[title] or SUBFAILED(k=v, ...)
        else:
            m = re.match(r"^(?:FAILED|ERROR)\s+(tests/\S+)", joined)
        if m:
            failing.add(m.group(1))
    ids = [l.strip() for l in open("/ids.txt") if "::" in l]
    result = set()
    for f in failing:
        if "::" in f:
            base = re.sub(r"\[.*$", "", f).rstrip(":")
            result.update(i for i in ids if i == base or i.startswith(base + "::") or i == f)
        else:  # a whole file failed to import
            result.update(i for i in ids if i.startswith(f + "::"))
    summary = text.strip().splitlines()[-1] if text.strip() else ""
    failed_lines = [" ".join(e)[:2000] for e in entries]
    reported = re.search(r"(\d+) (failed|error)", summary)
    if reported and not result:
        print(f"WARNING {name}: pytest reported failures but none were parsed", flush=True)
    json.dump({"failing": sorted(result), "summary": summary, "returncode": proc.returncode,
               "unparsed": bool(reported and not result),
               "failed_lines": failed_lines[:40]}, open(out, "w"))
    print(name, len(result), summary, flush=True)


if __name__ == "__main__":
    names = sorted(f[:-6] for f in os.listdir(MUT) if f.endswith(".patch"))
    for name in names:
        run(name)
