# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Ontology designer drafts: S3 revisions, the ``studio`` field of the proposal row, and opening.

Layout under ``proposals/{ns}/{id}/studio/``:

- ``rev-{n:06d}/ontology.nt`` and ``rev-{n:06d}/r2rml.nt``: the draft at revision ``n``, canonical
  N-Triples (A's ``canonical_nt``). Immutable once written.
- ``ops/{n:06d}.json``: the change record of revision ``n`` (kind, ops, inverse, author, source,
  rationale, time).
  Writing it with ``If-None-Match: *`` claims the number ``n``; only the claimer writes ``rev-{n}``.

The proposal row (``{ns}#PROPOSAL#{id}`` / ``META``) carries ``studio = {base_version, revision,
ontology_id, style, applied, redo, counts, opened_at, opened_by, updated_at, updated_by}``. A revision
becomes current only by the conditional update ``studio.revision = :expected``. A
writer that dies after claiming, or loses the commit, leaves a gap: its record and files stay as they
are, the next writer claims the next free number, and readers, who walk the records' ``baseRevision``
links back from ``studio.revision``, never reach it. Purging ``proposals/{ns}/`` removes the leftovers.
"""

from __future__ import annotations

import contextlib
import functools
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError
from coa_common import validate_namespace_name

from coa_ontology import dynamo_store
from coa_ontology.abox import store as abox_store
from coa_ontology.catalog.ingest import schema_subjects
from coa_ontology.versions import publish as version_publish
from coa_ontology.versions import registry, snapshot

from . import iris
from .messages import english, message
from .ops import Draft, parse_nt

EDITABLE_STATUSES = ("pending", "updated", "accept_failed")
FORMAT_VERSION = 1
MAX_CLAIM_ATTEMPTS = 20
# The model's ``StudioRevision`` bound on a revision a request names.
MAX_REVISION = 1_000_000_000
_PROPOSAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WRITE_CONFLICT_CODES = ("PreconditionFailed", "ConditionalRequestConflict")


class StudioError(Exception):
    """A designer request the current state refuses; ``status`` and ``code`` go into the HTTP error."""

    status = 409
    code = "StudioConflict"

    def __init__(self, message: str, **extra: Any) -> None:
        """Keep the message and any structured fields for the response body."""
        super().__init__(message)
        self.message = message
        self.extra = extra

    def detail(self) -> dict[str, Any]:
        """The ``detail`` body of the HTTP error."""
        return {"code": self.code, "message": self.message, **self.extra}


class ProposalNotFound(StudioError):
    """No proposal with that id in the namespace."""

    status = 404
    code = "ProposalNotFound"


class NotOpen(StudioError):
    """The proposal has no designer draft yet."""

    status = 404
    code = "StudioDraftNotOpen"


class NotEditable(StudioError):
    """The proposal's status no longer allows edits (accepting, accepted, rejected, cancelled...)."""

    code = "ProposalNotEditable"


class DraftConflict(StudioError):
    """The request was made against another revision than the current one."""

    code = "DraftConflict"

    def __init__(self, revision: int) -> None:
        """Report the current revision so the client can reload."""
        super().__init__(
            f"Another change was applied first; the draft is now at revision {revision}.", revision=revision
        )


class RebaseRequired(StudioError):
    """A newer version was published after the draft was opened."""

    code = "RebaseRequired"

    def __init__(self, base_version: int | None, head_version: int | None) -> None:
        """Report both versions."""
        built_on = "no version" if base_version is None else f"v{base_version}"
        newest = "none" if head_version is None else f"v{head_version}"
        super().__init__(
            f"The draft was built on {built_on}, but the newest version is {newest}; rebase it first.",
            baseVersion=base_version,
            headVersion=head_version,
        )


class CommitGuardFailed(Exception):
    """An extra transaction item of a commit failed its condition; the revision did not become current.

    ``index`` is the item's position among the extra items, from 0.
    """

    def __init__(self, index: int) -> None:
        """Record which extra item failed."""
        super().__init__(f"Extra commit item {index} failed its condition")
        self.index = index


class StudioStateError(StudioError):
    """Stored designer state is damaged (a revision's files are missing); an operator must look."""

    status = 500
    code = "StudioStateError"


@dataclass(frozen=True)
class StudioState:
    """The ``studio`` field of a proposal row."""

    base_version: int | None
    revision: int
    ontology_id: str
    style: str
    applied: tuple[int, ...]
    redo: tuple[int, ...]
    counts: dict[str, int]
    opened_at: str
    opened_by: str
    updated_at: str
    updated_by: str


@dataclass(frozen=True)
class Revision:
    """One stored revision: its graphs and their canonical text (for inverse line deltas)."""

    number: int
    draft: Draft
    tbox_nt: str
    r2rml_nt: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _checked(namespace: str, proposal_id: str) -> None:
    validate_namespace_name(namespace)
    if not _PROPOSAL_ID.match(proposal_id):
        raise ValueError(f"Invalid proposal id {proposal_id!r}")


def prefix(namespace: str, proposal_id: str) -> str:
    """S3 prefix of a proposal's designer objects (inside the proposal's own prefix)."""
    _checked(namespace, proposal_id)
    return f"{dynamo_store._S3_PROPOSALS_PREFIX}/{namespace}/{proposal_id}/studio"


def revision_key(namespace: str, proposal_id: str, revision: int, name: str) -> str:
    """S3 key of one revision file: ``ontology.nt`` or ``r2rml.nt``."""
    if name not in ("ontology.nt", "r2rml.nt"):
        raise ValueError(f"Unknown revision file {name!r}")
    return f"{prefix(namespace, proposal_id)}/rev-{revision:06d}/{name}"


def record_key(namespace: str, proposal_id: str, revision: int) -> str:
    """S3 key of the change record of ``revision``."""
    return f"{prefix(namespace, proposal_id)}/ops/{revision:06d}.json"


def _put(key: str, body: str, content_type: str, *, if_absent: bool = False) -> bool:
    """Write an object; with ``if_absent`` return False instead of overwriting one that exists."""
    extra = {"IfNoneMatch": "*"} if if_absent else {}
    try:
        dynamo_store._get_s3().put_object(
            Bucket=dynamo_store._S3_BUCKET, Key=key, Body=body.encode("utf-8"), ContentType=content_type, **extra
        )
    except ClientError as e:
        if if_absent and e.response.get("Error", {}).get("Code") in _WRITE_CONFLICT_CODES:
            return False
        raise
    return True


def _get(key: str) -> str | None:
    try:
        obj = dynamo_store._get_s3().get_object(Bucket=dynamo_store._S3_BUCKET, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    body: bytes = obj["Body"].read()
    return body.decode("utf-8")


def _delete(keys: list[str]) -> None:
    for key in keys:
        # Best-effort: an orphan revision is harmless, nothing points at it.
        with contextlib.suppress(ClientError):
            dynamo_store._get_s3().delete_object(Bucket=dynamo_store._S3_BUCKET, Key=key)


def state_of(item: dict[str, Any]) -> StudioState | None:
    """The proposal row's designer state, or None when it was never opened in the designer."""
    raw = _plain(item.get("studio"))
    if not raw:
        return None
    return StudioState(
        base_version=raw.get("base_version"),
        revision=int(raw["revision"]),
        ontology_id=str(raw["ontology_id"]),
        style=str(raw.get("style") or iris.STRUCTURED),
        applied=tuple(int(n) for n in raw.get("applied") or []),
        redo=tuple(int(n) for n in raw.get("redo") or []),
        counts={k: int(v) for k, v in (raw.get("counts") or {}).items()},
        opened_at=str(raw.get("opened_at") or ""),
        opened_by=str(raw.get("opened_by") or ""),
        updated_at=str(raw.get("updated_at") or ""),
        updated_by=str(raw.get("updated_by") or ""),
    )


def get_proposal(
    namespace: str, proposal_id: str, *, hydrate_turtle: bool = False, consistent: bool = False
) -> dict[str, Any]:
    """The proposal row, or :class:`ProposalNotFound`; ``consistent`` reads it strongly consistently."""
    _checked(namespace, proposal_id)
    item = dynamo_store.get_proposal_by_id(
        proposal_id,
        namespace=namespace,
        hydrate_turtle=hydrate_turtle,
        hydrate_matches=False,
        hydrate_constraints=hydrate_turtle,
        consistent=consistent,
    )
    if not item:
        raise ProposalNotFound(f"Proposal '{proposal_id}' not found")
    return item


def require_state(item: dict[str, Any], *, editable: bool) -> StudioState:
    """The designer state of ``item``; with ``editable``, also refuse a proposal whose status forbids edits."""
    state = state_of(item)
    if state is None:
        raise NotOpen("This proposal is not open in the Ontology designer")
    if editable and item.get("status") not in EDITABLE_STATUSES:
        raise NotEditable(
            f"The proposal is {item.get('status')}; its draft can no longer change", status=item.get("status")
        )
    return state


def scheme(namespace: str, state: StudioState) -> iris.IriScheme:
    """The IRI scheme the draft's ops use."""
    return iris.IriScheme(
        namespace=namespace,
        ontology_prefix=iris.scheme_for(namespace, state.ontology_id, None).ontology_prefix,
        style=state.style,
    )


def counts(d: Draft) -> dict[str, int]:
    """Term counts stored on the row so ``GET .../studio`` needs no S3 read."""
    classes, obj_props, dt_props = schema_subjects(d.tbox)
    return {
        "classes": len(classes),
        "objectProperties": len(obj_props),
        "datatypeProperties": len(dt_props),
        "triplesMaps": len(snapshot._triples_maps(d.r2rml)),
    }


@functools.lru_cache(maxsize=4)
def _cached_revision(namespace: str, proposal_id: str, revision: int) -> Revision:
    tbox_nt = _get(revision_key(namespace, proposal_id, revision, "ontology.nt"))
    r2rml_nt = _get(revision_key(namespace, proposal_id, revision, "r2rml.nt"))
    if tbox_nt is None or r2rml_nt is None:
        raise StudioStateError(f"The files of draft revision {revision} are missing")
    return Revision(revision, Draft(parse_nt(tbox_nt), parse_nt(r2rml_nt)), tbox_nt, r2rml_nt)


def load_revision(namespace: str, proposal_id: str, revision: int) -> Revision:
    """Read one revision. Revisions are immutable, so a few are cached; never mutate ``draft`` (copy it)."""
    return _cached_revision(namespace, proposal_id, revision)


def clear_cache() -> None:
    """Forget cached revisions (tests that reuse proposal ids call this)."""
    _cached_revision.cache_clear()


def revision_text(namespace: str, proposal_id: str, revision: int) -> tuple[str, str]:
    """The canonical N-Triples of a revision's ontology and mapping."""
    rev = load_revision(namespace, proposal_id, revision)
    return rev.tbox_nt, rev.r2rml_nt


def get_record(namespace: str, proposal_id: str, revision: int) -> dict[str, Any]:
    """The change record of ``revision``."""
    text = _get(record_key(namespace, proposal_id, revision))
    if text is None:
        raise StudioStateError(f"The change record of draft revision {revision} is missing")
    record: dict[str, Any] = json.loads(text)
    return record


def claim_and_write(
    namespace: str, proposal_id: str, start: int, record: dict[str, Any], tbox_nt: str, r2rml_nt: str
) -> int:
    """Claim the first free revision number from ``start``, write its record and files, and return it.

    The claim is the record put with ``If-None-Match: *``. A number taken by another writer, live
    or dead, is skipped; nothing is written to a number this call did not claim.
    """
    for n in range(start, start + MAX_CLAIM_ATTEMPTS):
        body = json.dumps({**record, "revision": n, "formatVersion": FORMAT_VERSION}, sort_keys=True)
        if _put(record_key(namespace, proposal_id, n), body, "application/json", if_absent=True):
            _put(revision_key(namespace, proposal_id, n, "ontology.nt"), tbox_nt, "application/n-triples")
            _put(revision_key(namespace, proposal_id, n, "r2rml.nt"), r2rml_nt, "application/n-triples")
            return n
    raise StudioError(f"Could not claim a draft revision after {MAX_CLAIM_ATTEMPTS} attempts; try again")


def discard(namespace: str, proposal_id: str, revision: int) -> None:
    """Delete a claimed revision that never became current and was never referenced (a lost open).

    The record is the claim, so it goes last: while it exists no other writer can take the number,
    and once it is gone nothing of this revision is left to delete under a new owner.
    """
    _delete(
        [
            revision_key(namespace, proposal_id, revision, "ontology.nt"),
            revision_key(namespace, proposal_id, revision, "r2rml.nt"),
            record_key(namespace, proposal_id, revision),
        ]
    )


def _key(namespace: str, proposal_id: str) -> dict[str, str]:
    return {"PK": dynamo_store._proposal_pk(namespace, proposal_id), "SK": "META"}


def _conditional_failed(e: ClientError) -> bool:
    return e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def commit(
    namespace: str,
    proposal_id: str,
    *,
    expected: int,
    revision: int,
    applied: tuple[int, ...],
    redo: tuple[int, ...],
    counts_: dict[str, int],
    author: str,
    base_version: int | None = None,
    rebased: bool = False,
    also_ops: Sequence[tuple[str, dict[str, Any]]] = (),
) -> None:
    """Make ``revision`` current if the draft is still at ``expected`` and the proposal still editable.

    ``also_ops`` are more DynamoDB transaction items (``(kind, args)`` pairs, as
    ``abox.store._transact`` takes them) that must succeed with the update: then all of them
    and the update go as one transaction. When one of them fails its condition while the draft
    condition held, :class:`CommitGuardFailed` is raised and the revision stays a gap.

    On a lost race the matching :class:`StudioError` is raised and the claimed revision is left in
    place as an unreferenced gap (see the module docstring). It is never deleted here, because a
    revision that looks lost may be an ancestor of the current one: a write that landed while its
    response was lost, followed by another writer's commit. When the failed condition is only
    botocore retrying a write that had already landed (the row now says ``revision``), the commit
    succeeded and nothing is raised.
    """
    now = _now()
    names = {"#st": "studio", "#rev": "revision", "#ap": "applied", "#rd": "redo", "#ct": "counts"}
    names |= {"#ua": "updated_at", "#ub": "updated_by", "#s": "status"}
    values: dict[str, Any] = {
        ":n": revision,
        ":exp": expected,
        ":ap": list(applied),
        ":rd": list(redo),
        ":ct": counts_,
        ":now": now,
        ":by": author,
        ":updated": "updated",
        ":p": EDITABLE_STATUSES[0],
        ":u": EDITABLE_STATUSES[1],
        ":af": EDITABLE_STATUSES[2],
    }
    sets = [
        "#st.#rev = :n",
        "#st.#ap = :ap",
        "#st.#rd = :rd",
        "#st.#ct = :ct",
        "#st.#ua = :now",
        "#st.#ub = :by",
        "#s = :updated",
        "#ua = :now",
    ]
    if rebased:
        names["#bv"] = "base_version"
        values[":bv"] = base_version
        sets.append("#st.#bv = :bv")
    update: dict[str, Any] = {
        "Key": _key(namespace, proposal_id),
        "UpdateExpression": "SET " + ", ".join(sets),
        "ConditionExpression": "#st.#rev = :exp AND #s IN (:p, :u, :af)",
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }
    try:
        if also_ops:
            abox_store._transact([("Update", update), *also_ops])
        else:
            dynamo_store._get_table().update_item(**update)
    except (ClientError, abox_store._TransactionRejected) as e:
        if isinstance(e, ClientError) and not _conditional_failed(e):
            raise
        item = get_proposal(namespace, proposal_id)
        current = state_of(item)
        if current is not None and current.revision == revision:
            return  # botocore retried a write that had landed; only this writer holds ``revision``
        if isinstance(e, abox_store._TransactionRejected) and not e.failed[0]:
            raise CommitGuardFailed(e.failed.index(True, 1) - 1) from e
        raise DraftConflict(require_state(item, editable=True).revision) from e


def merged_base(
    namespace: str, proposal_id: str, item: dict[str, Any], *, ontology_id: str, created_by: str
) -> tuple[int | None, Draft]:
    """Merge the proposal onto the newest version, as a plain accept would publish it.

    Opening a draft and rebasing one both build their base here, so they cannot drift apart.

    ``item`` is the proposal row read with ``hydrate_turtle=True`` (see :func:`get_proposal`).
    In a namespace that predates versioning the live ontology is first captured as v1, so the
    result builds on it. ``created_by`` goes onto the publish request only; nothing is written.

    Returns ``(base_version, candidate)``. ``base_version`` is the version the candidate was
    merged onto (None when the namespace has no version). ``candidate`` is a new :class:`Draft`
    holding the ontology and mapping that ``publish.union_candidate`` returns for it; the caller
    owns it.

    Raises :class:`StudioError` when the namespace publishes versions of another ontology than
    ``ontology_id``, when the proposal holds no ontology, or when it cannot be merged onto that
    version (:class:`SnapshotValidationError`, wrapped). Errors of ``bootstrap_if_needed`` and
    ``publish_request_for`` propagate unchanged.
    """
    from coa_ontology import proposals

    head = registry.head_version(namespace)
    if head is None:
        version_publish.bootstrap_if_needed(namespace, ontology_id)
        head = registry.head_version(namespace)
    if head and head["ontology_id"] != ontology_id:
        raise StudioError(
            f"Namespace '{namespace}' publishes versions of ontology '{head['ontology_id']}', "
            f"not '{ontology_id}'; re-induce under the namespace's ontology prefix"
        )
    base = int(head["version"]) if head else None
    req = proposals.publish_request_for(item, namespace, proposal_id, ontology_id, created_by)
    if not req.ontology_turtle:
        raise StudioError("The proposal has no ontology to build a draft from")
    try:
        tbox, r2rml = version_publish.union_candidate(req, base)
    except snapshot.SnapshotValidationError as e:
        raise StudioError(
            f"The proposal cannot be merged onto {'no version' if base is None else f'v{base}'}: {e}"
        ) from e
    return base, Draft(tbox, r2rml)


def open_draft(namespace: str, proposal_id: str, opened_by: str) -> StudioState:
    """Create the proposal's draft (revision 0) from the newest version and the proposal; idempotent.

    The draft is what a plain accept would publish (:func:`merged_base`). A draft that already
    exists is returned as it is, whatever the proposal's status; the status decides only whether
    a new draft may be opened.
    """
    item = get_proposal(namespace, proposal_id, hydrate_turtle=True)
    existing = state_of(item)
    if existing is not None:
        return existing
    if item.get("status") not in EDITABLE_STATUSES:
        raise NotEditable(
            f"A {item.get('status')} proposal cannot be opened in the Ontology designer", status=item.get("status")
        )
    ontology_id = str(item.get("ontology_id") or "")
    if not ontology_id:
        raise StudioError("The proposal has no ontology id")
    base, d = merged_base(namespace, proposal_id, item, ontology_id=ontology_id, created_by=opened_by)
    tbox_nt, r2rml_nt = snapshot.canonical_nt(d.tbox), snapshot.canonical_nt(d.r2rml)
    now = _now()
    line = message("OPENED_DRAFT_NO_VERSION") if base is None else message("OPENED_DRAFT", version=base)
    record = {
        "baseRevision": None,
        "kind": "open",
        "source": "user",
        "author": opened_by,
        "rationale": None,
        "at": now,
        "baseVersion": base,
        "ops": [],
        "inverse": None,
        "summary": [english(line)],
        "summaryMessages": [line],
        "warnings": [],
        "warningMessages": [],
    }
    revision = claim_and_write(namespace, proposal_id, 0, record, tbox_nt, r2rml_nt)
    style = iris.DOCUMENT if item.get("source_type") == "UNSTRUCTURED" else iris.STRUCTURED
    studio: dict[str, Any] = {
        "base_version": base,
        "revision": revision,
        "ontology_id": ontology_id,
        "style": style,
        "applied": [],
        "redo": [],
        "counts": counts(d),
        "opened_at": now,
        "opened_by": opened_by,
        "updated_at": now,
        "updated_by": opened_by,
    }
    try:
        dynamo_store._get_table().update_item(
            Key=_key(namespace, proposal_id),
            UpdateExpression="SET #st = :studio, #u = :now",
            ConditionExpression="attribute_not_exists(#st) AND #s IN (:p, :up, :af)",
            ExpressionAttributeNames={"#st": "studio", "#s": "status", "#u": "updated_at"},
            ExpressionAttributeValues={
                ":studio": studio,
                ":now": now,
                ":p": EDITABLE_STATUSES[0],
                ":up": EDITABLE_STATUSES[1],
                ":af": EDITABLE_STATUSES[2],
            },
        )
    except ClientError as e:
        if not _conditional_failed(e):
            raise
        again = get_proposal(namespace, proposal_id)
        winner = state_of(again)
        if winner is not None and winner.opened_at == now:
            return winner  # botocore retried a write that had landed; only this call wrote that ``opened_at``
        discard(namespace, proposal_id, revision)
        if winner is not None:
            return winner  # a concurrent open won
        raise NotEditable(f"The proposal is {again.get('status')}", status=again.get("status")) from e
    state = state_of({"studio": studio})
    if state is None:  # pragma: no cover - studio is set just above
        raise StudioStateError("Opened draft has no state")
    return state


def head_version(namespace: str) -> int | None:
    """The newest published version a publish would build on now (None when there is none)."""
    head = registry.head_version(namespace)
    return int(head["version"]) if head else None
