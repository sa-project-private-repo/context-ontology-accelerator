# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A designer draft, published and deployed: what Neptune and the NL-to-SQL documents then serve to Tier 2.

Real Ontology designer ops (``studio.ops``) edit the sample draft, the draft is published as the next
version the way an accept publishes it (REPLACE onto its base), and the deployment runs every step with
the real Neptune store over an in-memory Neptune, as ``test_versions_deploy.TestRealStore`` does. Tier 2
reads the class markers, the property markers and the class documents this leaves behind.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from coa_common.constants import VOCAB_URI
from coa_ontology.catalog import ingest
from coa_ontology.stores import neptune_db_graph
from coa_ontology.studio import ops
from coa_ontology.versions import deploy, publish, registry, snapshot, storage
from rdflib import RDFS, Literal, URIRef

from . import studio_fakes as sf
from .test_versions_deploy import _environment, _Neptune

pytestmark = [
    pytest.mark.unit,
    # rdflib's own SPARQL engine calls its deprecated Dataset accessors when the fake Neptune runs a request.
    pytest.mark.filterwarnings(r"ignore:Dataset\.\w+ is deprecated:DeprecationWarning"),
]

IS_MAPPED = URIRef(VOCAB_URI + "isMapped")
PHYSICAL_NAME = URIRef(VOCAB_URI + "physicalName")
MAPPED_ENDS = (URIRef(VOCAB_URI + "mappedDomain"), URIRef(VOCAB_URI + "mappedRange"))
PURCHASE_ORDER = f"{sf.ONTO}PurchaseOrder"
# The sample draft labelled the way table_to_ontology labels classes (after their tables), with the
# datasource id every inducer writes on its maps.
TBOX = sf.TBOX.replace('rdfs:label "Customer"', 'rdfs:label "customer"').replace(
    'rdfs:label "Order"', 'rdfs:label "orders"'
)
R2RML = sf.R2RML.replace("a rr:TriplesMap ;", f'a rr:TriplesMap ; <{VOCAB_URI}datasourceId> "ds-1" ;')
EDITS = [
    {"op": "add_class", "label": "Premium customer", "parents": [sf.CUSTOMER]},
    {"op": "add_class", "label": "Loyalty programme"},
    {"op": "add_datatype_property", "label": "loyalty tier", "domain": sf.CUSTOMER, "range": "xsd:string"},
    {"op": "add_object_property", "label": "preferred order", "domain": sf.CUSTOMER, "range": sf.ORDER},
    # From the new class, which no table maps, into the mapped Customer: no join behind it either.
    {"op": "add_object_property", "label": "member", "domain": f"{sf.ONTO}LoyaltyProgramme", "range": sf.CUSTOMER},
    {"op": "set_label", "iri": sf.CUSTOMER, "label": "Client"},
    {"op": "set_label", "iri": sf.EMAIL, "label": "E-mail address"},
    {"op": "change_iri", "iri": sf.ORDER, "newIri": PURCHASE_ORDER},
    {"op": "delete_property", "iri": sf.TOTAL},
]
# A table_to_ontology induction (ontology and mapping) of a schema with every naming path the inducer has.
GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "structured_ascii_golden"


@dataclass
class _Served:
    neptune: _Neptune
    vec: MagicMock  # the vector store each deployment hands the documents to

    def markers(self, namespace: str, ontology_id: str, value: bool) -> set[str]:
        """The subjects of ``coa:isMapped value`` in the namespace's domain graph."""
        domain = self.neptune.ds.graph(URIRef(neptune_db_graph._ontology_graph_uri(ontology_id, namespace)))
        return {str(s) for s in domain.subjects(IS_MAPPED, Literal(value))}

    def physical_names(self, namespace: str, ontology_id: str) -> dict[str, str]:
        """The ``coa:physicalName`` of every term that has one in the namespace's domain graph."""
        domain = self.neptune.ds.graph(URIRef(neptune_db_graph._ontology_graph_uri(ontology_id, namespace)))
        return {str(s): str(o) for s, o in domain.subject_objects(PHYSICAL_NAME)}

    def mapped_ends(self, namespace: str, ontology_id: str) -> set[tuple[str, str, str]]:
        """Every ``coa:mappedDomain`` / ``coa:mappedRange`` triple in the domain graph, with the marker's local name."""
        domain = self.neptune.ds.graph(URIRef(neptune_db_graph._ontology_graph_uri(ontology_id, namespace)))
        return {(str(s), str(p).rsplit("#", 1)[-1], str(o)) for p in MAPPED_ENDS for s, o in domain.subject_objects(p)}

    def deploy(self, namespace: str, version: int) -> dict[str, dict[str, Any]]:
        """Deploy ``version``; return the class documents the deployment wrote, by IRI."""
        batches = self.vec.store_embeddings_batch
        seen = batches.call_count
        row = registry.get_deployment(namespace, deploy.start_deployment(namespace, version, requested_by="bob"))
        assert row["status"] == registry.DEPLOYED, row.get("error")
        written = [item for call in batches.call_args_list[seen:] for item in call.args[0]]
        return {d["entity_uri"]: d for d in written if d["entity_type"] == "class"}


@pytest.fixture
def served(versions_aws, monkeypatch) -> Iterator[_Served]:
    """Deployments run inline, through the real Neptune store, into an in-memory Neptune."""
    with _environment(versions_aws, monkeypatch, real_store=True) as env:
        yield _Served(env["neptune"], env["vec"])


def _context(doc: dict[str, Any]) -> str:
    """The text the NL-to-SQL writer is given (stored only when it differs from the embedded one)."""
    return doc.get("context_text", doc["text"])


def _publish_edits(base: int, edits: list[dict[str, Any]]) -> tuple[int, ops.Context]:
    """Apply ``edits`` to a draft of ``base`` and publish it as an accept does: REPLACE onto ``base``."""
    draft = ops.Draft(
        snapshot.parse_turtle(storage.require_version_file(sf.NS, base, "ontology.ttl")),
        snapshot.parse_turtle(storage.require_version_file(sf.NS, base, "mappings.r2rml")),
    )
    draft, ctx = ops.apply_ops(draft, ops.USER_OPS.validate_python(edits), sf.SCHEME)
    version = publish.publish_version(
        publish.PublishRequest(
            namespace=sf.NS,
            ontology_id=sf.ONTO,
            ontology_turtle=snapshot.canonical_nt(draft.tbox),
            r2rml_turtle=snapshot.canonical_nt(draft.r2rml),
            source_proposal_id="p-designer",
            created_by="alice",
            mode=publish.REPLACE,
            expected_parent_version=base,
        )
    )
    return version, ctx


class TestDesignerEditsAfterDeploy:
    @pytest.fixture
    def deployed(self, served) -> dict[str, Any]:
        """Deploy the sample as v1, then the edited draft as v2; return v2's documents and the new IRIs."""
        v1 = sf.publish_base(TBOX, R2RML)
        served.deploy(sf.NS, v1)
        v2, ctx = _publish_edits(v1, EDITS)
        premium, loyalty, tier, prefers, member = ctx.created
        documents = served.deploy(sf.NS, v2)
        return {
            "documents": documents,
            "premium": premium,
            "loyalty": loyalty,
            "tier": tier,
            "prefers": prefers,
            "member": member,
        }

    def test_the_renamed_class_stays_mapped_and_the_new_classes_are_not(self, served, deployed):
        assert served.markers(sf.NS, sf.ONTO, True) == {sf.CUSTOMER, PURCHASE_ORDER}
        domain = served.neptune.triples(neptune_db_graph._ontology_graph_uri(sf.ONTO, sf.NS))
        assert not any(sf.ORDER in (str(s), str(o)) for s, _, o in domain)
        for new_class in (deployed["premium"], deployed["loyalty"]):
            assert not any(str(s) == new_class and p == IS_MAPPED for s, p, _ in domain)

    def test_only_the_new_attribute_and_relationships_carry_the_false_marker(self, served, deployed):
        # "member" leaves an unmapped class, but serve's foreign-key walk would follow it back from Customer.
        assert served.markers(sf.NS, sf.ONTO, False) == {deployed["tier"], deployed["prefers"], deployed["member"]}

    def test_the_relabelled_class_and_attribute_keep_their_table_and_column_names(self, served, deployed):
        # Relabelled Client and E-mail address in the designer; the order class was renamed and its total deleted.
        assert served.physical_names(sf.NS, sf.ONTO) == {
            sf.CUSTOMER: "customer",
            PURCHASE_ORDER: "orders",
            sf.EMAIL: "email",
        }

    def test_only_the_mapped_classes_route_to_the_datasource(self, deployed):
        routed = {uri: doc["data_source_id"] for uri, doc in deployed["documents"].items() if "data_source_id" in doc}
        assert routed == {sf.CUSTOMER: "ds-1", PURCHASE_ORDER: "ds-1"}

    def test_the_documents_keep_the_physical_names_and_leave_the_new_terms_out(self, deployed):
        documents = deployed["documents"]
        customer = documents[sf.CUSTOMER]
        assert customer["text"] == "Table: customer | Description: A buyer | Synonyms: Client | Columns: email"
        assert _context(customer) == (
            "Table: customer | Description: A buyer | Synonyms: Client | Columns: email:string [label: E-mail address]"
        )
        # The relationship to the relabelled class names its table; the deleted attribute is gone with its column.
        order = documents[PURCHASE_ORDER]
        assert order["text"] == _context(order) == "Table: orders | Relationships: placed by->customer"


class TestPropertiesMovedUpToAParent:
    """A domain or range moved up to a parent (allowed, with a warning) leaves NL-to-SQL where the mapping is."""

    @pytest.fixture
    def deployed(self, served) -> dict[str, dict[str, Any]]:
        """Deploy the sample as v1, then v2 with email's domain and placed by's range moved up to Party."""
        v1 = sf.publish_base(TBOX, R2RML)
        served.deploy(sf.NS, v1)
        v2, ctx = _publish_edits(
            v1,
            [
                {"op": "update_property", "iri": sf.EMAIL, "domain": sf.PARTY},
                {"op": "update_property", "iri": sf.PLACED_BY, "range": sf.PARTY},
            ],
        )
        assert len(ctx.warnings) == 2  # one per edit: Party has no table of its own
        return served.deploy(sf.NS, v2)

    def test_deploy_records_the_class_the_mapping_reads_and_the_one_its_join_reaches(self, served, deployed):
        assert served.mapped_ends(sf.NS, sf.ONTO) == {
            (sf.EMAIL, "mappedDomain", sf.CUSTOMER),
            (sf.PLACED_BY, "mappedRange", sf.CUSTOMER),
        }
        assert served.markers(sf.NS, sf.ONTO, False) == set()  # both are still mapped

    def test_the_documents_keep_the_column_and_the_join_where_the_mapping_reads_them(self, deployed):
        assert _context(deployed[sf.CUSTOMER]) == "Table: customer | Description: A buyer | Columns: email:string"
        assert _context(deployed[sf.ORDER]) == (
            "Table: orders | Relationships: placed by->customer | Columns: total:decimal"
        )


def test_an_unedited_table_to_ontology_induction_keeps_its_documents_and_gets_no_property_marker(served):
    namespace, ontology_id = "ns-induced", "http://example.org/base"
    r2rml = (GOLDEN / "r2rml.nt").read_text(encoding="utf-8")
    version = publish.publish_version(
        publish.PublishRequest(
            namespace=namespace,
            ontology_id=ontology_id,
            ontology_turtle=(GOLDEN / "ontology.nt").read_text(encoding="utf-8"),
            r2rml_turtle=r2rml,
            source_proposal_id="p-induced",
            created_by="alice",
        )
    )
    deployed = served.deploy(namespace, version)

    # The documents built from the TBox alone, as they were before the mapping's names were read.
    graph = snapshot.parse_turtle(storage.require_version_file(namespace, version, "ontology.ttl"))
    classes, obj_props, dt_props = ingest.schema_subjects(graph)
    tbox_only = MagicMock()
    tbox_only.store_embeddings_batch.side_effect = lambda items: list(items)
    ingest._accumulate_embeddings(
        vector_store=tbox_only,
        graph=graph,
        ontology_id=ontology_id,
        classes=classes,
        obj_props=obj_props,
        dt_props=dt_props,
        namespace=namespace,
        class_to_datasource=ingest._class_datasource_map_from_r2rml(r2rml),
    )
    expected = {
        d["entity_uri"]: (d["text"], _context(d), d.get("data_source_id"))
        for d in tbox_only.store_embeddings_batch.call_args.args[0]
        if d["entity_type"] == "class"
    }
    assert {uri: (d["text"], _context(d), d.get("data_source_id")) for uri, d in deployed.items()} == expected
    assert _context(deployed["http://example.org/base/Orders"]) == (
        "Table: orders | Primary key: order_id | Relationships: customer_id->customers "
        "(Foreign key: orders.customer_id references customers.customer_id) | "
        "Columns: amount:double, ORDER DATE:dateTime, order_id:long"
    )
    assert served.markers(namespace, ontology_id, False) == set()
    assert served.markers(namespace, ontology_id, True) == {str(c) for c in classes}
    assert served.mapped_ends(namespace, ontology_id) == set()  # every mapped term is where its domain says
    # Every class and mapped attribute is named after its table or column, which is its label here, so the
    # foreign-key walk names the same tables and columns it did before deploy wrote the names.
    mapped = ingest._mapped_predicates_from_r2rml(r2rml)
    named = {str(c) for c in classes} | {str(p) for p in dt_props if str(p) in mapped}
    assert served.physical_names(namespace, ontology_id) == {
        str(term): str(graph.value(URIRef(term), RDFS.label)) for term in named
    }
