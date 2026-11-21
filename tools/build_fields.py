"""Build maps/fields.json: for each pair-format SQL fixture, which tests read which field (meta, sql, expected).

Facts about the pinned commit, from two sources that must agree before a fixture is used:
  * the recorder (tools/labeler/fieldtrack.py): Python-level reads of each field, per test, setup reads shared with the class
  * static reading of the test source: a test that opened the file only because its class stored the rows in
    setUpClass counts as "explained" when the recorder saw it never touch those rows (unpack or index one).
A fixture with any opener that neither source explains is left out, and the selector keeps its current rule for it.

Usage: python tools/build_fields.py <checkout> <maps.json.gz> <fields-*.jsonl dir> <out>
"""
import ast, collections, glob, json, os, re, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from br.analyze import Maps  # noqa: E402

root, maps_path, fields_dir, out_path = sys.argv[1:5]
M = Maps(maps_path)
records = [json.loads(l) for p in glob.glob(os.path.join(fields_dir, "fields-*.jsonl")) for l in open(p)]
by_class = collections.defaultdict(set)
for r in records:
    by_class[r["id"].rsplit("::", 1)[0]].add(r["id"])

readers = collections.defaultdict(lambda: collections.defaultdict(set))  # fixture -> field -> tests
for r in records:
    for f, k, _ in r["reads"]:
        readers[f][k].add(r["id"])
    for f, k in r["setup"]:  # class-level setup reads belong to every test in the class
        readers[f][k].update(by_class[r["id"].rsplit("::", 1)[0]])


def opens_of(path):
    mask = M.opens_mask(path)
    return {M.tests[i] for i in range(len(M.tests)) if (mask >> i) & 1}


_trees = {}


def class_node(test_file, cls):
    if test_file not in _trees:
        _trees[test_file] = ast.parse(open(os.path.join(root, test_file), encoding="utf-8").read())
    return next((n for n in _trees[test_file].body if isinstance(n, ast.ClassDef) and n.name == cls), None)


def loader_attrs(cls_node, fixture):
    """Attributes that setUpClass fills from the fixture: `cls.X = ... load_sql_fixture_pairs("<fixture>")`."""
    attrs = set()
    for fn in cls_node.body:
        if isinstance(fn, ast.FunctionDef) and fn.name == "setUpClass":
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign):
                    consts = [c.value for c in ast.walk(node.value) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
                    if fixture in consts:
                        attrs.update(t.attr for t in node.targets if isinstance(t, ast.Attribute))
    return attrs


def touches(cls_node, method, attrs, seen=None):
    """Does the method, or any self-method it calls, mention one of `attrs`?"""
    seen = seen or set()
    if method in seen:
        return False
    seen.add(method)
    fn = next((n for n in cls_node.body if isinstance(n, ast.FunctionDef) and n.name == method), None)
    if fn is None:
        return True  # inherited or dynamic: cannot tell, so assume it does
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute):
            if node.attr in attrs:
                return True
            if isinstance(node.value, ast.Name) and node.value.id in ("self", "cls") and \
                    any(isinstance(m, ast.FunctionDef) and m.name == node.attr for m in cls_node.body):
                if touches(cls_node, node.attr, attrs, seen):
                    return True
    return False


result, report = {}, []
for fixture, fields in sorted(readers.items()):
    path = f"tests/fixtures/{fixture}"
    openers = opens_of(path)
    touched_rows = fields.get("row", set())
    field_readers = {k: v for k, v in fields.items() if k != "row"}
    seen_readers = set().union(*field_readers.values()) if field_readers else set()
    problems = [t for t in sorted(touched_rows - seen_readers)]  # touched the rows but read no field: unknown use
    for t in sorted(openers - seen_readers - set(problems)):
        test_file, cls, method = (t.split("::") + [None, None])[:3]
        node = class_node(test_file, cls) if cls and method else None
        attrs = loader_attrs(node, fixture) if node else set()
        # explained when its class stored the rows in setUpClass, and the recorder saw this test never touch them
        if not attrs or t in touched_rows:
            problems.append(t)
    if openers - seen_readers and not problems:
        report.append(f"  explained statically: {len(openers - seen_readers)} class-sharing openers of {fixture} never touch its rows")
    if problems:
        report.append(f"  NOT explained: {fixture}: {len(problems)} openers, e.g. {problems[0]}")
        continue
    if not openers:
        report.append(f"  NOT explained: {fixture}: the map records no opener")
        continue
    result[path] = {k: sorted(v) for k, v in field_readers.items()}
json.dump({"files": result}, open(out_path, "w"), separators=(",", ":"))
print(f"{len(result)} of {len(readers)} fixtures explained -> {out_path} ({os.path.getsize(out_path) / 1000:.0f} KB)")
print("\n".join(report))
for path, f in sorted(result.items()):
    if "tpc" in path:
        print(path, {k: [t.split('::', 1)[1] for t in v] for k, v in f.items()})
