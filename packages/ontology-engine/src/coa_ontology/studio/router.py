# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Ontology designer routes, mounted at ``/ontology/proposals/{proposal_id}/studio``.

The draft and its typed change sets, the chat with the assistant (``/messages``, ``/events``) and
the change sets it proposes (``/changesets``).

Errors are ``HTTPException`` with ``detail = {"code", "message", ...}``; the web client reads
``code`` (``DraftConflict``, ``InvalidChange``, ``RebaseFailed``...). Every write takes the
``expectedRevision`` it was made against and answers with the new ``revision``. The author of a
write is the principal the API proxy forwards in ``X-Coa-Principal`` (``"unknown"`` without one).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal

from coa_common import validate_namespace_name
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

from coa_ontology.catalog.ingest import schema_subjects
from coa_ontology.pagination import InvalidNextToken, decode_next_token, page_body
from coa_ontology.versions import deploy as version_deploy
from coa_ontology.versions import principal, snapshot, storage

from . import agent, changes, changesets, chat_store, checks, graph_view, queries, questions, rebase
from . import draft as store
from .iris import MAX_IRI_LENGTH, IriError, check_iri

_log = logging.getLogger("studio.router")
# What the accept preview says when the governed metrics could not be listed; the details are only logged.
BREAKING_METRICS_ERROR = "Breaking metrics could not be checked; the graph database may be unreachable."


def _checked_namespace(namespace: str = "default") -> str:
    try:
        validate_namespace_name(namespace)
    except ValueError as e:
        raise HTTPException(422, {"code": "InvalidNamespace", "message": str(e)}) from e
    return namespace


def _checked_proposal_id(proposal_id: str, namespace: str = "default") -> str:
    """Refuse a malformed proposal id up front, so no other ``ValueError`` needs mapping to a 422."""
    try:
        store._checked(namespace, proposal_id)
    except ValueError as e:
        raise HTTPException(422, {"code": "InvalidRequest", "message": str(e)}) from e
    return proposal_id


LIST_HISTORY_MAX_RESULTS = changes.MAX_HISTORY
LIST_HISTORY_DEFAULT_MAX_RESULTS = 50

router = APIRouter(dependencies=[Depends(_checked_namespace), Depends(_checked_proposal_id)])


def _invalid_change(e: ValidationError) -> dict[str, Any]:
    """The 422 body for ops that do not parse: the first problem, with the index of its op.

    pydantic reports where an op failed as ``loc = (op index, op name, field)``. The rejected input
    is never echoed.
    """
    first = e.errors(include_url=False, include_context=False, include_input=False)[0]
    loc = first["loc"]
    detail: dict[str, Any] = {}
    where = ""
    if loc and isinstance(loc[0], int):
        detail["opIndex"] = loc[0]
        where = f"Op {loc[0]}" + (f" ({'.'.join(str(p) for p in loc[1:])})" if len(loc) > 1 else "") + ": "
    return {"code": "InvalidChange", "message": f"{where}{first['msg']}", **detail}


@contextmanager
def _http(*, client_ops: bool = False) -> Iterator[None]:
    """Turn designer errors into HTTP errors with a structured ``detail``.

    ``client_ops`` is set only by the route whose body holds ops to parse. Only there is a pydantic
    ``ValidationError`` the client's fault (422); anywhere else it means a stored record does not
    parse, a server fault. Any other exception, a ``ValueError`` from a corrupt record included,
    stays a 500 too.
    """
    try:
        yield
    except store.StudioError as e:
        raise HTTPException(e.status, e.detail()) from e
    except ValidationError as e:
        if not client_ops:
            raise
        raise HTTPException(422, _invalid_change(e)) from e
    except IriError as e:
        raise HTTPException(422, {"code": "InvalidIri", "message": str(e)}) from e


def _call[T](fn: Callable[[], T], *, client_ops: bool = False) -> T:
    with _http(client_ops=client_ops):
        return fn()


# The model's bounds: ``StudioRevision``, ``StudioOpList``, ``StudioOpIdList`` and ``StudioOpId``.
MAX_CHANGE_SET_BYTES = 2 * 1024 * 1024
MAX_SKIP_OPS = 1000
MAX_OP_ID_LENGTH = 32
_Revision = Annotated[int, Field(ge=0, le=store.MAX_REVISION)]
_OpId = Annotated[str, StringConstraints(min_length=3, max_length=MAX_OP_ID_LENGTH, pattern=r"^[0-9]+:[0-9]+$")]


class ChangesRequest(BaseModel):
    """Body of ``POST .../studio/changes``: 1 to 100 ops, at most ``MAX_CHANGE_SET_BYTES`` of them as JSON."""

    model_config = ConfigDict(populate_by_name=True)

    ops: list[dict[str, Any]] = Field(min_length=1, max_length=changes.MAX_OPS)
    expected_revision: _Revision = Field(alias="expectedRevision")
    dry_run: bool = Field(default=False, alias="dryRun")

    @field_validator("ops")
    @classmethod
    def _bounded(cls, ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(json.dumps(ops, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_CHANGE_SET_BYTES:
            raise ValueError(f"The ops are more than {MAX_CHANGE_SET_BYTES} bytes")
        return ops


class RevisionRequest(BaseModel):
    """Body of ``POST .../studio/undo`` and ``.../studio/redo``."""

    model_config = ConfigDict(populate_by_name=True)

    expected_revision: _Revision = Field(alias="expectedRevision")


class RebaseRequest(BaseModel):
    """Body of ``POST .../studio/rebase``."""

    model_config = ConfigDict(populate_by_name=True)

    expected_revision: _Revision = Field(alias="expectedRevision")
    skip_ops: list[_OpId] = Field(default_factory=list, alias="skipOps", max_length=MAX_SKIP_OPS)


def _draft_view(namespace: str, proposal_id: str, item: dict[str, Any], state: store.StudioState) -> dict[str, Any]:
    head = store.head_version(namespace)
    return {
        "proposalId": proposal_id,
        "status": item.get("status"),
        "editable": item.get("status") in store.EDITABLE_STATUSES,
        "revision": state.revision,
        "baseVersion": state.base_version,
        "headVersion": head,
        "rebaseRequired": head != state.base_version,
        "ontologyId": state.ontology_id,
        "counts": state.counts,
        "canUndo": bool(state.applied),
        "canRedo": bool(state.redo),
        "openedAt": state.opened_at,
        "openedBy": state.opened_by,
        "updatedAt": state.updated_at,
        "updatedBy": state.updated_by,
    }


def _current(namespace: str, proposal_id: str) -> tuple[dict[str, Any], store.StudioState, store.Revision]:
    item = store.get_proposal(namespace, proposal_id)
    state = store.require_state(item, editable=False)
    return item, state, store.load_revision(namespace, proposal_id, state.revision)


@router.post("")
def open_studio_draft(
    proposal_id: str, namespace: str = "default", x_coa_principal: Annotated[str | None, Header()] = None
) -> dict[str, Any]:
    """Open the proposal in the Ontology designer (idempotent) and return the draft."""

    def run() -> dict[str, Any]:
        state = store.open_draft(namespace, proposal_id, principal.caller(x_coa_principal))
        return _draft_view(namespace, proposal_id, store.get_proposal(namespace, proposal_id), state)

    return _call(run)


@router.get("")
def get_studio_draft(proposal_id: str, namespace: str = "default") -> dict[str, Any]:
    """Return the draft's revision, base version, whether a rebase is needed, and its counts."""

    def run() -> dict[str, Any]:
        item = store.get_proposal(namespace, proposal_id)
        return _draft_view(namespace, proposal_id, item, store.require_state(item, editable=False))

    return _call(run)


@router.get("/graph")
def get_studio_graph(
    proposal_id: str,
    namespace: str = "default",
    focus: Annotated[str | None, Query(max_length=MAX_IRI_LENGTH)] = None,
) -> dict[str, Any]:
    """Return the class graph (or the focus view around ``focus``) with ABox badges."""

    def run() -> dict[str, Any]:
        if focus is not None:
            check_iri(focus, "focus")
        _, state, rev = _current(namespace, proposal_id)
        badges = graph_view.abox_badges(namespace)
        try:
            return graph_view.graph_view(
                rev.draft, store.scheme(namespace, state), revision=state.revision, focus=focus, badges=badges
            )
        except KeyError as e:
            raise HTTPException(404, {"code": "TermNotFound", "message": f"{focus} is not a class of the draft"}) from e

    return _call(run)


@router.get("/term")
def get_studio_term(
    iri: Annotated[str, Query(max_length=MAX_IRI_LENGTH)], proposal_id: str, namespace: str = "default"
) -> dict[str, Any]:
    """Return one class or property of the draft for the Inspector."""

    def run() -> dict[str, Any]:
        check_iri(iri, "iri")
        _, state, rev = _current(namespace, proposal_id)
        badges = graph_view.abox_badges(namespace)
        try:
            return graph_view.term_view(rev.draft, store.scheme(namespace, state), iri, badges)
        except KeyError as e:
            raise HTTPException(404, {"code": "TermNotFound", "message": f"{iri} is not in the draft"}) from e

    return _call(run)


@router.get("/terms")
def search_studio_terms(
    proposal_id: str,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    namespace: str = "default",
    kind: Literal["class", "objectProperty", "datatypeProperty"] | None = None,
    limit: Annotated[int, Query(ge=1, le=graph_view.MAX_SEARCH)] = 20,
) -> dict[str, Any]:
    """Search the draft's terms by label or IRI."""

    def run() -> dict[str, Any]:
        _, state, rev = _current(namespace, proposal_id)
        return {"revision": state.revision, "terms": graph_view.search_terms(rev.draft, q, kind, limit)}

    return _call(run)


@router.post("/changes")
def apply_studio_changes(
    proposal_id: str,
    body: ChangesRequest,
    namespace: str = "default",
    x_coa_principal: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Apply a change set (all ops or none) to ``expectedRevision``; ``dryRun`` stores nothing."""
    return _call(
        lambda: changes.apply_change_set(
            namespace,
            proposal_id,
            body.ops,
            body.expected_revision,
            author=principal.caller(x_coa_principal),
            dry_run=body.dry_run,
        ),
        client_ops=True,
    )


@router.post("/undo")
def undo_studio_change(
    proposal_id: str,
    body: RevisionRequest,
    namespace: str = "default",
    x_coa_principal: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Undo the newest change set in effect."""
    return _call(
        lambda: changes.undo(namespace, proposal_id, body.expected_revision, author=principal.caller(x_coa_principal))
    )


@router.post("/redo")
def redo_studio_change(
    proposal_id: str,
    body: RevisionRequest,
    namespace: str = "default",
    x_coa_principal: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Redo the newest undone change set."""
    return _call(
        lambda: changes.redo(namespace, proposal_id, body.expected_revision, author=principal.caller(x_coa_principal))
    )


def history_scope(namespace: str, proposal_id: str) -> str:
    """The ``nextToken`` scope of one draft's history listing."""
    return f"studio-history:{namespace}:{proposal_id}"


@router.get("/history")
def list_studio_history(
    proposal_id: str,
    namespace: str = "default",
    max_results: Annotated[int | None, Query(alias="maxResults", ge=1, le=LIST_HISTORY_MAX_RESULTS)] = None,
    next_token: Annotated[str | None, Query(alias="nextToken")] = None,
) -> dict[str, Any]:
    """Return one page of the draft's revisions, newest first.

    ``revision``, ``canUndo`` and ``canRedo`` describe the draft now and are on every page. A
    malformed token, or one minted for another draft, is a 400.
    """
    scope = history_scope(namespace, proposal_id)
    try:
        key = decode_next_token(next_token, scope=scope, pk=proposal_id)
        if key is not None and not key["SK"].isdecimal():
            raise InvalidNextToken("not_a_revision")
    except InvalidNextToken as e:
        raise HTTPException(400, str(e)) from e
    start = int(key["SK"]) if key else None

    def run() -> dict[str, Any]:
        try:
            page = changes.history(namespace, proposal_id, max_results or LIST_HISTORY_DEFAULT_MAX_RESULTS, start)
        except changes.InvalidHistoryStart as e:
            raise HTTPException(400, str(InvalidNextToken("not_in_history"))) from e
        resume = page.pop("next")
        body = page_body(
            "entries",
            page.pop("entries"),
            {"PK": proposal_id, "SK": str(resume)} if resume is not None else None,
            scope=scope,
        )
        return {**page, **body}

    return _call(run)


@router.post("/rebase")
def rebase_studio_draft(
    proposal_id: str,
    body: RebaseRequest,
    namespace: str = "default",
    x_coa_principal: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Rebuild the draft on the newest version and replay its user ops (409 ``RebaseFailed`` lists failures)."""
    return _call(
        lambda: rebase.rebase(
            namespace,
            proposal_id,
            body.expected_revision,
            body.skip_ops,
            author=principal.caller(x_coa_principal),
        )
    )


def _renames(namespace: str, proposal_id: str, state: store.StudioState) -> list[dict[str, str]]:
    """Where each IRI the change sets in effect renamed or merged away ended up, oldest first.

    Every op moves the IRIs that had ended up at its ``old`` IRI on to its ``new`` one, and a merge
    makes them ``merged``. ``old`` itself is listed too, unless an earlier ``change_iri`` introduced
    it: that IRI was never in the base, so only the IRIs that reached it are listed.
    """
    moved: dict[str, tuple[str, str]] = {}  # an IRI -> (where it ended up, "renamed" or "merged")
    introduced: set[str] = set()  # the ``newIri`` of every ``change_iri`` so far
    for n in state.applied:
        for op in store.get_record(namespace, proposal_id, n).get("ops") or []:
            if op.get("op") == "change_iri":
                old, new, kind = op["iri"], op["newIri"], "renamed"
            elif op.get("op") == "merge_classes":
                old, new, kind = op["source"], op["target"], "merged"
            else:
                continue
            for origin, (to, was) in moved.items():
                if to == old:
                    moved[origin] = (new, "merged" if "merged" in (was, kind) else "renamed")
            if old not in introduced:
                moved[old] = (new, kind)
            if kind == "renamed":
                introduced.add(new)
    return [{"fromIri": o, "toIri": to, "kind": kind} for o, (to, kind) in moved.items() if o != to]


@router.get("/accept-preview")
def get_studio_accept_preview(proposal_id: str, namespace: str = "default") -> dict[str, Any]:
    """What accepting the current revision would publish: the diff, renames and governed metrics it breaks.

    ``structured`` is the structured-query check of the terms the diff adds or changes, and of the
    classes the base version answers and the draft does not, with the mapped terms of the base
    version that the draft deletes.
    """

    def run() -> dict[str, Any]:
        _, state, rev = _current(namespace, proposal_id)
        head = store.head_version(namespace)
        base = (
            snapshot.parse_turtle(storage.require_version_file(namespace, state.base_version, "ontology.ttl"))
            if state.base_version is not None
            else None
        )
        diff = snapshot.compute_diff(base, rev.draft.tbox)
        removed = {iri for entry in diff.values() for iri in entry["removed"]}
        class_iris, object_props, datatype_props = schema_subjects(rev.draft.tbox)
        present = {str(t) for t in (*class_iris, *object_props, *datatype_props)}
        renames = [
            r
            for r in _renames(namespace, proposal_id, state)
            if r["fromIri"] in removed and r["toIri"] in present  # a class renamed and then deleted is just removed
        ]
        classes = {str(c) for c in class_iris}
        breaking: list[dict[str, str]] = []
        breaking_error = None
        try:
            breaking = version_deploy.find_breaking_metrics(namespace, state.ontology_id, classes)
        except Exception:  # noqa: BLE001 - the preview still shows the diff when Neptune is unreachable
            _log.warning("studio[%s/%s]: breaking-metrics preview failed", namespace, proposal_id, exc_info=True)
            breaking_error = BREAKING_METRICS_ERROR
        touched = {iri for entry in diff.values() for iri in (*entry["added"], *entry["changed"])}
        preview: dict[str, Any] = {
            "revision": state.revision,
            "baseVersion": state.base_version,
            "headVersion": head,
            "rebaseRequired": head != state.base_version,
            "diff": diff,
            "renamed": renames,
            "breakingMetrics": breaking,
            "breakingMetricsError": breaking_error,
            "structured": checks.structured(namespace, state, rev.draft, only=touched, base_tbox=base),
        }
        # The fixed errors as codes too, for the web app to write in the user's language.
        if breaking_error:
            preview["breakingMetricsErrorCode"] = checks.ERROR_CODES.get(breaking_error)
        preview["structured"] = checks.with_error_code(preview["structured"])
        return preview

    return _call(run)


# ── Chat and agent change sets ──────────────────────────────────────────────

# The model's bounds: ``ListStudioEvents``, ``ListStudioChangeSets``, ``StudioOpIndexList`` and the
# ``opIndexes`` query of ``PreviewStudioChangeSet``.
LIST_CHANGE_SETS_MAX_RESULTS = 50
LIST_CHANGE_SETS_DEFAULT_MAX_RESULTS = 20
MAX_OP_INDEXES_QUERY = 200


class MessageRequest(BaseModel):
    """Body of ``POST .../studio/messages``; its limits are checked by ``agent.begin_turn`` (422 ``InvalidMessage``).

    ``clientToken`` is the API proxy's idempotency key; the engine ignores it.
    """

    model_config = ConfigDict(populate_by_name=True)

    text: str
    selection: list[str] = Field(default_factory=list)


class ChangeSetApplyRequest(BaseModel):
    """Body of ``POST .../studio/changesets/{changeSetId}/apply``."""

    model_config = ConfigDict(populate_by_name=True)

    expected_revision: _Revision = Field(alias="expectedRevision")
    # An empty, repeated or out-of-range index is ``changesets.InvalidOpIndexes`` (422 ``InvalidRequest``).
    op_indexes: list[int] | None = Field(default=None, alias="opIndexes", max_length=changesets.MAX_AGENT_OPS)


def _invalid_request(message: str) -> HTTPException:
    return HTTPException(422, {"code": "InvalidRequest", "message": message})


def _change_set_id(change_set_id: str) -> str:
    try:
        return changesets.check_id(change_set_id)
    except ValueError as e:
        raise _invalid_request(str(e)) from e


def _op_indexes(raw: str | None) -> list[int] | None:
    """``opIndexes`` of a query string, ``0,2,3``; None (every op) when absent."""
    if raw is None:
        return None
    try:
        return [int(part) for part in raw.split(",")]
    except ValueError as e:
        raise _invalid_request("opIndexes is a comma-separated list of op indexes, such as 0,2,3") from e


@router.post("/messages", status_code=202)
def send_studio_message(
    proposal_id: str,
    body: MessageRequest,
    namespace: str = "default",
    x_coa_principal: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Send a message to the assistant; its turn runs in the background (poll ``/events``)."""
    return _call(
        lambda: agent.send_message(
            namespace, proposal_id, body.text, body.selection, author=principal.caller(x_coa_principal)
        )
    )


@router.get("/events")
def list_studio_events(
    proposal_id: str,
    namespace: str = "default",
    after: Annotated[int, Query(ge=0, le=chat_store.MAX_SEQ)] = 0,
    limit: Annotated[int, Query(ge=1, le=chat_store.MAX_EVENTS_PAGE)] = 200,
) -> dict[str, Any]:
    """The chat's events with a seq above ``after``, oldest first, and the newest turn."""

    def run() -> dict[str, Any]:
        store.require_state(store.get_proposal(namespace, proposal_id), editable=False)
        return chat_store.list_events(namespace, proposal_id, after=after, limit=limit)

    return _call(run)


def change_sets_scope(namespace: str, proposal_id: str, status: str | None) -> str:
    """The ``nextToken`` scope of one draft's change set listing, with or without a ``status``."""
    return f"studio-changesets:{namespace}:{proposal_id}:{status or ''}"


@router.get("/changesets")
def list_studio_change_sets(
    proposal_id: str,
    namespace: str = "default",
    status: Literal["pending", "applied", "discarded"] | None = None,
    max_results: Annotated[int | None, Query(alias="maxResults", ge=1, le=LIST_CHANGE_SETS_MAX_RESULTS)] = None,
    next_token: Annotated[str | None, Query(alias="nextToken")] = None,
) -> dict[str, Any]:
    """One page of the change sets the assistant proposed, oldest first.

    ``revision`` describes the draft now and is on every page. A malformed token, or one minted for
    another listing, is a 400.
    """
    scope = change_sets_scope(namespace, proposal_id, status)
    try:
        start = decode_next_token(next_token, scope=scope, pk=changesets.partition(namespace, proposal_id))
        if start is not None and not start["SK"].startswith(changesets.CS_SK_PREFIX):
            raise InvalidNextToken("not_a_change_set")
    except InvalidNextToken as e:
        raise HTTPException(400, str(e)) from e

    def run() -> dict[str, Any]:
        page = changesets.list_change_sets(
            namespace,
            proposal_id,
            status,
            limit=max_results or LIST_CHANGE_SETS_DEFAULT_MAX_RESULTS,
            start=start,
        )
        return {"revision": page["revision"], **page_body("changeSets", page["changeSets"], page["next"], scope=scope)}

    return _call(run)


@router.get("/changesets/{change_set_id}/preview")
def preview_studio_change_set(
    proposal_id: str,
    change_set_id: str,
    namespace: str = "default",
    op_indexes: Annotated[str | None, Query(alias="opIndexes", min_length=1, max_length=MAX_OP_INDEXES_QUERY)] = None,
    focus: Annotated[str | None, Query(max_length=MAX_IRI_LENGTH)] = None,
) -> dict[str, Any]:
    """What applying the selected ops would do to the current revision, with the graph after them."""
    cs_id, indexes = _change_set_id(change_set_id), _op_indexes(op_indexes)
    return _call(lambda: changesets.preview(namespace, proposal_id, cs_id, indexes, focus))


@router.post("/changesets/{change_set_id}/apply")
def apply_studio_change_set(
    proposal_id: str,
    change_set_id: str,
    body: ChangeSetApplyRequest,
    namespace: str = "default",
    x_coa_principal: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Apply the selected ops of a pending change set (all of them without ``opIndexes``)."""
    cs_id = _change_set_id(change_set_id)
    return _call(
        lambda: changesets.apply(
            namespace,
            proposal_id,
            cs_id,
            body.expected_revision,
            body.op_indexes,
            author=principal.caller(x_coa_principal),
        )
    )


@router.post("/changesets/{change_set_id}/discard")
def discard_studio_change_set(
    proposal_id: str,
    change_set_id: str,
    namespace: str = "default",
    x_coa_principal: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Discard a pending change set."""
    cs_id = _change_set_id(change_set_id)
    return _call(lambda: changesets.discard(namespace, proposal_id, cs_id, author=principal.caller(x_coa_principal)))


# ── Checks, queries and competency questions ────────────────────────────────


@router.get("/checks")
def get_studio_checks(proposal_id: str, namespace: str = "default") -> dict[str, Any]:
    """The instant checks of the current revision and its comparison with the data."""
    return _call(lambda: checks.checks(namespace, proposal_id))


@router.post("/questions/{question_id}/check", status_code=202)
def check_studio_question(
    proposal_id: str,
    question_id: str,
    namespace: str = "default",
    x_coa_principal: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Ask the assistant to check a competency question against the draft (a turn, like a message)."""
    author = principal.caller(x_coa_principal)
    return _call(lambda: agent.send_question_check(namespace, proposal_id, question_id, author=author))


class QueryRequest(BaseModel):
    """Body of ``POST .../studio/queries``: SPARQL on the draft, or Cypher on the lexical graph."""

    model_config = ConfigDict(populate_by_name=True)

    language: Literal["sparql", "cypher"]
    target: Literal["draft", "lexical"]
    query: str


@router.post("/queries")
def run_studio_query(proposal_id: str, body: QueryRequest, namespace: str = "default") -> dict[str, Any]:
    """Run one read-only query of the console (at most 1,000 rows, 20 s)."""
    return _call(lambda: queries.run_query(namespace, proposal_id, body.language, body.target, body.query))


# ── Competency questions of the namespace ───────────────────────────────────

questions_router = APIRouter(dependencies=[Depends(_checked_namespace)])


class QuestionRequest(BaseModel):
    """Body of ``PUT /namespaces/{ns}/studio/questions/{questionId}``; limits checked by ``questions``."""

    model_config = ConfigDict(populate_by_name=True)

    text: str
    expectation: str | None = None


LIST_QUESTIONS_MAX_RESULTS = 100
LIST_QUESTIONS_DEFAULT_MAX_RESULTS = 50


@questions_router.get("")
def list_studio_questions(
    namespace: str = "default",
    max_results: Annotated[int | None, Query(alias="maxResults", ge=1, le=LIST_QUESTIONS_MAX_RESULTS)] = None,
    next_token: Annotated[str | None, Query(alias="nextToken")] = None,
) -> dict[str, Any]:
    """One page of the namespace's competency questions with their last check, oldest first.

    The token carries the ``createdAt`` and id of the page's last question. A malformed token, or one
    minted for another namespace, is a 400.
    """
    scope = f"studio-questions:{namespace}"
    try:
        key = decode_next_token(next_token, scope=scope, pk=questions.partition(namespace))
        after = None
        if key is not None:
            created, _, question_id = key["SK"].rpartition("#")
            if not questions.is_question_id(question_id):
                raise InvalidNextToken("not_a_question")
            after = (created, question_id)
    except InvalidNextToken as e:
        raise HTTPException(400, str(e)) from e

    def run() -> dict[str, Any]:
        page = questions.list_questions(namespace, limit=max_results or LIST_QUESTIONS_DEFAULT_MAX_RESULTS, after=after)
        resume = None if page["next"] is None else {"PK": questions.partition(namespace), "SK": "#".join(page["next"])}
        return page_body("questions", page["questions"], resume, scope=scope)

    return _call(run)


@questions_router.put("/{question_id}")
def put_studio_question(
    question_id: str,
    body: QuestionRequest,
    namespace: str = "default",
    x_coa_principal: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Create or replace a question (the client mints the id)."""
    author = principal.caller(x_coa_principal)
    return _call(lambda: questions.put_question(namespace, question_id, body.text, body.expectation, author=author))


@questions_router.delete("/{question_id}", status_code=204)
def delete_studio_question(question_id: str, namespace: str = "default") -> None:
    """Delete a question."""
    _call(lambda: questions.delete_question(namespace, question_id))
