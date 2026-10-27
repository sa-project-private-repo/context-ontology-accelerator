# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for SPARQL validator URI extraction and filtering."""

from __future__ import annotations

import pytest
from coa_serve.query_utils import IS_MAPPED_IRI
from coa_serve.tier2.ontop.sparql_validator import _SAFE_URI_RE, SPARQLValidator


@pytest.mark.unit
class TestSafeUriRegex:
    def test_rejects_a_trailing_newline(self):
        """`\\Z`, not `$`: `$` matches before a trailing newline, which would let a
        newline-bearing IRI reach SPARQL interpolation. A clean IRI still passes."""
        assert _SAFE_URI_RE.match("https://example.org/ontology")
        assert not _SAFE_URI_RE.match("https://example.org/ontology\n")
        assert not _SAFE_URI_RE.match("https://example.org/o\ninjected")


@pytest.fixture
def validator():
    """Validator with a mock graph client (no Neptune needed)."""
    from unittest.mock import AsyncMock

    mock_graph = AsyncMock()
    return SPARQLValidator(graph_client=mock_graph)


@pytest.mark.unit
class TestURIExtraction:
    def test_extracts_domain_uris(self, validator):
        sparql = """
        PREFIX : <http://example.org/insurance#>
        SELECT ?x WHERE { <http://example.org/insurance#Claim> a <http://example.org/insurance#Policy> }
        """
        uris = validator._extract_uris(sparql)
        assert "http://example.org/insurance#Claim" in uris
        assert "http://example.org/insurance#Policy" in uris

    def test_excludes_w3c_standard_uris(self, validator):
        sparql = """
        SELECT ?x WHERE {
            <http://example.org/insurance#Claim> a <http://www.w3.org/2002/07/owl#Class> .
            <http://example.org/insurance#claimId> <http://www.w3.org/2000/01/rdf-schema#range> <http://www.w3.org/2001/XMLSchema#string>
        }
        """
        uris = validator._extract_uris(sparql)
        assert "http://example.org/insurance#Claim" in uris
        assert "http://example.org/insurance#claimId" in uris
        # W3C URIs should be excluded
        assert all("www.w3.org" not in u for u in uris)

    def test_excludes_bare_namespace_uris(self, validator):
        sparql = "PREFIX : <http://example.org/insurance#> SELECT ?x WHERE { ?x a :Claim }"
        uris = validator._extract_uris(sparql)
        # The bare namespace URI (ending with #) should be excluded
        assert "http://example.org/insurance#" not in uris

    def test_excludes_slash_ending_namespaces(self, validator):
        sparql = "PREFIX ex: <http://example.org/> SELECT ?x WHERE { ex:thing a ex:Class }"
        uris = validator._extract_uris(sparql)
        assert "http://example.org/" not in uris

    def test_excludes_xmlns_and_purl(self, validator):
        sparql = """
        SELECT ?x WHERE {
            ?x <http://xmlns.com/foaf/0.1/name> "test" .
            ?x <http://purl.org/dc/elements/1.1/title> "title"
        }
        """
        uris = validator._extract_uris(sparql)
        assert len(uris) == 0

    def test_empty_sparql_returns_empty(self, validator):
        assert validator._extract_uris("") == []
        assert validator._extract_uris("SELECT ?x WHERE { ?x ?p ?o }") == []


# ── A property deploy marked unmapped is not part of the vocabulary ────────────


_IS_MAPPED = IS_MAPPED_IRI
_TEMPLATE = "https://ontology-workbench.local/{namespace}"
_PREFIXED = (
    "PREFIX ind: <http://example.org/o#>\n"
    "SELECT ?c ?tier WHERE { ?c a ind:Customer ; ind:customer_loyaltyTier ?tier . }"
)


@pytest.mark.unit
class TestUnmappedPropertyIsRefused:
    """An attribute or relationship the designer added to a mapped class has no mapping.

    Deploy marks it ``coa:isMapped false``. A generated query that uses it is refused
    with the feedback an unknown IRI gets, so the correction loop rewrites it; one
    that does not is judged exactly as before.
    """

    async def test_the_existence_check_counts_a_marked_iri_as_missing(self):
        from unittest.mock import AsyncMock

        client = AsyncMock()
        client.query.return_value = [{"found": "1"}]
        validator = SPARQLValidator(client, graph_uri_template=_TEMPLATE)

        await validator.validate("SELECT ?t WHERE { ?c <http://example.org/o#customer_loyaltyTier> ?t }", "ns")

        assert f"FILTER NOT EXISTS {{ ?s <{_IS_MAPPED}> false }}" in client.query.call_args_list[0][0][0]

    async def test_a_prefixed_name_marked_unmapped_is_refused_like_an_unknown_iri(self):
        from unittest.mock import AsyncMock

        client = AsyncMock()
        client.query.return_value = [{"s": "http://example.org/o#customer_loyaltyTier"}]
        validator = SPARQLValidator(client, graph_uri_template=_TEMPLATE)

        result = await validator.validate(_PREFIXED, "ns", graph_iris=["https://ontology-workbench.local/ns/g"])

        assert result.valid is False
        assert result.error == "URI not found in ontology"
        # The namespace's marked properties, read as a whole; the query's own names are not sent.
        marker_query = client.query.call_args[0][0]
        assert f"?s <{_IS_MAPPED}> false" in marker_query
        assert "customer_loyaltyTier" not in marker_query
        assert 'STRSTARTS(STR(?g), "https://ontology-workbench.local/ns/")' in marker_query
        client.ask.assert_not_called()  # refused before the domain/range stage

    async def test_a_failed_marker_read_refuses_nothing_and_is_read_again(self):
        from unittest.mock import AsyncMock

        client = AsyncMock()
        client.query.side_effect = RuntimeError("neptune down")
        client.ask.return_value = False
        validator = SPARQLValidator(client, graph_uri_template=_TEMPLATE)

        assert (await validator.validate(_PREFIXED, "ns")).valid is True
        assert (await validator.validate(_PREFIXED, "ns")).valid is True
        assert client.query.call_count == 2  # a failure is not kept

    async def test_the_marked_properties_are_read_once_a_minute_per_namespace(self):
        """Every Ontop answer validates its SPARQL up to three times; the marker costs one read, not one each."""
        from unittest.mock import AsyncMock

        client = AsyncMock()
        client.query.return_value = []
        client.ask.return_value = False
        now = [0.0]
        validator = SPARQLValidator(client, graph_uri_template=_TEMPLATE, clock=lambda: now[0])

        for _ in range(3):
            assert (await validator.validate(_PREFIXED, "ns")).valid is True
        await validator.validate(_PREFIXED, "other-ns")
        assert client.query.call_count == 2
        now[0] = 61.0  # a deploy is seen within a minute
        await validator.validate(_PREFIXED, "ns")
        assert client.query.call_count == 3

    def test_every_spelling_of_an_iri_resolves_to_the_iri_sparql_gives_it(self, validator):
        """What the query names is read from the parsed query, never from its text."""
        base, empty = "http://example.org/o#", "http://example.org/e#"
        sparql = (
            f"BASE <{base[:-1]}>\n"
            f"PREFIX : <{empty}>\n"
            f"PREFIX ind: <{base}>\n"
            f"PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
            "SELECT ?c WHERE {\n"
            "  ?c a :Customer ; ind:customers_会員-ランク ?r ; ind:customers_会員%E3%83%BBランク ?s ;\n"
            "     ind:loyalty.tier ?t ; ind:esc\\-aped ?u ; <#relative> ?v ; ind:p1|ind:p2/^ind:p3 ?w .\n"
            '  ?c rdfs:label "ind:notATerm" .\n'
            "  FILTER(?w != ind:inFilter)\n"
            "}"
        )
        named = validator._query_iris(sparql)
        assert empty + "Customer" in named
        assert {iri for iri in named if iri.startswith(base)} == {
            base + local
            for local in (
                "customers_会員-ランク",
                "customers_会員%E3%83%BBランク",
                "loyalty.tier",
                "esc-aped",
                "relative",
                "p1",
                "p2",
                "p3",
                "inFilter",
            )
        }
        assert not any("notATerm" in iri for iri in named)


@pytest.mark.unit
class TestUnmappedPropertyAgainstTheDeployedGraph:
    """The same, against the graph a deploy writes, through the real Neptune client."""

    # A designer attribute of the mapped class whose label has two words in Japanese: the designer
    # joins them with a hyphen when it mints the IRI.
    _MEMBER_RANK = "customers_会員-ランク"

    @classmethod
    def _validator(cls, **dataset_kwargs) -> SPARQLValidator:
        from rdflib import OWL, RDF, RDFS, URIRef
        from rdflib import Literal as RdfLiteral

        from . import deployed_graph

        ds = deployed_graph.designer_v2_dataset(**dataset_kwargs)
        rank = URIRef(deployed_graph.PREFIX + cls._MEMBER_RANK)
        g = ds.graph(URIRef(deployed_graph.GRAPH))
        g.add((rank, RDF.type, OWL.DatatypeProperty))
        g.add((rank, RDFS.domain, URIRef(deployed_graph.CUSTOMERS)))
        if dataset_kwargs.get("property_markers", True):
            g.add((rank, deployed_graph.IS_MAPPED, RdfLiteral(False)))
        return SPARQLValidator(deployed_graph.neptune_over(ds), graph_uri_template=deployed_graph.TEMPLATE)

    @classmethod
    def _queries(cls) -> dict[str, str]:
        from .deployed_graph import LOYALTY_TIER, ONTOLOGY_ID, PREFIX

        prefix = f"PREFIX ind: <{PREFIX}>\n"
        mapped = ("customers_id", "customers_name", "customers_email", "orders_id", "orders_status")
        # Ten distinct, existing, unmarked full IRIs fill the existence check before the marked one.
        ten = " ".join(f"?c{i} <{PREFIX}{mapped[i % 5]}> ?v{i} ." for i in range(5))
        ten += " " + " ".join(f"?k{i} a <{PREFIX}{name}> ." for i, name in enumerate(("Customers", "PurchaseOrder")))
        ten += f" ?x <{PREFIX}orders_totalAmount> ?y . ?x <{PREFIX}orders_customerId> ?z ."
        ten += f" ?q a <{PREFIX}PremiumCustomer> ."
        return {
            "prefixed": prefix + "SELECT ?c ?tier WHERE { ?c a ind:Customers ; ind:customers_loyaltyTier ?tier . }",
            "full_iri": f"SELECT ?tier WHERE {{ ?c <{LOYALTY_TIER}> ?tier . }}",
            "relationship": prefix + "SELECT ?o WHERE { ?c a ind:Customers ; ind:customers_preferredOrder ?o . }",
            "empty_prefix": f"PREFIX : <{PREFIX}>\nSELECT ?t WHERE {{ ?c a :Customers ; :customers_loyaltyTier ?t }}",
            "base_relative": f"BASE <{ONTOLOGY_ID}>\nSELECT ?t WHERE {{ ?c <#customers_loyaltyTier> ?t }}",
            "hyphenated": prefix + f"SELECT ?r WHERE {{ ?c a ind:Customers ; ind:{cls._MEMBER_RANK} ?r . }}",
            "eleventh_full_iri": f"SELECT * WHERE {{ {ten} ?c <{LOYALTY_TIER}> ?tier . }}",
            "mapped": prefix + "SELECT ?c ?email WHERE { ?c a ind:Customers ; ind:customers_email ?email . }",
        }

    @pytest.mark.parametrize(
        "name",
        ["prefixed", "full_iri", "relationship", "empty_prefix", "base_relative", "hyphenated", "eleventh_full_iri"],
    )
    async def test_a_query_on_a_marked_property_is_refused(self, name):
        from .deployed_graph import NS

        result = await self._validator().validate(self._queries()[name], NS)

        assert (result.valid, result.error) == (False, "URI not found in ontology")

    async def test_a_query_on_mapped_properties_passes(self):
        from .deployed_graph import LOYALTY_TIER, NS

        validator = self._validator()
        queries = self._queries()
        ten = queries["eleventh_full_iri"].replace(f"?c <{LOYALTY_TIER}> ?tier .", "")

        assert (await validator.validate(queries["mapped"], NS)).valid is True
        assert (await validator.validate(ten, NS)).valid is True  # the eleventh full IRI is what is refused

    @pytest.mark.parametrize(
        "name",
        ["prefixed", "full_iri", "relationship", "empty_prefix", "base_relative", "hyphenated", "mapped"],
    )
    async def test_without_property_markers_every_verdict_is_today_s(self, name):
        from coa_serve.tier2.ontop.sparql_validator import ValidationResult

        from .deployed_graph import NS

        result = await self._validator(property_markers=False).validate(self._queries()[name], NS)

        assert result == ValidationResult(valid=True)
