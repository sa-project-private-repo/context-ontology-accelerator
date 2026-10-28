# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import pytest
from coa_ontology.abox import lexical
from coa_ontology.abox.model import CURRENT_VALID_TO, LOCAL_ENTITY_CLASS, LexEntity

pytestmark = pytest.mark.unit


class FakeStore:
    def __init__(self, pages, details):
        self.pages = pages  # list of id lists
        self.details = details  # fid -> rows
        self.queries = []

    def _label(self, base):
        return f"`__{base}__abc__`"

    def _oc_query(self, cypher, params=None):
        self.queries.append((cypher, params))
        if "RETURN id(f) AS fid ORDER BY" in cypher:
            return [{"fid": f} for f in (self.pages.pop(0) if self.pages else [])]
        assert params["current"] == CURRENT_VALID_TO
        return [row for fid in params["ids"] for row in self.details.get(fid, [])]


def _row(fid, sid, sval, scls, rel, oid, oval, ocls, cid, ds="ds-1", key="raw/a.pdf"):
    return {
        "fid": fid,
        "sid": sid,
        "sval": sval,
        "scls": scls,
        "rel": rel,
        "oid": oid,
        "oval": oval,
        "ocls": ocls,
        "cid": cid,
        "ds": ds,
        "skey": key,
    }


def test_reads_entities_facts_and_provenance_paged(monkeypatch):
    monkeypatch.setattr(lexical, "PAGE_SIZE", 2)
    store = FakeStore(
        pages=[["f1", "f2"], ["f3"]],
        details={
            "f1": [
                _row("f1", "e1", "ACME", "Company", "PLACED", "e2", "PO-1", "Order", "c1"),
                _row("f1", "e1", "ACME", "Company", "PLACED", "e2", "PO-1", "Order", "c1"),
            ],  # duplicate path
            "f2": [_row("f2", "e1", "ACME", "Company", "PHONE", "e3", "555", LOCAL_ENTITY_CLASS, "c2")],
            "f3": [_row("f3", "e4", None, "Company", "X", None, None, None, "c3")],  # no subject value: skipped
        },
    )
    snap = lexical.read_lexical("ns", store=store)
    assert set(snap.entities) == {"e1", "e2", "e3"}
    assert snap.entities["e3"].cls == LOCAL_ENTITY_CLASS
    assert [(f.id, f.relation, f.object_id, f.prov.chunk_id) for f in snap.facts] == [
        ("f1", "PLACED", "e2", "c1"),
        ("f2", "PHONE", "e3", "c2"),
    ]
    assert snap.facts[0].prov.doc_source_id == "ds-1" and snap.facts[0].prov.source_key == "raw/a.pdf"
    assert store.queries[2][1]["after"] == "f2"  # paging cursor
    assert len(store.queries) == 4  # a short last page ends the scan: no trailing empty-page query


def test_details_are_fetched_in_batches_and_progress_is_reported(monkeypatch):
    monkeypatch.setattr(lexical, "BATCH_SIZE", 2)
    ids = [f"f{i}" for i in range(5)]
    store = FakeStore(
        pages=[ids],
        details={f: [_row(f, "e1", "ACME", "Company", "PLACED", "e2", "PO", "Order", f"c-{f}")] for f in ids},
    )
    ticks = []
    snap = lexical.read_lexical("ns", store=store, on_progress=lambda: ticks.append(len(store.queries)))
    assert [len(q[1]["ids"]) for q in store.queries[1:]] == [2, 2, 1]
    assert ticks == [1, 2, 3, 4]  # after the ids query, before the first details batch, then after each batch
    assert len(snap.facts) == 5


def test_a_full_page_asks_for_the_next_one(monkeypatch):
    monkeypatch.setattr(lexical, "PAGE_SIZE", 1)
    store = FakeStore(pages=[["f1"], []], details={"f1": [_row("f1", "e1", "A", "C", "R", "e2", "B", "C", "c1")]})
    lexical.read_lexical("ns", store=store)
    assert [q[1].get("after") for q in store.queries if "after" in (q[1] or {})] == ["", "f1"]


def test_attribute_fact_without_a_local_entity_gets_a_synthetic_complement():
    # graphrag only writes the complement entity (and its __OBJECT__ edge) when include_local_entities is set,
    # which is off by default: the value has to come from the fact text.
    store = FakeStore(
        pages=[["f1", "f2"]],
        details={
            "f1": [dict(_row("f1", "e1", "ACME", "Company", None, None, None, None, "c1"), fval="ACME PHONE 555 0100")],
            "f2": [dict(_row("f2", "e1", "ACME", "Company", None, None, None, None, "c2"), fval="ACME HEADQUARTERED")],
        },
    )
    snap = lexical.read_lexical("ns", store=store)
    assert snap.entities["f1#c"] == LexEntity("f1#c", "555 0100", LOCAL_ENTITY_CLASS)
    assert [(f.id, f.relation, f.object_id) for f in snap.facts] == [
        ("f1", "PHONE", "f1#c"),
        ("f2", "HEADQUARTERED", None),  # nothing after the predicate: no complement to keep
    ]


def test_result_does_not_depend_on_row_order_and_keeps_every_subject_and_object():
    rows = [
        _row("f1", "e1", "ACME", "Company", "PLACED", "e2", "PO-1", "Order", "c1"),
        _row("f1", "e5", "ACME", "Organization", "PLACED", "e2", "PO-1", "Order", "c1"),
        _row("f1", "e1", "ACME", "Company", "PLACED", "e6", "PO-1", "Purchase", "c1"),
        _row("f1", "e1", "ACME", "Company", "PLACED", "e2", "PO-1", "Order", "c9"),
    ]

    def read(order):
        return lexical.read_lexical("ns", store=FakeStore(pages=[["f1"]], details={"f1": order}))

    a, b = read(rows), read(list(reversed(rows)))
    assert a.facts == b.facts and a.entities == b.entities
    assert {(f.subject_id, f.object_id, f.prov.chunk_id) for f in a.facts} == {
        ("e1", "e2", "c1"),
        ("e5", "e2", "c1"),
        ("e1", "e6", "c1"),
        ("e1", "e2", "c9"),
    }
    assert set(a.entities) == {"e1", "e5", "e2", "e6"}


def test_a_complement_does_not_depend_on_row_order():
    # Two subject edges of one fact split its text differently, so they disagree on the complement.
    fval = "ACME Corp PHONE 555 0100"
    rows = [
        dict(_row("f1", "e1", "ACME Corp", "Company", "PHONE", None, None, None, "c1"), fval=fval),
        dict(_row("f1", "e5", "ACME", "Company", "PHONE", None, None, None, "c1"), fval=fval),
    ]

    def read(order):
        return lexical.read_lexical("ns", store=FakeStore(pages=[["f1"]], details={"f1": order}))

    a, b = read(rows), read(list(reversed(rows)))
    assert a.entities == b.entities and a.facts == b.facts
    assert a.entities["f1#c"] == LexEntity("f1#c", "555 0100", LOCAL_ENTITY_CLASS)  # the smallest one


def test_chunk_texts():
    class S(FakeStore):
        def _oc_query(self, cypher, params=None):
            return [{"id": i, "text": f"text {i}"} for i in params["ids"]]

    assert lexical.chunk_texts("ns", ["c1", "c2"], store=S([], {})) == {"c1": "text c1", "c2": "text c2"}


def test_relation_is_recovered_from_the_fact_text_when_graphrag_left_it_unset():
    # graphrag-lexical-graph 3.18.7 runs ``SET fact.relation = params.p`` but never passes ``p``,
    # so the property is absent in practice and the predicate has to come from ``Fact.value``.
    store = FakeStore(
        pages=[["f1", "f2"], []],
        details={
            "f1": [
                dict(_row("f1", "e1", "ACME", "Company", None, "e2", "PO-1", "Order", "c1"), fval="ACME PLACED PO-1")
            ],
            "f2": [dict(_row("f2", "e1", "ACME", "Company", None, None, None, None, "c2"), fval="ACME PHONE 555")],
        },
    )
    snap = lexical.read_lexical("ns", store=store)
    assert [(f.id, f.relation, f.object_id) for f in snap.facts] == [("f1", "PLACED", "e2"), ("f2", "PHONE", "f2#c")]


def test_chunk_texts_are_read_in_batches(monkeypatch):
    monkeypatch.setattr(lexical, "BATCH_SIZE", 2)
    seen = []

    class S(FakeStore):
        def _oc_query(self, cypher, params=None):
            seen.append(params["ids"])
            return [{"id": i, "text": f"text {i}"} for i in params["ids"]]

    got = lexical.chunk_texts("ns", ["c3", "c1", "c2", "c1", "c5"], store=S([], {}))
    assert seen == [["c1", "c2"], ["c3", "c5"]]
    assert got == {f"c{i}": f"text c{i}" for i in (1, 2, 3, 5)}


def test_endpoint_is_required(monkeypatch):
    monkeypatch.delenv("NEPTUNE_ENDPOINT", raising=False)
    monkeypatch.delenv("NDB_ENDPOINT", raising=False)
    with pytest.raises(RuntimeError, match="NEPTUNE_ENDPOINT"):
        lexical._endpoint()
    monkeypatch.setenv("NDB_ENDPOINT", "https://db.example.com:8183/")
    assert lexical._endpoint() == ("db.example.com", 8183)
