"""Does the package still import after the patch?

A statement that runs when a module loads can crash the import, and a crash fails every test that imports the module
(the mutants m001 and m017 failed over a thousand tests that way). Rather than assume the worst for any statement that is
not a plain literal, import the patched package in a subprocess and see.

The subprocess imports `sqlglot` and every module under it, with the patched sources of the whole commit served by a
loader hook instead of the files on disk (the disk is read-only, and the other files are untouched). Anything other than a
clean run, including a timeout or a crash of the subprocess itself, counts as "does not import cleanly", so the caller
keeps its wide selection.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

TIMEOUT_SECONDS = 60

_SCRIPT = r'''
import importlib, importlib.machinery, json, os, sys, time

patched = json.loads(sys.stdin.read())
sys.dont_write_bytecode = True
get_data = importlib.machinery.SourceFileLoader.get_data
path_stats = importlib.machinery.SourceFileLoader.path_stats


def patched_get_data(self, path):
    if path in patched:
        return patched[path].encode("utf-8")
    return get_data(self, path)


def patched_path_stats(self, path):
    stats = path_stats(self, path)
    if path in patched:  # a modification time of "now" makes any cached bytecode of the original stale
        stats = dict(stats, mtime=time.time())
    return stats


importlib.machinery.SourceFileLoader.get_data = patched_get_data
importlib.machinery.SourceFileLoader.path_stats = patched_path_stats

root = os.environ["BR_ROOT"]
names = ["sqlglot"]
for top, _, files in os.walk(os.path.join(root, "sqlglot")):
    for f in sorted(files):
        if f.endswith(".py") and f != "__main__.py":
            rel = os.path.relpath(os.path.join(top, f), root)[:-3].replace(os.sep, ".")
            names.append(rel[: -len(".__init__")] if rel.endswith(".__init__") else rel)
failed = []
for name in sorted(set(names)):
    try:
        importlib.import_module(name)
    except BaseException as error:
        failed.append(f"{name}: {type(error).__name__}")
        break
print(json.dumps(failed))
sys.exit(1 if failed else 0)
'''


def imports_cleanly(root: str, patched: dict[str, str]) -> tuple[bool, str]:
    """(clean, detail). `patched` maps absolute source paths under `root` to their patched text."""
    env = dict(os.environ, BR_ROOT=root, PYTHONPATH=root, PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run([sys.executable, "-c", _SCRIPT], input=json.dumps(patched), env=env, cwd=root,
                              capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError) as error:
        return False, f"could not run the check: {type(error).__name__}"
    if proc.returncode == 0:
        return True, "imports cleanly"
    return False, (proc.stdout.strip().splitlines() or [proc.stderr.strip()[-200:] or "failed"])[-1]
