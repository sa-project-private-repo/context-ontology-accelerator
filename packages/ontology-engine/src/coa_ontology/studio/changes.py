# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Change sets, dry runs, history, undo and redo of a designer draft.

Every write names the revision it was made against. A change set is applied to that revision as
a whole, its inverse is the canonical N-Triples line delta back to it (``restore_triples``), and
the result becomes the next revision. ``studio.applied`` lists the revisions whose change sets are
in effect (the undo stack); ``studio.redo`` the undone ones that can be re-applied. Every write is
refused unless the proposal is still in review (``draft.EDITABLE_STATUSES``).

Each revision ``n`` has a change record, ``ops/{n:06d}.json``, written as JSON. Every record, the
open record of ``draft.open_draft`` included, has these keys:

- ``formatVersion`` and ``revision`` (``n``);
- ``baseRevision``: the revision it was built on (None for the open record). The history follows
  these links back from ``studio.revision``, so a gap left by a writer that lost or died never
  appears in it;
- ``kind``: ``open``, ``apply``, ``undo``, ``redo`` or ``rebase``;
- ``source``: ``"user"`` for a direct edit, ``"agent"`` for a change set the agent proposed (``changesets``);
  and ``author``, who wrote it;
- ``rationale``: the reason given with a change set. Direct edits have none, so it is None;
- ``at``: the time of the write, and ``baseVersion``: the version the draft was built on;
- ``ops``: the ops of the change set, resolved: an ``add_class``, ``add_object_property`` or
  ``add_datatype_property`` op carries the ``iri`` it minted, even when the client left it out. A redo
  or a rebase replays these ops, so they create that very term, or fail when the IRI is taken, and
  never mint another IRI that later ops would not name (none for the open record);
- ``inverse``: a ``restore_triples`` op dump that undoes it (None for the open record);
- ``summary`` and ``warnings``: lines of text.

Records written since the history's sentences got codes also have ``summaryMessages`` and
``warningMessages``: the code and the parameters of each line of ``summary`` and of ``warnings``, in
the same order (``messages``). An older record has neither, and the history then gives only its
English lines.

The records written here add ``tboxSha256`` and ``r2rmlSha256``, the SHA-256 of the revision's
canonical text (the open record has no hashes). An undo or a redo adds ``target``, the revision whose
change set it reverses or repeats; a rebase adds ``skipped`` and ``fromVersion``; an applied agent
change set adds ``changeSetId`` and ``opIndexes``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from coa_ontology.versions import snapshot

from . import draft as store
from .messages import Message, english, listed, message
from .ops import (
    ANY_OPS,
    USER_OPS,
    AddClass,
    AddDatatypeProperty,
    AddObjectProperty,
    ChangeSetError,
    Context,
    Draft,
    RestoreTriples,
    apply_ops,
    line_delta,
)

MAX_OPS = 100
MAX_HISTORY = 100
ADD_OPS = (AddClass, AddObjectProperty, AddDatatypeProperty)


class InvalidChange(store.StudioError):
    """A change set that cannot be applied to the current revision; nothing changed."""

    code = "InvalidChange"


class InvalidHistoryStart(ValueError):
    """A history page asked to start at a revision that is not on the draft's history."""

    def __init__(self, start: int) -> None:
        """Name the refused start revision."""
        super().__init__(f"Revision {start} is not in the draft's history")
        self.start = start


class NothingToUndo(store.StudioError):
    """No change set is in effect."""

    code = "NothingToUndo"


class NothingToRedo(store.StudioError):
    """Nothing was undone since the last change."""

    code = "NothingToRedo"


def _editable(namespace: str, proposal_id: str, expected: int) -> store.StudioState:
    """The draft's state, if it is open, still editable and at revision ``expected``."""
    state = store.require_state(store.get_proposal(namespace, proposal_id), editable=True)
    if state.revision != expected:
        raise store.DraftConflict(state.revision)
    return state


def _apply(
    before: store.Revision, ops: Sequence[Any], state: store.StudioState, namespace: str
) -> tuple[Draft, Context]:
    """Apply ``ops`` to a copy of ``before`` (the revision cache is shared, so its draft is never edited)."""
    try:
        return apply_ops(before.draft, ops, store.scheme(namespace, state))
    except ChangeSetError as e:
        raise InvalidChange(str(e), opIndex=e.index) from e


def _resolved(ops: Sequence[Any], created: Sequence[str]) -> list[dict[str, Any]]:
    """``ops`` as stored: each add op has the IRI it minted (``created`` lists them in op order)."""
    minted = iter(created)
    out = []
    for op in ops:
        dump = op.model_dump(by_alias=True, exclude_none=True)
        if isinstance(op, ADD_OPS):
            dump["iri"] = next(minted)
        out.append(dump)
    return out


def _inverse(base_nt: tuple[str, str], tbox_nt: str, r2rml_nt: str) -> dict[str, Any]:
    inverse = RestoreTriples(
        op="restore_triples", tbox=line_delta(base_nt[0], tbox_nt), r2rml=line_delta(base_nt[1], r2rml_nt)
    )
    return inverse.model_dump(by_alias=True)


def write_revision(
    namespace: str,
    proposal_id: str,
    state: store.StudioState,
    *,
    kind: str,
    after: Draft,
    ops: list[dict[str, Any]],
    inverse_base: tuple[str, str],
    keep_applied: tuple[int, ...],
    push: bool,
    redo: tuple[int, ...],
    author: str,
    summary: list[str],
    warnings: list[str],
    extra: dict[str, Any] | None = None,
    base_version: int | None = None,
    rebased: bool = False,
    source: str = "user",
    rationale: str | None = None,
    also: Callable[[int], Sequence[tuple[str, dict[str, Any]]]] | None = None,
    summary_messages: list[Message] | None = None,
    warning_messages: list[Message] | None = None,
) -> int:
    """Store ``after`` as the next revision and make it current; return its number.

    The record is built on ``state.revision`` (``baseRevision``). The new ``studio.applied`` is
    ``keep_applied``, plus the new revision itself when ``push`` (its change set is then undoable).
    ``inverse_base`` is the canonical text the inverse restores. ``extra`` adds kind-specific keys
    (see the module docstring); it cannot replace the others. ``base_version`` is only used when
    ``rebased``, which moves the draft onto it. ``source`` and ``rationale`` go into the record, and
    so do ``summary_messages`` and ``warning_messages``, the coded lines of ``summary`` and
    ``warnings``, unless they are None.
    ``also`` gets the claimed revision number and returns DynamoDB transaction items that commit
    together with it (``draft.commit``). Raises :class:`DraftConflict` when another write made a
    revision current first, and ``draft.CommitGuardFailed`` when an ``also`` item failed; the
    number this call claimed then stays as an unreferenced gap.
    """
    tbox_nt, r2rml_nt = snapshot.canonical_nt(after.tbox), snapshot.canonical_nt(after.r2rml)
    record = {
        **(extra or {}),
        "baseRevision": state.revision,
        "kind": kind,
        "source": source,
        "author": author,
        "rationale": rationale,
        "at": datetime.now(UTC).isoformat(),
        "baseVersion": base_version if rebased else state.base_version,
        "ops": ops,
        "inverse": _inverse(inverse_base, tbox_nt, r2rml_nt),
        "summary": summary,
        **({"summaryMessages": summary_messages} if summary_messages is not None else {}),
        "warnings": warnings,
        **({"warningMessages": warning_messages} if warning_messages is not None else {}),
        "tboxSha256": snapshot.sha256_hex(tbox_nt),
        "r2rmlSha256": snapshot.sha256_hex(r2rml_nt),
    }
    revision = store.claim_and_write(namespace, proposal_id, state.revision + 1, record, tbox_nt, r2rml_nt)
    store.commit(
        namespace,
        proposal_id,
        expected=state.revision,
        revision=revision,
        applied=(*keep_applied, revision) if push else keep_applied,
        redo=redo,
        counts_=store.counts(after),
        author=author,
        base_version=base_version,
        rebased=rebased,
        also_ops=also(revision) if also else (),
    )
    return revision


def _result(
    revision: int, *, dry_run: bool, before: Draft, after: Draft, ctx: Context, can_undo: bool, can_redo: bool
) -> dict[str, Any]:
    return {
        "revision": revision,
        "dryRun": dry_run,
        "warnings": list(ctx.warnings),
        "warningMessages": listed(ctx.warning_messages),
        "created": list(ctx.created),
        "diff": snapshot.compute_diff(before.tbox, after.tbox),
        "canUndo": can_undo,
        "canRedo": can_redo,
    }


def apply_change_set(
    namespace: str,
    proposal_id: str,
    raw_ops: list[dict[str, Any]],
    expected_revision: int,
    *,
    author: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a user change set atomically; with ``dry_run`` compute the result and store nothing.

    ``raw_ops`` come from a client, so they are parsed as user ops (never ``restore_triples``); a
    malformed op raises pydantic's ``ValidationError``, an op that does not apply to the draft
    :class:`InvalidChange` with the failing op's ``opIndex``.
    """
    if not raw_ops:
        raise InvalidChange("A change set needs at least one op")
    if len(raw_ops) > MAX_OPS:
        raise InvalidChange(f"A change set has at most {MAX_OPS} ops")
    ops = USER_OPS.validate_python(raw_ops)
    state = _editable(namespace, proposal_id, expected_revision)
    before = store.load_revision(namespace, proposal_id, state.revision)
    after, ctx = _apply(before, ops, state, namespace)
    if dry_run:
        return _result(
            state.revision,
            dry_run=True,
            before=before.draft,
            after=after,
            ctx=ctx,
            can_undo=bool(state.applied),
            can_redo=bool(state.redo),
        )
    revision = write_revision(
        namespace,
        proposal_id,
        state,
        kind="apply",
        after=after,
        ops=_resolved(ops, ctx.created),
        inverse_base=(before.tbox_nt, before.r2rml_nt),
        keep_applied=state.applied,
        push=True,
        redo=(),
        author=author,
        summary=[english(m) for m in ctx.summary_messages],
        warnings=list(ctx.warnings),
        summary_messages=list(ctx.summary_messages),
        warning_messages=list(ctx.warning_messages),
    )
    return _result(revision, dry_run=False, before=before.draft, after=after, ctx=ctx, can_undo=True, can_redo=False)


def undo(namespace: str, proposal_id: str, expected_revision: int, *, author: str) -> dict[str, Any]:
    """Apply the inverse of the newest change set in effect as a new revision."""
    state = _editable(namespace, proposal_id, expected_revision)
    if not state.applied:
        raise NothingToUndo("There is no change to undo")
    target = state.applied[-1]
    record = store.get_record(namespace, proposal_id, target)
    inverse = ANY_OPS.validate_python([record["inverse"]])
    before = store.load_revision(namespace, proposal_id, state.revision)
    after, ctx = _apply(before, inverse, state, namespace)
    line = message("UNDID_REVISION", revision=target)
    revision = write_revision(
        namespace,
        proposal_id,
        state,
        kind="undo",
        after=after,
        ops=[record["inverse"]],
        inverse_base=(before.tbox_nt, before.r2rml_nt),
        keep_applied=state.applied[:-1],
        push=False,
        redo=(*state.redo, target),
        author=author,
        summary=[english(line)],
        warnings=[],
        extra={"target": target},
        summary_messages=[line],
        warning_messages=[],
    )
    return _result(
        revision,
        dry_run=False,
        before=before.draft,
        after=after,
        ctx=ctx,
        can_undo=len(state.applied) > 1,
        can_redo=True,
    )


def redo(namespace: str, proposal_id: str, expected_revision: int, *, author: str) -> dict[str, Any]:
    """Re-apply the newest undone change set as a new revision."""
    state = _editable(namespace, proposal_id, expected_revision)
    if not state.redo:
        raise NothingToRedo("There is no undone change to redo")
    target = state.redo[-1]
    record = store.get_record(namespace, proposal_id, target)
    ops = ANY_OPS.validate_python(record["ops"])
    before = store.load_revision(namespace, proposal_id, state.revision)
    after, ctx = _apply(before, ops, state, namespace)
    revision = write_revision(
        namespace,
        proposal_id,
        state,
        kind="redo",
        after=after,
        ops=record["ops"],
        inverse_base=(before.tbox_nt, before.r2rml_nt),
        keep_applied=state.applied,
        push=True,
        redo=state.redo[:-1],
        author=author,
        summary=list(record.get("summary") or []),
        warnings=list(ctx.warnings),
        extra={"target": target},
        summary_messages=record.get("summaryMessages"),  # None for a record from before the codes
        warning_messages=list(ctx.warning_messages),
    )
    return _result(
        revision,
        dry_run=False,
        before=before.draft,
        after=after,
        ctx=ctx,
        can_undo=True,
        can_redo=len(state.redo) > 1,
    )


def history(namespace: str, proposal_id: str, limit: int = 50, start: int | None = None) -> dict[str, Any]:
    """One page of the draft's revisions, newest first: at most ``limit``, from ``start`` (default the current one).

    The walk starts at ``start`` and follows each record's ``baseRevision``, so a number claimed by a
    writer that lost or died (a gap) is never listed. ``next`` is the revision the next page starts
    at, absent on the last page. A ``start`` past the current revision, or one that has no record, is
    refused with :class:`InvalidHistoryStart`.
    """
    state = store.require_state(store.get_proposal(namespace, proposal_id), editable=False)
    if start is not None and start > state.revision:
        raise InvalidHistoryStart(start)
    entries: list[dict[str, Any]] = []
    revision: int | None = state.revision if start is None else start
    while revision is not None and len(entries) < min(limit, MAX_HISTORY):
        try:
            record = store.get_record(namespace, proposal_id, revision)
        except store.StudioStateError:
            if revision == start:  # a resume point the walk never reaches
                raise InvalidHistoryStart(start) from None
            raise
        entries.append(
            {
                "revision": revision,
                "kind": record["kind"],
                "source": record.get("source") or "user",
                "rationale": record.get("rationale"),
                "changeSetId": record.get("changeSetId"),
                "author": record.get("author"),
                "at": record.get("at"),
                "summary": record.get("summary") or [],
                "summaryMessages": listed(record.get("summaryMessages")),
                "opCount": len(record.get("ops") or []),
                "target": record.get("target"),
                "warnings": record.get("warnings") or [],
                "warningMessages": listed(record.get("warningMessages")),
                "active": revision in state.applied,
                "undone": revision in state.redo,
            }
        )
        revision = record.get("baseRevision")
    return {
        "revision": state.revision,
        "entries": entries,
        "next": revision,
        "canUndo": bool(state.applied),
        "canRedo": bool(state.redo),
    }
