# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Accepting a proposal open in the Ontology designer publishes its draft (replace mode)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from coa_ontology import dynamo_store, proposals
from coa_ontology.studio import changes, ops, rebase
from coa_ontology.studio import draft as store
from coa_ontology.versions import deploy, publish, registry, snapshot, storage
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import URIRef
from rdflib.namespace import SH

from .abox_fakes import InlineThread
from .studio_fakes import (
    CUSTOMER,
    EMAIL,
    NS,
    ONTO,
    R2RML,
    SCHEME,
    TBOX,
    draft,
    opened_draft,
    publish_base,
    stored_row,
    stored_state,
)
from .test_versions_replace import CLIENT, CONTACT, REINDUCED_SHAPES, _reinduced_customer

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _no_source_ddl():
    """The sample mapping makes a publish build source DDL, which needs a source catalog."""
    with patch("coa_ontology.proposals._generate_schema_sql", return_value=None):
        yield


@pytest.fixture
def client(versions_aws):
    """A proposal open in the designer at revision 1 (Customer deleted) on top of v1."""
    store.clear_cache()
    app = FastAPI()
    app.include_router(proposals.router, prefix="/ontology/proposals")
    with (
        patch.object(proposals, "Thread", InlineThread),
        patch.object(deploy, "start_deployment", MagicMock(return_value="d-1")),
    ):
        opened_draft()
        changes.apply_change_set(NS, "p-1", [{"op": "delete_class", "iri": CUSTOMER}], 0, author="alice")
        yield TestClient(app)
    store.clear_cache()


def _accept(http, body: dict | None):
    return http.post(f"/ontology/proposals/p-1/accept?namespace={NS}", json=body)


def test_accept_publishes_the_reviewed_revision_as_the_whole_next_version(client):
    r = _accept(client, {"expectedRevision": 1, "deploy": False})
    assert r.status_code == 202, r.text
    row = stored_row()
    assert row["status"] == "accepted" and int(row["published_version"]) == 2
    diff = json.loads(storage.require_version_file(NS, 2, "diff.json"))
    assert CUSTOMER in diff["classes"]["removed"]
    assert CUSTOMER not in storage.require_version_file(NS, 2, "ontology.ttl")


def test_accept_needs_the_revision_the_user_reviewed(client):
    missing = _accept(client, {"deploy": False})
    assert missing.status_code == 422 and missing.json()["detail"]["code"] == "ExpectedRevisionRequired"
    stale = _accept(client, {"expectedRevision": 0})
    assert stale.status_code == 409 and stale.json()["detail"] == {
        "code": "DraftConflict",
        "message": "Another change was applied first; the draft is now at revision 1.",
        "revision": 1,
    }
    assert stored_row()["status"] == "updated"
    assert registry.head_version(NS)["version"] == 1


def test_accept_after_a_newer_version_requires_a_rebase(client):
    publish_base(proposal="p-other")
    r = _accept(client, {"expectedRevision": 1})
    assert r.status_code == 409
    assert r.json()["detail"] == {
        "code": "RebaseRequired",
        "message": "The draft was built on v1, but the newest version is v2; rebase it first.",
        "baseVersion": 1,
        "headVersion": 2,
    }
    # Refused before the flip: the proposal stays in review and nothing more was published.
    assert stored_row()["status"] == "updated"
    assert registry.head_version(NS)["version"] == 2


def test_a_change_landing_after_the_check_loses_to_the_conditional_flip(client):
    real = dynamo_store.update_proposal

    def change_first(*args, **kwargs):
        if kwargs.get("status") == "accepting":
            changes.apply_change_set(NS, "p-1", [{"op": "add_class", "label": "Late"}], 1, author="bob")
        return real(*args, **kwargs)

    with patch.object(dynamo_store, "update_proposal", side_effect=change_first):
        r = _accept(client, {"expectedRevision": 1})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "DraftConflict" and r.json()["detail"]["revision"] == 2
    assert stored_row()["status"] == "updated" and stored_state().revision == 2
    assert registry.head_version(NS)["version"] == 1


def test_a_repeated_accept_is_idempotent_only_for_the_revision_that_was_accepted(client):
    """A tab announces a 202 as the publish of the revision it sent, so a stale one hears DraftConflict."""
    changes.apply_change_set(NS, "p-1", [{"op": "add_class", "label": "Late"}], 1, author="bob")
    assert _accept(client, {"expectedRevision": 2, "deploy": False}).status_code == 202
    assert stored_row()["status"] == "accepted"
    stale = _accept(client, {"expectedRevision": 1, "deploy": True})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "DraftConflict" and stale.json()["detail"]["revision"] == 2
    missing = _accept(client, {"deploy": True})
    assert missing.status_code == 422 and missing.json()["detail"]["code"] == "ExpectedRevisionRequired"
    retried = _accept(client, {"expectedRevision": 2, "deploy": False})
    assert retried.status_code == 202 and retried.json() == {"proposal_id": "p-1", "status": "accepted"}
    assert registry.head_version(NS)["version"] == 2


def test_an_accept_in_progress_locks_the_draft_against_changes(client):
    class _NeverRuns(InlineThread):
        def start(self):
            pass

    with patch.object(proposals, "Thread", _NeverRuns):
        assert _accept(client, {"expectedRevision": 1}).status_code == 202
    assert stored_row()["status"] == "accepting"
    with pytest.raises(store.NotEditable):
        changes.apply_change_set(NS, "p-1", [{"op": "add_class", "label": "Late"}], 1, author="bob")
    again = _accept(client, {"expectedRevision": 1})
    assert again.status_code == 409 and again.json()["detail"] == "Accept already in progress for this proposal"


def test_a_publish_that_finds_a_newer_head_fails_the_accept_with_rebase_advice(client):
    stale = registry.head_version(NS)
    publish_base(proposal="p-other")  # v2 lands; the accept's own checks below still read v1
    with patch.object(registry, "head_version", return_value=stale):
        r = _accept(client, {"expectedRevision": 1})
    assert r.status_code == 202
    row = stored_row()
    assert row["status"] == "accept_failed" and "rebase the draft" in row["accept_error"]
    # The client tells this failure from any other by the code on accept_progress.
    assert row["accept_progress"] == {"step": "publish", "state": "failed", "code": "RebaseRequired"}
    assert registry.get_version(NS, 3)["status"] == registry.PUBLISH_FAILED


def test_a_draft_rebased_after_a_failed_accept_is_accepted_again(client):
    stale = registry.head_version(NS)
    publish_base(proposal="p-other")
    with patch.object(registry, "head_version", return_value=stale):
        assert _accept(client, {"expectedRevision": 1}).status_code == 202
    assert stored_row()["status"] == "accept_failed"

    rebased = rebase.rebase(NS, "p-1", 1, [], author="alice")  # an accept_failed draft is editable
    assert stored_row()["status"] == "updated" and rebased["baseVersion"] == 2

    r = _accept(client, {"expectedRevision": rebased["revision"], "deploy": False})
    assert r.status_code == 202, r.text
    row = stored_row()
    assert row["status"] == "accepted" and int(row["published_version"]) == 4
    assert row["accept_progress"] == {"step": "publish", "state": "done"}
    assert CUSTOMER not in storage.require_version_file(NS, 4, "ontology.ttl")


def test_a_failed_accept_of_a_draft_is_retried_at_the_same_revision(client):
    real = publish.publish_version
    calls: list[int] = []

    def busy_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise publish.PublishBusyError("another publish is running")
        return real(*args, **kwargs)

    with patch.object(publish, "publish_version", side_effect=busy_once):
        assert _accept(client, {"expectedRevision": 1, "deploy": False}).status_code == 202
        failed = stored_row()
        assert failed["status"] == "accept_failed" and "another publish is running" in failed["accept_error"]
        assert failed["accept_progress"] == {"step": "publish", "state": "failed"}
        assert stored_state().revision == 1
        assert _accept(client, {"expectedRevision": 1, "deploy": False}).status_code == 202
    row = stored_row()
    assert row["status"] == "accepted" and int(row["published_version"]) == 2 and row["accept_error"] == ""


def test_the_conditional_flip_needs_the_expected_studio_revision(client):
    with pytest.raises(ValueError, match="expected_status"):
        dynamo_store.update_proposal("p-1", namespace=NS, status="accepting", expected_studio_revision=1)
    with pytest.raises(ClientError) as stale:
        dynamo_store.update_proposal(
            "p-1", namespace=NS, status="accepting", expected_status="updated", expected_studio_revision=0
        )
    assert stale.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
    assert stored_row()["status"] == "updated"
    dynamo_store.update_proposal(
        "p-1", namespace=NS, status="accepting", expected_status="updated", expected_studio_revision=1
    )
    assert stored_row()["status"] == "accepting"


# ── An unedited draft of a re-induction publishes what a plain accept does ────

OTHER_NS = "22222222-3333-4444-5555-666666666666"


def _renamed_and_reinduced(namespace: str) -> None:
    """In ``namespace``: v1 the sample, v2 a designer accept that renamed Customer and its email attribute,
    and ``p-reinduce`` pending, a re-induction of the customer table that names the inducer's IRIs again."""

    def request(tbox: str, r2rml: str, **kwargs) -> publish.PublishRequest:
        return publish.PublishRequest(
            namespace=namespace,
            ontology_id=ONTO,
            ontology_turtle=tbox,
            r2rml_turtle=r2rml,
            source_proposal_id="p-earlier",
            created_by="alice",
            **kwargs,
        )

    assert publish.publish_version(request(TBOX, R2RML)) == 1
    renames = [
        {"op": "change_iri", "iri": CUSTOMER, "newIri": CLIENT},
        {"op": "change_iri", "iri": EMAIL, "newIri": CONTACT},
    ]
    renamed, _ = ops.apply_ops(draft(), ops.USER_OPS.validate_python(renames), SCHEME)
    tbox, r2rml = snapshot.canonical_nt(renamed.tbox), snapshot.canonical_nt(renamed.r2rml)
    assert publish.publish_version(request(tbox, r2rml, mode=publish.REPLACE, expected_parent_version=1)) == 2
    reinduced = _reinduced_customer()
    dynamo_store.create_proposal(
        proposal_id="p-reinduce",
        job_id="p-reinduce",
        namespace=namespace,
        ontology_id=ONTO,
        proposal_type="induction",
        ontology_turtle=reinduced.ontology_turtle,
        r2rml_turtle=reinduced.r2rml_turtle,
        source_type="STRUCTURED",
        metadata={"label": "Customers", "datasource_ids": [], "constraint_config": {"classes": []}},
    )


def _published_files(namespace: str, version: int) -> dict[str, str]:
    """Every file of the version but its manifest, which records who published it and when."""
    names = storage.read_manifest(namespace, version)["files"]
    return {name: storage.require_version_file(namespace, version, name) for name in names if name != "manifest.json"}


def test_an_unedited_draft_of_a_reinduction_publishes_the_shapes_a_plain_accept_does(versions_aws):
    """The proposal's compiled shapes name the inducer's IRIs; the version keeps the designer's renames of them.

    A plain accept renames the shapes as it renames the proposal. A designer accept publishes the draft, which
    holds the renamed terms already, so it needs the proposal's own Turtle to rename the shapes the same way;
    without it the shapes about the renamed class are pruned as shapes about terms the draft does not have.
    """
    store.clear_cache()
    app = FastAPI()
    app.include_router(proposals.router, prefix="/ontology/proposals")
    with (
        patch.object(proposals, "Thread", InlineThread),
        patch("coa_ontology.validation.shapes.config.ConstraintConfig", side_effect=lambda **kw: kw),
        patch("coa_ontology.validation.shapes.config.compile_to_shacl", return_value=REINDUCED_SHAPES),
    ):
        http = TestClient(app)
        for namespace in (NS, OTHER_NS):
            _renamed_and_reinduced(namespace)
        plain = http.post(f"/ontology/proposals/p-reinduce/accept?namespace={OTHER_NS}", json={"deploy": False})
        store.open_draft(NS, "p-reinduce", "alice")
        designer = http.post(
            f"/ontology/proposals/p-reinduce/accept?namespace={NS}", json={"expectedRevision": 0, "deploy": False}
        )
    store.clear_cache()

    assert plain.status_code == designer.status_code == 202, (plain.text, designer.text)
    assert stored_row("p-reinduce")["status"] == "accepted" and int(stored_row("p-reinduce")["published_version"]) == 3
    shapes = snapshot.parse_turtle(storage.require_version_file(NS, 3, "shapes.ttl"))
    assert set(shapes.objects(URIRef(f"{ONTO}CustomerShape"), SH.targetClass)) == {URIRef(CLIENT)}
    assert _published_files(NS, 3) == _published_files(OTHER_NS, 3)
