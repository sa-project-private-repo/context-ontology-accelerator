# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""DynamoDB rows for ontology versions, the ACTIVE pointer, deployments and the deploy lock.

Every row lives under ``PK=NAMESPACE#{ns}``, so namespace deletion's partition
purge (:func:`dynamo_store.delete_all_namespace_items`) removes them with the
ontology registry rows::

    SK=VERSION#{n:06d}        one published version (PUBLISHING -> PUBLISHED | PUBLISH_FAILED)
    SK=ACTIVE                 the version Neptune currently serves
    SK=DEPLOY#{deploymentId}  one deploy or rollback attempt (DEPLOYING -> DEPLOYED | DEPLOY_FAILED)
    SK=DEPLOYLOCK             at most one running deployment per namespace

Liveness: a PUBLISHING / DEPLOYING row's ``updated_at`` is its heartbeat, and the
lock's heartbeat is ``heartbeat_ts``. A row or lock whose heartbeat is older than
the stale threshold belongs to a dead worker and is swept / taken over.

Consistency: every read here is strongly consistent because each one feeds a write
decision. Status transitions are conditional writes, so a worker that lost a race
(stale sweep, lock takeover) gets :class:`VersionStateError` /
:class:`DeployLockLostError` instead of silently overwriting the winner's state.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from coa_ontology import dynamo_store

PUBLISHING = "PUBLISHING"
PUBLISHED = "PUBLISHED"
PUBLISH_FAILED = "PUBLISH_FAILED"
DEPLOYING = "DEPLOYING"
DEPLOYED = "DEPLOYED"
DEPLOY_FAILED = "DEPLOY_FAILED"

# A PUBLISHING row with no update for this long belongs to a dead worker (ECS task
# recycled mid-publish): a live publish refreshes it (touch_version) several times
# within the threshold.
PUBLISH_STALE_SECONDS = int(os.getenv("ONTOLOGY_PUBLISH_STALE_SECONDS", "900"))
# A DEPLOYING row or a deploy lock with no heartbeat for this long belongs to a dead
# worker and is swept / taken over. Deployments checkpoint between stages, which
# can be long (graph load, embeddings), so the threshold is more generous.
STALE_SECONDS = int(os.getenv("ONTOLOGY_VERSION_STALE_SECONDS", "1800"))

_VERSION_PREFIX = "VERSION#"
_DEPLOY_PREFIX = "DEPLOY#"
# The DEPLOY rows serving_deployment reads per page. A deployment that fails before its swap is
# rare, so the first page nearly always holds the answer.
_SERVING_PAGE = 10
_ACTIVE_SK = "ACTIVE"
_LOCK_SK = "DEPLOYLOCK"
# Holder ids of the deploy lock a single-ontology delete takes (``DELETE /ontologies?ontology_id=``).
DELETE_LOCK_PREFIX = "delete-"


class PublishInProgressError(Exception):
    """Another publish holds the next version number in this namespace."""


class DeployLockHeldError(Exception):
    """Another deployment, or an ontology delete, holds this namespace's deploy lock."""

    def __init__(self, deployment_id: str) -> None:
        """Record which deployment (or delete) holds the lock."""
        if not deployment_id:  # the holder released the lock before it could be read
            super().__init__("another deployment or delete is in progress in this namespace")
        elif deployment_id.startswith(DELETE_LOCK_PREFIX):
            super().__init__("an ontology delete is in progress in this namespace")
        else:
            super().__init__(f"deployment {deployment_id} is in progress")
        self.deployment_id = deployment_id


class BootstrapSupersededError(Exception):
    """A version is already published, so a first-version (bootstrap) reservation no longer applies."""

    def __init__(self, head_version: int) -> None:
        """Record the published version that superseded the bootstrap."""
        super().__init__(f"version {head_version} is already published")
        self.head_version = head_version


class ActiveExistsError(Exception):
    """The ACTIVE row already exists, so the bootstrap commit must not overwrite it."""


class VersionStateError(Exception):
    """A version or deployment row is no longer in the status this transition requires."""


class DeployLockLostError(Exception):
    """The deployment no longer holds the namespace deploy lock (or its DEPLOY row is no longer running)."""

    def __init__(self, deployment_id: str) -> None:
        """Record which deployment was fenced out."""
        super().__init__(f"deployment {deployment_id} no longer holds the deploy lock")
        self.deployment_id = deployment_id


def _now() -> datetime:
    return datetime.now(UTC)


def _pk(namespace: str) -> str:
    return dynamo_store._namespace_pk(namespace)


def _version_sk(version: int) -> str:
    return f"{_VERSION_PREFIX}{version:06d}"


def _deploy_sk(deployment_id: str) -> str:
    return f"{_DEPLOY_PREFIX}{deployment_id}"


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _public(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _plain(v) for k, v in item.items() if k not in ("PK", "SK")}


def _conditional_failed(e: ClientError) -> bool:
    return e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _get(namespace: str, sk: str) -> dict[str, Any] | None:
    item: dict[str, Any] | None = (
        dynamo_store._get_table().get_item(Key={"PK": _pk(namespace), "SK": sk}, ConsistentRead=True).get("Item")
    )
    return item


def _query(
    namespace: str, prefix: str, *, newest_first: bool = False, limit: int | None = None
) -> list[dict[str, Any]]:
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(_pk(namespace)) & Key("SK").begins_with(prefix),
        "ScanIndexForward": not newest_first,
        "ConsistentRead": True,
    }
    if limit:
        kwargs["Limit"] = limit
    table = dynamo_store._get_table()
    items: list[dict[str, Any]] = []
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if limit or "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _update_args(
    namespace: str,
    sk: str,
    fields: dict[str, Any],
    *,
    expected_status: str | None = None,
    expected_updated_at: str | None = None,
    remove: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build ``update_item`` arguments that ``SET`` ``fields`` plus ``updated_at`` and ``REMOVE`` ``remove``.

    Every attribute name is aliased (``status``, ``error`` ... are reserved words).
    ``expected_status`` / ``expected_updated_at`` become a ``ConditionExpression``,
    so the write is rejected (``ConditionalCheckFailedException``) when the row is
    missing or no longer matches what the caller read.
    """
    fields = {**fields, "updated_at": _now().isoformat()}
    aliases: dict[str, str] = {}

    def alias(attr: str) -> str:
        return aliases.setdefault(attr, f"#n{len(aliases)}")

    values: dict[str, Any] = {}
    assignments = []
    for i, (attr, value) in enumerate(fields.items()):
        values[f":v{i}"] = value
        assignments.append(f"{alias(attr)} = :v{i}")
    conditions = []
    if expected_status is not None:
        values[":expected_status"] = expected_status
        conditions.append(f"{alias('status')} = :expected_status")
    if expected_updated_at is not None:
        values[":expected_updated_at"] = expected_updated_at
        conditions.append(f"{alias('updated_at')} = :expected_updated_at")
    expression = "SET " + ", ".join(assignments)
    if remove:
        expression += " REMOVE " + ", ".join(alias(attr) for attr in remove)
    args: dict[str, Any] = {
        "Key": {"PK": _pk(namespace), "SK": sk},
        "UpdateExpression": expression,
        "ExpressionAttributeNames": {placeholder: attr for attr, placeholder in aliases.items()},
        "ExpressionAttributeValues": values,
    }
    if conditions:
        args["ConditionExpression"] = " AND ".join(conditions)
    return args


def _set(
    namespace: str,
    sk: str,
    *,
    expected_status: str | None = None,
    expected_updated_at: str | None = None,
    **fields: Any,
) -> None:
    """``SET`` ``fields`` plus ``updated_at`` on one row, optionally only while it still matches the caller's read."""
    dynamo_store._get_table().update_item(
        **_update_args(namespace, sk, fields, expected_status=expected_status, expected_updated_at=expected_updated_at)
    )


def _transition(namespace: str, sk: str, expected_status: str, what: str, **fields: Any) -> None:
    """:func:`_set` that requires ``expected_status``; raises :class:`VersionStateError` if the row moved on."""
    try:
        _set(namespace, sk, expected_status=expected_status, **fields)
    except ClientError as e:
        if _conditional_failed(e):
            raise VersionStateError(f"{what} is no longer {expected_status}") from e
        raise


class _TransactionRejected(Exception):
    """A transaction was cancelled by failed conditions; ``failed[i]`` flags operation ``i``."""

    def __init__(self, failed: list[bool]) -> None:
        super().__init__("transaction condition failed")
        self.failed = failed


def _transact(operations: list[tuple[str, dict[str, Any]]]) -> None:
    """Run ``operations`` (``(kind, args)`` pairs) as one all-or-nothing transaction.

    Raises :class:`_TransactionRejected` when a condition failed; other cancellations
    (throttling, conflicts) propagate as ``ClientError``.
    """
    table = dynamo_store._get_table()
    items = [{kind: {"TableName": table.name, **args}} for kind, args in operations]
    try:
        table.meta.client.transact_write_items(TransactItems=items)
    except ClientError as e:
        reasons = e.response.get("CancellationReasons") or []
        failed = [r.get("Code") == "ConditionalCheckFailed" for r in reasons]
        if e.response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(failed):
            raise _TransactionRejected(failed) from e
        raise


def _fenced(deployment_id: str, operations: list[tuple[str, dict[str, Any]]]) -> None:
    """:func:`_transact` where a failed condition means ``deployment_id`` was fenced out of the lock."""
    try:
        _transact(operations)
    except _TransactionRejected as e:
        raise DeployLockLostError(deployment_id) from e


def _lock_holder_args(namespace: str, deployment_id: str) -> dict[str, Any]:
    """Key plus the condition "``deployment_id`` holds the DEPLOYLOCK" (for delete / fence / condition check)."""
    return {
        "Key": {"PK": _pk(namespace), "SK": _LOCK_SK},
        "ConditionExpression": "deployment_id = :id",
        "ExpressionAttributeValues": {":id": deployment_id},
    }


def _reject_status(fields: dict[str, Any]) -> None:
    if "status" in fields:
        raise ValueError("status changes go through finish_deployment")


# ── Versions ──────────────────────────────────────────────────────────────


def _sweep_version(namespace: str, item: dict[str, Any]) -> dict[str, Any]:
    # The write only lands while the row is still the PUBLISHING row we read; a
    # worker that finished meanwhile wins and the (swallowed) rejection leaves the row alone.
    return dynamo_store.sweep_stale_record(
        item,
        is_in_progress=lambda status: status == PUBLISHING,
        max_age_seconds=PUBLISH_STALE_SECONDS,
        terminal_status=PUBLISH_FAILED,
        error_field="error",
        error_message="Publish worker did not complete (task restarted or timed out)",
        write=lambda fields: _set(
            namespace,
            item["SK"],
            expected_status=PUBLISHING,
            expected_updated_at=item.get("updated_at"),
            **fields,
        ),
        label=f"ontology {item.get('SK')} in {namespace}",
    )


def list_versions(namespace: str) -> list[dict[str, Any]]:
    """Return every version in the namespace, newest first, with dead publishes swept to failed."""
    return [_public(_sweep_version(namespace, item)) for item in _query(namespace, _VERSION_PREFIX, newest_first=True)]


def list_versions_page(
    namespace: str, *, limit: int, after: dict[str, str] | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Return one page of versions, newest first, and the key the next page resumes after (None at the end)."""
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(_pk(namespace)) & Key("SK").begins_with(_VERSION_PREFIX),
        "ScanIndexForward": False,
        "ConsistentRead": True,
        "Limit": limit,
    }
    if after:
        kwargs["ExclusiveStartKey"] = after
    resp = dynamo_store._get_table().query(**kwargs)
    items = [_public(_sweep_version(namespace, item)) for item in resp.get("Items", [])]
    return items, resp.get("LastEvaluatedKey")


def versions_partition_key(namespace: str) -> str:
    """The DynamoDB partition the namespace's version rows live in (for ``nextToken`` checks)."""
    return _pk(namespace)


def get_version(namespace: str, version: int) -> dict[str, Any] | None:
    """Return one version row, or None."""
    item = _get(namespace, _version_sk(version))
    return _public(_sweep_version(namespace, item)) if item else None


def _reset_floor(namespace: str) -> int:
    active = get_active(namespace) or {}
    return int(active.get("reset_after_version") or 0)


def published_head(versions: list[dict[str, Any]], floor: int) -> dict[str, Any] | None:
    """Newest PUBLISHED entry of ``versions`` (newest first) above the reset ``floor``."""
    return next((v for v in versions if v["status"] == PUBLISHED and v["version"] > floor), None)


def head_version(namespace: str) -> dict[str, Any] | None:
    """Return the newest PUBLISHED version the next publish builds on (None after a reset)."""
    return published_head(list_versions(namespace), _reset_floor(namespace))


def reserve_next_version(
    namespace: str,
    *,
    ontology_id: str,
    source: str,
    source_proposal_id: str | None,
    created_by: str,
    require_first: bool = False,
    title: str | None = None,
) -> tuple[int, int | None]:
    """Claim the next version number and return ``(version, parent_version)``.

    Raises :class:`PublishInProgressError` while the newest row is still
    PUBLISHING (its parent would be stale) or when another worker claims the same
    number first. Callers retry; publishes therefore run one at a time.

    With ``require_first`` the reservation is only for a first version (bootstrap):
    if the snapshot already has a head, :class:`BootstrapSupersededError` is raised
    and no row is written.

    ``title`` is recorded on the row for the ontology's registry row, which the first
    deployment creates (see ``deploy._load_target``).
    """
    versions = list_versions(namespace)  # one snapshot: the number and the parent must agree
    if versions and versions[0]["status"] == PUBLISHING:
        raise PublishInProgressError(f"version {versions[0]['version']} is still publishing")
    version = versions[0]["version"] + 1 if versions else 1
    head = published_head(versions, _reset_floor(namespace))
    parent = head["version"] if head else None
    if require_first and head:
        raise BootstrapSupersededError(head["version"])
    now = _now().isoformat()
    try:
        dynamo_store._get_table().put_item(
            Item={
                "PK": _pk(namespace),
                "SK": _version_sk(version),
                "version": version,
                "status": PUBLISHING,
                "parent_version": parent,
                "ontology_id": ontology_id,
                "source": source,
                "source_proposal_id": source_proposal_id,
                "created_by": created_by,
                "created_at": now,
                "updated_at": now,
                **({"title": title} if title else {}),
            },
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as e:
        if _conditional_failed(e):
            raise PublishInProgressError(f"version {version} was reserved concurrently") from e
        raise
    return version, parent


def _published_fields(
    files: dict[str, Any], diff_summary: dict[str, Any], source_proposals: list[str]
) -> dict[str, Any]:
    return {
        "status": PUBLISHED,
        "files": files,
        "diff_summary": diff_summary,
        "source_proposals": source_proposals,
        "error": "",
    }


def mark_published(
    namespace: str,
    version: int,
    *,
    files: dict[str, Any],
    diff_summary: dict[str, Any],
    source_proposals: list[str],
) -> None:
    """Flip a PUBLISHING version to PUBLISHED with its manifest summary.

    Raises :class:`VersionStateError` when the row is no longer PUBLISHING (for example
    the stale sweep already failed it), so a slow worker cannot resurrect it.
    """
    _transition(
        namespace,
        _version_sk(version),
        PUBLISHING,
        f"version {version}",
        **_published_fields(files, diff_summary, source_proposals),
    )


def touch_version(namespace: str, version: int) -> None:
    """Refresh ``updated_at`` (the publish heartbeat) of a PUBLISHING version.

    A concurrent stale sweep conditions on the ``updated_at`` it read, so it loses to
    this write. Raises :class:`VersionStateError` when the row is no longer PUBLISHING.
    """
    _transition(namespace, _version_sk(version), PUBLISHING, f"version {version}")


def commit_bootstrap_version(
    namespace: str,
    version: int,
    *,
    files: dict[str, Any],
    diff_summary: dict[str, Any],
    source_proposals: list[str],
    deployed_by: str = "system",
) -> None:
    """Publish the first version and make it ACTIVE in one transaction (no deployment ran).

    The adopted ontology is already what Neptune serves, so ACTIVE must point at it
    at the moment it becomes PUBLISHED. Raises :class:`VersionStateError` when the row
    is no longer PUBLISHING and :class:`ActiveExistsError` when an ACTIVE row exists
    (a deployment or a delete ran meanwhile); the first condition is reported first.
    Nothing is written on failure.
    """
    now = _now().isoformat()
    try:
        _transact(
            [
                (
                    "Update",
                    _update_args(
                        namespace,
                        _version_sk(version),
                        _published_fields(files, diff_summary, source_proposals),
                        expected_status=PUBLISHING,
                    ),
                ),
                (
                    "Put",
                    {
                        "Item": {
                            "PK": _pk(namespace),
                            "SK": _ACTIVE_SK,
                            "version": version,
                            "deployment_id": "bootstrap",
                            "deployed_by": deployed_by,
                            "deployed_at": now,
                            "updated_at": now,
                        },
                        "ConditionExpression": "attribute_not_exists(PK)",
                    },
                ),
            ]
        )
    except _TransactionRejected as e:
        if e.failed and e.failed[0]:
            raise VersionStateError(f"version {version} is no longer {PUBLISHING}") from e
        raise ActiveExistsError(f"namespace {namespace} already has an ACTIVE row") from e


def mark_publish_failed(namespace: str, version: int, error: str) -> None:
    """Record a failed publish; the number stays as a gap.

    Raises :class:`VersionStateError` when the row is no longer PUBLISHING.
    """
    _transition(
        namespace, _version_sk(version), PUBLISHING, f"version {version}", status=PUBLISH_FAILED, error=error[:1000]
    )


# ── ACTIVE pointer ────────────────────────────────────────────────────────


def get_active(namespace: str) -> dict[str, Any] | None:
    """Return the ACTIVE row, or None when nothing was ever deployed or bootstrapped."""
    item = _get(namespace, _ACTIVE_SK)
    return _public(item) if item else None


def set_active(
    namespace: str,
    *,
    version: int,
    deployment_id: str,
    deployed_by: str,
    fence_deployment_id: str | None = None,
) -> None:
    """Point ACTIVE at ``version``.

    The reset floor (``reset_after_version``) is kept: the versions at or below it
    belong to a deleted ontology and stay undeployable. ``reset_by_ontology_id`` is
    removed, since serving a version ends any pending delete reset.

    With ``fence_deployment_id`` the write happens in a transaction that also requires
    that deployment to still hold the deploy lock; otherwise
    :class:`DeployLockLostError` is raised and ACTIVE is untouched.
    """
    args = _update_args(
        namespace,
        _ACTIVE_SK,
        {
            "version": version,
            "deployment_id": deployment_id,
            "deployed_by": deployed_by,
            "deployed_at": _now().isoformat(),
        },
        remove=("reset_by_ontology_id",),
    )
    if fence_deployment_id is None:
        dynamo_store._get_table().update_item(**args)
        return
    _fenced(
        fence_deployment_id,
        [
            ("Update", args),
            ("ConditionCheck", _lock_holder_args(namespace, fence_deployment_id)),
        ],
    )


def reset_active(namespace: str, *, ontology_id: str | None = None, fence_deployment_id: str | None = None) -> None:
    """Record that the domain ``ontology_id`` was deleted: nothing is served and the next publish has no parent.

    Every version so far falls at or below the new floor (``reset_after_version``)
    and can no longer be deployed. ``reset_by_ontology_id`` names the deleted
    ontology until a version is deployed again, so a retried delete can finish the
    reset without moving the floor.

    Fenced like :func:`set_active`: with ``fence_deployment_id`` the write requires that
    holder to still hold the deploy lock, otherwise :class:`DeployLockLostError` is
    raised and ACTIVE is untouched.
    """
    versions = list_versions(namespace)
    now = _now().isoformat()
    item = {
        "PK": _pk(namespace),
        "SK": _ACTIVE_SK,
        "version": None,
        "deployment_id": None,
        "deployed_by": None,
        "reset_at": now,
        "reset_after_version": versions[0]["version"] if versions else 0,
        "reset_by_ontology_id": ontology_id,
        "updated_at": now,
    }
    if fence_deployment_id is None:
        dynamo_store._get_table().put_item(Item=item)
        return
    _fenced(
        fence_deployment_id,
        [
            ("Put", {"Item": item}),
            ("ConditionCheck", _lock_holder_args(namespace, fence_deployment_id)),
        ],
    )


# ── Deployments ───────────────────────────────────────────────────────────


def new_deployment_id() -> str:
    """Return a time-sortable deployment id (``YYYYmmddTHHMMSSZ-xxxxxxxx``)."""
    return f"{_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def create_deployment(
    namespace: str,
    deployment_id: str,
    *,
    version: int,
    requested_by: str,
    acknowledged_breaking_metrics: list[str],
) -> None:
    """Write a new DEPLOYING row."""
    now = _now().isoformat()
    dynamo_store._get_table().put_item(
        Item={
            "PK": _pk(namespace),
            "SK": _deploy_sk(deployment_id),
            "deployment_id": deployment_id,
            "version": version,
            "status": DEPLOYING,
            "partial": False,
            "progress": {},
            "requested_by": requested_by,
            "acknowledged_breaking_metrics": acknowledged_breaking_metrics,
            "error": "",
            "created_at": now,
            "updated_at": now,
        }
    )


def finish_deployment(
    namespace: str,
    deployment_id: str,
    *,
    status: str,
    partial: bool,
    progress: dict[str, Any],
    error: str,
) -> None:
    """Move a DEPLOYING row to DEPLOYED or DEPLOY_FAILED, once.

    DEPLOYED is fenced on lock ownership: the DEPLOY row update and the release of the
    DEPLOYLOCK happen in one transaction, so a worker that lost the lock to a takeover
    can neither report success nor free the new holder's lock
    (:class:`DeployLockLostError`). DEPLOY_FAILED is not fenced (a stalled worker may
    still record its own failure) and leaves the lock to its worker; it raises
    :class:`VersionStateError` when the row is no longer DEPLOYING (already finished, or
    failed by the stale sweep).
    """
    if status not in (DEPLOYED, DEPLOY_FAILED):
        raise ValueError(f"finish_deployment status must be {DEPLOYED} or {DEPLOY_FAILED}, got {status!r}")
    fields = {"status": status, "partial": partial, "progress": progress, "error": error[:1000]}
    if status == DEPLOY_FAILED:
        _transition(namespace, _deploy_sk(deployment_id), DEPLOYING, f"deployment {deployment_id}", **fields)
        return
    _fenced(
        deployment_id,
        [
            ("Update", _update_args(namespace, _deploy_sk(deployment_id), fields, expected_status=DEPLOYING)),
            ("Delete", _lock_holder_args(namespace, deployment_id)),
        ],
    )


def checkpoint_deployment(namespace: str, deployment_id: str, **fields: Any) -> None:
    """Record progress and refresh the lock heartbeat in one transaction, fenced on lock ownership.

    Both writes happen or neither does: the DEPLOY row must still be DEPLOYING and
    ``deployment_id`` must still hold the lock, otherwise :class:`DeployLockLostError`
    is raised and the worker must stop touching Neptune, AOSS and ACTIVE.
    """
    _reject_status(fields)
    _fenced(
        deployment_id,
        [
            ("Update", _update_args(namespace, _deploy_sk(deployment_id), fields, expected_status=DEPLOYING)),
            ("Update", _heartbeat_args(namespace, deployment_id)),
        ],
    )


def _sweep_deployment(namespace: str, item: dict[str, Any]) -> dict[str, Any]:
    was_running = item.get("status") == DEPLOYING
    # The swap step, which sends the MOVE, starts once staging is checkpointed done, so
    # from then on the served graph may already be the new one. Partial asks for the
    # redeploy that converges; reporting a clean failure could hide a half-swapped graph.
    staged = (item.get("progress") or {}).get("stage") == "done"
    swept = dynamo_store.sweep_stale_record(
        item,
        is_in_progress=lambda status: status == DEPLOYING,
        max_age_seconds=STALE_SECONDS,
        terminal_status=DEPLOY_FAILED,
        error_field="error",
        error_message="Deploy worker did not complete (task restarted or timed out)",
        write=lambda fields: _set(
            namespace,
            item["SK"],
            expected_status=DEPLOYING,
            expected_updated_at=item.get("updated_at"),
            **fields,
        ),
        label=f"ontology {item.get('SK')} in {namespace}",
        extra_fields={"partial": True} if staged else None,
    )
    # Only the read that moved the row DEPLOYING -> DEPLOY_FAILED frees the lock; a row
    # the worker itself failed is released by that worker.
    if was_running and swept.get("status") == DEPLOY_FAILED:
        release_deploy_lock(namespace, str(swept.get("deployment_id", "")))
    return swept


def get_deployment(namespace: str, deployment_id: str) -> dict[str, Any] | None:
    """Return one DEPLOY row, or None."""
    item = _get(namespace, _deploy_sk(deployment_id))
    return _public(_sweep_deployment(namespace, item)) if item else None


def latest_deployment(namespace: str) -> dict[str, Any] | None:
    """Return the most recent DEPLOY row, or None."""
    items = _query(namespace, _DEPLOY_PREFIX, newest_first=True, limit=1)
    return _public(_sweep_deployment(namespace, items[0])) if items else None


def serving_deployment(namespace: str) -> dict[str, Any] | None:
    """Return the most recent DEPLOY row that may have changed what is served, or None.

    A deployment that failed with ``partial`` false failed before any MOVE of it could land,
    so it left serving as the deployments before it did. Such rows are skipped: a ``partial``
    row before them still means serving is half-swapped, which a redeploy or rollback that
    failed early must not hide.

    DEPLOY rows are never deleted and the Versions tab polls this while a deployment runs, so
    the rows are read newest first a page at a time, and no further than the row returned.
    """
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(_pk(namespace)) & Key("SK").begins_with(_DEPLOY_PREFIX),
        "ScanIndexForward": False,
        "ConsistentRead": True,
        "Limit": _SERVING_PAGE,
    }
    table = dynamo_store._get_table()
    while True:
        resp = table.query(**kwargs)
        for item in resp.get("Items", []):
            row = _public(_sweep_deployment(namespace, item))
            if row.get("status") != DEPLOY_FAILED or row.get("partial"):
                return row
        if "LastEvaluatedKey" not in resp:
            return None
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def list_deployments(namespace: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Return the namespace's DEPLOY rows newest first (at most ``limit``), with dead workers swept to failed."""
    items = _query(namespace, _DEPLOY_PREFIX, newest_first=True, limit=limit)
    return [_public(_sweep_deployment(namespace, item)) for item in items]


# ── Deploy lock ───────────────────────────────────────────────────────────


def acquire_deploy_lock(namespace: str, deployment_id: str) -> None:
    """Take the namespace deploy lock, or raise :class:`DeployLockHeldError` naming its live holder."""
    now = _now()
    ts = int(now.timestamp())
    try:
        dynamo_store._get_table().put_item(
            Item={
                "PK": _pk(namespace),
                "SK": _LOCK_SK,
                "deployment_id": deployment_id,
                "heartbeat_ts": ts,
                "updated_at": now.isoformat(),
            },
            ConditionExpression=(
                "attribute_not_exists(PK) OR attribute_not_exists(heartbeat_ts) OR heartbeat_ts < :stale"
            ),
            ExpressionAttributeValues={":stale": ts - STALE_SECONDS},
        )
    except ClientError as e:
        if not _conditional_failed(e):
            raise
        holder = (_get(namespace, _LOCK_SK) or {}).get("deployment_id", "")
        raise DeployLockHeldError(str(holder)) from e


def _heartbeat_args(namespace: str, deployment_id: str) -> dict[str, Any]:
    now = _now()
    return {
        "Key": {"PK": _pk(namespace), "SK": _LOCK_SK},
        "UpdateExpression": "SET heartbeat_ts = :ts, updated_at = :now",
        "ConditionExpression": "deployment_id = :id",
        "ExpressionAttributeValues": {":ts": int(now.timestamp()), ":now": now.isoformat(), ":id": deployment_id},
    }


def heartbeat_deploy_lock(namespace: str, deployment_id: str) -> bool:
    """Refresh the lock's heartbeat; return False when ``deployment_id`` no longer holds it."""
    try:
        dynamo_store._get_table().update_item(**_heartbeat_args(namespace, deployment_id))
    except ClientError as e:
        if _conditional_failed(e):
            return False
        raise
    return True


def release_deploy_lock(namespace: str, deployment_id: str) -> None:
    """Delete the lock if ``deployment_id`` holds it (no-op otherwise)."""
    try:
        dynamo_store._get_table().delete_item(**_lock_holder_args(namespace, deployment_id))
    except ClientError as e:
        if not _conditional_failed(e):
            raise


def deploy_lock_holder(namespace: str) -> str | None:
    """Return the deployment id holding a live lock, or None."""
    item = _get(namespace, _LOCK_SK)
    if not item or int(item.get("heartbeat_ts", 0)) < int(_now().timestamp()) - STALE_SECONDS:
        return None
    return str(item["deployment_id"])
