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
from br.verify import all_passed  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = os.environ.get("BR_SRC", "/opt/sqlglot")
MAPS = os.environ.get("BR_MAPS", str(HERE / "maps" / "maps.json.gz"))
EXPLAIN = bool(os.environ.get("BR_EXPLAIN"))
VERIFY = os.environ.get("BR_VERIFY", "0") == "1"

# The local check (see verify_choice): a heavy test is dropped from the CI selection only when the cheap tests that
# run the same changed lines pass on the patched code.
HEAVY_SECONDS = 3.0         # a selected test this slow can be dropped ...
BIG_SECONDS = 20.0          # ... when at least one this slow is selected
CHEAP_SECONDS = 0.5         # the covering tests we run locally are at most this slow
HEAVY_SETUP_CLASSES = ("TestOptimizer",)  # class setup costs 13 to 14 s of CPU, so its tests are never "cheap"
MIN_SAVING_SECONDS = 8.0    # the local run costs about 4 s of CPU on a laptop; do not start it for less
CHECK_CPU_SECONDS = 10      # a local run that has used this much CPU is killed
CHECK_WALL_SECONDS = 30.0
BUDGET_SHARE = 0.5          # stop checking when this share of the run's time limit has passed
TIME_LIMIT_SECONDS = float(os.environ.get("BR_TIME_LIMIT", "180"))  # the practice limit; the final's is longer
MAX_WASTED_RUNS = int(os.environ.get("BR_MAX_WASTED", "5"))  # stop trying when wasted runs outnumber twice the passes by this much


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

    started = time.monotonic()
    test_by_number = {t["number"]: t for t in tests}
    wasted = passes = 0

    def verify_choice(analyzer, mask, chosen):
        """Drop heavy tests the cheap covering tests vouch for. Returns the new chosen list (or the old one).

        Only for a commit whose edits are all inside function bodies of package files (nothing runs at import, no
        test or data file), where a test of 20 s or more is selected and cheap tests cover the changed lines. Those
        cheap tests run here, on the patched code. If every one passes, the heavy tests and those cheap tests leave
        the CI selection. A failure, an error, a skip, a timeout, a crash, or an exhausted budget keeps the normal one.
        """
        nonlocal wasted, passes
        heavy = {n for n in chosen if test_by_number[n]["seconds"] >= HEAVY_SECONDS}
        if not (VERIFY and analyzer.body_only and wasted - 2 * passes < MAX_WASTED_RUNS
                and any(test_by_number[n]["seconds"] >= BIG_SECONDS for n in heavy)):
            return chosen
        cheap = sorted(n for n in numbers(analyzer.cover & mask) if n in set(chosen) and n not in heavy
                       and test_by_number[n]["seconds"] < CHEAP_SECONDS
                       and test_by_number[n]["id"].split("::")[1] not in HEAVY_SETUP_CLASSES)
        if not cheap:
            return chosen
        keep = [n for n in chosen if n not in heavy and n not in set(cheap)]
        saving = ci_cost(chosen) - ci_cost(keep)
        if saving < MIN_SAVING_SECONDS:
            analyzer.note(f"local check skipped: saves only {saving:.1f} s")
            return chosen
        if time.monotonic() - started > BUDGET_SHARE * TIME_LIMIT_SECONDS:
            analyzer.note("local check skipped: out of time budget")
            return chosen
        ok, detail, cpu = all_passed(SRC, analyzer.patched_sources, [test_by_number[n]["id"] for n in cheap],
                                     CHECK_CPU_SECONDS, CHECK_WALL_SECONDS)
        analyzer.note(f"local check of {len(cheap)} covering tests: {'passed' if ok else 'not clean'} ({detail}, {cpu:.1f} s CPU)")
        if not ok:
            wasted += 1
            return chosen
        passes += 1
        analyzer.note(f"local check passed: dropping {len(heavy)} heavy tests and {len(cheap)} verified ones, saves {saving:.1f} s")
        return keep

    def ci_cost(chosen):
        workers = [w for w in pack(sorted(chosen), ms) if w]
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
            chosen = verify_choice(analyzer, mask, chosen)
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
