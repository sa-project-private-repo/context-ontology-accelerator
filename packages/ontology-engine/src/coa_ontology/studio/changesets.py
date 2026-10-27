# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Change sets the Ontology designer agent proposes, and how they are previewed, applied or discarded.

A change set is the row ``STUDIOCS#{changeSetId}`` under the
proposal (``{ns}#PROPOSAL#{id}``). It holds the ops as the agent proposed them, which applied to
``created_revision`` when they were stored, resolved like a revision's record (each add op carries the
IRI it minted there), with their rationale and status (``pending``, ``applied`` or ``discarded``). The
agent only proposes. Applying runs the selected ops again on the current revision
and writes a revision with ``source = "agent"``. The same DynamoDB transaction flips the row from
``pending`` to ``applied`` (``draft.commit``), so a change set is applied at most once and never after
a discard. The row's ``summary`` and ``warnings`` lines also come as codes (``summary_messages`` and
``warning_messages``), except in a row stored before the codes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from coa_ontology import dynamo_store
from coa_ontology.abox.store import from_dynamo
from coa_ontology.versions import snapshot

from . import changes, chat_store, graph_view
from . import draft as store
from .iris import check_iri
from .messages import english, listed
from .ops import USER_OPS, ChangeSetError, apply_ops

CS_SK_PREFIX = "STUDIOCS#"
PENDING, APPLIED, DISCARDED = "pending", "applied", "discarded"
STATUSES = (PENDING, APPLIED, DISCARDED)
MAX_AGENT_OPS = 50
MAX_RATIONALE = 1000
# Below DynamoDB's 400 KB item limit, with room for the status fields set later.
MAX_ROW_BYTES = 300_000
# How many of the newest change sets the agent reads for its context.
MAX_RECENT = 500
# Bytes of change sets (as UTF-8 JSON) one ``ListStudioChangeSets`` page holds, well below the 6 MB
# Lambda response limit the API proxy answers through. A row larger than this still comes alone.
MAX_PAGE_BYTES = 4_000_000
_ID = re.compile(r"^[0-9A-Za-z-]{1,64}$")


class ChangeSetNotFound(store.StudioError):
    """No change set with that id on this proposal."""

    status = 404
    code = "ChangeSetNotFound"


class ChangeSetNotPending(store.StudioError):
    """The change set was already applied or discarded."""

    code = "ChangeSetNotPending"


class InvalidOpIndexes(store.StudioError, ValueError):
    """``opIndexes`` that do not name ops of the change set: empty, repeated or out of range."""

    status = 422
    code = "InvalidRequest"


class ChangeSetStale(store.StudioError):
    """The draft changed since the change set was proposed, and it no longer applies."""

    code = "ChangeSetStale"


def check_id(change_set_id: str) -> str:
    """Return ``change_set_id`` if it can be one; raise ``ValueError`` otherwise."""
    if not _ID.match(change_set_id):
        raise ValueError(f"Invalid change set id {change_set_id!r}")
    return change_set_id


def _key(namespace: str, proposal_id: str, change_set_id: str) -> dict[str, str]:
    store._checked(namespace, proposal_id)
    return {"PK": dynamo_store._proposal_pk(namespace, proposal_id), "SK": f"{CS_SK_PREFIX}{check_id(change_set_id)}"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def view(item: dict[str, Any]) -> dict[str, Any]:
    """A change set row as the API shows it."""
    return {
        "changeSetId": item["change_set_id"],
        "turnId": item.get("turn_id"),
        "status": item["status"],
        "createdAt": item.get("created_at"),
        "createdRevision": int(item["created_revision"]),
        "createdBy": item.get("created_by"),
        "rationale": item.get("rationale") or "",
        "ops": item.get("ops") or [],
        "summary": item.get("summary") or [],
        "summaryMessages": listed(item.get("summary_messages")),
        "warnings": item.get("warnings") or [],
        "warningMessages": listed(item.get("warning_messages")),
        "diffCounts": item.get("diff_counts"),
        "appliedRevision": item.get("applied_revision"),
        "appliedOpIndexes": item.get("applied_op_indexes"),
        "appliedBy": item.get("applied_by"),
        "appliedAt": item.get("applied_at"),
        "discardedBy": item.get("discarded_by"),
        "discardedAt": item.get("discarded_at"),
        "stale": item.get("stale"),
    }


def _item(namespace: str, proposal_id: str, change_set_id: str) -> dict[str, Any]:
    raw = (
        dynamo_store._get_table()
        .get_item(Key=_key(namespace, proposal_id, change_set_id), ConsistentRead=True)
        .get("Item")
    )
    if raw is None:
        raise ChangeSetNotFound(f"Change set '{change_set_id}' not found")
    item: dict[str, Any] = from_dynamo(raw)
    return item


def get_change_set(namespace: str, proposal_id: str, change_set_id: str) -> dict[str, Any]:
    """One change set, or :class:`ChangeSetNotFound`."""
    return view(_item(namespace, proposal_id, change_set_id))


def _not_pending(item: dict[str, Any]) -> ChangeSetNotPending:
    return ChangeSetNotPending(f"This change set was already {item['status']}", status=item["status"])


def propose(
    namespace: str, proposal_id: str, raw_ops: list[dict[str, Any]], rationale: str, *, turn_id: str, author: str
) -> dict[str, Any]:
    """Dry-run ``raw_ops`` on the current revision and store them as a pending change set; return its view.

    The draft is not changed. A malformed op raises pydantic's ``ValidationError``, an op that does
    not apply ``changes.InvalidChange`` with its ``opIndex``, and so do a change set without ops,
    with more than ``MAX_AGENT_OPS``, without a rationale or too large to store.
    """
    if not raw_ops:
        raise changes.InvalidChange("A change set needs at least one op")
    if len(raw_ops) > MAX_AGENT_OPS:
        raise changes.InvalidChange(f"A change set has at most {MAX_AGENT_OPS} ops; split it into smaller ones")
    text = " ".join((rationale or "").split())
    if not text:
        raise changes.InvalidChange("A change set needs a rationale")
    if len(text) > MAX_RATIONALE:
        raise changes.InvalidChange(f"A rationale is at most {MAX_RATIONALE} characters")
    ops = USER_OPS.validate_python(raw_ops)
    state = store.require_state(store.get_proposal(namespace, proposal_id), editable=True)
    before = store.load_revision(namespace, proposal_id, state.revision)
    after, ctx = changes._apply(before, ops, state, namespace)
    change_set_id = chat_store.new_id()
    item = {
        **_key(namespace, proposal_id, change_set_id),
        "change_set_id": change_set_id,
        "turn_id": turn_id,
        "status": PENDING,
        "created_at": _now(),
        "created_revision": state.revision,
        "created_by": author,
        "rationale": text,
        # Resolved like a revision's record: later ops may name an IRI an add op minted, and apply runs
        # the ops again on the current revision, where minting anew could pick another IRI.
        "ops": changes._resolved(ops, ctx.created),
        "summary": [english(m) for m in ctx.summary_messages],
        "summary_messages": list(ctx.summary_messages),
        "warnings": list(ctx.warnings),
        "warning_messages": list(ctx.warning_messages),
        "diff_counts": snapshot.diff_summary(snapshot.compute_diff(before.draft.tbox, after.tbox)),
    }
    if len(json.dumps(item, ensure_ascii=False)) > MAX_ROW_BYTES:
        raise changes.InvalidChange("This change set is too large to store; split it into smaller ones")
    dynamo_store._get_table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    return view(item)


def partition(namespace: str, proposal_id: str) -> str:
    """The partition key the proposal's change sets are stored under."""
    store._checked(namespace, proposal_id)
    return dynamo_store._proposal_pk(namespace, proposal_id)


def recent_change_sets(namespace: str, proposal_id: str, status: str | None = None) -> list[dict[str, Any]]:
    """The proposal's newest ``MAX_RECENT`` change sets, oldest first, optionally of one status (for the agent)."""
    query: dict[str, Any] = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :skp)",
        "ExpressionAttributeValues": {":pk": partition(namespace, proposal_id), ":skp": CS_SK_PREFIX},
        "ConsistentRead": True,
        "ScanIndexForward": False,  # newest first, so the cap drops the oldest
    }
    items: list[dict[str, Any]] = []
    while len(items) < MAX_RECENT:
        page = dynamo_store._get_table().query(**query)
        items.extend(from_dynamo(i) for i in page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return [view(i) for i in reversed(items[:MAX_RECENT]) if status is None or i["status"] == status]


def list_change_sets(
    namespace: str,
    proposal_id: str,
    status: str | None = None,
    *,
    limit: int,
    start: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One page of the proposal's change sets, oldest first, optionally of one status.

    Reads at most ``limit`` change sets after ``start`` (a key of an earlier page) and keeps the ones
    of ``status``, so a page can hold fewer, even none. The page also stops before the change set that
    would take it past ``MAX_PAGE_BYTES``, unless that change set would be its first. ``next`` is the
    key to resume after, or None at the end.
    """
    state = store.require_state(store.get_proposal(namespace, proposal_id), editable=False)
    query: dict[str, Any] = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :skp)",
        "ExpressionAttributeValues": {":pk": partition(namespace, proposal_id), ":skp": CS_SK_PREFIX},
        "ConsistentRead": True,
        "Limit": limit,
    }
    if start is not None:
        query["ExclusiveStartKey"] = start
    page = dynamo_store._get_table().query(**query)
    last = page.get("LastEvaluatedKey")
    resume = {"PK": str(last["PK"]), "SK": str(last["SK"])} if last else None
    listed: list[dict[str, Any]] = []
    size = 0
    read: dict[str, str] | None = start  # the key of the last row this page covers
    for item in (from_dynamo(i) for i in page.get("Items", [])):
        if status is None or item["status"] == status:
            shown = view(item)
            row = len(json.dumps(shown, ensure_ascii=False, default=str).encode("utf-8"))
            if listed and size + row > MAX_PAGE_BYTES:
                resume = read
                break
            listed.append(shown)
            size += row
        read = {"PK": str(item["PK"]), "SK": str(item["SK"])}
    return {"revision": state.revision, "changeSets": listed, "next": resume}


def _indexes(op_indexes: Sequence[int] | None, count: int) -> list[int]:
    """The ops to run, ascending: all of them for None.

    Raises :class:`InvalidOpIndexes` for an empty list, a repeated index or one out of range.
    """
    if op_indexes is None:
        return list(range(count))
    picked = sorted(set(op_indexes))
    if not picked:
        raise InvalidOpIndexes("opIndexes needs at least one op index")
    if len(picked) != len(op_indexes):
        raise InvalidOpIndexes("opIndexes names an op twice")
    if picked[0] < 0 or picked[-1] >= count:
        raise InvalidOpIndexes(f"opIndexes must be between 0 and {count - 1}")
    return picked


def preview(
    namespace: str, proposal_id: str, change_set_id: str, op_indexes: Sequence[int] | None, focus: str | None
) -> dict[str, Any]:
    """What applying the selected ops to the current revision would do, without storing anything.

    When they do not apply, ``applies`` is false and ``error`` names the op (by its index in the
    change set). Otherwise ``graph`` is ``graph_view.preview_view`` around ``focus``.
    """
    if focus is not None:
        check_iri(focus, "focus")
    item = _item(namespace, proposal_id, change_set_id)
    indexes = _indexes(op_indexes, len(item["ops"]))
    state = store.require_state(store.get_proposal(namespace, proposal_id), editable=False)
    before = store.load_revision(namespace, proposal_id, state.revision)
    scheme = store.scheme(namespace, state)
    result: dict[str, Any] = {"revision": state.revision, "changeSetId": change_set_id, "opIndexes": indexes}
    try:
        after, ctx = apply_ops(before.draft, USER_OPS.validate_python([item["ops"][i] for i in indexes]), scheme)
    except ChangeSetError as e:
        error = {"message": e.message, "opIndex": indexes[e.index]}
        return {**result, "applies": False, "error": error, "warnings": [], "created": [], "diff": None, "graph": None}
    diff = snapshot.compute_diff(before.draft.tbox, after.tbox)
    graph = graph_view.preview_view(
        before.draft,
        after,
        scheme,
        revision=state.revision,
        focus=focus,
        badges=graph_view.abox_badges(namespace),
        diff=diff,
    )
    return {
        **result,
        "applies": True,
        "error": None,
        "warnings": list(ctx.warnings),
        "created": list(ctx.created),
        "diff": diff,
        "graph": graph,
    }


def _applies(state: store.StudioState, before: store.Revision, raw_ops: list[dict[str, Any]], namespace: str) -> bool:
    """Whether every op of a change set applies to ``before``."""
    try:
        apply_ops(before.draft, USER_OPS.validate_python(raw_ops), store.scheme(namespace, state))
    except ChangeSetError:
        return False
    return True


def _mark_stale(namespace: str, proposal_id: str, change_set_id: str, stale: dict[str, Any]) -> None:
    try:
        dynamo_store._get_table().update_item(
            Key=_key(namespace, proposal_id, change_set_id),
            UpdateExpression="SET stale = :stale",
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":stale": stale, ":pending": PENDING},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def apply(
    namespace: str,
    proposal_id: str,
    change_set_id: str,
    expected_revision: int,
    op_indexes: Sequence[int] | None,
    *,
    author: str,
) -> dict[str, Any]:
    """Apply the selected ops of a pending change set (all for None) as one agent revision.

    Returns the result of ``changes.apply_change_set`` plus the updated ``changeSet``. When the ops
    do not apply, nothing is written: on the revision the change set was made on, that is
    ``changes.InvalidChange``; on a newer one, :class:`ChangeSetStale`, and the change set records
    it in ``stale``. :class:`ChangeSetNotPending` when it was applied or discarded, even by a write
    that lands while this one runs.
    """
    item = _item(namespace, proposal_id, change_set_id)
    if item["status"] != PENDING:
        raise _not_pending(item)
    indexes = _indexes(op_indexes, len(item["ops"]))
    state = changes._editable(namespace, proposal_id, expected_revision)
    before = store.load_revision(namespace, proposal_id, state.revision)
    ops = USER_OPS.validate_python([item["ops"][i] for i in indexes])
    try:
        after, ctx = apply_ops(before.draft, ops, store.scheme(namespace, state))
    except ChangeSetError as e:
        op_index = indexes[e.index]
        if state.revision == int(item["created_revision"]) or _applies(state, before, item["ops"], namespace):
            # The selection, not the draft, is at fault: the whole change set still applies.
            raise changes.InvalidChange(f"Op {op_index}: {e.message}", opIndex=op_index) from e
        message = (
            "The draft changed after the assistant proposed this change set, and it no longer applies: "
            f"Op {op_index}: {e.message}"
        )
        _mark_stale(
            namespace,
            proposal_id,
            change_set_id,
            {"revision": state.revision, "message": message, "opIndex": op_index},
        )
        raise ChangeSetStale(message, opIndex=op_index, revision=state.revision) from e
    applied_at = _now()

    def flip(revision: int) -> list[tuple[str, dict[str, Any]]]:
        update = {
            "Key": _key(namespace, proposal_id, change_set_id),
            "UpdateExpression": (
                "SET #s = :applied, applied_revision = :rev, applied_op_indexes = :ix, "
                "applied_by = :by, applied_at = :at REMOVE stale"
            ),
            "ConditionExpression": "#s = :pending",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {
                ":applied": APPLIED,
                ":rev": revision,
                ":ix": indexes,
                ":by": author,
                ":at": applied_at,
                ":pending": PENDING,
            },
        }
        return [("Update", update)]

    try:
        revision = changes.write_revision(
            namespace,
            proposal_id,
            state,
            kind="apply",
            after=after,
            ops=changes._resolved(ops, ctx.created),
            inverse_base=(before.tbox_nt, before.r2rml_nt),
            keep_applied=state.applied,
            push=True,
            redo=(),
            author=author,
            summary=[english(m) for m in ctx.summary_messages],
            warnings=list(ctx.warnings),
            summary_messages=list(ctx.summary_messages),
            warning_messages=list(ctx.warning_messages),
            extra={"changeSetId": change_set_id, "opIndexes": indexes},
            source="agent",
            rationale=item.get("rationale"),
            also=flip,
        )
    except store.CommitGuardFailed as e:
        raise _not_pending(_item(namespace, proposal_id, change_set_id)) from e
    result = changes._result(
        revision, dry_run=False, before=before.draft, after=after, ctx=ctx, can_undo=True, can_redo=False
    )
    return {**result, "changeSet": get_change_set(namespace, proposal_id, change_set_id)}


def discard(namespace: str, proposal_id: str, change_set_id: str, *, author: str) -> dict[str, Any]:
    """Mark a pending change set discarded and return it; :class:`ChangeSetNotPending` otherwise."""
    store.require_state(store.get_proposal(namespace, proposal_id), editable=False)
    _item(namespace, proposal_id, change_set_id)
    try:
        dynamo_store._get_table().update_item(
            Key=_key(namespace, proposal_id, change_set_id),
            UpdateExpression="SET #s = :discarded, discarded_by = :by, discarded_at = :at",
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":discarded": DISCARDED, ":by": author, ":at": _now(), ":pending": PENDING},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        raise _not_pending(_item(namespace, proposal_id, change_set_id)) from e
    return get_change_set(namespace, proposal_id, change_set_id)
