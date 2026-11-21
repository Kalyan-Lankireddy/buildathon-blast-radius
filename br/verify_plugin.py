"""pytest plugin for the selector's local check: serve the commit's patched sources instead of the files on disk.

The disk is read-only, so the patched text of each changed package file comes from a JSON file named by BR_PATCHED
(path -> new text). Same loader hook as br/importcheck.py. Loaded with `-p br.verify_plugin` before sqlglot is imported.
"""
import importlib.machinery
import json
import os
import sys
import time

sys.dont_write_bytecode = True
_patched = json.load(open(os.environ["BR_PATCHED"], encoding="utf-8"))
_get_data = importlib.machinery.SourceFileLoader.get_data
_path_stats = importlib.machinery.SourceFileLoader.path_stats


def _patched_get_data(self, path):
    if path in _patched:
        return _patched[path].encode("utf-8")
    return _get_data(self, path)


def _patched_path_stats(self, path):
    stats = _path_stats(self, path)
    if path in _patched:  # a modification time of "now" makes any cached bytecode of the original stale
        stats = dict(stats, mtime=time.time())
    return stats


importlib.machinery.SourceFileLoader.get_data = _patched_get_data
importlib.machinery.SourceFileLoader.path_stats = _patched_path_stats
