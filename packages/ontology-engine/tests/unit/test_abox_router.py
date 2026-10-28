# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import base64
import json
import re
from unittest.mock import MagicMock

import pytest
from coa_ontology.abox import assemble, queries, router, store
from coa_ontology.abox import mapping as mp
from coa_ontology.abox import model as m
from coa_ontology.abox.tbox import load_tbox
from coa_ontology.pagination import encode_next_token
from coa_ontology.versions import registry, storage
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import URIRef
from rdflib.namespace import RDF

from .abox_fakes import Neptune

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
o:placedBy a owl:ObjectProperty ; rdfs:label "placed by" ; rdfs:domain o:Order ; rdfs:range o:Customer .
o:total a owl:DatatypeProperty ; rdfs:label "total" ; rdfs:domain o:Order .
"""
C, O = "http://e.com/o#Customer", "http://e.com/o#Order"  # noqa: E741
PLACED, TOTAL = "http://e.com/o#placedBy", "http://e.com/o#total"


@pytest.fixture
def client(versions_aws, monkeypatch):
    versions_aws.s3.put_object(
        Bucket=versions_aws.bucket, Key=storage.version_key(NS, 2, "ontology.ttl"), Body=TTL.encode()
    )
    registry.set_active(NS, version=2, deployment_id="d-2", deployed_by="tester")
    router._tbox.cache_clear()
    requested = MagicMock(return_value=True)
    monkeypatch.setattr(router.build, "request_build", requested)
    app = FastAPI()
    app.include_router(router.router, prefix="/abox")
    app.include_router(router.internal_router, prefix="/internal/abox")
    c = TestClient(app)
    c.requested = requested
    return c


def test_state_view(client):
    assert client.get("/abox", params={"namespace": NS}).json()["status"] == "EMPTY"
    store.put_state(
        NS,
        status=store.READY,
        conforms_to_version=2,
        build_id="b1",
        built_at="t",
        counts={"instances": 1, "relations": 0, "attributes": 0, "evidence": 1},
        class_counts=[{"class_iri": C, "label": "Customer", "count": 1}],
        mapping_revision="r",
    )
    body = client.get("/abox", params={"namespace": NS}).json()
    assert body["current"] is True and body["conformsToVersion"] == 2 and body["activeVersion"] == 2
    assert body["classCounts"] == [{"classIri": C, "label": "Customer", "count": 1}]
    assert body["counts"]["instances"] == 1 and body["pending"] is False


def test_state_view_sweeps_a_stalled_build_but_never_fails_on_it(client, monkeypatch):
    sweep = MagicMock(return_value=True)
    monkeypatch.setattr(router.build, "recover_if_stalled", sweep)
    assert client.get("/abox", params={"namespace": NS}).status_code == 200
    sweep.assert_called_once_with(NS)
    sweep.side_effect = RuntimeError("dynamodb is down")
    assert client.get("/abox", params={"namespace": NS}).json()["status"] == "EMPTY"


def test_mappings_list_update_reset(client):
    row = mp.MappingRow(mp.CLASS, ("Company",), mp.IGNORE)
    mp.save_auto(NS, row)
    listed = client.get("/abox/mappings", params={"namespace": NS}).json()["mappings"]
    assert [(x["id"], x["target"], x["origin"]) for x in listed] == [(row.id, "ignore", "auto")]
    assert client.get("/abox/mappings", params={"namespace": NS, "status": "stale"}).json() == {"mappings": []}
    assert client.get("/abox/mappings", params={"namespace": NS, "kind": "bogus"}).status_code == 422
    r = client.put(
        f"/abox/mappings/{row.id}",
        params={"namespace": NS},
        json={"target": C},
        headers={"X-Coa-Principal": "alice%40example.com"},
    )
    assert r.status_code == 200 and r.json()["origin"] == "manual" and r.json()["updatedBy"] == "alice@example.com"
    assert r.json()["targetLabel"] == "Customer"
    client.requested.assert_called_with(NS, "mapping edit")
    assert (
        client.put(f"/abox/mappings/{row.id}", params={"namespace": NS}, json={"target": PLACED}).status_code == 422
    )  # a property is no class target
    assert (
        client.put(
            f"/abox/mappings/{row.id}", params={"namespace": NS}, json={"target": C, "direction": "inverse"}
        ).status_code
        == 422
    )
    reset = client.delete(f"/abox/mappings/{row.id}", params={"namespace": NS})
    assert reset.json()["origin"] == "auto"
    client.requested.assert_called_with(NS, "mapping reset")
    assert client.put("/abox/mappings/class.zz", params={"namespace": NS}, json={"target": C}).status_code == 404
    assert client.delete("/abox/mappings/class.zz", params={"namespace": NS}).status_code == 404


def test_an_attribute_row_takes_free_but_never_ignore(client):
    row = mp.MappingRow(mp.ATTRIBUTE, (O, "NOTE"), mp.FREE)
    mp.save_auto(NS, row)
    refused = client.put(f"/abox/mappings/{row.id}", params={"namespace": NS}, json={"target": mp.IGNORE})
    assert refused.status_code == 422
    client.requested.assert_not_called()
    assert mp.get_row(NS, row.id).origin == mp.AUTO  # the refused edit wrote nothing
    ok = client.put(f"/abox/mappings/{row.id}", params={"namespace": NS}, json={"target": TOTAL})
    assert ok.status_code == 200 and ok.json()["targetLabel"] == "total"


def test_a_relation_target_must_fit_the_key_classes_in_its_direction(client):
    """The build drops every link the property's domain and range do not admit, so the row would map nothing."""
    row = mp.MappingRow(mp.RELATION, ("PLACED", C, O), mp.IGNORE)
    mp.save_auto(NS, row)
    refused = client.put(f"/abox/mappings/{row.id}", params={"namespace": NS}, json={"target": PLACED})
    assert refused.status_code == 422
    assert refused.json()["detail"] == (
        "placed by has domain Order and range Customer, so it does not fit a relation from Customer to Order. "
        "The inverse direction fits."
    )
    client.requested.assert_not_called()
    assert mp.get_row(NS, row.id).origin == mp.AUTO  # the refused edit wrote nothing
    ok = client.put(
        f"/abox/mappings/{row.id}", params={"namespace": NS}, json={"target": PLACED, "direction": "inverse"}
    )
    assert ok.status_code == 200 and ok.json()["status"] == "active"
    loop = mp.MappingRow(mp.RELATION, ("KNOWS", C, C), mp.IGNORE)
    mp.save_auto(NS, loop)
    neither = client.put(
        f"/abox/mappings/{loop.id}", params={"namespace": NS}, json={"target": PLACED, "direction": "inverse"}
    )
    assert neither.status_code == 422
    assert neither.json()["detail"].endswith("Neither direction fits; pick another property.")


@pytest.mark.parametrize(("name", "call"), [("set_manual", "put"), ("reset_to_auto", "delete")])
def test_a_concurrent_or_vanished_mapping_is_409_or_404(client, monkeypatch, name, call):
    row = mp.MappingRow(mp.CLASS, ("Company",), mp.IGNORE)
    mp.save_auto(NS, row)
    kwargs = {"json": {"target": C}} if call == "put" else {}

    def send():
        return getattr(client, call)(f"/abox/mappings/{row.id}", params={"namespace": NS}, **kwargs)

    monkeypatch.setattr(mp, name, MagicMock(side_effect=mp.MappingConflict("changed")))
    assert send().status_code == 409
    monkeypatch.setattr(mp, name, MagicMock(side_effect=KeyError(row.id)))
    assert send().status_code == 404
    client.requested.assert_not_called()


def test_an_invalid_namespace_is_422_not_500(client):
    for path in ("/abox", "/abox/instances", "/abox/mappings"):
        assert client.get(path, params={"namespace": "../bad"}).status_code == 422
    assert client.post("/abox/rebuild", params={"namespace": "../bad"}).status_code == 422
    client.requested.assert_not_called()


def test_rebuild_and_internal_request(client):
    assert client.post("/abox/rebuild", params={"namespace": NS}).status_code == 202
    client.requested.assert_called_with(NS, "manual rebuild")
    client.requested.return_value = False
    assert client.post("/abox/rebuild", params={"namespace": NS}).status_code == 409
    client.requested.return_value = True
    ok = client.post("/internal/abox/requests", json={"namespace": NS, "reason": "documents", "docSourceId": "ds-1"})
    assert ok.status_code == 202 and ok.json() == {"requested": True}
    client.requested.assert_called_with(NS, "documents (ds-1)")
    public = client.post("/internal/abox/requests", json={"namespace": NS}, headers={"X-Coa-Principal": "x"})
    assert public.status_code == 403
    assert client.post("/internal/abox/requests", json={"namespace": "../bad"}).status_code == 422


def test_the_internal_teardown_stops_builds(client):
    public = client.post("/internal/abox/teardown", json={"namespace": NS}, headers={"X-Coa-Principal": "x"})
    assert public.status_code == 403
    assert client.post("/internal/abox/teardown", json={"namespace": "../bad"}).status_code == 422
    assert store.lock_holder(NS) is None
    ok = client.post("/internal/abox/teardown", json={"namespace": NS})
    assert ok.status_code == 200 and ok.json() == {"stopped": True}
    assert store.is_teardown(store.lock_holder(NS))


def test_instances_and_instance(client, monkeypatch):
    row = {"iri": "urn:i1", "classIri": C, "label": "ACME", "altLabels": [], "evidenceCount": 2}
    monkeypatch.setattr(router.queries, "list_instances", lambda ns, **kw: ([row] * (kw["limit"] + 1), True))
    body = client.get("/abox/instances", params={"namespace": NS, "classIri": C, "maxResults": 1}).json()
    assert len(body["instances"]) == 1 and body["instances"][0]["classLabel"] == "Customer" and body["nextToken"]
    assert client.get("/abox/instances", params={"namespace": NS, "nextToken": "!!"}).status_code == 400
    # The pre-port offset token and an offset that is not a decimal count are refused too.
    legacy = base64.urlsafe_b64encode(json.dumps({"o": 50}).encode()).decode()
    assert client.get("/abox/instances", params={"namespace": NS, "nextToken": legacy}).status_code == 400
    for bad in ("1e999", "-1", "0", " 5"):
        token = encode_next_token({"PK": "offset", "SK": bad}, scope=router.instances_list_scope(NS))
        assert client.get("/abox/instances", params={"namespace": NS, "nextToken": token}).status_code == 400
    monkeypatch.setattr(router.queries, "instance", lambda ns, iri, labels: None)
    assert client.get("/abox/instance", params={"namespace": NS, "iri": "urn:none"}).status_code == 404


def test_a_malformed_iri_is_422(client):
    assert client.get("/abox/instance", params={"namespace": NS, "iri": "not an iri"}).status_code == 422
    assert client.get("/abox/instances", params={"namespace": NS, "classIri": "x> ?s ?p ?o"}).status_code == 422


def test_a_failure_after_the_iri_check_is_not_reported_as_an_invalid_iri(client, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("a bug further down")

    monkeypatch.setattr(router.queries, "instance", broken)
    monkeypatch.setattr(router.queries, "list_instances", broken)
    with pytest.raises(ValueError, match="a bug further down"):
        client.get("/abox/instance", params={"namespace": NS, "iri": "urn:i1"})
    with pytest.raises(ValueError, match="a bug further down"):
        client.get("/abox/instances", params={"namespace": NS, "classIri": C})


def _token(offset):
    return router._encode_offset(NS, offset)


def test_an_instance_token_from_another_namespace_is_400(client, monkeypatch):
    monkeypatch.setattr(router.queries, "list_instances", lambda ns, **kw: ([], False))
    other = router._encode_offset("99999999-2222-3333-4444-555555555555", 50)
    assert client.get("/abox/instances", params={"namespace": NS, "nextToken": other}).status_code == 400


def test_paging_and_search_are_bounded(client, monkeypatch):
    seen = []

    def listing(ns, **kw):
        seen.append(kw)
        return [], False

    monkeypatch.setattr(router.queries, "list_instances", listing)

    def get(**params):
        return client.get("/abox/instances", params={"namespace": NS, **params})

    assert get(nextToken=_token(10_000)).status_code == 200 and seen[-1]["offset"] == 10_000
    assert get(nextToken=_token(10_001)).status_code == 400
    assert get(q="x" * 200).status_code == 200
    assert get(q="x" * 201).status_code == 422
    assert get(classIri="http://e.com/" + "x" * 2048).status_code == 422
    assert get(maxResults=200).status_code == 200
    assert get(maxResults=201).status_code == 422 and get(maxResults=0).status_code == 422
    assert seen[-1]["limit"] == 200
    assert get().status_code == 200 and seen[-1]["limit"] == router.LIST_INSTANCES_DEFAULT_MAX_RESULTS
    assert len(seen) == 4


def test_the_listing_stops_at_the_paging_cap_without_a_token_it_would_refuse(client, monkeypatch):
    row = {"iri": "urn:i1", "classIri": C, "label": "ACME", "altLabels": [], "evidenceCount": 1}
    monkeypatch.setattr(router.queries, "list_instances", lambda ns, **kw: ([row] * (kw["limit"] + 1), True))

    def next_token(token, limit):
        response = client.get("/abox/instances", params={"namespace": NS, "nextToken": token, "maxResults": limit})
        assert response.status_code == 200
        return response.json().get("nextToken")

    assert next_token(_token(9_950), 50) == _token(10_000)  # a page may start at the cap
    assert next_token(_token(10_000), 50) is None  # more instances match, but no page starts past it
    assert next_token(_token(9_990), 30) is None  # 10_020 would be refused
    # Followed to its end, a listing hands out only tokens the API accepts.
    token, pages = _token(9_000), 0
    while token:
        token, pages = next_token(token, 200), pages + 1
    assert pages == 6  # the pages at 9_000, 9_200, …, 10_000


def test_the_status_filter_takes_only_known_statuses(client):
    for key, target, status in (
        ("A", C, mp.ACTIVE),
        ("B", "http://e.com/o#Gone", mp.STALE),
        ("C", mp.IGNORE, mp.IGNORED),
    ):
        mp.save_auto(NS, mp.MappingRow(mp.CLASS, (key,), target, status=status))

    def listed(status):
        response = client.get("/abox/mappings", params={"namespace": NS, "status": status})
        assert response.status_code == 200
        return [x["key"] for x in response.json()["mappings"]]

    assert [listed(s) for s in ("active", "stale", "ignored")] == [[["A"]], [["B"]], [["C"]]]
    assert client.get("/abox/mappings", params={"namespace": NS, "status": "bogus"}).status_code == 422


def test_mappings_are_listed_one_page_at_a_time(client):
    rows = [mp.MappingRow(mp.CLASS, (f"K{i}",), C) for i in range(5)]
    rows.append(mp.MappingRow(mp.CLASS, ("Gone",), "http://e.com/o#Gone", status=mp.STALE))
    for row in rows:
        mp.save_auto(NS, row)

    def pages(**params):
        seen, token = [], None
        while True:
            query = {"namespace": NS, **params, **({"nextToken": token} if token else {})}
            response = client.get("/abox/mappings", params=query)
            assert response.status_code == 200
            body = response.json()
            assert len(body["mappings"]) <= params.get("maxResults", router.LIST_MAPPINGS_DEFAULT_MAX_RESULTS)
            seen.extend(x["id"] for x in body["mappings"])
            token = body.get("nextToken")
            if not token:
                return seen

    every = pages(maxResults=2)
    assert every == sorted(r.id for r in rows)  # mapping-id order, nothing skipped or repeated
    assert pages(maxResults=2, status="active") == sorted(r.id for r in rows[:5])
    assert pages() == every


def test_a_mapping_token_resumes_only_its_own_listing(client):
    for i in range(3):
        mp.save_auto(NS, mp.MappingRow(mp.CLASS, (f"K{i}",), C))
    token = client.get("/abox/mappings", params={"namespace": NS, "maxResults": 1}).json()["nextToken"]
    assert client.get("/abox/mappings", params={"namespace": NS, "nextToken": token}).status_code == 200
    for params in ({"kind": "class"}, {"status": "active"}):
        assert client.get("/abox/mappings", params={"namespace": NS, "nextToken": token, **params}).status_code == 400
    assert client.get("/abox/mappings", params={"namespace": NS, "nextToken": "!!"}).status_code == 400
    for size in (0, 201):
        assert client.get("/abox/mappings", params={"namespace": NS, "maxResults": size}).status_code == 422


def test_a_refused_target_is_not_echoed(client):
    row = mp.MappingRow(mp.CLASS, ("Company",), mp.IGNORE)
    mp.save_auto(NS, row)
    refused = client.put(f"/abox/mappings/{row.id}", params={"namespace": NS}, json={"target": "http://e.com/<probe>"})
    assert refused.status_code == 422 and "probe" not in refused.text
    for target in ("", "http://e.com/" + "x" * 2048):
        assert (
            client.put(f"/abox/mappings/{row.id}", params={"namespace": NS}, json={"target": target}).status_code == 422
        )


def test_a_manual_ignore_is_reported_as_ignored(client):
    row = mp.MappingRow(mp.CLASS, ("Company",), C)
    mp.save_auto(NS, row)
    r = client.put(f"/abox/mappings/{row.id}", params={"namespace": NS}, json={"target": mp.IGNORE})
    assert r.status_code == 200 and (r.json()["status"], r.json()["targetLabel"]) == ("ignored", "ignore")


def test_an_edit_without_an_active_version_is_409(client, monkeypatch):
    row = mp.MappingRow(mp.CLASS, ("Company",), mp.IGNORE)
    mp.save_auto(NS, row)
    monkeypatch.setattr(router.registry, "get_active", lambda namespace: None)
    r = client.put(f"/abox/mappings/{row.id}", params={"namespace": NS}, json={"target": C})
    assert r.status_code == 409
    assert mp.get_row(NS, row.id).origin == mp.AUTO  # nothing was written
    client.requested.assert_not_called()


def test_an_unavailable_snapshot_is_409_before_anything_is_written(client):
    row = mp.MappingRow(mp.CLASS, ("Company",), C)
    mp.save_auto(NS, row)
    mp.set_manual(NS, row.id, C, mp.FORWARD, "alice")
    registry.set_active(NS, version=3, deployment_id="d-3", deployed_by="tester")  # v3 has no ontology.ttl
    p = {"namespace": NS}
    for response in (
        client.get("/abox/instances", params=p),
        client.get("/abox/instance", params={**p, "iri": "urn:i1"}),
        client.get("/abox/mappings", params=p),
        client.put(f"/abox/mappings/{row.id}", params=p, json={"target": C}),
        client.delete(f"/abox/mappings/{row.id}", params=p),
    ):
        assert response.status_code == 409
        assert response.json()["detail"] == "The active ontology version's snapshot is unavailable"
    assert mp.get_row(NS, row.id).origin == mp.MANUAL  # the reset did not run
    client.requested.assert_not_called()


def test_an_incomplete_snapshot_is_409_too(client, monkeypatch):
    def incomplete(namespace, version, name):
        raise storage.SnapshotIncompleteError("no manifest")

    monkeypatch.setattr(router.storage, "require_version_file", incomplete)
    router._tbox.cache_clear()
    assert client.get("/abox/mappings", params={"namespace": NS}).status_code == 409


def test_state_view_of_a_failed_build_is_not_current(client):
    store.put_state(
        NS,
        status=store.FAILED,
        conforms_to_version=1,  # the previous ABox is kept
        build_id="b1",
        built_at="t",
        counts={"instances": 3, "relations": 0, "attributes": 0, "evidence": 3},
        class_counts=[],
        mapping_revision="r",
        last_error="RuntimeError: Neptune refused the load",
    )
    body = client.get("/abox", params={"namespace": NS}).json()
    assert body["status"] == "FAILED" and body["lastError"] == "RuntimeError: Neptune refused the load"
    assert body["current"] is False and body["conformsToVersion"] == 1 and body["activeVersion"] == 2
    assert body["counts"]["instances"] == 3


def test_labels_fall_back_to_the_decoded_iri_tail():
    label = router._labeler(None)
    assert label("http://e.com/o#Purchase%20Order") == "Purchase Order"
    assert label("http://e.com/o/placed%20by/") == "placed by"
    known = router._labeler(load_tbox(TTL))
    assert known(C) == "Customer" and known("http://e.com/o#Gone") == "Gone"


# ── queries ─────────────────────────────────────────────────────────────────


def _prov(chunk):
    return m.Provenance(chunk, "ds-1", f"raw/{chunk}.pdf")


def _lexical():
    entities = {
        "e1": m.LexEntity("e1", "ACME Corp", "Company"),
        "e1b": m.LexEntity("e1b", "acme corp", "Organization"),
        "e2": m.LexEntity("e2", "PO-1", "Purchase Order"),
        "e3": m.LexEntity("e3", "$100", m.LOCAL_ENTITY_CLASS),
        "e5": m.LexEntity("e5", "Zed Ltd", "Company"),
        "e6": m.LexEntity("e6", "Bolt Inc", "Company"),
    }
    facts = (
        m.LexFact("f1", "e1", "PLACED", "e2", _prov("c1")),  # ACME PLACED PO-1, so PO-1 placedBy ACME (inverse)
        m.LexFact("f2", "e1b", "PLACED", "e2", _prov("c3")),
        m.LexFact("f3", "e2", "TOTAL", "e3", _prov("c1")),  # mapped to total
        m.LexFact("f4", "e2", "NOTE", "e3", _prov("c2")),  # kept as a free attribute
        m.LexFact("f5", "e5", "EXISTS", None, _prov("c5")),
        m.LexFact("f6", "e6", "EXISTS", None, _prov("c6")),
    )
    return m.LexicalSnapshot(entities, facts)


@pytest.fixture
def abox(monkeypatch):
    monkeypatch.delenv("NDB_GRAPH_URI_BASE", raising=False)
    monkeypatch.delenv("NDB_GRAPH_URI", raising=False)
    tbox = load_tbox(TTL)
    rows = mp.effective(
        [
            mp.MappingRow(mp.CLASS, ("Company",), C),
            mp.MappingRow(mp.CLASS, ("Organization",), C),
            mp.MappingRow(mp.CLASS, ("Purchase Order",), O),
            mp.MappingRow(mp.RELATION, ("PLACED", C, O), PLACED, direction=mp.INVERSE),
            mp.MappingRow(mp.ATTRIBUTE, (O, "TOTAL"), TOTAL),
            mp.MappingRow(mp.ATTRIBUTE, (O, "NOTE"), mp.FREE),
        ],
        tbox,
    )
    graph, _ = assemble.assemble(
        NS, tbox, _lexical(), {r.id: r for r in rows}, ([], []), version=2, mapping_revision="rev"
    )
    nep = Neptune()
    nep.load(store.graph_uri(NS), graph.serialize(format="turtle"))
    monkeypatch.setattr(queries, "_sparql_query", nep.query)
    monkeypatch.setattr(
        queries, "chunk_texts", lambda ns, ids: {c: f"text of {c} " + "x" * 2000 for c in ids if c != "c3"}
    )
    return nep


def _label(iri):
    return iri.rsplit("#", 1)[-1]


def test_list_instances_paginates_filters_and_counts_evidence(abox):
    page, more = queries.list_instances(NS, limit=2)
    assert [r["label"] for r in page] == ["ACME Corp", "Bolt Inc"] and more is True  # case-insensitive label order
    assert page[0]["altLabels"] == ["acme corp"] and page[0]["classIri"] == C
    assert page[0]["evidenceCount"] == 2 and page[1]["evidenceCount"] == 1  # one node per supporting chunk
    rest, more = queries.list_instances(NS, limit=2, offset=2)
    assert [r["label"] for r in rest] == ["PO-1", "Zed Ltd"] and more is False
    orders, _ = queries.list_instances(NS, class_iri=O)
    assert [r["label"] for r in orders] == ["PO-1"]
    found, _ = queries.list_instances(NS, q="ZED")
    assert [r["label"] for r in found] == ["Zed Ltd"]
    assert queries.list_instances(NS, q="no such thing") == ([], False)
    # Only instances: no evidence nodes, the header or the free-attribute definitions.
    every, _ = queries.list_instances(NS, limit=50)
    assert len(every) == 4


def test_instance_detail(abox):
    po = m.instance_iri(NS, O, "PO-1")
    acme = m.instance_iri(NS, C, "ACME Corp")
    detail = queries.instance(NS, po, _label)
    assert detail is not None
    assert detail["classes"] == [{"iri": O, "label": "Order"}] and detail["label"] == "PO-1"
    by_property = {a["property"]: a for a in detail["attributes"]}
    free = m.free_attribute_iri(NS, "NOTE")
    assert set(by_property) == {TOTAL, free}
    assert by_property[TOTAL]["value"] == "$100" and by_property[TOTAL]["free"] is False
    assert by_property[free]["label"] == "NOTE" and by_property[free]["free"] is True
    assert by_property[TOTAL]["evidence"] == ["c1"] and by_property[free]["evidence"] == ["c2"]
    (out,) = detail["outgoing"]
    assert out["property"] == PLACED and out["target"] == {"iri": acme, "label": "ACME Corp", "classIri": C}
    assert out["evidence"] == ["c1", "c3"] and detail["incoming"] == []
    chunks = {c["chunkId"]: c for c in detail["evidence"]}
    assert set(chunks) == {"c1", "c2", "c3"}
    assert chunks["c1"]["docSourceId"] == "ds-1" and chunks["c1"]["sourceKey"] == "raw/c1.pdf"
    assert chunks["c1"]["text"].startswith("text of c1") and len(chunks["c1"]["text"]) == 1000  # clipped
    assert chunks["c3"]["text"] == ""  # a chunk the lexical graph no longer has
    assert detail["truncated"] == {"incoming": False, "evidence": False}
    # The label lookup is limited to the predicates the instance uses; the evidence reads are DISTINCT.
    (free_lookup,) = [q for q in abox.queries if f"?p a <{m.FREE_ATTRIBUTE}>" in q]
    assert "VALUES ?p" in free_lookup and f"<{TOTAL}>" in free_lookup and f"<{free}>" in free_lookup
    assert [q for q in abox.queries if "UNION" in q]
    assert all(q.startswith("SELECT DISTINCT") for q in abox.queries if "UNION" in q)

    abox.queries.clear()
    other = queries.instance(NS, acme, _label)
    assert other is not None and other["altLabels"] == ["acme corp"]
    assert other["outgoing"] == [] and other["attributes"] == []
    assert other["incoming"] == [
        {"property": PLACED, "label": "placedBy", "source": {"iri": po, "label": "PO-1", "classIri": O}}
    ]
    assert {c["chunkId"] for c in other["evidence"]} == {"c1", "c3"}  # evidence of facts it is the object of
    assert other["truncated"] == {"incoming": False, "evidence": False}
    assert not [q for q in abox.queries if f"?p a <{m.FREE_ATTRIBUTE}>" in q]  # no attribute values, nothing to label


def test_instance_ignores_nodes_that_are_not_instances(abox):
    graph = abox.ds.graph(URIRef(store.graph_uri(NS)))
    evidence = next(graph.subjects(RDF.type, URIRef(m.EVIDENCE)))
    for iri in (str(evidence), m.header_iri(NS), m.free_attribute_iri(NS, "NOTE"), "urn:nobody"):
        assert queries.instance(NS, iri, _label) is None


def test_q_is_escaped(abox):
    abox.load(
        store.graph_uri(NS),
        f'<urn:q:1> a <{C}> ; <http://www.w3.org/2000/01/rdf-schema#label> "Say \\"hi\\" \\\\ there" .',
    )
    assert [r["label"] for r in queries.list_instances(NS, q='say "hi"')[0]] == ['Say "hi" \\ there']
    assert [r["label"] for r in queries.list_instances(NS, q="\\ there")[0]] == ['Say "hi" \\ there']
    assert len(queries.list_instances(NS, limit=50)[0]) == 5
    for payload in ('") || true || ("', '"))) } UNION { ?s ?p ?o } #', 'x" || true || "', '\\" || true || "', "\\\\"):
        assert queries.list_instances(NS, q=payload) == ([], False), payload


# ── the detail of a busy instance is bounded ────────────────────────────────

HUB = "http://e.com/d/hub"
PREFIXES = f"@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> . @prefix coa: <{m.COA}> .\n"


def _evidence_node(name, chunk, about=HUB, predicate=None, obj=None):
    predicate = predicate or queries.RDF_TYPE
    return (
        f"<http://e.com/d/{name}> a coa:Evidence ; coa:about <{about}> ; coa:predicate <{predicate}> ; "
        f'coa:object <{obj or C}> ; coa:chunkId "{chunk}" ; coa:docSourceId "ds-1" ; coa:sourceKey "raw/{chunk}.pdf" .'
    )


def _busy(monkeypatch, incoming, chunks):
    """A graph where the hub is the object of ``incoming`` relations read from its ``chunks`` supporting chunks.

    As in an assembled ABox, each relation has an evidence node, which cites one of those chunks.
    """
    assert chunks or not incoming  # a relation is always read from some chunk
    monkeypatch.delenv("NDB_GRAPH_URI_BASE", raising=False)
    monkeypatch.delenv("NDB_GRAPH_URI", raising=False)
    lines = [f'<{HUB}> a <{C}> ; rdfs:label "Hub" .']
    # Created in reverse so that the cap cannot be satisfied by insertion order.
    for i in reversed(range(incoming)):
        source = f"http://e.com/d/s{i:03d}"
        lines.append(f'<{source}> a <{O}> ; rdfs:label "S{i:03d}" ; <{PLACED}> <{HUB}> .')
        lines.append(_evidence_node(f"link{i:03d}", f"c{i % chunks:03d}", about=source, predicate=PLACED, obj=HUB))
    lines += [_evidence_node(f"ev{i:03d}", f"c{i:03d}") for i in reversed(range(chunks))]
    nep = Neptune()
    nep.load(store.graph_uri(NS), PREFIXES + "\n".join(lines))
    asked = []

    def texts(ns, ids):
        asked.append(list(ids))
        return {c: f"text of {c}" for c in ids}

    monkeypatch.setattr(queries, "_sparql_query", nep.query)
    monkeypatch.setattr(queries, "chunk_texts", texts)
    return asked


@pytest.mark.parametrize(
    ("incoming", "chunks", "truncated"),
    [(200, 50, False), (201, 51, True), (3, 2, False), (0, 0, False)],
)
def test_the_detail_of_a_busy_instance_is_capped(monkeypatch, incoming, chunks, truncated):
    asked = _busy(monkeypatch, incoming, chunks)
    detail = queries.instance(NS, HUB, _label)
    assert detail["truncated"] == {"incoming": truncated, "evidence": truncated}
    shown_in, shown_ev = min(incoming, 200), min(chunks, 50)
    # The first by source IRI / chunk id: the same page every time.
    assert [link["source"]["iri"] for link in detail["incoming"]] == [
        f"http://e.com/d/s{i:03d}" for i in range(shown_in)
    ]
    assert [e["chunkId"] for e in detail["evidence"]] == [f"c{i:03d}" for i in range(shown_ev)]
    assert all(e["text"] == f"text of {e['chunkId']}" for e in detail["evidence"])
    assert [ids for ids in asked if ids] == ([[e["chunkId"] for e in detail["evidence"]]] if chunks else [])
    assert all(len(ids) <= queries.MAX_CHUNKS for ids in asked)  # text is read only for the shown chunks


def test_incoming_links_are_read_through_their_evidence_nodes(abox):
    """No default Neptune index serves a pattern that binds only the object: the lookup starts at the evidence."""
    po, acme = m.instance_iri(NS, O, "PO-1"), m.instance_iri(NS, C, "ACME Corp")
    abox.queries.clear()
    detail = queries.instance(NS, acme, _label)
    assert [(link["property"], link["source"]["iri"]) for link in detail["incoming"]] == [(PLACED, po)]
    assert not [q for q in abox.queries if re.search(rf"\?\w+ \?\w+ <{re.escape(acme)}>", q)]
    assert [q for q in abox.queries if f"?e <{m.OBJECT}> <{acme}> ; <{m.ABOUT}> ?s ; <{m.PREDICATE}> ?p" in q]


def test_a_self_link_lists_its_chunk_once(monkeypatch):
    _busy(monkeypatch, 0, 0)
    nep = Neptune()
    nep.load(
        store.graph_uri(NS),
        PREFIXES
        + f'<{HUB}> a <{O}> ; rdfs:label "Hub" ; <{PLACED}> <{HUB}> .\n'
        + _evidence_node("ev", "c-self", predicate=PLACED, obj=HUB),
    )
    monkeypatch.setattr(queries, "_sparql_query", nep.query)
    detail = queries.instance(NS, HUB, _label)
    (out,) = detail["outgoing"]
    assert out["evidence"] == ["c-self"]  # the evidence node matches both sides of the UNION
    assert [e["chunkId"] for e in detail["evidence"]] == ["c-self"]
