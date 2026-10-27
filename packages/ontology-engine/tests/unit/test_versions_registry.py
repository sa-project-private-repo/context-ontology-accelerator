# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the version registry rows (coa_ontology.versions.registry)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from coa_ontology.versions import registry

pytestmark = pytest.mark.unit

NS = "ns-1"
ONTO = "https://example.com/onto"


def _reserve(**overrides):
    kwargs = {"ontology_id": ONTO, "source": "PROPOSAL", "source_proposal_id": "p-1", "created_by": "alice"}
    kwargs.update(overrides)
    return registry.reserve_next_version(NS, **kwargs)


def _publish_next() -> int:
    version, _ = _reserve()
    registry.mark_published(NS, version, files={}, diff_summary={}, source_proposals=["p-1"])
    return version


def _key(sk: str) -> dict[str, str]:
    return {"PK": f"NAMESPACE#{NS}", "SK": sk}


def _age(aws, sk: str, seconds: int) -> None:
    """Make ``updated_at`` of one row look ``seconds`` old."""
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    aws.table.update_item(Key=_key(sk), UpdateExpression="SET updated_at = :t", ExpressionAttributeValues={":t": old})


def _raw(aws, sk: str) -> dict:
    return aws.table.get_item(Key=_key(sk))["Item"]


def _set_lock_heartbeat(aws, heartbeat_ts: int) -> None:
    aws.table.update_item(
        Key=_key("DEPLOYLOCK"),
        UpdateExpression="SET heartbeat_ts = :t",
        ExpressionAttributeValues={":t": heartbeat_ts},
    )


def _new_deployment(deployment_id: str = "d-1", version: int = 1) -> None:
    registry.create_deployment(NS, deployment_id, version=version, requested_by="bob", acknowledged_breaking_metrics=[])


class _ReadSpy:
    """Table wrapper recording the ``ConsistentRead`` flag of every read."""

    def __init__(self, table) -> None:
        self._table = table
        self.reads: list[tuple[str, object]] = []

    def __getattr__(self, name: str):
        attr = getattr(self._table, name)
        if name not in ("get_item", "query"):
            return attr

        def spy(**kwargs):
            self.reads.append((name, kwargs.get("ConsistentRead")))
            return attr(**kwargs)

        return spy


class TestVersions:
    def test_first_reservation_is_v1_without_parent(self, versions_aws):
        assert _reserve() == (1, None)
        row = registry.get_version(NS, 1)
        assert row["status"] == registry.PUBLISHING and row["ontology_id"] == ONTO

    def test_reservation_waits_while_previous_version_is_publishing(self, versions_aws):
        _reserve()
        with pytest.raises(registry.PublishInProgressError):
            _reserve()

    def test_next_version_parents_on_latest_published_and_skips_failed(self, versions_aws):
        assert _publish_next() == 1
        version, _ = _reserve()
        registry.mark_publish_failed(NS, version, "boom")
        assert _reserve() == (3, 1)

    def test_list_is_newest_first_and_head_is_latest_published(self, versions_aws):
        _publish_next()
        _publish_next()
        assert [v["version"] for v in registry.list_versions(NS)] == [2, 1]
        assert registry.head_version(NS)["version"] == 2

    def test_stale_publishing_row_is_swept_to_failed(self, versions_aws):
        _reserve()
        _age(versions_aws, "VERSION#000001", registry.PUBLISH_STALE_SECONDS + 5)
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISH_FAILED
        assert _reserve() == (2, None)

    def test_reset_active_makes_next_publish_start_from_scratch(self, versions_aws):
        _publish_next()
        registry.reset_active(NS)
        assert registry.get_active(NS)["version"] is None
        assert registry.head_version(NS) is None
        assert _reserve() == (2, None)


class TestActive:
    def test_set_and_get_active(self, versions_aws):
        registry.set_active(NS, version=3, deployment_id="d-1", deployed_by="bob")
        active = registry.get_active(NS)
        assert active["version"] == 3 and active["deployment_id"] == "d-1" and active["deployed_by"] == "bob"


class TestDeployments:
    def test_create_checkpoint_finish_get_and_latest(self, versions_aws):
        registry.create_deployment(
            NS, "20261014T000000Z-a", version=1, requested_by="bob", acknowledged_breaking_metrics=[]
        )
        registry.create_deployment(
            NS, "20261014T000100Z-b", version=2, requested_by="bob", acknowledged_breaking_metrics=["m-1"]
        )
        registry.acquire_deploy_lock(NS, "20261014T000100Z-b")
        registry.checkpoint_deployment(NS, "20261014T000100Z-b", progress={"stage": "graph"})
        assert registry.get_deployment(NS, "20261014T000100Z-b")["progress"] == {"stage": "graph"}
        registry.finish_deployment(
            NS, "20261014T000100Z-b", status=registry.DEPLOYED, partial=False, progress={"stage": "done"}, error=""
        )
        row = registry.get_deployment(NS, "20261014T000100Z-b")
        assert row["status"] == registry.DEPLOYED and row["progress"] == {"stage": "done"}
        assert row["acknowledged_breaking_metrics"] == ["m-1"]
        assert registry.latest_deployment(NS)["deployment_id"] == "20261014T000100Z-b"

    def test_list_deployments_is_newest_first_and_limited(self, versions_aws):
        assert registry.list_deployments(NS) == []
        for dep_id in ("20261014T000000Z-a", "20261014T000200Z-c", "20261014T000100Z-b"):
            _new_deployment(dep_id)
        _new_deployment("20261014T000300Z-x")
        registry.create_deployment(
            "ns-2", "20261014T000400Z-z", version=1, requested_by="bob", acknowledged_breaking_metrics=[]
        )
        ids = [row["deployment_id"] for row in registry.list_deployments(NS)]
        assert ids == ["20261014T000300Z-x", "20261014T000200Z-c", "20261014T000100Z-b", "20261014T000000Z-a"]
        limited = registry.list_deployments(NS, limit=2)
        assert [row["deployment_id"] for row in limited] == ids[:2]
        assert "PK" not in limited[0] and limited[0]["status"] == registry.DEPLOYING

    def test_serving_deployment_skips_the_failures_that_left_serving_untouched(self, versions_aws):
        """A deployment that failed before its swap changed nothing, so an earlier partial one still holds."""
        assert registry.serving_deployment(NS) is None
        for dep_id, status, partial, serving in [
            ("20261014T000000Z-a", registry.DEPLOY_FAILED, True, "20261014T000000Z-a"),
            ("20261014T000100Z-b", registry.DEPLOYED, False, "20261014T000100Z-b"),
            ("20261014T000200Z-c", registry.DEPLOY_FAILED, True, "20261014T000200Z-c"),
            ("20261014T000300Z-d", registry.DEPLOY_FAILED, False, "20261014T000200Z-c"),
        ]:
            _new_deployment(dep_id)
            if status == registry.DEPLOYED:
                registry.acquire_deploy_lock(NS, dep_id)  # finishing DEPLOYED releases it
            registry.finish_deployment(NS, dep_id, status=status, partial=partial, progress={}, error="")
            assert registry.serving_deployment(NS)["deployment_id"] == serving, dep_id
        _new_deployment("20261014T000400Z-e")  # a running deployment may already have swapped
        assert registry.serving_deployment(NS)["deployment_id"] == "20261014T000400Z-e"

    def test_serving_deployment_reads_no_further_than_the_row_it_returns(self, versions_aws):
        """It read every DEPLOY row, though they grow with every deploy and the Versions tab
        asks for it every 3 s while a deployment runs."""
        ids = [f"20261014T{n:06d}Z-x" for n in range(30)]
        for n, dep_id in enumerate(ids):
            _new_deployment(dep_id)
            # The oldest three deployed; the newer ones failed before their swap, except the newest, still running.
            if n < 3:
                registry.acquire_deploy_lock(NS, dep_id)  # finishing DEPLOYED releases it
                registry.finish_deployment(NS, dep_id, status=registry.DEPLOYED, partial=False, progress={}, error="")
            elif n < len(ids) - 1:
                registry.finish_deployment(
                    NS, dep_id, status=registry.DEPLOY_FAILED, partial=False, progress={}, error=""
                )
        table = versions_aws.table
        with patch.object(table, "query", wraps=table.query) as query:
            assert registry.serving_deployment(NS)["deployment_id"] == ids[-1]
        assert query.call_count == 1 and query.call_args.kwargs["Limit"] < len(ids)

        registry.finish_deployment(NS, ids[-1], status=registry.DEPLOY_FAILED, partial=False, progress={}, error="")
        with patch.object(registry, "_SERVING_PAGE", 2), patch.object(table, "query", wraps=table.query) as query:
            assert registry.serving_deployment(NS)["deployment_id"] == ids[2]
        assert query.call_count == 14  # 27 failures, then ids[2] in the 14th page; the page of ids[1] and ids[0] unread

    def test_new_deployment_ids_sort_by_time(self):
        assert registry.new_deployment_id()[:8] == datetime.now(UTC).strftime("%Y%m%d")


class TestDeployLock:
    def test_second_acquire_raises_with_holder(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-1")
        with pytest.raises(registry.DeployLockHeldError) as exc:
            registry.acquire_deploy_lock(NS, "d-2")
        assert exc.value.deployment_id == "d-1"
        assert registry.deploy_lock_holder(NS) == "d-1"

    def test_a_delete_holding_the_lock_is_reported_as_a_delete(self, versions_aws):
        registry.acquire_deploy_lock(NS, "delete-1")
        with pytest.raises(registry.DeployLockHeldError) as exc:
            registry.acquire_deploy_lock(NS, "d-2")
        assert exc.value.deployment_id == "delete-1"
        assert str(exc.value) == "an ontology delete is in progress in this namespace"

    def test_a_holder_that_vanished_is_reported_generically(self):
        assert str(registry.DeployLockHeldError("")) == "another deployment or delete is in progress in this namespace"

    def test_release_only_by_holder(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-1")
        registry.release_deploy_lock(NS, "d-2")
        assert registry.deploy_lock_holder(NS) == "d-1"
        registry.release_deploy_lock(NS, "d-1")
        assert registry.deploy_lock_holder(NS) is None
        registry.acquire_deploy_lock(NS, "d-3")

    def test_stale_lock_can_be_taken_over(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-1")
        versions_aws.table.update_item(
            Key={"PK": f"NAMESPACE#{NS}", "SK": "DEPLOYLOCK"},
            UpdateExpression="SET heartbeat_ts = :t",
            ExpressionAttributeValues={":t": 0},
        )
        assert registry.deploy_lock_holder(NS) is None
        registry.acquire_deploy_lock(NS, "d-2")
        assert registry.deploy_lock_holder(NS) == "d-2"

    def test_stale_deploying_row_is_failed_and_lock_released(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-1")
        registry.create_deployment(NS, "d-1", version=1, requested_by="bob", acknowledged_breaking_metrics=[])
        old = (datetime.now(UTC) - timedelta(seconds=registry.STALE_SECONDS + 5)).isoformat()
        versions_aws.table.update_item(
            Key={"PK": f"NAMESPACE#{NS}", "SK": "DEPLOY#d-1"},
            UpdateExpression="SET updated_at = :t",
            ExpressionAttributeValues={":t": old},
        )
        assert registry.get_deployment(NS, "d-1")["status"] == registry.DEPLOY_FAILED
        assert registry.deploy_lock_holder(NS) is None

    @pytest.mark.parametrize(
        "progress, partial",
        [
            ({}, False),
            ({"stage": "done"}, True),  # the swap step, and its MOVE, may have started
            ({"stage": "done", "swap": "done", "embeddings": "done"}, True),
        ],
        ids=["staging", "staged", "swapped"],
    )
    def test_a_swept_deployment_is_partial_once_staging_finished(self, versions_aws, progress, partial):
        registry.acquire_deploy_lock(NS, "d-1")
        registry.create_deployment(NS, "d-1", version=1, requested_by="bob", acknowledged_breaking_metrics=[])
        if progress:
            registry.checkpoint_deployment(NS, "d-1", progress=progress)
        _age(versions_aws, "DEPLOY#d-1", registry.STALE_SECONDS + 5)
        swept = registry.get_deployment(NS, "d-1")
        assert swept["status"] == registry.DEPLOY_FAILED and swept["partial"] is partial
        assert _raw(versions_aws, "DEPLOY#d-1")["partial"] is partial


class TestConsistentReads:
    def test_reserve_computes_number_and_parent_from_one_versions_snapshot(self, versions_aws):
        _publish_next()
        real_query = registry._query
        version_queries: list[str] = []

        def drifting_query(namespace, prefix, **kwargs):
            items = real_query(namespace, prefix, **kwargs)
            if prefix != "VERSION#":
                return items
            version_queries.append(prefix)
            if len(version_queries) == 1:
                return items
            # A second read would observe a publish that landed in between.
            newer = {**items[0], "SK": "VERSION#000002", "version": Decimal(2)}
            return [newer, *items]

        with patch.object(registry, "_query", drifting_query):
            assert _reserve() == (2, 1)
        assert len(version_queries) == 1

    def test_every_registry_read_is_strongly_consistent(self, versions_aws):
        spy = _ReadSpy(versions_aws.table)
        with patch("coa_ontology.dynamo_store._get_table", return_value=spy):
            _publish_next()
            registry.list_versions(NS)
            registry.get_version(NS, 1)
            registry.head_version(NS)
            registry.get_active(NS)
            registry.reset_active(NS)
            _new_deployment("d-1")
            registry.get_deployment(NS, "d-1")
            registry.latest_deployment(NS)
            registry.serving_deployment(NS)
            registry.list_deployments(NS)
            registry.acquire_deploy_lock(NS, "d-1")
            with pytest.raises(registry.DeployLockHeldError):
                registry.acquire_deploy_lock(NS, "d-2")
            registry.deploy_lock_holder(NS)
        assert {name for name, _ in spy.reads} == {"get_item", "query"}
        assert all(consistent is True for _, consistent in spy.reads)


class TestVersionTransitions:
    def test_mark_published_is_rejected_once_the_row_was_swept(self, versions_aws):
        _reserve()
        _age(versions_aws, "VERSION#000001", registry.PUBLISH_STALE_SECONDS + 5)
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISH_FAILED
        with pytest.raises(registry.VersionStateError):
            registry.mark_published(NS, 1, files={}, diff_summary={}, source_proposals=[])
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISH_FAILED

    def test_mark_publish_failed_does_not_overwrite_a_published_version(self, versions_aws):
        _publish_next()
        with pytest.raises(registry.VersionStateError):
            registry.mark_publish_failed(NS, 1, "late failure")
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISHED

    def test_transition_on_missing_version_does_not_create_a_row(self, versions_aws):
        with pytest.raises(registry.VersionStateError):
            registry.mark_published(NS, 7, files={}, diff_summary={}, source_proposals=[])
        assert registry.get_version(NS, 7) is None

    def test_sweep_loses_the_race_against_a_late_publish(self, versions_aws):
        _reserve()
        _age(versions_aws, "VERSION#000001", registry.PUBLISH_STALE_SECONDS + 5)
        stale_read = _raw(versions_aws, "VERSION#000001")
        registry.mark_published(NS, 1, files={}, diff_summary={}, source_proposals=[])
        swept = registry._sweep_version(NS, stale_read)
        assert swept["status"] == registry.PUBLISHING  # unswept: the write was rejected
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISHED

    def test_versions_use_the_shorter_publish_stale_threshold(self, versions_aws):
        assert registry.PUBLISH_STALE_SECONDS < registry.STALE_SECONDS
        age = registry.PUBLISH_STALE_SECONDS + 5
        _reserve()
        _age(versions_aws, "VERSION#000001", age)
        registry.acquire_deploy_lock(NS, "d-1")
        _new_deployment("d-1")
        _age(versions_aws, "DEPLOY#d-1", age)
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISH_FAILED
        assert registry.get_deployment(NS, "d-1")["status"] == registry.DEPLOYING
        assert registry.deploy_lock_holder(NS) == "d-1"


class TestDeploymentTransitions:
    def test_finish_deployment_records_the_outcome_once(self, versions_aws):
        _new_deployment("d-1")
        registry.finish_deployment(
            NS, "d-1", status=registry.DEPLOY_FAILED, partial=True, progress={"stage": "aoss"}, error="boom"
        )
        row = registry.get_deployment(NS, "d-1")
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is True
        assert row["progress"] == {"stage": "aoss"} and row["error"] == "boom"
        with pytest.raises(registry.VersionStateError):
            registry.finish_deployment(NS, "d-1", status=registry.DEPLOY_FAILED, partial=False, progress={}, error="")
        with pytest.raises(registry.DeployLockLostError):
            registry.finish_deployment(NS, "d-1", status=registry.DEPLOYED, partial=False, progress={}, error="")
        assert registry.get_deployment(NS, "d-1")["status"] == registry.DEPLOY_FAILED

    def test_successful_finish_records_the_outcome_and_releases_the_lock(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-1")
        _new_deployment("d-1")
        registry.finish_deployment(
            NS, "d-1", status=registry.DEPLOYED, partial=False, progress={"stage": "done"}, error=""
        )
        row = registry.get_deployment(NS, "d-1")
        assert row["status"] == registry.DEPLOYED and row["progress"] == {"stage": "done"}
        assert "Item" not in versions_aws.table.get_item(Key=_key("DEPLOYLOCK"), ConsistentRead=True)
        registry.acquire_deploy_lock(NS, "d-2")  # the next deployment can start right away

    def test_failed_finish_leaves_the_lock_to_its_worker(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-1")
        _new_deployment("d-1")
        registry.finish_deployment(NS, "d-1", status=registry.DEPLOY_FAILED, partial=True, progress={}, error="x")
        assert registry.deploy_lock_holder(NS) == "d-1"

    def test_finish_deployment_only_accepts_terminal_statuses(self, versions_aws):
        _new_deployment("d-1")
        with pytest.raises(ValueError):
            registry.finish_deployment(NS, "d-1", status=registry.DEPLOYING, partial=False, progress={}, error="")

    def test_finish_deployment_on_missing_row_does_not_create_it(self, versions_aws):
        with pytest.raises(registry.VersionStateError):
            registry.finish_deployment(NS, "nope", status=registry.DEPLOY_FAILED, partial=False, progress={}, error="")
        registry.acquire_deploy_lock(NS, "nope")
        with pytest.raises(registry.DeployLockLostError):
            registry.finish_deployment(NS, "nope", status=registry.DEPLOYED, partial=False, progress={}, error="")
        assert registry.get_deployment(NS, "nope") is None
        assert registry.deploy_lock_holder(NS) == "nope"  # the rejected transaction kept the lock

    def test_deployment_sweep_skips_a_row_that_heartbeated_after_it_was_read(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-1")
        _new_deployment("d-1")
        _age(versions_aws, "DEPLOY#d-1", registry.STALE_SECONDS + 5)
        stale_read = _raw(versions_aws, "DEPLOY#d-1")
        registry.checkpoint_deployment(NS, "d-1", progress={"stage": "graph"})  # worker is alive after all
        swept = registry._sweep_deployment(NS, stale_read)
        assert swept["status"] == registry.DEPLOYING
        assert registry.get_deployment(NS, "d-1")["status"] == registry.DEPLOYING
        assert registry.deploy_lock_holder(NS) == "d-1"

    def test_reading_a_failed_deployment_does_not_release_the_lock(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-1")
        _new_deployment("d-1")
        registry.finish_deployment(NS, "d-1", status=registry.DEPLOY_FAILED, partial=False, progress={}, error="x")
        assert registry.get_deployment(NS, "d-1")["status"] == registry.DEPLOY_FAILED
        assert registry.latest_deployment(NS)["deployment_id"] == "d-1"
        assert registry.deploy_lock_holder(NS) == "d-1"  # only the worker (or a stale sweep) releases it


class TestFencing:
    def test_checkpoint_updates_the_row_and_refreshes_the_lock_together(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-1")
        _new_deployment("d-1")
        stale_ts = int(datetime.now(UTC).timestamp()) - 100
        _set_lock_heartbeat(versions_aws, stale_ts)
        registry.checkpoint_deployment(NS, "d-1", progress={"stage": "graph"}, partial=True)
        row = registry.get_deployment(NS, "d-1")
        assert row["progress"] == {"stage": "graph"} and row["partial"] is True
        assert _raw(versions_aws, "DEPLOYLOCK")["heartbeat_ts"] > stale_ts + 50

    def test_checkpoint_by_a_non_holder_changes_nothing(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-2")
        _new_deployment("d-1")
        stale_ts = int(datetime.now(UTC).timestamp()) - 100
        _set_lock_heartbeat(versions_aws, stale_ts)
        with pytest.raises(registry.DeployLockLostError):
            registry.checkpoint_deployment(NS, "d-1", progress={"stage": "graph"})
        assert registry.get_deployment(NS, "d-1")["progress"] == {}
        assert _raw(versions_aws, "DEPLOYLOCK")["heartbeat_ts"] == stale_ts

    def test_checkpoint_after_the_row_finished_changes_nothing(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-1")
        _new_deployment("d-1")
        registry.finish_deployment(NS, "d-1", status=registry.DEPLOY_FAILED, partial=False, progress={}, error="x")
        stale_ts = int(datetime.now(UTC).timestamp()) - 100
        _set_lock_heartbeat(versions_aws, stale_ts)
        with pytest.raises(registry.DeployLockLostError):
            registry.checkpoint_deployment(NS, "d-1", progress={"stage": "late"})
        assert _raw(versions_aws, "DEPLOYLOCK")["heartbeat_ts"] == stale_ts

    def test_checkpoint_cannot_write_status(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-1")
        _new_deployment("d-1")
        with pytest.raises(ValueError):
            registry.checkpoint_deployment(NS, "d-1", status=registry.DEPLOYED)

    def test_taken_over_deployment_cannot_checkpoint_or_finish(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-1")
        _new_deployment("d-1")
        _set_lock_heartbeat(versions_aws, 0)  # d-1 stalls past the lock's stale threshold
        registry.acquire_deploy_lock(NS, "d-2")
        with pytest.raises(registry.DeployLockLostError):
            registry.checkpoint_deployment(NS, "d-1", progress={"stage": "aoss"})
        # Even before any sweep marks d-1 failed, it cannot report success or free d-2's lock.
        with pytest.raises(registry.DeployLockLostError):
            registry.finish_deployment(NS, "d-1", status=registry.DEPLOYED, partial=False, progress={}, error="")
        assert _raw(versions_aws, "DEPLOY#d-1")["status"] == registry.DEPLOYING
        assert registry.deploy_lock_holder(NS) == "d-2"
        # d-1's row ages out; the sweep fails it without touching d-2's lock.
        _age(versions_aws, "DEPLOY#d-1", registry.STALE_SECONDS + 5)
        assert registry.get_deployment(NS, "d-1")["status"] == registry.DEPLOY_FAILED
        assert registry.deploy_lock_holder(NS) == "d-2"
        with pytest.raises(registry.DeployLockLostError):
            registry.finish_deployment(NS, "d-1", status=registry.DEPLOYED, partial=False, progress={}, error="")
        assert registry.get_deployment(NS, "d-1")["status"] == registry.DEPLOY_FAILED

    def test_taken_over_deployment_may_still_record_its_own_failure(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-1")
        _new_deployment("d-1")
        _set_lock_heartbeat(versions_aws, 0)
        registry.acquire_deploy_lock(NS, "d-2")
        registry.finish_deployment(NS, "d-1", status=registry.DEPLOY_FAILED, partial=False, progress={}, error="lost")
        assert registry.get_deployment(NS, "d-1")["status"] == registry.DEPLOY_FAILED
        assert registry.deploy_lock_holder(NS) == "d-2"

    def test_fenced_set_active_writes_only_for_the_lock_holder(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-1")
        registry.set_active(NS, version=2, deployment_id="d-1", deployed_by="bob", fence_deployment_id="d-1")
        assert registry.get_active(NS)["version"] == 2

    def test_fenced_set_active_is_rejected_after_a_takeover(self, versions_aws):
        registry.set_active(NS, version=1, deployment_id="d-0", deployed_by="bob")
        registry.acquire_deploy_lock(NS, "d-1")
        _set_lock_heartbeat(versions_aws, 0)
        registry.acquire_deploy_lock(NS, "d-2")
        with pytest.raises(registry.DeployLockLostError):
            registry.set_active(NS, version=2, deployment_id="d-1", deployed_by="bob", fence_deployment_id="d-1")
        assert registry.get_active(NS)["version"] == 1

    def test_fenced_set_active_is_rejected_without_any_lock(self, versions_aws):
        with pytest.raises(registry.DeployLockLostError):
            registry.set_active(NS, version=2, deployment_id="d-1", deployed_by="bob", fence_deployment_id="d-1")
        assert registry.get_active(NS) is None

    def test_fenced_reset_active_writes_only_for_the_lock_holder(self, versions_aws):
        _publish_next()
        registry.set_active(NS, version=1, deployment_id="d-0", deployed_by="bob")
        registry.acquire_deploy_lock(NS, "delete-1")
        registry.reset_active(NS, fence_deployment_id="delete-1")
        assert registry.get_active(NS)["version"] is None
        assert registry.get_active(NS)["reset_after_version"] == 1

    def test_fenced_reset_active_is_rejected_for_another_lock_holder(self, versions_aws):
        _publish_next()
        registry.set_active(NS, version=1, deployment_id="d-0", deployed_by="bob")
        registry.acquire_deploy_lock(NS, "delete-1")
        with pytest.raises(registry.DeployLockLostError):
            registry.reset_active(NS, fence_deployment_id="delete-2")
        assert registry.get_active(NS)["version"] == 1

    def test_heartbeat_reports_whether_the_lock_is_still_held(self, versions_aws):
        registry.acquire_deploy_lock(NS, "d-1")
        stale_ts = int(datetime.now(UTC).timestamp()) - 100
        _set_lock_heartbeat(versions_aws, stale_ts)
        assert registry.heartbeat_deploy_lock(NS, "d-1") is True
        assert _raw(versions_aws, "DEPLOYLOCK")["heartbeat_ts"] > stale_ts + 50
        assert registry.heartbeat_deploy_lock(NS, "d-2") is False
        registry.release_deploy_lock(NS, "d-1")
        assert registry.heartbeat_deploy_lock(NS, "d-1") is False


class TestRegistryDetails:
    def test_lock_without_heartbeat_can_be_taken_over(self, versions_aws):
        versions_aws.table.put_item(Item={**_key("DEPLOYLOCK"), "deployment_id": "d-0"})
        assert registry.deploy_lock_holder(NS) is None
        registry.acquire_deploy_lock(NS, "d-1")
        assert registry.deploy_lock_holder(NS) == "d-1"

    def test_reset_active_records_reset_at_not_a_deployment_time(self, versions_aws):
        _publish_next()
        registry.reset_active(NS)
        active = registry.get_active(NS)
        assert "reset_at" in active and "deployed_at" not in active
        assert active["reset_after_version"] == 1

    def test_reset_active_records_which_ontology_was_reset(self, versions_aws):
        _publish_next()
        registry.reset_active(NS, ontology_id=ONTO)
        assert registry.get_active(NS)["reset_by_ontology_id"] == ONTO

    @pytest.mark.parametrize("fenced", [False, True])
    def test_set_active_keeps_the_reset_floor_and_ends_the_pending_reset(self, versions_aws, fenced):
        _publish_next()
        registry.reset_active(NS, ontology_id=ONTO)
        assert _reserve() == (2, None)
        registry.mark_published(NS, 2, files={}, diff_summary={}, source_proposals=[])
        if fenced:
            registry.acquire_deploy_lock(NS, "d-1")
        registry.set_active(
            NS, version=2, deployment_id="d-1", deployed_by="bob", fence_deployment_id="d-1" if fenced else None
        )
        active = registry.get_active(NS)
        assert active["version"] == 2 and active["deployment_id"] == "d-1"
        assert active["reset_after_version"] == 1 and "reset_by_ontology_id" not in active
        assert registry.head_version(NS)["version"] == 2

    def test_deployment_ids_sort_in_time_order(self, versions_aws):
        first = datetime(2026, 10, 14, 0, 0, 9, tzinfo=UTC)
        ids = []
        for offset in (0, 1, 61, 3600):
            with patch.object(registry, "_now", return_value=first + timedelta(seconds=offset)):
                ids.append(registry.new_deployment_id())
        assert ids[0].startswith("20261014T000009Z-") and ids[-1].startswith("20261014T010009Z-")
        assert ids == sorted(ids)


class TestRequireFirst:
    def test_first_reservation_is_allowed(self, versions_aws):
        assert _reserve(require_first=True) == (1, None)
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISHING

    def test_existing_head_raises_without_writing_a_row(self, versions_aws):
        _publish_next()
        with pytest.raises(registry.BootstrapSupersededError) as exc:
            _reserve(require_first=True)
        assert exc.value.head_version == 1
        assert [v["version"] for v in registry.list_versions(NS)] == [1]
        assert registry.get_version(NS, 2) is None

    def test_failed_and_reset_history_has_no_head_so_it_is_allowed(self, versions_aws):
        version, _ = _reserve()
        registry.mark_publish_failed(NS, version, "boom")
        assert _reserve(require_first=True) == (2, None)
        registry.mark_published(NS, 2, files={}, diff_summary={}, source_proposals=[])
        registry.reset_active(NS)
        assert _reserve(require_first=True) == (3, None)

    def test_a_running_publish_still_wins_over_the_head_check(self, versions_aws):
        _reserve()
        with pytest.raises(registry.PublishInProgressError):
            _reserve(require_first=True)

    def test_head_check_reads_the_versions_once(self, versions_aws):
        _publish_next()
        real_query = registry._query
        version_queries: list[str] = []

        def drifting_query(namespace, prefix, **kwargs):
            items = real_query(namespace, prefix, **kwargs)
            if prefix == "VERSION#":
                version_queries.append(prefix)
            return items

        with patch.object(registry, "_query", drifting_query), pytest.raises(registry.BootstrapSupersededError):
            _reserve(require_first=True)
        assert len(version_queries) == 1


class TestTouchVersion:
    def test_refreshes_updated_at_of_a_publishing_row(self, versions_aws):
        _reserve()
        _age(versions_aws, "VERSION#000001", registry.PUBLISH_STALE_SECONDS + 5)
        registry.touch_version(NS, 1)
        row = registry.get_version(NS, 1)  # the sweep would have failed it without the touch
        assert row["status"] == registry.PUBLISHING
        age = datetime.now(UTC) - datetime.fromisoformat(row["updated_at"])
        assert age < timedelta(seconds=30)

    @pytest.mark.parametrize("finish", ["published", "failed"])
    def test_rejects_a_row_that_is_no_longer_publishing(self, versions_aws, finish):
        _reserve()
        if finish == "published":
            registry.mark_published(NS, 1, files={}, diff_summary={}, source_proposals=[])
        else:
            registry.mark_publish_failed(NS, 1, "boom")
        before = _raw(versions_aws, "VERSION#000001")["updated_at"]
        with pytest.raises(registry.VersionStateError):
            registry.touch_version(NS, 1)
        assert _raw(versions_aws, "VERSION#000001")["updated_at"] == before

    def test_rejects_a_missing_version_without_creating_it(self, versions_aws):
        with pytest.raises(registry.VersionStateError):
            registry.touch_version(NS, 9)
        assert registry.get_version(NS, 9) is None

    def test_a_concurrent_sweep_loses_to_the_touch(self, versions_aws):
        _reserve()
        _age(versions_aws, "VERSION#000001", registry.PUBLISH_STALE_SECONDS + 5)
        stale_read = _raw(versions_aws, "VERSION#000001")
        registry.touch_version(NS, 1)
        swept = registry._sweep_version(NS, stale_read)
        assert swept["status"] == registry.PUBLISHING
        assert _raw(versions_aws, "VERSION#000001")["status"] == registry.PUBLISHING


class TestCommitBootstrapVersion:
    FILES = {"ontology.ttl": {"sha256": "abc", "bytes": 3}}
    DIFF = {"added": 2}

    def _commit(self, version=1, **overrides):
        kwargs = {"files": self.FILES, "diff_summary": self.DIFF, "source_proposals": ["p-1"]}
        kwargs.update(overrides)
        registry.commit_bootstrap_version(NS, version, **kwargs)

    def test_publishes_the_version_and_activates_it_in_one_step(self, versions_aws):
        _reserve(require_first=True)
        self._commit()
        row = registry.get_version(NS, 1)
        assert row["status"] == registry.PUBLISHED and row["files"] == self.FILES
        assert row["diff_summary"] == self.DIFF and row["source_proposals"] == ["p-1"]
        active = registry.get_active(NS)
        assert active["version"] == 1 and active["deployment_id"] == "bootstrap"
        assert active["deployed_by"] == "system" and active["deployed_at"] and active["updated_at"]
        assert registry.head_version(NS)["version"] == 1
        assert _reserve() == (2, 1)

    def test_deployed_by_can_be_overridden(self, versions_aws):
        _reserve()
        self._commit(deployed_by="alice")
        assert registry.get_active(NS)["deployed_by"] == "alice"

    def test_existing_active_row_is_never_overwritten(self, versions_aws):
        _reserve()
        registry.set_active(NS, version=5, deployment_id="d-5", deployed_by="bob")
        before = _raw(versions_aws, "ACTIVE")
        with pytest.raises(registry.ActiveExistsError):
            self._commit()
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISHING  # nothing was written
        assert _raw(versions_aws, "ACTIVE") == before

    def test_concurrent_reset_active_makes_the_commit_fail_cleanly(self, versions_aws):
        _publish_next()
        registry.reset_active(NS)  # ontology deleted: ACTIVE now holds the reset marker
        _reserve(require_first=True)
        # A second delete lands between the reservation and the commit.
        registry.reset_active(NS)
        with pytest.raises(registry.ActiveExistsError):
            self._commit(version=2)
        assert registry.get_version(NS, 2)["status"] == registry.PUBLISHING
        active = registry.get_active(NS)
        assert active["version"] is None and active["reset_after_version"] == 2

    def test_commit_on_a_fresh_namespace_races_with_reset(self, versions_aws):
        _reserve(require_first=True)
        registry.reset_active(NS)
        with pytest.raises(registry.ActiveExistsError):
            self._commit()
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISHING
        assert registry.get_active(NS)["version"] is None

    def test_row_swept_meanwhile_rejects_the_commit_and_leaves_active_unset(self, versions_aws):
        _reserve()
        _age(versions_aws, "VERSION#000001", registry.PUBLISH_STALE_SECONDS + 5)
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISH_FAILED
        with pytest.raises(registry.VersionStateError):
            self._commit()
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISH_FAILED
        assert registry.get_active(NS) is None

    def test_version_state_is_reported_before_active_when_both_conditions_fail(self, versions_aws):
        _reserve()
        registry.mark_publish_failed(NS, 1, "boom")
        registry.set_active(NS, version=5, deployment_id="d-5", deployed_by="bob")
        with pytest.raises(registry.VersionStateError):
            self._commit()

    def test_missing_version_row_is_not_created(self, versions_aws):
        with pytest.raises(registry.VersionStateError):
            self._commit(version=4)
        assert registry.get_version(NS, 4) is None
        assert registry.get_active(NS) is None
