# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""HTTP routes for ontology versions (mounted under ``/ontology``).

Responses are camelCase to match the Smithy contract in ``ontology-graph.smithy``.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from coa_ontology.pagination import InvalidNextToken, decode_next_token, page_body
from coa_ontology.versions import deploy, principal, registry, snapshot, storage

_log = logging.getLogger("versions.router")

router = APIRouter()

# Input bounds, per ``ontology-graph.smithy``: OntologyVersionNumber @range(min: 1,
# max: 1000000000), OntologyDeploymentId @length(min: 1, max: 128), and
# BreakingMetricIdList @length(max: 1000) of BreakingMetricId @length(min: 1, max: 128).
MAX_VERSION_NUMBER = 1_000_000_000
MAX_DEPLOYMENT_ID_LENGTH = 128
MAX_ACKNOWLEDGED_BREAKING_METRICS = 1000
MAX_BREAKING_METRIC_ID_LENGTH = 128
# ListOntologyVersions ``maxResults`` bounds and default, per its Smithy @range.
LIST_VERSIONS_DEFAULT_MAX_RESULTS = 50
LIST_VERSIONS_MAX_RESULTS = 200

_VersionNumber = Annotated[int, Path(ge=1, le=MAX_VERSION_NUMBER)]
_BreakingMetricId = Annotated[str, StringConstraints(min_length=1, max_length=MAX_BREAKING_METRIC_ID_LENGTH)]


class DeployVersionRequest(BaseModel):
    """Body of ``POST /ontology/versions/{version}/deploy``."""

    model_config = ConfigDict(populate_by_name=True)

    acknowledged_breaking_metrics: list[_BreakingMetricId] = Field(
        default_factory=list, alias="acknowledgedBreakingMetrics", max_length=MAX_ACKNOWLEDGED_BREAKING_METRICS
    )


def _version_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": row["version"],
        "status": row["status"],
        "parentVersion": row.get("parent_version"),
        "ontologyId": row.get("ontology_id"),
        "source": row.get("source"),
        "sourceProposalId": row.get("source_proposal_id"),
        "createdBy": row.get("created_by"),
        "createdAt": row.get("created_at"),
        "diffSummary": row.get("diff_summary"),
        "error": row.get("error") or None,
    }


def _deployment_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "deploymentId": row["deployment_id"],
        "version": row["version"],
        "status": row["status"],
        "partial": bool(row.get("partial")),
        "progress": row.get("progress") or {},
        "requestedBy": row.get("requested_by"),
        "error": row.get("error") or None,
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
    }


def _snapshot_conflict(namespace: str, version: int, message: str) -> HTTPException:
    """Return the 409 for a version whose snapshot cannot be described, after logging why."""
    _log.warning("versions[%s]: v%d cannot be described: %s", namespace, version, message)
    return HTTPException(409, message)


def versions_list_scope(namespace: str) -> str:
    """``nextToken`` scope of a ListOntologyVersions listing (see :mod:`coa_ontology.pagination`)."""
    return f"versions:{namespace}"


@router.get("/versions")
def list_versions(
    namespace: str = "default",
    max_results: int | None = Query(None, alias="maxResults", ge=1, le=LIST_VERSIONS_MAX_RESULTS),
    next_token: str | None = Query(None, alias="nextToken"),
) -> dict[str, Any]:
    """List one page of versions newest first, with the active version, deployments and reset floor.

    The namespace-wide fields are on every page. ``servingDeployment``: the latest deployment
    that may have changed what is served (:func:`registry.serving_deployment`); ``partial`` there
    means serving is half-swapped. ``resetAfterVersion``: versions at or below it belong to a
    deleted ontology and cannot be deployed. A malformed token, or one minted for another
    namespace, is a 400.
    """
    scope = versions_list_scope(namespace)
    try:
        start_key = decode_next_token(next_token, scope=scope, pk=registry.versions_partition_key(namespace))
    except InvalidNextToken as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows, next_key = registry.list_versions_page(
        namespace, limit=max_results or LIST_VERSIONS_DEFAULT_MAX_RESULTS, after=start_key
    )
    active = registry.get_active(namespace) or {}
    body = page_body("versions", [_version_view(v) for v in rows], next_key, scope=scope)
    body.update(
        {
            "activeVersion": active.get("version"),
            "latestDeployment": _deployment_view(registry.latest_deployment(namespace)),
            "servingDeployment": _deployment_view(registry.serving_deployment(namespace)),
            "resetAfterVersion": active.get("reset_after_version"),
        }
    )
    return body


@router.get("/versions/{version}")
def get_version(version: _VersionNumber, namespace: str = "default") -> dict[str, Any]:
    """Return one version's manifest, diff, download links and deploy impact.

    Only a PUBLISHED version has a snapshot to describe; its snapshot must be complete (409
    otherwise). Any other status returns empty snapshot fields.
    """
    row = registry.get_version(namespace, version)
    if not row:
        raise HTTPException(404, f"Version {version} not found")
    view: dict[str, Any] = {
        "version": _version_view(row),
        "manifest": {},
        "diff": {},
        "downloadUrls": {},
        "breakingMetrics": [],
    }
    if row["status"] != registry.PUBLISHED:
        return view
    try:
        manifest = storage.read_manifest(namespace, version)
        if manifest is None:
            raise storage.SnapshotIncompleteError(f"Version {version} has no snapshot manifest")
        diff_text = storage.read_version_file(namespace, version, "diff.json")
        diff = json.loads(diff_text) if diff_text else {}
        breaking = deploy.preview_breaking_metrics(namespace, version, manifest=manifest)
    except json.JSONDecodeError as e:
        raise _snapshot_conflict(namespace, version, f"Version {version} has a corrupt diff.json") from e
    except (storage.SnapshotIncompleteError, deploy.DeployConflictError, snapshot.SnapshotValidationError) as e:
        raise _snapshot_conflict(namespace, version, str(e)) from e
    view["manifest"] = manifest
    view["diff"] = diff
    view["downloadUrls"] = {name: storage.presign_version_file(namespace, version, name) for name in manifest["files"]}
    view["breakingMetrics"] = breaking
    return view


@router.post("/versions/{version}/deploy", status_code=202)
def deploy_version(
    version: _VersionNumber,
    body: DeployVersionRequest | None = None,
    namespace: str = "default",
    x_coa_principal: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Start deploying (or rolling back to) ``version``."""
    try:
        deployment_id = deploy.start_deployment(
            namespace,
            version,
            requested_by=principal.caller(x_coa_principal),
            acknowledged_breaking_metrics=(body.acknowledged_breaking_metrics if body else []),
        )
    except deploy.BreakingMetricsError as e:
        raise HTTPException(409, {"message": str(e), "breakingMetrics": e.metrics}) from e
    except (deploy.DeployConflictError, registry.DeployLockHeldError) as e:
        raise HTTPException(409, str(e)) from e
    return JSONResponse(status_code=202, content={"deploymentId": deployment_id})


@router.get("/deployments/{deployment_id}")
def get_deployment(
    deployment_id: Annotated[str, Path(min_length=1, max_length=MAX_DEPLOYMENT_ID_LENGTH)],
    namespace: str = "default",
) -> dict[str, Any]:
    """Return one deployment's status and per-step progress."""
    view = _deployment_view(registry.get_deployment(namespace, deployment_id))
    if not view:
        raise HTTPException(404, f"Deployment {deployment_id} not found")
    return view
