# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import json
import time

import httpx
import pytest
from botocore.exceptions import ClientError
from coa_ontology import dynamo_store
from coa_ontology.abox import build, extract, mapping, matcher, store
from coa_ontology.abox import model as m
from coa_ontology.abox.tbox import load_tbox
from coa_ontology.versions import deploy, registry, storage
from coa_ontology.versions.snapshot import canonical_nt
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

from .abox_fakes import InlineThread, Neptune

pytestmark = [
    pytest.mark.unit,
    # rdflib's own SPARQL engine calls its deprecated Dataset accessors when the fake Neptune runs a request.
    pytest.mark.filterwarnings(r"ignore:Dataset\.\w+ is deprecated:DeprecationWarning"),
]

NS = "11111111-2222-3333-4444-555555555555"
TTL = """
@prefix owl: <http://www.w3.org/2002/07/owl#> . @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix o: <http://e.com/o#> .
o:Customer a owl:Class ; rdfs:label "Customer" . o:Order a owl:Class ; rdfs:label "Order" .
o:placedBy a owl:ObjectProperty ; rdfs:domain o:Order ; rdfs:range o:Customer .
"""
# Contract has no lexical instance, so a build re-extracts it.
TTL_WITH_CONTRACT = TTL + 'o:Contract a owl:Class ; rdfs:label "Contract" .\n'
C = "http://e.com/o#Customer"
ONE_TRIPLE = "<http://e.com/s> <http://e.com/p> <http://e.com/o> .\n"


def _snapshot():
    prov = m.Provenance("c1", "ds-1", "raw/a.pdf")
    return m.LexicalSnapshot(
        {"e1": m.LexEntity("e1", "ACME", "Customer"), "e2": m.LexEntity("e2", "PO-1", "Order")},
        (m.LexFact("f1", "e2", "PLACED_BY", "e1", prov),),
    )


def _read(ns, on_progress=None):
    return _snapshot()


def _embed(texts):
    return [[float(len(t))] for t in texts]


def _judge(system, user):
    items = json.loads(user)["items"]
    # Pick the candidate whose text starts with the item's own words (Customer to Customer, Order to Order).
    return {
        "decisions": [
            {
                "id": it["id"],
                "choice": next(
                    (i for i, c in enumerate(it["candidates"]) if c.split(":")[0].lower() in it["text"].lower()), 0
                ),
            }
            for it in items
        ]
    }


def _steal():
    """Another worker takes the lock over (as after this one's lease went stale)."""
    store.release_lock(NS, store.lock_holder(NS))
    store.acquire_lock(NS, "someone-else")


def _throttled(op="TransactWriteItems"):
    return ClientError({"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, op)


@pytest.fixture
def env(versions_aws, monkeypatch):
    monkeypatch.delenv("NDB_GRAPH_URI_BASE", raising=False)
    monkeypatch.delenv("NDB_GRAPH_URI", raising=False)
    monkeypatch.setattr(deploy, "_STEP_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(deploy, "_AMBIGUOUS_MOVE_WAIT_SECONDS", 0)
    monkeypatch.setattr(build, "_RESTART_BACKOFF_SECONDS", 0)
    nep = Neptune()
    monkeypatch.setattr(build, "_sparql_update", nep.update)
    monkeypatch.setattr(build, "_sparql_query", nep.query)
    monkeypatch.setattr(build, "_gsp_post_turtle", nep.gsp_post)
    monkeypatch.setattr(build, "Thread", InlineThread)
    calls = {"read": 0}

    def deps(snapshot=None, on_read=None):
        def read(ns, on_progress=None):
            calls["read"] += 1
            if on_read:
                on_read()
            return snapshot or _snapshot()

        return build.Deps(
            read_lexical=read,
            embed=_embed,
            judge=_judge,
            extract_judge=lambda s, u: {"entities": [], "relations": []},
            search=lambda i, v, k: [],
            embed_query=lambda t: [0.0],
        )

    return versions_aws, nep, deps, calls


def _activate(aws, version, ttl=TTL):
    aws.s3.put_object(Bucket=aws.bucket, Key=storage.version_key(NS, version, "ontology.ttl"), Body=ttl.encode())
    registry.set_active(NS, version=version, deployment_id=f"d-{version}", deployed_by="tester")


def _staging_graphs(nep):
    return [g for g in nep.graphs() if "/_staging/" in g]


def _set_heartbeat(aws, ts):
    aws.table.update_item(
        Key=store.ddb_key(NS, store.LOCK_SK),
        UpdateExpression="SET heartbeat_ts = :ts",
        ExpressionAttributeValues={":ts": int(ts)},
    )


# ── Builds ──────────────────────────────────────────────────────────────────


def test_nothing_to_build_without_a_version(env):
    assert build.request_build(NS, "documents") is False
    assert not store.has_pending(NS)


def test_build_publishes_a_ready_abox(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    assert build.request_build(NS, "deploy v3")
    state = store.get_state(NS)
    assert (state["status"], state["conforms_to_version"]) == (store.READY, 3)
    triples = nep.triples(store.graph_uri(NS))
    acme = URIRef(m.instance_iri(NS, C, "ACME"))
    assert (acme, RDF.type, URIRef(C)) in triples
    header = URIRef(m.header_iri(NS))
    assert (header, URIRef(m.CONFORMS_TO_VERSION), Literal("v3")) in triples
    assert (header, URIRef(m.BUILD_ID), Literal(state["build_id"])) in triples
    archived = store.get_text(store.build_key(NS, state["build_id"]))
    lines = archived.split("\n")[:-1]
    assert lines == sorted(lines) and len(lines) == len(triples)
    assert set(Graph().parse(data=archived, format="nt")) == triples
    assert store.lock_holder(NS) is None and not store.has_pending(NS)
    assert _staging_graphs(nep) == []


def test_header_lines_merge_in_canonical_order():
    body = Graph()
    body.add((URIRef(m.instance_iri(NS, C, "ACME")), RDF.type, URIRef(C)))
    body.add((URIRef(m.header_iri(NS)), URIRef(m.CONFORMS_TO_VERSION), Literal("v3")))
    built_at = "2026-10-16T00:00:00+00:00"
    full = Graph()
    for triple in body:
        full.add(triple)
    full.add((URIRef(m.header_iri(NS)), URIRef(m.BUILD_ID), Literal("b1")))
    full.add((URIRef(m.header_iri(NS)), URIRef(m.BUILT_AT), Literal(built_at, datatype=XSD.dateTime)))
    assert build._with_header(canonical_nt(body), NS, "b1", built_at) == canonical_nt(full)


def test_a_label_with_a_line_separator_loads_intact(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    prov = m.Provenance("c1", "ds-1", "raw/a.pdf")
    name = "ACME\u2028Corp"  # N-Triples leaves U+2028 unescaped; str.splitlines() would cut the line
    snapshot = m.LexicalSnapshot(
        {"e1": m.LexEntity("e1", name, "Customer"), "e2": m.LexEntity("e2", "PO-1", "Order")},
        (m.LexFact("f1", "e2", "PLACED_BY", "e1", prov),),
    )
    monkeypatch.setattr(build, "_default_deps", lambda: deps(snapshot=snapshot))
    build.request_build(NS, "documents")
    assert store.get_state(NS)["status"] == store.READY
    assert Literal(name) in {o for _, _, o in nep.triples(store.graph_uri(NS))}


def test_same_inputs_same_checksum(env, monkeypatch):
    aws, _, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    build.request_build(NS, "a")
    first = store.get_state(NS)
    build.request_build(NS, "b")
    second = store.get_state(NS)
    assert first["build_id"] != second["build_id"] and first["nt_sha256"] == second["nt_sha256"]


def test_reset_version_clears_the_abox(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    build.request_build(NS, "deploy")
    registry.reset_active(NS, ontology_id="o")
    build.request_build(NS, "deleted")
    assert store.get_state(NS)["status"] == store.EMPTY
    assert nep.triples(store.graph_uri(NS)) == set()


def test_a_clear_keeps_track_of_a_staging_graph_a_build_left(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    build.request_build(NS, "deploy")
    # A build that gave up with its MOVE in doubt keeps its staging graph and records it.
    store.update_state(NS, status=store.FAILED, last_build_id="kept")
    nep.load(store.staging_uri(NS, "kept"), ONE_TRIPLE)
    build.clear_abox(NS)
    assert store.get_state(NS)["status"] == store.EMPTY
    assert nep.triples(store.staging_uri(NS, "kept"))  # never dropped by the clear itself
    build.drop_graphs(NS)  # namespace deletion
    assert nep.graphs() == []


def test_clear_abox_and_drop_graphs_need_no_lock(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    build.request_build(NS, "deploy")
    assert store.lock_holder(NS) is None and nep.triples(store.graph_uri(NS))
    build.clear_abox(NS)  # the delete hook runs under DEPLOYLOCK, not ABOXLOCK
    assert store.get_state(NS)["status"] == store.EMPTY
    assert nep.triples(store.graph_uri(NS)) == set()

    build.request_build(NS, "redeploy")
    store.put_state(NS, status=store.FAILED, build_id="b1", last_build_id="b2")
    nep.load(store.staging_uri(NS, "b1"), ONE_TRIPLE)
    nep.load(store.staging_uri(NS, "b2"), ONE_TRIPLE)
    build.drop_graphs(NS)
    assert nep.graphs() == []


# ── Fencing ─────────────────────────────────────────────────────────────────


def test_superseded_build_is_retried_on_the_new_version(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    switched = {"done": False}

    def bump():
        if not switched["done"]:
            switched["done"] = True
            _activate(aws, 4)

    monkeypatch.setattr(build, "_default_deps", lambda: deps(on_read=bump))
    build.request_build(NS, "deploy v3")
    assert calls["read"] == 2
    assert store.get_state(NS)["conforms_to_version"] == 4
    assert len(nep.moves()) == 1  # the v3 build never swapped
    assert _staging_graphs(nep) == []


def test_a_version_change_at_the_swap_is_superseded(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())

    def bump_after_the_load(text):
        if "COUNT" in text and calls["read"] == 1:  # the v3 build's staging graph is loaded
            _activate(aws, 4)

    nep.before_query = bump_after_the_load
    build.request_build(NS, "deploy v3")
    assert calls["read"] == 2
    assert store.get_state(NS)["conforms_to_version"] == 4
    assert len(nep.moves()) == 1
    assert (URIRef(m.header_iri(NS)), URIRef(m.CONFORMS_TO_VERSION), Literal("v4")) in nep.triples(store.graph_uri(NS))
    assert _staging_graphs(nep) == []


def test_a_superseded_build_leaves_its_staging_graph_to_the_next_build(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    refused = []

    def bump_after_the_load(text):
        if "COUNT" in text and calls["read"] == 1:
            _activate(aws, 4)

    def refuse_the_superseded_drop(text):
        if text.startswith("DROP") and "/_staging/" in text and nep.gsp_targets and not refused:
            refused.append(text)
            raise httpx.ConnectError("connection reset")

    nep.before_query, nep.before_update = bump_after_the_load, refuse_the_superseded_drop
    build.request_build(NS, "deploy v3")
    assert refused and calls["read"] == 2
    assert store.get_state(NS)["conforms_to_version"] == 4
    assert _staging_graphs(nep) == []  # the v4 build dropped what the superseded v3 build could not


def test_a_superseded_build_never_restores_a_building_status(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    build.request_build(NS, "deploy v3")
    good = store.get_state(NS)
    store.update_state(NS, status=store.BUILDING, build_id="dead")  # a worker died mid-build

    def bump_after_the_load(text):
        if "COUNT" in text and calls["read"] == 2:
            _activate(aws, 4)

    nep.before_query = bump_after_the_load
    real, written = store.put_state, []

    def spy(ns, **fields):
        written.append(fields)
        return real(ns, **fields)

    monkeypatch.setattr(store, "put_state", spy)
    build.request_build(NS, "recover")
    restored = written[0]  # the superseded build's restore; the v4 build then writes READY
    assert (restored["status"], restored["conforms_to_version"]) == (store.READY, 3)
    assert restored["last_build_id"] not in (None, "dead", good["build_id"])
    assert store.get_state(NS)["conforms_to_version"] == 4


def test_a_throttled_requeue_is_retried(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())

    def bump_after_the_load(text):
        if "COUNT" in text and calls["read"] == 1:
            _activate(aws, 4)

    nep.before_query = bump_after_the_load
    real, requests = store.request, []

    def flaky(ns, reason):
        requests.append(reason)
        if len(requests) == 2:  # the superseded build's re-queue
            raise _throttled("UpdateItem")
        return real(ns, reason)

    monkeypatch.setattr(store, "request", flaky)
    build.request_build(NS, "deploy v3")
    assert len(requests) == 3 and calls["read"] == 2
    assert store.get_state(NS)["conforms_to_version"] == 4


def test_lost_lock_never_swaps(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps(on_read=_steal))
    build.request_build(NS, "deploy")
    assert nep.triples(store.graph_uri(NS)) == set()
    assert nep.moves() == []
    assert store.lock_holder(NS) == "someone-else"
    assert store.has_pending(NS)  # re-queued for the holder that took over
    assert _staging_graphs(nep) == []


def test_lock_lost_after_the_load_drops_staging_and_never_swaps(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())

    def steal_on_count(text):
        if "COUNT" in text and store.lock_holder(NS) != "someone-else":
            assert _staging_graphs(nep)  # the staging graph is loaded
            _steal()

    nep.before_query = steal_on_count
    build.request_build(NS, "deploy")
    assert nep.moves() == []
    assert _staging_graphs(nep) == []
    assert nep.triples(store.graph_uri(NS)) == set()


def test_requeues_skip_a_purged_namespace(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)

    def purge_and_steal():
        _steal()
        for sk in ("ACTIVE", store.REQ_SK):  # the namespace purge deletes every row
            aws.table.delete_item(Key=store.ddb_key(NS, sk))

    monkeypatch.setattr(build, "_default_deps", lambda: deps(on_read=purge_and_steal))
    build.request_build(NS, "deploy")
    assert nep.moves() == []
    assert not store.has_pending(NS)


# ── Failures and retries ────────────────────────────────────────────────────


def test_a_short_staging_load_fails_and_drops_staging(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    load = nep.gsp_post
    monkeypatch.setattr(build, "_gsp_post_turtle", lambda g, nt, **_k: load(g, nt.split("\n", 1)[1]))  # loses a triple
    build.request_build(NS, "deploy")
    state = store.get_state(NS)
    assert state["status"] == store.FAILED and "expected" in state["last_error"]
    assert nep.moves() == []
    assert _staging_graphs(nep) == []


def test_failure_keeps_the_previous_abox(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    build.request_build(NS, "deploy")
    good = store.get_state(NS)
    before = nep.triples(store.graph_uri(NS))

    def boom():
        raise RuntimeError("neptune down")

    monkeypatch.setattr(build, "_default_deps", lambda: deps(on_read=boom))
    build.request_build(NS, "documents")
    state = store.get_state(NS)
    assert state["status"] == store.FAILED and "neptune down" in state["last_error"]
    for kept in ("conforms_to_version", "build_id", "counts", "class_counts", "nt_sha256", "mapping_revision"):
        assert state[kept] == good[kept], kept
    assert state["last_build_id"] not in (None, good["build_id"])
    assert nep.triples(store.graph_uri(NS)) == before
    assert store.lock_holder(NS) is None


def test_a_failed_building_write_records_failed(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    real = store.update_state

    def refuse_building(ns, **fields):
        if fields.get("status") == store.BUILDING:
            raise _throttled("UpdateItem")
        return real(ns, **fields)

    monkeypatch.setattr(store, "update_state", refuse_building)
    build.request_build(NS, "deploy")
    state = store.get_state(NS)
    assert state["status"] == store.FAILED and "ThrottlingException" in state["last_error"]
    assert calls["read"] == 0 and nep.moves() == []


def test_transient_step_failures_are_retried(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    failed: set[str] = set()

    def fail_once(kind, error):
        if kind not in failed:
            failed.add(kind)
            raise error

    request = httpx.Request("POST", "https://neptune.example/sparql")

    def before_update(text):
        if text.startswith("DROP SILENT GRAPH"):
            fail_once("drop", httpx.ConnectError("refused", request=request))
        elif text.startswith("MOVE"):
            fail_once("move", httpx.HTTPStatusError("503", request=request, response=httpx.Response(503)))

    def before_query(text):
        if "COUNT" in text:
            fail_once("count", httpx.ReadTimeout("slow", request=request))

    nep.before_update, nep.before_query = before_update, before_query
    post, put_text = nep.gsp_post, store.put_text

    def flaky_post(graph, nt, **kwargs):
        fail_once("post", httpx.ConnectError("reset", request=request))
        return post(graph, nt, **kwargs)

    def flaky_put_text(key, body, content_type="application/json"):
        fail_once("s3", ClientError({"Error": {"Code": "SlowDown", "Message": "slow"}}, "PutObject"))
        return put_text(key, body, content_type)

    monkeypatch.setattr(build, "_gsp_post_turtle", flaky_post)
    monkeypatch.setattr(store, "put_text", flaky_put_text)
    waits = _outwait(monkeypatch, lambda: None)
    build.request_build(NS, "deploy")
    assert failed == {"drop", "move", "count", "post", "s3"}
    assert store.get_state(NS)["status"] == store.READY
    assert len(nep.moves()) == 2 and waits == []  # the refused MOVE (Neptune answered 503) was sent again at once
    assert nep.triples(store.graph_uri(NS))


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("no reply"),
        httpx.WriteError("broken pipe"),
        httpx.ReadError("connection reset"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
    ids=["read-timeout", "write-error", "read-error", "remote-protocol-error"],
)
def test_a_lost_move_reply_after_the_graph_landed(env, monkeypatch, error):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    lost = []

    def lose_the_reply(text):
        if text.startswith("MOVE") and not lost:
            lost.append(text)
            raise error

    nep.after_update = lose_the_reply
    waits = _outwait(monkeypatch, lambda: None)
    build.request_build(NS, "deploy")
    state = store.get_state(NS)
    assert lost and state["status"] == store.READY and len(waits) == 1  # in doubt, so waited out
    assert len(nep.moves()) == 1  # resolved by asking for the header, never sent again
    assert any(q.startswith("ASK") for q in nep.queries)
    assert (URIRef(m.header_iri(NS)), URIRef(m.BUILD_ID), Literal(state["build_id"])) in nep.triples(
        store.graph_uri(NS)
    )


def test_an_unresolved_move_is_checked_again_before_it_is_resent(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    lost, asked = [], []

    def lose_the_reply(text):
        if text.startswith("MOVE") and not lost:
            lost.append(text)
            raise httpx.ReadTimeout("no reply")

    def fail_the_first_ask(text):
        if text.startswith("ASK"):
            asked.append(text)
            if len(asked) == 1:
                raise httpx.ReadTimeout("no reply")

    nep.after_update, nep.before_query = lose_the_reply, fail_the_first_ask
    build.request_build(NS, "deploy")
    state = store.get_state(NS)
    assert state["status"] == store.READY and len(asked) == 2
    assert len(nep.moves()) == 1  # a second MOVE from the emptied staging graph would wipe the ABox
    assert (URIRef(m.header_iri(NS)), URIRef(m.BUILD_ID), Literal(state["build_id"])) in nep.triples(
        store.graph_uri(NS)
    )


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("no connection"),
        httpx.PoolTimeout("no free connection"),
        RuntimeError("AWS credentials not available"),  # signing failed: nothing was sent
    ],
    ids=["connect-error", "connect-timeout", "pool-timeout", "before-sending"],
)
def test_a_move_that_never_arrived_is_sent_again_at_once(env, monkeypatch, error):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    dropped = []

    def drop_the_request(text):
        if text.startswith("MOVE") and not dropped:
            dropped.append(text)
            raise error

    nep.before_update = drop_the_request
    waits = _outwait(monkeypatch, lambda: None)
    build.request_build(NS, "deploy")
    assert dropped and store.get_state(NS)["status"] == store.READY
    assert len(nep.moves()) == 2 and waits == []
    assert nep.triples(store.graph_uri(NS))


def _outwait(monkeypatch, during):
    """Run ``during`` while the build waits out a MOVE in doubt; returns the list of waits."""
    waits, real = [], deploy._outwait_in_flight_move

    def outwait(heartbeat):
        waits.append(heartbeat)
        heartbeat()  # the beat of one wait slice
        during()
        real(heartbeat)

    monkeypatch.setattr(deploy, "_outwait_in_flight_move", outwait)
    return waits


def _hold_the_first_move(nep, also=lambda: None):
    """The first MOVE times out while Neptune keeps running it."""

    def before_update(text):
        if text.startswith("MOVE") and len(nep.moves()) == 1:
            also()
            nep.run_later(text)

    nep.before_update = before_update


def _drop_staging(nep):
    for graph in _staging_graphs(nep):
        nep.ds.update(f"DROP GRAPH <{graph}>")


def test_a_move_still_running_after_its_timeout_is_waited_out_not_resent(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    _hold_the_first_move(nep)
    waits = _outwait(monkeypatch, nep.settle)  # Neptune ends the MOVE within its query timeout, which the wait outlasts
    build.request_build(NS, "deploy")
    state = store.get_state(NS)
    assert waits and state["status"] == store.READY
    assert len(nep.moves()) == 1  # a second MOVE from the emptied staging graph would wipe the ABox
    archived = Graph().parse(data=store.get_text(store.build_key(NS, state["build_id"])), format="nt")
    assert nep.triples(store.graph_uri(NS)) == set(archived)


def _archived(state):
    return set(Graph().parse(data=store.get_text(store.build_key(NS, state["build_id"])), format="nt"))


def _header_build_id(nep):
    return next((str(o) for _, p, o in nep.triples(store.graph_uri(NS)) if str(p) == m.BUILD_ID), None)


def test_a_move_that_returns_is_verified(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    failed = []
    real = store.put_state

    def spy(ns, **fields):
        if fields.get("status") == store.FAILED:
            failed.append(fields["last_error"])
        return real(ns, **fields)

    def empty_the_staging_graph(text):
        if text.startswith("MOVE") and len(nep.moves()) == 1:  # it moves an empty graph and answers 200
            _drop_staging(nep)

    nep.before_update = empty_the_staging_graph
    monkeypatch.setattr(store, "put_state", spy)
    build.request_build(NS, "deploy")
    assert len(failed) == 1 and "lacks" in failed[0]
    # The active graph lost its ABox, so one more build puts one back.
    state = store.get_state(NS)
    assert state["status"] == store.READY and calls["read"] == 2 and len(nep.moves()) == 2
    assert nep.triples(store.graph_uri(NS)) == _archived(state)


def test_a_move_in_doubt_whose_staging_graph_is_gone_is_not_resent(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    failed = []
    real = store.put_state

    def spy(ns, **fields):
        if fields.get("status") == store.FAILED:
            failed.append(fields["last_error"])
        return real(ns, **fields)

    def lose_the_staging_graph_and_the_reply(text):
        if text.startswith("MOVE") and len(nep.moves()) == 1:
            _drop_staging(nep)
            raise httpx.ReadTimeout("no reply")

    nep.before_update = lose_the_staging_graph_and_the_reply
    monkeypatch.setattr(store, "put_state", spy)
    waits = _outwait(monkeypatch, lambda: None)
    build.request_build(NS, "deploy")
    assert len(failed) == 1 and "staging graph" in failed[0]
    assert len(waits) == 1  # given up at once, not retried
    # Moving the missing staging graph would empty the active graph: the first build never resent
    # its MOVE, and one more build rebuilt the ABox.
    assert len(nep.moves()) == 2 and calls["read"] == 2
    assert store.get_state(NS)["status"] == store.READY


def test_staging_is_checked_again_before_a_rolled_back_move_is_resent(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    _hold_the_first_move(nep)
    staging_asks = []

    def fail_the_first_staging_ask(text):
        if text.startswith("ASK") and "/_staging/" in text:
            staging_asks.append(text)
            if len(staging_asks) == 1:
                raise httpx.ReadTimeout("no reply")

    nep.before_query = fail_the_first_staging_ask
    _outwait(monkeypatch, lambda: nep.settle(commit=False))  # Neptune cancels it at its query timeout
    build.request_build(NS, "deploy")
    assert store.get_state(NS)["status"] == store.READY
    assert len(staging_asks) == 2 and len(nep.moves()) == 2  # resent only once staging was seen intact


def test_a_move_that_returned_is_checked_again_without_waiting(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    asked = []

    def fail_the_first_ask(text):
        if text.startswith("ASK"):
            asked.append(text)
            if len(asked) == 1:
                raise httpx.ReadTimeout("no reply")

    nep.before_query = fail_the_first_ask
    waits = _outwait(monkeypatch, lambda: None)
    build.request_build(NS, "deploy")
    assert store.get_state(NS)["status"] == store.READY
    assert len(nep.moves()) == 1 and len(asked) == 2 and waits == []  # it returned, so it is not running


def test_a_swap_out_of_attempts_in_doubt_looks_once_more_before_giving_up(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    _hold_the_first_move(nep)
    asked = []

    def fail_two_asks(text):
        if text.startswith("ASK") and "/_abox/" in text:
            asked.append(text)
            if len(asked) <= 2:  # the checks of attempts 2 and 3
                raise httpx.ReadTimeout("no reply")

    nep.before_query = fail_two_asks
    _outwait(monkeypatch, nep.settle)  # the MOVE commits while it is waited out
    build.request_build(NS, "deploy")
    state = store.get_state(NS)
    assert state["status"] == store.READY and _header_build_id(nep) == state["build_id"]
    assert len(nep.moves()) == 1 and calls["read"] == 1


def test_a_move_still_running_when_the_attempts_run_out_is_waited_out(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    request = httpx.Request("POST", "https://neptune.example/sparql")

    def refuse_twice_then_hold(text):
        if text.startswith("MOVE") and len(nep.moves()) <= 2:
            raise httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request))
        if text.startswith("MOVE"):
            nep.run_later(text)

    nep.before_update = refuse_twice_then_hold
    waits = _outwait(monkeypatch, nep.settle)
    build.request_build(NS, "deploy")
    state = store.get_state(NS)
    assert len(waits) == 1 and not nep.in_flight
    assert state["status"] == store.READY and _header_build_id(nep) == state["build_id"]
    assert len(nep.moves()) == 3


def test_a_failing_heartbeat_does_not_end_a_swap_in_doubt(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    monkeypatch.setattr(build, "BEAT_SECONDS", 0)
    real, failed = store.heartbeat, []

    def throttled_while_running(ns, build_id):
        if nep.in_flight:
            failed.append(len(waits))
            raise _throttled("UpdateItem")
        return real(ns, build_id)

    monkeypatch.setattr(store, "heartbeat", throttled_while_running)
    _hold_the_first_move(nep)
    waits = _outwait(monkeypatch, nep.settle)
    build.request_build(NS, "deploy")
    state = store.get_state(NS)
    assert 1 in failed  # a heartbeat failed during the wait
    assert waits and state["status"] == store.READY and _header_build_id(nep) == state["build_id"]
    assert len(nep.moves()) == 1


def test_a_failing_heartbeat_does_not_fail_the_build(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    monkeypatch.setattr(build, "BEAT_SECONDS", 0)
    real, failed = store.heartbeat, []

    def throttled_until_the_load_is_checked(ns, build_id):
        if not any("COUNT" in q for q in nep.queries):
            failed.append(build_id)
            raise _throttled("UpdateItem")
        return real(ns, build_id)

    monkeypatch.setattr(store, "heartbeat", throttled_until_the_load_is_checked)
    build.request_build(NS, "deploy")
    assert len(failed) > 5 and store.get_state(NS)["status"] == store.READY


def test_failed_heartbeats_are_throttled_too(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    calls = []

    def throttled(ns, build_id):
        calls.append(build_id)
        raise _throttled("UpdateItem")

    monkeypatch.setattr(store, "heartbeat", throttled)
    build.request_build(NS, "deploy")
    # One beat, then one renewal per swap attempt: an outage does not cost a heartbeat per beat() call.
    assert len(calls) == 1 + deploy._STEP_MAX_ATTEMPTS and nep.moves() == []


@pytest.mark.parametrize("recovers", [True, False])
def test_the_swap_renews_the_lease_right_before_the_move(env, monkeypatch, recovers):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    real, real_active, failing = store.heartbeat, registry.get_active, {"on": True, "aged": False}

    def heartbeat(ns, build_id):
        if failing["on"]:
            raise _throttled("UpdateItem")
        return real(ns, build_id)

    def age_the_lease_after_the_load(text):
        if "COUNT" in text and "/_staging/" in text and not failing["aged"]:
            # Heartbeats failed all along: seconds of lease are left when the swap starts.
            _set_heartbeat(aws, time.time() - store.STALE_SECONDS + 5)
            failing["aged"] = True

    def get_active(ns):
        if failing["aged"] and recovers:
            failing["on"] = False  # writes work again from the swap's version check, after the last beat
        return real_active(ns)

    leases = []

    def lease_at_the_move(text):
        if text.startswith("MOVE"):
            item = aws.table.get_item(Key=store.ddb_key(NS, store.LOCK_SK))["Item"]
            leases.append(time.time() - int(item["heartbeat_ts"]))

    monkeypatch.setattr(store, "heartbeat", heartbeat)
    monkeypatch.setattr(registry, "get_active", get_active)
    nep.before_query, nep.before_update = age_the_lease_after_the_load, lease_at_the_move
    build.request_build(NS, "deploy")
    state = store.get_state(NS)
    if recovers:
        # The MOVE goes out with a full lease, so no other worker can take the lock while it is in doubt.
        assert len(leases) == 1 and leases[0] < 5 and state["status"] == store.READY
    else:
        assert leases == [] and state["status"] == store.FAILED  # the lease could not be renewed: never sent


def test_a_build_gives_up_once_its_heartbeats_failed_for_half_the_lease(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    monkeypatch.setattr(build, "BEAT_SECONDS", 0)
    monkeypatch.setattr(build, "_LEASE_GIVE_UP_SECONDS", 0)  # as if half the lease had gone by
    real, workers = store.heartbeat, []

    def failing_for_the_first_worker(ns, build_id):
        if build_id not in workers:
            workers.append(build_id)
        if build_id == workers[0]:
            raise _throttled("UpdateItem")
        return real(ns, build_id)

    monkeypatch.setattr(store, "heartbeat", failing_for_the_first_worker)
    build.request_build(NS, "deploy")
    assert len(workers) == 2 and calls["read"] == 1  # the first build gave up before it read; the next built
    assert store.get_state(NS)["status"] == store.READY and len(nep.moves()) == 1


def test_a_build_left_in_doubt_keeps_its_staging_graph_and_queues_one_more_build(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    _hold_the_first_move(nep)  # Neptune ends it only when the next update arrives

    def no_answer_in_the_first_build(text):
        if text.startswith("ASK") and calls["read"] == 1:
            raise httpx.ReadTimeout("no reply")

    nep.before_query = no_answer_in_the_first_build
    build.request_build(NS, "deploy")
    first_move = nep.moves()[0]
    staging = first_move.split()[2]  # MOVE GRAPH <staging> TO GRAPH <active>
    after = nep.updates[nep.updates.index(first_move) + 1 :]
    # Only the next build drops it (as a leftover); the build that gave up in doubt left it alone.
    assert len([u for u in after if u.startswith("DROP") and staging in u]) == 1
    state = store.get_state(NS)
    assert state["status"] == store.READY and calls["read"] == 2 and not store.has_pending(NS)
    assert _header_build_id(nep) == state["build_id"]


def test_only_one_more_build_is_queued_per_worker_run(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    _hold_the_first_move(nep)

    def no_answer(text):
        if text.startswith("ASK"):
            raise httpx.ReadTimeout("no reply")

    nep.before_query = no_answer
    build.request_build(NS, "deploy")
    assert calls["read"] == 2  # the build and the one it queued
    assert store.get_state(NS)["status"] == store.FAILED
    assert not store.has_pending(NS) and store.lock_holder(NS) is None


def test_a_build_that_settles_gives_the_follow_up_back(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)

    def on_read():
        if calls["read"] == 2:
            store.request(NS, "a mapping edit")  # arrives during the follow-up build

    monkeypatch.setattr(build, "_default_deps", lambda: deps(on_read=on_read))
    _hold_the_first_move(nep)

    def no_answer_in_builds_1_and_3(text):
        if text.startswith("ASK") and "/_abox/" in text and calls["read"] in (1, 3):
            raise httpx.ReadTimeout("no reply")

    nep.before_query = no_answer_in_builds_1_and_3
    build.request_build(NS, "deploy")
    state = store.get_state(NS)
    assert calls["read"] == 4  # build 2 was READY, so unsettled build 3 got its own follow-up
    assert state["status"] == store.READY and _header_build_id(nep) == state["build_id"]


@pytest.mark.parametrize("then", ["next build", "namespace deletion"])
def test_a_staging_graph_kept_before_a_failed_clear_is_still_dropped_later(env, monkeypatch, then):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    build.request_build(NS, "deploy")
    store.update_state(NS, status=store.FAILED, last_build_id="kept")  # a MOVE in doubt left it
    kept = store.staging_uri(NS, "kept")
    nep.load(kept, ONE_TRIPLE)
    registry.reset_active(NS, ontology_id="o")
    refused = []

    def refuse_the_first_clear(text):
        if text.startswith("DROP") and "/_abox/" in text and len(refused) < deploy._STEP_MAX_ATTEMPTS:
            refused.append(text)
            raise httpx.ReadTimeout("no reply")

    nep.before_update = refuse_the_first_clear
    build.request_build(NS, "deleted")  # fails, and its follow-up clears
    assert len(refused) == deploy._STEP_MAX_ATTEMPTS and store.get_state(NS)["status"] == store.EMPTY
    assert nep.triples(kept)  # a clear never drops a staging graph
    if then == "next build":
        _activate(aws, 4)
        build.request_build(NS, "redeploy")
        assert store.get_state(NS)["status"] == store.READY
    else:
        build.drop_graphs(NS)
    assert nep.triples(kept) == set()


def test_a_failed_clear_queues_one_more_build(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    build.request_build(NS, "deploy")
    registry.reset_active(NS, ontology_id="o")
    refused = []

    def refuse_the_first_clear(text):
        if text.startswith("DROP") and "/_abox/" in text and len(refused) < deploy._STEP_MAX_ATTEMPTS:
            refused.append(text)
            raise httpx.ReadTimeout("no reply")

    nep.before_update = refuse_the_first_clear
    build.request_build(NS, "deleted")
    assert len(refused) == deploy._STEP_MAX_ATTEMPTS
    assert store.get_state(NS)["status"] == store.EMPTY and not store.has_pending(NS)
    assert nep.triples(store.graph_uri(NS)) == set()


def test_a_new_version_does_not_stop_a_move_in_doubt_being_resolved(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    _hold_the_first_move(nep, also=lambda: _activate(aws, 4))
    _outwait(monkeypatch, nep.settle)
    build.request_build(NS, "deploy v3")
    state = store.get_state(NS)
    assert (state["status"], state["conforms_to_version"]) == (store.READY, 3)  # it landed; v4's build follows
    assert len(nep.moves()) == 1 and not nep.dropped_after_first_move()


@pytest.mark.parametrize("lands", [True, False])
def test_a_lost_lock_waits_until_the_move_in_doubt_is_resolved(env, monkeypatch, lands):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    monkeypatch.setattr(build, "BEAT_SECONDS", 0)  # the beat between attempts sees the lock gone
    _hold_the_first_move(nep, also=_steal)
    _outwait(monkeypatch, lambda: nep.settle(commit=lands))
    build.request_build(NS, "deploy")
    assert len(nep.moves()) == 1 and store.has_pending(NS)  # re-queued for the holder that took over
    landed = (URIRef(m.header_iri(NS)), URIRef(m.CONFORMS_TO_VERSION), Literal("v3")) in nep.triples(
        store.graph_uri(NS)
    )
    assert landed is lands
    if lands:
        assert not nep.dropped_after_first_move()
        assert store.get_state(NS)["status"] == store.BUILDING  # its READY write was fenced out
    else:
        assert _staging_graphs(nep) == []  # dropped once the MOVE was known to have rolled back


def test_a_throttled_ready_write_is_retried(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    real, throttled = store.put_state, []

    def flaky(ns, **fields):
        if fields.get("status") == store.READY and not throttled:
            throttled.append(fields)
            raise _throttled()
        return real(ns, **fields)

    monkeypatch.setattr(store, "put_state", flaky)
    build.request_build(NS, "deploy")
    state = store.get_state(NS)
    assert throttled and state["status"] == store.READY and "last_error" not in state
    assert len(nep.moves()) == 1


def test_a_ready_write_that_keeps_failing_after_the_swap_still_says_ready(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    real = store.put_state

    def failing(ns, **fields):
        if fields.get("status") == store.READY and "last_error" not in fields:
            raise _throttled()
        return real(ns, **fields)

    monkeypatch.setattr(store, "put_state", failing)
    build.request_build(NS, "deploy")
    state = store.get_state(NS)
    assert state["status"] == store.READY and state["conforms_to_version"] == 3
    assert "READY state write failed" in state["last_error"]
    assert (URIRef(m.header_iri(NS)), URIRef(m.BUILD_ID), Literal(state["build_id"])) in nep.triples(
        store.graph_uri(NS)
    )


def _fail_first(fn, calls):
    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise _throttled("Query")
        return fn(*args, **kwargs)

    return flaky


@pytest.mark.parametrize(
    ("module", "name"),
    [
        (registry, "get_active"),
        (store, "get_state"),
        (storage, "require_version_file"),
        (mapping, "list_rows"),
        (mapping, "persist_status"),
        (mapping, "save_auto"),
    ],
)
def test_a_throttled_read_or_mapping_write_is_retried(env, monkeypatch, module, name):
    aws, _, deps, _ = env
    _activate(aws, 3)
    # A row whose stored status is out of date, so the build persists the recomputed one.
    mapping.save_auto(NS, mapping.MappingRow(mapping.CLASS, ("Vendor",), C, status=mapping.STALE))
    calls = []
    monkeypatch.setattr(module, name, _fail_first(getattr(module, name), calls))
    assert store.acquire_lock(NS, "L")
    assert build.run_build(NS, "L", deps()) == store.READY
    assert len(calls) >= 2


@pytest.mark.parametrize("dep", ["read_lexical", "embed", "embed_query", "search"])
def test_a_throttled_dependency_call_is_retried(env, monkeypatch, dep):
    aws, _, deps, _ = env
    _activate(aws, 3, TTL_WITH_CONTRACT)  # Contract is re-extracted: its query is embedded and searched
    monkeypatch.setattr(matcher, "TOP_K", 1)  # two TBox classes, so ranking them needs embeddings
    d = deps()
    calls = []
    setattr(d, dep, _fail_first(getattr(d, dep), calls))
    assert store.acquire_lock(NS, "L")
    assert build.run_build(NS, "L", d) == store.READY
    assert len(calls) >= 2


def test_a_build_that_cannot_read_the_active_version_records_failed(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    build.request_build(NS, "deploy")
    good = store.get_state(NS)
    real, reads = registry.get_active, []

    def failing(ns):
        reads.append(ns)
        if len(reads) > 1:  # request_build's own check passes; every read in the build fails
            raise _throttled("GetItem")
        return real(ns)

    monkeypatch.setattr(registry, "get_active", failing)
    build.request_build(NS, "documents")
    state = store.get_state(NS)
    assert state["status"] == store.FAILED and "ThrottlingException" in state["last_error"]
    for kept in ("conforms_to_version", "build_id", "counts", "nt_sha256"):
        assert state[kept] == good[kept], kept
    assert len(reads) == 1 + deploy._STEP_MAX_ATTEMPTS and calls["read"] == 1
    assert store.lock_holder(NS) is None


def test_class_prompts_show_sample_values_and_keep_the_hash(env, monkeypatch):
    aws, _, deps, _ = env
    _activate(aws, 3)
    prov = m.Provenance("c1", "ds-1", "raw/a.pdf")
    names = ["Initech", "ACME", "Hooli", "Globex"]
    customers = {f"e{i}": m.LexEntity(f"e{i}", name, "Customer") for i, name in enumerate(names, 1)}
    snapshot = m.LexicalSnapshot(
        {**customers, "o1": m.LexEntity("o1", "PO-1", "Order")}, (m.LexFact("f1", "o1", "PLACED_BY", "e1", prov),)
    )
    prompts = []

    def judge(system, user):
        prompts.append(json.loads(user))
        return _judge(system, user)

    d = deps(snapshot=snapshot)
    d.judge = judge
    monkeypatch.setattr(build, "_default_deps", lambda: d)
    build.request_build(NS, "deploy")
    shown = {it["text"] for p in prompts for it in p["items"] if it["kind"] == mapping.CLASS}
    assert shown == {"Customer (e.g. ACME, Globex, Hooli)", "Order (e.g. PO-1)"}
    [plain] = matcher.class_items(["Customer"], load_tbox(TTL), matcher.Embeddings(_embed))
    row = mapping.get_row(NS, mapping.mapping_id(mapping.CLASS, ("Customer",)))
    assert row.target == C and row.candidate_hash == matcher.candidate_hash(plain.candidates, plain.text)


# ── Recovery and clean-up ───────────────────────────────────────────────────


def test_a_stalled_build_is_recovered_on_read(env, monkeypatch):
    aws, _, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    store.put_state(NS, status=store.BUILDING, build_id="dead")  # worker died; no lock row
    assert build.recover_if_stalled(NS)
    assert store.get_state(NS)["status"] == store.READY and calls["read"] == 1
    assert not build.recover_if_stalled(NS)


def test_a_stalled_build_with_a_stale_lock_is_recovered(env, monkeypatch):
    aws, _, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    assert store.acquire_lock(NS, "dead")  # the worker died holding the lock ...
    aws.table.update_item(  # ... which went stale
        Key=store.ddb_key(NS, store.LOCK_SK),
        UpdateExpression="SET heartbeat_ts = :old",
        ExpressionAttributeValues={":old": 0},
    )
    store.put_state(NS, status=store.BUILDING, build_id="dead")
    assert build.recover_if_stalled(NS)
    assert store.get_state(NS)["status"] == store.READY and calls["read"] == 1
    assert store.lock_holder(NS) is None


def test_leftover_staging_graphs_are_dropped(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    crashed, failed = store.staging_uri(NS, "dead"), store.staging_uri(NS, "older")
    nep.load(crashed, ONE_TRIPLE)
    nep.load(failed, ONE_TRIPLE)
    # A crashed worker left "dead" mid-load; a failed build "older" could not drop its own.
    store.put_state(NS, status=store.BUILDING, build_id="dead", last_build_id="older")
    assert {crashed, failed} <= set(nep.graphs())
    build.request_build(NS, "documents")
    assert nep.triples(crashed) == set() and nep.triples(failed) == set()
    assert _staging_graphs(nep) == []
    assert store.get_state(NS)["status"] == store.READY


# ── Coalescing and the worker ───────────────────────────────────────────────


def test_request_during_a_build_runs_one_more_build(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    once = {"done": False}

    def request_again():
        if not once["done"]:
            once["done"] = True
            store.request(NS, "documents")

    monkeypatch.setattr(build, "_default_deps", lambda: deps(on_read=request_again))
    build.request_build(NS, "deploy")
    assert calls["read"] == 2
    assert len(nep.moves()) == 2


def test_no_second_worker_while_the_lock_is_held(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    assert store.acquire_lock(NS, "running")
    assert build.request_build(NS, "documents")
    assert calls["read"] == 0 and nep.moves() == []
    assert store.has_pending(NS) and store.lock_holder(NS) == "running"  # left for the running build


def test_a_request_just_before_the_release_still_builds(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    real, late = store.release_lock, []

    def release(ns, build_id):
        if not late:  # a request lands after the last take_pending, while the lock is still held
            late.append(build.request_build(NS, "documents"))
        real(ns, build_id)

    monkeypatch.setattr(store, "release_lock", release)
    build.request_build(NS, "deploy")
    assert late == [True]
    assert calls["read"] == 2 and len(nep.moves()) == 2
    assert not store.has_pending(NS) and store.lock_holder(NS) is None


def test_a_worker_exception_does_not_strand_the_request(env, monkeypatch):
    aws, _, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    real, failures = store.take_pending, []

    def flaky(ns, build_id):
        if not failures:
            failures.append(build_id)
            raise _throttled()
        return real(ns, build_id)

    monkeypatch.setattr(store, "take_pending", flaky)
    build.request_build(NS, "deploy")
    assert failures and calls["read"] == 1
    assert store.get_state(NS)["status"] == store.READY
    assert not store.has_pending(NS) and store.lock_holder(NS) is None


def test_a_worker_that_keeps_failing_leaves_the_request_pending(env, monkeypatch):
    aws, _, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    real, attempts = store.take_pending, []

    def failing(ns, build_id):
        attempts.append(build_id)
        raise _throttled()

    monkeypatch.setattr(store, "take_pending", failing)
    build.request_build(NS, "deploy")
    assert len(attempts) == build._MAX_RESTARTS + 1  # bounded, not a hot loop of workers
    assert calls["read"] == 0
    assert store.has_pending(NS) and store.lock_holder(NS) is None
    monkeypatch.setattr(store, "take_pending", real)  # DynamoDB recovers; the next read re-arms the build
    assert build.recover_if_stalled(NS)
    assert store.get_state(NS)["status"] == store.READY and not store.has_pending(NS)


def test_failing_workers_back_off_before_each_restart(env, monkeypatch):
    aws, _, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    monkeypatch.setattr(build, "_RESTART_BACKOFF_SECONDS", 10)
    slept = []
    monkeypatch.setattr(build.time, "sleep", slept.append)

    def failing(ns, build_id):
        raise _throttled()

    monkeypatch.setattr(store, "take_pending", failing)
    build.request_build(NS, "deploy")
    assert slept == [10, 20, 30]  # doubling, capped


# ── Heartbeats ──────────────────────────────────────────────────────────────


def test_heartbeats_are_throttled(env, monkeypatch):
    aws, _, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    real, beats = store.heartbeat, []

    def counted(ns, build_id):
        beats.append(build_id)
        return real(ns, build_id)

    monkeypatch.setattr(store, "heartbeat", counted)
    build.request_build(NS, "a")
    assert len(beats) == 2  # the first beat and the renewal before the MOVE; the rest fall inside BEAT_SECONDS
    monkeypatch.setattr(build, "BEAT_SECONDS", 0)
    build.request_build(NS, "b")
    assert len(beats) > 5


def test_a_long_lexical_read_heartbeats(env, monkeypatch):
    aws, nep, _, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "BEAT_SECONDS", 0)
    given, finished = [], []

    def read(ns, on_progress=None):
        given.append(on_progress)
        _steal()
        if on_progress:
            on_progress()  # after a details batch
        finished.append(ns)
        return _snapshot()

    monkeypatch.setattr(build, "_default_deps", lambda: build.Deps(read_lexical=read, embed=_embed, judge=_judge))
    build.request_build(NS, "deploy")
    assert given[0] is not None and finished == [] and nep.moves() == []


def test_the_archive_step_heartbeats(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    monkeypatch.setattr(build, "BEAT_SECONDS", 0)
    put_text = store.put_text

    def archive_and_steal(key, body, content_type="application/json"):
        put_text(key, body, content_type)
        if key.endswith("/abox.nt"):
            _steal()

    monkeypatch.setattr(store, "put_text", archive_and_steal)
    build.request_build(NS, "deploy")
    assert nep.gsp_targets == [] and nep.moves() == []


def _sequential_build_ids(monkeypatch):
    """Build ids in creation order (real ones made within one second sort at random)."""
    n = iter(range(10**6))
    monkeypatch.setattr(store, "new_build_id", lambda: f"20260101T000000Z-{next(n):08d}")


def _archived_builds(aws):
    page = aws.s3.list_object_versions(Bucket=aws.bucket, Prefix=f"ontologies/{NS}/abox/builds/")
    return sorted({v["Key"].split("/")[-2] for v in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]})


def test_a_ready_build_prunes_the_old_archives(env, monkeypatch):
    aws, _, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    monkeypatch.setattr(store, "ARCHIVES_KEPT", 2)
    _sequential_build_ids(monkeypatch)
    built = []
    for _ in range(3):
        build.request_build(NS, "documents")
        built.append(store.get_state(NS)["build_id"])
    assert _archived_builds(aws) == built[1:]


def test_a_failed_build_keeps_the_live_abox_archive(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    monkeypatch.setattr(store, "ARCHIVES_KEPT", 1)
    _sequential_build_ids(monkeypatch)
    build.request_build(NS, "deploy")
    live = store.get_state(NS)["build_id"]
    load = nep.gsp_post
    monkeypatch.setattr(build, "_gsp_post_turtle", lambda g, nt, **_k: load(g, nt.split("\n", 1)[1]))  # loses a triple
    build.request_build(NS, "documents")  # archived, then fails at the load
    state = store.get_state(NS)
    assert state["status"] == store.FAILED and state["build_id"] == live
    assert _archived_builds(aws) == sorted([live, state["last_build_id"]])


def test_a_failed_prune_does_not_fail_the_build(env, monkeypatch):
    aws, _, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())

    def denied(namespace, current):
        raise RuntimeError("could not delete 1 archive version(s): AccessDenied")

    monkeypatch.setattr(store, "prune_builds", denied)
    build.request_build(NS, "deploy")
    state = store.get_state(NS)
    assert state["status"] == store.READY and "last_error" not in state
    assert store.lock_holder(NS) is None and not store.has_pending(NS)


def test_every_load_batch_heartbeats(env, monkeypatch):
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    monkeypatch.setattr(build, "BEAT_SECONDS", 0)
    monkeypatch.setattr(build, "_LOAD_BATCH_LINES", 1)
    post = nep.gsp_post

    def post_and_steal(graph, nt, **kwargs):
        _steal()
        return post(graph, nt, **kwargs)

    monkeypatch.setattr(build, "_gsp_post_turtle", post_and_steal)
    build.request_build(NS, "deploy")
    assert len(nep.gsp_targets) == 1 and nep.moves() == []


def test_each_mapping_batch_is_saved_while_the_build_holds_the_lock(env, monkeypatch):
    aws, nep, _, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "BEAT_SECONDS", 0)
    monkeypatch.setattr(matcher, "BATCH_SIZE", 1)
    judged = []

    def judge(system, user):
        judged.append(user)
        if len(judged) == 2:  # the lock is taken over while the second class batch is judged
            _steal()
        return _judge(system, user)

    monkeypatch.setattr(build, "_default_deps", lambda: build.Deps(read_lexical=_read, embed=_embed, judge=judge))
    build.request_build(NS, "deploy")
    assert len(judged) == 2
    assert len(mapping.list_rows(NS, mapping.CLASS)) == 1  # the first batch was saved; the second is fenced out
    assert nep.moves() == []


def test_embedding_calls_heartbeat(env, monkeypatch):
    aws, nep, _, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "BEAT_SECONDS", 0)
    monkeypatch.setattr(matcher, "TOP_K", 1)  # two TBox classes, so ranking them needs embeddings
    embedded = []

    def embed(texts):
        embedded.append(texts)
        _steal()
        return _embed(texts)

    monkeypatch.setattr(build, "_default_deps", lambda: build.Deps(read_lexical=_read, embed=embed, judge=_judge))
    build.request_build(NS, "deploy")
    assert len(embedded) == 1 and mapping.list_rows(NS) == [] and nep.moves() == []


def test_chunk_retrieval_heartbeats(env, monkeypatch):
    aws, nep, _, _ = env
    _activate(aws, 3, TTL_WITH_CONTRACT)
    monkeypatch.setattr(build, "BEAT_SECONDS", 0)
    searched, extracted = [], []

    def search(index, vector, k):
        searched.append(index)
        _steal()
        return [{"id": "chunk-1", "value": "ACME signed contract K-1."}]

    monkeypatch.setattr(
        build,
        "_default_deps",
        lambda: build.Deps(
            read_lexical=_read,
            embed=_embed,
            judge=_judge,
            extract_judge=lambda s, u: extracted.append(u) or {"entities": [], "relations": []},
            search=search,
            embed_query=lambda text: [1.0],
        ),
    )
    build.request_build(NS, "deploy")
    assert searched and extracted == [] and nep.moves() == []


def test_reextraction_heartbeats(env, monkeypatch):
    aws, nep, _, _ = env
    _activate(aws, 3, TTL_WITH_CONTRACT)
    monkeypatch.setattr(build, "BEAT_SECONDS", 0)
    chunks = [{"id": f"chunk-{i:02}", "value": "ACME signed contract K-1."} for i in range(4 * extract.WORKERS)]
    extracted = []

    def extract_judge(system, user):
        extracted.append(user)
        if len(extracted) == 1:
            _steal()
        time.sleep(0.05)  # slow enough that the beat after the first chunk stops the rest
        return {"entities": [], "relations": []}

    monkeypatch.setattr(
        build,
        "_default_deps",
        lambda: build.Deps(
            read_lexical=_read,
            embed=_embed,
            judge=_judge,
            extract_judge=extract_judge,
            search=lambda index, vector, k: chunks,
            embed_query=lambda text: [1.0],
        ),
    )
    build.request_build(NS, "deploy")
    assert 0 < len(extracted) < len(chunks) and nep.moves() == []


# ── Namespace teardown ──────────────────────────────────────────────────────


def _writes(aws, nep):
    """Everything a build has written that a namespace teardown must delete after it."""
    page = aws.s3.list_object_versions(Bucket=aws.bucket, Prefix=f"ontologies/{NS}/abox/")
    return {
        "mapping": sorted((r.id, r.status) for r in mapping.list_rows(NS)),
        "s3": sorted(v["Key"] for v in page.get("Versions", [])),
        "posts": len(nep.gsp_targets),
        "moves": len(nep.moves()),
    }


def test_a_teardown_starts_no_build(env, monkeypatch):
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    assert build.stop_builds(NS) == 0  # no build was running
    build.request_build(NS, "documents (ds-1)")  # the deletion pipeline of a deleted document source
    assert calls["read"] == 0 and store.get_state(NS) is None and nep.graphs() == []
    assert store.is_teardown(store.lock_holder(NS))


def test_a_teardown_waits_for_a_running_build_as_long_as_for_a_move_in_doubt(env, monkeypatch):
    monkeypatch.setattr(deploy, "_AMBIGUOUS_MOVE_WAIT_SECONDS", 150)
    assert store.acquire_lock(NS, "b1")
    assert build.stop_builds(NS) == 150


@pytest.mark.parametrize("at", ["lexical read", "re-extraction", "assembly", "archive"])
def test_a_teardown_stops_a_running_build_before_its_next_write(env, monkeypatch, at):
    """Heartbeats are throttled (``BEAT_SECONDS`` is left at its default), so it is the lock check right
    before each write that keeps a build from writing after a teardown took the lock."""
    aws, nep, _, _ = env
    _activate(aws, 3, TTL_WITH_CONTRACT)
    seen: dict = {}

    def stop(point):
        if point == at and not seen:
            build.stop_builds(NS)
            seen.update(_writes(aws, nep))

    def read(ns, on_progress=None):
        stop("lexical read")
        return _snapshot()

    def extract_judge(system, user):
        stop("re-extraction")
        return {"entities": [], "relations": []}

    assemble, put_text = build.assemble, store.put_text

    def assemble_then_stop(*a, **k):
        result = assemble(*a, **k)
        stop("assembly")
        return result

    def put_then_stop(key, body, content_type="application/json"):
        put_text(key, body, content_type)
        if key.endswith("/abox.nt"):
            stop("archive")

    monkeypatch.setattr(build, "assemble", assemble_then_stop)
    monkeypatch.setattr(store, "put_text", put_then_stop)
    monkeypatch.setattr(
        build,
        "_default_deps",
        lambda: build.Deps(
            read_lexical=read,
            embed=_embed,
            judge=_judge,
            extract_judge=extract_judge,
            search=lambda index, vector, k: [{"id": "chunk-1", "value": "ACME signed contract K-1."}],
            embed_query=lambda text: [1.0],
        ),
    )
    build.request_build(NS, "documents (ds-1)")
    assert seen, f"the build never reached the {at}"
    assert _writes(aws, nep) == seen  # nothing landed after the teardown took the lock
    assert store.is_teardown(store.lock_holder(NS)) and store.graph_uri(NS) not in nep.graphs()


def test_the_moves_and_loads_are_sent_once_per_lock_check(env, monkeypatch):
    """Neptune's client retries a write refused for memory on its own, after the build's lock check.

    The build's own retry checks the lock before each attempt instead, so a teardown that took the
    lock gets no write resent after its wait.
    """
    aws, nep, deps, _ = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    build.request_build(NS, "documents")
    assert store.get_state(NS)["status"] == store.READY
    assert {retry for sent, retry in nep.sends if sent.startswith(("MOVE", "POST "))} == {False}
    assert {retry for sent, retry in nep.sends if sent.startswith("DROP")} == {True}  # a drop may be retried


def test_a_request_during_the_namespace_purge_starts_no_build(env, monkeypatch):
    """The purge deletes the partition in SK order: ABOXLOCK comes before ACTIVE and VERSION#. A request
    that came in between would start a build, so the teardown's lock must outlive those rows."""
    aws, nep, deps, calls = env
    _activate(aws, 3)
    monkeypatch.setattr(build, "_default_deps", lambda: deps())
    for i in range(30):  # more rows than one batch of deletes
        mapping.save_auto(NS, mapping.MappingRow(mapping.CLASS, (f"Class{i:02}",), C))
    build.stop_builds(NS)
    client = aws.table.meta.client
    flush = client.batch_write_item
    flushes = []

    def flush_then_request(**kwargs):
        result = flush(**kwargs)
        flushes.append(len(kwargs["RequestItems"][aws.table.name]))
        build.request_build(NS, "documents (ds-1)")  # a document deletion pipeline ends mid-purge
        return result

    monkeypatch.setattr(client, "batch_write_item", flush_then_request)
    dynamo_store.delete_all_namespace_items(NS)
    assert len(flushes) > 1 and calls["read"] == 0
    left = aws.table.query(KeyConditionExpression="PK = :pk", ExpressionAttributeValues={":pk": f"NAMESPACE#{NS}"})
    assert left["Items"] == []
