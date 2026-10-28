# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The terms no table maps, as both Tier 2 writers are told them, and their CANNOT_ANSWER reply.

The reader runs its queries against the graph a deploy writes after designer edits
(``deployed_graph``), answered by rdflib through the real Neptune client, so these show
what a writer is told: the designer's classes and the attributes and relationships
deploy marked unmapped, never a mapped class, a parent of one, a class named like one
or like one of their columns, or a class of a foundational ontology; and nothing at
all for a namespace without class markers.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote

import pytest
from coa_common.constants import GRAPH_BASE_URI
from coa_common.label_language import LabelPreference
from coa_serve import namespace_language
from coa_serve.served_version import ONTOLOGY_TYPE_IRI
from coa_serve.tier2.unmapped_terms import (
    MAX_TERMS,
    TermEnd,
    UnmappedTerm,
    UnmappedTerms,
    UnmappedTermsReader,
    _name_keys,
    declined,
    sparql_prompt_block,
    sql_prompt_block,
)
from rdflib import OWL, RDF, RDFS, Dataset, Literal, URIRef

from .deployed_graph import (
    CUSTOMER_SEGMENT,
    CUSTOMERS,
    GRAPH,
    IN_SEGMENT,
    IS_MAPPED,
    LOYALTY_PROGRAMME,
    LOYALTY_TIER,
    MEMBER,
    NS,
    PREFERRED_ORDER,
    PREFIX,
    PURCHASE_ORDER,
    TEMPLATE,
    designer_v2_dataset,
    neptune_over,
)

pytestmark = pytest.mark.unit

PREMIUM = PREFIX + "PremiumCustomer"


class _Counting:
    """The real client over ``ds``, counting the queries the reader sends."""

    def __init__(self, ds: Dataset, *, fail_first: bool = False, delay_s: float = 0.0):
        self._inner = neptune_over(ds)
        self.queries = 0
        self._fail_first = fail_first
        self._delay_s = delay_s

    async def query(self, sparql: str, **kwargs):
        self.queries += 1
        if self._fail_first:
            self._fail_first = False
            raise RuntimeError("neptune unavailable")
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        return await self._inner.query(sparql, **kwargs)


def _reader(ds: Dataset) -> UnmappedTermsReader:
    return UnmappedTermsReader(neptune_over(ds), graph_uri_template=TEMPLATE)


def _graph(ds: Dataset):
    return ds.graph(URIRef(GRAPH))


def _add_class(ds: Dataset, iri: str, label: str, graph=None) -> URIRef:
    g = graph if graph is not None else _graph(ds)
    term = URIRef(iri)
    g.add((term, RDF.type, OWL.Class))
    g.add((term, RDFS.label, Literal(label)))
    return term


def _documents(ds: Dataset):
    """A documents ontology induced in the same namespace, in its own graph, and its class IRI prefix."""
    ontology = f"{GRAPH_BASE_URI}/namespace/{NS}/ontology/unstructured"
    g = ds.graph(URIRef(f"https://ontology-workbench.local/{NS}/{quote(ontology, safe='')}"))
    g.add((URIRef(ontology), RDF.type, OWL.Ontology))
    g.add((URIRef(ontology), URIRef(ONTOLOGY_TYPE_IRI), Literal("induced")))
    return g, f"{ontology}/class/"


def _without_markers(ds: Dataset) -> Dataset:
    """The namespace as accepted before deploy wrote any ``coa:isMapped`` marker."""
    g = _graph(ds)
    g.remove((None, IS_MAPPED, None))
    return ds


async def _terms(ds: Dataset) -> dict[str, UnmappedTerm]:
    return {t.iri: t for t in (await _reader(ds).read(NS)).terms}


@pytest.fixture
def bind_language():
    """Bind a namespace language for the test, as the entrypoint gate does."""

    def bind(language: str) -> None:
        namespace_language.bind_label_preference(LabelPreference.for_namespace(language))

    yield bind
    namespace_language.bind_label_preference(LabelPreference.untagged_first())


class TestReader:
    async def test_lists_the_designer_terms_with_the_classes_they_belong_to(self):
        terms = await _reader(designer_v2_dataset()).read(NS)

        customers = TermEnd(label="Client", table="customers")
        assert list(terms.terms) == [
            UnmappedTerm(iri=CUSTOMER_SEGMENT, label="Customer segment", kind="class"),
            UnmappedTerm(iri=LOYALTY_PROGRAMME, label="Loyalty programme", kind="class"),
            UnmappedTerm(iri=PREMIUM, label="Premium customer", kind="class", of=(customers,)),
            UnmappedTerm(iri=LOYALTY_TIER, label="loyalty tier", kind="attribute", of=(customers,)),
            UnmappedTerm(
                iri=IN_SEGMENT,
                label="in segment",
                kind="relationship",
                of=(customers,),
                to=(TermEnd(label="Customer segment"),),
            ),
            UnmappedTerm(
                iri=MEMBER,
                label="member",
                kind="relationship",
                of=(TermEnd(label="Loyalty programme"),),
                to=(customers,),
            ),
            UnmappedTerm(
                iri=PREFERRED_ORDER,
                label="preferred order",
                kind="relationship",
                of=(customers,),
                to=(TermEnd(label="orders", table="orders"),),
            ),
        ]
        assert terms.omitted == 0

    async def test_a_namespace_without_class_markers_lists_nothing_after_one_query(self):
        client = _Counting(_without_markers(designer_v2_dataset()))

        terms = await UnmappedTermsReader(client, graph_uri_template=TEMPLATE).read(NS)

        assert not terms
        assert terms == UnmappedTerms()
        assert client.queries == 1

    async def test_a_namespace_deployed_before_property_markers_lists_its_classes_only(self):
        terms = await _terms(designer_v2_dataset(property_markers=False))

        assert set(terms) == {CUSTOMER_SEGMENT, LOYALTY_PROGRAMME, PREMIUM}

    async def test_a_mapped_class_and_its_parents_and_equivalents_are_not_listed(self):
        """Structured queries answer a parent of a mapped class through it, at any depth."""
        ds = designer_v2_dataset()
        g = _graph(ds)
        party = _add_class(ds, PREFIX + "Party", "Party")
        agent = _add_class(ds, PREFIX + "Agent", "Agent")
        buyer = _add_class(ds, PREFIX + "Buyer", "Buyer")
        g.add((URIRef(CUSTOMERS), RDFS.subClassOf, party))
        g.add((party, RDFS.subClassOf, agent))
        g.add((buyer, OWL.equivalentClass, URIRef(CUSTOMERS)))

        terms = await _terms(ds)

        assert not {CUSTOMERS, PURCHASE_ORDER, str(party), str(agent), str(buyer)} & set(terms)
        assert PREMIUM in terms

    async def test_a_class_named_like_a_mapped_class_is_not_listed(self):
        """A class found in documents beside the table of its name is the same thing: the table answers it."""
        ds = designer_v2_dataset()
        documents = f"{PREFIX.rsplit('/', 1)[0]}/unstructured/class/"
        order = _add_class(ds, documents + "Order", "Order")  # the table and label are "orders"
        client = _add_class(ds, documents + "Client", "client")  # the relabelled Customers
        customer = _add_class(ds, documents + "Customer", "Customer")  # its table is "customers"
        company = _add_class(ds, documents + "Company", "Company")

        terms = await _terms(ds)

        assert not {str(order), str(client), str(customer)} & set(terms)
        assert terms[str(company)].label == "Company"

    async def test_a_class_named_like_a_mapped_column_is_not_listed(self):
        """A class found in documents beside the column of its name is answered from that column."""
        ds = designer_v2_dataset()
        # The column total_amount, relabelled: its table column still names it.
        amount = URIRef(PREFIX + "orders_totalAmount")
        _graph(ds).set((amount, RDFS.label, Literal("Order total")))
        g, documents = _documents(ds)
        status = _add_class(ds, documents + "Status", "Status", graph=g)  # the column orders.status
        total = _add_class(ds, documents + "Total-amount", "Total amount", graph=g)
        # An attribute deploy marked unmapped has no column: a class of its name is listed.
        tier = _add_class(ds, documents + "Loyalty-tier", "Loyalty tier", graph=g)

        terms = await _terms(ds)

        assert not {str(status), str(total)} & set(terms)
        assert terms[str(tier)].label == "Loyalty tier"

    async def test_a_class_named_like_a_mapped_class_s_iri_is_not_listed(self):
        """The designer renamed Orders to PurchaseOrder and kept its label orders: a class Purchase order is it."""
        ds = designer_v2_dataset()
        g, documents = _documents(ds)
        purchase_order = _add_class(ds, documents + "Purchase-order", "Purchase order", graph=g)

        assert str(purchase_order) not in await _terms(ds)

    @pytest.mark.parametrize(
        ("unmapped", "mapped", "same"),
        [
            ("Order", "orders", True),
            ("Company", "companies", True),
            ("Customer segment", "customer_segments", True),
            ("Address", "address", True),
            ("Address", "addresses", True),
            ("Branch", "branches", True),
            ("Status", "statuses", True),
            ("Person", "people", True),
            ("Sales person", "sales_people", True),
            ("Child", "children", True),
            ("Premium customer", "customers", False),
            ("Organization", "customers", False),
            ("Customer segment", "customers", False),
        ],
    )
    def test_names_compare_without_case_separators_or_plural(self, unmapped, mapped, same):
        assert bool(_name_keys(unmapped) & _name_keys(mapped)) is same

    async def test_a_class_of_a_foundational_ontology_is_not_listed(self):
        ds = designer_v2_dataset()
        reference = "https://schema.example.org/"
        g = ds.graph(URIRef(f"https://ontology-workbench.local/{NS}/{quote(reference, safe='')}"))
        g.add((URIRef(reference), RDF.type, OWL.Ontology))
        g.add((URIRef(reference), URIRef(ONTOLOGY_TYPE_IRI), Literal("foundational")))
        invoice = _add_class(ds, reference + "Invoice", "Invoice", graph=g)

        terms = await _terms(ds)

        assert str(invoice) not in terms
        assert PREMIUM in terms

    async def test_lists_at_most_max_terms_keeping_those_tied_to_mapped_classes(self):
        """The classes on their own are labelled to sort first, so only the priority keeps the others."""
        ds = designer_v2_dataset()
        for n in range(60):
            _add_class(ds, f"{PREFIX}Topic{n:02d}", f"Aa topic {n:02d}")

        terms = await _reader(ds).read(NS)

        assert len(terms.terms) == MAX_TERMS
        assert terms.omitted == 3 + 60 + 4 - MAX_TERMS
        listed = {t.iri for t in terms.terms}
        assert {PREMIUM, LOYALTY_TIER, PREFERRED_ORDER, IN_SEGMENT, MEMBER} <= listed
        assert f"{PREFIX}Topic44" in listed and f"{PREFIX}Topic45" not in listed
        assert not {CUSTOMER_SEGMENT, LOYALTY_PROGRAMME} & listed

    async def test_labels_follow_the_namespace_language(self, bind_language):
        bind_language("ja")
        ds = designer_v2_dataset()
        _graph(ds).add((URIRef(PREMIUM), RDFS.label, Literal("プレミアム顧客", lang="ja")))

        assert (await _terms(ds))[PREMIUM].label == "プレミアム顧客"

    async def test_a_read_is_kept_for_a_minute(self):
        client = _Counting(designer_v2_dataset())
        reader = UnmappedTermsReader(client, graph_uri_template=TEMPLATE)

        first = await reader.read(NS)
        queries = client.queries
        assert await reader.read(NS) == first
        assert client.queries == queries == 4

    async def test_a_failed_read_lists_nothing_for_half_a_minute_then_is_read_again(self):
        now = [0.0]
        client = _Counting(designer_v2_dataset(), fail_first=True)
        reader = UnmappedTermsReader(client, graph_uri_template=TEMPLATE, clock=lambda: now[0])

        assert await reader.read(NS) == UnmappedTerms()
        now[0] += 29.0
        assert await reader.read(NS) == UnmappedTerms()
        assert client.queries == 1
        now[0] += 1.0
        assert PREMIUM in {t.iri for t in (await reader.read(NS)).terms}

    async def test_a_slow_read_lists_nothing_and_the_next_writer_does_not_wait_for_it_again(self):
        """One request reads up to three times (NL-to-SQL, Ontop and its retry): the budget is paid once."""
        client = _Counting(designer_v2_dataset(), delay_s=0.5)
        reader = UnmappedTermsReader(client, graph_uri_template=TEMPLATE, timeout_s=0.05)

        assert await reader.read(NS) == UnmappedTerms()
        queries = client.queries
        for _ in range(2):
            assert await reader.read(NS) == UnmappedTerms()
        assert client.queries == queries

    async def test_no_graph_template_lists_nothing(self, monkeypatch):
        monkeypatch.delenv("GRAPH_URI_TEMPLATE", raising=False)
        client = _Counting(designer_v2_dataset())

        assert await UnmappedTermsReader(client).read(NS) == UnmappedTerms()
        assert client.queries == 0


class TestDeclined:
    @pytest.mark.parametrize(
        "reply",
        [
            "CANNOT_ANSWER\nloyalty tier",
            "CANNOT_ANSWER: premium customer",
            "**CANNOT_ANSWER**\nsign-up date",
            "```\nCANNOT_ANSWER\nloyalty tier\n```",
            "```sql\nCANNOT_ANSWER\n```",
            "The schema has no column for the loyalty tier.\n\nCANNOT_ANSWER",
            # Reasoned first, then the reply in the code block the system prompt asks for.
            "The schema has no column for the loyalty tier.\n```sql\nCANNOT_ANSWER\nloyalty tier\n```",
            "Loyalty tier is listed as a term with no table.\n```sparql\nCANNOT_ANSWER\nloyalty tier\n```",
            "The schema has no column for the loyalty tier.\n\n```\n**CANNOT_ANSWER**\n```",
            "```CANNOT_ANSWER\nloyalty tier```",
        ],
    )
    def test_a_decline(self, reply):
        assert declined(reply)

    @pytest.mark.parametrize(
        "reply",
        [
            "```sql\nSELECT count(*) FROM orders\n```\nConfidence: 0.9",
            "No need to reply CANNOT_ANSWER: the orders table answers it.\n```sql\nSELECT count(*) FROM orders\n```",
            "The orders table answers it.\n```sql\nSELECT count(*) FROM orders\n```\nNo CANNOT_ANSWER needed.",
            "SELECT name FROM customers",
            "I cannot answer this question.",
            "",
        ],
    )
    def test_not_a_decline(self, reply):
        assert not declined(reply)


class TestPromptBlocks:
    _TERMS = UnmappedTerms(
        terms=(
            UnmappedTerm(iri=PREMIUM, label="Premium customer", kind="class", of=(TermEnd("Client", "customers"),)),
            UnmappedTerm(
                iri=LOYALTY_TIER, label="loyalty tier", kind="attribute", of=(TermEnd("Client", "customers"),)
            ),
            UnmappedTerm(
                iri=MEMBER,
                label="member",
                kind="relationship",
                of=(TermEnd("Loyalty programme"),),
                to=(TermEnd("Client", "customers"),),
            ),
        ),
        omitted=2,
    )

    def test_the_sql_block_names_classes_by_their_table(self):
        block = sql_prompt_block(self._TERMS)

        assert block.splitlines()[:6] == [
            "## Terms with no table behind them",
            "The ontology has these terms, but no table or column of this database holds their data:",
            "- Premium customer (a kind of customers)",
            "- loyalty tier (an attribute of customers)",
            "- member (a relationship from Loyalty programme to customers)",
            "- and 2 more",
        ]

    def test_the_sparql_block_names_classes_by_their_label(self):
        block = sparql_prompt_block(self._TERMS)

        assert "- Premium customer (a kind of Client)" in block
        assert "- member (a relationship from Loyalty programme to Client)" in block
        assert "SPARQL" in block and " SQL " not in block

    @pytest.mark.parametrize("render", [sql_prompt_block, sparql_prompt_block])
    def test_the_rule_asks_for_cannot_answer_and_never_a_stand_in(self, render):
        rule = render(self._TERMS).split("\n\n")[-1]

        assert "Reply with CANNOT_ANSWER on the first line and the missing term on the second" in rule
        assert "Do not answer it with a different" in rule
        assert "is answered as usual" in rule

    @pytest.mark.parametrize("render", [sql_prompt_block, sparql_prompt_block])
    def test_nothing_to_list_renders_nothing(self, render):
        assert render(UnmappedTerms()) == ""
