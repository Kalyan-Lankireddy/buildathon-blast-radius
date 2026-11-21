"""Blast Radius selector: for each commit, print which tests to run and how to split them.

The selection comes from maps measured on sqlglot at the pinned commit (per-test line coverage, which
classes each test built, which files it opened). See br/analyze.py for the rules. Any commit this
program cannot read with confidence gets the full suite, which never misses.

Logs go to stderr; stdout holds only the answers.
"""
from __future__ import annotations

import json
import os
import resource
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from br.analyze import Analyzer, FullSuite, Maps  # noqa: E402
from br.packing import pack  # noqa: E402
from br.patchparse import PatchError, parse_patch  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = os.environ.get("BR_SRC", "/opt/sqlglot")
MAPS = os.environ.get("BR_MAPS", str(HERE / "maps" / "maps.json.gz"))
EXPLAIN = bool(os.environ.get("BR_EXPLAIN"))


def duration_ms(test: dict) -> int:
    return int(test["seconds"] * 1000 + 0.5)  # the scorer's rule: round(seconds * 1000), halves up


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: duration_ms(t) for t in tests}
    number_by_id = {t["id"]: t["number"] for t in tests}
    all_numbers = sorted(ms)

    maps = Maps(MAPS)
    # map index (position in the labeling run) -> test number in this tests.json; unknown ids are dropped
    number_of_index = [number_by_id.get(name) for name in maps.tests]
    known = {n for n in number_of_index if n is not None}
    missing = [n for n in all_numbers if n not in known]  # tests the maps have never seen: always run

    def numbers(mask: int) -> list[int]:
        out, i = [], 0
        while mask:
            if mask & 1 and number_of_index[i] is not None:
                out.append(number_of_index[i])
            mask >>= 1
            i += 1
        return out

    def ci_seconds(mask: int) -> float:
        workers = [w for w in pack(sorted(set(numbers(mask)) | set(missing)), ms) if w]
        return 0.0 if not workers else (max(sum(ms[n] for n in w) for w in workers) + 2000 * len(workers)) / 1000

    full_packed = pack(all_numbers, ms)
    full_selected = 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        analyzer = Analyzer(maps, SRC)
        analyzer.cost_fn = ci_seconds
        try:
            files = parse_patch(patch.read_text(encoding="utf-8"))
            mask = analyzer.select(files)
            chosen = sorted(set(numbers(mask)) | set(missing)) if mask else sorted(missing)
            workers = pack(chosen, ms)
        except (FullSuite, PatchError, UnicodeDecodeError, RecursionError, SyntaxError, ValueError) as error:
            analyzer.note(f"fallback to full suite: {error}")
            workers = full_packed
            full_selected += 1
        except Exception as error:  # never crash: a crash is a rejected run
            analyzer.note(f"fallback to full suite after unexpected {type(error).__name__}: {error}")
            workers = full_packed
            full_selected += 1
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        if EXPLAIN:
            print(f"== {patch.stem}", file=sys.stderr)
            for line in analyzer.reasons:
                print(f"   {line}", file=sys.stderr)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)  # the import check runs in a subprocess; the board counts it
    cpu = time.process_time() + children.ru_utime + children.ru_stime
    print(f"selected for {full_selected} commits: the full suite; cpu {cpu:.2f} s "
          f"({children.ru_utime + children.ru_stime:.2f} s in subprocesses)", file=sys.stderr)


if __name__ == "__main__":
    main()
