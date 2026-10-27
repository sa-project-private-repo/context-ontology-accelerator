# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Ontop T-Box prompt after a designer edit is deployed, read through the real Neptune client.

The other T-Box tests assert on query text against a mocked client. These run every
query against the graph a deploy writes (``deployed_graph``), answered by rdflib, so they
show what the SPARQL writer is actually offered: an attribute or relationship the
designer added to a mapped class, with no column or join behind it, must not reach it;
the mapped properties and the relabelled and renamed classes must; and a namespace
deployed before the property marker existed must get exactly the prompt it got before.
"""

from __future__ import annotations

import json

import pytest
from coa_common.constants import URN_PREFIX
from coa_serve.tier2.ontop.tbox_context import TBoxContextBuilder
from coa_serve.tier2.ontop.types import VectorHit
from rdflib import RDFS, Literal, URIRef

from .deployed_graph import (
    CUSTOMERS,
    GRAPH,
    LOYALTY_TIER,
    NS,
    PREFERRED_ORDER,
    PREFIX,
    TEMPLATE,
    designer_v2_dataset,
    neptune_over,
)

pytestmark = pytest.mark.unit

# The prompt's own separators, written as escapes: an arrow and a dash.
_TO = "\u2192"
_DASH = "\u2014"

# What the builder rendered for this graph before the property gate existed. A
# namespace without property markers must keep getting exactly this.
_PROMPT_BEFORE_THE_GATE = "\n".join(
    [
        f"Ontology namespace: {NS}",
        f"PREFIX ind: <{PREFIX}>",
        "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>",
        "",
        "Classes:",
        " ind:Customers (label: Client)",
        " ind:PurchaseOrder (label: orders)",
        "",
        "Properties:",
        " ind:customers_id (label: id) domain: ind:Customers range: xsd:integer",
        " ind:customers_name (label: name) domain: ind:Customers range: xsd:string",
        " ind:customers_email (label: email) domain: ind:Customers range: xsd:string",
        " ind:customers_loyaltyTier (label: loyalty tier) domain: ind:Customers range: xsd:string",
        " ind:orders_id (label: id) domain: ind:PurchaseOrder range: xsd:integer",
        " ind:orders_status (label: status) domain: ind:PurchaseOrder range: xsd:string"
        ' allowed values: ["cancelled", "completed", "pending"] | description: Fulfilment state of the order',
        " ind:orders_totalAmount (label: total_amount) domain: ind:PurchaseOrder range: xsd:double",
        "",
        f"Join paths (ObjectProperties {_DASH} use these for multi-table queries):",
        f" ind:Customers {_TO} ind:PurchaseOrder via ind:customers_preferredOrder (Client {_TO} orders)",
        f" ind:PurchaseOrder {_TO} ind:Customers via ind:orders_customerId (orders {_TO} Client)"
        " | description: Foreign key: orders.customer_id references customers.id",
    ]
)


async def _prompt(*, property_markers: bool) -> str:
    builder = TBoxContextBuilder(neptune_over(designer_v2_dataset(property_markers=property_markers)), TEMPLATE)
    context = await builder.build([], NS, query="how many clients are there")
    return builder.format_for_prompt(context, NS)


async def test_the_prompt_drops_the_unmapped_attribute_and_relationship():
    prompt = await _prompt(property_markers=True)

    assert "loyaltyTier" not in prompt and "loyalty tier" not in prompt
    assert "preferredOrder" not in prompt and "preferred order" not in prompt
    # Removing exactly those two lines leaves today's prompt: every mapped property,
    # the FK join path and both edited classes are still offered.
    expected = "\n".join(
        line
        for line in _PROMPT_BEFORE_THE_GATE.splitlines()
        if "loyaltyTier" not in line and "preferredOrder" not in line
    )
    assert prompt == expected
    assert " ind:Customers (label: Client)" in prompt  # relabelled in the designer
    assert " ind:PurchaseOrder (label: orders)" in prompt  # IRI renamed in the designer


async def test_a_namespace_without_property_markers_gets_today_s_prompt():
    assert await _prompt(property_markers=False) == _PROMPT_BEFORE_THE_GATE


async def test_a_vector_hit_on_the_unmapped_attribute_does_not_bring_it_in():
    """The large-namespace path fetches the hits' definitions instead of the whole schema."""
    builder = TBoxContextBuilder(neptune_over(designer_v2_dataset()), TEMPLATE)
    hits = [
        VectorHit(type="ontology_class", score=0.9, entity_id="c", uri=CUSTOMERS),
        VectorHit(type="ontology_property", score=0.9, entity_id="p", uri=LOYALTY_TIER),
    ]

    classes, properties = await builder._fetch_ontology_context(hits, NS)

    assert [c["uri"] for c in classes] == [CUSTOMERS]
    assert LOYALTY_TIER not in {p["uri"] for p in properties}
    assert {p["label"] for p in properties} == {"id", "name", "email"}


async def test_the_label_match_fallback_does_not_bring_in_the_unmapped_attribute():
    builder = TBoxContextBuilder(neptune_over(designer_v2_dataset()), TEMPLATE)

    classes, properties = await builder._fetch_by_entities("loyalty tier of every client", NS)

    assert [c["uri"] for c in classes] == [CUSTOMERS]
    assert {p["label"] for p in properties} == {"id", "name", "email"}


async def test_the_unmapped_attribute_s_glossary_and_description_stay_out():
    """The glossary and the descriptions are fetched only for terms the gated context holds.

    A steward can give the designer's attribute synonyms (aiContext) and a description,
    and a vector hit can name it; neither may bring it back into the prompt.
    """

    async def build(*, property_markers: bool):
        ds = designer_v2_dataset(property_markers=property_markers)
        g = ds.graph(URIRef(GRAPH))
        ai_context = {"synonyms": ["gold status"], "instructions": "Tiers are bronze, silver and gold."}
        g.add((URIRef(LOYALTY_TIER), URIRef(f"urn:{URN_PREFIX}:vocab#aiContext"), Literal(json.dumps(ai_context))))
        g.add((URIRef(LOYALTY_TIER), RDFS.comment, Literal("Loyalty programme tier of the customer")))
        builder = TBoxContextBuilder(neptune_over(ds), TEMPLATE)
        hits = [
            VectorHit(type="ontology_class", score=0.9, entity_id="c", uri=CUSTOMERS),
            VectorHit(type="ontology_property", score=0.9, entity_id="p", uri=LOYALTY_TIER),
        ]
        context = await builder.build(hits, NS, query="the loyalty tier of every client")
        return context, builder.format_for_prompt(context, NS)

    context, prompt = await build(property_markers=True)
    unmarked_context, unmarked_prompt = await build(property_markers=False)

    assert context.glossary == []
    for text in ("gold status", "bronze, silver and gold", "Loyalty programme tier"):
        assert text not in prompt
        assert text in unmarked_prompt
    assert [term.label for term in unmarked_context.glossary] == ["loyalty tier"]


async def test_the_join_paths_keep_the_foreign_key_and_drop_the_unmapped_relationship():
    builder = TBoxContextBuilder(neptune_over(designer_v2_dataset()), TEMPLATE)

    joins = await builder._fetch_object_properties(NS, class_uris=[CUSTOMERS, PREFIX + "PurchaseOrder"])

    assert [j["uri"] for j in joins] == [PREFIX + "orders_customerId"]
    assert PREFERRED_ORDER not in {j["uri"] for j in joins}
