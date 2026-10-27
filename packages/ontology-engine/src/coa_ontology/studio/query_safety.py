# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The guards of the designer query console.

SPARQL runs on the Neptune cluster that holds every namespace's graphs, and Neptune's default
graph is the union of all of them. ``prepare_sparql`` therefore refuses any query that names its
own dataset or graphs (``FROM``, ``FROM NAMED``, ``GRAPH``, ``SERVICE``) and injects
``FROM <graph>`` clauses itself: with them, the default graph is exactly those graphs. The injected
query is parsed again and must name exactly them, or it is refused.

openCypher runs on the shared lexical graph, where only node labels carry the namespace (its
tenant id). ``prepare_cypher`` refuses write clauses, procedure calls and ``UNION``, and every node
pattern must carry only the namespace's labels or reuse a variable bound by such a pattern since
the last ``WITH``. It fails closed: a parenthesis it cannot read as a node pattern or a plain
expression is refused, and so are backtick names other than labels and any non-ASCII identifier.

Both guards work on a masked copy of the query, in which strings, comments and (for SPARQL) IRIs
are blanked, so a keyword or a brace inside one of them changes nothing. A SPARQL ``<`` right after
an operand inside an expression is less-than, not the start of an IRI, so a comparison written
without spaces (``?y<2000&&?y>1900``) is read as one. Both cap the rows at the top level and set
Neptune's own timeout; a query may not set its own query hints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import draft as store

MAX_QUERY_CHARS = 20_000
# The rewrite of isBlank, isIRI and isURI copies each call's argument, so calls nested in one another
# multiply a query's length; the rewritten query is parsed again, which takes about a second at this length.
MAX_REWRITTEN_CHARS = 2 * MAX_QUERY_CHARS
SPARQL_FORMS = ("SELECT", "ASK", "CONSTRUCT", "DESCRIBE")
VKG_FORMS = ("SELECT", "ASK")
# The prefix the timeout hint is written with; a query that declares it itself is refused.
HINT_PREFIX = "coastudiohint"
HINT_IRI = "http://aws.amazon.com/neptune/vocab/v01/QueryHints#"
LEXICAL_LABEL_BASES = ("Entity", "Fact", "Statement", "Chunk", "Source", "Topic", "SYS_Class")
CYPHER_FORBIDDEN = (
    "CREATE",
    "MERGE",
    "SET",
    "DELETE",
    "DETACH",
    "REMOVE",
    "CALL",
    "LOAD",
    "FOREACH",
    "DROP",
    "UNION",
    "USING",
)


class QueryRefused(store.StudioError):
    """A query the console does not run; the message says why."""

    status = 422
    code = "QueryRefused"


# ── Masking ─────────────────────────────────────────────────────────────────

_IRIREF = re.compile(r'<[^<>"{}|^`\\\x00-\x20]*>')


def _blank(chars: list[str], start: int, end: int) -> None:
    for i in range(start, end):
        if chars[i] != "\n":
            chars[i] = " "


def _short_string_end(text: str, i: int, quote: str, *, multiline: bool = False) -> int:
    """The index after the string literal that starts at ``i``: one line, or any number with ``multiline``.

    An unterminated multi-line string (an openCypher one) is refused: masking the rest of the
    query as a string would hide whatever follows it.
    """
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == quote or (text[j] == "\n" and not multiline):
            return j + 1
        j += 1
    if multiline:
        raise QueryRefused("The query has an unterminated string")
    return len(text)


def _sparql_string_end(text: str, i: int, quote: str) -> int:
    """The index after the SPARQL string literal at ``i``, short or long (triple-quoted)."""
    long_quote = quote * 3
    if not text.startswith(long_quote, i):
        return _short_string_end(text, i, quote)
    j = i + 3
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text.startswith(long_quote, j):
            return j + 3
        j += 1
    return len(text)


# The tokens the SPARQL scan tells apart: one that ends an operand (a variable, a number, a literal, ``)``...),
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


def _scan_sparql(text: str) -> tuple[str, list[str], list[int]]:
    """``text`` masked, its IRIs, and where the query sent needs a space after a ``<`` that compares.

    A ``<`` starts an IRI unless it follows an operand inside an expression: there the grammar has no
    IRI, so it is less-than (or ``<=``), as in ``FILTER(?y<2000&&?y>1900)``. SPARQL 1.1 (section 19.3)
    reads ``<2000&&?y>`` there as an IRI all the same, by longest match, and so does a parser that
    follows it (RDF4J refuses the query), which also reads ``<<`` in ``?a<<http://e/x>`` as RDF-star's.
    The positions after each such ``<`` or ``<=`` are returned so that a space can make every parser
    read the comparison that rdflib, which checks the query, reads. A prefixed name that ends in ``-``,
    ``.``, ``:`` or ``%`` is not taken for an operand, and neither is a keyword, so a ``<`` after one
    starts an IRI wherever a valid query could have one. Serve's ``comparison_spacing`` copies this scan
    for the console's VKG target, whose parser is RDF4J's; the tests check that both space alike.
    """
    chars = list(text)
    iris: list[str] = []
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
            _blank(chars, i, end)
        elif c in "\"'":
            end = _sparql_string_end(text, i, c)
            _blank(chars, i, end)
            token = (_OPERAND, "")
        elif c == "<":
            m = _IRIREF.match(text, i)
            if brackets[-1].expression and last[0] in (_OPERAND, _NAME):
                end = i + 2 if text.startswith("<=", i) else i + 1
                if m or text.startswith("<<", i):  # an IRI, or RDF-star's <<, by longest match
                    spaced.append(end)
                token = (_SYMBOL, "<")
            elif m:
                iris.append(m.group(0)[1:-1])
                end = m.end()
                _blank(chars, i, end)
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
    return "".join(chars), iris, spaced


def mask_sparql(text: str, iris: list[str] | None = None) -> str:
    """``text`` with strings, comments and IRIs blanked (same length, newlines kept); ``iris`` collects the IRIs."""
    masked, found, _ = _scan_sparql(text)
    if iris is not None:
        iris.extend(found)
    return masked


def _spaced_comparisons(query: str) -> str:
    """``query`` with a space after each ``<`` or ``<=`` that compares where SPARQL's lexer reads an IRI or ``<<``."""
    _, _, spaced = _scan_sparql(query)
    for at in reversed(spaced):
        query = query[:at] + " " + query[at:]
    return query


def mask_cypher(text: str, *, backticks: bool) -> str:
    """``text`` with strings and comments blanked, and backtick names too when ``backticks``."""
    chars = list(text)
    i = 0
    while i < len(text):
        c = text[i]
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = len(text) if end == -1 else end
            _blank(chars, i, end)
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise QueryRefused("The query has an unterminated comment")
            _blank(chars, i, end + 2)
            i = end + 2
        elif c in "\"'":
            end = _short_string_end(text, i, c, multiline=True)
            _blank(chars, i, end)
            i = end
        elif c == "`":
            end = text.find("`", i + 1)
            if end == -1:
                raise QueryRefused("The query has an unterminated backtick name")
            if backticks:
                _blank(chars, i, end + 1)
            i = end + 1
        else:
            i += 1
    return "".join(chars)


def _word_before(masked: str, pos: int) -> str:
    """The word that ends right before ``pos``, whitespace skipped ("" when a symbol comes first)."""
    j = pos
    while j > 0 and masked[j - 1].isspace():
        j -= 1
    k = j
    while k > 0 and (masked[k - 1].isalnum() or masked[k - 1] == "_"):
        k -= 1
    return masked[k:j]


def _word_start_before(masked: str, pos: int) -> int:
    j = pos
    while j > 0 and masked[j - 1].isspace():
        j -= 1
    while j > 0 and (masked[j - 1].isalnum() or masked[j - 1] == "_"):
        j -= 1
    return j


def _check_length(query: str) -> None:
    if not query.strip():
        raise QueryRefused("The query is empty")
    if len(query) > MAX_QUERY_CHARS:
        raise QueryRefused(f"A query is at most {MAX_QUERY_CHARS} characters")


def _check_escapes(query: str) -> None:
    r"""Refuse ``\u`` escapes: rdflib expands them before it parses, and what reads the raw text may not."""
    if re.search(r"\\[uU]", query):
        raise QueryRefused("Unicode escapes (\\u, \\U) are not allowed; write the characters themselves")


_HINT_NAME = re.compile(r"(?<![\w.\-])hint\s*:", re.IGNORECASE)


def _check_sparql_text(query: str) -> None:
    r"""Refuse what would make the text checked here differ from the text Neptune runs, and query hints.

    rdflib expands ``\u`` escapes before it parses, while the masking here reads the raw text; a
    query with one could hide ``GRAPH`` from one of them. Hints would let a query lift its timeout.
    """
    _check_length(query)
    _check_escapes(query)
    iris: list[str] = []
    masked = mask_sparql(query, iris)
    # Every IRI is written whole, so the hint namespace cannot be assembled from BASE, a relative IRI
    # or an escaped local name (``h:vocab\/v01``) and slip past the text check below.
    if re.search(r"\bBASE\b", masked, re.IGNORECASE):
        raise QueryRefused("BASE is not allowed; write full IRIs")
    if any(not re.match(r"[A-Za-z][A-Za-z0-9+.\-]*:", iri) for iri in iris):
        raise QueryRefused("Relative IRIs are not allowed; write full IRIs")
    if "\\" in masked:
        raise QueryRefused("Escaped characters in prefixed names are not allowed")
    if "neptune/vocab" in query.lower() or _HINT_NAME.search(masked):
        raise QueryRefused("Neptune query hints are not allowed: the console sets the timeout")


# ── SPARQL ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PreparedSparql:
    """A query ready to send: its text and its form (``SELECT``, ``ASK``, ``CONSTRUCT``, ``DESCRIBE``)."""

    text: str
    form: str


_FORM_NAMES = {"SelectQuery": "SELECT", "AskQuery": "ASK", "ConstructQuery": "CONSTRUCT", "DescribeQuery": "DESCRIBE"}


def _parse(query: str) -> Any:
    from rdflib.plugins.sparql.parser import parseQuery

    try:
        return parseQuery(query)
    except Exception as e:  # noqa: BLE001 - pyparsing raises several kinds; all mean "not a query we run"
        first = str(e).splitlines()[0] if str(e) else type(e).__name__
        raise QueryRefused(
            f"The console runs SPARQL queries (SELECT, ASK, CONSTRUCT, DESCRIBE); this one does not parse: {first}"
        ) from e


def _names(node: Any, out: set[str]) -> set[str]:
    """Every rule name in an rdflib parse tree."""
    from rdflib.plugins.sparql.parserutils import CompValue

    if isinstance(node, CompValue):
        out.add(node.name)
        for value in node.values():
            _names(value, out)
    elif isinstance(node, (list, tuple)) or type(node).__name__ == "ParseResults":
        for value in node:
            _names(value, out)
    return out


def _check_parsed(parsed: Any, forms: tuple[str, ...]) -> tuple[str, Any]:
    query = parsed[1]
    form = _FORM_NAMES.get(getattr(query, "name", ""), "")
    if form not in forms:
        raise QueryRefused(f"Only {', '.join(forms)} queries can run here")
    if "datasetClause" in query:
        raise QueryRefused("FROM and FROM NAMED are not allowed: the console chooses the graphs a query reads")
    names = _names(parsed, set())
    if "GraphGraphPattern" in names:
        raise QueryRefused("GRAPH is not allowed: the console chooses the graphs a query reads")
    if "ServiceGraphPattern" in names:
        raise QueryRefused("SERVICE is not allowed")
    return form, query


def _matching_brace(masked: str, start: int) -> int:
    """The index of the ``}`` that closes the ``{`` at ``start``."""
    depth = 0
    for i in range(start, len(masked)):
        if masked[i] == "{":
            depth += 1
        elif masked[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise QueryRefused("The query has an unclosed {")  # pragma: no cover - the parse found it closed


def _first_group(masked: str, start: int) -> int | None:
    """The first ``{`` at parenthesis depth 0 from ``start``, or None."""
    depth = 0
    for i in range(start, len(masked)):
        c = masked[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "{" and depth == 0:
            return i
    return None


@dataclass(frozen=True)
class _Points:
    """Where a query gets its dataset clause and its timeout hint."""

    dataset_at: int
    hint_at: int | None  # right after the WHERE group's ``{``; None for a DESCRIBE without one
    # The WHERE group's ``}`` when the group is a sub-select: nothing may precede one in its group,
    # so the sub-select is put in a group of its own after the hint.
    sub_select_end: int | None = None


def _injection_points(masked: str, form: str) -> _Points:
    keyword = re.search(rf"\b{form}\b", masked, re.IGNORECASE)
    if keyword is None:  # pragma: no cover - the parse found the form
        raise QueryRefused(f"No {form} keyword")
    pos = keyword.end()
    short_construct = False
    if form == "CONSTRUCT":
        short_construct = re.match(r"\s*WHERE\b", masked[pos:], re.IGNORECASE) is not None
        if not short_construct:
            template = _first_group(masked, pos)
            if template is None:  # pragma: no cover - the parse found a template
                raise QueryRefused("The CONSTRUCT template is missing")
            pos = _matching_brace(masked, template) + 1
    brace = _first_group(masked, pos)
    if short_construct:
        # Its group is also the template, so a timeout hint there would show up in the result.
        raise QueryRefused("Write CONSTRUCT with a template: CONSTRUCT { ... } WHERE { ... }")
    if brace is None:
        if form != "DESCRIBE":  # pragma: no cover - only DESCRIBE may lack a WHERE clause
            raise QueryRefused("The query has no WHERE clause")
        modifier = re.search(r"\b(ORDER|LIMIT|OFFSET|VALUES|GROUP|HAVING)\b", masked[pos:], re.IGNORECASE)
        return _Points(pos + modifier.start() if modifier else len(masked), None)
    dataset_at = _word_start_before(masked, brace) if _word_before(masked, brace).upper() == "WHERE" else brace
    sub_select = re.match(r"\s*SELECT\b", masked[brace + 1 :], re.IGNORECASE) is not None
    return _Points(dataset_at, brace + 1, _matching_brace(masked, brace) if sub_select else None)


def _iri_text(iri: str) -> str:
    from coa_ontology.stores.neptune_db_graph import _iri

    return _iri(iri)


def _string_text(value: str) -> str:
    from coa_ontology.stores.neptune_db_graph import _esc

    return f'"{_esc(value)}"'


# A call of isBlank, isIRI or isURI. Its name follows a space, an opening or closing bracket (a second
# HAVING condition may follow the first with no space), a comma or an operator; after anything else it is
# part of a prefixed name (ex:isBlank), a variable (?isIRI) or a label (_:isURI).
_BLANK_NODE_TEST = re.compile(r"(?<![^\s()!&|,=<>])(isBlank|isIRI|isURI)\s*\(", re.IGNORECASE)


def _blank_node_tests(text: str, masked: str, prefix: str) -> str:
    """``text`` with each isBlank, isIRI and isURI call answering for the IRIs under ``prefix`` as for blank nodes.

    ``prefix`` comes as a SPARQL string literal. The calls in an argument are rewritten in each copy of it.
    """
    out: list[str] = []
    at = 0
    while m := _BLANK_NODE_TEST.search(masked, at):
        paren = m.end() - 1
        close = _matching_paren(masked, paren)
        arg = _blank_node_tests(text[paren + 1 : close], masked[paren + 1 : close], prefix)
        if m.group(1).upper() == "ISBLANK":
            test = f"(isBlank({arg}) || (isIRI({arg}) && STRSTARTS(STR({arg}), {prefix})))"
        else:
            test = f"(isIRI({arg}) && !STRSTARTS(STR({arg}), {prefix}))"
        out += [text[at : m.start()], test]
        at = close + 1
    out.append(text[at:])
    rewritten = "".join(out)
    if len(rewritten) > MAX_REWRITTEN_CHARS:  # checked at each level of nesting, before the next one copies it
        raise QueryRefused("The query has too many isBlank, isIRI or isURI calls, or nests them too deeply")
    return rewritten


def _top_level_values(masked: str) -> int:
    """The start of the query's trailing ``VALUES`` (the last one outside every brace)."""
    depth, found = 0, -1
    for m in re.finditer(r"[{}]|(?<![?$\w:])VALUES\b", masked, re.IGNORECASE):
        token = m.group(0)
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        elif depth == 0:
            found = m.start()
    if found == -1:  # pragma: no cover - the parse found a trailing VALUES
        raise QueryRefused("The trailing VALUES clause could not be found")
    return found


def _top_limit(query: Any) -> int | None:
    # Not query.get(...): rdflib's CompValue.get takes ``variables`` as its second argument.
    clause = query["limitoffset"] if "limitoffset" in query else None  # noqa: SIM401
    return int(clause["limit"]) if clause is not None and "limit" in clause else None


def prepare_sparql(
    query: str, graphs: list[str], *, timeout_ms: int, limit: int, genid: str | None = None
) -> PreparedSparql:
    """``query`` scoped to ``graphs``, with a Neptune timeout hint and a row cap; :class:`QueryRefused` otherwise.

    A ``SELECT``, ``CONSTRUCT`` or ``DESCRIBE`` gets ``LIMIT {limit + 1}`` (before a trailing
    ``VALUES``) unless it has a ``LIMIT`` of its own, which may not be larger; the extra row tells
    a caller that there were more.

    ``genid`` is the prefix of the IRIs that stand for blank nodes in those graphs (a draft's load
    writes them): ``isBlank`` is made true for them, and ``isIRI`` and ``isURI`` false.

    A comparison written without spaces, such as ``?y<2000&&?y>1900``, gets a space after its ``<``
    (see ``_scan_sparql``).
    """
    _check_sparql_text(query)
    form, parsed_query = _check_parsed(_parse(query), SPARQL_FORMS)
    query = _spaced_comparisons(query)
    masked = mask_sparql(query)
    if re.search(rf"\b{HINT_PREFIX}\s*:", masked, re.IGNORECASE):
        raise QueryRefused(f"The prefix {HINT_PREFIX}: is reserved")
    user_limit = _top_limit(parsed_query)
    if user_limit is not None and user_limit > limit + 1:
        raise QueryRefused(f"A query returns at most {limit} rows; use LIMIT {limit} or less")
    if genid:  # before the insertions, whose places are found in the text they go into
        query = _blank_node_tests(query, masked, _string_text(genid))
        masked = mask_sparql(query)
    points = _injection_points(masked, form)
    insertions = [(points.dataset_at, " " + " ".join(f"FROM {_iri_text(g)}" for g in graphs) + " ")]
    if points.hint_at is not None:
        hint = f" {HINT_PREFIX}:Query {HINT_PREFIX}:queryTimeout {int(timeout_ms)} . "
        if points.sub_select_end is None:
            insertions.append((points.hint_at, hint))
        else:  # { SELECT ... } becomes { hint . { SELECT ... } }
            insertions += [(points.hint_at, hint + "{ "), (points.sub_select_end, " } ")]
    if form != "ASK" and user_limit is None:
        at = _top_level_values(masked) if "valuesClause" in parsed_query else len(query)
        insertions.append((at, f"\nLIMIT {limit + 1}\n"))
    text = query
    # From the end, so earlier positions stay valid; at one position the later piece goes in first
    # (a DESCRIBE without WHERE takes its FROM and its LIMIT both at the end).
    for at, _, piece in sorted(((at, n, piece) for n, (at, piece) in enumerate(insertions)), reverse=True):
        text = text[:at] + piece + text[at:]
    if points.hint_at is not None:
        text = f"PREFIX {HINT_PREFIX}: <{HINT_IRI}>\n{text}"
    _verify(text, graphs, form, limit if form != "ASK" else None)
    return PreparedSparql(text, form)


def _verify(text: str, graphs: list[str], form: str, limit: int | None) -> None:
    """The final query must read exactly ``graphs`` (every FROM is ours, there is no other) and be capped."""
    try:
        parsed = _parse(text)
    except QueryRefused as e:  # the user's query parsed: what does not is the console's rewrite of it
        raise QueryRefused("The query could not be scoped to the draft") from e
    query = parsed[1]
    if _FORM_NAMES.get(getattr(query, "name", ""), "") != form:
        raise QueryRefused("The query could not be scoped to the draft")  # pragma: no cover - defensive
    # Not query.get(..., []): rdflib's CompValue.get takes ``variables`` as its second argument.
    clauses = query["datasetClause"] if "datasetClause" in query else []  # noqa: SIM401
    defaults = sorted(str(c["default"]) for c in clauses if "default" in c)
    named = [c for c in clauses if "named" in c]
    names = _names(parsed, set())
    if defaults != sorted(graphs) or named or "GraphGraphPattern" in names or "ServiceGraphPattern" in names:
        raise QueryRefused("The query could not be scoped to the draft")
    top = _top_limit(query)
    if limit is not None and (top is None or top > limit + 1):
        raise QueryRefused("The query could not be capped")  # pragma: no cover - defensive


def check_vkg_sparql(query: Any) -> str:
    r"""The form of a query that may run over the VKG (``SELECT`` or ``ASK``); :class:`QueryRefused` otherwise.

    The rule of serve's copy, ``coa_serve.tier2.ontop.sparql_guard.check_vkg_sparql``: the length, no
    ``\u`` escape, then the form, no dataset clause, no ``GRAPH`` and no ``SERVICE``. A VKG query
    goes to Ontop, not Neptune, so the Neptune checks (``BASE``, relative IRIs, hints) do not apply.
    """
    if not isinstance(query, str):
        raise QueryRefused("The query must be a string")
    _check_length(query)
    _check_escapes(query)
    form, _ = _check_parsed(_parse(query), VKG_FORMS)
    return form


# ── openCypher ──────────────────────────────────────────────────────────────

# A node pattern: an optional variable, labels and an optional property map, nothing else.
_NODE = re.compile(
    r"\s*(?P<var>[A-Za-z_][A-Za-z0-9_]*)?\s*"
    r"(?P<labels>(?::\s*[A-Za-z_][A-Za-z0-9_]*\s*)*)"
    r"(?P<props>\{[^{}]*\})?\s*"
)
_LABEL = re.compile(r":\s*([A-Za-z_][A-Za-z0-9_]*)")
# A backtick name after ``:`` (a label or a relationship type) of plain characters.
_TICKED_NAME = re.compile(r":(\s*)`([A-Za-z0-9_]+)`")
# Reserved words: a ``(`` after one of these opens a pattern or an expression; after any other
# word it is a function call, whose argument is an expression.
_KEYWORDS = frozenset(
    {
        "MATCH", "OPTIONAL", "WHERE", "WITH", "RETURN", "UNWIND", "UNION", "ALL", "AND", "OR", "XOR", "NOT",
        "IN", "IS", "NULL", "CASE", "WHEN", "THEN", "ELSE", "END", "AS", "DISTINCT", "ORDER", "BY", "SKIP",
        "LIMIT", "ASC", "DESC", "ASCENDING", "DESCENDING", "EXISTS", "STARTS", "ENDS", "CONTAINS", "TRUE",
        "FALSE", "ON", "USING", "YIELD",
    }
)  # fmt: skip
# Inside an expression group, what only a node pattern could mean: a label, an inline WHERE, ``IS`` a label.
_PATTERN_ONLY = re.compile(r":|\bWHERE\b|\bIS\b(?!\s+(?:NOT\s+)?NULL\b)", re.IGNORECASE)


def tenant_labels(tenant: str) -> frozenset[str]:
    """The node labels of one namespace in the lexical graph (``__Entity__{tenant}__`` and the others)."""
    return frozenset(f"__{base}__{tenant}__" for base in LEXICAL_LABEL_BASES)


def _matching_paren(text: str, start: int) -> int:
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise QueryRefused("The query has an unclosed (")


def _cypher_text(query: str) -> str:
    """``query`` with strings and comments blanked and backtick names after ``:`` unquoted; refused if odd."""
    text = _TICKED_NAME.sub(r":\1\2", mask_cypher(query, backticks=False))
    if "`" in text:
        raise QueryRefused("Backtick names are only allowed for labels and relationship types")
    if any(ord(c) > 127 for c in text):
        raise QueryRefused("Names must be plain ASCII letters, digits and underscores")
    if "$" in text:
        raise QueryRefused("Parameters are not supported in the console; write the values in the query")
    return text


_CLAUSE = re.compile(r"\b(MATCH|WHERE|WITH|RETURN|UNWIND|ORDER|SKIP|LIMIT)\b", re.IGNORECASE)


def _depths(text: str) -> list[int]:
    """How many ``(``, ``[`` and ``{`` are open before each character."""
    out, depth = [], 0
    for c in text:
        out.append(depth)
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
    return out


def _check_nodes(text: str, allowed: frozenset[str], tenant: str) -> None:
    """Every node pattern carries only ``allowed`` labels, or reuses a variable bound since the last ``WITH``.

    Only a pattern written directly in a ``MATCH`` clause binds a variable for the rest of the query.
    One inside a pattern comprehension, an ``EXISTS { }`` subquery or any other expression binds it
    in that local scope only, so a later ``(x)`` would be a new, unlabeled node.
    """
    resets = [m.start() for m in re.finditer(r"\bWITH\b", text, re.IGNORECASE)]
    depth = _depths(text)
    clauses = [(m.start(), m.group(1).upper()) for m in _CLAUSE.finditer(text) if depth[m.start()] == 0]
    bound: set[str] = set()
    for open_at in (i for i, c in enumerate(text) if c == "("):
        while resets and resets[0] < open_at:
            resets.pop(0)
            bound = set()  # a WITH starts a new scope; what it passes on must be matched again
        word = _word_before(text, open_at)
        if word and word.upper() not in _KEYWORDS:
            continue  # a function call such as count(n): its argument is an expression
        content = text[open_at + 1 : _matching_paren(text, open_at)]
        node = _NODE.fullmatch(content)
        if node is None:
            if _PATTERN_ONLY.search(content):
                raise QueryRefused("Write each node pattern as (variable:Label {property: value})")
            continue  # an expression in parentheses; the patterns inside it are checked on their own
        labels = _LABEL.findall(node.group("labels") or "")
        var = node.group("var")
        if labels:
            foreign = [lbl for lbl in labels if lbl not in allowed]
            if foreign:
                raise QueryRefused(
                    f"The label {foreign[0]} is not one of this namespace's labels; use one of "
                    f"{', '.join(sorted(allowed))}"
                )
            clause = next((kw for at, kw in reversed(clauses) if at < open_at), None)
            if var and depth[open_at] == 0 and clause == "MATCH":
                bound.add(var)
        elif not var or var not in bound:
            raise QueryRefused(
                "Every node pattern needs one of this namespace's labels, such as "
                f"`__Entity__{tenant}__`, or a variable bound by such a pattern since the last WITH"
            )


def prepare_cypher(query: str, tenant: str, *, limit: int, timeout_ms: int) -> str:
    """``query`` if it only reads the namespace's nodes, with Neptune's timeout and at most ``limit + 1`` rows."""
    _check_length(query)
    text = _cypher_text(query)
    forbidden = re.search(rf"\b({'|'.join(CYPHER_FORBIDDEN)})\b", text, re.IGNORECASE)
    if forbidden:
        raise QueryRefused(f"{forbidden.group(1).upper()} is not allowed: the console only reads the lexical graph")
    _check_nodes(text, tenant_labels(tenant), tenant)
    returns = list(re.finditer(r"\bRETURN\b", text, re.IGNORECASE))
    tail = text[returns[-1].end() :] if returns else text
    own = re.search(r"\bLIMIT\s+(\d+)\s*;?\s*$", tail, re.IGNORECASE)
    if own is None and re.search(r"\bLIMIT\b", tail, re.IGNORECASE):
        raise QueryRefused("The final LIMIT must be a number")
    if own is not None and int(own.group(1)) > limit + 1:
        raise QueryRefused(f"A query returns at most {limit} rows; use LIMIT {limit} or less")
    out = query.rstrip().rstrip(";").rstrip()
    if own is None:
        out = f"{out}\nLIMIT {limit + 1}"
    return f"USING QUERY:TIMEOUTMILLISECONDS {int(timeout_ms)}\n{out}"
