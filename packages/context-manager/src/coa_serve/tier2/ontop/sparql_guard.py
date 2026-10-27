# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The guard of a caller's own SPARQL over the VKG (the Ontology designer query console).

``check_vkg_sparql`` is a copy of ``coa_ontology.studio.query_safety.check_vkg_sparql``, which serve
cannot import: only ``SELECT`` and ``ASK``, and no ``FROM``, ``FROM NAMED``, ``GRAPH`` or ``SERVICE``.
The VKG is one namespace's, so the guard keeps the query to what the VKG translates; the SQL firewall
still checks the caller's grants on the SQL it becomes. ``vkg_terms`` reads the classes and properties
a query names, which the console checks against the namespace's graph before the VKG translates it.
"""

from __future__ import annotations

from typing import Any

MAX_SPARQL_CHARS = 20_000
VKG_FORMS = ("SELECT", "ASK")
_FORM_NAMES = {"SelectQuery": "SELECT", "AskQuery": "ASK", "ConstructQuery": "CONSTRUCT", "DescribeQuery": "DESCRIBE"}


class SparqlRefused(ValueError):
    """A query the console does not run over the VKG; the message says why."""


def _names(node: Any, out: set[str]) -> set[str]:
    from rdflib.plugins.sparql.parserutils import CompValue

    if isinstance(node, CompValue):
        out.add(node.name)
        for value in node.values():
            _names(value, out)
    elif isinstance(node, (list, tuple)) or type(node).__name__ == "ParseResults":
        for value in node:
            _names(value, out)
    return out


def check_vkg_sparql(query: Any) -> str:
    """The form of a query that may run over the VKG (``SELECT`` or ``ASK``); :class:`SparqlRefused` otherwise."""
    from rdflib.plugins.sparql.parser import parseQuery

    if not isinstance(query, str) or not query.strip():
        raise SparqlRefused("The query is empty")
    if len(query) > MAX_SPARQL_CHARS:
        raise SparqlRefused(f"A query is at most {MAX_SPARQL_CHARS} characters")
    if "\\u" in query or "\\U" in query:
        # rdflib expands them before it parses; the VKG might not, and then the two read different queries.
        raise SparqlRefused("Unicode escapes (\\u, \\U) are not allowed; write the characters themselves")
    try:
        parsed = parseQuery(query)
    except Exception as e:  # noqa: BLE001 - pyparsing raises several kinds; all mean "not a query we run"
        first = str(e).splitlines()[0] if str(e) else type(e).__name__
        raise SparqlRefused(f"The query does not parse: {first}") from e
    body = parsed[1]
    form = _FORM_NAMES.get(getattr(body, "name", ""), "")
    if form not in VKG_FORMS:
        raise SparqlRefused(f"Only {', '.join(VKG_FORMS)} queries can run over the VKG")
    if "datasetClause" in body:
        raise SparqlRefused("FROM and FROM NAMED are not allowed")
    names = _names(parsed, set())
    if "GraphGraphPattern" in names:
        raise SparqlRefused("GRAPH is not allowed")
    if "ServiceGraphPattern" in names:
        raise SparqlRefused("SERVICE is not allowed")
    return form


def _path_needs(path: Any) -> frozenset[str]:
    """The IRIs every match of a predicate goes through, along property paths.

    An alternative needs what all its alternatives need; ``*`` and ``?`` also match with no step, and a
    negated path names what it excludes, so neither needs anything.
    """
    from rdflib import URIRef
    from rdflib.paths import AlternativePath, InvPath, MulPath, SequencePath

    if isinstance(path, URIRef):
        return frozenset([str(path)])
    if isinstance(path, SequencePath):
        return frozenset().union(*(_path_needs(arg) for arg in path.args))
    if isinstance(path, AlternativePath):
        return frozenset.intersection(*(_path_needs(arg) for arg in path.args))
    if isinstance(path, InvPath):
        return _path_needs(path.arg)
    if isinstance(path, MulPath) and not path.zero:
        return _path_needs(path.path)
    return frozenset()


def _needs(node: Any) -> frozenset[tuple[bool, str]]:
    """``(is_class, iri)`` for the classes and properties every answer of an algebra node needs.

    A basic graph pattern needs the classes it types and the predicates it uses, and a join what both
    sides need. ``OPTIONAL`` and ``MINUS`` need what their left side needs, a ``UNION`` what every branch
    needs. The expression of a ``FILTER``, a ``BIND`` or a projection (``EXISTS`` and ``NOT EXISTS``
    included) and a subquery are left to the VKG: a subquery that aggregates without ``GROUP BY`` has a
    row whatever it matches. Every other node needs what its pattern needs.
    """
    from rdflib import RDF, URIRef
    from rdflib.plugins.sparql.parserutils import CompValue

    if not isinstance(node, CompValue):
        return frozenset()
    if node.name == "BGP":
        needed: set[tuple[bool, str]] = set()
        for _, p, o in node.triples:
            needed.update((False, iri) for iri in _path_needs(p))
            if p == RDF.type and isinstance(o, URIRef):
                needed.add((True, str(o)))
        return frozenset(needed)
    if node.name == "Join":
        return _needs(node.p1) | _needs(node.p2)
    if node.name == "Union":
        return _needs(node.p1) & _needs(node.p2)
    if node.name in ("LeftJoin", "Minus"):
        return _needs(node.p1)
    if node.name == "ToMultiSet":  # a subquery, or VALUES
        return frozenset()
    return _needs(node.get("p"))


def vkg_terms(query: str) -> tuple[list[str], list[str]]:
    """The classes and the properties every answer of ``query`` needs, each sorted (:func:`_needs`).

    The classes are the objects of ``rdf:type``, the properties the predicates. Read from the query's
    algebra, so prefixed names are resolved and property paths followed. An IRI anywhere else (a
    subject, an object, ``VALUES``, a ``FILTER``) is a value, such as a row the mapping builds
    (``<.../customers/1>``), not a term. A term no answer needs is not checked: the query can still
    have answers without it, so the VKG decides. Standard vocabularies are kept; the caller leaves
    them out. Nothing when the query does not translate to an algebra.
    """
    from rdflib.plugins.sparql.algebra import translateQuery
    from rdflib.plugins.sparql.parser import parseQuery

    try:
        algebra = translateQuery(parseQuery(query)).algebra
    except Exception:  # noqa: BLE001 - such a query goes to the VKG unchecked, as before
        return [], []
    needed = _needs(algebra)
    return sorted(iri for is_class, iri in needed if is_class), sorted(iri for is_class, iri in needed if not is_class)
