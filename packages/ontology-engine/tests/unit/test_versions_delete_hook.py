# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deleting the domain ontology interacts with versions: blocked mid-deploy, then stops serving."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from coa_ontology import dynamo_store
from coa_ontology.catalog.routers import ontologies
from coa_ontology.versions import deploy as version_deploy
from coa_ontology.versions import registry, storage
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

NS = "ns-1"
ONTO = "https://example.com/onto"
OTHER = "https://example.com/other"
EMIT = "coa_ontology.versions.deploy.emit_ontology_deployed"
CLEAR_ABOX = "coa_ontology.abox.build.clear_abox"
REQUEST_ABOX_BUILD = "coa_ontology.abox.build.request_build"


@pytest.fixture(autouse=True)
def abox_clear():
    """The domain ABox clear talks to Neptune; every test here sees it as a mock."""
    with patch(CLEAR_ABOX) as clear:
        yield clear


@pytest.fixture(autouse=True)
def abox_request():
    """A requested ABox build would start a worker thread; every test here sees the request as a mock."""
    with patch(REQUEST_ABOX_BUILD) as request:
        yield request


class _InlineThread:
    """Run the delete worker inline, so the test observes the lock after the worker finished."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _stores(graph=None, vector=None):
    graph = graph or MagicMock()
    vector = vector or MagicMock()
    vector.list_searchable_entity_uris.return_value = []
    return patch.object(ontologies, "_stores", return_value=(graph, vector))


def _http() -> TestClient:
    app = FastAPI()
    app.include_router(ontologies.router, prefix="/ontologies")
    return TestClient(app)


def _delete(**params):
    return _http().delete("/ontologies/", params={"namespace": NS, **params})


def _graph_with(uri: str) -> MagicMock:
    graph = MagicMock()
    graph.get_ontology_by_uri.return_value = {"uri": uri}
    return graph


def _induced(*ontology_ids: str) -> None:
    for oid in ontology_ids:
        dynamo_store.put_ontology_registry(NS, oid, {"uri": oid, "ontology_type": "induced"})


def _serve_v1(aws) -> None:
    """v1 of ONTO is published, ACTIVE and copied to ``latest/``."""
    registry.reserve_next_version(NS, ontology_id=ONTO, source="PROPOSAL", source_proposal_id="p", created_by="a")
    registry.mark_published(NS, 1, files={}, diff_summary={}, source_proposals=[])
    registry.set_active(NS, version=1, deployment_id="d", deployed_by="a")
    _put_latest(aws)


def _put_latest(aws) -> None:
    aws.s3.put_object(Bucket=aws.bucket, Key=f"ontologies/{NS}/latest/ontology.ttl", Body=b"x")


def _publish_v2_of_other(*, deploy: bool = False, aws=None) -> None:
    registry.reserve_next_version(NS, ontology_id=OTHER, source="PROPOSAL", source_proposal_id="p2", created_by="a")
    registry.mark_published(NS, 2, files={}, diff_summary={}, source_proposals=[])
    if deploy:
        registry.set_active(NS, version=2, deployment_id="d-2", deployed_by="a")
        aws.s3.put_object(Bucket=aws.bucket, Key=f"ontologies/{NS}/latest/ontology.ttl", Body=b"y")


def _first_delete_leaves_latest(aws, monkeypatch) -> None:
    """Delete ONTO (served as v1) while ``clear_latest`` keeps failing: ACTIVE is reset, ``latest/`` is not."""
    monkeypatch.setattr(version_deploy, "_STEP_BACKOFF_SECONDS", 0)
    _serve_v1(aws)
    _induced(ONTO)
    with (
        _stores(_graph_with(ONTO)),
        patch.object(ontologies, "Thread", _InlineThread),
        patch(EMIT),
        patch.object(storage, "clear_latest", side_effect=RuntimeError("S3 down")),
    ):
        assert _delete(ontology_id=ONTO).status_code == 202
    assert dynamo_store.get_ontology_registry(NS, ONTO)["delete_error"].startswith("Version reset failed")
    active = registry.get_active(NS)
    assert active["version"] is None and active["reset_after_version"] == 1
    assert active["reset_by_ontology_id"] == ONTO
    assert storage.read_latest(NS, "ontology.ttl") == "x"


def _retry_delete(ontology_id: str = ONTO):
    with _stores(_graph_with(ontology_id)), patch.object(ontologies, "Thread", _InlineThread), patch(EMIT) as emit:
        assert _delete(ontology_id=ontology_id).status_code == 202
    return emit


def _age_version(aws, version: int, seconds: int) -> None:
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    aws.table.update_item(
        Key={"PK": f"NAMESPACE#{NS}", "SK": f"VERSION#{version:06d}"},
        UpdateExpression="SET updated_at = :t",
        ExpressionAttributeValues={":t": old},
    )


# ── Single-ontology delete holds the deploy lock ──────────────────────────


def test_delete_is_refused_while_a_deployment_runs(versions_aws):
    dynamo_store.put_ontology_registry(NS, ONTO, {"uri": ONTO, "ontology_type": "induced"})
    registry.acquire_deploy_lock(NS, "d-1")
    graph = _graph_with(ONTO)
    with _stores(graph):
        r = _delete(ontology_id=ONTO)
    assert r.status_code == 409 and "d-1" in r.json()["detail"]
    assert registry.deploy_lock_holder(NS) == "d-1"
    assert dynamo_store.get_ontology_registry(NS, ONTO).get("status") is None
    graph.delete_ontology.assert_not_called()


def test_delete_holds_the_deploy_lock_until_the_worker_finishes(versions_aws):
    dynamo_store.put_ontology_registry(NS, ONTO, {"uri": ONTO, "ontology_type": "induced"})
    graph = _graph_with(ONTO)
    holders: list[str | None] = []
    graph.delete_ontology.side_effect = lambda _id: holders.append(registry.deploy_lock_holder(NS))
    with (
        _stores(graph),
        patch.object(ontologies, "Thread", _InlineThread),
        patch("coa_ontology.versions.deploy.emit_ontology_deployed"),
    ):
        r = _delete(ontology_id=ONTO)
    assert r.status_code == 202
    assert len(holders) == 1 and (holders[0] or "").startswith("delete-")
    assert registry.deploy_lock_holder(NS) is None
    assert dynamo_store.get_ontology_registry(NS, ONTO) is None


def test_induced_guard_refusal_releases_the_lock(versions_aws):
    uploaded = "https://example.com/uploaded"
    dynamo_store.put_ontology_registry(NS, uploaded, {"uri": uploaded, "ontology_type": "user_uploaded"})
    dynamo_store.put_ontology_registry(NS, ONTO, {"uri": ONTO, "ontology_type": "induced"})
    graph = _graph_with(uploaded)
    with _stores(graph):
        r = _delete(ontology_id=uploaded)
    assert r.status_code == 409 and "induced ontologies exist" in r.json()["detail"]
    assert registry.deploy_lock_holder(NS) is None
    graph.delete_ontology.assert_not_called()


def test_status_flip_failure_releases_the_lock(versions_aws):
    dynamo_store.put_ontology_registry(NS, ONTO, {"uri": ONTO, "ontology_type": "induced"})
    graph = _graph_with(ONTO)
    with _stores(graph), patch.object(dynamo_store, "update_ontology_registry", side_effect=RuntimeError("throttled")):
        r = _delete(ontology_id=ONTO)
    assert r.status_code == 500
    assert registry.deploy_lock_holder(NS) is None
    graph.delete_ontology.assert_not_called()


def test_worker_releases_the_lock_when_the_graph_drop_fails(versions_aws):
    registry.acquire_deploy_lock(NS, "delete-1")
    graph = MagicMock()
    graph.delete_ontology.side_effect = RuntimeError("Neptune throttled")
    with _stores(graph):
        ontologies._delete_ontology_backend(NS, ONTO, [], version_reset="reset", lock_id="delete-1")
    assert registry.deploy_lock_holder(NS) is None


def test_worker_heartbeats_the_lock_after_the_graph_drop_and_the_embedding_teardown(versions_aws):
    registry.acquire_deploy_lock(NS, "delete-1")
    graph, vector = MagicMock(), MagicMock()
    with patch.object(registry, "heartbeat_deploy_lock", wraps=registry.heartbeat_deploy_lock) as beat:
        beats_seen: list[int] = []
        graph.delete_ontology.side_effect = lambda _id: beats_seen.append(beat.call_count)
        vector.delete_embeddings_for_ontology.side_effect = lambda *_a, **_k: beats_seen.append(beat.call_count)
        with _stores(graph, vector):
            ontologies._delete_ontology_backend(NS, ONTO, [], lock_id="delete-1")
    assert beats_seen == [0, 1]
    assert beat.call_count == 2
    beat.assert_called_with(NS, "delete-1")
    assert registry.deploy_lock_holder(NS) is None


# ── Single-ontology delete vs. other holders of the lock ─────────────────


def test_reissued_delete_while_its_own_delete_runs_is_accepted_without_a_second_worker(versions_aws):
    _induced(ONTO)
    graph = _graph_with(ONTO)
    with _stores(graph), patch.object(ontologies, "Thread") as thread:
        first = _delete(ontology_id=ONTO)
        holder = registry.deploy_lock_holder(NS)
        again = _delete(ontology_id=ONTO)
    assert first.status_code == 202 and again.status_code == 202
    assert thread.call_count == 1
    row = dynamo_store.get_ontology_registry(NS, ONTO)
    assert row["status"] == "deleting" and row["delete_lock_id"] == holder
    assert registry.deploy_lock_holder(NS) == holder


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"status": "deleting", "delete_lock_id": "delete-dead"},
        {"status": "deleting", "delete_error": "Graph delete failed: throttled", "delete_lock_id": "delete-dead"},
        {"status": "deleting"},
    ],
    ids=["live", "own-dead", "own-failed-earlier", "legacy"],
)
def test_delete_while_another_delete_runs_is_refused(versions_aws, row):
    dynamo_store.put_ontology_registry(NS, OTHER, {"uri": OTHER, "ontology_type": "induced", **row})
    registry.acquire_deploy_lock(NS, "delete-running")
    graph = _graph_with(OTHER)
    with _stores(graph), patch.object(ontologies, "Thread") as thread:
        r = _delete(ontology_id=OTHER)
    assert r.status_code == 409
    assert r.json()["detail"] == "Another ontology delete is in progress in this namespace; retry shortly."
    thread.assert_not_called()
    assert registry.deploy_lock_holder(NS) == "delete-running"


def test_reissued_delete_while_its_own_failed_worker_is_finishing_is_refused(versions_aws):
    dynamo_store.put_ontology_registry(
        NS,
        ONTO,
        {
            "uri": ONTO,
            "ontology_type": "induced",
            "status": "deleting",
            "delete_error": "Graph delete failed: throttled",
            "delete_lock_id": "delete-running",
        },
    )
    registry.acquire_deploy_lock(NS, "delete-running")
    with _stores(_graph_with(ONTO)), patch.object(ontologies, "Thread") as thread:
        r = _delete(ontology_id=ONTO)
    assert r.status_code == 409
    assert r.json()["detail"] == "This ontology's previous delete is still finishing; retry shortly."
    thread.assert_not_called()


def test_a_lock_holder_that_vanished_is_reported_generically(versions_aws):
    _induced(ONTO)
    with (
        _stores(_graph_with(ONTO)),
        patch.object(registry, "acquire_deploy_lock", side_effect=registry.DeployLockHeldError("")),
        patch.object(ontologies, "Thread") as thread,
    ):
        r = _delete(ontology_id=ONTO)
    assert r.status_code == 409
    assert r.json()["detail"] == "Another deployment or delete is in progress in this namespace; retry shortly."
    thread.assert_not_called()


def test_the_registry_row_is_read_again_once_the_lock_is_held(versions_aws):
    acquire = registry.acquire_deploy_lock

    def acquire_after_a_deployment_wrote_the_row(namespace, holder):
        _induced(ONTO)  # the deployment that held the lock wrote the row before releasing it
        acquire(namespace, holder)

    with (
        _stores(_graph_with(ONTO)),
        patch.object(registry, "acquire_deploy_lock", side_effect=acquire_after_a_deployment_wrote_the_row),
        patch.object(ontologies, "Thread"),
    ):
        r = _delete(ontology_id=ONTO)
    assert r.status_code == 202
    assert dynamo_store.get_ontology_registry(NS, ONTO)["status"] == "deleting"


def test_delete_is_refused_while_a_version_of_the_ontology_publishes(versions_aws):
    registry.reserve_next_version(NS, ontology_id=ONTO, source="PROPOSAL", source_proposal_id="p", created_by="a")
    _induced(ONTO)
    graph = _graph_with(ONTO)
    with _stores(graph), patch.object(ontologies, "Thread") as thread:
        r = _delete(ontology_id=ONTO)
    assert r.status_code == 409
    assert r.json()["detail"] == "A version publish is in progress; retry after it finishes."
    thread.assert_not_called()
    assert registry.deploy_lock_holder(NS) is None
    assert dynamo_store.get_ontology_registry(NS, ONTO).get("status") is None


def test_a_publish_of_the_domain_ontology_does_not_block_deleting_another_one(versions_aws):
    registry.reserve_next_version(NS, ontology_id=ONTO, source="PROPOSAL", source_proposal_id="p", created_by="a")
    _induced(ONTO, OTHER)
    with _stores(_graph_with(OTHER)), patch.object(ontologies, "Thread", _InlineThread), patch(EMIT) as emit:
        r = _delete(ontology_id=OTHER)
    assert r.status_code == 202
    assert dynamo_store.get_ontology_registry(NS, OTHER) is None
    assert registry.get_version(NS, 1)["status"] == registry.PUBLISHING
    emit.assert_not_called()


@pytest.mark.parametrize("deleted", [OTHER, ONTO], ids=["the-publishing-one", "the-served-one"])
def test_a_publish_blocks_deleting_its_ontology_and_a_delete_that_resets_serving(versions_aws, deleted):
    # Each condition alone: v2 publishes OTHER (deleting OTHER does not reset ACTIVE = v1 of ONTO),
    # and deleting ONTO resets serving while another ontology's version publishes.
    _serve_v1(versions_aws)
    registry.reserve_next_version(NS, ontology_id=OTHER, source="PROPOSAL", source_proposal_id="p2", created_by="a")
    _induced(ONTO, OTHER)
    graph = _graph_with(deleted)
    with _stores(graph), patch.object(ontologies, "Thread") as thread:
        r = _delete(ontology_id=deleted)
    assert r.status_code == 409
    assert r.json()["detail"] == "A version publish is in progress; retry after it finishes."
    thread.assert_not_called()
    assert registry.deploy_lock_holder(NS) is None


def test_a_publish_blocks_resuming_a_partial_reset(versions_aws, monkeypatch):
    _first_delete_leaves_latest(versions_aws, monkeypatch)
    registry.reserve_next_version(NS, ontology_id=OTHER, source="PROPOSAL", source_proposal_id="p2", created_by="a")
    with _stores(_graph_with(ONTO)), patch.object(ontologies, "Thread") as thread:
        r = _delete(ontology_id=ONTO)
    assert r.status_code == 409
    assert r.json()["detail"] == "A version publish is in progress; retry after it finishes."
    thread.assert_not_called()


def test_a_stale_publish_does_not_block_the_delete(versions_aws):
    registry.reserve_next_version(NS, ontology_id=ONTO, source="PROPOSAL", source_proposal_id="p", created_by="a")
    _age_version(versions_aws, 1, registry.PUBLISH_STALE_SECONDS + 60)
    _induced(ONTO)
    with _stores(_graph_with(ONTO)), patch.object(ontologies, "Thread", _InlineThread), patch(EMIT):
        r = _delete(ontology_id=ONTO)
    assert r.status_code == 202
    assert registry.get_version(NS, 1)["status"] == registry.PUBLISH_FAILED
    assert dynamo_store.get_ontology_registry(NS, ONTO) is None


# ── After the delete: stop serving ────────────────────────────────────────


def test_deleting_the_domain_ontology_stops_serving_it(versions_aws):
    _serve_v1(versions_aws)
    _induced(ONTO)
    with _stores(_graph_with(ONTO)), patch.object(ontologies, "Thread", _InlineThread), patch(EMIT) as emit:
        r = _delete(ontology_id=ONTO)
    assert r.status_code == 202
    active = registry.get_active(NS)
    assert active["version"] is None and active["reset_after_version"] == 1
    assert storage.read_latest(NS, "ontology.ttl") is None
    emit.assert_called_once_with(NS, None, None)
    assert dynamo_store.get_ontology_registry(NS, ONTO) is None
    assert registry.deploy_lock_holder(NS) is None


def test_deleting_the_domain_ontology_stops_vkg_serving_before_it_clears_the_abox_and_requests_a_rebuild(
    versions_aws, abox_clear, abox_request
):
    _serve_v1(versions_aws)
    _induced(ONTO)
    calls: list[str] = []
    abox_clear.side_effect = lambda *_a, **_k: calls.append("clear_abox")
    abox_request.side_effect = lambda *_a, **_k: calls.append("request_build")
    clear_latest = storage.clear_latest

    def recording_clear_latest(namespace):
        calls.append("clear_latest")
        clear_latest(namespace)

    with (
        _stores(_graph_with(ONTO)),
        patch.object(ontologies, "Thread", _InlineThread),
        patch.object(storage, "clear_latest", side_effect=recording_clear_latest),
        patch(EMIT, side_effect=lambda *_a, **_k: calls.append("emit")) as emit,
    ):
        assert _delete(ontology_id=ONTO).status_code == 202
    emit.assert_called_once_with(NS, None, None)
    abox_clear.assert_called_once_with(NS)
    # VKG stops serving first: dropping a large ABox may be slow or fail, and must not delay that.
    # A build racing the delete may still swap its ABox in after the clear; ACTIVE is already reset,
    # so the requested build clears whatever it left behind.
    abox_request.assert_called_once_with(NS, "domain ontology deleted")
    assert calls == ["clear_latest", "emit", "clear_abox", "request_build"]
    assert dynamo_store.get_ontology_registry(NS, ONTO) is None


def test_a_failing_rebuild_request_does_not_fail_the_delete(versions_aws, abox_request, caplog):
    _serve_v1(versions_aws)
    _induced(ONTO)
    abox_request.side_effect = RuntimeError("DynamoDB 500")
    with _stores(_graph_with(ONTO)), patch.object(ontologies, "Thread", _InlineThread), patch(EMIT) as emit:
        assert _delete(ontology_id=ONTO).status_code == 202
    abox_request.assert_called_once_with(NS, "domain ontology deleted")
    emit.assert_called_once_with(NS, None, None)
    assert storage.read_latest(NS, "ontology.ttl") is None
    assert dynamo_store.get_ontology_registry(NS, ONTO) is None
    assert any("domain ABox rebuild" in r.getMessage() for r in caplog.records)


def test_a_failed_abox_clear_requests_no_rebuild(versions_aws, monkeypatch, abox_clear, abox_request):
    monkeypatch.setattr(version_deploy, "_STEP_BACKOFF_SECONDS", 0)
    _serve_v1(versions_aws)
    _induced(ONTO)
    abox_clear.side_effect = RuntimeError("Neptune down")
    with _stores(_graph_with(ONTO)), patch.object(ontologies, "Thread", _InlineThread), patch(EMIT):
        assert _delete(ontology_id=ONTO).status_code == 202
    abox_request.assert_not_called()


def test_a_failed_abox_clear_keeps_the_row_for_a_reissued_delete_after_vkg_stopped_serving(
    versions_aws, monkeypatch, abox_clear
):
    monkeypatch.setattr(version_deploy, "_STEP_BACKOFF_SECONDS", 0)
    _serve_v1(versions_aws)
    _induced(ONTO)
    abox_clear.side_effect = RuntimeError("Neptune down")
    vector = MagicMock()
    with _stores(_graph_with(ONTO), vector), patch.object(ontologies, "Thread", _InlineThread), patch(EMIT) as emit:
        assert _delete(ontology_id=ONTO).status_code == 202
    assert abox_clear.call_count == version_deploy._STEP_MAX_ATTEMPTS
    row = dynamo_store.get_ontology_registry(NS, ONTO)
    assert row["status"] == "deleting" and row["delete_error"].startswith("Version reset failed: RuntimeError")
    # Serving stopped before the clear was attempted; only the embedding teardown and the row delete wait.
    assert storage.read_latest(NS, "ontology.ttl") is None
    emit.assert_called_once_with(NS, None, None)
    vector.delete_embeddings_for_ontology.assert_not_called()

    abox_clear.side_effect = None
    abox_clear.reset_mock()
    with _stores(_graph_with(ONTO), vector), patch.object(ontologies, "Thread", _InlineThread), patch(EMIT) as emit:
        assert _delete(ontology_id=ONTO).status_code == 202
    abox_clear.assert_called_once_with(NS)
    emit.assert_called_once_with(NS, None, None)  # every step runs again: all are idempotent
    vector.delete_embeddings_for_ontology.assert_called_once_with(ONTO, namespace=NS)
    assert dynamo_store.get_ontology_registry(NS, ONTO) is None


def test_a_resumed_reset_clears_the_abox_and_requests_a_rebuild_too(
    versions_aws, monkeypatch, abox_clear, abox_request
):
    _first_delete_leaves_latest(versions_aws, monkeypatch)
    abox_clear.reset_mock()
    abox_request.reset_mock()
    _retry_delete()
    abox_clear.assert_called_once_with(NS)
    abox_request.assert_called_once_with(NS, "domain ontology deleted")


def test_before_any_version_deleting_the_only_induced_ontology_stops_serving_it(versions_aws):
    _put_latest(versions_aws)
    _induced(ONTO)
    with _stores(_graph_with(ONTO)), patch.object(ontologies, "Thread", _InlineThread), patch(EMIT) as emit:
        r = _delete(ontology_id=ONTO)
    assert r.status_code == 202
    active = registry.get_active(NS)
    assert active["version"] is None and active["reset_after_version"] == 0
    assert storage.read_latest(NS, "ontology.ttl") is None
    emit.assert_called_once_with(NS, None, None)


@pytest.mark.parametrize("with_versions", [True, False], ids=["versions", "no-versions"])
def test_deleting_an_induced_ontology_that_is_not_the_domain_one_leaves_serving_alone(versions_aws, with_versions):
    if with_versions:
        _serve_v1(versions_aws)
    else:
        _put_latest(versions_aws)
    _induced(ONTO, OTHER)
    with _stores(_graph_with(OTHER)), patch.object(ontologies, "Thread", _InlineThread), patch(EMIT) as emit:
        r = _delete(ontology_id=OTHER)
    assert r.status_code == 202
    assert dynamo_store.get_ontology_registry(NS, OTHER) is None
    active = registry.get_active(NS)
    if with_versions:
        assert active["version"] == 1
    else:
        assert active is None
    assert storage.read_latest(NS, "ontology.ttl") == "x"
    emit.assert_not_called()


@pytest.mark.parametrize("ontology_type", ["induced", "foundational"])
def test_deleting_an_ontology_that_is_not_the_domain_one_leaves_the_abox_alone(
    versions_aws, abox_clear, abox_request, ontology_type
):
    _serve_v1(versions_aws)
    if ontology_type == "induced":
        _induced(ONTO, OTHER)
    else:  # a foundational ontology can only be deleted while no induced one exists
        dynamo_store.put_ontology_registry(NS, OTHER, {"uri": OTHER, "ontology_type": ontology_type})
    with _stores(_graph_with(OTHER)), patch.object(ontologies, "Thread", _InlineThread), patch(EMIT):
        assert _delete(ontology_id=OTHER).status_code == 202
    assert dynamo_store.get_ontology_registry(NS, OTHER) is None
    abox_clear.assert_not_called()
    abox_request.assert_not_called()


def test_deleting_the_ontology_that_took_over_resets_serving_again(versions_aws):
    # ONTO was deleted; OTHER's v2 was then published and deployed.
    _serve_v1(versions_aws)
    registry.reset_active(NS, ontology_id=ONTO)
    _publish_v2_of_other(deploy=True, aws=versions_aws)
    _induced(OTHER)
    emit = _retry_delete(OTHER)
    active = registry.get_active(NS)
    assert active["version"] is None and active["reset_after_version"] == 2
    assert active["reset_by_ontology_id"] == OTHER
    assert storage.read_latest(NS, "ontology.ttl") is None
    emit.assert_called_once_with(NS, None, None)


def test_deleting_a_foundational_ontology_leaves_serving_alone(versions_aws):
    fibo = "https://fibo.example/onto"
    _put_latest(versions_aws)
    dynamo_store.put_ontology_registry(NS, fibo, {"uri": fibo, "ontology_type": "foundational"})
    with _stores(_graph_with(fibo)), patch.object(ontologies, "Thread", _InlineThread), patch(EMIT) as emit:
        r = _delete(ontology_id=fibo)
    assert r.status_code == 202
    assert registry.get_active(NS) is None
    assert storage.read_latest(NS, "ontology.ttl") == "x"
    emit.assert_not_called()


def test_the_version_reset_runs_between_the_graph_drop_and_the_embedding_teardown(versions_aws):
    _serve_v1(versions_aws)
    graph, vector = MagicMock(), MagicMock()
    seen: list[tuple[str, int | None]] = []
    graph.delete_ontology.side_effect = lambda _id: seen.append(("drop", registry.get_active(NS)["version"]))
    vector.delete_embeddings_for_ontology.side_effect = lambda *_a, **_k: seen.append(
        ("embeddings", registry.get_active(NS)["version"])
    )
    with _stores(graph, vector), patch(EMIT):
        ontologies._delete_ontology_backend(NS, ONTO, [], version_reset="reset")
    assert seen == [("drop", 1), ("embeddings", None)]


def test_a_failed_version_reset_keeps_the_row_for_a_reissued_delete(versions_aws, monkeypatch):
    monkeypatch.setattr(version_deploy, "_STEP_BACKOFF_SECONDS", 0)
    _serve_v1(versions_aws)
    _induced(ONTO)
    graph, vector = _graph_with(ONTO), MagicMock()
    with (
        _stores(graph, vector),
        patch.object(ontologies, "Thread", _InlineThread),
        patch(EMIT, side_effect=RuntimeError("EventBridge down")) as emit,
    ):
        r = _delete(ontology_id=ONTO)
    assert r.status_code == 202
    assert emit.call_count == version_deploy._STEP_MAX_ATTEMPTS
    row = dynamo_store.get_ontology_registry(NS, ONTO)
    assert row["status"] == "deleting" and row["delete_error"].startswith("Version reset failed: RuntimeError")
    vector.delete_embeddings_for_ontology.assert_not_called()
    assert registry.deploy_lock_holder(NS) is None

    with _stores(graph, vector), patch.object(ontologies, "Thread", _InlineThread), patch(EMIT) as emit:
        r = _delete(ontology_id=ONTO)
    assert r.status_code == 202
    emit.assert_called_once_with(NS, None, None)
    assert registry.get_active(NS)["version"] is None
    assert storage.read_latest(NS, "ontology.ttl") is None
    vector.delete_embeddings_for_ontology.assert_called_once_with(ONTO, namespace=NS)
    assert dynamo_store.get_ontology_registry(NS, ONTO) is None
    assert registry.deploy_lock_holder(NS) is None


def test_a_retry_resumes_a_partial_reset_without_touching_active(versions_aws, monkeypatch):
    _first_delete_leaves_latest(versions_aws, monkeypatch)
    active = registry.get_active(NS)
    emit = _retry_delete()
    assert storage.read_latest(NS, "ontology.ttl") is None
    emit.assert_called_once_with(NS, None, None)
    assert registry.get_active(NS) == active
    assert dynamo_store.get_ontology_registry(NS, ONTO) is None


def test_a_retry_after_another_ontology_was_deployed_leaves_its_serving_alone(versions_aws, monkeypatch):
    _first_delete_leaves_latest(versions_aws, monkeypatch)
    _publish_v2_of_other(deploy=True, aws=versions_aws)
    active = registry.get_active(NS)
    emit = _retry_delete()
    assert registry.get_active(NS) == active and active["version"] == 2
    assert storage.read_latest(NS, "ontology.ttl") == "y"
    emit.assert_not_called()
    assert dynamo_store.get_ontology_registry(NS, ONTO) is None


def test_a_retry_after_another_ontology_was_only_published_keeps_its_lineage(versions_aws, monkeypatch):
    _first_delete_leaves_latest(versions_aws, monkeypatch)
    _publish_v2_of_other()
    emit = _retry_delete()
    assert storage.read_latest(NS, "ontology.ttl") is None
    emit.assert_called_once_with(NS, None, None)
    assert registry.get_active(NS)["reset_after_version"] == 1
    version, parent = registry.reserve_next_version(
        NS, ontology_id=OTHER, source="PROPOSAL", source_proposal_id="p3", created_by="a"
    )
    assert (version, parent) == (3, 2)


def test_deleting_a_recreated_ontology_that_was_only_published_moves_the_floor_again(versions_aws):
    # ONTO was deleted, then induced again under the same id and published (not deployed) as v2.
    _serve_v1(versions_aws)
    registry.reset_active(NS, ontology_id=ONTO)
    registry.reserve_next_version(NS, ontology_id=ONTO, source="PROPOSAL", source_proposal_id="p2", created_by="a")
    registry.mark_published(NS, 2, files={}, diff_summary={}, source_proposals=[])
    _induced(ONTO)
    emit = _retry_delete()
    active = registry.get_active(NS)
    assert active["reset_after_version"] == 2 and active["reset_by_ontology_id"] == ONTO
    emit.assert_called_once_with(NS, None, None)
    assert registry.head_version(NS) is None


def _version(number: int, ontology_id: str, *, published: bool) -> None:
    registry.reserve_next_version(
        NS, ontology_id=ontology_id, source="PROPOSAL", source_proposal_id=f"p{number}", created_by="a"
    )
    if published:
        registry.mark_published(NS, number, files={}, diff_summary={}, source_proposals=[])
    else:
        registry.mark_publish_failed(NS, number, "snapshot write failed")


def test_a_failed_publish_of_another_ontology_on_top_does_not_hide_the_lineage_head(versions_aws):
    _version(1, ONTO, published=True)
    _version(2, OTHER, published=False)
    _induced(ONTO)
    emit = _retry_delete()
    active = registry.get_active(NS)
    assert active["version"] is None and active["reset_by_ontology_id"] == ONTO
    assert active["reset_after_version"] == 2
    emit.assert_called_once_with(NS, None, None)


def test_a_failed_publish_of_the_deleted_ontology_on_top_leaves_the_lineage_deployable(versions_aws):
    _version(1, OTHER, published=True)
    _version(2, ONTO, published=False)
    _induced(ONTO)
    emit = _retry_delete()
    emit.assert_not_called()
    assert registry.get_active(NS) is None
    # Past the reset-floor check: the deploy only fails on the (absent) snapshot.
    with pytest.raises(version_deploy.DeployConflictError, match="no complete snapshot"):
        version_deploy.start_deployment(NS, 1, requested_by="a")


def test_a_resumed_reset_is_fenced_on_the_delete_lock(versions_aws, monkeypatch, abox_clear, abox_request):
    _first_delete_leaves_latest(versions_aws, monkeypatch)
    abox_clear.reset_mock()
    abox_request.reset_mock()
    registry.acquire_deploy_lock(NS, "d-2")  # the delete lost its lock to a deployment
    with patch(EMIT) as emit, pytest.raises(registry.DeployLockLostError):
        ontologies._reset_versions_after_delete(NS, ONTO, "delete-1", resume=True)
    assert storage.read_latest(NS, "ontology.ttl") == "x"
    emit.assert_not_called()
    abox_clear.assert_not_called()
    abox_request.assert_not_called()


def test_a_superseded_worker_cannot_stamp_the_new_delete(versions_aws):
    # A re-issued delete took the stale lock over and re-flipped the row; the old worker fails late.
    dynamo_store.put_ontology_registry(
        NS,
        ONTO,
        {
            "uri": ONTO,
            "ontology_type": "induced",
            "status": "deleting",
            "delete_error": "",
            "delete_lock_id": "delete-new",
        },
    )
    registry.acquire_deploy_lock(NS, "delete-new")
    graph = MagicMock()
    graph.delete_ontology.side_effect = RuntimeError("Neptune throttled")
    with _stores(graph):
        ontologies._delete_ontology_backend(NS, ONTO, [], lock_id="delete-old")
    row = dynamo_store.get_ontology_registry(NS, ONTO)
    assert row["delete_error"] == "" and row["delete_lock_id"] == "delete-new"
    assert registry.deploy_lock_holder(NS) == "delete-new"


def test_a_lost_lock_stops_the_teardown_at_the_version_reset(versions_aws):
    _serve_v1(versions_aws)
    dynamo_store.put_ontology_registry(
        NS, ONTO, {"uri": ONTO, "ontology_type": "induced", "status": "deleting", "delete_lock_id": "delete-1"}
    )
    registry.acquire_deploy_lock(NS, "d-2")  # a deployment took the lock over from the delete
    graph, vector = MagicMock(), MagicMock()
    with _stores(graph, vector), patch(EMIT) as emit:
        ontologies._delete_ontology_backend(NS, ONTO, [], version_reset="reset", lock_id="delete-1")
    graph.delete_ontology.assert_called_once_with(ONTO)
    row = dynamo_store.get_ontology_registry(NS, ONTO)
    assert row["status"] == "deleting"
    assert row["delete_error"].startswith("Version reset failed: DeployLockLostError")
    vector.delete_embeddings_for_ontology.assert_not_called()
    emit.assert_not_called()
    assert registry.get_active(NS)["version"] == 1
    assert registry.deploy_lock_holder(NS) == "d-2"


def test_a_failed_registry_row_delete_is_recorded_on_the_row(versions_aws):
    _induced(ONTO)
    with (
        _stores(_graph_with(ONTO)),
        patch.object(ontologies, "Thread", _InlineThread),
        patch(EMIT),
        patch.object(dynamo_store, "delete_ontology_registry", side_effect=RuntimeError("throttled")),
    ):
        r = _delete(ontology_id=ONTO)
    assert r.status_code == 202
    row = dynamo_store.get_ontology_registry(NS, ONTO)
    assert row["status"] == "deleting"
    # The exception type only: deleteError is returned by ListOntologies.
    assert row["delete_error"] == "Registry row delete failed: RuntimeError"
    assert registry.deploy_lock_holder(NS) is None


def test_a_delete_error_never_recreates_a_removed_row(versions_aws):
    ontologies._mark_delete_error(NS, ONTO, "Graph delete failed: late")
    assert dynamo_store.get_ontology_registry(NS, ONTO) is None


def test_the_reset_is_fenced_on_the_delete_lock(versions_aws, abox_clear, abox_request):
    _serve_v1(versions_aws)
    registry.acquire_deploy_lock(NS, "d-2")  # the delete lost its lock to a deployment
    with patch(EMIT) as emit, pytest.raises(registry.DeployLockLostError):
        ontologies._reset_versions_after_delete(NS, ONTO, "delete-1")
    assert registry.get_active(NS)["version"] == 1
    assert storage.read_latest(NS, "ontology.ttl") == "x"
    emit.assert_not_called()
    abox_clear.assert_not_called()
    abox_request.assert_not_called()


# ── Reading a delete whose worker died ────────────────────────────────────

DEAD = "Delete worker did not complete; retry the delete."


def _deleting(lock_id: str | None = "delete-dead", **extra) -> None:
    data = {"uri": ONTO, "ontology_type": "induced", "status": "deleting", "delete_error": "", **extra}
    if lock_id:
        data["delete_lock_id"] = lock_id
    dynamo_store.put_ontology_registry(NS, ONTO, data)


def _listed_delete_error() -> str | None:
    (row,) = _http().get("/ontologies/", params={"namespace": NS}).json()["ontologies"]
    return row["deleteError"]


@pytest.mark.parametrize(
    "lock_id, holder, stale",
    [
        ("delete-dead", None, False),
        ("delete-dead", "delete-dead", True),
        ("delete-dead", "delete-other", False),
        (None, "delete-other", False),
    ],
    ids=["lock-free", "lock-stale", "other-holder", "legacy-row"],
)
def test_reading_a_delete_whose_worker_is_gone_stamps_a_retryable_error(versions_aws, lock_id, holder, stale):
    _deleting(lock_id)
    if holder:
        registry.acquire_deploy_lock(NS, holder)
        if stale:
            versions_aws.table.update_item(
                Key={"PK": f"NAMESPACE#{NS}", "SK": "DEPLOYLOCK"},
                UpdateExpression="SET heartbeat_ts = :ts",
                ExpressionAttributeValues={":ts": 0},
            )
    assert _listed_delete_error() == DEAD
    assert dynamo_store.get_ontology_registry(NS, ONTO)["delete_error"] == DEAD


def test_reading_a_delete_whose_worker_holds_the_lock_leaves_it_in_progress(versions_aws):
    _deleting("delete-live")
    registry.acquire_deploy_lock(NS, "delete-live")
    assert not _listed_delete_error()
    with _stores(_graph_with(ONTO)):
        assert not _http().get(f"/ontologies/{ONTO}", params={"namespace": NS}).json()["delete_error"]
    assert not dynamo_store.get_ontology_registry(NS, ONTO)["delete_error"]


def test_reading_one_ontology_also_stamps_a_dead_delete(versions_aws):
    _deleting()
    with _stores(_graph_with(ONTO)):
        body = _http().get(f"/ontologies/{ONTO}", params={"namespace": NS}).json()
    assert body["delete_error"] == DEAD


def test_a_dead_delete_can_be_reissued_after_the_stamp(versions_aws):
    _deleting()
    assert _listed_delete_error() == DEAD
    with _stores(_graph_with(ONTO)), patch.object(ontologies, "Thread", _InlineThread), patch(EMIT):
        r = _delete(ontology_id=ONTO)
    assert r.status_code == 202
    assert dynamo_store.get_ontology_registry(NS, ONTO) is None


def test_the_stamp_loses_to_a_worker_that_finished_meanwhile(versions_aws, caplog):
    _deleting()
    listed = dynamo_store.list_ontologies_registry(NS)
    dynamo_store.delete_ontology_registry(NS, ONTO)  # the worker removed the row after the listing read it
    with (
        patch.object(dynamo_store, "list_ontologies_registry_page", return_value=(listed, None)),
        caplog.at_level("INFO"),
    ):
        assert not _listed_delete_error()
    assert dynamo_store.get_ontology_registry(NS, ONTO) is None
    assert any(r.levelname == "INFO" and "changed" in r.getMessage() for r in caplog.records)


def test_the_stamp_loses_to_a_reissued_delete(versions_aws):
    _deleting("delete-dead")
    listed = dynamo_store.list_ontologies_registry(NS)
    _deleting("delete-new")  # a re-issued delete flipped the row after the listing read it
    registry.acquire_deploy_lock(NS, "delete-other")
    with patch.object(dynamo_store, "list_ontologies_registry_page", return_value=(listed, None)):
        _listed_delete_error()
    row = dynamo_store.get_ontology_registry(NS, ONTO)
    assert row["delete_lock_id"] == "delete-new" and not row["delete_error"]


# ── Namespace-wide delete ─────────────────────────────────────────────────


def test_namespace_delete_drops_leftover_staging_graphs_before_the_purge(versions_aws):
    calls = MagicMock()
    calls.purge.return_value = 0
    vector = MagicMock()
    vector.delete_index.return_value = True
    with (
        _stores(vector=vector),
        patch("coa_ontology.versions.deploy.drop_staging_graphs", calls.drop),
        patch.object(dynamo_store, "delete_all_namespace_items", calls.purge),
    ):
        r = _delete()
    assert r.status_code == 200
    assert [c[0] for c in calls.mock_calls] == ["drop", "purge"]
    calls.drop.assert_called_once_with(NS)


def test_namespace_delete_survives_a_staging_cleanup_failure(versions_aws):
    vector = MagicMock()
    vector.delete_index.return_value = True
    with (
        _stores(vector=vector),
        patch("coa_ontology.versions.deploy.drop_staging_graphs", side_effect=RuntimeError("Neptune down")),
        patch.object(dynamo_store, "delete_all_namespace_items", return_value=0) as purge,
    ):
        r = _delete()
    assert r.status_code == 200
    purge.assert_called_once_with(NS)
