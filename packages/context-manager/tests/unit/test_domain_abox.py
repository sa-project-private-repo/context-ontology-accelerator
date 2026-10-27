# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the domain ABox augmenter of the Tier 3 lexical path."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from unittest.mock import AsyncMock, patch

import pytest
from coa_serve.clients.neptune import _SPARQL_WRITE_PATTERN, NeptuneGraphClient, SPARQLValidationError
from coa_serve.lexical.baseline_retriever import BaselineLexicalRetriever
from coa_serve.lexical.strategies import RetrieverStrategy
from coa_serve.tier3.domain_abox import DomainAboxAugmenter, context_enabled, norm_key
from coa_serve.tier3.graph_traverser import GraphEntity
from coa_serve.tier3.knowledge_retriever import KnowledgeRetriever
from rdflib import OWL, RDF, RDFS, Dataset, Graph, Literal, URIRef

from .lexical_helpers import entity_contexts, make_synthesizer, make_toolkit_node, source_metadata, topic_beam_nodes

pytestmark = pytest.mark.unit
NS = "11111111-2222-3333-4444-555555555555"
TEMPLATE = "https://ontology-workbench.local/{namespace}"
ABOX = f"https://ontology-workbench.local/_abox/{NS}"
COA = "urn:coa:vocab#"
DATA = f"http://coa.amazon.com/namespace/{NS}/data/"
ACME = f"{DATA}Customer/aaaa"
PO = f"{DATA}Order/bbbb"
LOAD = f"{DATA}Load/cccc"
VERSIONS = [{"v": "v3"}, {"a": "v3"}]
HOPS = [
    {
        "s": PO,
        "sl": "PO-1",
        "sc": "http://e.com/o#Order",
        "p": "http://e.com/o#placedBy",
        "o": ACME,
        "ol": "ACME",
        "oc": "http://e.com/o#Customer",
        "doc": "raw/ds-1/contract.pdf",
    }
]
FACT = 'Order "PO-1" -placedBy-> Customer "ACME" [source: contract.pdf]'
ENTITY = GraphEntity(uri="e1", label="ACME", type="Company")


def _chunk(doc: str) -> str:
    """The id graphrag gives the one chunk of ``doc`` (``{sourceId}:{hash}``), which ABox evidence stores."""
    digest = hashlib.sha256(doc.encode()).hexdigest()
    return f"aws:1111111122223333444455555:{digest[:8]}:{digest[8:12]}:{digest[12:20]}"


C1 = _chunk("c.pdf")
C2 = _chunk("b.pdf")


def _client(version_rows, id_rows, hop_rows, key_rows=None, chunk_rows=None):
    """A fake graph client that tells the augmenter's queries apart by what they read."""
    client = AsyncMock()

    async def query(sparql, **_):
        if "conformsToVersion" in sparql:
            return version_rows
        if "VALUES ?seed" in sparql:
            return hop_rows
        if "chunkId" in sparql:
            return chunk_rows or []
        if "normKey" in sparql:
            return id_rows if key_rows is None else key_rows
        return id_rows

    client.query.side_effect = query
    return client


def _queries(client):
    return [c.args[0] for c in client.query.call_args_list]


def _hops(client):
    return [q for q in _queries(client) if "VALUES ?seed" in q]


def _guard_accepts(query: str) -> bool:
    """Whether ``NeptuneGraphClient``'s write-keyword guard lets ``query`` through."""
    try:
        NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")._validate_sparql(query)
    except SPARQLValidationError:
        return False
    return True


def _augmenter(client, **kwargs):
    return DomainAboxAugmenter(client, graph_uri_template=TEMPLATE, **kwargs)


# ── norm_key ────────────────────────────────────────────────────────────


def test_norm_key_matches_the_engine():
    # Golden values of ontology-engine's test_abox_model.py: serve and the engine must agree on them.
    assert norm_key("  ACME   Corp ") == hashlib.sha256(b"acme corp").hexdigest()[:16]
    assert norm_key("  ACME   Corp ") == "ea6f9c07a2f95c78"


def test_norm_key_drops_format_characters_like_the_engine():
    # Zero-width space and joiner, soft hyphen, BOM and word joiner are Unicode category Cf.
    assert norm_key("AC\u200bME\u200d Co\u00adrp\ufeff\u2060") == "ea6f9c07a2f95c78"
    assert norm_key("\u200b ACME \u200b") == norm_key("acme")
    assert norm_key("AC\u200bME") == norm_key("acme")


# ── linking and rendering ───────────────────────────────────────────────


async def test_links_entities_and_renders_facts():
    client = _client(VERSIONS, [{"i": ACME}], HOPS)
    facts, step = await _augmenter(client).augment(NS, [ENTITY])
    assert facts.version == "v3" and facts.linked == 1
    assert facts.facts == (FACT,)
    assert step["step"] == "domain_abox" and step["status"] == "success"
    queries = _queries(client)
    by_id, by_key = queries[1], queries[2]
    assert '"e1"' in by_id and "lexicalEntityId" in by_id and "normKey" not in by_id
    assert norm_key("ACME") in by_key and "normKey" in by_key and "lexicalEntityId" not in by_key
    for q in (by_id, by_key):
        assert f"<https://ontology-workbench.local/_abox/{NS}>" in q


async def test_entity_id_matches_come_before_label_key_matches():
    others = [f"{DATA}Customer/{i:04d}" for i in range(10)]
    # Ten instances by id: the label keys are not even asked.
    client = _client(VERSIONS, [{"i": i} for i in others], [])
    facts, _ = await _augmenter(client).augment(NS, [ENTITY])
    assert facts.linked == 10
    assert not any("normKey" in q for q in _queries(client))
    # Fewer than ten by id: the label keys fill the rest, an instance found twice counts once.
    client = _client(VERSIONS, [{"i": others[0]}, {"i": others[1]}], [], key_rows=[{"i": others[1]}, {"i": ACME}])
    facts, _ = await _augmenter(client).augment(NS, [ENTITY])
    assert facts.linked == 3
    seeded = _hops(client)[0]
    assert seeded.index(f"<{others[1]}>") < seeded.index(f"<{ACME}>")


async def test_no_linked_instance_means_no_facts_and_no_hop():
    client = _client(VERSIONS, [], HOPS)
    facts, step = await _augmenter(client).augment(NS, [ENTITY])
    assert facts.facts == () and facts.linked == 0
    assert step["status"] == "success"
    assert not _hops(client)


async def test_second_hop_runs_below_the_threshold_and_dedupes():
    client = _client(VERSIONS, [{"i": ACME}], HOPS)
    facts, _ = await _augmenter(client).augment(NS, [ENTITY])
    assert len(_hops(client)) == 2
    # The second hop reads the same statement again: still one fact line.
    assert facts.facts == (FACT,)


async def test_fact_lines_are_capped():
    rows = [{**HOPS[0], "s": f"{PO}{i:03d}", "sl": f"PO-{i:03d}", "doc": ""} for i in range(40)]
    client = _client(VERSIONS, [{"i": ACME}], rows)
    facts, _ = await _augmenter(client).augment(NS, [ENTITY])
    assert len(facts.facts) == 30
    # 40 facts are over the threshold: no second hop.
    assert len(_hops(client)) == 1


async def test_labels_and_file_names_are_escaped_and_capped_and_iris_decoded():
    row = {
        **HOPS[0],
        "sl": 'say "hi"\n' + "x" * 300,
        "sc": "http://e.com/o#Order%20Line",
        "doc": 'raw/ds-1/"odd".pdf',
    }
    client = _client(VERSIONS, [{"i": ACME}], [row])
    facts, _ = await _augmenter(client).augment(NS, [ENTITY])
    label = ('say "hi" ' + "x" * 300)[:160].replace('"', '\\"')
    assert facts.facts == (f'Order Line "{label}" -placedBy-> Customer "ACME" [source: \\"odd\\".pdf]',)


# ── linking by the retrieved chunks ─────────────────────────────────────


async def test_without_lexical_entities_the_instances_of_the_retrieved_chunks_are_linked():
    client = _client(VERSIONS, [], HOPS, chunk_rows=[{"i": ACME, "rank": "0"}])
    facts, step = await _augmenter(client).augment(NS, [], chunk_ids=(C1, C2))
    assert facts.version == "v3" and facts.linked == 1 and facts.facts == (FACT,)
    assert step["status"] == "success" and step["detail"] == "1 instances linked; 1 facts (v3)"
    queries = _queries(client)
    assert len(queries) == 4  # version, by chunk, two hops
    by_chunk = queries[1]
    # Each chunk with its rank in the retrieval: an instance read from several counts its best one.
    assert by_chunk.startswith(
        "PREFIX hint: <http://aws.amazon.com/neptune/vocab/v01/QueryHints#> SELECT ?i (MIN(?r) AS ?rank) "
        "WHERE { hint:Query hint:queryTimeout 2000 . "
    )
    assert f'GRAPH <{ABOX}> {{ VALUES (?c ?r) {{ ("{C1}" 0) ("{C2}" 1) }} ' in by_chunk
    assert "?e <urn:coa:vocab#chunkId> ?c ; <urn:coa:vocab#about> ?i" in by_chunk
    assert by_chunk.endswith("GROUP BY ?i ORDER BY ?rank ?i LIMIT 10")
    assert not any("lexicalEntityId" in q or "normKey" in q for q in queries)


async def test_with_lexical_entities_the_chunks_are_not_read():
    entities_only = _client(VERSIONS, [{"i": ACME}], HOPS)
    expected, expected_step = await _augmenter(entities_only).augment(NS, [ENTITY])
    client = _client(VERSIONS, [{"i": ACME}], HOPS, chunk_rows=[{"i": PO}])
    facts, step = await _augmenter(client).augment(NS, [ENTITY], chunk_ids=(C1, C2))
    assert facts == expected and step["detail"] == expected_step["detail"]
    assert _queries(client) == _queries(entities_only)
    assert not any("chunkId" in q for q in _queries(client))


async def test_chunk_ids_are_listed_once_each_in_their_order_and_only_when_plain():
    # A chunk id is written between quotes, so only one that matches the entity id pattern is listed. One
    # that reads as a guard word is listed too, as the guard does not read literals. A chunk cited twice
    # keeps its first rank.
    client = _client(VERSIONS, [], [])
    chunk_ids = (C1, 'x" } DROP', "a b", "c" * 129, "aws:t:load:1", C1, C2)
    facts, step = await _augmenter(client).augment(NS, [], chunk_ids=chunk_ids)
    assert step["status"] == "success" and facts.linked == 0 and facts.facts == ()
    queries = _queries(client)
    assert f'VALUES (?c ?r) {{ ("{C1}" 0) ("aws:t:load:1" 1) ("{C2}" 2) }}' in queries[1]
    assert all(_guard_accepts(q) for q in queries)
    # No plain chunk id: the chunks are not read.
    client = _client(VERSIONS, [], [], chunk_rows=[{"i": ACME}])
    facts, step = await _augmenter(client).augment(NS, [], chunk_ids=("a b",))
    assert step["status"] == "success" and facts.linked == 0
    assert not any("chunkId" in q for q in _queries(client))


# ── when the step is skipped ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rows", "detail"),
    [
        ([], "no deployed version (vN)"),
        ([{"a": "v3"}], "no deployed version (vN)"),
        ([{"v": "2026-10-15T00:00:00Z"}, {"a": "v3"}], "no deployed version (vN)"),
        ([{"v": "v3"}], "no domain ABox has been built"),
        ([{"v": "v4"}, {"a": "v3"}], "ABox conforms to v3, Neptune serves v4"),
        ([{"v": "v3"}, {"v": "v4"}, {"a": "v3"}, {"a": "v4"}], "several ontology versions (v3,v4)"),
    ],
)
async def test_skips_with_a_reason_of_its_own(rows, detail):
    facts, step = await _augmenter(_client(rows, [], [])).augment(NS, [ENTITY])
    assert facts is None and step["status"] == "skipped"
    assert detail in step["detail"]


async def test_skips_without_a_graph_uri_template(monkeypatch):
    monkeypatch.delenv("GRAPH_URI_TEMPLATE", raising=False)
    client = _client(VERSIONS, [], [])
    facts, step = await DomainAboxAugmenter(client).augment(NS, [GraphEntity(uri="e1", label="A", type="T")])
    assert facts is None and step["status"] == "skipped"
    assert step["detail"] == "GRAPH_URI_TEMPLATE is not configured"
    client.query.assert_not_called()


async def test_skips_without_lexical_entities_or_chunks():
    client = _client(VERSIONS, [], [])
    facts, step = await _augmenter(client).augment(NS, [])
    assert facts is None and step["status"] == "skipped"
    assert step["detail"] == "no lexical entities or chunks to link"
    client.query.assert_not_called()


async def test_timeout_and_errors_never_raise():
    client = AsyncMock()

    async def slow(*a, **k):
        await asyncio.sleep(1)

    client.query.side_effect = slow
    facts, step = await _augmenter(client, timeout_s=0.01).augment(NS, [ENTITY])
    assert facts is None and step["status"] == "timeout"
    client.query.side_effect = RuntimeError("boom")
    facts, step = await _augmenter(client).augment(NS, [ENTITY])
    assert facts is None and step["status"] == "error"


async def test_version_lookup_is_cached_with_its_reason():
    client = _client(VERSIONS, [], [])
    aug = _augmenter(client)
    await aug.augment(NS, [ENTITY])
    await aug.augment(NS, [ENTITY])
    assert sum("conformsToVersion" in q for q in _queries(client)) == 1
    client = _client([{"v": "v4"}, {"a": "v3"}], [], [])
    aug = _augmenter(client)
    _, first = await aug.augment(NS, [ENTITY])
    _, second = await aug.augment(NS, [ENTITY])
    assert sum("conformsToVersion" in q for q in _queries(client)) == 1
    assert first["detail"] == second["detail"] and "v4" in second["detail"]


# ── the queries ─────────────────────────────────────────────────────────


async def test_every_query_carries_the_neptune_timeout_hint():
    client = _client(VERSIONS, [{"i": ACME}], HOPS)
    await _augmenter(client, timeout_s=1.5).augment(NS, [ENTITY])
    queries = _queries(client)
    assert len(queries) == 5  # version, by id, by key, two hops
    for q in queries:
        assert q.startswith("PREFIX hint: <http://aws.amazon.com/neptune/vocab/v01/QueryHints#> SELECT")
        assert q.count("hint:Query hint:queryTimeout 1500 .") == 1
        assert re.search(r"WHERE \{ hint:Query hint:queryTimeout 1500 \. ", q)


async def test_hop_query_finds_incoming_links_through_their_evidence_and_is_deterministic():
    client = _client(VERSIONS, [{"i": ACME}], HOPS)
    await _augmenter(client).augment(NS, [ENTITY])
    hop = _hops(client)[0]
    assert "?seed ?p ?o" in hop  # outgoing
    assert "?ev <urn:coa:vocab#object> ?seed ; <urn:coa:vocab#about> ?s ; <urn:coa:vocab#predicate> ?p" in hop
    assert "?s ?p ?seed" not in hop  # no index serves a bare incoming scan
    assert "(MIN(?src) AS ?doc)" in hop
    assert hop.endswith("GROUP BY ?s ?sl ?sc ?p ?o ?ol ?oc ORDER BY ?s ?p ?o LIMIT 60")


async def test_version_query_is_not_cut_off_and_filters_versions_in_sparql():
    client = _client(VERSIONS, [], [])
    await _augmenter(client).augment(NS, [ENTITY])
    version_query = _queries(client)[0]
    assert "LIMIT" not in version_query
    assert 'REGEX(?v, "^v[0-9]+$")' in version_query


async def test_queries_pass_the_neptune_write_guard():
    # NeptuneGraphClient rejects a query with INSERT, DELETE, DROP, CREATE, LOAD, CLEAR, COPY, MOVE or ADD as a
    # word outside its string literals and IRIs. A lexical entity id can fall back to the entity's own value,
    # so an id is listed only when it matches the plain id pattern: one that reads as one of those words stays
    # between its quotes, and one that would close them is left out.
    client = _client(VERSIONS, [{"i": ACME}], HOPS)
    entities = [
        GraphEntity(uri="e1", label="Add", type="Company"),
        GraphEntity(uri="Add", label="Add", type="Company"),
        GraphEntity(uri="load", label="Load", type="Company"),
        GraphEntity(uri="a:move-1", label="Move", type="Company"),
        GraphEntity(uri='x" } DROP', label="x", type="Company"),
    ]
    facts, step = await _augmenter(client).augment(NS, entities)
    assert step["status"] == "success", step
    assert facts.facts
    queries = _queries(client)
    assert len(queries) == 5
    for q in queries:
        assert _guard_accepts(q), q
    by_id = queries[1]
    assert all(f'"{i}"' in by_id for i in ("e1", "Add", "load", "a:move-1")), by_id
    assert "DROP" not in by_id
    assert norm_key("Add") in queries[2]


async def test_an_instance_iri_with_a_guard_word_is_a_seed_and_a_neighbour_like_any_other():
    # An instance of a class named Load has /Load/ in its IRI. The guard does not read IRIs.
    load_row = {
        **HOPS[0],
        "s": ACME,
        "sl": "ACME",
        "sc": "http://e.com/o#Customer",
        "p": "http://e.com/o#shippedOn",
        "o": LOAD,
        "ol": "L-1",
        "oc": "http://e.com/o#Load",
        "doc": "",
    }
    client = _client(VERSIONS, [{"i": LOAD}, {"i": ACME}], [HOPS[0], load_row])
    facts, step = await _augmenter(client).augment(NS, [ENTITY])
    assert step["status"] == "success", step
    assert facts.linked == 2
    assert facts.facts == ('Customer "ACME" -shippedOn-> Load "L-1"', FACT)
    hops = _hops(client)
    assert len(hops) == 2
    assert f"<{LOAD}>" in hops[0] and f"<{PO}>" in hops[1]
    assert _SPARQL_WRITE_PATTERN.search(hops[0]) and _guard_accepts(hops[0])
    # Reached from ACME, the Load instance is one of the neighbours the second hop starts from.
    client = _client(VERSIONS, [{"i": ACME}], [HOPS[0], load_row])
    facts, step = await _augmenter(client).augment(NS, [ENTITY])
    assert step["status"] == "success" and facts.linked == 1, step
    assert f"<{LOAD}>" in _hops(client)[1] and f"<{PO}>" in _hops(client)[1]
    assert all(_guard_accepts(q) for q in _queries(client))


async def test_a_failing_second_hop_keeps_the_first_hops_facts():
    client = AsyncMock()
    hop_calls = 0

    async def query(sparql, **_):
        nonlocal hop_calls
        if "conformsToVersion" in sparql:
            return VERSIONS
        if "VALUES ?seed" in sparql:
            hop_calls += 1
            if hop_calls == 2:
                raise RuntimeError("neighbours are too expensive")
            return HOPS
        return [{"i": ACME}]

    client.query.side_effect = query
    facts, step = await _augmenter(client).augment(NS, [ENTITY])
    assert step["status"] == "success", step
    assert hop_calls == 2
    assert facts.facts == (FACT,)


# ── against a graph, through the real Neptune client ────────────────────


def _evidence(abox: Graph, data: str, s: str, p: str, o: URIRef, doc: str) -> None:
    """The evidence node of the statement ``(s, p, o)`` read from the chunk of ``doc``, as the engine writes it."""
    e = URIRef(f"{data}evidence/{hashlib.sha256(f'{s} {p} {o} {doc}'.encode()).hexdigest()[:16]}")
    abox.add((e, RDF.type, URIRef(f"{COA}Evidence")))
    abox.add((e, URIRef(f"{COA}about"), URIRef(s)))
    abox.add((e, URIRef(f"{COA}predicate"), URIRef(p)))
    abox.add((e, URIRef(f"{COA}object"), o))
    abox.add((e, URIRef(f"{COA}chunkId"), Literal(_chunk(doc))))
    abox.add((e, URIRef(f"{COA}sourceKey"), Literal(f"raw/ds/{doc}")))


def _instance(abox: Graph, data: str, iri: str, cls: str, label: str, *docs: str, lexical_id: str = "") -> None:
    """An instance read from the chunks of ``docs``: its type, label, keys and type evidence."""
    abox.add((URIRef(iri), RDF.type, URIRef(f"http://e.com/o#{cls}")))
    abox.add((URIRef(iri), RDFS.label, Literal(label)))
    abox.add((URIRef(iri), URIRef(f"{COA}normKey"), Literal(norm_key(label))))
    if lexical_id:
        abox.add((URIRef(iri), URIRef(f"{COA}lexicalEntityId"), Literal(lexical_id)))
    for doc in docs:
        _evidence(abox, data, iri, str(RDF.type), URIRef(f"http://e.com/o#{cls}"), doc)


def _header(abox: Graph, data: str, version: str) -> None:
    """The ABox header, naming the version the ABox was built for."""
    abox.add((URIRef(f"{data}abox"), RDF.type, URIRef(f"{COA}DomainAbox")))
    abox.add((URIRef(f"{data}abox"), URIRef(f"{COA}conformsToVersion"), Literal(version)))


def _deployed(ds: Dataset, namespace: str, version: str) -> None:
    """The namespace's induced ontology graph, stamped with the version a deploy writes."""
    ontology = ds.graph(URIRef(f"https://ontology-workbench.local/{namespace}/ontology"))
    o = URIRef("http://e.com/o")
    ontology.add((o, RDF.type, OWL.Ontology))
    ontology.set((o, OWL.versionInfo, Literal(version)))
    ontology.add((o, URIRef("https://ontology-workbench.local/vocab#ontology_type"), Literal("induced")))


def _abox() -> Dataset:
    """An ABox shaped like ontology-engine's output: the header, instances, links and their evidence.

    Every evidence node names its chunk (one per document here), and an instance has type evidence
    from each chunk it was read from, as the engine derives it from the facts it is an end of.
    """
    ds = Dataset()
    abox = ds.graph(URIRef(ABOX))

    def link(s, p, o, *docs):
        abox.add((URIRef(s), URIRef(f"http://e.com/o#{p}"), URIRef(o)))
        for doc in docs:
            _evidence(abox, DATA, s, f"http://e.com/o#{p}", URIRef(o), doc)

    product = f"{DATA}Product/dddd"
    _instance(abox, DATA, ACME, "Customer", "ACME", "a.pdf", "c.pdf", "z.pdf", lexical_id="ent1")
    _instance(abox, DATA, PO, "Order", "PO-1", "a.pdf", "b.pdf", "z.pdf")
    _instance(abox, DATA, product, "Product", "Widget", "b.pdf")
    _instance(abox, DATA, LOAD, "Load", "L-1", "b.pdf", "c.pdf")
    link(PO, "placedBy", ACME, "z.pdf", "a.pdf")
    link(PO, "contains", product, "b.pdf")
    link(PO, "carriedBy", LOAD, "b.pdf")
    link(ACME, "shippedOn", LOAD, "c.pdf")
    _header(abox, DATA, "v3")
    _deployed(ds, NS, "v3")
    return ds


def _neptune_over(ds: Dataset, sent: list[str] | None = None) -> NeptuneGraphClient:
    """The real client, with its guard and LIMIT handling, whose HTTP call is answered by rdflib.

    ``sent`` collects the queries that reach the HTTP call.
    """
    client = NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")

    async def sparql_post(sparql: str):
        if sent is not None:
            sent.append(sparql)
        # Neptune consumes the hint triple; rdflib would read it as a pattern nothing matches.
        without_hint = re.sub(r"hint:Query hint:queryTimeout \d+ \. ", "", sparql)
        return json.loads(ds.query(without_hint).serialize(format="json"))

    client._sparql_post = sparql_post  # type: ignore[method-assign]
    return client


def _serve(
    ds: Dataset, sent: list[str] | None = None
) -> tuple[KnowledgeRetriever, BaselineLexicalRetriever, AsyncMock]:
    """Tier 3's lexical path as serve wires it, with the augmenter reading ``ds`` through the real client."""
    lexical = BaselineLexicalRetriever(
        graph_store_uri="neptune-db://test.neptune.amazonaws.com:8182",
        vector_store_uri="aoss://https://test.aoss.amazonaws.com",
    )
    synthesizer = make_synthesizer()
    retriever = KnowledgeRetriever(
        vector_retriever=None,
        graph_traverser=None,
        synthesizer=synthesizer,
        lexical_retriever=lexical,
        domain_abox=_augmenter(_neptune_over(ds, sent)),
    )
    return retriever, lexical, synthesizer


def _domain_step(result) -> dict:
    return next(s for s in result.trace_steps if s["step"] == "domain_abox")


async def test_reads_an_abox_through_the_real_neptune_client():
    aug = _augmenter(_neptune_over(_abox()))
    facts, step = await aug.augment(NS, [GraphEntity(uri="ent1", label="Something else", type="T")])
    assert step["status"] == "success", step
    assert facts.version == "v3" and facts.linked == 1
    # The seed is ACME. Its incoming link comes through the evidence, with the smallest source key. The
    # second hop (from the Load and the Order) adds the Order's other links.
    assert facts.facts == (
        'Customer "ACME" -shippedOn-> Load "L-1" [source: c.pdf]',
        'Order "PO-1" -placedBy-> Customer "ACME" [source: a.pdf]',
        'Order "PO-1" -carriedBy-> Load "L-1" [source: b.pdf]',
        'Order "PO-1" -contains-> Product "Widget" [source: b.pdf]',
    )


async def test_links_by_label_key_also_an_instance_whose_iri_has_a_guard_word():
    aug = _augmenter(_neptune_over(_abox()))
    facts, step = await aug.augment(NS, [GraphEntity(uri="Widget", label="widget", type="T")])
    assert step["status"] == "success", step
    assert facts.linked == 1 and len(facts.facts) == 3
    # The Load instance (/Load/ in its IRI) is linked and expanded like any other.
    facts, step = await aug.augment(NS, [GraphEntity(uri="unknown", label="L-1", type="T")])
    assert step["status"] == "success", step
    assert facts.linked == 1
    assert facts.facts == (
        'Customer "ACME" -shippedOn-> Load "L-1" [source: c.pdf]',
        'Order "PO-1" -carriedBy-> Load "L-1" [source: b.pdf]',
        'Order "PO-1" -contains-> Product "Widget" [source: b.pdf]',
        'Order "PO-1" -placedBy-> Customer "ACME" [source: a.pdf]',
    )


async def test_a_namespace_without_a_matching_abox_is_skipped_through_the_real_client():
    ds = _abox()
    ds.remove_graph(ds.graph(URIRef(f"https://ontology-workbench.local/{NS}/ontology")))
    facts, step = await _augmenter(_neptune_over(ds)).augment(NS, [ENTITY])
    assert facts is None and step["status"] == "skipped" and "no deployed version" in step["detail"]


async def test_topic_beam_results_link_by_chunk_through_the_real_neptune_client():
    # TopicBeamSearch, the Standard default, builds no entity contexts: its results reach the ABox through
    # the chunks they cite. The c.pdf chunk is evidence about ACME and the Load, the b.pdf chunk about the
    # Order, the Product and the Load.
    nodes = topic_beam_nodes(
        [
            (C1.rsplit(":", 1)[0], C1, ["ACME shipped on load L-1."]),
            (C2.rsplit(":", 1)[0], C2, ["PO-1 contains a Widget and was carried by load L-1."]),
        ]
    )
    sent: list[str] = []
    retriever, lexical, synthesizer = _serve(_abox(), sent)
    with patch.object(lexical, "_retrieve_sync", return_value=nodes):
        result = await retriever.resolve(
            "Which load did ACME ship on?", NS, retriever_strategy=RetrieverStrategy.TOPIC_BEAM
        )

    assert _domain_step(result)["status"] == "success", _domain_step(result)
    assert _domain_step(result)["detail"] == "4 instances linked; 4 facts (v3)"
    # The instances of the best-ranked chunk (c.pdf) first, then those of b.pdf, each by IRI.
    hop = next(q for q in sent if "VALUES ?seed" in q)
    seeds = re.search(r"VALUES \?seed \{ (.*?) \}", hop).group(1).split()
    assert seeds == [f"<{i}>" for i in (ACME, LOAD, PO, f"{DATA}Product/dddd")]
    kwargs = synthesizer.synthesize.call_args.kwargs
    assert kwargs["domain_version"] == "v3"
    assert kwargs["domain_facts"] == [
        'Customer "ACME" -shippedOn-> Load "L-1" [source: c.pdf]',
        'Order "PO-1" -carriedBy-> Load "L-1" [source: b.pdf]',
        'Order "PO-1" -contains-> Product "Widget" [source: b.pdf]',
        'Order "PO-1" -placedBy-> Customer "ACME" [source: a.pdf]',
    ]
    assert synthesizer.synthesize.call_args.args[2] == []  # no lexical entity
    assert not any("lexicalEntityId" in q or "normKey" in q for q in sent)


async def test_entity_results_link_as_before_through_the_real_neptune_client():
    # A retriever that builds entity contexts. Its result also cites the b.pdf chunk, which holds no
    # evidence about ACME: the entity is linked to ACME as before, and the chunk is not read.
    node = make_toolkit_node(
        metadata={
            "result": {
                "source": source_metadata(C2.rsplit(":", 1)[0]),
                "topics": [{"topic": "Orders", "topicId": "t1", "chunks": [{"chunkId": C2}], "statements": []}],
            },
            "entity_contexts": entity_contexts([("ent1", "Something else", "Company")]),
        }
    )
    sent: list[str] = []
    retriever, lexical, synthesizer = _serve(_abox(), sent)
    with patch.object(lexical, "_retrieve_sync", return_value=[node]):
        result = await retriever.resolve("Who is ACME?", NS, retriever_strategy=RetrieverStrategy.ENTITY_BASED)

    before: list[str] = []
    expected, expected_step = await _augmenter(_neptune_over(_abox(), before)).augment(
        NS, [GraphEntity(uri="ent1", label="Something else", type="Company")]
    )
    assert _domain_step(result)["detail"] == expected_step["detail"] == "1 instances linked; 4 facts (v3)"
    assert synthesizer.synthesize.call_args.kwargs["domain_facts"] == list(expected.facts)
    assert sent == before
    assert not any("chunkId" in q for q in sent)


async def test_a_chunk_of_another_namespace_links_nothing_through_the_real_neptune_client():
    other = "99999999-8888-7777-6666-555555555555"
    other_data = f"http://coa.amazon.com/namespace/{other}/data/"
    ds = _abox()
    other_abox = ds.graph(URIRef(f"https://ontology-workbench.local/_abox/{other}"))
    _instance(other_abox, other_data, f"{other_data}Customer/eeee", "Customer", "Globex", "globex.pdf")
    _header(other_abox, other_data, "v3")
    _deployed(ds, other, "v3")
    aug = _augmenter(_neptune_over(ds))

    facts, step = await aug.augment(NS, [], chunk_ids=(_chunk("globex.pdf"),))
    assert step["status"] == "success" and step["detail"] == "0 instances linked; 0 facts (v3)", step
    # In its own namespace the same chunk links its instance.
    facts, step = await aug.augment(other, [], chunk_ids=(_chunk("globex.pdf"),))
    assert step["detail"] == "1 instances linked; 0 facts (v3)", step


async def test_a_chunk_of_another_version_links_nothing_through_the_real_neptune_client():
    # The build for the next version has read a new document, but its ABox is still in its staging graph.
    ds = _abox()
    staging = ds.graph(URIRef(f"https://ontology-workbench.local/_staging/{NS}/abox-20261027T000000Z-1a2b3c4d"))
    _instance(staging, DATA, f"{DATA}Customer/ffff", "Customer", "Initech", "initech.pdf")
    _header(staging, DATA, "v4")
    facts, step = await _augmenter(_neptune_over(ds)).augment(NS, [], chunk_ids=(_chunk("initech.pdf"),))
    assert step["status"] == "success" and step["detail"] == "0 instances linked; 0 facts (v3)", step

    # Neptune serves v4 while the ABox still conforms to v3: the ABox is not read at all.
    ds = _abox()
    _deployed(ds, NS, "v4")
    sent: list[str] = []
    facts, step = await _augmenter(_neptune_over(ds, sent)).augment(NS, [], chunk_ids=(C1,))
    assert facts is None and step["status"] == "skipped"
    assert step["detail"] == "the ABox conforms to v3, Neptune serves v4"
    assert len(sent) == 1  # the version check only


async def test_linking_by_chunk_is_bounded_through_the_real_neptune_client():
    # Sixty retrieved chunks, each read for an instance of its own. The first 50 are listed, and 10 instances
    # linked: those of the ten best-ranked chunks. Ranks compare as numbers (rank 10 is not second), and the
    # instance IRIs sort the other way round from the ranks, so an order by IRI would pick others.
    ds = _abox()
    docs = [f"d{k:02d}.pdf" for k in range(60)]
    for k, doc in enumerate(docs):
        _instance(ds.graph(URIRef(ABOX)), DATA, f"{DATA}Customer/{59 - k:04d}", "Customer", f"C-{k}", doc)
    sent: list[str] = []
    facts, step = await _augmenter(_neptune_over(ds, sent)).augment(NS, [], chunk_ids=tuple(map(_chunk, docs)))

    assert step["status"] == "success" and step["detail"] == "10 instances linked; 0 facts (v3)", step
    by_chunk = next(q for q in sent if "chunkId" in q)
    assert re.findall(r'\("([^"]+)" (\d+)\)', by_chunk) == [(_chunk(d), str(k)) for k, d in enumerate(docs[:50])]
    hop = next(q for q in sent if "VALUES ?seed" in q)
    seeds = re.search(r"VALUES \?seed \{ (.*?) \}", hop).group(1).split()
    assert seeds == [f"<{DATA}Customer/{59 - k:04d}>" for k in range(10)]


def test_flag(monkeypatch):
    monkeypatch.delenv("ABOX_CONTEXT_ENABLED", raising=False)
    assert context_enabled()
    monkeypatch.setenv("ABOX_CONTEXT_ENABLED", "false")
    assert not context_enabled()
