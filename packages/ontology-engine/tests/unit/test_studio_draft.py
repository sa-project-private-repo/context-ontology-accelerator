# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import contextlib
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from coa_ontology import dynamo_store, proposals
from coa_ontology.studio import draft as store
from coa_ontology.studio.ops import parse_nt
from coa_ontology.versions import publish, snapshot
from rdflib import OWL, RDF, URIRef

from .studio_fakes import (
    CUSTOMER,
    NS,
    ONTO,
    SCHEME,
    SHIPMENT,
    opened_draft,
    publish_base,
    seed_proposal,
    stored_row,
    stored_state,
)

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _env(versions_aws):
    store.clear_cache()
    with patch("coa_ontology.proposals._generate_schema_sql", return_value=None):
        yield versions_aws
    store.clear_cache()


def _accept_would_publish(base: int | None) -> tuple[str, str]:
    """Canonical text of the ontology and mapping a plain accept of ``p-1`` onto ``base`` publishes."""
    req = proposals.publish_request_for(dynamo_store.get_proposal_by_id("p-1", namespace=NS), NS, "p-1", ONTO, "alice")
    tbox, r2rml = publish.union_candidate(req, base)
    return snapshot.canonical_nt(tbox), snapshot.canonical_nt(r2rml)


@contextlib.contextmanager
def _first_update_reported_lost(table):
    """The first ``update_item`` is applied, yet its caller sees a failed condition (botocore retried it)."""
    real_update = table.update_item
    calls = []

    def applied_then_failed(**kwargs):
        result = real_update(**kwargs)
        if not calls:
            calls.append(1)
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "retried"}}, "UpdateItem"
            )
        return result

    with patch.object(table, "update_item", side_effect=applied_then_failed):
        yield


def _claim(start: int = 1) -> int:
    return store.claim_and_write(NS, "p-1", start, {"kind": "change"}, "", "")


def _commit(expected: int, revision: int, **kwargs) -> None:
    store.commit(
        NS,
        "p-1",
        expected=expected,
        revision=revision,
        applied=(revision,),
        redo=(),
        counts_={"classes": 7},
        author="bob",
        **kwargs,
    )


def test_keys_live_under_the_proposals_prefix():
    assert store.revision_key(NS, "p-1", 7, "ontology.nt") == f"proposals/{NS}/p-1/studio/rev-000007/ontology.nt"
    assert store.record_key(NS, "p-1", 7) == f"proposals/{NS}/p-1/studio/ops/000007.json"
    with pytest.raises(ValueError):
        store.revision_key(NS, "../x", 1, "ontology.nt")
    with pytest.raises(ValueError):
        store.revision_key(NS, "p-1", 1, "shapes.nt")


def test_open_builds_revision_zero_as_the_union_accept_would():
    publish_base()
    seed_proposal()
    state = store.open_draft(NS, "p-1", "alice")
    assert (state.revision, state.base_version, state.ontology_id, state.style) == (0, 1, ONTO, "structured")
    assert state.applied == () and state.redo == ()
    assert state.counts == {"classes": 6, "objectProperties": 1, "datatypeProperties": 2, "triplesMaps": 2}
    assert stored_state() == state
    tbox_nt, r2rml_nt = store.revision_text(NS, "p-1", 0)
    tbox = parse_nt(tbox_nt)
    assert (URIRef(SHIPMENT), RDF.type, OWL.Class) in tbox and (URIRef(CUSTOMER), RDF.type, OWL.Class) in tbox
    assert (tbox_nt, r2rml_nt) == _accept_would_publish(1)
    record = store.get_record(NS, "p-1", 0)
    assert record["kind"] == "open" and record["baseVersion"] == 1 and record["revision"] == 0
    assert stored_row()["status"] == "pending"


def test_an_unstructured_proposal_opens_in_the_document_style():
    publish_base()
    seed_proposal(source_type="UNSTRUCTURED")
    state = store.open_draft(NS, "p-1", "alice")
    assert state.style == "document"
    assert store.scheme(NS, state).style == "document"


def test_scheme_follows_the_states_ontology_and_style():
    state = opened_draft()
    assert store.scheme(NS, state) == SCHEME


def test_open_is_idempotent():
    publish_base()
    seed_proposal()
    first = store.open_draft(NS, "p-1", "alice")
    again = store.open_draft(NS, "p-1", "bob")
    assert again == first
    assert store._get(store.record_key(NS, "p-1", 1)) is None


@pytest.mark.parametrize("status", ["accepting", "accepted", "rejected", "cancelled"])
def test_opening_again_returns_the_draft_whatever_the_proposals_status(status):
    first = opened_draft()
    dynamo_store.update_proposal("p-1", namespace=NS, status=status)
    assert store.open_draft(NS, "p-1", "bob") == first
    assert stored_row()["status"] == status


def test_open_on_a_brand_new_namespace_has_no_base():
    seed_proposal()
    state = store.open_draft(NS, "p-1", "alice")
    assert state.base_version is None
    assert store.head_version(NS) is None


def test_the_open_record_gives_its_line_as_a_code():
    seed_proposal()
    store.open_draft(NS, "p-1", "alice")
    record = store.get_record(NS, "p-1", 0)
    assert record["summary"] == ["Opened the draft on no version"]
    assert record["summaryMessages"] == [{"code": "OPENED_DRAFT_NO_VERSION", "params": {}}]


def test_open_in_a_legacy_namespace_captures_v1_first():
    seed_proposal()
    captured = []

    def capture_v1(namespace, ontology_id):
        if not captured:  # publish_version calls bootstrap_if_needed again
            captured.append((namespace, ontology_id))
            publish_base()

    with patch.object(publish, "bootstrap_if_needed", side_effect=capture_v1):
        state = store.open_draft(NS, "p-1", "alice")
    assert captured == [(NS, ONTO)]
    assert state.base_version == 1
    assert store.head_version(NS) == 1


@pytest.mark.parametrize("status", ["accepting", "accepted", "rejected", "cancelled"])
def test_open_refuses_a_proposal_that_cannot_change(status):
    seed_proposal(status=status)
    with pytest.raises(store.NotEditable):
        store.open_draft(NS, "p-1", "alice")


def test_open_unknown_proposal():
    with pytest.raises(store.ProposalNotFound):
        store.open_draft(NS, "nope", "alice")


def test_merged_base_is_the_head_and_what_a_plain_accept_publishes():
    publish_base()
    seed_proposal()
    item = store.get_proposal(NS, "p-1", hydrate_turtle=True)
    base, candidate = store.merged_base(NS, "p-1", item, ontology_id=ONTO, created_by="alice")
    assert base == 1
    assert (snapshot.canonical_nt(candidate.tbox), snapshot.canonical_nt(candidate.r2rml)) == _accept_would_publish(1)


def test_merged_base_refuses_a_namespace_that_publishes_another_ontology():
    publish_base()
    seed_proposal()
    item = store.get_proposal(NS, "p-1", hydrate_turtle=True)
    with pytest.raises(store.StudioError, match="publishes versions of ontology"):
        store.merged_base(NS, "p-1", item, ontology_id=f"{ONTO[:-1]}-other#", created_by="alice")


def test_merged_base_refuses_a_proposal_with_no_ontology():
    publish_base()
    seed_proposal()
    item = store.get_proposal(NS, "p-1", hydrate_turtle=True)
    with (
        patch.object(proposals, "proposal_turtle", return_value=("", "")),
        pytest.raises(store.StudioError, match="no ontology"),
    ):
        store.merged_base(NS, "p-1", item, ontology_id=ONTO, created_by="alice")


def test_merged_base_reports_a_proposal_that_cannot_be_merged():
    publish_base()
    seed_proposal()
    item = store.get_proposal(NS, "p-1", hydrate_turtle=True)
    unmergeable = snapshot.SnapshotValidationError("two labels for one class")
    with (
        patch.object(publish, "union_candidate", side_effect=unmergeable),
        pytest.raises(store.StudioError, match="cannot be merged onto v1: two labels for one class"),
    ):
        store.merged_base(NS, "p-1", item, ontology_id=ONTO, created_by="alice")


def test_a_taken_revision_number_is_skipped():
    store._put(store.record_key(NS, "p-1", 0), "{}", "application/json")
    n = store.claim_and_write(NS, "p-1", 0, {"kind": "open"}, "", "")
    assert n == 1
    assert store.get_record(NS, "p-1", 1)["revision"] == 1


def test_claiming_gives_up_when_every_number_in_reach_is_taken():
    for n in range(store.MAX_CLAIM_ATTEMPTS):
        store._put(store.record_key(NS, "p-1", n), "{}", "application/json")
    with pytest.raises(store.StudioError, match="Could not claim"):
        store.claim_and_write(NS, "p-1", 0, {"kind": "open"}, "", "")
    assert store._get(store.revision_key(NS, "p-1", store.MAX_CLAIM_ATTEMPTS, "ontology.nt")) is None


def test_discarding_frees_the_number_only_after_its_files_are_gone(versions_aws):
    n = store.claim_and_write(NS, "p-1", 0, {"kind": "change"}, "loser-tbox", "loser-r2rml")
    real_delete = versions_aws.s3.delete_object
    reclaimed = []

    def delete_then_let_another_writer_claim(**kwargs):
        result = real_delete(**kwargs)
        if kwargs["Key"] == store.record_key(NS, "p-1", n) and not reclaimed:  # the number is free from here on
            reclaimed.append(store.claim_and_write(NS, "p-1", n, {"kind": "change"}, "winner-tbox", "winner-r2rml"))
        return result

    with patch.object(versions_aws.s3, "delete_object", side_effect=delete_then_let_another_writer_claim):
        store.discard(NS, "p-1", n)
    assert reclaimed == [n]
    assert store._get(store.revision_key(NS, "p-1", n, "ontology.nt")) == "winner-tbox"
    assert store._get(store.revision_key(NS, "p-1", n, "r2rml.nt")) == "winner-r2rml"


def test_a_cached_revision_survives_its_files_until_the_cache_is_cleared():
    opened_draft()
    first = store.load_revision(NS, "p-1", 0)
    assert store.load_revision(NS, "p-1", 0) is first
    store.discard(NS, "p-1", 0)
    assert store.load_revision(NS, "p-1", 0) is first
    store.clear_cache()
    with pytest.raises(store.StudioStateError, match="revision 0 are missing"):
        store.load_revision(NS, "p-1", 0)
    with pytest.raises(store.StudioStateError, match="revision 0 is missing"):
        store.get_record(NS, "p-1", 0)


def test_a_lost_open_race_returns_the_winner():
    publish_base()
    seed_proposal()
    winner = store.open_draft(NS, "p-1", "alice")
    store.clear_cache()
    real_get = store.get_proposal
    calls = []

    def stale_then_real(*args, **kwargs):
        item = real_get(*args, **kwargs)
        if not calls:
            calls.append(1)
            item = {k: v for k, v in item.items() if k != "studio"}  # this opener read before the winner wrote
        return item

    with patch.object(store, "get_proposal", side_effect=stale_then_real):
        assert store.open_draft(NS, "p-1", "bob") == winner
    assert store._get(store.record_key(NS, "p-1", 1)) is None  # the loser's claim was discarded
    assert store._get(store.revision_key(NS, "p-1", 1, "ontology.nt")) is None
    assert winner.opened_by == "alice" and stored_state() == winner
    assert store.revision_text(NS, "p-1", winner.revision) == _accept_would_publish(1)


def test_a_retried_open_that_had_already_landed_keeps_its_draft(versions_aws):
    publish_base()
    seed_proposal()
    with _first_update_reported_lost(versions_aws.table):
        state = store.open_draft(NS, "p-1", "alice")
    assert state == stored_state() and state.revision == 0
    assert store.revision_text(NS, "p-1", 0) == _accept_would_publish(1)
    assert store.get_record(NS, "p-1", 0)["kind"] == "open"


def test_require_state_wants_an_open_draft_and_optionally_an_editable_proposal():
    seed_proposal()
    with pytest.raises(store.NotOpen):
        store.require_state(stored_row(), editable=False)
    publish_base()
    store.open_draft(NS, "p-1", "alice")
    assert store.require_state(stored_row(), editable=True) == stored_state()
    dynamo_store.update_proposal("p-1", namespace=NS, status="accepted")
    assert store.require_state(stored_row(), editable=False) == stored_state()
    with pytest.raises(store.NotEditable) as raised:
        store.require_state(stored_row(), editable=True)
    assert raised.value.detail()["status"] == "accepted"


def test_commit_makes_the_claimed_revision_current():
    opened_draft()
    n = _claim()
    _commit(0, n)
    state = stored_state()
    assert (state.revision, state.applied, state.redo, state.counts) == (1, (1,), (), {"classes": 7})
    assert (state.updated_by, state.opened_by, state.base_version) == ("bob", "alice", 1)
    assert stored_row()["status"] == "updated"
    assert store.get_record(NS, "p-1", 1)["revision"] == 1


def test_a_retried_commit_that_had_already_landed_keeps_its_revision(versions_aws):
    opened_draft()
    n = store.claim_and_write(NS, "p-1", 1, {"kind": "change"}, *store.revision_text(NS, "p-1", 0))
    with _first_update_reported_lost(versions_aws.table):
        _commit(0, n)
    assert stored_state().revision == n
    assert store.load_revision(NS, "p-1", n).tbox_nt == store.revision_text(NS, "p-1", 0)[0]
    assert store.get_record(NS, "p-1", n)["kind"] == "change"


def test_commit_moves_the_base_only_for_a_rebase():
    opened_draft()
    _commit(0, _claim(), base_version=5)
    assert stored_state().base_version == 1
    _commit(1, _claim(2), base_version=5, rebased=True)
    assert stored_state().base_version == 5


def test_a_lost_commit_names_the_current_revision_and_leaves_its_claim_as_a_gap():
    opened_draft()
    text = store.revision_text(NS, "p-1", 0)
    first, second = (store.claim_and_write(NS, "p-1", 1, {"kind": "change"}, *text) for _ in range(2))
    assert (first, second) == (1, 2)  # two writers both read revision 0
    _commit(0, first)
    with pytest.raises(store.DraftConflict) as raised:
        _commit(0, second)
    assert raised.value.detail() == {
        "code": "DraftConflict",
        "message": "Another change was applied first; the draft is now at revision 1.",
        "revision": 1,
    }
    assert stored_state().revision == first
    assert store.load_revision(NS, "p-1", first).tbox_nt == text[0]  # the current revision is untouched
    for name in ("ontology.nt", "r2rml.nt"):  # the loser's revision stays: nothing walks to it, nothing deletes it
        assert store._get(store.revision_key(NS, "p-1", second, name)) is not None
    assert store.get_record(NS, "p-1", second)["revision"] == second


def test_commit_refuses_a_proposal_that_left_review():
    opened_draft()
    n = _claim()
    dynamo_store.update_proposal("p-1", namespace=NS, status="accepting")
    with pytest.raises(store.NotEditable):
        _commit(0, n)
    assert store.get_record(NS, "p-1", n)["revision"] == n
    assert stored_state().revision == 0 and stored_row()["status"] == "accepting"
    assert store.load_revision(NS, "p-1", 0).number == 0


def test_commit_without_an_open_draft_is_not_open():
    seed_proposal()
    n = _claim(0)
    with pytest.raises(store.NotOpen):
        _commit(0, n)
    assert store.get_record(NS, "p-1", n)["revision"] == n
