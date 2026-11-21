"""Run a few cheap tests on the patched code, in one process, and say whether every one passed.

Used by the selector to decide that a heavy test can be left out of the CI selection (see selector.py). The question
asked here is narrow: "did all of these tests pass?". Anything else, a failure, an error, a skip, a timeout, a crash,
a budget kill, a missing test, means "no", and the caller falls back to its normal selection.
"""
from __future__ import annotations

import json
import os
import re
import resource
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def all_passed(root: str, patched: dict[str, str], ids: list[str], cpu_seconds: int, wall_seconds: float) -> tuple[bool, str, float]:
    """Returns (every test passed, a short reason, CPU seconds the child used)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8") as f:
        json.dump(patched, f)
        f.flush()
        env = dict(os.environ, BR_PATCHED=f.name, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=os.pathsep.join([HERE, root]))
        cmd = [sys.executable, "-m", "pytest", "-q", "--tb=no", "-rfEsxX", "-p", "no:cacheprovider", "-p", "no:xdist", "-x",
               "-p", "br.verify_plugin", "--timeout=15"] + ids

        def limit():  # a runaway local check is killed at its CPU budget
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))

        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            proc = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, timeout=wall_seconds, preexec_fn=limit)
        except subprocess.TimeoutExpired:
            return False, "timeout", _cpu_since(before)
        except Exception as error:  # never let the check crash the selector
            return False, f"could not run: {type(error).__name__}", _cpu_since(before)
        cpu = _cpu_since(before)
    text = proc.stdout
    summary = next((l for l in reversed(text.splitlines()) if re.search(r"\d+ passed", l)), "")
    if proc.returncode != 0:
        return False, f"exit {proc.returncode}", cpu
    if not summary or re.search(r"\b(failed|error|errors|skipped|xfailed|xpassed|warnings?)\b", re.sub(r"\d+ warnings?", "", summary)):
        return False, f"summary: {summary or 'none'}", cpu
    if re.search(r"^(FAILED|ERROR|SUBFAILED|SKIPPED)\b", text, re.M):
        return False, "failure lines present", cpu
    return True, summary.strip("= "), cpu


def _cpu_since(before) -> float:
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return after.ru_utime - before.ru_utime + after.ru_stime - before.ru_stime
