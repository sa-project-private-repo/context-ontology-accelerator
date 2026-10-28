# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Domain ABox API (spec §7).

Public routes are mounted at ``/abox``. ``internal_router``, mounted at ``/internal/abox``, is
reachable only inside the VPC (the API proxy never maps it) and refuses any call carrying
``X-Coa-Principal``, which the proxy sets on every public call.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from coa_common import validate_namespace_name
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from coa_ontology.pagination import InvalidNextToken, decode_next_token, encode_next_token, page_body
from coa_ontology.stores.neptune_db_graph import _iri
from coa_ontology.versions import principal, registry, storage

from . import build, mapping, queries, store
from .model import iri_tail
from .tbox import TBox, load_tbox

_log = logging.getLogger("abox.router")
# A deeper OFFSET is an expensive scan: the listing is narrowed with ``classIri`` or ``q`` instead.
MAX_OFFSET = 10_000
# Input bounds, per ``domain-abox.smithy``: the ``maxResults`` @range of ListDomainInstances and
# ListAboxMappings, the ``q`` @length, the VertexIri @length of ``classIri`` and ``iri``, and the
# UpdateAboxMapping ``target`` @length.
LIST_INSTANCES_DEFAULT_MAX_RESULTS = 50
LIST_INSTANCES_MAX_RESULTS = 200
LIST_MAPPINGS_DEFAULT_MAX_RESULTS = 100
LIST_MAPPINGS_MAX_RESULTS = 200
MAX_QUERY_LENGTH = 200
MAX_IRI_LENGTH = 2048
MAX_TARGET_LENGTH = 2048
_STATUSES = (mapping.ACTIVE, mapping.STALE, mapping.IGNORED)


def _checked_namespace(namespace: str = "default") -> str:
    try:
        validate_namespace_name(namespace)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return namespace


def _checked_iri(value: str, name: str) -> str:
    try:
        _iri(value)
    except ValueError as e:
        raise HTTPException(422, f"{name} is not a valid IRI") from e
    return value


router = APIRouter(dependencies=[Depends(_checked_namespace)])
internal_router = APIRouter()


@functools.lru_cache(maxsize=16)
def _tbox(namespace: str, version: int) -> TBox:
    return load_tbox(storage.require_version_file(namespace, version, "ontology.ttl"))


def _active_tbox(namespace: str) -> TBox | None:
    version = (registry.get_active(namespace) or {}).get("version")
    if version is None:
        return None
    try:
        return _tbox(namespace, int(version))
    except (storage.SnapshotFileMissingError, storage.SnapshotIncompleteError) as e:
        _log.warning("abox[%s]: the snapshot of the active v%s is unavailable: %s", namespace, version, e)
        raise HTTPException(409, "The active ontology version's snapshot is unavailable") from e


def _labeler(tbox: TBox | None) -> Callable[[str], str]:
    def label(iri: str) -> str:
        if tbox is not None:
            info = tbox.classes.get(iri) or tbox.object_props.get(iri) or tbox.datatype_props.get(iri)
            if info:
                return info.label
        return unquote(iri_tail(iri))

    return label


def _mapping_view(row: mapping.MappingRow, tbox: TBox | None) -> dict[str, Any]:
    target_label = row.target if row.target in (mapping.IGNORE, mapping.FREE) else _labeler(tbox)(row.target)
    return {
        "id": row.id,
        "kind": row.kind,
        "key": list(row.key),
        "target": row.target,
        "targetLabel": target_label,
        "direction": row.direction,
        "origin": row.origin,
        "status": row.status,
        "rationale": row.rationale,
        "decidedForVersion": row.decided_for_version,
        "updatedBy": row.updated_by,
        "updatedAt": row.updated_at,
    }


def instances_list_scope(namespace: str) -> str:
    """``nextToken`` scope of a ListDomainInstances listing (see :mod:`coa_ontology.pagination`)."""
    return f"abox-instances:{namespace}"


def mappings_list_scope(namespace: str, kind: str | None, status: str | None) -> str:
    """``nextToken`` scope of a ListAboxMappings listing: a token resumes only the filter it was minted for."""
    return f"abox-mappings:{namespace}:{kind or ''}:{status or ''}"


# The instance listing pages by position in the label-ordered SPARQL result, not by a DynamoDB
# key: the token carries the offset in the shared codec's key shape, so it gets the same strict,
# namespace-scoped decoding.
_OFFSET_PK = "offset"


def _encode_offset(namespace: str, offset: int) -> str | None:
    return encode_next_token({"PK": _OFFSET_PK, "SK": str(offset)}, scope=instances_list_scope(namespace))


def _decode_offset(namespace: str, token: str | None) -> int:
    """The offset ``token`` resumes at (0 for the first page); a bad token is a 400."""
    try:
        key = decode_next_token(token, scope=instances_list_scope(namespace), pk=_OFFSET_PK)
    except InvalidNextToken as exc:
        raise HTTPException(400, str(exc)) from exc
    if key is None:
        return 0
    offset = key["SK"]
    if not offset.isdigit() or not 0 < int(offset) <= MAX_OFFSET:
        raise HTTPException(400, "Invalid nextToken.")
    return int(offset)


@router.get("")
def get_domain_abox(namespace: str = "default") -> dict[str, Any]:
    """Return the state of the namespace's domain ABox and whether it matches the active ontology version."""
    try:
        build.recover_if_stalled(namespace)  # read-time sweep of a build whose worker died
    except Exception:
        _log.warning("abox[%s]: stalled-build check failed", namespace, exc_info=True)
    state = store.get_state(namespace) or {"status": store.EMPTY}
    active = (registry.get_active(namespace) or {}).get("version")
    conforms = state.get("conforms_to_version")
    counts = state.get("counts") or {"instances": 0, "relations": 0, "attributes": 0, "evidence": 0}
    return {
        "status": state.get("status", store.EMPTY),
        "current": conforms is not None and conforms == active,
        "conformsToVersion": conforms,
        "activeVersion": active,
        "buildId": state.get("build_id"),
        "builtAt": state.get("built_at"),
        "counts": counts,
        "classCounts": [
            {"classIri": c["class_iri"], "label": c["label"], "count": c["count"]}
            for c in state.get("class_counts") or []
        ],
        "mappingRevision": state.get("mapping_revision"),
        "pending": store.has_pending(namespace),
        "lastError": state.get("last_error"),
    }


@router.get("/instances")
def list_domain_instances(
    namespace: str = "default",
    class_iri: Annotated[str | None, Query(alias="classIri", max_length=MAX_IRI_LENGTH)] = None,
    q: Annotated[str | None, Query(max_length=MAX_QUERY_LENGTH)] = None,
    max_results: Annotated[int | None, Query(alias="maxResults", ge=1, le=LIST_INSTANCES_MAX_RESULTS)] = None,
    next_token: Annotated[str | None, Query(alias="nextToken")] = None,
) -> dict[str, Any]:
    """List instances by label, optionally of one class or matching ``q`` in the label.

    The list stops at ``MAX_OFFSET``: no token is handed out for a page that ``_decode_offset``
    would refuse. A malformed token, or one minted for another namespace, is a 400.
    """
    offset = _decode_offset(namespace, next_token)
    limit = max_results or LIST_INSTANCES_DEFAULT_MAX_RESULTS
    if class_iri:
        _checked_iri(class_iri, "classIri")
    label = _labeler(_active_tbox(namespace))
    rows, more = queries.list_instances(namespace, class_iri=class_iri, q=q, limit=limit, offset=offset)
    body: dict[str, Any] = {"instances": [{**r, "classLabel": label(r["classIri"])} for r in rows[:limit]]}
    if more and offset + limit <= MAX_OFFSET:
        body["nextToken"] = _encode_offset(namespace, offset + limit)
    return body


@router.get("/instance")
def get_domain_instance(
    iri: Annotated[str, Query(max_length=MAX_IRI_LENGTH)], namespace: str = "default"
) -> dict[str, Any]:
    """Return one instance with its attributes, links and evidence."""
    _checked_iri(iri, "iri")
    found = queries.instance(namespace, iri, _labeler(_active_tbox(namespace)))
    if found is None:
        raise HTTPException(404, "Instance not found in the domain ABox")
    return found


@router.get("/mappings")
def list_abox_mappings(
    namespace: str = "default",
    kind: str | None = None,
    status: str | None = None,
    max_results: Annotated[int | None, Query(alias="maxResults", ge=1, le=LIST_MAPPINGS_MAX_RESULTS)] = None,
    next_token: Annotated[str | None, Query(alias="nextToken")] = None,
) -> dict[str, Any]:
    """List one page of the namespace's mapping rows, optionally of one kind or status.

    Rows come in mapping-id order (kind, then digest). A malformed token, or one minted for
    another namespace or filter, is a 400.
    """
    if kind is not None and kind not in mapping.KINDS:
        raise HTTPException(422, f"kind must be one of {', '.join(mapping.KINDS)}")
    if status is not None and status not in _STATUSES:
        raise HTTPException(422, f"status must be one of {', '.join(_STATUSES)}")
    scope = mappings_list_scope(namespace, kind, status)
    try:
        start_key = decode_next_token(next_token, scope=scope, pk=store.namespace_pk(namespace))
    except InvalidNextToken as exc:
        raise HTTPException(400, str(exc)) from exc
    tbox = _active_tbox(namespace)
    rows, next_key = mapping.list_rows_page(
        namespace,
        kind,
        status,
        limit=max_results or LIST_MAPPINGS_DEFAULT_MAX_RESULTS,
        after=start_key,
    )
    return page_body("mappings", [_mapping_view(r, tbox) for r in rows], next_key, scope=scope)


class MappingUpdate(BaseModel):
    """Body of ``PUT /abox/mappings/{id}``: the TBox term (or ``ignore`` / ``free``) a row maps to."""

    target: str = Field(min_length=1, max_length=MAX_TARGET_LENGTH)
    direction: Literal["forward", "inverse"] = "forward"


def _row_or_404(namespace: str, mapping_id: str) -> mapping.MappingRow:
    row = mapping.get_row(namespace, mapping_id) if mapping.is_mapping_id(mapping_id) else None
    if row is None:
        raise HTTPException(404, "Mapping not found")
    return row


def _misfit(tbox: TBox, key: tuple[str, ...], target: str, direction: str) -> str:
    """Why ``target`` cannot map a relation key in ``direction``: its domain and range, and what fits instead."""
    label = _labeler(tbox)
    prop = tbox.object_props[target]

    def classes(iris: frozenset[str]) -> str:
        return " or ".join(sorted(label(i) for i in iris)) or "any class"

    subject, obj = (key[2], key[1]) if direction == mapping.INVERSE else (key[1], key[2])
    other = mapping.FORWARD if direction == mapping.INVERSE else mapping.INVERSE
    hint = (
        f"The {other} direction fits."
        if mapping.relation_fits(tbox, key, target, other)
        else "Neither direction fits; pick another property."
    )
    return (
        f"{prop.label} has domain {classes(prop.domain)} and range {classes(prop.range)}, "
        f"so it does not fit a relation from {label(subject)} to {label(obj)}. {hint}"
    )


@router.put("/mappings/{mapping_id}")
def update_abox_mapping(
    mapping_id: str,
    body: MappingUpdate,
    namespace: str = "default",
    x_coa_principal: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Pin a mapping row to a human decision and queue a rebuild."""
    row = _row_or_404(namespace, mapping_id)
    tbox = _active_tbox(namespace)
    if tbox is None:
        raise HTTPException(409, "The namespace has no active ontology version")
    if not mapping.valid_target(tbox, row.kind, body.target):
        raise HTTPException(422, f"target is not a valid {row.kind} target in the active ontology")
    if body.direction != mapping.FORWARD and row.kind != mapping.RELATION:
        raise HTTPException(422, "Only relation mappings have a direction")
    if (
        row.kind == mapping.RELATION
        and body.target != mapping.IGNORE
        and not mapping.relation_fits(tbox, row.key, body.target, body.direction)
    ):
        raise HTTPException(422, _misfit(tbox, row.key, body.target, body.direction))
    try:
        updated = mapping.set_manual(
            namespace, mapping_id, body.target, body.direction, principal.caller(x_coa_principal)
        )
    except mapping.MappingConflict as e:
        raise HTTPException(409, "The mapping changed meanwhile; reload and try again") from e
    except KeyError as e:
        raise HTTPException(404, "Mapping not found") from e
    build.request_build(namespace, "mapping edit")
    return _mapping_view(updated, tbox)


@router.delete("/mappings/{mapping_id}")
def reset_abox_mapping(
    mapping_id: str,
    namespace: str = "default",
    x_coa_principal: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Hand a manual mapping row back to the matcher and queue a rebuild."""
    _row_or_404(namespace, mapping_id)
    tbox = _active_tbox(namespace)  # before the write: an unavailable snapshot must not fail a reset that already ran
    try:
        updated = mapping.reset_to_auto(namespace, mapping_id, principal.caller(x_coa_principal))
    except mapping.MappingConflict as e:
        raise HTTPException(409, "The mapping changed meanwhile; reload and try again") from e
    except KeyError as e:
        raise HTTPException(404, "Mapping not found") from e
    build.request_build(namespace, "mapping reset")
    return _mapping_view(updated, tbox)


@router.post("/rebuild")
def rebuild_domain_abox(namespace: str = "default") -> JSONResponse:
    """Queue a rebuild of the domain ABox."""
    if not build.request_build(namespace, "manual rebuild"):
        raise HTTPException(409, "There is no ontology version to build the domain ABox from")
    return JSONResponse(status_code=202, content={"requested": True})


class InternalRequest(BaseModel):
    """Body of ``POST /internal/abox/requests``, sent by the document pipeline."""

    model_config = ConfigDict(populate_by_name=True)

    namespace: str
    reason: str = "documents"
    doc_source_id: str | None = Field(default=None, alias="docSourceId")


@internal_router.post("/requests")
def request_from_pipeline(
    body: InternalRequest, x_coa_principal: Annotated[str | None, Header()] = None
) -> JSONResponse:
    """Queue a rebuild on behalf of the document pipeline."""
    if x_coa_principal is not None:
        raise HTTPException(403, "This endpoint is internal")
    _checked_namespace(body.namespace)
    reason = f"{body.reason} ({body.doc_source_id})" if body.doc_source_id else body.reason
    return JSONResponse(status_code=202, content={"requested": build.request_build(body.namespace, reason[:200])})


class InternalTeardown(BaseModel):
    """Body of ``POST /internal/abox/teardown``, sent by the namespace deletion pipeline."""

    namespace: str


@internal_router.post("/teardown")
def stop_for_teardown(
    body: InternalTeardown, x_coa_principal: Annotated[str | None, Header()] = None
) -> dict[str, bool]:
    """Stop the namespace's builds for good: its deletion pipeline has started (spec §10).

    The pipeline calls this before it deletes the namespace's sources, each of which ends by
    requesting a build. Nothing waits here; the namespace teardown that deletes the ABox does.
    """
    if x_coa_principal is not None:
        raise HTTPException(403, "This endpoint is internal")
    _checked_namespace(body.namespace)
    build.stop_builds(body.namespace)
    return {"stopped": True}
