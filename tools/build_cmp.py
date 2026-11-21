"""Fold the comparison profiler's per-process dumps into maps/cmp.json.gz (see tools/labeler/cmpprofile.py).

Usage: python tools/build_cmp.py <dump dir> <out file>
Keeps only the sites that ran at least once in the whole suite and where the swapped operator always agreed.
"""
import glob, gzip, json, os, sys

dump_dir, out = sys.argv[1:3]
total = {}
files = sorted(glob.glob(os.path.join(dump_dir, "cmp.*.json")))
for path in files:
    for key, (n, bad) in json.load(open(path)).items():
        t = total.setdefault(key, [0, 0])
        t[0] += n
        t[1] += bad
sites: dict[str, list] = {}
for key, (n, bad) in total.items():
    rel, line, col = key.rsplit(":", 2)
    if n > 0 and bad == 0:
        sites.setdefault(rel, []).append([int(line), int(col)])
for v in sites.values():
    v.sort()
with gzip.open(out, "wt", encoding="utf-8") as f:
    json.dump({"sites": sites}, f, separators=(",", ":"))
seen = len(total)
proved = sum(len(v) for v in sites.values())
print(f"{len(files)} dumps; {seen} sites executed; {proved} provable ({100 * proved / max(seen, 1):.0f}%); "
      f"{os.path.getsize(out) / 1000:.0f} KB -> {out}")
