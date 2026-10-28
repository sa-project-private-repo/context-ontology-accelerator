# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Agent change sets: propose, preview, apply (all or some), discard."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from coa_ontology import dynamo_store
from coa_ontology.studio import changes, changesets, messages
from coa_ontology.studio import draft as store
from coa_ontology.studio.ops import mentioned
from pydantic import ValidationError

from .studio_fakes import CUSTOMER, EMAIL, ITEM, NS, ONTO, ORDER, PARTY, VIP, opened_draft, stored_row, stored_state

pytestmark = [pytest.mark.unit]
P = "p-1"
SUPPLIER = f"{ONTO}Supplier"
ADD_SUPPLIER = {"op": "add_class", "label": "Supplier", "parents": [PARTY], "iri": SUPPLIER}
SUPPLIES = {"op": "add_object_property", "label": "supplies", "domain": SUPPLIER, "range": ITEM}
RELABEL = {"op": "set_label", "iri": ORDER, "label": "Purchase order"}
WHY = "Contracts name suppliers of items"


@pytest.fixture(autouse=True)
def _env(versions_aws):
    store.clear_cache()
    with patch("coa_ontology.proposals._generate_schema_sql", return_value=None):
        opened_draft(P)
        yield versions_aws
    store.clear_cache()


def _propose(ops: list[dict], rationale: str = WHY) -> dict:
    return changesets.propose(NS, P, ops, rationale, turn_id="t-1", author="alice")


def _apply(cs: dict, expected: int, op_indexes: list[int] | None = None) -> dict:
    return changesets.apply(NS, P, cs["changeSetId"], expected, op_indexes, author="bob")


def test_propose_dry_runs_and_stores_a_pending_change_set():
    cs = _propose([ADD_SUPPLIER, SUPPLIES])
    assert cs["status"] == changesets.PENDING and cs["createdRevision"] == 0 and cs["createdBy"] == "alice"
    assert cs["rationale"] == WHY and cs["turnId"] == "t-1"
    assert cs["summary"] == ["Added class Supplier", "Added relationship supplies from Supplier to Item"]
    assert cs["ops"][0] == ADD_SUPPLIER and cs["ops"][1]["op"] == "add_object_property"
    assert cs["diffCounts"]["classes"]["added"] == 1 and cs["diffCounts"]["objectProperties"]["added"] == 1
    assert cs["appliedRevision"] is None and cs["stale"] is None
    assert stored_state().revision == 0  # proposing changes nothing
    assert not mentioned(store.load_revision(NS, P, 0).draft, SUPPLIER)
    assert changesets.get_change_set(NS, P, cs["changeSetId"]) == cs


def test_propose_refuses_ops_that_do_not_apply_with_the_op_index():
    with pytest.raises(changes.InvalidChange) as raised:
        _propose([ADD_SUPPLIER, {"op": "add_parent", "iri": CUSTOMER, "parent": f"{ONTO}Nowhere"}])
    assert raised.value.detail()["opIndex"] == 1
    assert changesets.list_change_sets(NS, P, limit=20)["changeSets"] == []


def test_propose_refuses_too_many_ops_a_missing_rationale_and_malformed_ops():
    with patch.object(changesets, "MAX_AGENT_OPS", 1), pytest.raises(changes.InvalidChange):
        _propose([ADD_SUPPLIER, SUPPLIES])
    with pytest.raises(changes.InvalidChange):
        _propose([ADD_SUPPLIER], rationale="  ")
    with pytest.raises(changes.InvalidChange):
        _propose([])
    with pytest.raises(ValidationError):
        _propose([{"op": "add_class"}])
    with pytest.raises(ValidationError):
        _propose([{"op": "restore_triples"}])  # the internal inverse is never proposed


RETYPE_TOTAL = {"op": "replace_datatype", "from": "xsd:decimal", "to": "xsd:double"}
TOTAL_REFUSED = (
    "xsd:decimal cannot be replaced with xsd:double: order_total is mapped from column total, typed decimal; "
    "its datatype follows the column."
)


def test_a_change_set_that_retypes_a_mapped_column_is_refused_at_that_op():
    with pytest.raises(changes.InvalidChange) as raised:
        _propose([RELABEL, RETYPE_TOTAL])
    assert raised.value.detail()["opIndex"] == 1 and raised.value.message == f"Op 1: {TOTAL_REFUSED}"
    assert changesets.list_change_sets(NS, P, limit=20)["changeSets"] == []
    # A change set stored before the refusal existed shows it on that op, and applies only without it.
    cs = _propose([RELABEL])
    dynamo_store._get_table().update_item(
        Key=changesets._key(NS, P, cs["changeSetId"]),
        UpdateExpression="SET ops = :ops, summary = :summary",
        ExpressionAttributeValues={
            ":ops": [RELABEL, RETYPE_TOTAL],
            ":summary": [*cs["summary"], "Replaced the datatype xsd:decimal with xsd:double"],
        },
    )
    preview = changesets.preview(NS, P, cs["changeSetId"], None, None)
    assert not preview["applies"] and preview["error"] == {"message": TOTAL_REFUSED, "opIndex": 1}
    with pytest.raises(changes.InvalidChange) as raised:
        _apply(cs, 0)
    assert raised.value.detail()["opIndex"] == 1 and stored_state().revision == 0
    assert changesets.get_change_set(NS, P, cs["changeSetId"])["stale"] is None
    assert _apply(cs, 0, [0])["changeSet"]["appliedOpIndexes"] == [0]


def test_apply_writes_an_agent_revision_and_marks_it_applied():
    cs = _propose([ADD_SUPPLIER, SUPPLIES])
    result = _apply(cs, 0)
    assert result["revision"] == 1 and result["canUndo"] and SUPPLIER in result["diff"]["classes"]["added"]
    applied = result["changeSet"]
    assert (applied["status"], applied["appliedRevision"], applied["appliedOpIndexes"], applied["appliedBy"]) == (
        changesets.APPLIED,
        1,
        [0, 1],
        "bob",
    )
    record = store.get_record(NS, P, 1)
    assert (record["source"], record["rationale"], record["changeSetId"], record["opIndexes"]) == (
        "agent",
        WHY,
        cs["changeSetId"],
        [0, 1],
    )
    assert record["author"] == "bob" and record["ops"][1]["iri"]  # resolved: the minted property IRI is stored
    assert stored_state().applied == (1,) and stored_row()["status"] == "updated"
    changes.undo(NS, P, 1, author="bob")  # an applied agent change set undoes like any other
    assert not mentioned(store.load_revision(NS, P, 2).draft, SUPPLIER)


def test_a_partial_apply_applies_only_the_selected_ops_in_order():
    cs = _propose([RELABEL, ADD_SUPPLIER, SUPPLIES])
    result = _apply(cs, 0, [2, 1])
    assert result["changeSet"]["appliedOpIndexes"] == [1, 2]
    draft = store.load_revision(NS, P, 1).draft
    assert mentioned(draft, SUPPLIER)
    assert store.get_record(NS, P, 1)["summary"] == cs["summary"][1:]


def test_an_applied_change_set_gives_the_history_lines_of_the_ops_it_applied_as_codes():
    cs = _propose([RELABEL, ADD_SUPPLIER, SUPPLIES])
    _apply(cs, 0, [1])
    record = store.get_record(NS, P, 1)
    assert record["summary"] == ["Added class Supplier"]
    assert record["summaryMessages"] == [{"code": "ADD_CLASS", "params": {"label": "Supplier"}}]


def test_a_change_set_gives_its_lines_and_warnings_as_codes():
    """The chat's change set cards were in the engine's English whatever the user's language."""
    cs = _propose([ADD_SUPPLIER, {"op": "update_property", "iri": EMAIL, "domain": PARTY}])
    assert cs["summaryMessages"] == [
        {"code": "ADD_CLASS", "params": {"label": "Supplier"}},
        {"code": "UPDATE_PROPERTY", "params": {"fields": "domain", "term": "email"}},  # by its label
    ]
    assert cs["summary"] == [messages.english(m) for m in cs["summaryMessages"]]
    assert [m["code"] for m in cs["warningMessages"]] == ["DOMAIN_ON_PARENT_TABLE"]
    assert cs["warnings"] == [messages.english(m) for m in cs["warningMessages"]]
    assert changesets.list_change_sets(NS, P, limit=20)["changeSets"] == [cs]
    # Applying it gives, and writes into the revision's record, the warnings of the ops it applied.
    assert _apply(cs, 0)["warningMessages"] == cs["warningMessages"]
    assert store.get_record(NS, P, 1)["warningMessages"] == cs["warningMessages"]


def test_a_change_set_stored_before_the_codes_keeps_its_english_lines():
    cs = _propose([ADD_SUPPLIER])
    dynamo_store._get_table().update_item(
        Key=changesets._key(NS, P, cs["changeSetId"]), UpdateExpression="REMOVE summary_messages, warning_messages"
    )
    old = changesets.get_change_set(NS, P, cs["changeSetId"])
    assert old["summary"] == ["Added class Supplier"]
    assert old["summaryMessages"] is None and old["warningMessages"] is None


def test_a_partial_apply_without_its_prerequisite_is_refused_with_the_op_index():
    cs = _propose([ADD_SUPPLIER, SUPPLIES])
    preview = changesets.preview(NS, P, cs["changeSetId"], [1], None)
    assert not preview["applies"] and preview["error"]["opIndex"] == 1 and preview["graph"] is None
    with pytest.raises(changes.InvalidChange) as raised:
        _apply(cs, 0, [1])
    assert raised.value.detail()["opIndex"] == 1
    assert stored_state().revision == 0
    assert changesets.get_change_set(NS, P, cs["changeSetId"])["status"] == changesets.PENDING


def test_apply_on_a_newer_revision_reapplies_or_marks_stale():
    cs = _propose([ADD_SUPPLIER, SUPPLIES])
    changes.apply_change_set(NS, P, [RELABEL], 0, author="carol")  # an unrelated edit
    assert _apply(cs, 1)["changeSet"]["status"] == changesets.APPLIED

    other = _propose([{"op": "add_parent", "iri": VIP, "parent": PARTY}])
    changes.apply_change_set(NS, P, [{"op": "delete_class", "iri": VIP, "cascade": "detach"}], 2, author="carol")
    with pytest.raises(changesets.ChangeSetStale) as raised:
        _apply(other, 3)
    detail = raised.value.detail()
    assert (detail["opIndex"], detail["revision"]) == (0, 3) and "no longer applies" in detail["message"]
    stale = changesets.get_change_set(NS, P, other["changeSetId"])
    assert stale["status"] == changesets.PENDING and stale["stale"]["revision"] == 3
    assert stored_state().revision == 3

    changes.undo(NS, P, 3, author="carol")  # VIP is back, so the change set applies again
    applied = _apply(other, 4)["changeSet"]
    assert applied["status"] == changesets.APPLIED and applied["stale"] is None


def test_a_change_set_is_applied_at_most_once():
    cs = _propose([ADD_SUPPLIER])
    _apply(cs, 0)
    with pytest.raises(changesets.ChangeSetNotPending) as raised:
        _apply(cs, 1)
    assert raised.value.detail()["status"] == changesets.APPLIED
    assert stored_state().revision == 1


def test_apply_loses_to_a_discard_and_leaves_no_revision():
    cs = _propose([ADD_SUPPLIER])
    real = changes.write_revision

    def discard_first(*args, **kwargs):
        changesets.discard(NS, P, cs["changeSetId"], author="carol")  # lands between the dry run and the commit
        return real(*args, **kwargs)

    with (
        patch.object(changesets.changes, "write_revision", side_effect=discard_first),
        pytest.raises(changesets.ChangeSetNotPending) as raised,
    ):
        _apply(cs, 0)
    assert raised.value.detail()["status"] == changesets.DISCARDED
    assert stored_state().revision == 0 and stored_state().applied == ()


def test_apply_needs_the_expected_revision_and_an_editable_draft():
    cs = _propose([ADD_SUPPLIER])
    with pytest.raises(store.DraftConflict):
        _apply(cs, 7)
    dynamo_store.update_proposal(P, namespace=NS, status="accepted")
    with pytest.raises(store.NotEditable):
        _apply(cs, 0)
    with pytest.raises(store.NotEditable):
        _propose([ADD_SUPPLIER])


def test_bad_op_indexes_and_ids_are_refused():
    cs = _propose([ADD_SUPPLIER, SUPPLIES])
    for bad in ([], [0, 0], [2], [-1]):
        with pytest.raises(changesets.InvalidOpIndexes) as raised:
            _apply(cs, 0, bad)
        assert (raised.value.status, raised.value.code) == (422, "InvalidRequest")
    with pytest.raises(ValueError):
        changesets.get_change_set(NS, P, "bad id!")
    with pytest.raises(changesets.ChangeSetNotFound):
        changesets.get_change_set(NS, P, "20260101T000000000000Z-00000000")


def test_discard_and_list_by_status():
    first = _propose([ADD_SUPPLIER])
    second = _propose([RELABEL])
    discarded = changesets.discard(NS, P, first["changeSetId"], author="carol")
    assert (discarded["status"], discarded["discardedBy"]) == (changesets.DISCARDED, "carol")
    with pytest.raises(changesets.ChangeSetNotPending):
        changesets.discard(NS, P, first["changeSetId"], author="carol")
    listed = changesets.list_change_sets(NS, P, limit=20)
    assert (listed["revision"], listed["next"]) == (0, None)
    assert [c["changeSetId"] for c in listed["changeSets"]] == [first["changeSetId"], second["changeSetId"]]
    pending = changesets.list_change_sets(NS, P, changesets.PENDING, limit=20)["changeSets"]
    assert [c["changeSetId"] for c in pending] == [second["changeSetId"]]
    assert [c["changeSetId"] for c in changesets.recent_change_sets(NS, P, changesets.PENDING)] == [
        second["changeSetId"]
    ]


def test_the_listing_pages_oldest_first():
    sets = [_propose([{"op": "set_label", "iri": ORDER, "label": f"Order {n}"}]) for n in range(3)]
    seen, start = [], None
    for _ in range(4):
        page = changesets.list_change_sets(NS, P, limit=2, start=start)
        seen += [c["changeSetId"] for c in page["changeSets"]]
        start = page["next"]
        if start is None:
            break
    assert seen == [c["changeSetId"] for c in sets]


def test_preview_marks_added_changed_and_removed():
    cs = _propose([ADD_SUPPLIER, RELABEL, {"op": "delete_class", "iri": VIP, "cascade": "detach"}])
    preview = changesets.preview(NS, P, cs["changeSetId"], None, None)
    assert preview["applies"] and preview["error"] is None and preview["opIndexes"] == [0, 1, 2]
    assert preview["revision"] == 0 and preview["created"] == [SUPPLIER]
    marks = {n["iri"]: n["preview"] for n in preview["graph"]["nodes"]}
    assert (marks[SUPPLIER], marks[ORDER], marks[VIP], marks[PARTY]) == ("added", "changed", "removed", None)
    edges = {e["id"]: e["preview"] for e in preview["graph"]["edges"]}
    assert edges[f"sub|{SUPPLIER}|{PARTY}"] == "added"
    assert edges[f"sub|{VIP}|{CUSTOMER}"] == "removed"
    assert stored_state().revision == 0


def test_an_add_op_keeps_the_iri_it_minted_so_a_taken_one_makes_the_change_set_stale():
    """A later op names the IRI the add op minted. Minting again on a newer revision would give the new
    term a ``_2`` IRI and point that later op at the term that took the first one."""
    without_iri = {k: v for k, v in ADD_SUPPLIER.items() if k != "iri"}
    cs = _propose([without_iri, SUPPLIES])
    assert cs["ops"][0] == ADD_SUPPLIER and cs["ops"][1]["iri"]
    changes.apply_change_set(NS, P, [{"op": "add_class", "label": "Supplier"}], 0, author="carol")
    with pytest.raises(changesets.ChangeSetStale) as raised:
        _apply(cs, 1)
    assert raised.value.detail()["opIndex"] == 0 and f"{SUPPLIER} already exists" in raised.value.message
    assert stored_state().revision == 1
    assert changesets.get_change_set(NS, P, cs["changeSetId"])["stale"]["revision"] == 1


def test_a_subset_missing_its_prerequisite_on_a_newer_revision_is_not_stale():
    cs = _propose([ADD_SUPPLIER, SUPPLIES])
    changes.apply_change_set(NS, P, [RELABEL], 0, author="carol")  # the draft moves on, unrelated
    with pytest.raises(changes.InvalidChange) as raised:
        _apply(cs, 1, [1])  # the whole change set still applies: only the selection is wrong
    assert raised.value.detail()["opIndex"] == 1
    assert changesets.get_change_set(NS, P, cs["changeSetId"])["stale"] is None


def test_a_page_stops_at_its_byte_budget_and_resumes_there():
    sets = [_propose([{"op": "set_label", "iri": ORDER, "label": f"Order {n}"}]) for n in range(3)]
    one = len(json.dumps(changesets.get_change_set(NS, P, sets[0]["changeSetId"]), ensure_ascii=False).encode())
    with patch.object(changesets, "MAX_PAGE_BYTES", one * 2 + 10):
        first = changesets.list_change_sets(NS, P, limit=20)
        rest = changesets.list_change_sets(NS, P, limit=20, start=first["next"])
    assert [c["changeSetId"] for c in first["changeSets"]] == [s["changeSetId"] for s in sets[:2]]
    assert [c["changeSetId"] for c in rest["changeSets"]] == [sets[2]["changeSetId"]]
    assert rest["next"] is None


def test_a_change_set_larger_than_the_budget_comes_alone():
    sets = [_propose([{"op": "set_label", "iri": ORDER, "label": f"Order {n}"}]) for n in range(2)]
    seen, start = [], None
    with patch.object(changesets, "MAX_PAGE_BYTES", 10):
        for _ in range(4):
            page = changesets.list_change_sets(NS, P, limit=20, start=start)
            assert len(page["changeSets"]) == 1
            seen.append(page["changeSets"][0]["changeSetId"])
            start = page["next"]
            if start is None:
                break
    assert seen == [s["changeSetId"] for s in sets]


def test_the_agent_reads_the_newest_change_sets():
    sets = [_propose([{"op": "set_label", "iri": ORDER, "label": f"Order {n}"}]) for n in range(3)]
    with patch.object(changesets, "MAX_RECENT", 2):
        listed = changesets.recent_change_sets(NS, P)
    assert [c["changeSetId"] for c in listed] == [sets[1]["changeSetId"], sets[2]["changeSetId"]]
