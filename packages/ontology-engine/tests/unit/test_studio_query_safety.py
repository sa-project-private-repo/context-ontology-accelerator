# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The designer query console's SPARQL and openCypher guards."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
from coa_ontology.studio import query_safety as qs
from rdflib import RDF, BNode, Graph, Literal, URIRef
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue

pytestmark = [pytest.mark.unit]
DRAFT = "https://ontology-workbench.local/_studio/ns-1/p-1/r3"
ABOX = "https://ontology-workbench.local/_abox/ns-1"
GRAPHS = [DRAFT, ABOX]
GENID = f"{DRAFT}/genid/"
TENANT = "550e8400e29b41d4a71644665"
ENTITY = f"`__Entity__{TENANT}__`"
CHUNK = f"`__Chunk__{TENANT}__`"


def _prepare(query: str, genid: str | None = None) -> qs.PreparedSparql:
    return qs.prepare_sparql(query, GRAPHS, timeout_ms=20000, limit=1000, genid=genid)


def _dataset(text: str) -> list[str]:
    query = parseQuery(text)[1]
    return sorted(str(c["default"]) for c in query["datasetClause"])


@pytest.mark.parametrize(
    ("query", "form"),
    [
        ("SELECT ?s WHERE { ?s ?p ?o }", "SELECT"),
        ("select ?s { ?s ?p ?o } order by ?s", "SELECT"),
        ("ASK { ?s a ?c }", "ASK"),
        ("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }", "CONSTRUCT"),
        ("DESCRIBE <http://e/x>", "DESCRIBE"),
        ("DESCRIBE ?s WHERE { ?s a ?c }", "DESCRIBE"),
        ("PREFIX ex: <http://e/> SELECT (COUNT(*) AS ?n) WHERE { ?s a ex:C . FILTER(EXISTS { ?s ?p ?o }) }", "SELECT"),
    ],
)
def test_every_form_is_scoped_to_exactly_the_given_graphs(query, form):
    prepared = _prepare(query)
    assert prepared.form == form
    assert _dataset(prepared.text) == sorted(GRAPHS)


def test_the_timeout_hint_goes_first_in_the_where_group_but_never_into_a_construct_template():
    text = _prepare("SELECT ?s WHERE { ?s ?p ?o }").text
    assert text.startswith(f"PREFIX {qs.HINT_PREFIX}: <{qs.HINT_IRI}>")
    assert f"{{ {qs.HINT_PREFIX}:Query {qs.HINT_PREFIX}:queryTimeout 20000 . " in text
    template_first = _prepare("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }").text
    assert template_first.index("CONSTRUCT { ?s ?p ?o }") < template_first.index("queryTimeout")
    assert "queryTimeout" not in _prepare("DESCRIBE <http://e/x>").text
    with pytest.raises(qs.QueryRefused):  # its group is the template: it cannot take the hint
        _prepare("CONSTRUCT WHERE { ?s ?p ?o }")


@pytest.mark.parametrize(
    ("query", "form"),
    [
        (
            "PREFIX owl: <http://www.w3.org/2002/07/owl#>\n"
            "SELECT ?c ?n WHERE { SELECT ?c (COUNT(?i) AS ?n) WHERE { ?i a ?c . ?c a owl:Class } GROUP BY ?c }",
            "SELECT",
        ),
        ("SELECT ?c (COUNT(?i) AS ?n) { # the classes\n SELECT ?c ?i { ?i a ?c } } GROUP BY ?c", "SELECT"),
        ("ASK { SELECT ?s WHERE { ?s ?p ?o } }", "ASK"),
        ("CONSTRUCT { ?s ?p ?o } WHERE { SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 5 }", "CONSTRUCT"),
        ("DESCRIBE ?s WHERE { SELECT ?s WHERE { ?s a ?c } VALUES ?c { <http://e/C> } }", "DESCRIBE"),
    ],
)
def test_a_where_group_that_is_a_sub_select_is_scoped_and_hinted(query, form):
    """The hint went in front of the sub-select, and the re-parse refused a valid query."""
    prepared = _prepare(query)
    assert prepared.form == form and _dataset(prepared.text) == sorted(GRAPHS)
    flat = " ".join(qs.mask_sparql(prepared.text).split())  # without the comment
    assert f"{{ {qs.HINT_PREFIX}:Query {qs.HINT_PREFIX}:queryTimeout 20000 . {{ SELECT" in flat
    assert _top_limit(prepared.text) == (None if form == "ASK" else 1001)


def test_a_rewritten_query_that_does_not_parse_is_not_blamed_on_the_user():
    """The refusal said the user's valid query did not parse, at positions of the rewrite."""
    with pytest.raises(qs.QueryRefused) as raised:
        qs._verify(
            f"SELECT * FROM <{DRAFT}> FROM <{ABOX}> WHERE {{ h:Query h:x 1 . SELECT * {{ }} }}", GRAPHS, "SELECT", 1
        )
    assert raised.value.message == "The query could not be scoped to the draft"


def _top_limit(text: str):
    query = parseQuery(text)[1]
    return int(query["limitoffset"]["limit"]) if "limitoffset" in query and "limit" in query["limitoffset"] else None


def test_a_variable_named_values_is_not_a_values_clause():
    text = _prepare("SELECT ?values WHERE { ?values ?p ?o } VALUES ?values { 1 }").text
    assert _top_limit(text) == 1001


def test_every_result_is_capped_at_the_top_level():
    assert _prepare("SELECT ?s WHERE { ?s ?p ?o }").text.rstrip().endswith("LIMIT 1001")
    assert _prepare("SELECT ?s WHERE { ?s ?p ?o } LIMIT 5").text.count("LIMIT") == 1
    assert "LIMIT" not in _prepare("ASK { ?s ?p ?o }").text
    # A trailing VALUES or an OFFSET of its own does not leave the query uncapped.
    assert _top_limit(_prepare("SELECT ?s WHERE { ?s ?p ?o } VALUES ?s { <http://e/a> }").text) == 1001
    assert _top_limit(_prepare("SELECT ?s WHERE { ?s ?p ?o } OFFSET 0").text) == 1001
    assert _top_limit(_prepare("SELECT ?s WHERE { ?s ?p ?o } OFFSET 5 VALUES ?s { <http://e/a> }").text) == 1001
    assert _top_limit(_prepare("SELECT ?s WHERE { { SELECT ?s { ?s ?p ?o } LIMIT 5 } }").text) == 1001
    with pytest.raises(qs.QueryRefused):
        _prepare("SELECT ?s WHERE { ?s ?p ?o } LIMIT 100000000")


def test_construct_and_describe_are_capped_and_a_limit_may_ask_for_the_extra_row():
    """Only SELECT was shown capped, and only a LIMIT far above the cap refused."""
    for query in (
        "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
        "DESCRIBE ?s WHERE { ?s a ?c }",
        "DESCRIBE <http://e/x>",
    ):
        assert _top_limit(_prepare(query).text) == 1001, query
    for query in ("SELECT ?s WHERE { ?s ?p ?o } LIMIT 1001", "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o } LIMIT 1001"):
        assert _top_limit(_prepare(query).text) == 1001, query
    for query in (
        "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1002",
        "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o } LIMIT 1002",
        "DESCRIBE ?s WHERE { ?s a ?c } LIMIT 1002",
    ):
        with pytest.raises(qs.QueryRefused) as raised:
            _prepare(query)
        assert raised.value.message == "A query returns at most 1000 rows; use LIMIT 1000 or less", query


def test_braces_and_keywords_inside_strings_iris_and_comments_change_nothing():
    query = """PREFIX ex: <http://e/x#>
    # SELECT * FROM <http://other> WHERE { ?a ?b ?c }
    SELECT ?s WHERE { ?s ex:note "a } WHERE { brace", 'GRAPH ?g' ; ex:long \"\"\"FROM <http://x> {\"\"\" }"""
    prepared = _prepare(query)
    assert _dataset(prepared.text) == sorted(GRAPHS)
    assert '"a } WHERE { brace"' in prepared.text


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM <http://other> WHERE { ?s ?p ?o }",
        "SELECT * FROM NAMED <http://other> WHERE { GRAPH ?g { ?s ?p ?o } }",
        "SELECT * WHERE { GRAPH ?g { ?s ?p ?o } }",
        "SELECT * WHERE { ?s ?p ?o . FILTER EXISTS { GRAPH <http://other> { ?s ?p ?o } } }",
        "SELECT * WHERE { { SELECT ?s WHERE { GRAPH ?g { ?s ?p ?o } } } }",
        "SELECT * WHERE { SELECT ?s WHERE { GRAPH ?g { ?s ?p ?o } } }",
        "SELECT * WHERE { SERVICE <http://evil/sparql> { ?s ?p ?o } }",
        "INSERT DATA { <http://e/a> <http://e/b> <http://e/c> }",
        "DROP GRAPH <http://e/g>",
        "this is not sparql",
        "",
    ],
)
def test_queries_that_choose_their_own_graphs_or_write_are_refused(query):
    with pytest.raises(qs.QueryRefused) as raised:
        _prepare(query)
    assert raised.value.status == 422 and raised.value.code == "QueryRefused"


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * WHERE { ?s ?p ?o . FILTER EXISTS { SERVICE <http://evil/sparql> { ?s ?p ?o } } }",
        "SELECT * WHERE { ?s ?p ?o . FILTER NOT EXISTS { SERVICE SILENT <http://evil/sparql> { ?s ?p ?o } } }",
        "SELECT * WHERE { { SELECT ?s WHERE { SERVICE <http://evil/sparql> { ?s ?p ?o } } } }",
        "ASK { SELECT ?s WHERE { SERVICE <http://evil/sparql> { ?s ?p ?o } } }",
    ],
)
def test_service_is_refused_inside_exists_and_subqueries_too(query):
    """Only GRAPH was shown refused when nested."""
    with pytest.raises(qs.QueryRefused) as raised:
        _prepare(query)
    assert raised.value.message == "SERVICE is not allowed"


@pytest.mark.parametrize(
    "query",
    [
        f"PREFIX {qs.HINT_PREFIX}: <http://x/> SELECT * WHERE {{ ?s ?p ?o }}",
        "SELECT * WHERE { ?s ?p ?o }" + " " * qs.MAX_QUERY_CHARS,
        # Query hints: a query may not lift its own timeout.
        f"PREFIX h: <{qs.HINT_IRI}> SELECT * WHERE {{ h:Query h:queryTimeout 600000 . ?s ?p ?o }}",
        f"SELECT * WHERE {{ <{qs.HINT_IRI}Query> <{qs.HINT_IRI}queryTimeout> 600000 . ?s ?p ?o }}",
        "SELECT * WHERE { hint:Query hint:queryTimeout 600000 . ?s ?p ?o }",
        # rdflib expands \u escapes before it parses; the masking reads the raw text.
        'SELECT * WHERE { ?s ?p "x" , "\\u0022 . GRAPH ?g { ?a ?b ?c } . ?s ?p \\u0022" . }',
        'SELECT * WHERE { ?s ?p "\\U00000022" }',
        # The re-review's hint namespaces assembled from BASE, a relative IRI or an escaped local name.
        "BASE <http://aws.amazon.com/neptune/> PREFIX h: <vocab/v01/QueryHints#> "
        "SELECT * WHERE { h:Query h:queryTimeout 600000 . ?s ?p ?o }",
        "BASE <http://aws.amazon.com/neptune/> SELECT * WHERE "
        "{ <vocab/v01/QueryHints#Query> <vocab/v01/QueryHints#queryTimeout> 600000 . ?s ?p ?o }",
        "PREFIX h: <vocab/v01/QueryHints#> SELECT * WHERE { h:Query h:queryTimeout 600000 . ?s ?p ?o }",
        r"PREFIX h: <http://aws.amazon.com/neptune/> SELECT * WHERE "
        r"{ h:vocab\/v01\/QueryHints\#Query h:vocab\/v01\/QueryHints\#queryTimeout 600000 . ?s ?p ?o }",
    ],
)
def test_reserved_prefixes_hints_escapes_and_oversized_queries_are_refused(query):
    with pytest.raises(qs.QueryRefused):
        _prepare(query)


def test_the_final_query_can_only_see_the_scoped_graphs():
    """Whatever the user writes, the text sent to Neptune names exactly our graphs and nothing else."""
    tricky = [
        "SELECT ?s WHERE{?s ?p ?o}",
        "SELECT ?s\nWHERE\n{\n?s ?p ?o\n}",
        "SELECT (STR(?s) AS ?x) { ?s ?p ?o }",
        "BASE <http://e/> SELECT * { <a> ?p ?o }",
        "SELECT * WHERE { ?s ?p ?o OPTIONAL { ?o ?q ?r } MINUS { ?s a ?t } }",
        "DESCRIBE ?s ORDER BY ?s LIMIT 3",
    ]
    for query in tricky:
        try:
            text = _prepare(query).text
        except qs.QueryRefused:
            continue
        parsed = parseQuery(text)[1]
        assert _dataset(text) == sorted(GRAPHS), query
        assert not [c for c in parsed["datasetClause"] if "named" in c], query


# Anonymous classes: a restriction among Order's parents, and a union that Party is equivalent to.
ANONYMOUS = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <http://e/> .
ex:Thing a owl:Class .
ex:Order a owl:Class ; rdfs:subClassOf ex:Thing, [ a owl:Restriction ; owl:onProperty ex:by ; owl:maxCardinality 1 ] .
ex:Party a owl:Class ; owl:equivalentClass [ a owl:Class ; owl:unionOf ( ex:Person ex:Company ) ] .
ex:Person a owl:Class .
ex:Company a owl:Class .
"""
PREFIXES = "PREFIX owl: <http://www.w3.org/2002/07/owl#> PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"


def _rows(graph: Graph, query: str) -> list[tuple[str, ...]]:
    return sorted(tuple(str(term) for term in row) for row in graph.query(query))


def _as_loaded(ontology: Graph) -> Graph:
    """``ontology`` with each blank node an IRI under GENID, as a draft's load writes it.

    It also holds the timeout hint: Neptune reads the hint as a hint, rdflib as a pattern, which it then matches.
    """
    loaded = Graph()
    for triple in ontology:
        loaded.add(tuple(URIRef(f"{GENID}{term}") if isinstance(term, BNode) else term for term in triple))
    loaded.add((URIRef(f"{qs.HINT_IRI}Query"), URIRef(f"{qs.HINT_IRI}queryTimeout"), Literal(20000)))
    return loaded


@pytest.mark.parametrize(
    "query",
    [
        "SELECT ?c ?super WHERE { ?c rdfs:subClassOf ?super FILTER(!isBlank(?super)) }",
        "SELECT ?c WHERE { ?c a owl:Class FILTER(isIRI(?c)) }",
        "SELECT ?c WHERE { ?c a owl:Class FILTER isURI(?c) }",
        "SELECT ?c WHERE { ?c a owl:Class FILTER(IsIri(?c) && !ISBLANK(?c)) }",
        # The isBlank in isIRI's argument is rewritten in each copy of that argument.
        "SELECT ?c WHERE { ?c rdfs:subClassOf ?s FILTER(isIRI(IF(isBlank(?s), ?s, ?c))) }",
        "SELECT ?c WHERE { ?c a owl:Class FILTER(isIRI(?c)) "
        "FILTER NOT EXISTS { ?c rdfs:subClassOf ?r FILTER(isBlank(?r)) } }",
        "SELECT ?c WHERE { { SELECT ?c WHERE { ?c rdfs:subClassOf ?r FILTER(isBlank(?r)) } } }",
        "SELECT ?c WHERE { SELECT ?c WHERE { ?c a owl:Class FILTER(isIRI(?c)) } }",
        "SELECT ?c (isBlank(?s) AS ?anonymous) WHERE { ?c rdfs:subClassOf ?s }",
        "SELECT ?c WHERE { ?c rdfs:subClassOf ?s } GROUP BY ?c HAVING (SUM(IF(isBlank(?s), 1, 0)) > 0)",
        # A call right after a closing bracket, with no space: a second HAVING condition.
        "SELECT ?c WHERE { ?c a owl:Class } GROUP BY ?c HAVING (COUNT(*) > 0)isIRI(?c)",
    ],
)
def test_blank_node_tests_answer_on_the_loaded_draft_as_on_the_ontology(query):
    """The load writes a draft's blank nodes as IRIs, so in a query isBlank was false for
    them and isIRI and isURI true; a restriction was a named parent, a union a named class."""
    ontology = Graph().parse(data=ANONYMOUS, format="turtle")
    prepared = _prepare(PREFIXES + query, GENID).text
    assert _rows(_as_loaded(ontology), prepared) == _rows(ontology, PREFIXES + query)


def test_each_blank_node_test_is_rewritten_where_it_is():
    prefix = f'"{GENID}"'
    text = _prepare("SELECT ?s WHERE { ?s ?p ?o FILTER(!isBlank(?o) && isURI(?s)) }", GENID).text
    assert (
        f"FILTER(!(isBlank(?o) || (isIRI(?o) && STRSTARTS(STR(?o), {prefix}))) "
        f"&& (isIRI(?s) && !STRSTARTS(STR(?s), {prefix})))"
    ) in text


@pytest.mark.parametrize(
    "query",
    [
        # No call: each form, a sub-select and a trailing VALUES.
        "SELECT ?s WHERE { ?s ?p ?o }",
        "ASK { ?s a ?c }",
        "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
        "DESCRIBE <http://e/x>",
        "SELECT ?c (COUNT(?i) AS ?n) { SELECT ?c ?i { ?i a ?c } } GROUP BY ?c VALUES ?c { <http://e/C> }",
        # The names in a string, a comment and an IRI, as an extension function's name and as a variable.
        'SELECT ?s WHERE { ?s ?p "isBlank(?s)", """isIRI(?s)""" }',
        "SELECT ?s WHERE { ?s ?p ?o } # FILTER(isIRI(?s))",
        "SELECT ?s WHERE { ?s <http://e/isURI(x)> ?o }",
        "PREFIX ex: <http://e/> SELECT ?s WHERE { ?s ?p ?o FILTER(ex:isBlank(?o)) }",
        "SELECT * WHERE { ?s ?isBlank (1 2) }",
    ],
)
def test_a_query_without_a_blank_node_test_is_prepared_as_before(query):
    assert _prepare(query, GENID).text == _prepare(query).text


def test_blank_node_tests_nested_too_deeply_are_refused():
    """Each rewrite copies its argument, so the text would triple at each level of nesting."""
    query = "SELECT ?s WHERE { ?s ?p ?o FILTER(" + "isBlank(" * 12 + "?o" + ")" * 12 + ") }"
    with pytest.raises(qs.QueryRefused) as raised:
        _prepare(query, GENID)
    assert raised.value.message == "The query has too many isBlank, isIRI or isURI calls, or nests them too deeply"
    assert _prepare(query).form == "SELECT"


# ── Comparisons written without spaces ──────────────────────────────────────

YEARS = """@prefix ex: <http://e/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
ex:a a ex:Person ; ex:year 1950 ; ex:name "Ann" ; ex:on "2019-06-01"^^xsd:date .
ex:b a ex:Person ; ex:year 2010 ; ex:name "Bartholomew" ; ex:on "2021-01-01"^^xsd:date .
ex:c a ex:Person ; ex:year 1850 ; ex:name "Cy" .
"""
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
_IRI_TOKEN = re.compile(r'<[^<>"{}|^`\\\x00-\x20]*>')


def _longest_match_iris(text: str) -> set[str]:
    """The IRIs of ``text`` for a lexer that takes the longest match at every ``<``, as SPARQL 1.1 says (19.3).

    RDF4J, the VKG's parser, is one: it reads ``FILTER(?y<2000&&?y>1900)`` as ``?y``, the IRI
    ``<2000&&?y>`` and ``1900``, and refuses the query. The queries here have no ``<`` in a string.
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
    """The IRIs rdflib reads in ``text``, as written."""
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


@pytest.mark.parametrize("query", COMPACT)
def test_a_comparison_without_spaces_runs_and_every_parser_reads_it_as_one(query):
    """The masking read ``<2000&&?y>`` as a relative IRI and refused the query."""
    assert not _longest_match_iris(query) <= _parsed_iris(query)  # as written, such a lexer reads another IRI
    prepared = _prepare(query)
    assert _dataset(prepared.text) == sorted(GRAPHS)
    assert _longest_match_iris(prepared.text) <= _parsed_iris(prepared.text)
    data = _as_loaded(Graph().parse(data=YEARS, format="turtle"))
    if prepared.form == "ASK":
        assert data.query(prepared.text).askAnswer is data.query(query).askAnswer is True
    else:
        assert _rows(data, prepared.text) == _rows(data, query) != []


def test_the_masking_keeps_a_comparison_and_still_blanks_the_iris_next_to_it():
    iris: list[str] = []
    query = "SELECT ?s WHERE { ?s<http://e/p>?y FILTER(?y<2000&&?y!=<http://e/x>) }"
    masked = qs.mask_sparql(query, iris)
    assert "FILTER(?y<2000&&?y!=" in masked and "http" not in masked
    assert iris == ["http://e/p", "http://e/x"]
    # The space goes in only where a lexer could read an IRI: here no ``>`` follows the ``<``.
    assert "FILTER(?y<=2000)" in _prepare("SELECT ?s WHERE { ?s ?p ?y FILTER(?y<=2000) }").text
    assert "FILTER(?y< 2000&&?y>1900)" in _prepare(COMPACT[0]).text


RELATIVE = "Relative IRIs are not allowed; write full IRIs"
NEAR_MISSES = {
    # An IRI that the grammar puts where an operand may follow an operator, or where a pattern has a term.
    "after-an-operator": ("SELECT * WHERE { ?s ?p ?o FILTER(?o=<vocab/v01/QueryHints#x>) }", RELATIVE),
    "after-a-minus-sign": ("SELECT * WHERE { ?s ?p ?o FILTER(?o=-<vocab/f>(?o)) }", RELATIVE),
    "after-a-subtraction": ("SELECT * WHERE { ?s ?p ?o FILTER(?o=1-<vocab/f>(?o)) }", RELATIVE),
    "after-distinct": ("SELECT (COUNT(DISTINCT<vocab/f>(?s)) AS ?n) WHERE { ?s ?p ?o }", RELATIVE),
    "in-a-list": ("SELECT * WHERE { ?s ?p ?o FILTER(?o IN(<http://e/a>,<vocab/b>)) }", RELATIVE),
    "as-a-datatype": ('SELECT * WHERE { ?s ?p ?o FILTER(?o<"1"^^<vocab/t>) }', RELATIVE),
    "an-object-after-a-variable": ("SELECT * WHERE { ?s ?p<vocab/v01/QueryHints#x> }", RELATIVE),
    "in-a-collection": ("SELECT * WHERE { ?s <http://e/p> (?o<vocab/x>) }", RELATIVE),
    "in-values": ("SELECT * WHERE { ?s ?p ?o } VALUES ?o { 1<vocab/x> }", RELATIVE),
    "a-function-in-order-by": ("SELECT * WHERE { ?s ?p ?o } ORDER BY ?o<vocab/f>(?s)", RELATIVE),
    "describe": ("DESCRIBE ?s<vocab/x> WHERE { ?s ?p ?o }", RELATIVE),
    "a-hint-after-a-filter": (
        "SELECT * WHERE { ?s ?p ?y FILTER(?y<5)<vocab/v01/QueryHints#Query> <vocab/v01/QueryHints#queryTimeout> 1 }",
        RELATIVE,
    ),
    # Where only a comparison fits, an IRI does not parse.
    "an-iri-for-a-comparison": (
        "SELECT * WHERE { ?s ?p ?o FILTER(?o<vocab/f>(?o)) }",
        "The console runs SPARQL queries (SELECT, ASK, CONSTRUCT, DESCRIBE); this one does not parse",
    ),
    # The other refusals, next to a comparison without spaces.
    "base": (
        "BASE <http://aws.amazon.com/neptune/> SELECT * WHERE { ?s ?p ?y FILTER(?y<2000&&?y>1900) }",
        "BASE is not allowed; write full IRIs",
    ),
    "a-hint-name": (
        # Neptune declares hint: itself; the masking took <hint:Query&&?y> for an IRI and hid the name.
        "SELECT * WHERE { ?s ?p ?y FILTER(?y<hint:Query&&?y>1) }",
        "Neptune query hints are not allowed: the console sets the timeout",
    ),
    "a-hint-iri": (
        f"SELECT * WHERE {{ ?s ?p ?y FILTER(?y<2000&&?y>1900) <{qs.HINT_IRI}Query> <{qs.HINT_IRI}queryTimeout> 1 }}",
        "Neptune query hints are not allowed: the console sets the timeout",
    ),
    "an-escaped-name": (
        r"PREFIX ex: <http://e/> SELECT * WHERE { ?s ?p ?y FILTER(?y<ex:a\/b&&?y>1) }",
        "Escaped characters in prefixed names are not allowed",
    ),
    "service": (
        "SELECT * WHERE { ?s ?p ?y FILTER(?y<2000&&EXISTS{SERVICE<http://evil/sparql>{?s ?p ?o}}) }",
        "SERVICE is not allowed",
    ),
    "graph": (
        "SELECT * WHERE { ?s ?p ?y FILTER(?y<2000&&EXISTS{GRAPH<http://other>{?s ?p ?o}}) }",
        "GRAPH is not allowed: the console chooses the graphs a query reads",
    ),
    "from": (
        "SELECT * FROM <http://other> WHERE { ?s ?p ?y FILTER(?y<2000&&?y>1900) }",
        "FROM and FROM NAMED are not allowed: the console chooses the graphs a query reads",
    ),
}


@pytest.mark.parametrize(("query", "message"), NEAR_MISSES.values(), ids=NEAR_MISSES.keys())
def test_iris_and_refused_clauses_next_to_comparisons_are_still_refused(query, message):
    with pytest.raises(qs.QueryRefused) as raised:
        _prepare(query)
    assert raised.value.message.startswith(message)


# Valid queries with an IRI right after an operand or an operator, without spaces.
NEXT_TO_IRIS = [
    "SELECT * WHERE { ?s ?p ?o FILTER(?o<5)?s<http://e/p> ?o }",
    "SELECT * WHERE { ?s ?p ?o FILTER(?o<5)<http://e/s> <http://e/p> ?o }",
    "SELECT ?s (COUNT(DISTINCT<http://e/f>(?o)) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?s<http://e/g>(?s)",
    "SELECT * WHERE { ?s ?p ?o } ORDER BY ?o<http://e/f>(?s) DESC(?s)",
    "DESCRIBE ?s<http://e/x>",
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


@pytest.mark.parametrize("query", NEXT_TO_IRIS)
def test_iris_next_to_operands_and_operators_still_run(query):
    prepared = _prepare(query)
    assert _dataset(prepared.text) == sorted(GRAPHS)
    assert _longest_match_iris(prepared.text) <= _parsed_iris(prepared.text)
    assert "<<" not in prepared.text  # RDF4J reads ``?a<<http://e/x>`` as RDF-star's <<


@pytest.mark.parametrize("query", [*COMPACT, *NEXT_TO_IRIS, *(query for query, _ in NEAR_MISSES.values())])
def test_every_iri_rdflib_reads_is_one_the_masking_collects(query):
    """What the relative-IRI check reads: an IRI the masking missed would reach Neptune unchecked."""
    try:
        # rdflib writes ``a`` as rdf:type, and a collection with rdf:first, rdf:rest and rdf:nil.
        parsed = _parsed_iris(query) - {str(t) for t in (RDF.type, RDF.first, RDF.rest, RDF.nil)}
    except Exception:  # noqa: BLE001 - a query rdflib does not parse is refused before it runs
        return
    iris: list[str] = []
    qs.mask_sparql(query, iris)
    assert parsed <= set(iris)


# Serve's copy of the scan, for the Database via VKG target, which serve cannot import from here either.
_SERVE_SPACING = (
    Path(__file__).resolve().parents[4]
    / "packages"
    / "context-manager"
    / "src"
    / "coa_serve"
    / "tier2"
    / "ontop"
    / "comparison_spacing.py"
)


@pytest.fixture(scope="module")
def serve_spacing():
    """``space_comparisons`` of serve's copy, loaded from its file (it imports nothing of serve)."""
    spec = importlib.util.spec_from_file_location("serve_comparison_spacing", _SERVE_SPACING)
    assert spec and spec.loader, _SERVE_SPACING
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module.space_comparisons
    del sys.modules[spec.name]


@pytest.mark.parametrize(
    "query",
    [
        *COMPACT,
        *NEXT_TO_IRIS,
        *(query for query, _ in NEAR_MISSES.values()),
        "SELECT ?s WHERE { ?s<http://e/p>?y FILTER(?y<2000&&?y!=<http://e/x>) }",
        "SELECT ?s WHERE { ?s ?p ?y FILTER(?y<=2000) }",
        'SELECT ?s WHERE { ?s ?p ?n FILTER(?n!="?y<2&&?y>1") } # FILTER(?y<2&&?y>1)',
    ],
)
def test_serve_spaces_a_comparison_for_the_vkg_as_the_draft_target_does(serve_spacing, query):
    """The VKG's parser lexes as RDF4J does: both copies of the scan must space the same ``<``."""
    assert serve_spacing(query) == qs._spaced_comparisons(query)


def test_vkg_queries_are_select_or_ask_without_their_own_graphs():
    assert qs.check_vkg_sparql("SELECT ?s WHERE { ?s ?p ?o }") == "SELECT"
    assert qs.check_vkg_sparql("ASK { ?s ?p ?o }") == "ASK"
    for bad in (
        "CONSTRUCT WHERE { ?s ?p ?o }",
        "SELECT * FROM <http://x> { ?s ?p ?o }",
        "SELECT * { SERVICE <http://x> { ?s ?p ?o } }",
    ):
        with pytest.raises(qs.QueryRefused):
            qs.check_vkg_sparql(bad)


_PLAIN = "SELECT * WHERE { ?s ?p ?o }"
# What serve's copy, coa_serve.tier2.ontop.sparql_guard.check_vkg_sparql, decides on each input
# (None: it refuses the query). VKG queries go to Ontop, so neither copy runs the Neptune checks.
SERVE_VKG_DECISIONS = {
    "base-with-a-relative-iri": ("BASE <http://e/> SELECT * WHERE { <a> ?p ?o }", "SELECT"),
    "a-comparison-without-spaces": ("SELECT * WHERE { ?s ?p ?y FILTER(?y<2000&&?y>1900) }", "SELECT"),
    "from": ("SELECT * FROM <http://other> WHERE { ?s ?p ?o }", None),
    "graph-in-filter-exists": ("SELECT * WHERE { ?s ?p ?o FILTER EXISTS { GRAPH ?g { ?s ?p ?o } } }", None),
    "service": ("SELECT * WHERE { SERVICE <http://evil/sparql> { ?s ?p ?o } }", None),
    "insert-data": ("INSERT DATA { <http://e/a> <http://e/b> <http://e/c> }", None),
    "unicode-escape": ('SELECT * WHERE { ?s ?p "\\u0041" }', None),
    "20000-characters": (_PLAIN + " " * (20_000 - len(_PLAIN)), "SELECT"),
    "20001-characters": (_PLAIN + " " * (20_001 - len(_PLAIN)), None),
    "not-a-string": (None, None),
}


@pytest.mark.parametrize(("query", "decision"), SERVE_VKG_DECISIONS.values(), ids=SERVE_VKG_DECISIONS.keys())
def test_the_vkg_guard_decides_as_serve_s_copy_does(query, decision):
    """The copy here also ran the Neptune text checks, so it refused BASE where serve accepts it."""
    if decision is None:
        with pytest.raises(qs.QueryRefused):
            qs.check_vkg_sparql(query)
    else:
        assert qs.check_vkg_sparql(query) == decision


# ── openCypher ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        f"MATCH (e:{ENTITY}) RETURN e.value",
        f"MATCH (e:{ENTITY})-[:`__RELATION__`]->(o:{ENTITY}) RETURN e, o LIMIT 10",
        f"MATCH (c:{CHUNK})<-[:`__MENTIONED_IN__`]-(e:{ENTITY}) WHERE (e)-->(:{ENTITY}) RETURN count(e)",
        f"MATCH (e:__Entity__{TENANT}__) RETURN e",
        f"MATCH (e:{ENTITY}) MATCH (e)-[r]->(x:{ENTITY}) RETURN e, r, x",
        f"MATCH (e:{ENTITY}) WHERE e.value CONTAINS 'CREATE (n)' RETURN e // DELETE in a comment",
        f"MATCH (e:{ENTITY} {{value: 'x'}}) RETURN size(collect(e))",
        f"MATCH (e:{ENTITY}) WHERE (e.value STARTS WITH 'A' OR e.value IS NULL) RETURN e",
        f"MATCH (e:{ENTITY}) WITH e MATCH (e2:{ENTITY}) RETURN e, e2",
    ],
)
def test_cypher_reading_the_namespace_s_nodes_runs(query):
    text = qs.prepare_cypher(query, TENANT, limit=1000, timeout_ms=20000)
    hint = "USING QUERY:TIMEOUTMILLISECONDS 20000\n"
    assert text.startswith(hint) and text[len(hint) :].startswith(query.rstrip(";"))
    assert ("LIMIT 1001" in text) == ("LIMIT" not in query)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) RETURN n",
        "MATCH () RETURN 1",
        f"MATCH (e:{ENTITY})-->(x) RETURN x",
        "MATCH (e:`__Entity__ffffffffffffffffffffffff0__`) RETURN e",
        f"MATCH (e:{ENTITY}:Admin) RETURN e",
        f"MATCH (e:{ENTITY}) SET e.value = 'x'",
        f"match (e:{ENTITY}) detach delete e",
        f"MATCH (e:{ENTITY}) CREATE (e)-[:R]->(e)",
        f"MATCH (e:{ENTITY}) MERGE (x:{ENTITY} {{value: 'y'}})",
        "CALL db.labels()",
        f"MATCH (e:{ENTITY}) CALL {{ MATCH (n) RETURN n }} RETURN e",
        f"MATCH (e:{ENTITY}) REMOVE e.value",
        "LOAD CSV FROM 'http://x' AS line RETURN line",
        f"MATCH (e:{ENTITY}) FOREACH (x IN [1] | SET e.v = x)",
        "",
        # The review's bypasses: a backtick variable, a Unicode identifier, a scope reset by WITH or
        # UNION, and a string spanning a newline that hid the rest of the query from the masking.
        "MATCH (`x`) RETURN `x`",
        "MATCH (é) RETURN é",
        f"MATCH (n:{ENTITY}) WITH count(*) AS c MATCH (n) RETURN n",
        f"MATCH (n:{ENTITY}) RETURN n UNION MATCH (n) RETURN n",
        "RETURN 'x\n' AS s UNION MATCH (n) RETURN n AS s //'",
        "RETURN 'x\n AS s MATCH (n) RETURN n //",
        "MATCH (n) /* RETURN n",
        # Label expressions, inline WHERE and IS-labels (Neo4j 5 syntax) are not node patterns we read.
        f"MATCH (n:{ENTITY}|Other) RETURN n",
        "MATCH (n WHERE n.value = 'x') RETURN n",
        "MATCH (n IS Other) RETURN n",
        f"MATCH (e:{ENTITY}) RETURN CASE WHEN (n)-->() THEN 1 END",
        # Rows and time: no LIMIT above the cap, no hints of its own.
        f"MATCH (e:{ENTITY}) RETURN e LIMIT 100000",
        f"USING QUERY:TIMEOUTMILLISECONDS 600000 MATCH (e:{ENTITY}) RETURN e",
        f"MATCH (e:{ENTITY}) RETURN e.`value`",
        # The re-review's local scopes: a variable a labeled pattern binds inside a pattern
        # comprehension or an EXISTS subquery is local to it; a later (x) is a new, unlabeled node.
        f"MATCH (a:{ENTITY}) WHERE size([(x:{ENTITY})--(y:{ENTITY}) | x]) >= 0 MATCH (x) RETURN x",
        f"MATCH (a:{ENTITY}) WITH a, [(x:{ENTITY})--(y:{ENTITY}) | x] AS xs MATCH (x) RETURN x",
        f"MATCH (a:{ENTITY}) WHERE any(q IN [(x:{ENTITY})-->(y:{ENTITY}) | 1] WHERE q = 1) "
        f"MATCH (x)-->(z:{ENTITY}) RETURN x",
        f"MATCH (a:{ENTITY}) WHERE EXISTS {{ MATCH (x:{ENTITY}) }} MATCH (x) RETURN x",
        f"MATCH (a:{ENTITY}) WHERE CASE WHEN size([(x:{CHUNK})--(w:{CHUNK}) | x]) > 0 THEN true ELSE true END "
        "MATCH (x) RETURN x",
        f"MATCH (a:{ENTITY}) WHERE (a)-->(b:{ENTITY}) MATCH (b) RETURN b",
        # A parameter as the property map is not a node pattern the guard reads.
        "MATCH (n $p) RETURN n",
    ],
)
def test_cypher_refuses_untenanted_patterns_and_writes(query):
    with pytest.raises(qs.QueryRefused):
        qs.prepare_cypher(query, TENANT, limit=1000, timeout_ms=20000)


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("DROP INDEX entity_value", "DROP is not allowed: the console only reads the lexical graph"),
        (
            f"MATCH (e:{ENTITY}) RETURN e // the index\ndrop index x",
            "DROP is not allowed: the console only reads the lexical graph",
        ),
        (f"MATCH (e:{ENTITY}) RETURN e.`value", "The query has an unterminated backtick name"),
    ],
)
def test_cypher_refuses_drop_and_an_unterminated_backtick_name(query, message):
    """No test had a DROP, or a backtick name that does not end."""
    with pytest.raises(qs.QueryRefused) as raised:
        qs.prepare_cypher(query, TENANT, limit=1000, timeout_ms=20000)
    assert raised.value.message == message


def test_cypher_caps_the_final_return_whatever_comes_before():
    text = qs.prepare_cypher(f"MATCH (e:{ENTITY}) WITH e LIMIT 1 RETURN e", TENANT, limit=1000, timeout_ms=1)
    assert text.endswith("LIMIT 1001")
    kept = qs.prepare_cypher(f"MATCH (e:{ENTITY}) RETURN e LIMIT 10;", TENANT, limit=1000, timeout_ms=1)
    assert kept.endswith("RETURN e LIMIT 10")


def test_cypher_names_the_labels_it_accepts():
    with pytest.raises(qs.QueryRefused) as raised:
        qs.prepare_cypher("MATCH (n) RETURN n", TENANT, limit=10, timeout_ms=1)
    assert f"__Entity__{TENANT}__" in raised.value.message
    assert qs.tenant_labels(TENANT) >= {f"__Entity__{TENANT}__", f"__Chunk__{TENANT}__"}
