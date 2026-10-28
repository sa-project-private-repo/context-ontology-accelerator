# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Accept publishes an immutable version and (optionally) starts its deployment."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from coa_ontology import dynamo_store, proposals
from coa_ontology.proposals import SchemaSqlCoverageError
from coa_ontology.versions import deploy, publish, registry, storage
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_REAL_START_DEPLOYMENT = deploy.start_deployment  # the ``client`` fixture replaces it

NS = "default"
ONTO = "https://example.com/onto"
TTL = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{ONTO}> a owl:Ontology .
<{ONTO}#Patient> a owl:Class ; rdfs:label "Patient" .
"""
R2RML = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
<{ONTO}#PatientMap> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "patient" ] ;
    rr:subjectMap [ rr:class <{ONTO}#Patient> ; rr:template "p/{{id}}" ] .
"""
BOOTSTRAP_PENDING = "the namespace's existing ontology is not captured as v1 yet; accept again"


class _InlineThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _seed(aws, proposal_id: str = "p-1", turtle: str = TTL, ontology_id: str = ONTO, **extra) -> None:
    metadata = extra.pop("metadata", {"label": "Induced", "datasource_ids": ["ds-1"]})
    aws.table.put_item(
        Item={
            "PK": f"{NS}#PROPOSAL#{proposal_id}",
            "SK": "META",
            "proposal_id": proposal_id,
            "job_id": f"job-{proposal_id}",
            "namespace": NS,
            "ontology_id": ontology_id,
            "status": "pending",
            "ontology_turtle": turtle,
            "metadata": metadata,
            **extra,
        }
    )


def _proposal(aws, proposal_id: str = "p-1") -> dict:
    return aws.table.get_item(Key={"PK": f"{NS}#PROPOSAL#{proposal_id}", "SK": "META"})["Item"]


def _set_status(aws, status: str, proposal_id: str = "p-1") -> None:
    aws.table.update_item(
        Key={"PK": f"{NS}#PROPOSAL#{proposal_id}", "SK": "META"},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status},
    )


@pytest.fixture
def client(versions_aws):
    app = FastAPI()
    app.include_router(proposals.router, prefix="/ontology/proposals")
    start = MagicMock(return_value="d-1")
    with (
        patch.object(proposals, "Thread", _InlineThread),
        patch("coa_ontology.proposals._generate_schema_sql", return_value=None),
        patch.object(deploy, "start_deployment", start),
        patch.object(proposals, "_ACCEPT_FLIP_BACKOFF_SECONDS", 0),
    ):
        yield TestClient(app), start, versions_aws


@pytest.fixture
def fake_publish():
    """Replace the publish itself; these tests are about what accept hands it and does with its outcome."""
    with patch.object(publish, "publish_version", MagicMock(return_value=1)) as fake:
        yield fake


def _request(fake_publish) -> publish.PublishRequest:
    fake_publish.assert_called_once()
    return fake_publish.call_args.args[0]


def _throttled() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}}, "UpdateItem"
    )


def _failing_update(fail_when, errors):
    """Wrap ``update_proposal`` so calls matching ``fail_when`` raise the next of ``errors`` (then pass through)."""
    real = dynamo_store.update_proposal
    pending = list(errors)
    calls = []

    def update(*args, **kwargs):
        if fail_when(kwargs):
            calls.append(kwargs)
            if pending:
                raise pending.pop(0)
        return real(*args, **kwargs)

    return update, calls


class TestAcceptPublishes:
    def test_publishes_v1_and_starts_deploy_by_default(self, client):
        http, start, aws = client
        _seed(aws)
        r = http.post("/ontology/proposals/p-1/accept", headers={"X-Coa-Principal": "alice@example.com"})
        assert r.status_code == 202, r.text
        row = _proposal(aws)
        assert row["status"] == "accepted" and int(row["published_version"]) == 1
        assert registry.get_version(NS, 1)["created_by"] == "alice@example.com"
        assert storage.read_manifest(NS, 1)["sourceProposals"] == ["p-1"]
        start.assert_called_once_with(NS, 1, requested_by="alice@example.com")
        assert row["auto_deploy_id"] == "d-1" and "auto_deploy_held" not in row

    def test_a_percent_encoded_principal_is_decoded(self, client, fake_publish):
        http, start, aws = client
        _seed(aws)
        r = http.post(
            "/ontology/proposals/p-1/accept", headers={"X-Coa-Principal": "%ED%99%8D%EA%B8%B8%EB%8F%99@example.com"}
        )
        assert r.status_code == 202, r.text
        assert _request(fake_publish).created_by == "홍길동@example.com"
        start.assert_called_once_with(NS, 1, requested_by="홍길동@example.com")

    def test_deploy_false_only_publishes(self, client):
        http, start, aws = client
        _seed(aws)
        assert http.post("/ontology/proposals/p-1/accept", json={"deploy": False}).status_code == 202
        row = _proposal(aws)
        assert row["status"] == "accepted" and int(row["published_version"]) == 1
        assert "auto_deploy_id" not in row and "auto_deploy_held" not in row
        start.assert_not_called()

    def test_breaking_metrics_hold_the_auto_deploy(self, client):
        http, start, aws = client
        metrics = [{"metricId": "m", "name": "m", "missingClassIri": "x"}]
        start.side_effect = deploy.BreakingMetricsError(metrics)
        _seed(aws)
        http.post("/ontology/proposals/p-1/accept")
        row = _proposal(aws)
        assert row["status"] == "accepted"
        assert row["auto_deploy_held"] == {"reason": "BREAKING_METRICS", "metrics": metrics}

    def test_running_deploy_holds_the_auto_deploy(self, client):
        http, start, aws = client
        start.side_effect = registry.DeployLockHeldError("d-0")
        _seed(aws)
        http.post("/ontology/proposals/p-1/accept")
        assert _proposal(aws)["auto_deploy_held"] == {"reason": "DEPLOY_IN_PROGRESS"}

    def test_an_auto_deploy_of_a_version_retired_meanwhile_is_held(self, client):
        http, start, aws = client
        _seed(aws)

        def delete_resets_then_deploy(namespace, version, **kwargs):
            registry.reset_active(namespace, ontology_id=ONTO)  # a delete stopped serving after the publish
            return _REAL_START_DEPLOYMENT(namespace, version, **kwargs)

        start.side_effect = delete_resets_then_deploy
        assert http.post("/ontology/proposals/p-1/accept").status_code == 202
        row = _proposal(aws)
        assert row["status"] == "accepted" and "auto_deploy_id" not in row
        held = row["auto_deploy_held"]
        assert held["reason"] == "DEPLOY_FAILED_TO_START"
        assert "Version v1 belongs to a deleted ontology" in held["error"]
        assert registry.deploy_lock_holder(NS) is None

    def test_a_table_missing_from_the_catalog_fails_the_accept_until_it_is_back(self, client):
        http, start, aws = client
        _seed(aws, r2rml_turtle=R2RML)
        with patch(
            "coa_ontology.proposals._generate_schema_sql",
            side_effect=SchemaSqlCoverageError("no matching physical table", missing=['"doctor"']),
        ):
            assert http.post("/ontology/proposals/p-1/accept").status_code == 202
        row = _proposal(aws)
        assert row["status"] == "accept_failed"
        assert row["accept_error"] == (
            'Mapped table(s) "doctor" are not available in the source catalog (removed, not approved, '
            "or temporarily unreachable). If the source is available, retry the accept; otherwise re-induce "
            "the datasource, or delete the domain ontology to drop its mappings."
        )
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISH_FAILED
        start.assert_not_called()

        with patch("coa_ontology.proposals._generate_schema_sql", return_value='CREATE TABLE "patient" ("id" INT);'):
            assert http.post("/ontology/proposals/p-1/accept").status_code == 202
        row = _proposal(aws)
        assert row["status"] == "accepted" and int(row["published_version"]) == 2
        assert storage.read_version_file(NS, 2, "vkg/schema.sql") == 'CREATE TABLE "patient" ("id" INT);'

    def test_invalid_turtle_lands_accept_failed_and_is_reacceptable(self, client):
        http, _, aws = client
        _seed(aws, turtle="not turtle <<<")
        http.post("/ontology/proposals/p-1/accept")
        row = _proposal(aws)
        assert row["status"] == "accept_failed" and "Invalid ontology" in row["accept_error"]
        assert row["accept_progress"] == {"step": "publish", "state": "failed"}
        aws.table.update_item(
            Key={"PK": f"{NS}#PROPOSAL#p-1", "SK": "META"},
            UpdateExpression="SET ontology_turtle = :t",
            ExpressionAttributeValues={":t": TTL},
        )
        assert http.post("/ontology/proposals/p-1/accept").status_code == 202
        assert _proposal(aws)["status"] == "accepted"

    def test_versions_of_another_ontology_reject_the_accept(self, client):
        http, _, aws = client
        _seed(aws, "p-1")
        http.post("/ontology/proposals/p-1/accept")
        _seed(aws, "p-2", ontology_id="https://other.example/onto")
        r = http.post("/ontology/proposals/p-2/accept", json={"ontology_id": "https://other.example/onto"})
        assert r.status_code == 409
        assert _proposal(aws, "p-2")["status"] == "pending"

    def test_versions_of_another_ontology_reject_an_accept_without_a_body(self, client):
        http, _, aws = client
        _seed(aws, "p-1")
        http.post("/ontology/proposals/p-1/accept")
        _seed(aws, "p-2", ontology_id="https://other.example/onto")
        r = http.post("/ontology/proposals/p-2/accept")
        assert r.status_code == 409
        assert f"publishes versions of ontology '{ONTO}'" in r.json()["detail"]
        assert _proposal(aws, "p-2")["status"] == "pending"


class TestAuthoritativeDatasources:
    def test_complete_table_induction_makes_its_datasources_authoritative(self, client, fake_publish):
        http, _, aws = client
        _seed(
            aws,
            metadata={
                "label": "Induced",
                "datasource_ids": ["ds-1"],
                "induction_strategy": "table_to_ontology",
                "dropped_tables_count": 0,
            },
        )
        http.post("/ontology/proposals/p-1/accept")
        assert _request(fake_publish).authoritative_datasources == ["ds-1"]

    @pytest.mark.parametrize(
        "extra",
        [
            {"induction_strategy": "rigor_ontology"},
            {"induction_strategy": "table_to_ontology", "dropped_tables_count": 2},
        ],
    )
    def test_other_or_partial_inductions_are_not_authoritative(self, client, fake_publish, extra):
        http, _, aws = client
        _seed(aws, metadata={"label": "Induced", "datasource_ids": ["ds-1"], **extra})
        http.post("/ontology/proposals/p-1/accept")
        req = _request(fake_publish)
        assert req.authoritative_datasources == []
        assert req.datasource_ids == ["ds-1"]


class TestPublishErrorMapping:
    @pytest.mark.parametrize(
        ("error", "prefix"),
        [
            (publish.PublishBusyError("another publish is still running"), "Publishing is busy: "),
            (publish.PublishStateError("v1 manifest missing"), "Operator action needed: v1 manifest missing"),
            (publish.BootstrapError("live graph inconsistent"), "Operator action needed: live graph inconsistent"),
            (RuntimeError("neptune down"), "RuntimeError: neptune down"),
        ],
    )
    def test_publish_failures_land_accept_failed_with_a_reason(self, client, fake_publish, error, prefix):
        http, start, aws = client
        fake_publish.side_effect = error
        _seed(aws)
        http.post("/ontology/proposals/p-1/accept")
        row = _proposal(aws)
        assert row["status"] == "accept_failed"
        assert row["accept_error"].startswith(prefix)
        assert row["accept_progress"] == {"step": "publish", "state": "failed"}
        start.assert_not_called()

    def test_busy_error_tells_the_user_to_accept_again(self, client, fake_publish):
        http, _, aws = client
        fake_publish.side_effect = publish.PublishBusyError("another publish is still running")
        _seed(aws)
        http.post("/ontology/proposals/p-1/accept")
        assert _proposal(aws)["accept_error"] == "Publishing is busy: another publish is still running"


class TestBootstrapPendingRetry:
    def test_retries_once_with_the_same_request(self, client, fake_publish):
        http, start, aws = client
        fake_publish.side_effect = [publish.BootstrapPendingError(BOOTSTRAP_PENDING), 1]
        _seed(aws)
        http.post("/ontology/proposals/p-1/accept")
        assert fake_publish.call_count == 2
        first, second = (c.args[0] for c in fake_publish.call_args_list)
        assert first is second
        assert _proposal(aws)["status"] == "accepted"
        start.assert_called_once()

    def test_a_second_bootstrap_pending_error_is_not_retried_again(self, client, fake_publish):
        http, _, aws = client
        fake_publish.side_effect = [publish.BootstrapPendingError(BOOTSTRAP_PENDING)] * 2 + [1]
        _seed(aws)
        http.post("/ontology/proposals/p-1/accept")
        assert fake_publish.call_count == 2
        row = _proposal(aws)
        assert row["status"] == "accept_failed" and row["accept_error"] == f"Publishing is busy: {BOOTSTRAP_PENDING}"

    def test_other_busy_errors_are_not_retried(self, client, fake_publish):
        http, _, aws = client
        fake_publish.side_effect = [publish.PublishBusyError(BOOTSTRAP_PENDING), 1]
        _seed(aws)
        http.post("/ontology/proposals/p-1/accept")
        assert fake_publish.call_count == 1
        assert _proposal(aws)["status"] == "accept_failed"


class TestProgressHeartbeat:
    def test_publish_progress_is_written_to_the_proposal_before_the_final_status(self, client, fake_publish):
        http, _, aws = client
        seen: dict = {}

        def _publish(req, *, on_progress=None):
            on_progress("waiting")
            row = _proposal(aws)
            seen.update(status=row["status"], progress=row["accept_progress"])
            return 1

        fake_publish.side_effect = _publish
        _seed(aws)
        http.post("/ontology/proposals/p-1/accept")
        assert seen == {"status": "accepting", "progress": {"step": "publish", "state": "waiting"}}
        row = _proposal(aws)
        assert row["status"] == "accepted"
        assert row["accept_progress"] == {"step": "publish", "state": "done"}

    def test_a_failed_heartbeat_write_does_not_fail_the_publish(self, client, fake_publish):
        http, _, aws = client

        def _publish(req, *, on_progress=None):
            with patch.object(dynamo_store, "update_proposal", side_effect=RuntimeError("throttled")):
                on_progress("publishing")  # must not raise into the publish
            return 1

        fake_publish.side_effect = _publish
        _seed(aws)
        http.post("/ontology/proposals/p-1/accept")
        assert _proposal(aws)["status"] == "accepted"


class TestFinalStatusFlip:
    def test_a_proposal_swept_during_the_publish_is_not_flipped_and_not_deployed(self, client, fake_publish):
        http, start, aws = client

        def _publish(req, *, on_progress=None):
            _set_status(aws, "accept_failed")  # the stale-sweep ran meanwhile
            return 1

        fake_publish.side_effect = _publish
        _seed(aws)
        http.post("/ontology/proposals/p-1/accept")
        row = _proposal(aws)
        assert row["status"] == "accept_failed"
        assert "published_version" not in row
        start.assert_not_called()

    def test_a_publish_failure_never_downgrades_an_accept_that_finished_meanwhile(self, client, fake_publish):
        """A re-accept after a sweep can finish first; the original worker's failure must not undo it."""
        http, _, aws = client

        def _publish(req, *, on_progress=None):
            _set_status(aws, "accepted")
            raise RuntimeError("late failure")

        fake_publish.side_effect = _publish
        _seed(aws)
        http.post("/ontology/proposals/p-1/accept")
        row = _proposal(aws)
        assert row["status"] == "accepted"
        assert "late failure" in row["accept_error"]
        assert row["accept_progress"] == {"step": "publish", "state": "failed"}

    def test_success_clears_the_previous_error_and_records_progress(self, client, fake_publish):
        http, _, aws = client
        fake_publish.side_effect = [RuntimeError("first try"), 1]
        _seed(aws)
        http.post("/ontology/proposals/p-1/accept")
        assert _proposal(aws)["status"] == "accept_failed"
        http.post("/ontology/proposals/p-1/accept")
        row = _proposal(aws)
        assert row["status"] == "accepted" and row["accept_error"] == ""
        assert row["accept_progress"] == {"step": "publish", "state": "done"}
        assert int(row["published_version"]) == 1

    def test_job_status_is_mirrored_as_ingested(self, client, fake_publish):
        http, _, aws = client
        _seed(aws)
        with patch.object(dynamo_store, "update_job_status") as mirror:
            http.post("/ontology/proposals/p-1/accept")
        mirror.assert_called_once_with("job-p-1", "ingested", namespace=NS)

    def test_a_failed_job_status_mirror_does_not_fail_the_accept(self, client, fake_publish):
        http, start, aws = client
        _seed(aws)
        with patch.object(dynamo_store, "update_job_status", side_effect=RuntimeError("throttled")):
            http.post("/ontology/proposals/p-1/accept")
        assert _proposal(aws)["status"] == "accepted"
        start.assert_called_once()

    def test_a_deploy_conflict_is_held_and_logged_as_a_warning(self, client, fake_publish, caplog):
        http, start, aws = client
        start.side_effect = deploy.DeployConflictError("ontology versions require the opensearch_neptune backend")
        _seed(aws)
        with caplog.at_level("WARNING", logger="proposals.accept"):
            http.post("/ontology/proposals/p-1/accept")
        row = _proposal(aws)
        assert row["status"] == "accepted"
        assert row["auto_deploy_held"] == {
            "reason": "DEPLOY_FAILED_TO_START",
            "error": "DeployConflictError: ontology versions require the opensearch_neptune backend",
        }
        held = [r for r in caplog.records if "auto-deploy" in r.getMessage()]
        assert [r.levelname for r in held] == ["WARNING"] and held[0].exc_info is None

    def test_an_unexpected_deploy_start_error_is_held_and_logged_with_its_traceback(self, client, fake_publish, caplog):
        http, start, aws = client
        start.side_effect = RuntimeError("neptune down")
        _seed(aws)
        with caplog.at_level("WARNING", logger="proposals.accept"):
            http.post("/ontology/proposals/p-1/accept")
        assert _proposal(aws)["auto_deploy_held"] == {
            "reason": "DEPLOY_FAILED_TO_START",
            "error": "RuntimeError: neptune down",
        }
        held = [r for r in caplog.records if "auto-deploy" in r.getMessage()]
        assert [r.levelname for r in held] == ["ERROR"] and held[0].exc_info is not None

    @pytest.mark.parametrize("deploy_error", [None, registry.DeployLockHeldError("d-0")])
    def test_a_failed_auto_deploy_record_is_logged_not_raised(self, client, fake_publish, caplog, deploy_error):
        http, start, aws = client
        start.side_effect = deploy_error
        _seed(aws)
        update, calls = _failing_update(lambda kw: "auto_deploy_id" in kw or "auto_deploy_held" in kw, [_throttled()])
        with patch.object(dynamo_store, "update_proposal", side_effect=update):
            assert http.post("/ontology/proposals/p-1/accept").status_code == 202
        assert len(calls) == 1
        assert _proposal(aws)["status"] == "accepted"
        assert any(r.levelname == "ERROR" and r.exc_info for r in caplog.records)


class TestFlipRetry:
    @pytest.mark.parametrize("error", [_throttled(), EndpointConnectionError(endpoint_url="https://dynamodb")])
    def test_a_transient_error_recording_the_version_is_retried(self, client, fake_publish, error):
        http, start, aws = client
        _seed(aws)
        update, calls = _failing_update(lambda kw: "published_version" in kw, [error])
        with patch.object(dynamo_store, "update_proposal", side_effect=update):
            http.post("/ontology/proposals/p-1/accept")
        assert len(calls) == 2
        assert _proposal(aws)["status"] == "accepted"
        start.assert_called_once()

    def test_spent_retries_leave_the_proposal_to_the_stale_sweep(self, client, fake_publish, caplog):
        http, start, aws = client
        _seed(aws)
        update, calls = _failing_update(lambda kw: "published_version" in kw, [_throttled()] * 3)
        with patch.object(dynamo_store, "update_proposal", side_effect=update):
            assert http.post("/ontology/proposals/p-1/accept").status_code == 202
        assert len(calls) == 3
        row = _proposal(aws)
        assert row["status"] == "accepting" and "published_version" not in row
        start.assert_not_called()
        assert any(r.levelname == "ERROR" and r.exc_info for r in caplog.records)

    def test_a_flip_that_landed_but_lost_its_response_counts_as_done(self, client, fake_publish):
        """Attempt 1 writes and then errors; attempt 2 then fails its condition on our own write."""
        http, start, aws = client
        _seed(aws)
        real = dynamo_store.update_proposal
        calls = []

        def update(*args, **kwargs):
            if "published_version" not in kwargs:
                return real(*args, **kwargs)
            calls.append(kwargs)
            if len(calls) == 1:
                real(*args, **kwargs)
                raise _throttled()
            return real(*args, **kwargs)

        with patch.object(dynamo_store, "update_proposal", side_effect=update):
            http.post("/ontology/proposals/p-1/accept")
        assert len(calls) == 2
        row = _proposal(aws)
        assert row["status"] == "accepted" and int(row["published_version"]) == 1
        start.assert_called_once_with(NS, 1, requested_by="unknown")
        assert row["auto_deploy_id"] == "d-1"

    def test_a_retry_that_finds_the_proposal_swept_still_stops(self, client, fake_publish):
        """Attempt 1 fails without writing; the sweep runs; attempt 2's condition failure is not our write."""
        http, start, aws = client
        _seed(aws)
        real = dynamo_store.update_proposal
        calls = []

        def update(*args, **kwargs):
            if "published_version" not in kwargs:
                return real(*args, **kwargs)
            calls.append(kwargs)
            if len(calls) == 1:
                _set_status(aws, "accept_failed")
                raise _throttled()
            return real(*args, **kwargs)

        with patch.object(dynamo_store, "update_proposal", side_effect=update):
            http.post("/ontology/proposals/p-1/accept")
        assert len(calls) == 2
        row = _proposal(aws)
        assert row["status"] == "accept_failed" and "published_version" not in row
        start.assert_not_called()


class TestRoute:
    def test_returns_202_accepting(self, client, fake_publish):
        http, _, aws = client
        _seed(aws)
        r = http.post("/ontology/proposals/p-1/accept")
        assert r.status_code == 202
        assert r.json() == {"proposal_id": "p-1", "status": "accepting"}

    def test_already_accepted_is_idempotent_and_publishes_nothing(self, client, fake_publish):
        http, start, aws = client
        _seed(aws, status="accepted")
        r = http.post("/ontology/proposals/p-1/accept")
        assert r.status_code == 202
        assert r.json() == {"proposal_id": "p-1", "status": "accepted"}
        fake_publish.assert_not_called()
        start.assert_not_called()

    @pytest.mark.parametrize("status", ["accepting", "embeddings_sync"])
    def test_an_accept_in_progress_is_a_conflict(self, client, fake_publish, status):
        http, _, aws = client
        _seed(aws, status=status)
        r = http.post("/ontology/proposals/p-1/accept")
        assert r.status_code == 409
        assert _proposal(aws)["status"] == status
        fake_publish.assert_not_called()

    def test_an_ontology_being_deleted_is_a_conflict(self, client, fake_publish):
        http, start, aws = client
        _seed(aws)
        dynamo_store.put_ontology_registry(NS, ONTO, {"uri": ONTO, "ontology_type": "induced", "status": "deleting"})
        r = http.post("/ontology/proposals/p-1/accept")
        assert r.status_code == 409
        assert r.json()["detail"] == f"Ontology '{ONTO}' is being deleted; retry after the delete finishes."
        assert _proposal(aws)["status"] == "pending"
        fake_publish.assert_not_called()
        start.assert_not_called()

    def test_a_concurrent_status_change_is_a_conflict(self, client, fake_publish):
        http, _, aws = client
        _seed(aws)
        read = dynamo_store.get_proposal_by_id

        def _read_then_race(*args, **kwargs):
            item = read(*args, **kwargs)
            _set_status(aws, "updated")  # another writer lands between the read and the flip
            return item

        with patch.object(dynamo_store, "get_proposal_by_id", side_effect=_read_then_race):
            r = http.post("/ontology/proposals/p-1/accept")
        assert r.status_code == 409
        assert "concurrently" in r.json()["detail"].lower()
        assert _proposal(aws)["status"] == "updated"
        fake_publish.assert_not_called()

    def test_hands_the_proposal_to_publish(self, client, fake_publish):
        http, _, aws = client
        constraints = {"rules": []}
        _seed(
            aws,
            r2rml_turtle="R2RML-ON-ITEM",
            metadata={"label": "Induced", "datasource_ids": ["ds-1", "ds-2"], "constraint_config": constraints},
        )
        http.post("/ontology/proposals/p-1/accept")
        req = _request(fake_publish)
        assert req.namespace == NS
        assert req.ontology_id == ONTO and req.uri_prefix == ONTO
        assert req.ontology_turtle == TTL and req.r2rml_turtle == "R2RML-ON-ITEM"
        assert req.source_proposal_id == "p-1"
        assert req.created_by == "unknown"
        assert req.datasource_ids == ["ds-1", "ds-2"]
        assert req.constraint_config == constraints

    def test_reviewed_turtle_and_r2rml_take_precedence(self, client, fake_publish):
        http, _, aws = client
        _seed(aws, r2rml_turtle="R2RML-ON-ITEM")
        reviewed = {"ontology_turtle": "REVIEWED-TTL", "r2rml_turtle": "REVIEWED-R2RML"}
        with patch.object(dynamo_store, "get_reviewed_proposal", return_value=reviewed):
            http.post("/ontology/proposals/p-1/accept")
        req = _request(fake_publish)
        assert (req.ontology_turtle, req.r2rml_turtle) == ("REVIEWED-TTL", "REVIEWED-R2RML")

    def test_explicit_ontology_id_is_the_publish_target(self, client, fake_publish):
        http, _, aws = client
        _seed(aws)
        target = "https://target.example/primary"
        http.post("/ontology/proposals/p-1/accept", json={"ontology_id": target})
        req = _request(fake_publish)
        assert req.ontology_id == target
        assert req.uri_prefix == ONTO

    @pytest.mark.parametrize(("principal", "created_by"), [(None, "unknown"), ("bob@example.com", "bob@example.com")])
    def test_can_be_called_in_process(self, client, fake_publish, principal, created_by):
        _, _, aws = client
        _seed(aws)
        kwargs = {"x_coa_principal": principal} if principal else {}
        resp = proposals.accept_proposal("p-1", namespace=NS, **kwargs)
        assert (resp.proposal_id, resp.status) == ("p-1", "accepting")
        assert _request(fake_publish).created_by == created_by
        assert _proposal(aws)["status"] == "accepted"

    def test_a_request_that_cannot_be_built_leaves_the_proposal_untouched(self, client, fake_publish):
        http, _, aws = client
        _seed(
            aws,
            metadata={
                "datasource_ids": ["ds-1"],
                "induction_strategy": "table_to_ontology",
                "dropped_tables_count": "?",
            },
        )
        with pytest.raises(ValueError):
            http.post("/ontology/proposals/p-1/accept")
        assert _proposal(aws)["status"] == "pending"
        fake_publish.assert_not_called()
