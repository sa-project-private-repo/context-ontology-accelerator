# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The namespace's mapping table: lexical strings mapped to TBox terms (spec §4).

One table per namespace (not per version), so a rollback keeps human corrections; each
row's validity against the active TBox is recomputed at every build (``effective``).

A row's kind fixes the shape of its key and the targets it accepts:

- ``class``: key ``(lexical class,)``. Target: a TBox class IRI, or ``ignore``.
- ``relation``: key ``(lexical relation, subject class IRI, object class IRI)``. Target: a TBox
  object property IRI, or ``ignore``. ``direction`` says whether the property runs subject to
  object (``forward``) or object to subject (``inverse``).
- ``attribute``: key ``(subject class IRI, lexical predicate)``. Target: a TBox datatype
  property IRI, or ``free`` (keep the value as a free-form attribute). An attribute is never
  ``ignore``d.

Automatic decisions never overwrite a human one (``save_auto``). Human edits are optimistic:
``set_manual`` and ``reset_to_auto`` raise ``MappingConflict`` when the row changed after it
was read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from coa_ontology import dynamo_store

from . import store
from .tbox import TBox

_log = logging.getLogger("abox.mapping")

CLASS, RELATION, ATTRIBUTE = "class", "relation", "attribute"
KINDS = (CLASS, RELATION, ATTRIBUTE)
IGNORE, FREE = "ignore", "free"
FORWARD, INVERSE = "forward", "inverse"
AUTO, MANUAL = "auto", "manual"
ACTIVE, STALE, IGNORED = "active", "stale", "ignored"

_SK = "ABOXMAP#"
_ID = re.compile(r"(class|relation|attribute)\.[0-9a-f]{32}")
# Number of parts in the key of each kind (see the module docstring).
_KEY_PARTS = {CLASS: 1, RELATION: 3, ATTRIBUTE: 2}


class MappingConflict(Exception):
    """The row changed after it was read, so the edit was not applied."""


def _check_key(kind: str, key: tuple[str, ...]) -> None:
    expected = _KEY_PARTS.get(kind)
    if expected is None:
        raise ValueError(f"Unknown mapping kind {kind!r}: expected one of {KINDS}")
    if len(key) != expected:
        raise ValueError(f"A {kind} mapping key has {expected} part(s), got {len(key)}")


def mapping_id(kind: str, key: tuple[str, ...]) -> str:
    """The stable, path-safe id of the row with this kind and key.

    The encoding is stored in DynamoDB sort keys and the API: changing it orphans stored rows.
    Raises ``ValueError`` when ``key`` does not have the shape of ``kind``.
    """
    _check_key(kind, key)
    return f"{kind}.{hashlib.sha256(chr(31).join(key).encode('utf-8')).hexdigest()[:32]}"


def is_mapping_id(value: str) -> bool:
    """Whether ``value`` has the shape of a mapping id."""
    return _ID.fullmatch(value) is not None


def _sk(mid: str) -> str:
    kind, digest = mid.split(".", 1)
    return f"{_SK}{kind}#{digest}"


@dataclass(frozen=True)
class MappingRow:
    """One lexical key and the TBox term it maps to (key shapes and targets: see the module docstring)."""

    kind: str
    key: tuple[str, ...]
    target: str
    direction: str = FORWARD
    origin: str = AUTO
    status: str = ACTIVE
    rationale: str = ""
    candidate_hash: str = ""
    decided_for_version: int | None = None
    updated_by: str = "system"
    updated_at: str = ""

    def __post_init__(self) -> None:
        """Reject a key that does not have the shape of the row's kind."""
        _check_key(self.kind, self.key)

    @property
    def id(self) -> str:
        """The row's mapping id."""
        return mapping_id(self.kind, self.key)


def _to_item(namespace: str, row: MappingRow) -> dict[str, Any]:
    item = {**asdict(row), "key": list(row.key), "updated_at": datetime.now(UTC).isoformat()}
    return {**store.ddb_key(namespace, _sk(row.id)), **{k: v for k, v in item.items() if v is not None}}


def _from_item(item: dict[str, Any]) -> MappingRow:
    item = store.from_dynamo(item)
    return MappingRow(
        kind=item["kind"],
        key=tuple(item["key"]),
        target=item["target"],
        direction=item.get("direction", FORWARD),
        origin=item.get("origin", AUTO),
        status=item.get("status", ACTIVE),
        rationale=item.get("rationale", ""),
        candidate_hash=item.get("candidate_hash", ""),
        decided_for_version=item.get("decided_for_version"),
        updated_by=item.get("updated_by", "system"),
        updated_at=item.get("updated_at", ""),
    )


def list_rows(namespace: str, kind: str | None = None) -> list[MappingRow]:
    """All mapping rows of the namespace (optionally one kind), sorted by kind and key."""
    prefix = f"{_SK}{kind}#" if kind else _SK
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(store.namespace_pk(namespace)) & Key("SK").begins_with(prefix),
        "ConsistentRead": True,
    }
    rows: list[MappingRow] = []
    table = dynamo_store._get_table()
    while True:
        resp = table.query(**kwargs)
        rows.extend(_from_item(i) for i in resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return sorted(rows, key=lambda r: (r.kind, r.key))


def list_rows_page(
    namespace: str,
    kind: str | None,
    status: str | None,
    *,
    limit: int,
    after: dict[str, str] | None = None,
) -> tuple[list[MappingRow], dict[str, str] | None]:
    """One page of mapping rows in sort-key order (kind, then id digest), optionally of one kind or status.

    Returns the rows and the key the next page resumes after (None at the end). The status is a
    filter, so a page may be short or empty while a resume key is still returned.
    """
    prefix = f"{_SK}{kind}#" if kind else _SK
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(store.namespace_pk(namespace)) & Key("SK").begins_with(prefix),
        "ConsistentRead": True,
    }
    if status is not None:
        kwargs["FilterExpression"] = Attr("status").eq(status)
    table = dynamo_store._get_table()
    items, next_key = dynamo_store._read_page(
        table.query, kwargs, limit, after, max_calls=dynamo_store.MAX_SCAN_CALLS_PER_PAGE
    )
    return [_from_item(i) for i in items], next_key


def get_row(namespace: str, mid: str) -> MappingRow | None:
    """One mapping row by id, or None."""
    item = dynamo_store._get_table().get_item(Key=store.ddb_key(namespace, _sk(mid)), ConsistentRead=True).get("Item")
    return _from_item(item) if item else None


def _conditional(namespace: str, kind: str, args: dict[str, Any], fence_build_id: str | None) -> bool:
    """Run one conditional ``Put`` or ``Update`` (``kind``); False when its condition failed.

    With ``fence_build_id`` the write only lands while that build holds ``ABOXLOCK``, otherwise
    ``store.LockLost`` is raised.
    """
    if fence_build_id is not None:
        return store.fenced_write(namespace, fence_build_id, (kind, args))
    table = dynamo_store._get_table()
    try:
        (table.put_item if kind == "Put" else table.update_item)(**args)
    except ClientError as e:
        if store.is_conditional_failure(e):
            return False
        raise
    return True


def save_auto(namespace: str, row: MappingRow, *, fence_build_id: str | None = None) -> bool:
    """Write an automatic decision (``origin=auto``) unless a human owns the row; False when one does.

    A build passes its lock as ``fence_build_id`` (see ``_conditional``).
    """
    put = {
        "Item": _to_item(namespace, replace(row, origin=AUTO)),
        "ConditionExpression": "attribute_not_exists(SK) OR attribute_not_exists(#origin) OR #origin = :auto",
        "ExpressionAttributeNames": {"#origin": "origin"},
        "ExpressionAttributeValues": {":auto": AUTO},
    }
    return _conditional(namespace, "Put", put, fence_build_id)


def _replace_if_unchanged(
    namespace: str, read: MappingRow, new: MappingRow, expected_updated_at: str | None
) -> MappingRow:
    """Write ``new`` over ``read`` unless the row changed since it was fetched.

    The row must still carry ``expected_updated_at``, which defaults to the ``updated_at`` of
    ``read``. Raises ``KeyError`` when the row is gone and ``MappingConflict`` when it changed.
    """
    version = read.updated_at if expected_updated_at is None else expected_updated_at
    item = _to_item(namespace, new)
    try:
        dynamo_store._get_table().put_item(
            Item=item,
            ConditionExpression="attribute_exists(SK) AND updated_at = :read",
            ExpressionAttributeValues={":read": version},
        )
    except ClientError as e:
        if not store.is_conditional_failure(e):
            raise
        if get_row(namespace, read.id) is None:
            raise KeyError(read.id) from e
        raise MappingConflict(f"Mapping {read.id} changed since the version being edited was read") from e
    return _from_item(item)


def set_manual(
    namespace: str,
    mid: str,
    target: str,
    direction: str,
    updated_by: str,
    expected_updated_at: str | None = None,
) -> MappingRow:
    """Pin a row to a human decision.

    ``expected_updated_at`` is the row's ``updated_at`` as the editor saw it. When given, the
    edit only applies to that version of the row, so an edit made from a stale view conflicts.
    Raises ``KeyError`` when the row does not exist and ``MappingConflict`` when it changed
    after this call read it or does not match ``expected_updated_at``.
    """
    row = get_row(namespace, mid)
    if row is None:
        raise KeyError(mid)
    status = IGNORED if target == IGNORE else ACTIVE  # what ``effective`` will say once the build runs
    new = replace(row, target=target, direction=direction, origin=MANUAL, status=status, updated_by=updated_by)
    return _replace_if_unchanged(namespace, row, new, expected_updated_at)


def reset_to_auto(namespace: str, mid: str, updated_by: str, expected_updated_at: str | None = None) -> MappingRow:
    """Hand a manual row back to the matcher.

    ``expected_updated_at`` is the row's ``updated_at`` as the editor saw it (see ``set_manual``).
    Raises ``KeyError`` when the row does not exist and ``MappingConflict`` when it changed
    after this call read it or does not match ``expected_updated_at``.
    """
    row = get_row(namespace, mid)
    if row is None:
        raise KeyError(mid)
    # An empty candidate hash makes the next build decide this row again.
    new = replace(row, origin=AUTO, candidate_hash="", rationale="", updated_by=updated_by)
    return _replace_if_unchanged(namespace, row, new, expected_updated_at)


def persist_status(namespace: str, rows: Iterable[MappingRow], *, fence_build_id: str | None = None) -> None:
    """Record recomputed statuses without touching origin or target.

    A write only lands on the row the build read: same origin (a row stored without one reads as
    ``auto``) and same target. A row that was changed or removed meanwhile is skipped and logged
    at INFO: its writer set its status, and the next build recomputes it. A build passes its lock
    as ``fence_build_id`` (see ``_conditional``).
    """
    for row in rows:
        update = {
            "Key": store.ddb_key(namespace, _sk(row.id)),
            "UpdateExpression": "SET #s = :s",
            "ConditionExpression": (
                "attribute_exists(SK) AND (attribute_not_exists(#o) OR #o = :origin) AND #t = :target"
            ),
            "ExpressionAttributeNames": {"#s": "status", "#o": "origin", "#t": "target"},
            "ExpressionAttributeValues": {":s": row.status, ":origin": row.origin, ":target": row.target},
        }
        if not _conditional(namespace, "Update", update, fence_build_id):
            _log.info("Mapping %s was changed or removed during the build; its status was not persisted", row.id)


def valid_target(tbox: TBox, kind: str, target: str) -> bool:
    """Whether ``target`` is allowed for a ``kind`` row against ``tbox`` (spec §4.1).

    A class or relation row takes ``ignore`` or a TBox term of its kind; an attribute row takes
    ``free`` or a TBox datatype property, never ``ignore``.
    """
    if kind == CLASS:
        return target == IGNORE or target in tbox.classes
    if kind == RELATION:
        return target == IGNORE or target in tbox.object_props
    if kind == ATTRIBUTE:
        return target == FREE or target in tbox.datatype_props
    return False


def relation_fits(tbox: TBox, key: tuple[str, ...], target: str, direction: str) -> bool:
    """Whether object property ``target`` admits a relation key's classes in ``direction``.

    The build links two instances only when the property's domain and range admit them
    (``tbox.fits``), so a row that does not fit maps nothing.
    """
    subject, obj = (key[2], key[1]) if direction == INVERSE else (key[1], key[2])
    return tbox.fits(tbox.object_props[target], subject, obj)


def effective(rows: Iterable[MappingRow], tbox: TBox) -> list[MappingRow]:
    """The rows with ``status`` recomputed against ``tbox``.

    A target not allowed by ``valid_target`` is stale, and so is a relation row whose property does
    not fit its classes in its direction (``relation_fits``): the build would drop every link of it.
    """
    out = []
    for row in rows:
        if not valid_target(tbox, row.kind, row.target):
            status = STALE
        elif row.target == IGNORE:
            status = IGNORED
        elif row.kind == RELATION and not relation_fits(tbox, row.key, row.target, row.direction):
            status = STALE
        else:
            status = ACTIVE
        out.append(replace(row, status=status))
    return out


def revision(rows: Iterable[MappingRow]) -> str:
    """A digest of the active mappings; rationale, origin and inactive rows do not affect it."""
    applied = sorted([r.kind, list(r.key), r.target, r.direction] for r in rows if r.status == ACTIVE)
    return hashlib.sha256(json.dumps(applied).encode("utf-8")).hexdigest()
