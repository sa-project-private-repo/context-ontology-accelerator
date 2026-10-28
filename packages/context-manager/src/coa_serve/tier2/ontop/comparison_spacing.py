# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A space after each ``<`` of a SPARQL comparison that the VKG's parser would read as an IRI.

rdflib, which ``sparql_guard`` parses the query console's SPARQL with, reads ``FILTER(?y<2000&&?y>1900)``
as two comparisons. SPARQL 1.1 (section 19.3) tokenizes by longest match, and RDF4J, the parser of the
VKG's Ontop, follows it: it reads ``<2000&&?y>`` as an IRI and refuses the query, and it reads ``<<`` in
``?a<<http://e/x>`` as RDF-star's. :func:`space_comparisons` puts a space after each such ``<`` or ``<=``,
so the VKG reads the comparisons the guard read.

A copy of the scan of ``coa_ontology.studio.query_safety`` (``_scan_sparql``), which serve cannot import,
reduced to the comparisons it spaces; the engine's tests check that both space every query alike.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_IRIREF = re.compile(r'<[^<>"{}|^`\\\x00-\x20]*>')
# The tokens the scan tells apart: one that ends an operand (a variable, a number, a literal, ``)``...),
# a prefixed name or an IRI (an operand, or the name of a function), a keyword, and any other symbol.
_OPERAND, _NAME, _KEYWORD, _SYMBOL = "operand", "name", "keyword", "symbol"
_VARIABLE = re.compile(r"[?$][\w\u00B7\u0300-\u036F\u203F\u2040]+")
_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_LANGTAG = re.compile(r"@[A-Za-z]+(?:-[A-Za-z0-9]+)*")
_WORD = re.compile(r"(?:[^\W\d]|:)[\w.\-:%\u00B7\u0300-\u036F\u203F\u2040]*")
# The keywords after which a ``(`` opens an expression until the clause ends.
_CLAUSES = {"SELECT": "SELECT", "GROUP": "MODIFIER", "ORDER": "MODIFIER", "HAVING": "MODIFIER"}
_CLAUSE_ENDS = frozenset({"WHERE", "FROM", "VALUES", "LIMIT", "OFFSET", "ASK", "CONSTRUCT", "DESCRIBE"})


@dataclass
class _Bracket:
    """An open ``(`` or ``{`` of a SPARQL query, or the query itself.

    ``expression`` is true for a ``(`` around an expression (a ``FILTER``, a ``BIND``, a projection,
    a solution modifier, a function's arguments), false for a collection, a path, a ``VALUES`` row
    and every ``{``. ``clause`` is the clause in progress at this level: ``SELECT``, ``MODIFIER``
    (``GROUP BY``, ``ORDER BY``, ``HAVING``) or none.
    """

    expression: bool
    clause: str = ""


def _string_end(text: str, i: int, quote: str) -> int:
    """The index after the SPARQL string literal at ``i``, short (one line) or long (triple-quoted)."""
    long_quote = quote * 3
    if text.startswith(long_quote, i):
        j = i + 3
        while j < len(text):
            if text[j] == "\\":
                j += 2
                continue
            if text.startswith(long_quote, j):
                return j + 3
            j += 1
        return len(text)
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] in (quote, "\n"):
            return j + 1
        j += 1
    return len(text)


def _comparisons(text: str) -> list[int]:
    """Where a space must follow a ``<`` or ``<=`` that compares, so that a longest-match lexer reads it so.

    A ``<`` starts an IRI unless it follows an operand inside an expression: there the grammar has no
    IRI, so it is less-than (or ``<=``). Such a ``<`` needs the space when an IRI, by longest match, or
    ``<<`` starts at it. A prefixed name that ends in ``-``, ``.``, ``:`` or ``%`` is not taken for an
    operand, and neither is a keyword. Strings and comments are skipped.
    """
    spaced: list[int] = []
    brackets = [_Bracket(expression=False)]
    last: tuple[str, str] = (_SYMBOL, "")
    before: tuple[str, str] = (_SYMBOL, "")
    i = 0
    while i < len(text):
        c = text[i]
        token: tuple[str, str] | None = None
        end = i + 1
        if c.isspace():
            pass
        elif c == "#":
            end = text.find("\n", i)
            end = len(text) if end == -1 else end
        elif c in "\"'":
            end = _string_end(text, i, c)
            token = (_OPERAND, "")
        elif c == "<":
            m = _IRIREF.match(text, i)
            if brackets[-1].expression and last[0] in (_OPERAND, _NAME):
                end = i + 2 if text.startswith("<=", i) else i + 1
                if m or text.startswith("<<", i):  # an IRI, or RDF-star's <<, by longest match
                    spaced.append(end)
                token = (_SYMBOL, "<")
            elif m:
                end = m.end()
                token = (_NAME, "")
            else:
                token = (_SYMBOL, "<")
        elif c == "(":
            top = brackets[-1]
            opens = (
                top.expression
                or bool(top.clause)
                or last in ((_KEYWORD, "FILTER"), (_KEYWORD, "BIND"))
                or (before == (_KEYWORD, "FILTER") and last[0] in (_KEYWORD, _NAME))  # FILTER regex(...)
            )
            brackets.append(_Bracket(expression=opens))
            token = (_SYMBOL, "(")
        elif c in "){}":
            if c == "{":
                brackets[-1].clause = ""
                brackets.append(_Bracket(expression=False))
                token = (_SYMBOL, "{")
            else:
                if len(brackets) > 1:
                    brackets.pop()
                token = (_OPERAND, c)
        elif (m := _VARIABLE.match(text, i)) or (m := _NUMBER.match(text, i)) or (m := _LANGTAG.match(text, i)):
            end = m.end()
            token = (_OPERAND, "")
        elif m := _WORD.match(text, i):
            word, end = m.group(0), m.end()
            if ":" in word:
                token = (_SYMBOL if word[-1] in "-.:%" else _NAME, "")
            else:
                token = (_KEYWORD, word.upper())
                if not brackets[-1].expression and token[1] in _CLAUSES:
                    brackets[-1].clause = _CLAUSES[token[1]]
                elif not brackets[-1].expression and token[1] in _CLAUSE_ENDS:
                    brackets[-1].clause = ""
        else:
            token = (_SYMBOL, c)
        if token is not None:
            before, last = last, token
        i = end
    return spaced


def space_comparisons(query: str) -> str:
    """``query`` with a space after each ``<`` or ``<=`` that compares where SPARQL's lexer reads an IRI or ``<<``."""
    for at in reversed(_comparisons(query)):
        query = query[:at] + " " + query[at:]
    return query
