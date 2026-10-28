# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import json
from unittest.mock import patch

import pytest
from coa_ontology import dynamo_store
from coa_ontology.studio import changes, messages
from coa_ontology.studio import draft as store
from coa_ontology.studio.ops import mentioned, parse_nt
from coa_ontology.versions import snapshot
from pydantic import ValidationError
from rdflib import URIRef

from .studio_fakes import (
    CUSTOMER,
    EMAIL,
    ITEM,
    NS,
    ONTO,
    ORDER,
    PARTY,
    VIP,
    opened_draft,
    seed_proposal,
    stored_row,
    stored_state,
)

pytestmark = [pytest.mark.unit]
ADD = {"op": "add_class", "label": "Supplier", "parents": [PARTY]}
DELETE = {"op": "delete_class", "iri": CUSTOMER, "cascade": "delete"}
# Present on every record, the open record included; ``claim_and_write`` adds the last two.
RECORD_KEYS = {
    "baseRevision",
    "kind",
    "source",
    "author",
    "rationale",
    "at",
    "baseVersion",
    "ops",
    "inverse",
    "summary",
    "summaryMessages",
    "warnings",
    "warningMessages",
    "revision",
    "formatVersion",
}


@pytest.fixture(autouse=True)
def _env(versions_aws):
    store.clear_cache()
    with patch("coa_ontology.proposals._generate_schema_sql", return_value=None):
        opened_draft()
        yield versions_aws
    store.clear_cache()


def _sha(revision: int) -> str:
    return snapshot.sha256_hex(store.revision_text(NS, "p-1", revision)[0])


def _apply(ops: list[dict], expected: int, **kw) -> dict:
    return changes.apply_change_set(NS, "p-1", ops, expected, author="alice", **kw)


def test_apply_makes_the_next_revision_with_its_record():
    opened = stored_state()
    result = _apply([ADD, DELETE], 0)
    assert result["revision"] == 1 and not result["dryRun"] and result["canUndo"] and not result["canRedo"]
    assert result["created"] == [f"{ONTO}Supplier"]
    assert result["diff"]["classes"]["added"] == [f"{ONTO}Supplier"]
    assert CUSTOMER in result["diff"]["classes"]["removed"]
    state = stored_state()
    assert (state.revision, state.applied, state.redo) == (1, (1,), ())
    assert state.counts["classes"] == 6 and state.updated_by == "alice"
    assert (state.opened_at, state.opened_by, state.base_version) == (opened.opened_at, opened.opened_by, 1)
    record = store.get_record(NS, "p-1", 1)
    assert record["kind"] == "apply" and record["baseRevision"] == 0 and len(record["ops"]) == 2
    assert (record["source"], record["author"], record["rationale"]) == ("user", "alice", None)
    assert record["baseVersion"] == 1 and record["revision"] == 1
    assert record["inverse"]["op"] == "restore_triples" and record["inverse"]["tbox"]["add"]
    assert set(record) == RECORD_KEYS | {"tboxSha256", "r2rmlSha256"}
    assert record["tboxSha256"] == _sha(1)
    assert record["r2rmlSha256"] == snapshot.sha256_hex(store.revision_text(NS, "p-1", 1)[1])
    assert set(store.get_record(NS, "p-1", 0)) == RECORD_KEYS  # the open record carries no hashes
    assert stored_row()["status"] == "updated"


def test_an_apply_record_stores_the_ops_with_the_iris_they_minted():
    ops = [
        {"op": "add_class", "label": "Customer"},  # CUSTOMER is taken, so the minted IRI is suffixed
        {"op": "add_class", "label": "Gadget", "iri": f"{ONTO}Widget"},  # an explicit IRI is kept as it is
        {"op": "set_label", "iri": PARTY, "label": "Actor"},
        {"op": "add_object_property", "label": "supplied by", "domain": ORDER, "range": CUSTOMER},
        {"op": "add_datatype_property", "label": "weight", "domain": ITEM, "range": "xsd:decimal"},
    ]
    sent = [dict(op) for op in ops]
    result = _apply(ops, 0)
    minted = [f"{ONTO}Customer_2", f"{ONTO}Widget", f"{ONTO}order_suppliedBy", f"{ONTO}item_weight"]
    assert result["created"] == minted
    record = store.get_record(NS, "p-1", 1)
    assert [op["iri"] for op in record["ops"]] == [minted[0], minted[1], PARTY, minted[2], minted[3]]
    for stored, original in zip(record["ops"], sent, strict=True):  # everything else the client sent is kept
        assert {k: stored[k] for k in original if k != "iri"} == {k: v for k, v in original.items() if k != "iri"}
    assert ops == sent  # the client's own ops are not edited
    # Redo repeats the stored ops, so it recreates the very same terms.
    changes.undo(NS, "p-1", 1, author="alice")
    redone = changes.redo(NS, "p-1", 2, author="alice")
    assert redone["created"] == minted
    assert store.get_record(NS, "p-1", redone["revision"])["ops"] == record["ops"]


def test_dry_run_stores_nothing():
    result = _apply([DELETE], 0, dry_run=True)
    assert result["dryRun"] and result["revision"] == 0 and CUSTOMER in result["diff"]["classes"]["removed"]
    assert stored_state().revision == 0 and stored_row()["status"] == "pending"
    assert store._get(store.record_key(NS, "p-1", 1)) is None


def test_a_stale_revision_is_a_conflict_with_the_current_one():
    _apply([ADD], 0)
    with pytest.raises(store.DraftConflict) as raised:
        _apply([DELETE], 0)
    assert raised.value.detail() == {
        "code": "DraftConflict",
        "message": "Another change was applied first; the draft is now at revision 1.",
        "revision": 1,
    }


def test_an_invalid_op_changes_nothing_and_names_its_index():
    with pytest.raises(changes.InvalidChange) as raised:
        _apply([ADD, {"op": "delete_class", "iri": f"{ONTO}Nope"}], 0)
    assert raised.value.extra["opIndex"] == 1
    assert stored_state().revision == 0
    assert store._get(store.record_key(NS, "p-1", 1)) is None
    with pytest.raises(changes.InvalidChange):
        _apply([], 0)
    with pytest.raises(changes.InvalidChange, match=f"at most {changes.MAX_OPS} ops"):
        _apply([ADD] * (changes.MAX_OPS + 1), 0)


def test_a_client_cannot_send_the_internal_inverse_op():
    forged = {"op": "restore_triples", "tbox": {"add": [], "remove": []}, "r2rml": {"add": [], "remove": []}}
    with pytest.raises(ValidationError):
        _apply([forged], 0)
    assert stored_state().revision == 0


def test_two_writers_on_the_same_revision_one_wins_and_the_loser_leaves_a_gap():
    state0 = stored_state()
    _apply([ADD], 0)  # writer A lands revision 1
    with (
        patch.object(changes, "_editable", return_value=state0),  # writer B read revision 0 before A
        pytest.raises(store.DraftConflict) as raised,
    ):
        _apply([DELETE], 0)
    assert raised.value.extra["revision"] == 1
    assert store.get_record(NS, "p-1", 2)["baseRevision"] == 0  # B's claim of 2 stays as an unreferenced gap
    current = store.load_revision(NS, "p-1", 1).draft
    assert mentioned(current, f"{ONTO}Supplier") and mentioned(current, CUSTOMER)  # the files are the winner's
    assert stored_state().revision == 1
    assert [e["revision"] for e in changes.history(NS, "p-1")["entries"]] == [1, 0]  # history never reaches the gap
    assert _apply([{"op": "set_label", "iri": PARTY, "label": "Actor"}], 1)["revision"] == 3  # the gap is skipped
    assert [e["revision"] for e in changes.history(NS, "p-1")["entries"]] == [3, 1, 0]


def test_a_writer_that_died_after_claiming_leaves_a_gap_not_a_block():
    store._put(store.record_key(NS, "p-1", 1), '{"kind": "apply"}', "application/json")  # dead writer's claim
    result = _apply([ADD], 0)
    assert result["revision"] == 2
    assert store.get_record(NS, "p-1", 2)["baseRevision"] == 0
    assert [e["revision"] for e in changes.history(NS, "p-1")["entries"]] == [2, 0]


def test_undo_redo_and_a_new_change_clearing_redo():
    _apply([ADD], 0)  # 1
    _apply([DELETE], 1)  # 2
    assert changes.undo(NS, "p-1", 2, author="bob")["revision"] == 3
    assert _sha(3) == _sha(1)
    assert (stored_state().applied, stored_state().redo) == ((1,), (2,))
    changes.undo(NS, "p-1", 3, author="bob")  # 4
    assert _sha(4) == _sha(0)
    with pytest.raises(changes.NothingToUndo):
        changes.undo(NS, "p-1", 4, author="bob")
    redone = changes.redo(NS, "p-1", 4, author="bob")  # 5 re-applies revision 1's ops
    assert redone["revision"] == 5 and _sha(5) == _sha(1)
    assert (stored_state().applied, stored_state().redo) == ((5,), (2,))
    _apply([{"op": "set_label", "iri": PARTY, "label": "Actor"}], 5)  # 6
    assert (stored_state().applied, stored_state().redo) == ((5, 6), ())
    with pytest.raises(changes.NothingToRedo):
        changes.redo(NS, "p-1", 6, author="bob")
    # Every record names the revision it was built on, so the history can walk back along them.
    assert [store.get_record(NS, "p-1", n)["baseRevision"] for n in range(1, 7)] == [0, 1, 2, 3, 4, 5]


def test_undo_and_redo_refuse_a_revision_mismatch():
    _apply([ADD], 0)
    with pytest.raises(store.DraftConflict):
        changes.undo(NS, "p-1", 0, author="bob")
    changes.undo(NS, "p-1", 1, author="bob")
    with pytest.raises(store.DraftConflict):
        changes.redo(NS, "p-1", 1, author="bob")


def test_history_is_newest_first_with_flags():
    _apply([ADD], 0)
    _apply([DELETE], 1)
    changes.undo(NS, "p-1", 2, author="bob")
    history = changes.history(NS, "p-1")
    assert [(e["revision"], e["kind"]) for e in history["entries"]] == [
        (3, "undo"),
        (2, "apply"),
        (1, "apply"),
        (0, "open"),
    ]
    flags = {e["revision"]: (e["active"], e["undone"]) for e in history["entries"]}
    assert flags[1] == (True, False) and flags[2] == (False, True)
    assert history["entries"][1]["summary"] == ["Deleted class Customer (delete its properties)"]
    assert history["entries"][0]["target"] == 2 and history["entries"][0]["author"] == "bob"
    assert history["canUndo"] and history["canRedo"]
    assert len(changes.history(NS, "p-1", limit=2)["entries"]) == 2
    with patch.object(changes, "MAX_HISTORY", 3):
        assert len(changes.history(NS, "p-1", limit=50)["entries"]) == 3


def _lines(entry: dict) -> list[tuple[str, dict[str, str]]]:
    return [(m["code"], m["params"]) for m in entry["summaryMessages"]]


def test_history_gives_each_line_as_a_code_and_values():
    """A Japanese user read the history in the engine's English."""
    _apply([ADD, DELETE], 0)
    changes.undo(NS, "p-1", 1, author="bob")
    changes.redo(NS, "p-1", 2, author="bob")
    entries = {e["revision"]: e for e in changes.history(NS, "p-1")["entries"]}
    added = [("ADD_CLASS", {"label": "Supplier"}), ("DELETE_CLASS_DELETE", {"term": "Customer"})]
    assert _lines(entries[0]) == [("OPENED_DRAFT", {"version": "1"})]
    assert _lines(entries[1]) == _lines(entries[3]) == added  # a redo repeats the lines of what it redoes
    assert _lines(entries[2]) == [("UNDID_REVISION", {"revision": "1"})]
    assert entries[2]["summary"] == ["Undid revision 1"] and entries[0]["summary"] == ["Opened the draft on v1"]


def test_a_revision_stored_before_the_codes_keeps_its_english_lines_and_so_does_its_redo():
    """Drafts opened before the codes have records with English lines only; they still list."""
    _apply([ADD], 0)
    key = store.record_key(NS, "p-1", 1)
    old = store.get_record(NS, "p-1", 1)
    del old["summaryMessages"]
    store._put(key, json.dumps(old), "application/json")
    changes.undo(NS, "p-1", 1, author="bob")
    changes.redo(NS, "p-1", 2, author="bob")
    entries = {e["revision"]: e for e in changes.history(NS, "p-1")["entries"]}
    assert (entries[1]["summary"], entries[1]["summaryMessages"]) == (["Added class Supplier"], None)
    assert (entries[3]["summary"], entries[3]["summaryMessages"]) == (["Added class Supplier"], None)
    assert _lines(entries[2]) == [("UNDID_REVISION", {"revision": "1"})]


MERGE_VIP = {"op": "merge_classes", "source": VIP, "target": CUSTOMER}


def test_a_revision_names_each_term_by_its_label_in_the_revision_it_changed():
    """The history read "Merged VipCustomer into Customer" for the class the draft labels VIP customer."""
    _apply([MERGE_VIP, {"op": "set_label", "iri": CUSTOMER, "label": "Client"}], 0)
    (entry,) = [e for e in changes.history(NS, "p-1")["entries"] if e["revision"] == 1]
    assert entry["summary"] == ["Merged VIP customer into Customer", "Renamed Customer to Client"]
    assert _lines(entry) == [
        ("MERGE_CLASSES", {"source": "VIP customer", "target": "Customer"}),
        ("SET_LABEL", {"term": "Customer", "label": "Client"}),
    ]


def test_a_revision_stored_with_iri_tails_keeps_its_lines_and_so_does_its_redo():
    """A line written before the lines named labels keeps its IRI tail: no stored entry is rewritten."""
    _apply([MERGE_VIP], 0)
    key = store.record_key(NS, "p-1", 1)
    old = store.get_record(NS, "p-1", 1)
    old["summary"] = ["Merged VipCustomer into Customer"]
    old["summaryMessages"] = [{"code": "MERGE_CLASSES", "params": {"source": "VipCustomer", "target": "Customer"}}]
    store._put(key, json.dumps(old), "application/json")
    changes.undo(NS, "p-1", 1, author="bob")
    changes.redo(NS, "p-1", 2, author="bob")
    entries = {e["revision"]: e for e in changes.history(NS, "p-1")["entries"]}
    for revision in (1, 3):
        assert entries[revision]["summary"] == ["Merged VipCustomer into Customer"]
        assert _lines(entries[revision]) == [("MERGE_CLASSES", {"source": "VipCustomer", "target": "Customer"})]


def test_history_gives_at_most_the_first_100_coded_lines():
    _apply([ADD], 0)
    record = store.get_record(NS, "p-1", 1)
    lines = [{"code": "ADD_CLASS", "params": {"label": f"C{n}"}} for n in range(messages.MAX_MESSAGES + 5)]
    record["summaryMessages"] = record["warningMessages"] = lines
    store._put(store.record_key(NS, "p-1", 1), json.dumps(record), "application/json")
    (entry,) = [e for e in changes.history(NS, "p-1")["entries"] if e["revision"] == 1]
    assert entry["summaryMessages"] == entry["warningMessages"] == lines[: messages.MAX_MESSAGES]


# Moving a mapped attribute's domain to a parent of its mapped class warns.
WARNED = {"op": "update_property", "iri": EMAIL, "domain": PARTY}
DOMAIN_ON_PARTY = {
    "code": "DOMAIN_ON_PARENT_TABLE",
    "params": {"property": "customer_email", "tables": "customer", "domain": "Party", "mappedClass": "Customer"},
}


def test_a_change_gives_its_warnings_as_codes_and_the_history_keeps_them():
    """A Japanese user read a change's warnings in the engine's English, on the page and in the history."""
    dry = _apply([WARNED], 0, dry_run=True)
    assert dry["warningMessages"] == [DOMAIN_ON_PARTY] and dry["warnings"] == [messages.english(DOMAIN_ON_PARTY)]
    assert _apply([WARNED], 0)["warningMessages"] == [DOMAIN_ON_PARTY]
    assert changes.undo(NS, "p-1", 1, author="bob")["warningMessages"] == []
    # A redo applies the ops again, and so gives their warnings again.
    assert changes.redo(NS, "p-1", 2, author="bob")["warningMessages"] == [DOMAIN_ON_PARTY]
    entries = {e["revision"]: e for e in changes.history(NS, "p-1")["entries"]}
    assert entries[1]["warningMessages"] == entries[3]["warningMessages"] == [DOMAIN_ON_PARTY]
    assert entries[1]["warnings"] == entries[3]["warnings"] == [messages.english(DOMAIN_ON_PARTY)]
    assert entries[0]["warningMessages"] == entries[2]["warningMessages"] == []


def test_a_revision_stored_before_the_codes_keeps_its_english_warnings():
    _apply([WARNED], 0)
    key = store.record_key(NS, "p-1", 1)
    old = store.get_record(NS, "p-1", 1)
    del old["summaryMessages"], old["warningMessages"]
    store._put(key, json.dumps(old), "application/json")
    (entry,) = [e for e in changes.history(NS, "p-1")["entries"] if e["revision"] == 1]
    assert entry["warnings"] == [messages.english(DOMAIN_ON_PARTY)] and entry["warningMessages"] is None


@pytest.mark.parametrize("status", ["accepting", "accepted", "cancelled", "rejected"])
def test_a_proposal_that_left_review_refuses_every_write(status):
    dynamo_store.update_proposal("p-1", namespace=NS, status=status)
    for call in (
        lambda: _apply([ADD], 0),
        lambda: changes.undo(NS, "p-1", 0, author="bob"),
        lambda: changes.redo(NS, "p-1", 0, author="bob"),
    ):
        with pytest.raises(store.NotEditable) as raised:
            call()
        assert (raised.value.status, raised.value.detail()["code"]) == (409, "ProposalNotEditable")
    assert stored_row()["status"] == status  # a refused write does not turn the proposal back into "updated"
    assert stored_state().revision == 0
    assert store._get(store.record_key(NS, "p-1", 1)) is None


@pytest.mark.parametrize("status", ["pending", "updated", "accept_failed"])
def test_a_proposal_still_in_review_accepts_a_write(status):
    dynamo_store.update_proposal("p-1", namespace=NS, status=status)
    assert _apply([ADD], 0)["revision"] == 1
    assert stored_row()["status"] == "updated"


def test_applying_undoing_and_redoing_leave_the_cached_revisions_untouched():
    base = store.load_revision(NS, "p-1", 0)
    _apply([ADD, DELETE], 0)
    changes.undo(NS, "p-1", 1, author="bob")
    changes.redo(NS, "p-1", 2, author="bob")
    for revision in (base, store.load_revision(NS, "p-1", 1), store.load_revision(NS, "p-1", 2)):  # each was a 'before'
        assert snapshot.canonical_nt(revision.draft.tbox) == revision.tbox_nt
        assert snapshot.canonical_nt(revision.draft.r2rml) == revision.r2rml_nt


def test_write_revision_takes_the_new_base_only_for_a_rebase_and_extra_cannot_replace_the_base_revision():
    base = store.load_revision(NS, "p-1", 0)
    write = {
        "kind": "rebase",
        "after": base.draft,
        "ops": [],
        "inverse_base": (base.tbox_nt, base.r2rml_nt),
        "keep_applied": (),
        "push": False,
        "redo": (),
        "author": "carol",
        "summary": ["Rebased onto v5"],
        "warnings": [],
    }
    extra = {"skipped": ["1:0"], "fromVersion": 1, "baseRevision": 99}
    first = changes.write_revision(NS, "p-1", stored_state(), **write, extra=extra, base_version=5, rebased=True)
    record = store.get_record(NS, "p-1", first)
    assert (first, record["baseRevision"], record["baseVersion"], record["skipped"], record["fromVersion"]) == (
        1,
        0,
        5,
        ["1:0"],
        1,
    )
    assert (stored_state().revision, stored_state().base_version, stored_state().applied) == (1, 5, ())
    second = changes.write_revision(NS, "p-1", stored_state(), **write, base_version=7)  # not a rebase: 7 is ignored
    assert store.get_record(NS, "p-1", second)["baseVersion"] == 5 and stored_state().base_version == 5


def test_the_same_base_and_ops_give_the_same_bytes_in_another_proposal():
    seed_proposal("p-2")
    store.open_draft(NS, "p-2", "alice")
    a = _apply([ADD, DELETE], 0)
    b = changes.apply_change_set(NS, "p-2", [ADD, DELETE], 0, author="carol")
    assert (
        store.get_record(NS, "p-1", a["revision"])["tboxSha256"]
        == store.get_record(NS, "p-2", b["revision"])["tboxSha256"]
    )
    assert (URIRef(CUSTOMER), None, None) not in parse_nt(store.revision_text(NS, "p-2", 1)[0])


def _write_args(**overrides) -> dict:
    """``write_revision`` arguments that store revision 0's draft again as a plain apply."""
    base = store.load_revision(NS, "p-1", stored_state().revision)
    return {
        "kind": "apply",
        "after": base.draft,
        "ops": [ADD],
        "inverse_base": (base.tbox_nt, base.r2rml_nt),
        "keep_applied": stored_state().applied,
        "push": True,
        "redo": (),
        "author": "dave",
        "summary": ["Added class Supplier"],
        "warnings": [],
        **overrides,
    }


def test_a_revision_records_its_source_and_rationale():
    revision = changes.write_revision(
        NS,
        "p-1",
        stored_state(),
        **_write_args(),
        source="agent",
        rationale="Suppliers are named in the contracts",
        extra={"changeSetId": "cs-1", "opIndexes": [0]},
    )
    record = store.get_record(NS, "p-1", revision)
    assert (record["source"], record["rationale"], record["changeSetId"]) == (
        "agent",
        "Suppliers are named in the contracts",
        "cs-1",
    )
    entry = changes.history(NS, "p-1")["entries"][0]
    assert (entry["source"], entry["rationale"], entry["changeSetId"]) == (
        "agent",
        "Suppliers are named in the contracts",
        "cs-1",
    )
    opened = changes.history(NS, "p-1")["entries"][-1]
    assert (opened["source"], opened["rationale"], opened["changeSetId"]) == ("user", None, None)


# A condition that always fails: the row does not exist.
MISSING_ROW = {"Key": {"PK": "MARKER", "SK": "NONE"}, "ConditionExpression": "attribute_exists(PK)"}


def _marker_put(revision: int) -> list[tuple[str, dict]]:
    return [("Put", {"Item": {"PK": "MARKER", "SK": f"REV#{revision}"}})]


def test_extra_transaction_items_commit_with_the_revision(versions_aws):
    seen: list[int] = []

    def also(revision: int) -> list[tuple[str, dict]]:
        seen.append(revision)
        return _marker_put(revision)

    revision = changes.write_revision(NS, "p-1", stored_state(), **_write_args(), also=also)
    assert seen == [revision] and stored_state().revision == revision == 1
    assert versions_aws.table.get_item(Key={"PK": "MARKER", "SK": "REV#1"}).get("Item")
    assert stored_row()["status"] == "updated"


def test_a_failed_extra_condition_leaves_the_revision_uncommitted():
    def also(revision: int) -> list[tuple[str, dict]]:
        return [*_marker_put(revision), ("ConditionCheck", MISSING_ROW)]

    with pytest.raises(store.CommitGuardFailed) as raised:
        changes.write_revision(NS, "p-1", stored_state(), **_write_args(), also=also)
    assert raised.value.index == 1
    assert stored_state().revision == 0 and stored_state().applied == ()


def test_a_draft_conflict_wins_over_a_failed_extra_condition():
    _apply([ADD], 0)
    stale = store.state_of({"studio": {**stored_row()["studio"], "revision": 0, "applied": []}})
    assert stale is not None

    def also(revision: int) -> list[tuple[str, dict]]:
        return [("ConditionCheck", MISSING_ROW)]

    with pytest.raises(store.DraftConflict):
        changes.write_revision(NS, "p-1", stale, **_write_args(), also=also)
    assert stored_state().revision == 1
