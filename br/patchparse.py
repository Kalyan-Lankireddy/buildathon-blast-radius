"""Unified diff parsing and application.

Strict on purpose: anything this module does not understand raises PatchError, and the selector then
runs the full suite for that commit, which can never miss.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


class PatchError(Exception):
    pass


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[tuple[str, str]]  # (' ' | '-' | '+', text without the newline)


@dataclass
class FilePatch:
    old_path: str | None = None  # None when the file is created
    new_path: str | None = None  # None when the file is deleted
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    renamed: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


@dataclass
class Edit:
    """What a file's hunks do, in old and new line coordinates (1-based)."""

    removed: list[int] = field(default_factory=list)  # old line numbers that are deleted or replaced
    # every added line: (new line number, nearest old line before it or 0, nearest old line after it)
    added: list[tuple[int, int, int]] = field(default_factory=list)
    removed_text: dict[int, str] = field(default_factory=dict)
    added_text: dict[int, str] = field(default_factory=dict)


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _clean_path(raw: str) -> str | None:
    raw = raw.split("\t")[0].strip()
    if raw == "/dev/null":
        return None
    if raw.startswith('"'):
        raise PatchError("quoted path")
    if raw[:2] in ("a/", "b/"):
        raw = raw[2:]
    return raw


def parse_patch(text: str) -> list[FilePatch]:
    lines = text.split("\n")
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            current = FilePatch()
            patches.append(current)
            m = re.match(r"diff --git a/(.+) b/(.+)$", line)
            if not m:
                raise PatchError("unparseable diff header")
            current.old_path, current.new_path = m.group(1), m.group(2)
            i += 1
        elif current is None:
            if line.strip() == "":
                i += 1
                continue
            raise PatchError("text before the first diff header")
        elif line.startswith("new file mode"):
            current.old_path = None
            i += 1
        elif line.startswith("deleted file mode"):
            current.new_path = None
            i += 1
        elif line.startswith("rename from "):
            current.renamed = True
            current.old_path = line[len("rename from "):]
            i += 1
        elif line.startswith("rename to "):
            current.renamed = True
            current.new_path = line[len("rename to "):]
            i += 1
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            current.binary = True
            i += 1
        elif line.startswith("--- "):
            old = _clean_path(line[4:])
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise PatchError("--- without +++")
            new = _clean_path(lines[i + 1][4:])
            if old is not None:
                current.old_path = old
            if new is not None:
                current.new_path = new
            i += 2
        elif line.startswith("@@"):
            m = _HUNK.match(line)
            if not m:
                raise PatchError("bad hunk header")
            old_start, old_len = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            new_start, new_len = int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1)
            i += 1
            hunk_lines: list[tuple[str, str]] = []
            old_seen = new_seen = 0
            while old_seen < old_len or new_seen < new_len:
                if i >= len(lines):
                    raise PatchError("hunk ends early")
                body = lines[i]
                tag = body[:1]
                if tag == "\\":
                    i += 1
                    continue
                if tag == "" :
                    tag, text = " ", ""  # a context line whose trailing space was stripped
                elif tag in " -+":
                    text = body[1:]
                else:
                    raise PatchError("bad hunk line")
                if tag in " -":
                    old_seen += 1
                if tag in " +":
                    new_seen += 1
                hunk_lines.append((tag, text))
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
            current.hunks.append(Hunk(old_start, old_len, new_start, new_len, hunk_lines))
        else:
            # index lines, mode lines, similarity, blank lines between diffs
            i += 1
    for patch in patches:
        if patch.old_path is None and patch.new_path is None:
            raise PatchError("no path")
    return patches


def apply_hunks(old_text: str, hunks: list[Hunk]) -> tuple[str, Edit]:
    """Return the patched text and the edit in old and new coordinates. Raises PatchError on mismatch."""
    old = old_text.split("\n")
    out: list[str] = []
    edit = Edit()
    pos = 0  # index into `old` of the next old line to copy
    for hunk in sorted(hunks, key=lambda h: h.old_start):
        start = hunk.old_start - 1 if hunk.old_len > 0 else hunk.old_start
        if start < pos or start > len(old):
            raise PatchError("overlapping or out of range hunk")
        out.extend(old[pos:start])
        pos = start
        for tag, text in hunk.lines:
            if tag in " -":
                if pos >= len(old) or old[pos] != text:
                    raise PatchError("context mismatch")
                if tag == "-":
                    edit.removed.append(pos + 1)
                    edit.removed_text[pos + 1] = text
                else:
                    out.append(text)
                pos += 1
            else:
                out.append(text)
                new_line = len(out)
                # `pos` old lines are consumed so far: the neighbours are old lines pos and pos + 1
                edit.added.append((new_line, pos, pos + 1))
                edit.added_text[new_line] = text
    out.extend(old[pos:])
    return "\n".join(out), edit
