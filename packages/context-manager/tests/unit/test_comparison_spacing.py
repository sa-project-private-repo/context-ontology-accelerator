# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A comparison written without spaces on the query console's VKG target (``comparison_spacing``)."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coa_serve.clients.base import QueryResult
from coa_serve.role_resolver import ResolvedProfile
from coa_serve.tier2.ontop.comparison_spacing import space_comparisons
from coa_serve.tier2.ontop.sparql_guard import check_vkg_sparql
from coa_serve.tier2.ontop.vkg_translator import Tier2Result
from coa_serve.tier2.sql_firewall import FirewallResult
from rdflib import Graph, URIRef
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue

pytestmark = pytest.mark.unit
NS = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"

YEARS = """@prefix ex: <http://e/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
ex:a a ex:Person ; ex:year 1950 ; ex:name "Ann" ; ex:on "2019-06-01"^^xsd:date .
ex:b a ex:Person ; ex:year 2010 ; ex:name "Bartholomew" ; ex:on "2021-01-01"^^xsd:date .
ex:c a ex:Person ; ex:year 1850 ; ex:name "Cy" .
"""
# The engine's cases (test_studio_query_safety.py), as the console's VKG target takes them: SELECT and ASK.
COMPACT = [
    "SELECT ?s WHERE { ?s <http://e/year> ?y FILTER(?y<2000&&?y>1900) }",
    "SELECT ?s WHERE { ?s <http://e/year> ?y FILTER(?y<=2000&&?y>=1900) }",
    # The example SPARQL 1.1 (section 19.3) gives of an IRI, inside a FILTER.
    "SELECT ?a ?b WHERE { ?s <http://e/year> ?a . ?t <http://e/year> ?b BIND(?b AS ?c) BIND(?a AS ?d) "
    "FILTER(?a<?b&&?c>?d) }",
    "SELECT ?s WHERE { ?s <http://e/name> ?n FILTER(STRLEN(?n)<5&&STRLEN(?n)>2) }",
    "SELECT ?s WHERE { ?s <http://e/on> ?d FILTER(YEAR(?d)<2020&&YEAR(?d)>2018) }",
    'SELECT ?s ?era WHERE { ?s <http://e/year> ?y BIND(IF(?y<2000&&?y>1900,"old","other") AS ?era) }',
    "SELECT ?s (?y<2000&&?y>1900 AS ?old) WHERE { ?s <http://e/year> ?y }",
    "SELECT ?c WHERE { ?s a ?c } GROUP BY ?c HAVING(COUNT(?s)<10&&COUNT(?s)>1)",
    "SELECT ?s WHERE { ?s <http://e/year> ?y } ORDER BY (?y<2000&&?y>1900)",
    "SELECT ?s WHERE { { SELECT ?s WHERE { ?s <http://e/year> ?y FILTER(?y<2000&&?y>1900) } } }",
    "SELECT ?s WHERE { ?s <http://e/year> ?y FILTER EXISTS { ?s <http://e/year> ?z FILTER(?z<2000&&?z>1900) } }",
    "ASK { ?s <http://e/year> ?y FILTER(?y<2000&&?y>1900) }",
]
# Valid queries with an IRI right after an operand or an operator, without spaces: the IRIs stay as they are.
NEXT_TO_IRIS = [
    "SELECT * WHERE { ?s ?p ?o FILTER(?o<5)?s<http://e/p> ?o }",
    "SELECT * WHERE { ?s ?p ?o FILTER(?o<5)<http://e/s> <http://e/p> ?o }",
    "SELECT ?s (COUNT(DISTINCT<http://e/f>(?o)) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?s<http://e/g>(?s)",
    "SELECT * WHERE { ?s ?p ?o } ORDER BY ?o<http://e/f>(?s) DESC(?s)",
    "SELECT * WHERE { ?s <http://e/p> (?o<http://e/x>) }",
    "SELECT * WHERE { ?s ?p ?o } VALUES (?s ?o) { (<http://e/a> 1) (2<http://e/b>) }",
    "SELECT * WHERE { ?s !(<http://e/a>|^<http://e/b>) ?o . ?o (<http://e/a>/<http://e/b>)* ?s }",
    'SELECT * WHERE { ?s ?p ?o FILTER(?o="1"^^<http://e/t>&&?s!=<http://e/x>) }',
    "SELECT * WHERE { ?s ?p ?o FILTER(<http://e/f>(?o)<<http://e/g>(?s)) }",
    "SELECT * WHERE { ?s ?p ?o FILTER(-<http://e/f>(?o)<1-<http://e/g>(?s)) }",
    "SELECT * WHERE { ?s ?p ?o FILTER(?o<5&&EXISTS{?s<http://e/q> ?x}) }",
    "SELECT * WHERE { [ <http://e/p> (1<http://e/x>) ] ?q ?r }",
    "SELECT ?x WHERE { BIND(<http://e/f>(1)<2 AS ?x) }",
    "PREFIX ex: <http://e/> SELECT * WHERE { ?s ex:p ?o FILTER(ex:f(?o)<ex:g(?s)) }",
]
_IRI_TOKEN = re.compile(r'<[^<>"{}|^`\\\x00-\x20]*>')


def _longest_match_iris(text: str) -> set[str]:
    """The IRIs of ``text`` for a lexer that takes the longest match at every ``<``, as RDF4J does.

    It reads ``FILTER(?y<2000&&?y>1900)`` as ``?y``, the IRI ``<2000&&?y>`` and ``1900``. The queries
    here have no ``<`` in a string.
    """
    found: set[str] = set()
    i = text.find("<")
    while i != -1:
        m = _IRI_TOKEN.match(text, i)
        if m:
            found.add(m.group(0)[1:-1])
        i = text.find("<", m.end() if m else i + 1)
    return found


def _parsed_iris(text: str) -> set[str]:
    """The IRIs rdflib, the guard's parser, reads in ``text``."""
    found: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, URIRef):
            found.add(str(node))
        elif isinstance(node, CompValue):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)) or type(node).__name__ == "ParseResults":
            for value in node:
                walk(value)

    walk(parseQuery(text))
    return found


def _answers(query: str):
    result = Graph().parse(data=YEARS, format="turtle").query(query)
    return result.askAnswer if result.type == "ASK" else sorted(tuple(str(v) for v in row) for row in result)


@pytest.mark.parametrize("query", COMPACT)
def test_a_comparison_without_spaces_reads_alike_in_the_guard_and_the_vkg(query):
    """RDF4J read ``<2000&&?y>`` as an IRI and refused the query that the guard let through."""
    assert not _longest_match_iris(query) <= _parsed_iris(query)  # as written, RDF4J reads another IRI
    spaced = space_comparisons(query)
    assert _longest_match_iris(spaced) <= _parsed_iris(spaced) == _parsed_iris(query)
    assert check_vkg_sparql(spaced) == check_vkg_sparql(query)
    assert _answers(spaced) == _answers(query) not in ([], False)


@pytest.mark.parametrize("query", NEXT_TO_IRIS)
def test_iris_next_to_operands_and_operators_are_left_alone(query):
    spaced = space_comparisons(query)
    assert _parsed_iris(spaced) == _parsed_iris(query)
    assert _longest_match_iris(spaced) <= _parsed_iris(spaced)
    assert "<<" not in spaced  # RDF4J reads ``?a<<http://e/x>`` as RDF-star's <<


@pytest.mark.parametrize(
    ("query", "sent"),
    [
        (COMPACT[0], "SELECT ?s WHERE { ?s <http://e/year> ?y FILTER(?y< 2000&&?y>1900) }"),
        (COMPACT[1], "SELECT ?s WHERE { ?s <http://e/year> ?y FILTER(?y<= 2000&&?y>=1900) }"),
        # Only where a lexer could read an IRI: here no ``>`` follows the ``<``.
        ("SELECT ?s WHERE { ?s ?p ?y FILTER(?y<=2000) }", "SELECT ?s WHERE { ?s ?p ?y FILTER(?y<=2000) }"),
        (
            "SELECT ?s WHERE { ?s ?p ?o FILTER(?o<<http://e/x>) }",
            "SELECT ?s WHERE { ?s ?p ?o FILTER(?o< <http://e/x>) }",
        ),
        # A string or a comment keeps what looks like a comparison in it.
        (
            "SELECT ?s WHERE { ?s ?p ?n FILTER(?n!=\"?y<2&&?y>1\"&&?n!='?y<2&&?y>1') } # FILTER(?y<2&&?y>1)",
            "SELECT ?s WHERE { ?s ?p ?n FILTER(?n!=\"?y<2&&?y>1\"&&?n!='?y<2&&?y>1') } # FILTER(?y<2&&?y>1)",
        ),
        ('SELECT ?s WHERE { ?s ?p """a<b>c""" }', 'SELECT ?s WHERE { ?s ?p """a<b>c""" }'),
    ],
)
def test_the_space_goes_after_the_less_than_and_nowhere_else(query, sent):
    assert space_comparisons(query) == sent


async def _invoke(payload):
    from coa_serve.main import invoke

    return [item async for item in invoke(payload, None)][0]


def _payload(sparql: str) -> dict:
    return {"action": "sparqlQuery", "sparql": sparql, "namespace": NS, "profile": {"tableAllowlist": ["*"]}}


@pytest.fixture
def allowed():
    translator = MagicMock()
    translator.resolve = AsyncMock()
    profile = ResolvedProfile(user_id="alice", email="alice@example.com", table_allowlist=["patients"])
    with (
        patch("coa_serve.main._ensure_initialized", AsyncMock(return_value=None)),
        patch("coa_serve.main._orchestrator", MagicMock()),
        patch("coa_serve.main._sources_registry", None),
        patch("coa_serve.main._vkg_translator", translator),
        patch("coa_serve.main._authorize_namespace_access", AsyncMock(return_value=(profile, None))),
    ):
        yield translator


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * WHERE { ?s ?p ?y FILTER(?y<2000&&EXISTS{SERVICE<http://evil/sparql>{?s ?p ?o}}) }",
        "SELECT * WHERE { ?s ?p ?y FILTER(?y<2000&&EXISTS{GRAPH<http://other>{?s ?p ?o}}) }",
        "SELECT * FROM <http://other> WHERE { ?s ?p ?y FILTER(?y<2000&&?y>1900) }",
        "CONSTRUCT { ?s ?p ?y } WHERE { ?s ?p ?y FILTER(?y<2000&&?y>1900) }",
    ],
)
async def test_the_guard_still_refuses_what_it_refused_next_to_such_a_comparison(allowed, query):
    result = await _invoke(_payload(query))
    assert result["statusCode"] == 400 and result["error"] == "ValidationError", result
    allowed.resolve.assert_not_called()


async def test_the_console_sends_the_vkg_the_comparison_with_its_space(allowed):
    allowed.resolve.return_value = Tier2Result(
        query_result=QueryResult(rows=[{"s": "http://e/a"}], columns=["s"], row_count=1, truncated=False),
        firewall_result=FirewallResult(denied=False, authorized_sql="SELECT ..."),
        executed_sql="SELECT s FROM people",
    )
    result = await _invoke(_payload(COMPACT[0]))
    assert result["statusCode"] == 200 and result["rows"] == [["http://e/a"]], result
    assert allowed.resolve.call_args.args == (space_comparisons(COMPACT[0]),)
