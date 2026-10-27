# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The designer query console: the per-revision draft graph, result shaping and errors."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from coa_ontology import dynamo_store, proposals
from coa_ontology.abox import store as abox_store
from coa_ontology.catalog.routers import ontologies as ontology_routes
from coa_ontology.studio import changes, queries
from coa_ontology.studio import draft as store
from coa_ontology.studio import router as studio_router
from coa_ontology.studio.query_safety import HINT_IRI, QueryRefused
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Graph, Literal, URIRef

from .abox_fakes import InlineThread
from .studio_fakes import (
    CUSTOMER,
    NS,
    ONTO,
    ORDER,
    PARTY,
    PLACED_BY,
    SHIPMENT,
    VIP,
    fake_neptune,
    opened_draft,
    publish_base,
    seed_proposal,
    stored_state,
)

pytestmark = [pytest.mark.unit]
BASE = "https://ontology-workbench.local"
TENANT = NS.replace("-", "")[:25]
SELECT = "SELECT ?c WHERE { ?c a <http://www.w3.org/2002/07/owl#Class> }"
THROTTLED = {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}}


@pytest.fixture
def neptune():
    with fake_neptune(BASE) as fake:
        yield fake


@pytest.fixture
def opened(versions_aws, neptune):
    store.clear_cache()
    with patch("coa_ontology.proposals._generate_schema_sql", return_value=None):
        opened_draft("p-1")
        yield neptune
    store.clear_cache()


def _graph(revision: int, proposal_id: str = "p-1") -> str:
    return f"{BASE}/_studio/{NS}/{proposal_id}/r{revision}"


def _key(proposal_id: str = "p-1") -> dict[str, str]:
    return {"PK": dynamo_store._proposal_pk(NS, proposal_id), "SK": queries.GRAPH_SK}


def _row(proposal_id: str = "p-1") -> dict[str, Any] | None:
    return dynamo_store._get_table().get_item(Key=_key(proposal_id)).get("Item")


def _closed(proposal_id: str = "p-1") -> bool:
    """The row is a tombstone: closed, and naming no graph."""
    row = _row(proposal_id)
    return row is not None and "closed" in row and not queries._named(row)


def _backdate(graph: str) -> None:
    """Mark ``graph`` superseded since longer ago than the grace period."""
    since = datetime.now(UTC) - timedelta(seconds=queries.SUPERSEDED_GRACE_SECONDS + 1)
    dynamo_store._get_table().update_item(
        Key=_key(),
        UpdateExpression="SET superseded.#g = :t",
        ExpressionAttributeNames={"#g": graph},
        ExpressionAttributeValues={":t": since.isoformat()},
    )


def _run(query: str = SELECT, language: str = "sparql", target: str = "draft") -> dict[str, Any]:
    return queries.run_query(NS, "p-1", language, target, query)


# ── The draft graph ─────────────────────────────────────────────────────────


def test_a_new_revision_loads_its_own_graph_and_supersedes_the_old_one(opened):
    _run()
    assert opened.markers(_graph(0)) and _row()["graph"] == _graph(0)
    tbox_lines = len(opened.graphs[_graph(0)])
    _run()
    assert opened.posts == [_graph(0)]  # the second query found the marker: no reload

    changes.apply_change_set(NS, "p-1", [{"op": "delete_class", "iri": CUSTOMER}], 0, author="a")
    result = _run()
    assert result["revision"] == 1
    assert opened.markers(_graph(1)) and len(opened.graphs[_graph(1)]) < tbox_lines
    assert _row()["graph"] == _graph(1) and int(_row()["revision"]) == 1
    assert f"FROM <{_graph(1)}>" in opened.sent[-1][0]  # the query read the revision it names
    # A query of revision 0 may still be reading its graph: it is marked, not dropped.
    assert opened.markers(_graph(0)) and set(_row()["superseded"]) == {_graph(0)}


def test_a_later_load_drops_the_graphs_superseded_for_longer_than_the_grace_period(opened):
    _run()
    changes.apply_change_set(NS, "p-1", [{"op": "delete_class", "iri": CUSTOMER}], 0, author="a")
    _run()  # revision 1 is recorded and revision 0 superseded
    first = _row()["superseded"][_graph(0)]
    changes.apply_change_set(NS, "p-1", [{"op": "set_label", "iri": ORDER, "label": "PO"}], 1, author="a")
    _run()  # revision 2: revision 1 is superseded too, and revision 0 keeps the time it was first marked
    superseded = _row()["superseded"]
    assert set(superseded) == {_graph(0), _graph(1)} and superseded[_graph(0)] == first
    assert {_graph(0), _graph(1), _graph(2)} <= set(opened.graphs)  # all younger than the grace period

    _backdate(_graph(0))
    opened.graphs[_graph(2)].discard(opened.markers(_graph(2))[0])  # revision 2 is loaded once more
    _run()
    assert _graph(0) not in opened.graphs and _graph(1) in opened.graphs
    assert set(_row()["superseded"]) == {_graph(1)} and _row()["graphs"] == {_graph(1), _graph(2)}


def test_a_sweep_that_fails_does_not_fail_the_query_that_loaded_the_draft(opened):
    """The user's query failed because the sweep after its record could not update the row."""
    _run()
    changes.apply_change_set(NS, "p-1", [{"op": "delete_class", "iri": CUSTOMER}], 0, author="a")
    _run()  # revision 1 is recorded and revision 0 superseded
    _backdate(_graph(0))
    changes.apply_change_set(NS, "p-1", [{"op": "set_label", "iri": ORDER, "label": "PO"}], 1, author="a")
    table = dynamo_store._get_table()
    real_update = table.update_item

    def throttled(**kwargs: Any) -> Any:
        if kwargs["UpdateExpression"].startswith("DELETE graphs"):  # the sweep's row update, after its DROP
            raise ClientError(THROTTLED, "UpdateItem")
        return real_update(**kwargs)

    with patch.object(table, "update_item", side_effect=throttled):
        assert _run()["revision"] == 2
    assert _row()["graph"] == _graph(2) and _graph(0) not in opened.graphs
    assert _graph(0) in _row()["superseded"]  # still named, so a later sweep finishes it


def test_a_query_overtaken_by_a_newer_revision_reads_its_whole_graph(opened):
    """Recording revision 1 dropped revision 0 while a query of revision 0 still loaded it.

    That query then put its marker into the emptied graph and answered from a graph holding only the marker.
    """
    lines = set(queries._load_lines(_graph(0), store.revision_text(NS, "p-1", 0)[0]))
    newer: list[dict[str, Any]] = []

    def post(graph: str, text: str, *, retry_oom: bool = True) -> dict:
        opened.post(graph, text, retry_oom=retry_oom)
        if graph == _graph(0) and not newer:
            # Revision 0's triples are in, its marker is not: a query of revision 1 loads and records it.
            changes.apply_change_set(NS, "p-1", [{"op": "delete_class", "iri": CUSTOMER}], 0, author="a")
            newer.append(_run())
        return {}

    with patch.object(queries.ndb, "_gsp_post_turtle", side_effect=post):
        result = _run()
    assert (result["revision"], newer[0]["revision"]) == (0, 1)
    assert _row()["graph"] == _graph(1) and set(_row()["superseded"]) == {_graph(0)}
    assert opened.graphs[_graph(0)] - set(opened.markers(_graph(0))) == lines and opened.markers(_graph(0))


def test_a_query_whose_graph_was_dropped_while_it_ran_answers_an_error(opened):
    """A graph dropped mid-query leaves the query a part of the draft, or none of it."""

    def query(text: str, accept: str) -> httpx.Response:
        if not text.startswith("ASK {"):  # the query itself, not a marker check
            opened.graphs.pop(_graph(0))  # as the end of the review drops it
        return opened.query(text, accept)

    with patch.object(queries, "_post_query", side_effect=query), pytest.raises(queries.QueryFailed) as raised:
        _run()
    assert raised.value.message == queries.DRAFT_CHANGED and raised.value.status == 502


def test_two_loaders_of_one_revision_write_the_same_graph(opened):
    state = stored_state()
    first = queries.ensure_draft_graph(NS, "p-1", state)
    opened.graphs[_graph(0)].discard(opened.markers(_graph(0))[0])  # as if a second loader were mid-way
    second = queries.ensure_draft_graph(NS, "p-1", state)
    assert first == second == (_graph(0), True)
    assert len(opened.markers(_graph(0))) == 1 and _row()["graph"] == _graph(0)


RESTRICTED = (
    "PREFIX owl: <http://www.w3.org/2002/07/owl#> PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
    "SELECT ?c ?p WHERE { ?c rdfs:subClassOf ?r . ?r a owl:Restriction ; owl:onProperty ?p }"
)


def _posted_as_neptune_stores_them(opened, stored: Graph):
    """A GSP post that also keeps each batch as Neptune does: a blank node is new in every document."""

    def post(graph: str, text: str, *, retry_oom: bool = True) -> dict:
        stored.parse(data=text, format="nt")
        return opened.post(graph, text, retry_oom=retry_oom)

    return post


def test_a_blank_node_structure_split_over_two_batches_stays_whole(opened):
    """The canonical text puts the lines of blank-node subjects last, so in a draft of
    several batches a restriction's own triples and the class pointing to it went in different POSTs."""
    stored = Graph()
    with (
        patch.object(queries, "LOAD_BATCH_LINES", 5),
        patch.object(queries.ndb, "_gsp_post_turtle", side_effect=_posted_as_neptune_stores_them(opened, stored)),
    ):
        _run()
    assert len(opened.posts) > 1
    assert [(str(c), str(p)) for c, p in stored.query(RESTRICTED)] == [(ORDER, PLACED_BY)]


def test_a_second_load_of_a_revision_adds_no_second_copy_of_its_blank_nodes(opened):
    """Two first queries at once, or a load redone after a failure, doubled every restriction."""
    stored = Graph()
    with patch.object(queries.ndb, "_gsp_post_turtle", side_effect=_posted_as_neptune_stores_them(opened, stored)):
        _run()
        size = len(stored)
        opened.graphs[_graph(0)].discard(opened.markers(_graph(0))[0])  # as if the first load failed before it
        _run()
    assert opened.posts == [_graph(0), _graph(0)] and len(stored) == size
    assert len(list(stored.query(RESTRICTED))) == 1


THING = "http://www.w3.org/2002/07/owl#Thing"
ANONYMOUS_TBOX = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<{ONTO[:-1]}> a owl:Ontology .
<{ORDER}> a owl:Class ; rdfs:label "Order" ; rdfs:subClassOf owl:Thing,
    [ a owl:Restriction ; owl:onProperty <{PLACED_BY}> ; owl:maxCardinality "1"^^xsd:nonNegativeInteger ] .
<{PARTY}> a owl:Class ; rdfs:label "Party" ;
    owl:equivalentClass [ a owl:Class ; owl:unionOf ( <{CUSTOMER}> <{VIP}> ) ] .
<{CUSTOMER}> a owl:Class ; rdfs:label "Customer" .
<{VIP}> a owl:Class ; rdfs:label "VIP customer" .
<{PLACED_BY}> a owl:ObjectProperty ; rdfs:label "placed by" ; rdfs:domain <{ORDER}> ; rdfs:range <{CUSTOMER}> .
"""
NAMED_PARENTS = (
    "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
    "SELECT ?c ?super WHERE { ?c rdfs:subClassOf ?super FILTER(!isBlank(?super)) }"
)
NAMED_CLASSES = "PREFIX owl: <http://www.w3.org/2002/07/owl#> SELECT ?c WHERE { ?c a owl:Class FILTER(isIRI(?c)) }"


@pytest.fixture
def anonymous(versions_aws, neptune):
    """A draft opened on ANONYMOUS_TBOX: Order has a named parent and a restriction, and Party is a union."""
    store.clear_cache()
    publish_base(ANONYMOUS_TBOX, "")
    seed_proposal("p-1")
    store.open_draft(NS, "p-1", "alice")
    yield neptune
    store.clear_cache()


def _run_on(opened, stored: Graph):
    """A ``_post_query`` that runs each query on ``stored`` with rdflib, as Neptune runs it on the loaded graph."""
    # Neptune reads the timeout hint as a hint; rdflib reads it as a pattern, which this triple then matches.
    stored.add(
        (URIRef(f"{HINT_IRI}Query"), URIRef(f"{HINT_IRI}queryTimeout"), Literal(int(queries.QUERY_SECONDS * 1000)))
    )

    def query(text: str, accept: str) -> httpx.Response:
        if text.startswith("ASK {"):  # the marker check
            return opened.query(text, accept)
        return httpx.Response(200, content=stored.query(text).serialize(format="json"))

    return query


def test_blank_node_tests_answer_on_the_draft_graph_as_on_the_draft(anonymous):
    """The load writes the draft's blank nodes as IRIs, so in a query isBlank was false and
    isIRI true for them; Order's restriction came back as a second named parent, and the union as a named class."""
    draft = Graph().parse(data=store.revision_text(NS, "p-1", 0)[0], format="nt")
    stored = Graph()
    with (
        patch.object(queries.ndb, "_gsp_post_turtle", side_effect=_posted_as_neptune_stores_them(anonymous, stored)),
        patch.object(queries, "_post_query", side_effect=_run_on(anonymous, stored)),
    ):
        parents, classes = _run(NAMED_PARENTS)["rows"], _run(NAMED_CLASSES)["rows"]
    assert [(str(c), str(s)) for c, s in draft.query(NAMED_PARENTS)] == [(ORDER, THING)]
    assert parents == [[{"type": "uri", "value": ORDER}, {"type": "uri", "value": THING}]]
    named = sorted(str(c) for (c,) in draft.query(NAMED_CLASSES))
    assert named == sorted([ORDER, PARTY, CUSTOMER, VIP, SHIPMENT])
    assert sorted(row[0]["value"] for row in classes) == named and {row[0]["type"] for row in classes} == {"uri"}


def test_a_loader_that_lost_to_a_newer_revision_does_not_record_its_graph(opened):
    old = stored_state()
    changes.apply_change_set(NS, "p-1", [{"op": "delete_class", "iri": CUSTOMER}], 0, author="a")
    _run()  # revision 1 is loaded and recorded
    graph, recorded = queries.ensure_draft_graph(NS, "p-1", old)
    assert (graph, recorded) == (_graph(0), False)
    assert _row()["graph"] == _graph(1) and _graph(1) in opened.graphs
    assert _graph(0) in opened.graphs and set(_row()["superseded"]) == {_graph(0)}  # its query may read it


def test_a_graph_without_its_marker_is_loaded_again(opened):
    _run()
    opened.graphs[_graph(0)] = {line for line in opened.graphs[_graph(0)] if queries.MARKER not in line}
    _run()
    assert opened.posts == [_graph(0), _graph(0)] and opened.markers(_graph(0))


def test_a_failed_load_keeps_its_graph_tracked_and_the_next_query_loads_it_again(opened):
    """Dropping a half-loaded graph could cut a concurrent load of the same revision short (review M4)."""
    with (
        patch.object(queries.ndb, "_sparql_update", side_effect=httpx.ConnectError("down")),
        pytest.raises(queries.QueryFailed) as raised,
    ):
        _run()  # the triples land, the marker does not
    assert raised.value.status == 502
    assert _graph(0) in opened.graphs and not opened.markers(_graph(0))
    assert _row() == {**_row(), "graphs": {_graph(0)}} and "graph" not in _row()
    _run()
    assert opened.markers(_graph(0)) and _row()["graph"] == _graph(0)


def test_every_graph_a_load_started_is_dropped_when_the_review_ends(opened):
    with (
        patch.object(queries.ndb, "_sparql_update", side_effect=httpx.ConnectError("down")),
        pytest.raises(queries.QueryFailed),
    ):
        _run()  # revision 0: started, never finished
    changes.apply_change_set(NS, "p-1", [{"op": "delete_class", "iri": CUSTOMER}], 0, author="a")
    _run()  # revision 1 recorded; revision 0's unfinished graph is superseded
    assert _row()["graphs"] == {_graph(0), _graph(1)} and set(_row()["superseded"]) == {_graph(0)}
    assert queries.drop_draft_graph(NS, "p-1") is True
    assert opened.graphs == {} and _closed()


def test_a_load_that_fails_after_a_newer_revision_swept_it_stays_tracked(opened):
    """The re-review's L-a: a sweep forgets the graph while it loads; the failed load tracks it again."""

    def swept_then_fails(graph, *args):
        opened.post(graph, "<http://e/a> <http://e/b> <http://e/c> .", retry_oom=False)
        queries._forget(_key(), graph)  # as a newer revision's sweep would
        opened.post(graph, "<http://e/a> <http://e/b> <http://e/d> .", retry_oom=False)
        raise httpx.ConnectError("down")

    with patch.object(queries, "_load", side_effect=swept_then_fails), pytest.raises(queries.QueryFailed):
        _run()
    assert _row()["graphs"] == {_graph(0)}
    queries.drop_draft_graph(NS, "p-1")
    assert _graph(0) not in opened.graphs


def test_a_load_that_fails_otherwise_keeps_its_graph_tracked_and_raises_its_own_error(opened):
    """Only an HTTP or runtime error of the load used to track its graph again."""
    error = ValueError("unexpected")

    def swept_then_fails(graph, *args):
        opened.post(graph, "<http://e/a> <http://e/b> <http://e/c> .", retry_oom=False)
        queries._forget(_key(), graph)  # as a newer revision's sweep would
        opened.post(graph, "<http://e/a> <http://e/b> <http://e/d> .", retry_oom=False)
        raise error

    with patch.object(queries, "_load", side_effect=swept_then_fails), pytest.raises(ValueError) as raised:
        _run()
    assert raised.value is error and _row()["graphs"] == {_graph(0)}


def test_a_record_that_fails_keeps_the_graph_tracked_and_raises_its_own_error(opened):
    error = ClientError(THROTTLED, "UpdateItem")
    with patch.object(queries, "_record", side_effect=error), pytest.raises(ClientError) as raised:
        _run()
    assert raised.value is error and _row()["graphs"] == {_graph(0)} and "graph" not in _row()


def test_a_load_that_fails_as_the_review_ends_keeps_no_graph(opened):
    def cancelled_then_fails(graph, *args):
        opened.post(graph, "<http://e/a> <http://e/b> <http://e/c> .", retry_oom=False)
        dynamo_store.update_proposal("p-1", namespace=NS, status="cancelled")
        raise ValueError("unexpected")

    with patch.object(queries, "_load", side_effect=cancelled_then_fails), pytest.raises(store.NotEditable):
        _run()
    assert opened.graphs == {} and _closed()


def test_a_review_that_ended_during_the_load_keeps_no_graph(opened):
    real_load = queries._load

    def load_then_cancel(*args):
        real_load(*args)
        dynamo_store.update_proposal("p-1", namespace=NS, status="cancelled")

    with patch.object(queries, "_load", side_effect=load_then_cancel), pytest.raises(store.NotEditable):
        _run()
    assert opened.graphs == {} and _closed()


def test_a_load_that_starts_after_the_review_ended_writes_nothing(opened):
    """The closed row refuses a load that read the draft before its review ended."""
    state = stored_state()
    _run()
    dynamo_store.update_proposal("p-1", namespace=NS, status="cancelled")
    queries.drop_draft_graph(NS, "p-1")
    with pytest.raises(store.NotEditable):
        queries.ensure_draft_graph(NS, "p-1", state)
    assert opened.posts == [_graph(0)] and opened.graphs == {} and _closed()


def test_a_load_that_the_end_of_the_review_overtakes_drops_what_it_wrote(opened):
    """The cancel closed the row and dropped the graph while a batch of the load was still on its way."""
    real_load = queries._load

    def load_across_the_end(graph, tbox_nt, revision):
        real_load(graph, tbox_nt, revision)
        dynamo_store.update_proposal("p-1", namespace=NS, status="cancelled")
        queries.drop_draft_graph(NS, "p-1")
        opened.post(graph, "<http://e/a> <http://e/b> <http://e/c> .", retry_oom=False)  # it lands after the drop

    with patch.object(queries, "_load", side_effect=load_across_the_end), pytest.raises(store.NotEditable) as raised:
        _run()
    assert raised.value.extra == {"status": "cancelled"}  # the error the proposal's state gives
    assert opened.graphs == {} and _closed()


def test_a_dynamodb_error_while_a_load_ends_still_drops_what_it_wrote(opened):
    """A throttled read of why the review ended skipped the drop; the graph stayed unnamed."""
    real_load, real_get = queries._load, store.get_proposal
    reads: list[tuple[Any, ...]] = []

    def load_across_the_end(graph, tbox_nt, revision):
        real_load(graph, tbox_nt, revision)
        dynamo_store.update_proposal("p-1", namespace=NS, status="cancelled")
        queries.drop_draft_graph(NS, "p-1")
        opened.post(graph, "<http://e/a> <http://e/b> <http://e/c> .", retry_oom=False)  # it lands after the drop

    def throttled_after_the_first(*args: Any, **kwargs: Any) -> dict[str, Any]:
        reads.append(args)
        if len(reads) > 1:  # the query's own check passes; the read at the end of the load is throttled
            raise ClientError(THROTTLED, "GetItem")
        return real_get(*args, **kwargs)

    with (
        patch.object(queries, "_load", side_effect=load_across_the_end),
        patch.object(store, "get_proposal", side_effect=throttled_after_the_first),
        pytest.raises(ClientError),
    ):
        _run()
    assert len(reads) == 2 and opened.graphs == {} and _closed()


def test_namespace_deletion_closes_the_rows_so_a_load_in_flight_records_nothing(opened):
    real_load = queries._load

    def load_across_the_purge(graph, tbox_nt, revision):
        real_load(graph, tbox_nt, revision)
        assert queries.drop_all_draft_graphs(NS) == 1

    with patch.object(queries, "_load", side_effect=load_across_the_purge), pytest.raises(store.NotEditable):
        _run()
    assert opened.graphs == {} and _closed() and "graph" not in _row()


def test_a_load_during_the_namespace_teardown_drops_its_graph(opened):
    """The teardown fences the domain ABox lock first, so a draft without a row before it is covered."""
    abox_store.fence_teardown(NS, 0)
    with pytest.raises(store.NotEditable) as raised:
        _run()
    assert "being deleted" in raised.value.message
    assert opened.posts == [_graph(0)] and opened.graphs == {} and _closed()


def test_a_load_during_an_accept_that_then_fails_leaves_the_draft_queryable(opened):
    """A load that ended while the proposal was accepting closed its row for good, so after
    the accept failed (accept_failed is editable) every query of the draft answered 409."""
    real_load = queries._load

    def load_then_accepting(*args: Any) -> None:
        real_load(*args)
        dynamo_store.update_proposal("p-1", namespace=NS, status="accepting")

    with patch.object(queries, "_load", side_effect=load_then_accepting), pytest.raises(store.NotEditable):
        _run()
    assert "closed" not in _row() and _row()["graph"] == _graph(0)  # tracked, for an accept that succeeds to drop
    dynamo_store.update_proposal("p-1", namespace=NS, status="accept_failed")
    assert _run()["revision"] == 0
    changes.apply_change_set(NS, "p-1", [{"op": "delete_class", "iri": CUSTOMER}], 0, author="a")
    assert _run()["revision"] == 1 and "closed" not in _row()


def test_the_draft_graph_is_outside_the_serving_prefix(opened):
    graph = queries.draft_graph_uri(NS, "p-1", 7)
    assert graph == _graph(7) and not graph.startswith(f"{BASE}/{NS}/")
    with pytest.raises(ValueError):
        queries.draft_graph_uri(NS, "../p-1", 0)


def test_the_query_is_scoped_to_the_draft_and_the_document_abox(opened):
    _run()
    text, accept = opened.sent[-1]
    assert f"FROM <{_graph(0)}>" in text and f"FROM <{queries.abox_store.graph_uri(NS)}>" in text
    assert accept == "application/sparql-results+json"


def test_a_refused_query_loads_nothing(opened):
    with pytest.raises(QueryRefused):
        _run("SELECT * WHERE { GRAPH ?g { ?s ?p ?o } }")
    assert opened.posts == []


def test_language_and_target_go_in_pairs_and_a_closed_draft_takes_no_sparql(opened):
    with pytest.raises(QueryRefused):
        _run(language="cypher", target="draft")
    dynamo_store.update_proposal("p-1", namespace=NS, status="cancelled")
    with pytest.raises(store.NotEditable):
        _run()
    assert opened.posts == []


def test_the_checks_that_the_review_goes_on_read_the_proposal_strongly_consistently(opened):
    """An eventually consistent read could miss a review that just ended."""
    table = dynamo_store._get_table()
    with patch.object(table, "get_item", wraps=table.get_item) as get:
        _run()  # the check before the query and the one after the load
        store.get_proposal(NS, "p-1")
    reads = [c.kwargs.get("ConsistentRead", False) for c in get.call_args_list if c.kwargs["Key"]["SK"] == "META"]
    assert reads == [True, True, False]


# ── Results ─────────────────────────────────────────────────────────────────


def test_select_rows_keep_their_term_kinds_and_are_capped(opened):
    bindings = [
        {
            "s": {"type": "uri", "value": "http://e/a"},
            "l": {"type": "literal", "value": "Kunde", "xml:lang": "de"},
            "n": {"type": "typed-literal", "value": "3", "datatype": "http://www.w3.org/2001/XMLSchema#integer"},
            "b": {"type": "bnode", "value": "b0"},
        }
    ] + [{"s": {"type": "uri", "value": f"http://e/{i}"}} for i in range(queries.MAX_ROWS)]
    opened.answer = httpx.Response(
        200, json={"head": {"vars": ["s", "l", "n", "b"]}, "results": {"bindings": bindings}}
    )
    result = _run()
    assert result["columns"] == ["s", "l", "n", "b"] and result["form"] == "SELECT"
    assert result["rows"][0] == [
        {"type": "uri", "value": "http://e/a"},
        {"type": "literal", "value": "Kunde", "lang": "de"},
        {"type": "literal", "value": "3", "datatype": "http://www.w3.org/2001/XMLSchema#integer"},
        {"type": "bnode", "value": "b0"},
    ]
    assert result["rows"][1][1] is None  # unbound
    assert result["rowCount"] == queries.MAX_ROWS and result["truncated"]
    assert len(result["rows"]) == queries.MAX_ROWS


def test_ask_is_one_boolean_cell(opened):
    opened.answer = httpx.Response(200, json={"boolean": True})
    result = _run("ASK { ?s ?p ?o }")
    assert result["form"] == "ASK" and result["columns"] == ["result"]
    assert result["rows"] == [[{"type": "literal", "value": "true"}]] and not result["truncated"]


def test_construct_results_are_subject_predicate_object_rows(opened):
    opened.answer = httpx.Response(200, text='<http://e/b> <http://e/p> "x"@en .\n<http://e/a> <http://e/p> _:n1 .\n')
    result = _run("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }")
    assert opened.sent[-1][1] == "application/n-triples"
    assert result["columns"] == ["subject", "predicate", "object"]
    assert result["rows"][0][0] == {"type": "uri", "value": "http://e/a"}
    assert result["rows"][0][2]["type"] == "bnode"
    assert result["rows"][1][2] == {"type": "literal", "value": "x", "lang": "en"}


def test_a_blank_node_of_the_draft_reads_as_a_blank_node(opened):
    """The load writes the draft's blank nodes as IRIs under its graph; a result shows them
    as blank nodes again, not as links to terms that do not exist."""
    node = f"{_graph(0)}/genid/h0123"
    opened.answer = httpx.Response(
        200, json={"head": {"vars": ["r"]}, "results": {"bindings": [{"r": {"type": "uri", "value": node}}]}}
    )
    assert _run()["rows"] == [[{"type": "bnode", "value": "h0123"}]]
    opened.answer = httpx.Response(200, text=f"<{node}> <http://www.w3.org/2002/07/owl#onProperty> <{PLACED_BY}> .\n")
    rows = _run("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }")["rows"]
    assert rows == [[{"type": "bnode", "value": "h0123"}, *rows[0][1:]]] and rows[0][2]["type"] == "uri"


def test_cypher_rows_take_the_union_of_columns_and_are_capped(opened):
    results = [{"e": {"~id": "1", "value": "Acme"}, "n": 3}] + [{"name": f"x{i}"} for i in range(queries.MAX_ROWS)]
    client = MagicMock()
    client.execute_open_cypher_query.return_value = {"results": results}
    with patch.object(queries, "_cypher_client", return_value=client):
        result = _run(f"MATCH (e:`__Entity__{TENANT}__`) RETURN e", "cypher", "lexical")
    sent = client.execute_open_cypher_query.call_args.kwargs["openCypherQuery"]
    assert sent.endswith(f"LIMIT {queries.MAX_ROWS + 1}")
    assert result["form"] == "MATCH" and result["columns"] == ["e", "n", "name"]
    assert result["rows"][0] == [
        {"type": "value", "value": '{"value": "Acme", "~id": "1"}'},
        {"type": "value", "value": "3"},
        None,
    ]
    assert result["rows"][1] == [None, None, {"type": "literal", "value": "x0"}]
    assert result["truncated"] and result["rowCount"] == queries.MAX_ROWS
    assert opened.posts == []  # a lexical query needs no draft graph


# ── Errors ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "body", "error", "code"),
    [
        (500, {"code": "TimeLimitExceededException", "detailedMessage": "x"}, queries.QueryTimeout, 504),
        (400, {"code": "MalformedQueryException", "detailedMessage": "Bad thing\nat line 3"}, QueryRefused, 422),
        (500, {"code": "InternalFailureException", "detailedMessage": "secret detail"}, queries.QueryFailed, 502),
        (503, None, queries.QueryFailed, 502),
    ],
)
def test_neptune_errors_map_to_timeout_refusal_or_failure(status, body, error, code):
    response = httpx.Response(status, json=body) if body is not None else httpx.Response(status, text="<html>")
    mapped = queries._neptune_error(response)
    assert isinstance(mapped, error) and mapped.status == code
    assert "secret detail" not in mapped.message and "at line 3" not in mapped.message


def _client_raising(exc: Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    real = httpx.Client
    return lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs)


@pytest.mark.parametrize(
    ("exc", "error"),
    [(httpx.ReadTimeout("slow"), queries.QueryTimeout), (httpx.ConnectError("down"), queries.QueryFailed)],
)
def test_a_neptune_that_does_not_answer_is_a_timeout_or_a_failure(exc, error):
    with (
        patch.object(queries.ndb, "NDB_ENDPOINT", "https://neptune:8182"),
        patch.object(queries.ndb, "_sign", return_value={}),
        patch.object(queries.httpx, "Client", _client_raising(exc)),
        pytest.raises(error),
    ):
        queries._post_query(SELECT, "application/sparql-results+json")


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "first line\nhidden tail"}}, "ExecuteOpenCypherQuery")


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (ReadTimeoutError(endpoint_url="https://neptune:8182"), 504),
        (_client_error("TimeLimitExceededException"), 504),
        (_client_error("MalformedQueryException"), 422),
        (_client_error("InternalFailureException"), 502),
    ],
)
def test_cypher_errors_map_to_timeout_refusal_or_failure(exc, status):
    client = MagicMock()
    client.execute_open_cypher_query.side_effect = exc
    with patch.object(queries, "_cypher_client", return_value=client), pytest.raises(store.StudioError) as raised:
        queries._cypher(NS, f"MATCH (e:`__Entity__{TENANT}__`) RETURN e")
    assert raised.value.status == status and "hidden tail" not in raised.value.message


def test_a_namespace_without_a_tenant_takes_no_cypher():
    with pytest.raises(QueryRefused):
        queries._tenant("default")


def test_the_lexical_client_tries_once_and_waits_no_longer_than_the_budget():
    """Every Cypher test replaced the client, so a retry or a longer wait went unnoticed."""
    with patch("coa_ontology.abox.lexical._endpoint", return_value=("neptune.example", 8182)):
        client = queries._cypher_client()
    assert client.meta.config.retries["total_max_attempts"] == 1
    assert client.meta.config.read_timeout == queries.QUERY_SECONDS == 20
    assert client.meta.endpoint_url == "https://neptune.example:8182"


def test_neptune_s_message_is_cut_to_one_line_of_300_characters():
    """The cap on what Neptune's error text shows had no test."""
    detail = "y" * 400 + "\nsecond line"
    sparql = queries._neptune_error(
        httpx.Response(400, json={"code": "MalformedQueryException", "detailedMessage": detail})
    )
    assert sparql.message == f"The graph database refused the query: {'y' * 300}"
    client = MagicMock()
    client.execute_open_cypher_query.side_effect = ClientError(
        {"Error": {"Code": "MalformedQueryException", "Message": detail}}, "ExecuteOpenCypherQuery"
    )
    with patch.object(queries, "_cypher_client", return_value=client), pytest.raises(QueryRefused) as raised:
        queries._cypher(NS, f"MATCH (e:`__Entity__{TENANT}__`) RETURN e")
    assert raised.value.message == f"The graph database refused the query: {'y' * 300}"


SECRET = "zq7-only-in-the-query"


def test_no_query_leaves_its_text_in_the_log(opened, caplog):
    """'the query text is never logged' had no test; refused, failed and run queries."""
    app = FastAPI()
    app.include_router(studio_router.router, prefix="/ontology/proposals/{proposal_id}/studio")
    http = TestClient(app)
    url = f"/ontology/proposals/p-1/studio/queries?namespace={NS}"
    real_query = opened.query

    def failing(text: str, accept: str) -> httpx.Response:
        if text.startswith("ASK {"):  # the marker check
            return real_query(text, accept)
        raise queries._neptune_error(httpx.Response(500, json={"code": "InternalFailureException"}))

    client = MagicMock()
    client.execute_open_cypher_query.side_effect = [{"results": []}, _client_error("InternalFailureException")]
    sparql = f'SELECT ?s WHERE {{ ?s ?p "{SECRET}" }}'
    cypher = f"MATCH (e:`__Entity__{TENANT}__`) WHERE e.value = '{SECRET}' RETURN e"

    def post(language: str, target: str, query: str) -> int:
        return http.post(url, json={"language": language, "target": target, "query": query}).status_code

    with caplog.at_level(logging.DEBUG), patch.object(queries, "_cypher_client", return_value=client):
        statuses = [
            post("sparql", "draft", sparql),
            post("sparql", "draft", f"SELECT * FROM <http://e/{SECRET}> WHERE {{ ?s ?p ?o }}"),
            post("cypher", "lexical", cypher),
            post("cypher", "lexical", cypher),
            post("cypher", "lexical", f"MATCH (n) WHERE n.value = '{SECRET}' RETURN n"),
        ]
        with patch.object(queries, "_post_query", side_effect=failing):
            statuses.append(post("sparql", "draft", sparql))
    assert statuses == [200, 422, 200, 502, 422, 502]
    assert caplog.records and SECRET not in caplog.text


def test_a_query_sent_to_neptune_is_not_logged_by_the_http_client(caplog):
    """The query goes in the request body, which the HTTP client's own log leaves out."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"head": {"vars": []}, "results": {"bindings": []}})

    real = httpx.Client
    with (
        patch.object(queries.ndb, "NDB_ENDPOINT", "https://neptune:8182"),
        patch.object(queries.ndb, "_sign", return_value={}),
        patch.object(queries.httpx, "Client", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs)),
        caplog.at_level(logging.DEBUG),
        # test_live_induction_run imports a script that leaves httpx's logger at WARNING.
        caplog.at_level(logging.DEBUG, logger="httpx"),
    ):
        queries._post_query(f'SELECT ?s WHERE {{ ?s ?p "{SECRET}" }}', "application/sparql-results+json")
    assert caplog.records and SECRET not in caplog.text


# ── Drops ───────────────────────────────────────────────────────────────────


def test_dropping_a_proposal_s_graphs_closes_its_row(opened):
    """The row stays as a tombstone, so a load that runs meanwhile cannot track a graph in it."""
    _run()
    assert queries.drop_draft_graph(NS, "p-1") is True
    assert _graph(0) not in opened.graphs and _closed()
    closed_at = _row()["closed"]
    assert queries.drop_draft_graph(NS, "p-1") is True  # nothing left: a no-op
    assert _row()["closed"] == closed_at


def test_a_graph_whose_drop_failed_stays_in_the_row_for_a_later_drop(opened):
    """The row was deleted whatever the DROP did, and nothing named the graph any more."""
    _run()
    with patch.object(queries.ndb, "_sparql_update", side_effect=httpx.ConnectError("down")):
        assert queries.drop_draft_graph(NS, "p-1") is False
    assert _row()["graphs"] == {_graph(0)} and _row()["graph"] == _graph(0) and "closed" in _row()
    assert queries.drop_draft_graph(NS, "p-1") is True
    assert _graph(0) not in opened.graphs and _closed()


def test_namespace_deletion_tries_a_failed_drop_again_and_logs_what_it_could_not_drop(versions_aws, neptune, caplog):
    """The purge deletes the rows next, so a graph it cannot drop is named in one ERROR."""
    table = dynamo_store._get_table()
    flaky, stuck = _graph(0), _graph(0, "p-2")
    for proposal_id, graph in (("p-1", flaky), ("p-2", stuck)):
        table.put_item(Item={**_key(proposal_id), "graph": graph, "revision": 0, "graphs": {graph}})
        neptune.graphs[graph] = {"x"}
    failures = {flaky: 1, stuck: queries.DROP_ATTEMPTS}

    def update(text: str, *, retry_oom: bool = True) -> None:
        graph = text.rsplit("<", 1)[1].rstrip(">")
        if failures.get(graph):
            failures[graph] -= 1
            raise httpx.ConnectError("down")
        neptune.update(text, retry_oom=retry_oom)

    with (
        patch.object(queries.ndb, "_sparql_update", side_effect=update),
        patch.object(queries, "_sleep") as slept,
        caplog.at_level(logging.ERROR, logger="studio.queries"),
    ):
        assert queries.drop_all_draft_graphs(NS) == 2
    assert set(neptune.graphs) == {stuck} and _closed("p-1")
    assert _row("p-2")["graphs"] == {stuck} and "closed" in _row("p-2")
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and NS in errors[0] and stuck in errors[0] and flaky not in errors[0]
    assert slept.call_count == 1 + (queries.DROP_ATTEMPTS - 1)


def test_namespace_deletion_drops_every_graph_through_dynamodb_errors(versions_aws, neptune, caplog):
    """The first throttled row update stopped the drops, and the graphs left were named nowhere."""
    table = dynamo_store._get_table()
    proposal_ids = ("p-1", "p-2", "p-3")
    for proposal_id in proposal_ids:
        graph = _graph(0, proposal_id)
        table.put_item(Item={**_key(proposal_id), "graph": graph, "revision": 0, "graphs": {graph}})
        neptune.graphs[graph] = {"x"}
    real_update = table.update_item
    failed: list[str] = []

    def throttled(**kwargs: Any) -> Any:
        # p-1's row cannot be closed, and p-2's row update after its DROP fails.
        expression = kwargs["UpdateExpression"]
        if (expression.startswith("SET closed") and kwargs["Key"] == _key("p-1")) or (
            expression.startswith("DELETE graphs") and kwargs["Key"] == _key("p-2")
        ):
            failed.append(expression.split(" ", 1)[0])
            raise ClientError(THROTTLED, "UpdateItem")
        return real_update(**kwargs)

    with (
        patch.object(table, "update_item", side_effect=throttled),
        caplog.at_level(logging.ERROR, logger="studio.queries"),
    ):
        assert queries.drop_all_draft_graphs(NS) == len(proposal_ids)
    assert sorted(failed) == ["DELETE", "SET"] and neptune.graphs == {}
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]  # every graph was dropped


def test_namespace_deletion_drops_every_draft_graph_of_the_namespace_only(versions_aws, neptune):
    table = dynamo_store._get_table()
    for pk, graph in (
        (dynamo_store._proposal_pk(NS, "p-1"), _graph(2)),
        (dynamo_store._proposal_pk(NS, "p-2"), _graph(0, "p-2")),
        (dynamo_store._proposal_pk(NS[:-1] + "6", "p-3"), "https://other/r0"),
    ):
        table.put_item(Item={"PK": pk, "SK": queries.GRAPH_SK, "graph": graph, "revision": 0, "graphs": {graph}})
        neptune.graphs[graph] = {"x"}
    # A load that started and never finished is tracked only in ``graphs``.
    table.update_item(
        Key={"PK": dynamo_store._proposal_pk(NS, "p-2"), "SK": queries.GRAPH_SK},
        UpdateExpression="ADD graphs :g",
        ExpressionAttributeValues={":g": {_graph(1, "p-2")}},
    )
    neptune.graphs[_graph(1, "p-2")] = {"x"}
    assert queries.drop_all_draft_graphs(NS) == 2
    assert set(neptune.graphs) == {"https://other/r0"}
    assert _closed("p-1") and _closed("p-2")
    other = table.get_item(Key={"PK": dynamo_store._proposal_pk(NS[:-1] + "6", "p-3"), "SK": queries.GRAPH_SK})
    assert "closed" not in other["Item"]


def _delete_the_ontology(*proposal_ids: str) -> None:
    """Run the single-ontology delete's teardown of ``proposal_ids``, with stores that hold nothing."""
    vectors = MagicMock()
    vectors.list_searchable_entity_uris.return_value = []
    with patch.object(ontology_routes, "_stores", return_value=(MagicMock(), vectors)):
        ontology_routes._tear_down_ontology(NS, ONTO, list(proposal_ids), None, None)


def test_deleting_the_ontology_drops_the_draft_graphs_of_its_proposals(opened):
    """The proposal's partition went, its STUDIOGRAPH row too, and nothing named its graphs."""
    _run()
    _delete_the_ontology("p-1")
    assert dynamo_store.get_proposal_by_id("p-1", namespace=NS) is None
    assert opened.graphs == {} and _row() is None


def test_a_draft_graph_whose_drop_failed_keeps_its_row_through_the_proposal_deletion(opened):
    _run()
    with patch.object(queries.ndb, "_sparql_update", side_effect=httpx.ConnectError("down")):
        _delete_the_ontology("p-1")
    assert dynamo_store.get_proposal_by_id("p-1", namespace=NS) is None
    assert _row()["graphs"] == {_graph(0)} and "closed" in _row()
    assert queries.drop_all_draft_graphs(NS) == 1  # the namespace purge still finds it
    assert opened.graphs == {}


@pytest.fixture
def proposals_http(opened):
    app = FastAPI()
    app.include_router(proposals.router, prefix="/ontology/proposals")
    with patch.object(queries, "Thread", InlineThread):
        _run()
        yield TestClient(app)


def test_cancel_drops_the_draft_graph(proposals_http, opened):
    r = proposals_http.post(f"/ontology/proposals/p-1/cancel?namespace={NS}")
    assert r.status_code == 200, r.text
    assert _graph(0) not in opened.graphs and _closed()


def test_reject_drops_the_draft_graph(proposals_http, opened):
    r = proposals_http.post(
        f"/ontology/proposals/reject?namespace={NS}", json={"proposal_ids": ["p-1"], "reason": "no"}
    )
    assert r.status_code == 200 and r.json()[0]["status"] == "rejected"
    assert _graph(0) not in opened.graphs and _closed()


def test_a_published_accept_drops_the_draft_graph(proposals_http, opened):
    with (
        patch.object(proposals, "Thread", InlineThread),
        patch("coa_ontology.proposals._generate_schema_sql", return_value=None),
    ):
        r = proposals_http.post(
            f"/ontology/proposals/p-1/accept?namespace={NS}", json={"expectedRevision": 0, "deploy": False}
        )
    assert r.status_code == 202, r.text
    assert dynamo_store.get_proposal_by_id("p-1", namespace=NS)["status"] == "accepted"
    assert _graph(0) not in opened.graphs and _closed()


# ── Route ───────────────────────────────────────────────────────────────────


def test_the_queries_route_answers_rows_and_structured_errors(opened):
    app = FastAPI()
    app.include_router(studio_router.router, prefix="/ontology/proposals/{proposal_id}/studio")
    http = TestClient(app)
    url = f"/ontology/proposals/p-1/studio/queries?namespace={NS}"
    r = http.post(url, json={"language": "sparql", "target": "draft", "query": SELECT})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"language", "target", "revision", "form", "columns", "rows", "rowCount", "truncated", "elapsedMs"} <= set(
        body
    )
    r = http.post(url, json={"language": "sparql", "target": "draft", "query": "SELECT * FROM <http://x> { ?s ?p ?o }"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "QueryRefused"
    r = http.post(url, json={"language": "gremlin", "target": "draft", "query": "g.V()"})
    assert r.status_code == 422
