# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for turtle_export.format_grouped_turtle."""

import pytest
from coa_common.constants import VOCAB_PREFIX, VOCAB_URI
from coa_ontology.catalog.turtle_export import format_grouped_turtle
from rdflib import Graph

# Neptune's GSP GET emits prefix-light Turtle: full URIs, no ``@prefix`` for the
# engine's own vocab. This fixture reproduces that shape with a triple in COA's
# vocabulary (``isMapped``) written as an absolute IRI — the exact condition
# that made the export auto-mint an undeclared ``ns1:`` prefix and produce
# unparseable output.
COA_VOCAB_TURTLE = f"""\
<https://example.org/onto#> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> \
<http://www.w3.org/2002/07/owl#Ontology> .

<https://example.org/onto#Customer> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> \
<http://www.w3.org/2002/07/owl#Class> ;
    <http://www.w3.org/2000/01/rdf-schema#label> "Customer" ;
    <{VOCAB_URI}isMapped> true ;
    <{VOCAB_URI}distinctValues> 42 .
"""

SAMPLE_TURTLE = """\
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

<https://example.org/onto#> a owl:Ontology ;
    rdfs:label "Test Ontology" .

<https://example.org/onto#Customer> a owl:Class ;
    rdfs:label "Customer" .

<https://example.org/onto#Policy> a owl:Class ;
    rdfs:label "Policy" .

<https://example.org/onto#customer_name> a rdf:Property , owl:DatatypeProperty ;
    rdfs:label "Name" ;
    rdfs:domain <https://example.org/onto#Customer> ;
    rdfs:range xsd:string .

<https://example.org/onto#customer_policy> a rdf:Property , owl:ObjectProperty ;
    rdfs:label "Has Policy" ;
    rdfs:domain <https://example.org/onto#Customer> ;
    rdfs:range <https://example.org/onto#Policy> .
"""


@pytest.mark.unit
class TestFormatGroupedTurtle:
    def test_groups_by_type(self):
        result = format_grouped_turtle(SAMPLE_TURTLE)
        # Should have section headers
        assert "# ── Ontology" in result
        assert "# ── Classes" in result
        assert "# ── Object properties" in result
        assert "# ── Datatype properties" in result

    def test_binds_ontology_prefix(self):
        result = format_grouped_turtle(SAMPLE_TURTLE)
        # The ontology namespace should be bound as the empty prefix
        assert "@prefix : <https://example.org/onto#>" in result

    def test_uses_short_prefixes(self):
        result = format_grouped_turtle(SAMPLE_TURTLE)
        # Classes should use the short prefix form
        assert ":Customer" in result
        assert ":Policy" in result

    def test_classes_before_properties(self):
        result = format_grouped_turtle(SAMPLE_TURTLE)
        classes_pos = result.index("# ── Classes")
        obj_pos = result.index("# ── Object properties")
        dt_pos = result.index("# ── Datatype properties")
        assert classes_pos < obj_pos < dt_pos

    def test_preserves_all_triples(self):
        result = format_grouped_turtle(SAMPLE_TURTLE)
        # Key content should survive the reformat
        assert "Customer" in result
        assert "Policy" in result
        assert "Has Policy" in result
        assert "Name" in result

    def test_invalid_turtle_returns_original(self):
        bad = "this is not valid turtle @@@"
        result = format_grouped_turtle(bad)
        assert result == bad

    def test_empty_input(self):
        result = format_grouped_turtle("")
        # Empty input parses as empty graph — should produce minimal output
        assert isinstance(result, str)

    def test_no_unused_prefixes(self):
        result = format_grouped_turtle(SAMPLE_TURTLE)
        # Should NOT have rdflib's auto-bound prefixes like brick, csvw, etc.
        assert "brick:" not in result
        assert "csvw:" not in result
        assert "foaf:" not in result

    def test_ontology_declaration_in_ontology_section(self):
        result = format_grouped_turtle(SAMPLE_TURTLE)
        onto_section_start = result.index("# ── Ontology")
        classes_section_start = result.index("# ── Classes")
        # The ontology declaration should be between these two markers
        assert "owl:Ontology" in result[onto_section_start:classes_section_start]

    def test_output_round_trips_with_coa_vocab(self):
        """The exported document must re-parse. Triples in the engine's own
        (prefix-light) vocab used to auto-mint an undeclared ``ns1:`` prefix in
        the body while the prefix block never declared it, so any RDF parser
        rejected the file. Re-parsing the output is the guard that catches it."""
        result = format_grouped_turtle(COA_VOCAB_TURTLE)
        # Must not raise BadSyntax ("Prefix ... not bound").
        Graph().parse(data=result, format="turtle")

    def test_the_download_keeps_the_property_marker_deploy_wrote(self):
        """A property of a mapped class with no mapping carries ``coa:isMapped false`` in Neptune; the
        download shows it like the class marker (an upload strips both, so it is never trusted back)."""
        served = COA_VOCAB_TURTLE + (
            "<https://example.org/onto#customer_tier> "
            "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#DatatypeProperty> ;\n"
            f"    <{VOCAB_URI}isMapped> false .\n"
        )
        exported = Graph().parse(data=format_grouped_turtle(served), format="turtle")
        assert set(exported) == set(Graph().parse(data=served, format="turtle"))

    def test_the_download_keeps_the_physical_names_deploy_wrote(self):
        """Deploy names a mapped class and attribute by their table and column (``coa:physicalName``); the
        download shows them like the markers, and an upload strips them the same way."""
        served = COA_VOCAB_TURTLE + (
            "<https://example.org/onto#Customers> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
            f'<http://www.w3.org/2002/07/owl#Class> ;\n    <{VOCAB_URI}physicalName> "customers" .\n'
            "<https://example.org/onto#customers_email> "
            "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#DatatypeProperty> ;\n"
            f'    <{VOCAB_URI}physicalName> "email" .\n'
        )
        result = format_grouped_turtle(served)
        assert set(Graph().parse(data=result, format="turtle")) == set(Graph().parse(data=served, format="turtle"))
        assert f'{VOCAB_PREFIX}:physicalName "customers"' in result

    def test_coa_vocab_uses_declared_prefix_not_automint(self):
        """The COA vocab renders with its real, DECLARED prefix — never an
        auto-minted ``ns1:``/``ns2:`` that would be undeclared in the output."""
        result = format_grouped_turtle(COA_VOCAB_TURTLE)
        assert f"@prefix {VOCAB_PREFIX}: <{VOCAB_URI}>" in result
        assert f"{VOCAB_PREFIX}:isMapped" in result
        # No auto-minted throwaway prefixes leaked into the document.
        assert "ns1:" not in result
        assert "ns2:" not in result

    def test_output_round_trips_with_unknown_unbound_vocab(self):
        """Structural guard: a namespace NOT in the curated bind list must still
        round-trip. Any future predicate from an unbound vocabulary should get a
        declared prefix rather than reintroducing the undeclared-prefix bug."""
        unknown_turtle = """\
<https://example.org/onto#> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> \
<http://www.w3.org/2002/07/owl#Ontology> .

<https://example.org/onto#Thing> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> \
<http://www.w3.org/2002/07/owl#Class> ;
    <http://unknown.example/vocab#weird> "value" .
"""
        result = format_grouped_turtle(unknown_turtle)
        Graph().parse(data=result, format="turtle")  # must not raise
