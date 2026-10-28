# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Layer-0 Tier-2 answerability signal: ingest_ontology tags R2RML-mapped classes.

End-to-end (through ``ingest_ontology`` with mocked stores) verification that a
class which is the ``rr:class`` of an R2RML TriplesMap is stored with
``is_mapped=True`` (=> serve T-Box context exposes it to the structured-query
prompt), while a class NOT in the R2RML is stored with ``is_mapped=False``
(=> hidden). Unstructured proposals pass no R2RML → every class is unmapped.
"""

from __future__ import annotations

from unittest.mock import MagicMock, create_autospec, patch

import pytest

pytestmark = pytest.mark.unit

from coa_common.constants import VOCAB_URI
from coa_ontology.catalog.ingest import IngestMetadata, ingest_ontology
from coa_ontology.stores.na_graph import NeptuneAnalyticsGraphStore

_ONTOLOGY_TTL = """
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex:   <http://example.org/o#> .

<http://example.org/o#> a owl:Ontology .
ex:Orders    a owl:Class ; rdfs:label "Orders" .
ex:DocConcept a owl:Class ; rdfs:label "DocConcept" .
"""

# R2RML maps ONLY ex:Orders (structured/table-backed). ex:DocConcept has no
# TriplesMap → it is an unmapped (e.g. document-induced) class.
_R2RML_TTL = """
@prefix rr:  <http://www.w3.org/ns/r2rml#> .
@prefix coa: <http://coa.amazon.com/vocab/coa#> .
@prefix ex:  <http://example.org/o#> .

ex:TriplesMap_Orders a rr:TriplesMap ;
    coa:datasourceId "ds-orders-1" ;
    rr:subjectMap [ rr:class ex:Orders ] .
"""


def _ingest_with_r2rml(r2rml_content, *, return_result=False):
    graph_store, vector_store = MagicMock(), MagicMock()
    with (
        patch("coa_ontology.catalog.ingest.dynamo_store") as ds,
        patch(
            "coa_ontology.catalog.ingest._accumulate_embeddings",
            return_value={"status": "ok", "count": 0, "model_id": "test", "index": "test"},
        ),
    ):
        ds.get_ontology_registry.return_value = None
        ds.put_namespace_meta.return_value = None
        result = ingest_ontology(
            graph_store,
            vector_store,
            _ONTOLOGY_TTL,
            namespace="ns-test",
            metadata=IngestMetadata(ontology_id="http://example.org/o#", format="turtle", source="unit-test"),
            validate=False,
            allow_append=True,
            r2rml_content=r2rml_content,
        )
    if return_result:
        return result
    return _is_mapped_by_uri(graph_store)


def _is_mapped_by_uri(graph_store) -> dict:
    """Extract is_mapped per class URI from whichever projection path ran.

    Ingest batches into ``store_classes_batch(classes=[{class_uri, is_mapped,
    ...}])`` when the backend supports it (real NDB store + MagicMock, which
    auto-provides the attr), and falls back to per-class ``store_class`` calls
    otherwise. Read from whichever was invoked so the assertion is independent
    of the batching optimization.
    """
    if graph_store.store_classes_batch.call_args_list:
        out: dict = {}
        for call in graph_store.store_classes_batch.call_args_list:
            for c in call.kwargs["classes"]:
                out[c["class_uri"]] = c.get("is_mapped")
        return out
    return {c.kwargs["class_uri"]: c.kwargs.get("is_mapped") for c in graph_store.store_class.call_args_list}


class TestIngestIsMappedTagging:
    def test_mapped_class_tagged_true_unmapped_false(self):
        by_uri = _ingest_with_r2rml(_R2RML_TTL)
        assert by_uri["http://example.org/o#Orders"] is True
        assert by_uri["http://example.org/o#DocConcept"] is False

    def test_no_r2rml_all_classes_unmapped(self):
        # Unstructured proposal path: no R2RML → nothing is mapped.
        by_uri = _ingest_with_r2rml(None)
        assert by_uri["http://example.org/o#Orders"] is False
        assert by_uri["http://example.org/o#DocConcept"] is False

    def test_per_item_fallback_when_backend_lacks_batch(self):
        # A bare MagicMock auto-provides store_classes_batch, so the other tests
        # only ever exercise the batch path. A backend WITHOUT the batch methods
        # (e.g. Neptune Analytics) must take the per-item store_class fallback —
        # spec= restricts the mock's attributes so hasattr(store, batch) is False.
        graph_store = MagicMock(spec=NeptuneAnalyticsGraphStore)
        vector_store = MagicMock()
        assert not hasattr(graph_store, "store_classes_batch")  # gate will pick fallback
        with (
            patch("coa_ontology.catalog.ingest.dynamo_store") as ds,
            patch(
                "coa_ontology.catalog.ingest._accumulate_embeddings",
                return_value={"status": "ok", "count": 0, "model_id": "test", "index": "test"},
            ),
        ):
            ds.get_ontology_registry.return_value = None
            ds.put_namespace_meta.return_value = None
            ingest_ontology(
                graph_store,
                vector_store,
                _ONTOLOGY_TTL,
                namespace="ns-test",
                metadata=IngestMetadata(ontology_id="http://example.org/o#", format="turtle", source="unit-test"),
                validate=False,
                allow_append=True,
                r2rml_content=_R2RML_TTL,
            )
        # Fallback path ran: per-item store_class. The batch method can't have
        # been called — the spec'd mock doesn't expose it (asserted above), which
        # is also why we can't reference `.store_classes_batch` here to assert on.
        assert graph_store.store_class.call_count >= 2
        by_uri = {c.kwargs["class_uri"]: c.kwargs.get("is_mapped") for c in graph_store.store_class.call_args_list}
        assert by_uri["http://example.org/o#Orders"] is True
        assert by_uri["http://example.org/o#DocConcept"] is False


# An ontology payload that FORGES the marker: ex:DocConcept has no R2RML
# TriplesMap (genuinely unmapped) yet asserts coa:isMapped true itself. This is
# the injection the strip must neutralize.
_FORGED_ONTOLOGY_TTL = """
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix coa:  <http://coa.amazon.com/vocab/coa#> .
@prefix ex:   <http://example.org/o#> .

<http://example.org/o#> a owl:Ontology .
ex:Orders     a owl:Class ; rdfs:label "Orders" .
ex:DocConcept a owl:Class ; rdfs:label "DocConcept" ;
    coa:isMapped "true"^^<http://www.w3.org/2001/XMLSchema#boolean> .
"""


def _ingest_capture_turtle(ontology_ttl, r2rml_content):
    """Ingest and return (is_mapped-by-uri, turtle-string-passed-to-load_turtle)."""
    graph_store, vector_store = MagicMock(), MagicMock()
    with (
        patch("coa_ontology.catalog.ingest.dynamo_store") as ds,
        patch(
            "coa_ontology.catalog.ingest._accumulate_embeddings",
            return_value={"status": "ok", "count": 0, "model_id": "test", "index": "test"},
        ),
    ):
        ds.get_ontology_registry.return_value = None
        ds.put_namespace_meta.return_value = None
        ingest_ontology(
            graph_store,
            vector_store,
            ontology_ttl,
            namespace="ns-test",
            metadata=IngestMetadata(ontology_id="http://example.org/o#", format="turtle", source="unit-test"),
            validate=False,
            allow_append=True,
            r2rml_content=r2rml_content,
        )
    by_uri = _is_mapped_by_uri(graph_store)
    turtle = graph_store.load_turtle.call_args.kwargs["turtle"]
    return by_uri, turtle


class TestIngestStripsForgedIsMapped:
    """coa:isMapped is a server-derived trust boundary: a caller-supplied marker
    in the ontology payload must NOT survive to the graph, or an unmapped/unbacked
    class could force itself into the Tier-2 structured prompt."""

    def test_forged_marker_stripped_from_loaded_turtle(self):
        _, turtle = _ingest_capture_turtle(_FORGED_ONTOLOGY_TTL, _R2RML_TTL)
        # The raw Turtle bulk-loaded into Neptune must carry NO isMapped triple —
        # store_class is the only permitted emitter.
        assert "isMapped" not in turtle

    def test_forged_marker_does_not_flip_store_class(self):
        by_uri, _ = _ingest_capture_turtle(_FORGED_ONTOLOGY_TTL, _R2RML_TTL)
        # DocConcept forged the marker but has no TriplesMap → still stored unmapped;
        # Orders is mapped via R2RML as normal (proves the strip didn't over-reach).
        assert by_uri["http://example.org/o#DocConcept"] is False
        assert by_uri["http://example.org/o#Orders"] is True


class TestIngestMappedClassCount:
    """Observability: ingest reports how many classes are Tier-2-mapped, the
    signal that tells operators whether a namespace will be dark to Tier-2."""

    def test_result_reports_mapped_class_count(self):
        result = _ingest_with_r2rml(_R2RML_TTL, return_result=True)
        # Orders is mapped (has a TriplesMap), DocConcept is not → exactly 1.
        assert result["mapped_class_count"] == 1
        assert result["class_count"] >= 2  # both classes ingested

    def test_no_r2rml_reports_zero_mapped(self):
        # Unstructured path: 0 mapped classes — the dark-to-Tier-2 signal.
        result = _ingest_with_r2rml(None, return_result=True)
        assert result["mapped_class_count"] == 0


class TestProjectionFailureRollback:
    """A class/property projection that fails partway must roll the ontology
    back on a FRESH ingest (Neptune DB has no multi-statement transaction, so a
    batch that fails on a later chunk leaves earlier writes committed). On an
    APPEND the pre-existing state is live and must NOT be wiped.
    """

    def _run(self, *, existing_row):
        from coa_ontology.catalog.ingest import IngestStoreError, ingest_ontology

        graph_store, vector_store = MagicMock(), MagicMock()
        # Bare MagicMock auto-provides store_classes_batch → the batch path runs.
        graph_store.store_classes_batch.side_effect = RuntimeError("neptune 400 on chunk 2")
        with (
            patch("coa_ontology.catalog.ingest.dynamo_store") as ds,
            patch(
                "coa_ontology.catalog.ingest._accumulate_embeddings",
                return_value={"status": "ok", "count": 0, "model_id": "t", "index": "t"},
            ),
        ):
            # get_ontology_registry is the fresh-vs-append signal: a row + allow_append
            # → appending=True; None → fresh ingest.
            ds.get_ontology_registry.return_value = existing_row
            ds.put_namespace_meta.return_value = None
            with pytest.raises(IngestStoreError, match="class projection failed"):
                ingest_ontology(
                    graph_store,
                    vector_store,
                    _ONTOLOGY_TTL,
                    namespace="ns-test",
                    metadata=IngestMetadata(ontology_id="http://example.org/o#", format="turtle", source="unit-test"),
                    validate=False,
                    allow_append=True,
                    r2rml_content=None,
                )
        return graph_store

    def test_fresh_ingest_rolls_back_on_projection_failure(self):
        # No existing row → fresh ingest → the failed projection must trigger a
        # delete_ontology cleanup so no partial class set survives.
        graph_store = self._run(existing_row=None)
        graph_store.delete_ontology.assert_called_once_with("http://example.org/o#")

    def test_append_does_not_roll_back_on_projection_failure(self):
        # A real pre-existing row + allow_append → appending=True → the live
        # ontology must NOT be wiped on a transient projection failure.
        graph_store = self._run(existing_row={"uri": "http://example.org/o#", "status": "active"})
        graph_store.delete_ontology.assert_not_called()


# Orders is mapped, with a PredicateObjectMap for status only: its "loyalty note" attribute has no column
# behind it. DocConcept is unmapped, so its attribute is no structured term at all.
_PROPERTY_ONTOLOGY_TTL = (
    _ONTOLOGY_TTL
    + """
ex:status a owl:DatatypeProperty ; rdfs:label "status" ; rdfs:domain ex:Orders .
ex:loyaltyNote a owl:DatatypeProperty ; rdfs:label "loyalty note" ; rdfs:domain ex:Orders .
ex:mentions a owl:DatatypeProperty ; rdfs:label "mentions" ; rdfs:domain ex:DocConcept .
"""
)
_PROPERTY_R2RML_TTL = """
@prefix rr:  <http://www.w3.org/ns/r2rml#> .
@prefix coa: <http://coa.amazon.com/vocab/coa#> .
@prefix ex:  <http://example.org/o#> .

ex:TriplesMap_Orders a rr:TriplesMap ;
    coa:datasourceId "ds-orders-1" ;
    rr:logicalTable [ rr:tableName "\\"orders\\"" ] ;
    rr:subjectMap [ rr:class ex:Orders ] ;
    rr:predicateObjectMap [ rr:predicate ex:status ; rr:objectMap [ rr:column "\\"status\\"" ] ] .
"""


def _ingest_properties(graph_store, r2rml_content):
    """Ingest the property ontology into ``graph_store``; return the mocked embedding step."""
    with (
        patch("coa_ontology.catalog.ingest.dynamo_store") as ds,
        patch(
            "coa_ontology.catalog.ingest._accumulate_embeddings",
            return_value={"status": "ok", "count": 0, "model_id": "test", "index": "test"},
        ) as embeddings,
    ):
        ds.get_ontology_registry.return_value = None
        ds.put_namespace_meta.return_value = None
        ingest_ontology(
            graph_store,
            MagicMock(),
            _PROPERTY_ONTOLOGY_TTL,
            namespace="ns-test",
            metadata=IngestMetadata(ontology_id="http://example.org/o#", format="turtle", source="unit-test"),
            validate=False,
            allow_append=True,
            r2rml_content=r2rml_content,
        )
    return embeddings


def _unmapped_by_uri(graph_store) -> dict:
    """The ``unmapped`` flag per property URI, from whichever projection path ran."""
    if hasattr(graph_store, "store_properties_batch") and graph_store.store_properties_batch.call_args_list:
        return {
            p["prop_uri"]: p.get("unmapped")
            for call in graph_store.store_properties_batch.call_args_list
            for p in call.kwargs["properties"]
        }
    return {c.kwargs["prop_uri"]: c.kwargs.get("unmapped") for c in graph_store.store_property.call_args_list}


class TestIngestPropertyMarker:
    """A property of a mapped class with no PredicateObjectMap is projected ``unmapped`` (coa:isMapped false)."""

    def test_only_the_unmapped_property_of_a_mapped_class_is_flagged(self):
        graph_store = MagicMock()
        _ingest_properties(graph_store, _PROPERTY_R2RML_TTL)
        assert _unmapped_by_uri(graph_store) == {
            "http://example.org/o#status": False,
            "http://example.org/o#loyaltyNote": True,
            "http://example.org/o#mentions": False,
        }

    def test_per_item_fallback_passes_the_flag(self):
        # autospec, unlike spec=, checks each call against the store's signature.
        graph_store = create_autospec(NeptuneAnalyticsGraphStore, instance=True)
        _ingest_properties(graph_store, _PROPERTY_R2RML_TTL)
        assert _unmapped_by_uri(graph_store)["http://example.org/o#loyaltyNote"] is True

    def test_no_r2rml_flags_no_property(self):
        graph_store = MagicMock()
        _ingest_properties(graph_store, None)
        assert not any(_unmapped_by_uri(graph_store).values())

    def test_the_class_documents_get_the_mappings_physical_names(self):
        embeddings = _ingest_properties(MagicMock(), _PROPERTY_R2RML_TTL)
        names = embeddings.call_args.kwargs["physical_names"]
        assert names.tables == {"http://example.org/o#Orders": {"orders"}}
        assert names.columns == {"http://example.org/o#Orders": {"http://example.org/o#status": "status"}}

    def test_the_mapping_is_parsed_once(self, monkeypatch):
        from rdflib import Graph

        parsed: list[str] = []
        real_parse = Graph.parse

        def parse(self, *args, **kwargs):
            parsed.append(kwargs.get("data"))
            return real_parse(self, *args, **kwargs)

        monkeypatch.setattr(Graph, "parse", parse)
        graph_store = MagicMock()
        _ingest_properties(graph_store, _PROPERTY_R2RML_TTL)
        assert parsed.count(_PROPERTY_R2RML_TTL) == 1
        assert _unmapped_by_uri(graph_store)["http://example.org/o#loyaltyNote"] is True


def _physical_names_by_uri(graph_store) -> tuple[dict, dict]:
    """The ``physical_name`` of every class and property spec, from whichever projection path ran."""
    if hasattr(graph_store, "store_classes_batch") and graph_store.store_classes_batch.call_args_list:
        classes = [c for call in graph_store.store_classes_batch.call_args_list for c in call.kwargs["classes"]]
        props = [p for call in graph_store.store_properties_batch.call_args_list for p in call.kwargs["properties"]]
    else:
        classes = [c.kwargs for c in graph_store.store_class.call_args_list]
        props = [c.kwargs for c in graph_store.store_property.call_args_list]
    return (
        {c["class_uri"]: c.get("physical_name") for c in classes},
        {p["prop_uri"]: p.get("physical_name") for p in props},
    )


class TestIngestPhysicalNames:
    """The legacy ingest projects ``coa:physicalName`` from the R2RML as deploy does, and only from it."""

    # The Neptune Analytics store has no batch methods, so ingest calls its store_class and
    # store_property with every projected key; autospec fails the test if one is not a parameter.
    @pytest.mark.parametrize(
        "graph_store", [MagicMock, lambda: create_autospec(NeptuneAnalyticsGraphStore, instance=True)]
    )
    def test_a_mapped_class_and_its_mapped_attribute_get_their_table_and_column(self, graph_store):
        store = graph_store()
        _ingest_properties(store, _PROPERTY_R2RML_TTL)
        classes, props = _physical_names_by_uri(store)
        assert classes == {"http://example.org/o#Orders": "orders", "http://example.org/o#DocConcept": ""}
        assert props == {
            "http://example.org/o#status": "status",
            "http://example.org/o#loyaltyNote": "",
            "http://example.org/o#mentions": "",
        }

    def test_without_r2rml_no_term_gets_a_name(self):
        store = MagicMock()
        _ingest_properties(store, None)
        classes, props = _physical_names_by_uri(store)
        assert not any(classes.values()) and not any(props.values())

    def test_a_caller_supplied_physical_name_is_stripped(self):
        forged = _ONTOLOGY_TTL.replace(
            'ex:DocConcept a owl:Class ; rdfs:label "DocConcept" .',
            f'ex:DocConcept a owl:Class ; rdfs:label "DocConcept" ; <{VOCAB_URI}physicalName> "secret_table" .',
        )
        assert "secret_table" in forged
        _, turtle = _ingest_capture_turtle(forged, _R2RML_TTL)
        assert "physicalName" not in turtle and "secret_table" not in turtle

    def test_a_caller_supplied_mapping_placement_is_stripped(self):
        # Where a mapping puts a property is derived like the names: an upload cannot move a column for serve.
        forged = _ONTOLOGY_TTL.replace(
            'ex:DocConcept a owl:Class ; rdfs:label "DocConcept" .',
            f'ex:DocConcept a owl:Class ; rdfs:label "DocConcept" ; <{VOCAB_URI}mappedDomain> ex:DocConcept ; '
            f"<{VOCAB_URI}mappedRange> ex:DocConcept .",
        )
        assert "mappedDomain" in forged
        _, turtle = _ingest_capture_turtle(forged, _R2RML_TTL)
        assert "mappedDomain" not in turtle and "mappedRange" not in turtle
