"""Follow what the dialect metaclass builds from a class constant.

`_Dialect.__new__` runs once per dialect class, at import, and turns class constants into other class attributes:

    numeric_escapes = klass.tokenizer_class.NUMERIC_ESCAPES
    if "x" in numeric_escapes: fmt = "\\x{:02x}" ...
    klass.ESCAPED_SEQUENCES.setdefault(chr(code), fmt.format(code))

so a test that never reads NUMERIC_ESCAPES can still depend on it through ESCAPED_SEQUENCES. `trace(function)` follows every
ALL_CAPS attribute read through local variables, branches and loops into the class attributes the function writes, and
reports for each written attribute which constants flow into it. A constant that reaches anything the tracer does not
understand (a call with side effects, a statement kind it does not model) is reported as untraced, and the caller keeps its
wide rule for it.
"""
from __future__ import annotations

import ast
import re

CAPS = re.compile(r"^[A-Z][A-Z0-9_]*$")
MUTATORS = {"setdefault", "update", "pop", "popitem", "append", "extend", "add", "remove", "discard", "clear", "insert"}
PURE_CALLS = {"seq_get", "new_trie", "_with_strict_time_inverse", "list", "dict", "set", "tuple", "sorted", "len", "any", "all",
              "next", "isinstance", "getattr", "chr", "ord", "zip", "enumerate", "range", "type", "str", "int", "bool"}
PURE_METHODS = {"items", "keys", "values", "get", "copy", "split", "join", "lower", "upper", "startswith", "endswith", "format",
                "strip", "replace", "isprintable", "isalpha", "isupper", "islower", "index", "count"}


class Tracer:
    def __init__(self):
        self.var: dict[str, set[str]] = {}      # local name -> constants that flow into it
        self.attr: dict[str, set[str]] = {}     # attribute name -> constants that flow into it
        self.derived: dict[str, set[str]] = {}  # attribute the function writes -> constants that flow into it
        self.failed: set[str] = set()
        self.local_defs: dict[str, set[str]] = {}

    # ---- what constants does an expression depend on?
    def sources(self, node) -> set[str]:
        out: set[str] = set()
        if node is None:
            return out
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Load):
                if CAPS.match(sub.attr):
                    out.add(sub.attr)
                out |= self.attr.get(sub.attr, set())
            elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                out |= self.var.get(sub.id, set())
                out |= self.local_defs.get(sub.id, set())
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and CAPS.match(sub.value):
                out.add(sub.value)  # getattr(x, "NAME")
        return out

    def untraced(self, constants: set[str]) -> None:
        self.failed |= constants

    def calls_are_pure(self, node) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if name not in PURE_CALLS and name not in PURE_METHODS and name not in self.local_defs and name not in MUTATORS:
                    return False
        return True

    # ---- statements
    def store(self, target, taint: set[str]) -> None:
        if isinstance(target, ast.Name):
            self.var.setdefault(target.id, set()).update(taint)
        elif isinstance(target, ast.Attribute):
            self.derived.setdefault(target.attr, set()).update(taint)
            self.attr.setdefault(target.attr, set()).update(taint)
        elif isinstance(target, ast.Subscript):
            base = target.value
            if isinstance(base, ast.Attribute):
                self.derived.setdefault(base.attr, set()).update(taint)
                self.attr.setdefault(base.attr, set()).update(taint)
            elif isinstance(base, ast.Name):
                self.var.setdefault(base.id, set()).update(taint)
            else:
                self.untraced(taint)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self.store(element, taint)
        elif isinstance(target, ast.Starred):
            self.store(target.value, taint)
        else:
            self.untraced(taint)

    def block(self, body, cond: set[str]) -> None:
        for stmt in body:
            self.statement(stmt, cond)

    def statement(self, stmt, cond: set[str]) -> None:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            value = stmt.value
            taint = self.sources(value) | cond
            if isinstance(stmt, ast.AugAssign):
                taint |= self.sources(stmt.target)
            if taint and value is not None and not self.calls_are_pure(value):
                self.untraced(taint)
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in targets:
                self.store(target, taint)
        elif isinstance(stmt, ast.Expr):
            call = stmt.value
            taint = self.sources(call) | cond
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr in MUTATORS \
                    and isinstance(call.func.value, ast.Attribute):
                self.store(call.func.value, taint)  # klass.ATTR.setdefault(...) rewrites ATTR
            elif taint and not (isinstance(call, ast.Constant)):
                self.untraced(taint)  # a bare call whose effect we do not model
        elif isinstance(stmt, ast.If):
            test = self.sources(stmt.test)
            self.block(stmt.body, cond | test)
            self.block(stmt.orelse, cond | test)
        elif isinstance(stmt, ast.For):
            it = self.sources(stmt.iter) | cond
            self.store(stmt.target, it)
            for _ in range(2):  # a value assigned late in the body can feed an earlier statement of the next pass
                self.block(stmt.body, cond | it)
            self.block(stmt.orelse, cond | it)
        elif isinstance(stmt, ast.FunctionDef):
            self.local_defs[stmt.name] = self.sources(stmt) | set()
        elif isinstance(stmt, (ast.Pass, ast.Continue, ast.Break, ast.Global, ast.Nonlocal)):
            pass
        else:  # return, raise, with, try, while, delete, import, ...: fine only if no constant is involved
            taint = self.sources(stmt) | cond
            if taint:
                self.untraced(taint)


def trace(function: ast.FunctionDef) -> tuple[dict[str, set[str]], set[str]]:
    """(attribute written -> constants flowing into it, constants that could not be followed)."""
    tracer = Tracer()
    tracer.block(function.body, set())
    return tracer.derived, tracer.failed
