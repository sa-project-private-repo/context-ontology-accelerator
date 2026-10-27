# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""DynamoDB state rows, S3 artifacts and graph IRIs of the domain ABox (spec §6.2).

Rows live under ``PK=NAMESPACE#{ns}`` so namespace deletion's prefix purge removes them:

- ``ABOXREQ``: ``pending`` flag + ``last_reason``: requests are coalesced into one flag. None
  is lost provided the worker re-checks ``has_pending`` after it releases the lock.
- ``ABOXLOCK``: heartbeated lease of the one running build (same rule as ``DEPLOYLOCK``). A
  namespace teardown takes it for good (``fence_teardown``), so no build runs while it deletes.
- ``ABOX``: state of the active ABox (status, version, counts, errors).

All reads are strongly consistent: each feeds a write decision. A running build writes
``ABOXREQ``, ``ABOX`` and its mapping rows fenced on its ``ABOXLOCK`` (``take_pending`` always,
``put_state``, ``update_state`` and ``fenced_write`` with the build's id), so a stalled worker
that lost the lock cannot overwrite the build that took over (``LockLost``). Unfenced writes are
for callers that hold a different lock, such as ``clear_abox`` under ``DEPLOYLOCK``.
"""

from __future__ import annotations

import math
import os
import random
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError
from coa_common import validate_namespace_name

from coa_ontology import dynamo_store
from coa_ontology.stores.neptune_db_graph import _resolve_graph_base
from coa_ontology.versions.storage import PREFIX

REQ_SK = "ABOXREQ"
LOCK_SK = "ABOXLOCK"
STATE_SK = "ABOX"
EMPTY, BUILDING, READY, FAILED = "EMPTY", "BUILDING", "READY", "FAILED"
# A lock with no heartbeat for this long belongs to a dead worker and may be taken over. A live
# build heartbeats between stages, which can be long (Bedrock calls, graph load), so the
# threshold is as generous as DEPLOYLOCK's.
STALE_SECONDS = int(os.getenv("ABOX_STALE_SECONDS", "1800"))
# DynamoDB rejects a write to an item that a transaction currently holds (the item is in the
# fenced writes below) with TransactionConflict, and botocore does not retry it. The conflict
# is brief, so retry a few times with jittered exponential backoff before giving up.
CONFLICT_ATTEMPTS = 5
CONFLICT_BACKOFF_SECONDS = 0.1
_sleep = time.sleep  # tests patch this to skip the wait


class LockLost(Exception):
    """The build no longer holds ``ABOXLOCK`` (it went stale and was taken over)."""

    def __init__(self, build_id: str) -> None:
        """Record which build was fenced out."""
        super().__init__(f"build {build_id} no longer holds the ABOX lock")
        self.build_id = build_id


class _TransactionRejected(Exception):
    """A transaction was cancelled by failed conditions; ``failed[i]`` flags operation ``i``."""

    def __init__(self, failed: list[bool]) -> None:
        super().__init__("transaction condition failed")
        self.failed = failed


def _now() -> datetime:
    return datetime.now(UTC)


def namespace_pk(namespace: str) -> str:
    """The partition key of the namespace's rows; raises ``ValueError`` on an invalid namespace."""
    validate_namespace_name(namespace)
    return dynamo_store._namespace_pk(namespace)


def ddb_key(namespace: str, sk: str) -> dict[str, str]:
    """The primary key of one of the namespace's rows."""
    return {"PK": namespace_pk(namespace), "SK": sk}


def is_conditional_failure(e: ClientError) -> bool:
    """Whether ``e`` is a failed condition expression."""
    return e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def from_dynamo(value: Any) -> Any:
    """``value`` with every ``Decimal`` turned into an ``int`` (whole numbers) or a ``float``."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value


def _to_dynamo(value: Any) -> Any:
    """``value`` as DynamoDB stores it: ``float`` becomes ``Decimal`` and ``tuple`` becomes ``list``.

    Raises ``ValueError`` for NaN and infinity, which DynamoDB cannot hold.
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Cannot store the non-finite number {value!r}")
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    return value


def _backoff(retry: int) -> None:
    """Wait before retry number ``retry`` (1-based): full jitter over an exponentially growing window."""
    _sleep(random.uniform(0, CONFLICT_BACKOFF_SECONDS * 2 ** (retry - 1)))  # noqa: S311


def _retry_conflicts[T](call: Callable[[], T]) -> T:
    """Run ``call``, retrying a ``TransactionConflictException`` up to ``CONFLICT_ATTEMPTS`` attempts in all."""
    attempt = 1
    while True:
        try:
            return call()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "TransactionConflictException" or (
                attempt >= CONFLICT_ATTEMPTS
            ):
                raise
            _backoff(attempt)
            attempt += 1


def _get(namespace: str, sk: str) -> dict[str, Any] | None:
    item = dynamo_store._get_table().get_item(Key=ddb_key(namespace, sk), ConsistentRead=True).get("Item")
    return from_dynamo(item) if item else None


def _transact(operations: list[tuple[str, dict[str, Any]]]) -> None:
    """Run ``operations`` (``(kind, args)`` pairs) as one all-or-nothing transaction.

    Raises :class:`_TransactionRejected` when a condition failed. A cancellation because another
    transaction holds one of the items (``TransactionConflict``) is retried like a plain write's
    conflict; if it persists, or for any other cancellation (throttling), the ``ClientError``
    propagates.
    """
    table = dynamo_store._get_table()
    items = [{kind: {"TableName": table.name, **args}} for kind, args in operations]
    attempt = 1
    while True:
        try:
            table.meta.client.transact_write_items(TransactItems=items)
            return
        except ClientError as e:
            codes = [r.get("Code") for r in e.response.get("CancellationReasons") or []]
            cancelled = e.response.get("Error", {}).get("Code") == "TransactionCanceledException"
            if cancelled and "ConditionalCheckFailed" in codes:
                raise _TransactionRejected([c == "ConditionalCheckFailed" for c in codes]) from e
            conflict = (cancelled and "TransactionConflict" in codes) or (
                e.response.get("Error", {}).get("Code") == "TransactionConflictException"
            )
            if not conflict or attempt >= CONFLICT_ATTEMPTS:
                raise
            _backoff(attempt)
            attempt += 1


def _lock_holder_args(namespace: str, build_id: str) -> dict[str, Any]:
    """Key plus the condition "``build_id`` holds the ABOXLOCK" (for delete / fence / condition check)."""
    return {
        "Key": ddb_key(namespace, LOCK_SK),
        "ConditionExpression": "build_id = :id",
        "ExpressionAttributeValues": {":id": build_id},
    }


def _fenced(namespace: str, build_id: str, operations: list[tuple[str, dict[str, Any]]]) -> None:
    """:func:`_transact` ``operations`` together with a check that ``build_id`` still holds the lock.

    Raises :class:`LockLost` when the check (or any other condition) fails.
    """
    try:
        _transact([*operations, ("ConditionCheck", _lock_holder_args(namespace, build_id))])
    except _TransactionRejected as e:
        raise LockLost(build_id) from e


def fenced_write(namespace: str, build_id: str, operation: tuple[str, dict[str, Any]]) -> bool:
    """Run one conditional write (a ``(kind, args)`` pair) only while ``build_id`` holds the lock.

    Returns False when the write's own condition failed. Raises :class:`LockLost` when the lock
    check failed, whatever the write's own condition did.
    """
    try:
        _transact([operation, ("ConditionCheck", _lock_holder_args(namespace, build_id))])
    except _TransactionRejected as e:
        if len(e.failed) > 1 and e.failed[1]:
            raise LockLost(build_id) from e
        return False
    return True


def new_build_id() -> str:
    """Return a sortable, unique build id."""
    return f"{_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


# ── Requests ────────────────────────────────────────────────────────────────


def request(namespace: str, reason: str) -> None:
    """Record that a build is wanted (coalesced into one ``pending`` flag)."""
    table = dynamo_store._get_table()
    _retry_conflicts(
        lambda: table.update_item(
            Key=ddb_key(namespace, REQ_SK),
            UpdateExpression="SET pending = :t, requested_at = :now, last_reason = :r",
            ExpressionAttributeValues={":t": True, ":now": _now().isoformat(), ":r": reason[:200]},
        )
    )


def take_pending(namespace: str, build_id: str) -> bool:
    """Clear ``pending`` if set; True when this call cleared it.

    Fenced on ``build_id`` holding the lock: raises :class:`LockLost` if it does not, so a
    worker that went stale cannot swallow a request meant for the build that took over.
    """
    try:
        _transact(
            [
                (
                    "Update",
                    {
                        "Key": ddb_key(namespace, REQ_SK),
                        "UpdateExpression": "SET pending = :f",
                        "ConditionExpression": "pending = :t",
                        "ExpressionAttributeValues": {":t": True, ":f": False},
                    },
                ),
                ("ConditionCheck", _lock_holder_args(namespace, build_id)),
            ]
        )
    except _TransactionRejected as e:
        if len(e.failed) > 1 and e.failed[1]:
            raise LockLost(build_id) from e
        return False
    return True


def has_pending(namespace: str) -> bool:
    """True while a build request is waiting."""
    return bool((_get(namespace, REQ_SK) or {}).get("pending"))


# ── Lock ────────────────────────────────────────────────────────────────────


def acquire_lock(namespace: str, build_id: str) -> bool:
    """Take ``ABOXLOCK``; False when a live build holds it.

    A lock row is takeable when it has no heartbeat or its heartbeat is older than ``STALE_SECONDS``.
    """
    now = _now()
    ts = int(now.timestamp())
    table = dynamo_store._get_table()
    try:
        _retry_conflicts(
            lambda: table.put_item(
                Item={
                    **ddb_key(namespace, LOCK_SK),
                    "build_id": build_id,
                    "heartbeat_ts": ts,
                    "updated_at": now.isoformat(),
                },
                ConditionExpression=(
                    "attribute_not_exists(PK) OR attribute_not_exists(heartbeat_ts) OR heartbeat_ts < :stale"
                ),
                ExpressionAttributeValues={":stale": ts - STALE_SECONDS},
            )
        )
    except ClientError as e:
        if is_conditional_failure(e):
            return False
        raise
    return True


def heartbeat(namespace: str, build_id: str) -> bool:
    """Refresh the lease; False when ``build_id`` no longer holds it."""
    now = _now()
    table = dynamo_store._get_table()
    try:
        _retry_conflicts(
            lambda: table.update_item(
                Key=ddb_key(namespace, LOCK_SK),
                UpdateExpression="SET heartbeat_ts = :ts, updated_at = :now",
                ConditionExpression="build_id = :id",
                ExpressionAttributeValues={":ts": int(now.timestamp()), ":now": now.isoformat(), ":id": build_id},
            )
        )
    except ClientError as e:
        if is_conditional_failure(e):
            return False
        raise
    return True


def release_lock(namespace: str, build_id: str) -> None:
    """Drop the lease if ``build_id`` holds it; a no-op otherwise."""
    table = dynamo_store._get_table()
    try:
        _retry_conflicts(lambda: table.delete_item(**_lock_holder_args(namespace, build_id)))
    except ClientError as e:
        if not is_conditional_failure(e):
            raise


def lock_holder(namespace: str) -> str | None:
    """The live holder's build id; a stale lock has no holder."""
    item = _get(namespace, LOCK_SK)
    if not item or int(item.get("heartbeat_ts", 0)) < int(_now().timestamp()) - STALE_SECONDS:
        return None
    return str(item["build_id"])


# The holder ``fence_teardown`` puts on the lock; no build id has it.
TEARDOWN_PREFIX = "teardown-"


def is_teardown(holder: str | None) -> bool:
    """Whether ``holder`` is a namespace teardown's (``fence_teardown``) rather than a build's."""
    return bool(holder and holder.startswith(TEARDOWN_PREFIX))


def fence_teardown(namespace: str, settle_seconds: float) -> int:
    """Take ``ABOXLOCK`` for a namespace teardown, whoever holds it; return the seconds to wait.

    While the teardown's lease is live nothing acquires the lock, so no build starts, and a build
    that held it fails its next fenced write or lock check (``LockLost``). Writes that build sent
    before may still land for ``settle_seconds``: that is the wait returned when a live build held
    the lock. Taking the lock again renews the lease and keeps what is left of the first wait.
    The namespace purge deletes the lock row with the rest.
    """
    table = dynamo_store._get_table()
    while True:
        old = _get(namespace, LOCK_SK)
        now = int(_now().timestamp())
        settles_ts = now
        if old and is_teardown(str(old["build_id"])):
            settles_ts = max(now, int(old.get("settles_ts", now)))
        elif old and int(old.get("heartbeat_ts", 0)) >= now - STALE_SECONDS:
            settles_ts = now + math.ceil(settle_seconds)
        if old:
            condition = {
                "ConditionExpression": "build_id = :old",
                "ExpressionAttributeValues": {":old": old["build_id"]},
            }
        else:
            condition = {"ConditionExpression": "attribute_not_exists(PK)"}
        item = {
            **ddb_key(namespace, LOCK_SK),
            "build_id": f"{TEARDOWN_PREFIX}{uuid.uuid4().hex[:8]}",
            "heartbeat_ts": now,
            "settles_ts": settles_ts,
            "updated_at": _now().isoformat(),
        }
        try:
            _retry_conflicts(lambda: table.put_item(Item=item, **condition))  # noqa: B023 (called at once)
        except ClientError as e:
            if not is_conditional_failure(e):
                raise
            continue  # the lock changed hands since it was read
        return settles_ts - now


# ── State ───────────────────────────────────────────────────────────────────


def get_state(namespace: str) -> dict[str, Any] | None:
    """The ``ABOX`` state row without its keys, or None when no build has run."""
    item = _get(namespace, STATE_SK)
    if item is None:
        return None
    return {k: v for k, v in item.items() if k not in ("PK", "SK")}


def stalled(namespace: str) -> bool:
    """True when a build is owed but no live build holds the lock, so a worker must be re-armed.

    Two cases, both with no live lock:

    - the state says ``BUILDING``: the worker died mid-build;
    - ``ABOXREQ.pending`` is set: the worker died before it marked ``BUILDING``, or a request
      was re-queued and its worker thread died.

    A worker acquires the lock before it marks ``BUILDING`` and takes ``pending`` only while it
    holds the lock. The answer is also momentarily true between ``request`` and the worker taking
    the lock; re-arming then is harmless because ``acquire_lock`` is exclusive.
    """
    if lock_holder(namespace) is not None:
        return False
    state = get_state(namespace)
    return (state is not None and state.get("status") == BUILDING) or has_pending(namespace)


def put_state(namespace: str, *, fence_build_id: str | None = None, **fields: Any) -> None:
    """Replace the whole ``ABOX`` state row.

    With ``fence_build_id`` the write only lands while that build holds the lock, otherwise
    :class:`LockLost` is raised.
    """
    args = {"Item": _to_dynamo({**ddb_key(namespace, STATE_SK), **fields, "updated_at": _now().isoformat()})}
    if fence_build_id is None:
        table = dynamo_store._get_table()
        _retry_conflicts(lambda: table.put_item(**args))
        return
    _fenced(namespace, fence_build_id, [("Put", args)])


def update_state(namespace: str, *, fence_build_id: str | None = None, **fields: Any) -> None:
    """Set the given fields on the ``ABOX`` state row, keeping the others.

    With ``fence_build_id`` the write only lands while that build holds the lock, otherwise
    :class:`LockLost` is raised.
    """
    fields = _to_dynamo({**fields, "updated_at": _now().isoformat()})
    args = {
        "Key": ddb_key(namespace, STATE_SK),
        "UpdateExpression": "SET " + ", ".join(f"#n{i} = :v{i}" for i in range(len(fields))),
        "ExpressionAttributeNames": {f"#n{i}": k for i, k in enumerate(fields)},
        "ExpressionAttributeValues": {f":v{i}": v for i, v in enumerate(fields.values())},
    }
    if fence_build_id is None:
        table = dynamo_store._get_table()
        _retry_conflicts(lambda: table.update_item(**args))
        return
    _fenced(namespace, fence_build_id, [("Update", args)])


# ── Graphs and S3 ───────────────────────────────────────────────────────────


def graph_uri(namespace: str) -> str:
    """The named graph that holds the active ABox."""
    # Outside {base}/{ns}/: no existing STRSTARTS consumer sees instance data by accident.
    validate_namespace_name(namespace)
    return f"{_resolve_graph_base()}/_abox/{namespace}"


def staging_uri(namespace: str, build_id: str) -> str:
    """The staging graph one build loads into before the swap."""
    validate_namespace_name(namespace)
    return f"{_resolve_graph_base()}/_staging/{namespace}/abox-{build_id}"


def _builds_prefix(namespace: str) -> str:
    validate_namespace_name(namespace)
    return f"{PREFIX}/{namespace}/abox/builds/"


def build_key(namespace: str, build_id: str) -> str:
    """The S3 key of one build's canonical N-Triples."""
    return f"{_builds_prefix(namespace)}{build_id}/abox.nt"


# Build archives kept per namespace by prune_builds, besides the live ABox's.
ARCHIVES_KEPT = 5


def prune_builds(namespace: str, current: str) -> None:
    """Delete the build archives except the ``ARCHIVES_KEPT`` newest and ``current``'s (the live ABox's).

    Build ids sort in the order they were made. The bucket is versioned, so every version and
    delete marker is deleted by its id: a plain delete only adds a delete marker and keeps the
    bytes. Raises ``RuntimeError`` when S3 refuses to delete a version.
    """
    prefix = _builds_prefix(namespace)
    s3 = dynamo_store._get_s3()
    by_build: dict[str, list[dict[str, str]]] = {}
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=dynamo_store._S3_BUCKET, Prefix=prefix):
        for v in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]:
            build_id = v["Key"][len(prefix) :].split("/", 1)[0]
            by_build.setdefault(build_id, []).append({"Key": v["Key"], "VersionId": v["VersionId"]})
    kept = {current, *sorted(by_build, reverse=True)[:ARCHIVES_KEPT]}
    doomed = [v for build_id, versions in by_build.items() if build_id not in kept for v in versions]
    for i in range(0, len(doomed), 1000):  # delete_objects takes at most 1000 keys
        batch = {"Objects": doomed[i : i + 1000], "Quiet": True}
        errors = s3.delete_objects(Bucket=dynamo_store._S3_BUCKET, Delete=batch).get("Errors", [])
        if errors:
            raise RuntimeError(f"could not delete {len(errors)} archive version(s): {errors[0].get('Code')}")


def extract_key(namespace: str, digest: str) -> str:
    """The S3 key of one cached re-extraction result."""
    validate_namespace_name(namespace)
    return f"{PREFIX}/{namespace}/abox/extract/{digest}.json"


def put_text(key: str, body: str, content_type: str = "application/json") -> None:
    """Write a UTF-8 object to the ontology bucket."""
    dynamo_store._get_s3().put_object(
        Bucket=dynamo_store._S3_BUCKET, Key=key, Body=body.encode("utf-8"), ContentType=content_type
    )


def get_text(key: str) -> str | None:
    """Read a UTF-8 object from the ontology bucket; None when it does not exist."""
    try:
        obj = dynamo_store._get_s3().get_object(Bucket=dynamo_store._S3_BUCKET, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    return obj["Body"].read().decode("utf-8")
