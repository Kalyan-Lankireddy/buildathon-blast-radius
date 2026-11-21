"""A parsed Python file with the lookups the analysis needs: statements by line, scopes, spans."""
from __future__ import annotations

import ast

COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
            ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
if hasattr(ast, "TryStar"):
    COMPOUND += (ast.TryStar,)
if hasattr(ast, "Match"):
    COMPOUND += (ast.Match,)
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


class Scope:
    """Where a line lives. kind is one of runtime, funcheader, classbody, classheader, module."""

    def __init__(self, kind, stmt, func=None, cls=None):
        self.kind = kind
        self.stmt = stmt
        self.func = func   # nearest enclosing function node (runtime, funcheader)
        self.cls = cls     # qualname of the class (classbody, classheader)


class PyTree:
    def __init__(self, text: str):
        self.text = text
        self.lines = text.split("\n")
        self.module = ast.parse(text)
        self.stmts: list[tuple[int, int, int, ast.stmt]] = []  # (start, end, depth, node)
        self.parent: dict[int, ast.AST] = {}
        self.functions: dict[str, ast.AST] = {}
        self.multiline_strings: list[tuple[int, int]] = []
        self._walk()

    # ---------------------------------------------------------------- indexing
    def _walk(self):
        stack = [(self.module, 0)]
        while stack:
            node, depth = stack.pop()
            for child in ast.iter_child_nodes(node):
                self.parent[id(child)] = node
                d = depth
                if isinstance(child, ast.stmt):
                    d = depth + 1
                    self.stmts.append((self.start(child), child.end_lineno, d, child))
                    if isinstance(child, FUNCS):
                        self.functions.setdefault(self.qualname(child), child)
                elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                    if child.end_lineno > child.lineno:
                        self.multiline_strings.append((child.lineno, child.end_lineno))
                elif isinstance(child, ast.JoinedStr) and child.end_lineno > child.lineno:
                    self.multiline_strings.append((child.lineno, child.end_lineno))
                stack.append((child, d))

    @staticmethod
    def start(node) -> int:
        decorators = getattr(node, "decorator_list", None) or []
        return min([node.lineno] + [d.lineno for d in decorators])

    @staticmethod
    def header_end(node) -> int:
        body = getattr(node, "body", None)
        if isinstance(node, COMPOUND) and body:
            return max(node.lineno, body[0].lineno - 1)
        return node.end_lineno

    # ---------------------------------------------------------------- queries
    def innermost(self, line: int):
        best = None
        for start, end, depth, node in self.stmts:
            if start <= line <= end and (best is None or depth > best[0]):
                best = (depth, node)
        return best[1] if best else None

    def in_multiline_string(self, line: int) -> bool:
        return any(a < line <= b or a <= line < b for a, b in self.multiline_strings)

    def is_comment_or_blank(self, line: int) -> bool:
        if line < 1 or line > len(self.lines):
            return True
        stripped = self.lines[line - 1].strip()
        return (stripped == "" or stripped.startswith("#")) and not self.in_multiline_string(line)

    def child_stmts(self, node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                yield child
            elif isinstance(child, (ast.ExceptHandler, getattr(ast, "match_case", ()))):
                for sub in ast.iter_child_nodes(child):
                    if isinstance(sub, ast.stmt):
                        yield sub

    def own_lines(self, node) -> list[int]:
        """Lines of a statement that are not inside one of its child statements."""
        start, end = self.start(node), node.end_lineno
        if not isinstance(node, COMPOUND):
            return list(range(start, end + 1))
        taken: set[int] = set()
        for child in self.child_stmts(node):
            taken.update(range(self.start(child), child.end_lineno + 1))
        return [n for n in range(start, end + 1) if n not in taken]

    def qualname(self, node) -> str:
        parts = []
        cur = self.parent.get(id(node))
        parts.append(node.name)
        while cur is not None:
            if isinstance(cur, ast.ClassDef):
                parts.append(cur.name)
            elif isinstance(cur, FUNCS):
                parts.append("<locals>")
                parts.append(cur.name)
            cur = self.parent.get(id(cur))
        return ".".join(reversed(parts))

    def enclosing(self, node):
        """Ancestors of `node`, nearest first."""
        cur = self.parent.get(id(node))
        while cur is not None:
            yield cur
            cur = self.parent.get(id(cur))

    def scope_of(self, line: int) -> Scope | None:
        node = self.innermost(line)
        if node is None:
            return Scope("module", None)
        if isinstance(node, FUNCS) and line <= self.header_end(node):
            return Scope("funcheader", node, func=node)
        if isinstance(node, ast.ClassDef) and line <= self.header_end(node):
            return Scope("classheader", node, cls=self.qualname(node))
        for anc in self.enclosing(node):
            if isinstance(anc, FUNCS):
                return Scope("runtime", node, func=anc)
            if isinstance(anc, ast.ClassDef):
                return Scope("classbody", node, cls=self.qualname(anc))
        return Scope("module", node)

    def lambda_body_line(self, stmt, line: int) -> bool:
        """True when `line` is inside a lambda of `stmt` but not on the line the lambda starts."""
        for node in ast.walk(stmt):
            if isinstance(node, ast.Lambda) and node.lineno < line <= node.end_lineno:
                return True
        return False

    def class_names(self) -> list[str]:
        return [self.qualname(n) for _, _, _, n in self.stmts if isinstance(n, ast.ClassDef)]
