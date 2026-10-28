# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A small structured draft shared by the Ontology designer tests."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import httpx
from coa_ontology.studio import iris, queries
from coa_ontology.studio.ops import Draft
from coa_ontology.versions import snapshot

if TYPE_CHECKING:
    from coa_ontology.studio.draft import StudioState

NS = "11111111-2222-3333-4444-555555555555"
ONTO = f"http://coa.amazon.com/namespace/{NS}/ontology/induced#"
FIBO = "https://spec.edmcouncil.org/fibo/ontology/FND/Agents/Agent"
SCHEME = iris.scheme_for(NS, ONTO, "STRUCTURED")
PARTY, CUSTOMER, VIP, ORDER, ITEM = (f"{ONTO}{n}" for n in ("Party", "Customer", "VipCustomer", "Order", "Item"))
PLACED_BY, EMAIL, TOTAL = f"{ONTO}order_placedBy", f"{ONTO}customer_email", f"{ONTO}order_total"

TBOX = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<{ONTO[:-1]}> a owl:Ontology .
<{PARTY}> a owl:Class ; rdfs:label "Party" .
<{CUSTOMER}> a owl:Class ; rdfs:label "Customer" ; rdfs:comment "A buyer" ;
    rdfs:subClassOf <{PARTY}>, <{FIBO}> ; skos:exactMatch <{FIBO}> ; <{ONTO}matchConfidence> "0.9"^^xsd:float .
<{VIP}> a owl:Class ; rdfs:label "VIP customer" ; rdfs:subClassOf <{CUSTOMER}> .
<{ORDER}> a owl:Class ; rdfs:label "Order" ;
    rdfs:subClassOf [ a owl:Restriction ; owl:onProperty <{PLACED_BY}> ; owl:someValuesFrom <{CUSTOMER}> ] .
<{ITEM}> a owl:Class ; rdfs:label "Item" .
<{PLACED_BY}> a owl:ObjectProperty ; rdfs:label "placed by" ; rdfs:domain <{ORDER}> ; rdfs:range <{CUSTOMER}> .
<{EMAIL}> a owl:DatatypeProperty ; rdfs:label "email" ; rdfs:domain <{CUSTOMER}> ; rdfs:range xsd:string .
<{TOTAL}> a owl:DatatypeProperty ; rdfs:label "total" ; rdfs:domain <{ORDER}> ; rdfs:range xsd:decimal .
"""
R2RML = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<{ONTO}TriplesMap_Customer> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "customer" ] ;
    rr:subjectMap <{ONTO}TriplesMap_Customer/SubjectMap> ;
    rr:predicateObjectMap <{ONTO}TriplesMap_Customer/POM_Email> .
<{ONTO}TriplesMap_Customer/SubjectMap> rr:class <{CUSTOMER}> ; rr:template "c/{{id}}" .
<{ONTO}TriplesMap_Customer/POM_Email> rr:predicate <{EMAIL}> ;
    rr:objectMap <{ONTO}TriplesMap_Customer/POM_Email/ObjectMap> .
<{ONTO}TriplesMap_Customer/POM_Email/ObjectMap> rr:column "email" ; rr:datatype xsd:string .
<{ONTO}TriplesMap_Order> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "orders" ] ;
    rr:subjectMap <{ONTO}TriplesMap_Order/SubjectMap> ;
    rr:predicateObjectMap <{ONTO}TriplesMap_Order/POM_CustomerId>, <{ONTO}TriplesMap_Order/POM_Total> .
<{ONTO}TriplesMap_Order/SubjectMap> rr:class <{ORDER}> ; rr:template "o/{{id}}" .
<{ONTO}TriplesMap_Order/POM_CustomerId> rr:predicate <{PLACED_BY}> ;
    rr:objectMap <{ONTO}TriplesMap_Order/POM_CustomerId/ObjectMap> .
<{ONTO}TriplesMap_Order/POM_CustomerId/ObjectMap> rr:parentTriplesMap <{ONTO}TriplesMap_Customer> ;
    rr:joinCondition [ rr:child "customer_id" ; rr:parent "id" ] .
<{ONTO}TriplesMap_Order/POM_Total> rr:predicate <{TOTAL}> ;
    rr:objectMap <{ONTO}TriplesMap_Order/POM_Total/ObjectMap> .
<{ONTO}TriplesMap_Order/POM_Total/ObjectMap> rr:column "total" ; rr:datatype xsd:decimal .
"""


def draft() -> Draft:
    """A fresh copy of the sample draft."""
    return Draft(snapshot.parse_turtle(TBOX), snapshot.parse_turtle(R2RML))


# ── Moto-backed helpers (use with the ``versions_aws`` fixture) ─────────────

SHIPMENT = f"{ONTO}Shipment"
PROPOSAL_TBOX = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{ONTO[:-1]}> a owl:Ontology .
<{SHIPMENT}> a owl:Class ; rdfs:label "Shipment" .
"""


def publish_base(turtle: str = TBOX, r2rml: str = R2RML, proposal: str = "p-base") -> int:
    """Publish a version the ordinary (union) way and return its number.

    A non-empty ``r2rml`` makes the publish build the source DDL, which needs a source catalog;
    patch ``coa_ontology.proposals._generate_schema_sql`` around the call, as the accept tests do.
    """
    from coa_ontology.versions import publish

    return publish.publish_version(
        publish.PublishRequest(
            namespace=NS,
            ontology_id=ONTO,
            ontology_turtle=turtle,
            r2rml_turtle=r2rml,
            source_proposal_id=proposal,
            created_by="alice",
        )
    )


def seed_proposal(
    proposal_id: str = "p-1",
    turtle: str = PROPOSAL_TBOX,
    r2rml: str = "",
    source_type: str = "STRUCTURED",
    status: str = "pending",
) -> None:
    """Store a proposal row and its S3 Turtle the way induction does."""
    from coa_ontology import dynamo_store

    dynamo_store.create_proposal(
        proposal_id=proposal_id,
        job_id=proposal_id,
        namespace=NS,
        ontology_id=ONTO,
        proposal_type="induction",
        ontology_turtle=turtle,
        r2rml_turtle=r2rml,
        source_type=source_type,
        metadata={"label": "Shipping", "datasource_ids": []},
    )
    if status != "pending":
        dynamo_store.update_proposal(proposal_id, namespace=NS, status=status)


def stored_row(proposal_id: str = "p-1") -> dict[str, Any]:
    """The proposal row as the API reads it (no Turtle), with its ``studio`` field."""
    from coa_ontology import dynamo_store

    row = dynamo_store.get_proposal_by_id(proposal_id, namespace=NS, hydrate_turtle=False)
    assert row is not None
    return row


def stored_state(proposal_id: str = "p-1") -> StudioState:
    """The designer state stored on the proposal row; the proposal must be open in the designer."""
    from coa_ontology.studio import draft

    state = draft.state_of(stored_row(proposal_id))
    assert state is not None
    return state


def opened_draft(proposal_id: str = "p-1", opened_by: str = "alice") -> StudioState:
    """Publish the sample ontology as v1, seed a proposal and open its designer draft at revision 0.

    Needs the ``versions_aws`` fixture and ``coa_ontology.proposals._generate_schema_sql`` patched
    to return None (the sample mapping makes the publish build source DDL).
    """
    from coa_ontology.studio import draft

    publish_base()
    seed_proposal(proposal_id)
    return draft.open_draft(NS, proposal_id, opened_by)


# ── Neptune for the query console ───────────────────────────────────────────


class FakeNeptune:
    """Named graphs as sets of N-Triples lines; queries other than the marker ASK answer ``answer``."""

    def __init__(self) -> None:
        self.graphs: dict[str, set[str]] = {}
        self.posts: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.answer: httpx.Response = httpx.Response(200, json={"head": {"vars": []}, "results": {"bindings": []}})

    def post(self, graph: str, text: str, *, retry_oom: bool = True) -> dict:
        assert retry_oom is False
        self.posts.append(graph)
        self.graphs.setdefault(graph, set()).update(line for line in text.splitlines() if line)
        return {}

    def update(self, update: str, *, retry_oom: bool = True) -> None:
        assert retry_oom is False
        if m := re.fullmatch(r"DROP SILENT GRAPH <([^>]+)>", update):
            self.graphs.pop(m.group(1), None)
        elif m := re.fullmatch(r"INSERT DATA \{ GRAPH <([^>]+)> \{ (.+) \} \}", update):
            self.graphs.setdefault(m.group(1), set()).add(m.group(2) + " .")
        else:  # pragma: no cover - the module sends nothing else
            raise AssertionError(update)

    def query(self, text: str, accept: str) -> httpx.Response:
        if m := re.fullmatch(r'ASK \{ GRAPH <([^>]+)> \{ <[^>]+> <[^>]+> "(\d+)" \} \}', text):
            graph = m.group(1)
            marker = f'<{graph}> <{queries.MARKER}> "{m.group(2)}" .'
            return httpx.Response(200, json={"boolean": marker in self.graphs.get(graph, set())})
        self.sent.append((text, accept))
        return self.answer

    def markers(self, graph: str) -> list[str]:
        return [line for line in self.graphs.get(graph, set()) if queries.MARKER in line]


@contextmanager
def fake_neptune(base: str = "https://ontology-workbench.local") -> Iterator[FakeNeptune]:
    """Patch the query console's Neptune calls with a :class:`FakeNeptune`."""
    fake = FakeNeptune()
    with (
        patch.object(queries.ndb, "_gsp_post_turtle", side_effect=fake.post),
        patch.object(queries.ndb, "_sparql_update", side_effect=fake.update),
        patch.object(queries, "_post_query", side_effect=fake.query),
        patch.object(queries.ndb, "_resolve_graph_base", return_value=base),
    ):
        yield fake
