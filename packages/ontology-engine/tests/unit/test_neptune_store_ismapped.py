# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""NeptuneDBGraphStore.store_class emits the coa:isMapped marker correctly.

This exercises the REAL store_class SPARQL emission (only the network call
``_sparql_update`` is patched), so the exact INSERT DATA triple — including the
coa:isMapped IRI and the ``"true"^^xsd:boolean`` literal the serve T-Box filter
matches against — is verified. Everything else in the pipeline mocks store_class,
so without this the actual triple syntax would be untested until deploy.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from coa_common.constants import GRAPH_BASE_URI

pytestmark = pytest.mark.unit

from coa_ontology.stores.neptune_db_graph import NeptuneDBGraphStore

_ISMAPPED_IRI = f"{GRAPH_BASE_URI}/vocab/coa#isMapped"
_XSD_BOOL = "http://www.w3.org/2001/XMLSchema#boolean"


def _capture_store_class(*, is_mapped: bool) -> str:
    """Run the real store_class with _sparql_update patched; return the SPARQL."""
    store = NeptuneDBGraphStore(endpoint="https://ndb.example:8182", namespace="ns-test")
    with patch("coa_ontology.stores.neptune_db_graph._sparql_update") as upd:
        store.store_class(
            ontology_uri="http://example.org/o#",
            class_uri="http://example.org/o#Orders",
            labels=["Orders"],
            is_mapped=is_mapped,
        )
        assert upd.call_count == 1, "store_class must issue exactly one SPARQL update"
        return upd.call_args[0][0]


class TestStoreClassIsMappedEmission:
    def test_mapped_class_emits_ismapped_boolean_triple(self):
        sparql = _capture_store_class(is_mapped=True)
        # The exact triple the serve filter (?class <scl#isMapped> true) matches.
        assert _ISMAPPED_IRI in sparql
        assert f'"true"^^<{_XSD_BOOL}>' in sparql
        # Sanity: it's inside an INSERT DATA and carries the class as subject.
        assert "INSERT DATA" in sparql
        assert "http://example.org/o#Orders" in sparql

    def test_unmapped_class_omits_ismapped_triple(self):
        sparql = _capture_store_class(is_mapped=False)
        # Absent marker == unmapped == hidden by the serve filter. No isMapped
        # triple, and definitely no "false" (we never emit a negative).
        assert _ISMAPPED_IRI not in sparql
        # Class is still stored as an owl:Class (just without the marker).
        assert "http://example.org/o#Orders" in sparql
        assert "2002/07/owl#Class" in sparql

    def test_written_triple_matches_serve_filter_pattern_via_rdflib(self):
        """Round-trip: the literal store_class WRITES must satisfy the term the
        serve T-Box filter QUERIES (``?class <scl#isMapped> true``).

        The write side emits ``"true"^^xsd:boolean``; the read side uses the
        SPARQL keyword ``true``. This asserts, via rdflib, that they are the
        SAME term — so the real Neptune write→read actually matches (the one
        thing mocks can't prove short of a live cluster)."""
        import re

        from rdflib import Graph

        # Pull the ACTUAL isMapped triple line store_class emitted (not a
        # hand-written copy), load it into rdflib, and confirm the serve filter's
        # `true` keyword matches the written "true"^^xsd:boolean term.
        sparql = _capture_store_class(is_mapped=True)
        m = re.search(rf"(\S+\s+<{re.escape(_ISMAPPED_IRI)}>\s+\S+\s*\.)", sparql)
        assert m, f"could not find the isMapped triple in emitted SPARQL:\n{sparql}"
        triple = m.group(1)

        g = Graph()
        g.parse(data=triple, format="turtle")
        ask = g.query(f"ASK {{ ?c <{_ISMAPPED_IRI}> true }}")
        assert bool(ask), (
            f"serve filter `<isMapped> true` must match the boolean literal store_class actually wrote: {triple!r}"
        )


def _capture_store_property(*, unmapped: bool, batch: bool = False) -> str:
    """Run the real store_property (or the batch path) with _sparql_update patched; return the SPARQL."""
    store = NeptuneDBGraphStore(endpoint="https://ndb.example:8182", namespace="ns-test")
    spec = {
        "prop_uri": "http://example.org/o#customers_loyaltyTier",
        "label": "loyalty tier",
        "domains": ["http://example.org/o#Customers"],
        "unmapped": unmapped,
    }
    with patch("coa_ontology.stores.neptune_db_graph._sparql_update") as upd:
        if batch:
            store.store_properties_batch(ontology_uri="http://example.org/o#", properties=[spec])
        else:
            store.store_property(ontology_uri="http://example.org/o#", **spec)
        assert upd.call_count == 1
        return upd.call_args[0][0]


class TestStorePropertyUnmappedMarker:
    """An unmapped property of a mapped class carries ``coa:isMapped false``; every other property carries nothing."""

    @pytest.mark.parametrize("batch", [False, True], ids=["store_property", "store_properties_batch"])
    def test_unmapped_property_emits_the_false_marker(self, batch):
        sparql = _capture_store_property(unmapped=True, batch=batch)
        assert f'<http://example.org/o#customers_loyaltyTier> <{_ISMAPPED_IRI}> "false"^^<{_XSD_BOOL}> .' in sparql
        assert '"true"' not in sparql

    @pytest.mark.parametrize("batch", [False, True], ids=["store_property", "store_properties_batch"])
    def test_other_properties_carry_no_marker(self, batch):
        assert _capture_store_property(unmapped=False, batch=batch).count(_ISMAPPED_IRI) == 0

    def test_written_triple_is_what_the_serve_property_gate_excludes(self):
        """Serve drops a property with ``FILTER NOT EXISTS { ?p <isMapped> false }``; the literal written must match."""
        import re

        from rdflib import Graph

        sparql = _capture_store_property(unmapped=True)
        lines = [line.strip() for line in sparql.splitlines() if line.strip().endswith(" .")]
        g = Graph()
        g.parse(data="\n".join(lines), format="turtle")
        g.parse(data='<http://example.org/o#customers_email> <http://www.w3.org/2000/01/rdf-schema#label> "email" .')
        rows = g.query(
            "SELECT ?p WHERE { ?p <http://www.w3.org/2000/01/rdf-schema#label> ?l "
            f"FILTER NOT EXISTS {{ ?p <{_ISMAPPED_IRI}> false }} }}"
        )
        assert [str(r.p) for r in rows] == ["http://example.org/o#customers_email"]
        assert re.search(r'"false"\^\^<[^>]+#boolean>', sparql)


_PHYSICAL_NAME_IRI = f"{GRAPH_BASE_URI}/vocab/coa#physicalName"


def _capture_projection(*, batch: bool, table: str, column: str) -> str:
    """Store a class and a property with physical names through the real store; return the SPARQL sent."""
    store = NeptuneDBGraphStore(endpoint="https://ndb.example:8182", namespace="ns-test")
    cls = {
        "class_uri": "http://example.org/o#Customers",
        "labels": ["Client"],
        "is_mapped": True,
        "physical_name": table,
    }
    prop = {"prop_uri": "http://example.org/o#customers_email", "label": "E-mail address", "physical_name": column}
    with patch("coa_ontology.stores.neptune_db_graph._sparql_update") as upd:
        if batch:
            store.store_classes_batch(ontology_uri="http://example.org/o#", classes=[cls])
            store.store_properties_batch(ontology_uri="http://example.org/o#", properties=[prop])
        else:
            store.store_class(ontology_uri="http://example.org/o#", **cls)
            store.store_property(ontology_uri="http://example.org/o#", **prop)
        return "\n".join(c.args[0] for c in upd.call_args_list)


class TestPhysicalNameMarker:
    """A mapped class read from one table, and an attribute read from one column, carry ``coa:physicalName``."""

    @pytest.mark.parametrize("batch", [False, True], ids=["one_by_one", "batch"])
    def test_the_table_and_column_names_are_written_as_plain_literals(self, batch):
        sparql = _capture_projection(batch=batch, table="customers", column="email")
        assert f'<http://example.org/o#Customers> <{_PHYSICAL_NAME_IRI}> "customers" .' in sparql
        assert f'<http://example.org/o#customers_email> <{_PHYSICAL_NAME_IRI}> "email" .' in sparql

    @pytest.mark.parametrize("batch", [False, True], ids=["one_by_one", "batch"])
    def test_no_name_writes_no_triple(self, batch):
        assert _PHYSICAL_NAME_IRI not in _capture_projection(batch=batch, table="", column="")

    def test_the_written_triple_reads_back_as_the_name_even_with_a_quote_in_it(self):
        from rdflib import Graph, Literal, URIRef

        sparql = _capture_projection(batch=False, table='order "lines"', column="email")
        lines = [line.strip() for line in sparql.splitlines() if line.strip().endswith(" .")]
        g = Graph().parse(data="\n".join(lines), format="turtle")
        names = dict(g.subject_objects(URIRef(_PHYSICAL_NAME_IRI)))
        assert names == {
            URIRef("http://example.org/o#Customers"): Literal('order "lines"'),
            URIRef("http://example.org/o#customers_email"): Literal("email"),
        }


_MAPPED_DOMAIN_IRI = f"{GRAPH_BASE_URI}/vocab/coa#mappedDomain"
_MAPPED_RANGE_IRI = f"{GRAPH_BASE_URI}/vocab/coa#mappedRange"


def _capture_mapped_ends(*, batch: bool, domains: list[str], ranges: list[str]) -> str:
    """Store a relationship with the classes its mapping puts it on; return the SPARQL sent."""
    store = NeptuneDBGraphStore(endpoint="https://ndb.example:8182", namespace="ns-test")
    prop = {
        "prop_uri": "http://example.org/o#orders_placedBy",
        "domains": ["http://example.org/o#Orders"],
        "ranges": ["http://example.org/o#Party"],
        "mapped_domains": domains,
        "mapped_ranges": ranges,
    }
    with patch("coa_ontology.stores.neptune_db_graph._sparql_update") as upd:
        if batch:
            store.store_properties_batch(ontology_uri="http://example.org/o#", properties=[prop])
        else:
            store.store_property(ontology_uri="http://example.org/o#", **prop)
        return "\n".join(c.args[0] for c in upd.call_args_list)


class TestMappedEndsMarker:
    """A property its mapping puts on (or joins to) another class than its domain (or range) says names that class."""

    @pytest.mark.parametrize("batch", [False, True], ids=["store_property", "store_properties_batch"])
    def test_the_classes_are_written_as_iris(self, batch):
        sparql = _capture_mapped_ends(
            batch=batch, domains=["http://example.org/o#Orders"], ranges=["http://example.org/o#Customers"]
        )
        p = "<http://example.org/o#orders_placedBy>"
        assert f"{p} <{_MAPPED_DOMAIN_IRI}> <http://example.org/o#Orders> ." in sparql
        assert f"{p} <{_MAPPED_RANGE_IRI}> <http://example.org/o#Customers> ." in sparql

    @pytest.mark.parametrize("batch", [False, True], ids=["store_property", "store_properties_batch"])
    def test_none_writes_no_triple(self, batch):
        sparql = _capture_mapped_ends(batch=batch, domains=[], ranges=[])
        assert _MAPPED_DOMAIN_IRI not in sparql and _MAPPED_RANGE_IRI not in sparql
