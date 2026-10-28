# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Rebase a designer draft onto the newest version.

The proposal is merged onto the new head again, exactly as opening does (``draft.merged_base``), and
the ops of the change sets in effect (``studio.applied``, oldest first) are replayed onto it. Inverses
and undone change sets are not replayed. The stored ops are resolved (see ``changes``): an op that
adds a term names the IRI it minted, so on a head that defines that IRI already it fails; it is never
given another IRI. When every op applies, the result is one new revision whose change set holds all
replayed ops; otherwise nothing changes and the failing ops are reported by id
``"{revision}:{index}"`` so the caller can retry with ``skipOps``.

The term an op introduces cannot be trusted to exist in the replay when that op is skipped, when it
fails, or when it depends on one that did not make it (``iri`` of an add, ``newIri`` of ``change_iri``).
An op after it that names such an IRI would act on whatever the new head has under that IRI, so it is
reported as a failed dependant, with the op at the root of the chain, and is not replayed. The report
of a first try is thus complete, and the caller skips each root with its dependants.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from coa_ontology.versions import snapshot

from . import changes
from . import draft as store
from .messages import Message, english, message
from .ops import ANY_OPS, ChangeIri, ChangeSetError, apply_ops


class NothingToRebase(store.StudioError):
    """The draft is already built on the newest version."""

    code = "NothingToRebase"


class RebaseFailed(store.StudioError):
    """Some ops do not apply to the new base; the draft is unchanged."""

    code = "RebaseFailed"


def _version(version: int | None) -> str:
    return "no version" if version is None else f"v{version}"


# The free-text fields of a stored op. Every other field holds an IRI (or a fixed word such as ``kind``).
_TEXT_FIELDS = frozenset({"op", "label", "comment", "lang"})


def _introduced(op: Any, raw: dict[str, Any]) -> str | None:
    """The IRI ``op`` brings into the draft: the term an add creates, or the IRI ``change_iri`` renames to."""
    if isinstance(op, changes.ADD_OPS):
        return raw.get("iri")
    if isinstance(op, ChangeIri):
        return raw.get("newIri")
    return None


def _named(raw: dict[str, Any]) -> Iterator[str]:
    """The IRIs a stored op names: the strings of its non-text fields, a list field (``parents``) flattened."""
    for key, value in raw.items():
        if key in _TEXT_FIELDS:
            continue
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            yield from (v for v in value if isinstance(v, str))


def rebase(
    namespace: str, proposal_id: str, expected_revision: int, skip_ops: list[str], *, author: str
) -> dict[str, Any]:
    """Rebuild the draft on the newest version and replay its user ops; see the module docstring.

    ``skip_ops`` are op ids (``"{revision}:{index}"``) of the change sets in effect, which are left
    out of the replay. Raises :class:`NothingToRebase` when the draft already is on the newest
    version, :class:`RebaseFailed` (``extra["failures"]`` and ``extra["revision"]``) when ops do
    not apply, ``changes.InvalidChange`` for an unknown op id, and :class:`StudioError` when the
    proposal cannot be merged onto the newest version (``draft.merged_base``). Like every write,
    it refuses a proposal that left review (:class:`NotEditable`) and a stale ``expected_revision``
    (:class:`DraftConflict`).
    """
    item = store.get_proposal(namespace, proposal_id, hydrate_turtle=True)
    state = store.require_state(item, editable=True)
    if state.revision != expected_revision:
        raise store.DraftConflict(state.revision)
    head_version, fresh = store.merged_base(
        namespace, proposal_id, item, ontology_id=state.ontology_id, created_by=author
    )
    if head_version == state.base_version:
        raise NothingToRebase(f"The draft is already built on {_version(head_version)}")
    fresh_nt = (snapshot.canonical_nt(fresh.tbox), snapshot.canonical_nt(fresh.r2rml))

    records = [store.get_record(namespace, proposal_id, n) for n in state.applied]
    stored: list[tuple[str, dict[str, Any], Any]] = []  # (op id, the op as stored, the parsed op), oldest first
    for rec in records:
        raw_ops = rec.get("ops") or []
        for i, (raw, op) in enumerate(zip(raw_ops, ANY_OPS.validate_python(raw_ops), strict=True)):
            stored.append((f"{rec['revision']}:{i}", raw, op))
    skip = set(skip_ops)
    unknown = sorted(skip - {op_id for op_id, _, _ in stored})
    if unknown:
        raise changes.InvalidChange(f"Unknown op ids in skipOps: {', '.join(unknown)}")
    skipped = [op_id for op_id, _, _ in stored if op_id in skip]  # in the order of the replay
    scheme = store.scheme(namespace, state)
    current = fresh
    replayed: list[dict[str, Any]] = []
    summary: list[Message] = []
    failures: list[dict[str, Any]] = []
    absent: dict[str, str] = {}  # an IRI the replay may not have -> the skipped or failed op at the root of that
    for op_id, raw, op in stored:
        introduced = _introduced(op, raw)
        blocker = op_id if op_id in skip else None
        if blocker is None:
            blocker = next((absent[v] for v in _named(raw) if v in absent and v != introduced), None)
            if blocker is not None:
                kind = "skipped" if blocker in skip else "failed"
                failures.append({"opId": op_id, "op": raw, "error": f"depends on {kind} op {blocker}"})
            else:
                try:
                    current, ctx = apply_ops(current, [op], scheme)
                except ChangeSetError as e:
                    failures.append({"opId": op_id, "op": raw, "error": e.message})
                    blocker = op_id
                else:
                    summary.extend(ctx.summary_messages)  # naming terms as the replay labels them
        if blocker is None:
            if introduced:
                absent.pop(introduced, None)  # a replayed op put the term there: later ops may name it
            replayed.append(raw)
        elif introduced:
            absent[introduced] = blocker
    if failures:
        raise RebaseFailed(
            f"{len(failures)} op(s) do not apply to {_version(head_version)}; skip them to rebase",
            failures=failures,
            revision=state.revision,
        )
    line = message("REBASED_NO_VERSION") if head_version is None else message("REBASED", version=head_version)
    revision = changes.write_revision(
        namespace,
        proposal_id,
        state,
        kind="rebase",
        after=current,
        ops=replayed,
        inverse_base=fresh_nt,
        keep_applied=(),
        push=bool(replayed),
        redo=(),
        author=author,
        summary=[english(m) for m in (line, *summary)],
        warnings=[],
        extra={"skipped": skipped, "fromVersion": state.base_version},
        base_version=head_version,
        rebased=True,
        summary_messages=[line, *summary],
        warning_messages=[],
    )
    return {
        "revision": revision,
        "baseVersion": head_version,
        "replayed": len(replayed),
        "skipped": skipped,
        "canUndo": bool(replayed),
        "canRedo": False,
    }
