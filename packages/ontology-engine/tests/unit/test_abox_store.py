# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import contextlib
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from coa_ontology.abox import store

pytestmark = pytest.mark.unit

NS = "11111111-2222-3333-4444-555555555555"


def _raw(aws, sk: str) -> dict:
    return aws.table.get_item(Key={"PK": f"NAMESPACE#{NS}", "SK": sk})["Item"]


def test_request_and_take_pending(versions_aws):
    assert store.acquire_lock(NS, "b1")
    assert not store.take_pending(NS, "b1")
    store.request(NS, "deploy v2")
    assert store.has_pending(NS)
    assert store.take_pending(NS, "b1")
    assert not store.take_pending(NS, "b1")
    store.request(NS, "documents")
    assert store.has_pending(NS)


def test_take_pending_is_fenced_on_the_lock(versions_aws):
    store.request(NS, "documents")
    with pytest.raises(store.LockLost) as no_lock:  # nobody holds the lock
        store.take_pending(NS, "b1")
    assert no_lock.value.build_id == "b1"
    assert store.has_pending(NS)  # the request was not swallowed

    assert store.acquire_lock(NS, "b2")
    with pytest.raises(store.LockLost):  # someone else holds it
        store.take_pending(NS, "b1")
    assert store.has_pending(NS)

    store.take_pending(NS, "b2")
    with pytest.raises(store.LockLost):  # a lost lock wins over "nothing pending"
        store.take_pending(NS, "b1")


def test_lock_is_exclusive_until_released_or_stale(versions_aws, monkeypatch):
    assert store.acquire_lock(NS, "b1")
    assert not store.acquire_lock(NS, "b2")
    assert store.lock_holder(NS) == "b1"
    assert store.heartbeat(NS, "b1") and not store.heartbeat(NS, "b2")
    store.release_lock(NS, "b2")  # not the holder: no-op
    assert store.lock_holder(NS) == "b1"
    monkeypatch.setattr(store, "STALE_SECONDS", -1)  # every lock is stale
    assert store.lock_holder(NS) is None
    assert store.acquire_lock(NS, "b2")
    monkeypatch.setattr(store, "STALE_SECONDS", 1800)
    store.release_lock(NS, "b2")
    assert store.lock_holder(NS) is None


def test_state_roundtrip(versions_aws):
    assert store.get_state(NS) is None
    store.put_state(NS, status=store.READY, conforms_to_version=3, counts={"instances": 2})
    st = store.get_state(NS)
    assert st["status"] == "READY" and st["conforms_to_version"] == 3 and st["counts"] == {"instances": 2}
    store.update_state(NS, status=store.BUILDING, build_id="b9")
    st = store.get_state(NS)
    assert (st["status"], st["build_id"], st["conforms_to_version"]) == ("BUILDING", "b9", 3)


def test_graph_iris_and_keys(monkeypatch):
    monkeypatch.delenv("NDB_GRAPH_URI_BASE", raising=False)
    monkeypatch.delenv("NDB_GRAPH_URI", raising=False)
    assert store.graph_uri(NS) == f"https://ontology-workbench.local/_abox/{NS}"
    assert store.staging_uri(NS, "b1") == f"https://ontology-workbench.local/_staging/{NS}/abox-b1"
    assert store.build_key(NS, "b1") == f"ontologies/{NS}/abox/builds/b1/abox.nt"
    assert store.extract_key(NS, "ab" * 32) == f"ontologies/{NS}/abox/extract/{'ab' * 32}.json"


def _versioned(aws) -> None:
    aws.s3.put_bucket_versioning(Bucket=aws.bucket, VersioningConfiguration={"Status": "Enabled"})


def _archive(aws, build_id: str, namespace: str = NS) -> None:
    aws.s3.put_object(Bucket=aws.bucket, Key=store.build_key(namespace, build_id), Body=b"x")


def _versions(aws, prefix: str) -> list[str]:
    """Every version and delete marker under ``prefix``, as keys."""
    page = aws.s3.list_object_versions(Bucket=aws.bucket, Prefix=prefix)
    return sorted(v["Key"] for v in [*page.get("Versions", []), *page.get("DeleteMarkers", [])])


def test_prune_builds_deletes_every_version_but_the_newest_and_the_current(versions_aws, monkeypatch):
    monkeypatch.setattr(store, "ARCHIVES_KEPT", 2)
    _versioned(versions_aws)
    for build_id in ("20260101T000000Z-a", "20260102T000000Z-b", "20260103T000000Z-c", "20260104T000000Z-d"):
        _archive(versions_aws, build_id)
    _archive(versions_aws, "20260102T000000Z-b")  # a retried write: a second version
    versions_aws.s3.delete_object(Bucket=versions_aws.bucket, Key=store.build_key(NS, "20260102T000000Z-b"))

    store.prune_builds(NS, current="20260101T000000Z-a")

    # Kept: the two newest builds and the current one, oldest as it is. Nothing of b is left:
    # neither of its versions nor its delete marker.
    assert _versions(versions_aws, f"ontologies/{NS}/abox/builds/") == sorted(
        store.build_key(NS, b) for b in ("20260101T000000Z-a", "20260103T000000Z-c", "20260104T000000Z-d")
    )


def test_prune_builds_touches_only_this_namespaces_archives(versions_aws, monkeypatch):
    monkeypatch.setattr(store, "ARCHIVES_KEPT", 1)
    _versioned(versions_aws)
    other = "99999999-2222-3333-4444-555555555555"
    for build_id in ("20260101T000000Z-a", "20260102T000000Z-b"):
        _archive(versions_aws, build_id)
        _archive(versions_aws, build_id, namespace=other)
    store.put_text(store.extract_key(NS, "ab" * 32), "{}")

    store.prune_builds(NS, current="20260102T000000Z-b")

    assert _versions(versions_aws, f"ontologies/{NS}/") == sorted(
        [store.build_key(NS, "20260102T000000Z-b"), store.extract_key(NS, "ab" * 32)]
    )
    assert len(_versions(versions_aws, f"ontologies/{other}/")) == 2


def test_prune_builds_raises_when_a_delete_fails(versions_aws, monkeypatch):
    monkeypatch.setattr(store, "ARCHIVES_KEPT", 1)
    for build_id in ("20260101T000000Z-a", "20260102T000000Z-b"):
        _archive(versions_aws, build_id)
    s3 = versions_aws.s3
    denied = {"Errors": [{"Key": store.build_key(NS, "20260101T000000Z-a"), "Code": "AccessDenied"}]}
    with (
        patch.object(s3, "delete_objects", return_value=denied),
        pytest.raises(RuntimeError, match="AccessDenied"),
    ):
        store.prune_builds(NS, current="20260102T000000Z-b")
    with pytest.raises(ValueError):
        store.graph_uri("../x")


def test_s3_text_roundtrip(versions_aws):
    key = store.extract_key(NS, "cd" * 32)
    assert store.get_text(key) is None
    store.put_text(key, '{"a": 1}')
    assert store.get_text(key) == '{"a": 1}'


def test_lock_without_heartbeat_can_be_taken_over(versions_aws):
    versions_aws.table.put_item(Item={"PK": f"NAMESPACE#{NS}", "SK": "ABOXLOCK", "build_id": "b0"})
    assert store.lock_holder(NS) is None
    assert store.acquire_lock(NS, "b1")
    assert store.lock_holder(NS) == "b1"


def test_put_state_is_fenced_on_the_lock(versions_aws):
    store.put_state(NS, status=store.READY, conforms_to_version=3)  # unfenced needs no lock
    with pytest.raises(store.LockLost) as lost:
        store.put_state(NS, fence_build_id="b1", status=store.BUILDING)
    assert lost.value.build_id == "b1"
    assert store.get_state(NS)["status"] == "READY"

    assert store.acquire_lock(NS, "b1")
    store.put_state(NS, fence_build_id="b1", status=store.BUILDING, build_id="b1")
    assert store.get_state(NS)["status"] == "BUILDING"
    with pytest.raises(store.LockLost):
        store.put_state(NS, fence_build_id="b2", status=store.FAILED)
    assert store.get_state(NS)["status"] == "BUILDING"


def test_update_state_is_fenced_on_the_lock(versions_aws):
    store.put_state(NS, status=store.BUILDING, conforms_to_version=3)
    assert store.acquire_lock(NS, "b1")
    store.update_state(NS, fence_build_id="b1", build_id="b1")
    assert store.get_state(NS)["build_id"] == "b1"
    with pytest.raises(store.LockLost):
        store.update_state(NS, fence_build_id="b2", status=store.FAILED)
    st = store.get_state(NS)
    assert (st["status"], st["conforms_to_version"]) == ("BUILDING", 3)

    store.release_lock(NS, "b1")  # the lease ended: the same build is now fenced out
    with pytest.raises(store.LockLost):
        store.update_state(NS, fence_build_id="b1", status=store.READY)


def test_stalled_when_building_without_a_live_lock(versions_aws, monkeypatch):
    assert not store.stalled(NS)  # no state, no request
    store.put_state(NS, status=store.READY)
    assert not store.stalled(NS)
    store.update_state(NS, status=store.BUILDING)
    assert store.stalled(NS)  # BUILDING and nobody holds the lock
    assert store.acquire_lock(NS, "b1")
    assert not store.stalled(NS)
    monkeypatch.setattr(store, "STALE_SECONDS", -1)  # the holder stopped heartbeating
    assert store.stalled(NS)


def test_stalled_when_a_request_is_pending_without_a_live_lock(versions_aws, monkeypatch):
    store.request(NS, "documents")  # the worker died before it took the lock or marked BUILDING
    assert store.stalled(NS)
    store.put_state(NS, status=store.READY)  # a finished earlier build does not clear the request
    assert store.stalled(NS)

    assert store.acquire_lock(NS, "b1")
    assert not store.stalled(NS)  # a live build will take the request
    monkeypatch.setattr(store, "STALE_SECONDS", -1)
    assert store.stalled(NS)  # its worker died with the request still pending
    monkeypatch.setattr(store, "STALE_SECONDS", 1800)

    assert store.take_pending(NS, "b1")
    store.release_lock(NS, "b1")
    assert not store.stalled(NS)  # request served, build finished: nothing is owed
    store.request(NS, "deploy v2")  # re-queued while the build ran, and its worker thread died
    assert store.stalled(NS)


# ── Namespace teardown ──────────────────────────────────────────────────────


def test_a_teardown_takes_the_lock_from_a_live_build_and_waits_for_it(versions_aws):
    assert store.acquire_lock(NS, "b1")
    assert store.fence_teardown(NS, 150) == 150  # writes the build sent before may still land
    holder = store.lock_holder(NS)
    assert store.is_teardown(holder) and not store.is_teardown("b1") and not store.is_teardown(None)
    assert not store.heartbeat(NS, "b1")  # the build is fenced out
    with pytest.raises(store.LockLost):
        store.update_state(NS, fence_build_id="b1", status=store.READY)
    assert not store.acquire_lock(NS, "b2")  # and no other build starts
    store.release_lock(NS, "b1")  # the build's own release leaves the teardown's lock alone
    assert store.lock_holder(NS) == holder


def test_a_teardown_with_no_live_build_waits_for_nothing(versions_aws):
    assert store.fence_teardown(NS, 150) == 0  # no lock at all
    assert store.is_teardown(store.lock_holder(NS))
    assert not store.acquire_lock(NS, "b1")


def test_a_teardown_does_not_wait_for_a_stale_build(versions_aws, monkeypatch):
    assert store.acquire_lock(NS, "b1")
    versions_aws.table.update_item(
        Key=store.ddb_key(NS, store.LOCK_SK),
        UpdateExpression="SET heartbeat_ts = :ts",
        ExpressionAttributeValues={":ts": 1},  # its worker died long ago
    )
    assert store.fence_teardown(NS, 150) == 0


def test_a_teardown_taken_again_keeps_the_first_wait_and_renews_the_lease(versions_aws):
    assert store.acquire_lock(NS, "b1")
    assert store.fence_teardown(NS, 150) == 150
    versions_aws.table.update_item(
        Key=store.ddb_key(NS, store.LOCK_SK),
        UpdateExpression="SET heartbeat_ts = heartbeat_ts - :d, settles_ts = settles_ts - :d",
        ExpressionAttributeValues={":d": 100},  # taken 100 s ago
    )
    assert 0 < store.fence_teardown(NS, 150) <= 50  # what is left for the same build, not a new wait
    assert int(_raw(versions_aws, store.LOCK_SK)["heartbeat_ts"]) >= int(time.time()) - 5


def test_a_teardown_retries_a_transaction_conflict(versions_aws, monkeypatch):
    monkeypatch.setattr(store, "_sleep", lambda s: None)
    assert store.acquire_lock(NS, "b1")
    with _flaky(versions_aws, _conflict("PutItem")) as table:
        assert store.fence_teardown(NS, 150) == 150
    assert table.calls == 2 and store.is_teardown(store.lock_holder(NS))


def test_state_floats_survive_the_roundtrip(versions_aws):
    store.put_state(NS, status=store.READY, score=0.25, nested={"a": [1.5, {"b": 2.75}], "n": 3})
    st = store.get_state(NS)
    assert st["score"] == 0.25 and st["nested"] == {"a": [1.5, {"b": 2.75}], "n": 3}
    assert type(st["nested"]["n"]) is int
    store.put_state(NS, **st)  # a restored state can be written back as is
    store.update_state(NS, **store.get_state(NS))
    again = store.get_state(NS)
    assert again["score"] == 0.25 and again["nested"] == {"a": [1.5, {"b": 2.75}], "n": 3}
    assert isinstance(_raw(versions_aws, "ABOX")["score"], Decimal)


def test_public_dynamo_helpers(versions_aws):
    assert store.namespace_pk(NS) == f"NAMESPACE#{NS}"
    assert store.ddb_key(NS, "ABOXMAP#x") == {"PK": f"NAMESPACE#{NS}", "SK": "ABOXMAP#x"}
    with pytest.raises(ValueError):
        store.namespace_pk("../x")
    with pytest.raises(ValueError):
        store.ddb_key("../x", "ABOX")
    assert store.from_dynamo({"a": Decimal("2"), "b": [Decimal("0.5")]}) == {"a": 2, "b": [0.5]}
    assert type(store.from_dynamo(Decimal("2"))) is int

    assert store.acquire_lock(NS, "b1")
    with pytest.raises(ClientError) as e:
        versions_aws.table.delete_item(
            Key=store.ddb_key(NS, "ABOXLOCK"),
            ConditionExpression="build_id = :id",
            ExpressionAttributeValues={":id": "other"},
        )
    assert store.is_conditional_failure(e.value)
    assert not store.is_conditional_failure(ClientError({"Error": {"Code": "Throttling"}}, "PutItem"))


# ── Transaction conflicts ───────────────────────────────────────────────────


def _conflict(operation="UpdateItem"):
    return ClientError({"Error": {"Code": "TransactionConflictException", "Message": "conflict"}}, operation)


def _cancelled(*codes):
    error = {"Code": "TransactionCanceledException", "Message": "cancelled"}
    reasons = [{"Code": c} for c in codes]
    return ClientError({"Error": error, "CancellationReasons": reasons}, "TransactWriteItems")


class _Flaky:
    """Table wrapper whose first writes raise ``errors`` (one per call), then behave normally."""

    def __init__(self, table, errors):
        self._table = table
        self.name = table.name
        self.errors = list(errors)
        self.calls = 0
        self.meta = SimpleNamespace(client=SimpleNamespace(transact_write_items=self._transact))

    def _call(self, name, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return getattr(self._table, name)(**kwargs)

    def put_item(self, **kwargs):
        return self._call("put_item", **kwargs)

    def update_item(self, **kwargs):
        return self._call("update_item", **kwargs)

    def delete_item(self, **kwargs):
        return self._call("delete_item", **kwargs)

    def _transact(self, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self._table.meta.client.transact_write_items(**kwargs)

    def __getattr__(self, name):
        return getattr(self._table, name)


@contextlib.contextmanager
def _flaky(aws, *errors):
    flaky = _Flaky(aws.table, errors)
    with patch("coa_ontology.dynamo_store._get_table", return_value=flaky):
        yield flaky


@pytest.fixture
def sleeps(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(store, "_sleep", waits.append)
    return waits


def test_request_retries_a_transaction_conflict(versions_aws, sleeps):
    with _flaky(versions_aws, _conflict(), _conflict()) as table:
        store.request(NS, "documents")
    assert table.calls == 3 and len(sleeps) == 2
    assert store.has_pending(NS)


def test_acquire_lock_retries_a_transaction_conflict(versions_aws, sleeps):
    with _flaky(versions_aws, _conflict("PutItem")) as table:
        assert store.acquire_lock(NS, "b1")
    assert table.calls == 2 and len(sleeps) == 1
    assert store.lock_holder(NS) == "b1"


def test_heartbeat_retries_a_transaction_conflict(versions_aws, sleeps):
    assert store.acquire_lock(NS, "b1")
    with _flaky(versions_aws, _conflict(), _conflict()) as table:
        assert store.heartbeat(NS, "b1")
    assert table.calls == 3 and len(sleeps) == 2


def test_release_lock_retries_a_transaction_conflict(versions_aws, sleeps):
    assert store.acquire_lock(NS, "b1")
    with _flaky(versions_aws, _conflict("DeleteItem")) as table:
        store.release_lock(NS, "b1")
    assert table.calls == 2 and len(sleeps) == 1
    assert store.lock_holder(NS) is None


def test_unfenced_state_writes_retry_a_transaction_conflict(versions_aws, sleeps):
    with _flaky(versions_aws, _conflict("PutItem")) as table:
        store.put_state(NS, status=store.READY)
    assert table.calls == 2
    with _flaky(versions_aws, _conflict()) as table:
        store.update_state(NS, status=store.FAILED)
    assert table.calls == 2 and len(sleeps) == 2
    assert store.get_state(NS)["status"] == "FAILED"


def test_conflict_retries_are_bounded_and_back_off(versions_aws, sleeps):
    with _flaky(versions_aws, *[_conflict()] * 5) as table, pytest.raises(ClientError) as e:
        store.request(NS, "documents")
    assert e.value.response["Error"]["Code"] == "TransactionConflictException"
    assert table.calls == store.CONFLICT_ATTEMPTS == 5
    assert len(sleeps) == 4
    assert all(0 <= wait <= 0.1 * 2**i for i, wait in enumerate(sleeps))

    sleeps.clear()
    with _flaky(versions_aws, *[_conflict()] * 4) as table:  # the fifth attempt succeeds
        store.request(NS, "documents")
    assert table.calls == 5 and len(sleeps) == 4


def test_other_errors_are_not_retried(versions_aws, sleeps):
    throttled = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
    with _flaky(versions_aws, throttled) as table, pytest.raises(ClientError):
        store.request(NS, "documents")
    assert table.calls == 1 and not sleeps

    assert store.acquire_lock(NS, "b1")
    with _flaky(versions_aws) as table:  # a failed condition is an answer, not a conflict
        assert not store.acquire_lock(NS, "b2")
    assert table.calls == 1 and not sleeps


def test_take_pending_retries_a_transaction_conflict(versions_aws, sleeps):
    assert store.acquire_lock(NS, "b1")
    store.request(NS, "documents")
    conflict = _cancelled("TransactionConflict", "None")
    with _flaky(versions_aws, conflict, conflict) as table:
        assert store.take_pending(NS, "b1")
    assert table.calls == 3 and len(sleeps) == 2
    assert not store.has_pending(NS)


def test_fenced_state_writes_retry_a_transaction_conflict(versions_aws, sleeps):
    assert store.acquire_lock(NS, "b1")
    with _flaky(versions_aws, _cancelled("None", "TransactionConflict")) as table:
        store.put_state(NS, fence_build_id="b1", status=store.BUILDING)
    assert table.calls == 2
    with _flaky(versions_aws, _cancelled("TransactionConflict", "None")) as table:
        store.update_state(NS, fence_build_id="b1", build_id="b1")
    assert table.calls == 2 and len(sleeps) == 2
    assert store.get_state(NS)["build_id"] == "b1"


def test_a_persistent_transaction_conflict_is_neither_false_nor_lock_lost(versions_aws, sleeps):
    assert store.acquire_lock(NS, "b1")
    store.request(NS, "documents")
    with _flaky(versions_aws, *[_cancelled("TransactionConflict", "None")] * 5) as table, pytest.raises(ClientError):
        store.take_pending(NS, "b1")
    assert table.calls == 5 and len(sleeps) == 4
    assert store.has_pending(NS)


def test_a_failed_condition_in_a_cancelled_transaction_is_not_retried(versions_aws, sleeps):
    assert store.acquire_lock(NS, "b1")
    store.request(NS, "documents")
    with _flaky(versions_aws, _cancelled("ConditionalCheckFailed", "None")) as table:
        assert not store.take_pending(NS, "b1")
    assert table.calls == 1
    with _flaky(versions_aws, _cancelled("None", "ConditionalCheckFailed")) as table, pytest.raises(store.LockLost):
        store.take_pending(NS, "b1")
    assert table.calls == 1
    # A real failed condition outranks a conflict on the other item.
    mixed = _cancelled("TransactionConflict", "ConditionalCheckFailed")
    with _flaky(versions_aws, mixed) as table, pytest.raises(store.LockLost):
        store.take_pending(NS, "b1")
    assert table.calls == 1 and not sleeps


def test_other_cancellations_are_not_retried(versions_aws, sleeps):
    assert store.acquire_lock(NS, "b1")
    with _flaky(versions_aws, _cancelled("ThrottlingError", "None")) as table, pytest.raises(ClientError):
        store.put_state(NS, fence_build_id="b1", status=store.BUILDING)
    assert table.calls == 1 and not sleeps


def test_state_values_are_made_storable(versions_aws):
    store.put_state(NS, status=store.READY, pair=(1, 2.5), nested={"t": (Decimal("1"), [0.5])})
    st = store.get_state(NS)
    assert st["pair"] == [1, 2.5] and st["nested"] == {"t": [1, [0.5]]}
    store.update_state(NS, triple=("a", "b"))
    assert store.get_state(NS)["triple"] == ["a", "b"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(versions_aws, bad):
    store.put_state(NS, status=store.READY)
    with pytest.raises(ValueError):
        store.put_state(NS, status=store.FAILED, score=bad)
    with pytest.raises(ValueError):
        store.update_state(NS, nested={"scores": [1.0, bad]})
    with pytest.raises(ValueError):
        store.put_state(NS, fence_build_id="b1", score=bad)  # rejected before the fence is checked
    st = store.get_state(NS)
    assert st["status"] == "READY" and "score" not in st and "nested" not in st
