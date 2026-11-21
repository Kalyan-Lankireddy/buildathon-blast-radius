"""Which field of a pair-format SQL fixture does an edit change?

tests/helpers.py reads these files like this (load_sql_fixture_pairs):

    statements = _filter_comments(text).split(";")      # drops empty lines and lines that start with "--"
    for i in range(0, len(statements), 2):
        sql, meta = _extract_meta(statements[i].strip())  # leading "#" lines are meta
        expected = statements[i + 1].strip()
        yield meta, sql, expected

So statement 0, 2, 4... is an input (its meta lines and its sql) and statement 1, 3, 5... is the expected output.
`changed_field` returns "sql" or "expected" when every changed line plainly belongs to one of them, and None whenever
that is not certain, so the caller keeps the wider rule.
"""
from __future__ import annotations

import re

# str.splitlines() also splits on these; the line numbers of a patch do not, so such a file is not analysed
_ODD_BREAKS = re.compile("[\r\x0b\x0c\x1c\x1d\x1e\x85  ]")


def _lines(text: str):
    """line number (1-based) -> (statement index, text) for the lines the loader keeps, or None if unsure."""
    if _ODD_BREAKS.search(text):
        return None
    out, stmt = {}, 0
    for number, line in enumerate(text.split("\n"), start=1):
        if not line or line.startswith("--"):
            continue  # dropped by _filter_comments
        semicolons = line.count(";")
        if semicolons == 0:
            out[number] = (stmt, line)
        elif semicolons == 1 and line.split(";", 1)[1].strip() == "":
            out[number] = (stmt, line)  # the line closes its statement; nothing of the next one is on it
            stmt += 1
        else:
            out[number] = (None, line)  # statements share this line
    return out


def changed_field(old_text: str, new_text: str, removed: list[int], added: list[int]) -> str | None:
    old, new = _lines(old_text), _lines(new_text)
    if old is None or new is None:
        return None
    parities = set()
    for lines, numbers in ((old, removed), (new, added)):
        for n in numbers:
            if n not in lines:
                return None  # a comment or blank line: leave it to the wider rule
            stmt, text = lines[n]
            if stmt is None or text.lstrip().startswith("#"):
                return None  # two statements on one line, or a meta line
            parities.add(stmt % 2)
    removed_semicolons = sum(old[n][1].count(";") for n in removed)
    added_semicolons = sum(new[n][1].count(";") for n in added)
    if removed_semicolons != added_semicolons or len(parities) != 1:
        return None  # statement boundaries moved, or both halves were touched
    return "expected" if parities.pop() else "sql"
