# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A namespace graph as a deploy writes it, served through the real Neptune client.

:func:`designer_v2_dataset` is v2 of a ``table_to_ontology`` induction of the tables
``customers`` and ``orders`` (the classes ``Customers`` and ``Orders``), deployed after
these edits in the Ontology designer:

- the class ``Customers`` relabelled ``Client`` (its IRI and table are unchanged);
- the class ``Orders`` renamed to ``PurchaseOrder`` (the mapping follows the IRI, so it
  stays mapped);
- the property mapped from ``customers.signup_date`` deleted;
- the new classes ``Premium customer`` (a subclass of ``Customers``), ``Loyalty
  programme`` and ``Customer segment``, which no table maps;
- the new attribute ``loyalty tier`` and the new relationships ``preferred order`` (to
  ``PurchaseOrder``) and ``in segment`` (to ``Customer segment``) on ``Customers``,
  which no column or join maps;
- the new relationship ``member`` from ``Loyalty programme`` to ``Customers``, which no
  join maps either.

Deploy marks the mapped classes ``coa:isMapped true`` and, with ``property_markers``,
the four unmapped properties of or into the mapped class ``coa:isMapped false``. A
namespace deployed before the property marker existed has the same graph without them.
With ``physical_names``, deploy also names the mapped classes and attributes after their
table and column (``coa:physicalName``): ``Customers`` is the table ``customers`` although
it is labelled ``Client``. A namespace deployed before that has no such triple.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from coa_common.constants import GRAPH_BASE_URI, VOCAB_URI
from coa_serve.clients.neptune import NeptuneGraphClient
from rdflib import OWL, RDF, RDFS, XSD, Dataset, Literal, URIRef

NS = "designer-demo"
TEMPLATE = "https://ontology-workbench.local/{namespace}"
ONTOLOGY_ID = f"{GRAPH_BASE_URI}/namespace/{NS}/ontology/induced"
PREFIX = f"{ONTOLOGY_ID}#"
GRAPH = f"https://ontology-workbench.local/{NS}/{quote(ONTOLOGY_ID, safe='')}"

IS_MAPPED = URIRef(f"{VOCAB_URI}isMapped")
PHYSICAL_NAME = URIRef(f"{VOCAB_URI}physicalName")
# Where the mapping puts a property whose domain or range says otherwise (deploy writes them then).
MAPPED_DOMAIN = URIRef(f"{VOCAB_URI}mappedDomain")
MAPPED_RANGE = URIRef(f"{VOCAB_URI}mappedRange")
_WB = "https://ontology-workbench.local/vocab#"

CUSTOMERS = PREFIX + "Customers"
PURCHASE_ORDER = PREFIX + "PurchaseOrder"
CUSTOMER_SEGMENT = PREFIX + "CustomerSegment"
LOYALTY_PROGRAMME = PREFIX + "LoyaltyProgramme"
LOYALTY_TIER = PREFIX + "customers_loyaltyTier"
PREFERRED_ORDER = PREFIX + "customers_preferredOrder"
IN_SEGMENT = PREFIX + "customers_inSegment"
MEMBER = PREFIX + "loyaltyProgramme_member"


def designer_v2_dataset(
    *, property_markers: bool = True, physical_names: bool = True, version: str | None = "v2"
) -> Dataset:
    """Return the namespace's graph after the deploy described in the module docstring.

    Args:
        property_markers: Whether deploy marked the unmapped properties of the mapped
            class ``coa:isMapped false``.
        physical_names: Whether deploy named the mapped classes and attributes after
            their table and column (``coa:physicalName``).
        version: The ``owl:versionInfo`` deploy stamped, or ``None`` for a namespace
            accepted before versions existed.
    """
    ds = Dataset()
    g = ds.graph(URIRef(GRAPH))
    ontology = URIRef(ONTOLOGY_ID)
    g.add((ontology, RDF.type, OWL.Ontology))
    g.add((ontology, URIRef(f"{_WB}ontology_type"), Literal("induced")))
    if version:
        g.add((ontology, OWL.versionInfo, Literal(version)))

    def named(iri: URIRef, physical: str | None) -> None:
        if physical and physical_names:
            g.add((iri, PHYSICAL_NAME, Literal(physical)))

    def cls(name: str, label: str, *, table: str | None = None, parent: str | None = None) -> None:
        iri = URIRef(PREFIX + name)
        g.add((iri, RDF.type, OWL.Class))
        g.add((iri, RDFS.label, Literal(label)))
        if table:
            g.add((iri, IS_MAPPED, Literal(True)))
        named(iri, table)
        if parent:
            g.add((iri, RDFS.subClassOf, URIRef(parent)))

    def prop(
        name: str,
        kind: URIRef,
        domain: str,
        rng: URIRef | str,
        label: str,
        *,
        unmapped: bool = False,
        column: str | None = None,
    ) -> URIRef:
        iri = URIRef(PREFIX + name)
        g.add((iri, RDF.type, kind))
        g.add((iri, RDFS.domain, URIRef(domain)))
        g.add((iri, RDFS.range, URIRef(rng)))
        g.add((iri, RDFS.label, Literal(label)))
        if unmapped and property_markers:
            g.add((iri, IS_MAPPED, Literal(False)))
        named(iri, column)
        return iri

    cls("Customers", "Client", table="customers")
    cls("PurchaseOrder", "orders", table="orders")
    cls("PremiumCustomer", "Premium customer", parent=CUSTOMERS)
    cls("LoyaltyProgramme", "Loyalty programme")
    cls("CustomerSegment", "Customer segment")

    data, link = OWL.DatatypeProperty, OWL.ObjectProperty
    prop("customers_id", data, CUSTOMERS, XSD.integer, "id", column="id")
    prop("customers_name", data, CUSTOMERS, XSD.string, "name", column="name")
    prop("customers_email", data, CUSTOMERS, XSD.string, "email", column="email")
    prop("customers_loyaltyTier", data, CUSTOMERS, XSD.string, "loyalty tier", unmapped=True)
    prop("orders_id", data, PURCHASE_ORDER, XSD.integer, "id", column="id")
    status = prop("orders_status", data, PURCHASE_ORDER, XSD.string, "status", column="status")
    g.add((status, RDFS.comment, Literal("Fulfilment state of the order")))
    for value in ("cancelled", "completed", "pending"):
        g.add((status, URIRef(f"{VOCAB_URI}distinctValues"), Literal(value)))
    prop("orders_totalAmount", data, PURCHASE_ORDER, XSD.double, "total_amount", column="total_amount")
    fk = prop("orders_customerId", link, PURCHASE_ORDER, CUSTOMERS, "customer_id")
    g.add((fk, RDFS.comment, Literal("Foreign key: orders.customer_id references customers.id")))
    prop("customers_preferredOrder", link, CUSTOMERS, PURCHASE_ORDER, "preferred order", unmapped=True)
    prop("customers_inSegment", link, CUSTOMERS, CUSTOMER_SEGMENT, "in segment", unmapped=True)
    prop("loyaltyProgramme_member", link, LOYALTY_PROGRAMME, CUSTOMERS, "member", unmapped=True)
    return ds


def neptune_over(ds: Dataset) -> NeptuneGraphClient:
    """The real client, with its write guard and LIMIT handling, whose HTTP call rdflib answers."""
    client = NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")

    async def sparql_post(sparql: str):
        # Neptune consumes the hint triple; rdflib would read it as a pattern nothing matches.
        without_hint = re.sub(r"hint:Query hint:queryTimeout \d+ \. ", "", sparql)
        return json.loads(ds.query(without_hint).serialize(format="json"))

    client._sparql_post = sparql_post  # type: ignore[method-assign]
    return client
