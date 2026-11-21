"""Comparison profiler (labeling only; never part of the submission image).

Rewrites every single-operator comparison in sqlglot's own source (is, is not, ==, !=, <, <=, >, >=) so that, besides
returning the original result, it also computes the result the swapped operator would have given
(is<->==, is not<->!=, <<->=<=, >><->=>=) and records whether the two agreed. A site where they agreed on every
execution of the whole test suite is one where such a swap cannot change what any test sees.

A site is recorded as "bad" when the swapped result differed, was not a plain bool, or raised. Sites never executed
are not provable. Import-time executions count: the rewrite happens in the import hook. Child processes (the test
suite's spawn pools, subprocess tests) load this through sitecustomize and each writes its own dump at exit.
"""
import ast
import atexit
import builtins
import importlib.machinery
import json
import operator
import os
import sys

SRC = os.environ.get("BR_CMP_SRC", "/src/sqlglot/sqlglot" + os.sep)
OUT = os.environ.get("BR_CMP_OUT")

NAMES = [ast.Is, ast.IsNot, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE]
FUNCS = [operator.is_, operator.is_not, operator.eq, operator.ne, operator.lt, operator.le, operator.gt, operator.ge]
ALT = [2, 3, 0, 1, 5, 4, 7, 6]
KIND = {cls: i for i, cls in enumerate(NAMES)}
STATS = {}


def _record(key, op, a, b):
    res = FUNCS[op](a, b)  # the original behaviour, exceptions included
    try:
        alt = FUNCS[ALT[op]](a, b)
        ok = type(res) is bool and type(alt) is bool and res is alt
    except BaseException:
        ok = False
    rec = STATS.get(key)
    if rec is None:
        STATS[key] = rec = [0, 0]
    rec[0] += 1
    if not ok:
        rec[1] += 1
    return res


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in KIND:
            return node
        key = f"{self.rel}:{node.lineno}:{node.col_offset}"
        call = ast.Call(func=ast.Name(id="br_cmp_hook", ctx=ast.Load()),
                        args=[ast.Constant(key), ast.Constant(KIND[type(node.ops[0])]), node.left, node.comparators[0]],
                        keywords=[])
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):  # never use cached bytecode: always rewrite from source
        path = self.get_filename(fullname)
        data = self.get_data(path)
        rel = "sqlglot/" + path[len(SRC):].replace(os.sep, "/")
        tree = ast.parse(data, path)
        tree = ast.fix_missing_locations(_Rewrite(rel).visit(tree))
        return compile(tree, path, "exec", dont_inherit=True)


class _Finder:
    @classmethod
    def find_spec(cls, name, path=None, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader) \
                and spec.origin and spec.origin.startswith(SRC):
            spec.loader = _Loader(spec.loader.name, spec.loader.path)
        return spec


def _dump():
    if OUT:
        with open(os.path.join(OUT, f"cmp.{os.getpid()}.json"), "w") as f:
            json.dump(STATS, f)


def install():
    sys.dont_write_bytecode = True
    builtins.br_cmp_hook = _record
    sys.meta_path.insert(0, _Finder)
    atexit.register(_dump)
