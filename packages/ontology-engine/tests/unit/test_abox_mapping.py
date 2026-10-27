# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import dataclasses
import logging
from unittest.mock import patch

import pytest
from coa_ontology.abox import mapping as mp
from coa_ontology.abox import store
from coa_ontology.abox.tbox import load_tbox

pytestmark = pytest.mark.unit

NS = "11111111-2222-3333-4444-555555555555"
TTL = """
@prefix owl: <http://www.w3.org/2002/07/owl#> . @prefix o: <http://e.com/o#> .
o:Customer a owl:Class . o:Order a owl:Class .
o:placedBy a owl:ObjectProperty . o:total a owl:DatatypeProperty .
"""
CUSTOMER, ORDER = "http://e.com/o#Customer", "http://e.com/o#Order"
PLACED_BY, TOTAL = "http://e.com/o#placedBy", "http://e.com/o#total"


def _row(kind=mp.CLASS, key=("Company",), target=CUSTOMER, **kw):
    return mp.MappingRow(kind=kind, key=key, target=target, **kw)


def _attr(target=TOTAL, **kw):
    return _row(kind=mp.ATTRIBUTE, key=(ORDER, "total"), target=target, **kw)


def _rel(target=PLACED_BY, **kw):
    return _row(kind=mp.RELATION, key=("placed", ORDER, CUSTOMER), target=target, **kw)


def _key(row):
    return {"PK": f"NAMESPACE#{NS}", "SK": f"ABOXMAP#{row.kind}#{row.id.split('.')[1]}"}


def test_ids_are_kind_scoped_and_path_safe():
    a = mp.mapping_id(mp.CLASS, ("Company",))
    assert a.startswith("class.") and len(a.split(".", 1)[1]) == 32
    assert mp.mapping_id(mp.ATTRIBUTE, ("Company", "name")).startswith("attribute.")
    assert mp.is_mapping_id(a) and not mp.is_mapping_id("class.zz") and not mp.is_mapping_id("x/../y")


def test_mapping_ids_are_stable():
    # Ids are stored in DynamoDB sort keys: changing the encoding would orphan stored rows.
    assert mp.mapping_id(mp.CLASS, ("Company",)) == "class.de4743c879734dc38cb3e270ecefcae0"
    assert mp.mapping_id(mp.RELATION, ("placed", CUSTOMER, ORDER)) == "relation.3716a83cc16dbdab53cf688926edd377"
    assert mp.mapping_id(mp.ATTRIBUTE, (ORDER, "total")) == "attribute.e7eb63dc00556e979add845ca6f11420"


def test_is_mapping_id_matches_the_whole_string():
    valid = mp.mapping_id(mp.CLASS, ("Company",))
    assert not mp.is_mapping_id(valid + "\n")
    assert not mp.is_mapping_id("\n" + valid)
    assert not mp.is_mapping_id(valid.upper())
    assert not mp.is_mapping_id("other." + valid.split(".")[1])


@pytest.mark.parametrize(
    ("kind", "key"),
    [
        (mp.CLASS, ()),
        (mp.CLASS, ("a", "b")),
        (mp.RELATION, ("a", "b")),
        (mp.RELATION, ("a", "b", "c", "d")),
        (mp.ATTRIBUTE, ("a",)),
        (mp.ATTRIBUTE, ("a", "b", "c")),
        ("other", ("a",)),
    ],
)
def test_key_shape_is_enforced_per_kind(kind, key):
    with pytest.raises(ValueError):
        mp.mapping_id(kind, key)
    with pytest.raises(ValueError):
        mp.MappingRow(kind=kind, key=key, target=mp.IGNORE)


def test_rows_are_immutable():
    row = _row()
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.target = mp.IGNORE  # type: ignore[misc]
    assert dataclasses.replace(row, target=mp.IGNORE).target == mp.IGNORE


def test_auto_save_never_overwrites_manual(versions_aws):
    assert mp.save_auto(NS, _row())
    mp.set_manual(NS, _row().id, ORDER, mp.FORWARD, "alice")
    assert not mp.save_auto(NS, _row(target=CUSTOMER, rationale="llm"))
    got = mp.get_row(NS, _row().id)
    assert (got.target, got.origin, got.updated_by) == (ORDER, mp.MANUAL, "alice")


def test_save_auto_forces_origin_auto(versions_aws):
    assert mp.save_auto(NS, _row(origin=mp.MANUAL))
    assert mp.get_row(NS, _row().id).origin == mp.AUTO


def test_save_auto_replaces_a_row_that_has_no_origin(versions_aws):
    row = _row()
    versions_aws.table.put_item(Item={**_key(row), "kind": "class", "key": ["Company"], "target": ORDER})
    assert mp.save_auto(NS, row)
    assert mp.get_row(NS, row.id).target == CUSTOMER


def test_a_fenced_auto_save_lands_only_while_the_build_holds_the_lock(versions_aws):
    assert store.acquire_lock(NS, "b1")
    assert mp.save_auto(NS, _row(), fence_build_id="b1")
    mp.set_manual(NS, _row().id, ORDER, mp.FORWARD, "alice")
    assert not mp.save_auto(NS, _row(), fence_build_id="b1")  # a human still owns the row
    other = _row(key=("Vendor",))
    store.fence_teardown(NS, 0)  # a namespace teardown takes the lock
    with pytest.raises(store.LockLost):
        mp.save_auto(NS, other, fence_build_id="b1")
    assert mp.get_row(NS, other.id) is None
    assert mp.get_row(NS, _row().id).origin == mp.MANUAL


def test_a_fenced_status_write_lands_only_while_the_build_holds_the_lock(versions_aws, caplog):
    tb = load_tbox(TTL)
    gone = _row(key=("Gone",), target="http://e.com/o#Removed")
    kept = _row(key=("Kept",), target="http://e.com/o#Removed")
    for row in (gone, kept):
        mp.save_auto(NS, row)
    read = {r.key: r for r in mp.effective(mp.list_rows(NS), tb)}
    assert store.acquire_lock(NS, "b1")
    mp.set_manual(NS, gone.id, CUSTOMER, mp.FORWARD, "alice")
    with caplog.at_level(logging.INFO, logger="abox.mapping"):
        mp.persist_status(NS, [read[("Gone",)]], fence_build_id="b1")  # changed meanwhile: skipped, as unfenced
    assert mp.get_row(NS, gone.id).status == mp.ACTIVE
    assert sum("changed or removed during the build" in r.message for r in caplog.records) == 1
    store.fence_teardown(NS, 0)
    with pytest.raises(store.LockLost):
        mp.persist_status(NS, [read[("Kept",)]], fence_build_id="b1")
    assert mp.get_row(NS, kept.id).status == mp.ACTIVE  # not marked stale after the lock was lost


def test_reset_to_auto_forces_redecision(versions_aws):
    mp.save_auto(NS, _row(candidate_hash="h1"))
    mp.set_manual(NS, _row().id, mp.IGNORE, mp.FORWARD, "alice")
    got = mp.reset_to_auto(NS, _row().id, "bob")
    assert (got.origin, got.candidate_hash, got.updated_by) == (mp.AUTO, "", "bob")


def test_set_manual_on_missing_row_raises(versions_aws):
    with pytest.raises(KeyError):
        mp.set_manual(NS, mp.mapping_id(mp.CLASS, ("Nope",)), mp.IGNORE, mp.FORWARD, "a")


def test_reset_to_auto_on_missing_row_raises(versions_aws):
    with pytest.raises(KeyError):
        mp.reset_to_auto(NS, mp.mapping_id(mp.CLASS, ("Nope",)), "a")


def _race(monkeypatch, then):
    """Run ``then()`` right after the next ``get_row`` read, as a concurrent writer would."""
    real = mp.get_row
    fired = []

    def racing(namespace, mid):
        row = real(namespace, mid)
        if not fired:
            fired.append(True)
            then()
        return row

    monkeypatch.setattr(mp, "get_row", racing)


@pytest.mark.parametrize("edit", ["set_manual", "reset_to_auto"])
def test_edit_of_a_row_that_changed_after_the_read_conflicts(versions_aws, monkeypatch, edit):
    mp.save_auto(NS, _row())
    mp.set_manual(NS, _row().id, ORDER, mp.FORWARD, "alice")
    _race(monkeypatch, lambda: mp.set_manual(NS, _row().id, mp.IGNORE, mp.FORWARD, "carol"))
    with pytest.raises(mp.MappingConflict):
        if edit == "set_manual":
            mp.set_manual(NS, _row().id, CUSTOMER, mp.FORWARD, "bob")
        else:
            mp.reset_to_auto(NS, _row().id, "bob")
    got = mp.get_row(NS, _row().id)
    assert (got.target, got.origin, got.updated_by) == (mp.IGNORE, mp.MANUAL, "carol")


@pytest.mark.parametrize("edit", ["set_manual", "reset_to_auto"])
def test_edit_of_a_row_deleted_after_the_read_raises_key_error(versions_aws, monkeypatch, edit):
    mp.save_auto(NS, _row())
    _race(
        monkeypatch,
        lambda: versions_aws.table.delete_item(Key=_key(_row())),
    )
    with pytest.raises(KeyError):
        if edit == "set_manual":
            mp.set_manual(NS, _row().id, ORDER, mp.FORWARD, "bob")
        else:
            mp.reset_to_auto(NS, _row().id, "bob")


def test_edit_returns_the_stored_row(versions_aws):
    mp.save_auto(NS, _row(decided_for_version=3))
    got = mp.set_manual(NS, _row().id, ORDER, mp.FORWARD, "alice")
    assert got == mp.get_row(NS, _row().id)
    assert got.decided_for_version == 3 and type(got.decided_for_version) is int


def test_a_manual_ignore_is_stored_as_ignored(versions_aws):
    mp.save_auto(NS, _row())
    ignored = mp.set_manual(NS, _row().id, mp.IGNORE, mp.FORWARD, "alice")
    assert (ignored.origin, ignored.status) == (mp.MANUAL, mp.IGNORED)
    assert mp.get_row(NS, _row().id).status == mp.IGNORED
    # Pointing the row at a term again makes it apply.
    assert mp.set_manual(NS, _row().id, ORDER, mp.FORWARD, "alice").status == mp.ACTIVE


def test_numbers_roundtrip_as_ints(versions_aws):
    mp.save_auto(NS, _row(decided_for_version=7))
    got = mp.get_row(NS, _row().id)
    assert got.decided_for_version == 7 and type(got.decided_for_version) is int
    [listed] = mp.list_rows(NS)
    assert type(listed.decided_for_version) is int


def test_effective_marks_stale_and_ignored():
    tb = load_tbox(TTL)
    rows = [
        _row(),
        _row(key=("Gone",), target="http://e.com/o#Removed", origin=mp.MANUAL),
        _row(key=("Noise",), target=mp.IGNORE),
        _attr(mp.FREE),
    ]
    eff = {r.key: r.status for r in mp.effective(rows, tb)}
    assert eff == {
        ("Company",): mp.ACTIVE,
        ("Gone",): mp.STALE,
        ("Noise",): mp.IGNORED,
        (ORDER, "total"): mp.ACTIVE,
    }


def test_targets_must_match_the_kind():
    tb = load_tbox(TTL)
    cases = [
        (_row(), mp.ACTIVE),
        (_row(target=mp.IGNORE), mp.IGNORED),
        (_row(target=mp.FREE), mp.STALE),  # free is attribute-only
        (_row(target=TOTAL), mp.STALE),  # a property is not a class
        (_row(target=PLACED_BY), mp.STALE),
        (_rel(), mp.ACTIVE),
        (_rel(mp.IGNORE), mp.IGNORED),
        (_rel(mp.FREE), mp.STALE),
        (_rel(CUSTOMER), mp.STALE),  # a class is not an object property
        (_rel(TOTAL), mp.STALE),
        (_attr(), mp.ACTIVE),
        (_attr(mp.FREE), mp.ACTIVE),
        (_attr(mp.IGNORE), mp.STALE),  # an attribute cannot be ignored
        (_attr(CUSTOMER), mp.STALE),
        (_attr(PLACED_BY), mp.STALE),
    ]
    for row, status in cases:
        assert mp.effective([row], tb)[0].status == status, (row.kind, row.target)
        assert mp.valid_target(tb, row.kind, row.target) == (status != mp.STALE), (row.kind, row.target)


def test_a_relation_row_is_stale_when_its_property_does_not_fit_its_classes_that_way():
    """The build drops every link of such a row; a manual one gets there when a new version changes a domain."""
    tb = load_tbox(
        TTL
        + "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        + "o:placedBy rdfs:domain o:Order ; rdfs:range o:Customer .\n"
    )
    assert mp.effective([_rel(origin=mp.MANUAL)], tb)[0].status == mp.ACTIVE  # Order to Customer
    assert mp.effective([_rel(origin=mp.MANUAL, direction=mp.INVERSE)], tb)[0].status == mp.STALE
    assert mp.effective([_rel(mp.IGNORE, direction=mp.INVERSE)], tb)[0].status == mp.IGNORED


def test_revision_ignores_rationale_and_inactive_rows():
    tb = load_tbox(TTL)
    a = mp.revision(mp.effective([_row(rationale="x")], tb))
    b = mp.revision(mp.effective([_row(rationale="y"), _row(key=("Gone",), target="http://e.com/o#Removed")], tb))
    assert a == b
    assert a != mp.revision(mp.effective([_row(target=ORDER)], tb))


def test_revision_depends_on_direction_but_not_order_origin_or_audit_fields():
    tb = load_tbox(TTL)
    one, two = _row(), _rel()
    base = mp.revision(mp.effective([one, two], tb))
    assert mp.revision(mp.effective([two, one], tb)) == base
    noisy = [
        dataclasses.replace(one, origin=mp.MANUAL, updated_by="alice", updated_at="2026-01-01", candidate_hash="h"),
        dataclasses.replace(two, updated_by="bob", updated_at="2026-02-02", decided_for_version=9),
    ]
    assert mp.revision(mp.effective(noisy, tb)) == base
    flipped = [one, dataclasses.replace(two, direction=mp.INVERSE)]
    assert mp.revision(mp.effective(flipped, tb)) != base


def test_list_rows_filters_by_kind(versions_aws):
    mp.save_auto(NS, _row())
    mp.save_auto(NS, _attr(mp.FREE))
    assert [r.kind for r in mp.list_rows(NS)] == [mp.ATTRIBUTE, mp.CLASS]
    assert [r.kind for r in mp.list_rows(NS, mp.CLASS)] == [mp.CLASS]


def test_list_rows_follows_pagination(versions_aws):
    class TwoPerPage:
        def __init__(self, table):
            self._table = table
            self.queries = 0

        def __getattr__(self, name):
            return getattr(self._table, name)

        def query(self, **kwargs):
            self.queries += 1
            return self._table.query(Limit=2, **kwargs)

    for i in range(5):
        mp.save_auto(NS, _row(key=(f"Company{i}",)))
    paged = TwoPerPage(versions_aws.table)
    with patch("coa_ontology.dynamo_store._get_table", return_value=paged):
        assert [r.key for r in mp.list_rows(NS)] == [(f"Company{i}",) for i in range(5)]
    assert paged.queries == 3


def test_persist_status_records_the_recomputed_status(versions_aws):
    tb = load_tbox(TTL)
    gone = _row(key=("Gone",), target="http://e.com/o#Removed")
    mp.save_auto(NS, gone)
    mp.save_auto(NS, _row())
    mp.persist_status(NS, mp.effective(mp.list_rows(NS), tb))
    assert mp.get_row(NS, gone.id).status == mp.STALE
    assert mp.get_row(NS, _row().id).status == mp.ACTIVE


def test_persist_status_skips_a_row_changed_after_the_build_read_it(versions_aws, caplog):
    tb = load_tbox(TTL)
    gone = _row(key=("Gone",), target="http://e.com/o#Removed")
    reassigned = _row(key=("Other",), target="http://e.com/o#Removed")
    deleted = _row(key=("Deleted",), target="http://e.com/o#Removed")
    kept = _row(key=("Kept",), target="http://e.com/o#Removed")
    for row in (gone, reassigned, deleted, kept):
        mp.save_auto(NS, row)
    read = mp.effective(mp.list_rows(NS), tb)
    assert {r.key: r.status for r in read}[("Gone",)] == mp.STALE

    # While the build ran, a human fixed one row, another build retargeted one, one was removed.
    mp.set_manual(NS, gone.id, CUSTOMER, mp.FORWARD, "alice")
    mp.save_auto(NS, dataclasses.replace(reassigned, target=ORDER))
    versions_aws.table.delete_item(Key=_key(deleted))

    with caplog.at_level(logging.INFO, logger="abox.mapping"):
        mp.persist_status(NS, read)  # does not raise
    assert mp.get_row(NS, gone.id).status == mp.ACTIVE  # alice's row is not marked stale
    assert (mp.get_row(NS, reassigned.id).target, mp.get_row(NS, reassigned.id).status) == (ORDER, mp.ACTIVE)
    assert mp.get_row(NS, deleted.id) is None  # not resurrected
    assert mp.get_row(NS, kept.id).status == mp.STALE  # the untouched row was updated
    assert sum("changed or removed during the build" in r.message for r in caplog.records) == 3


def test_persist_status_updates_a_row_stored_without_origin(versions_aws, caplog):
    tb = load_tbox(TTL)
    row = _row(key=("Gone",), target="http://e.com/o#Removed")
    versions_aws.table.put_item(Item={**_key(row), "kind": "class", "key": ["Gone"], "target": row.target})
    [read] = mp.list_rows(NS)
    assert read.origin == mp.AUTO  # a row without origin reads as automatic
    with caplog.at_level(logging.INFO, logger="abox.mapping"):
        mp.persist_status(NS, mp.effective([read], tb))
    assert versions_aws.table.get_item(Key=_key(row))["Item"]["status"] == mp.STALE
    assert not caplog.records


def test_expected_updated_at_rejects_an_edit_from_a_stale_view(versions_aws):
    mp.save_auto(NS, _row())
    seen = mp.get_row(NS, _row().id).updated_at
    mp.set_manual(NS, _row().id, ORDER, mp.FORWARD, "alice")  # someone else edits after the view was loaded

    with pytest.raises(mp.MappingConflict):
        mp.set_manual(NS, _row().id, mp.IGNORE, mp.FORWARD, "bob", expected_updated_at=seen)
    with pytest.raises(mp.MappingConflict):
        mp.reset_to_auto(NS, _row().id, "bob", expected_updated_at=seen)
    got = mp.get_row(NS, _row().id)
    assert (got.target, got.origin, got.updated_by) == (ORDER, mp.MANUAL, "alice")

    edited = mp.set_manual(NS, _row().id, mp.IGNORE, mp.FORWARD, "bob", expected_updated_at=got.updated_at)
    assert (edited.target, edited.updated_by) == (mp.IGNORE, "bob")
    reset = mp.reset_to_auto(NS, _row().id, "carol", expected_updated_at=edited.updated_at)
    assert (reset.origin, reset.updated_by) == (mp.AUTO, "carol")


def test_expected_updated_at_still_requires_the_row(versions_aws):
    mid = mp.mapping_id(mp.CLASS, ("Nope",))
    with pytest.raises(KeyError):
        mp.set_manual(NS, mid, mp.IGNORE, mp.FORWARD, "a", expected_updated_at="2026-01-01T00:00:00+00:00")
    with pytest.raises(KeyError):
        mp.reset_to_auto(NS, mid, "a", expected_updated_at="2026-01-01T00:00:00+00:00")
