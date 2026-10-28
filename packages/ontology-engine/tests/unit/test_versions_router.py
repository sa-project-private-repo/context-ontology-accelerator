# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""HTTP contract of the ontology version routes."""

from __future__ import annotations

import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from coa_common.constants import VOCAB_URI
from coa_ontology.versions import deploy, publish, registry, router, snapshot, storage
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

NS = "ns-1"
ONTO = "https://example.com/onto"
EX = "https://example.com/onto#"
FILES = {"ontology.ttl": {"sha256": "x", "bytes": 1}}
MANIFEST = {"version": 1, "ontologyId": ONTO, "files": FILES}
TTL = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{ONTO}> a owl:Ontology ; rdfs:label "Hospital" .
<{EX}Patient> a owl:Class ; rdfs:label "Patient" .
"""
R2RML = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
<{EX}PatientMap> a rr:TriplesMap ;
    <{VOCAB_URI}datasourceId> "ds-1" ;
    rr:logicalTable [ rr:tableName "patient" ] ;
    rr:subjectMap [ rr:class <{EX}Patient> ; rr:template "p/{{id}}" ] .
"""
VERSION_KEYS = {
    "version",
    "status",
    "parentVersion",
    "ontologyId",
    "source",
    "sourceProposalId",
    "createdBy",
    "createdAt",
    "diffSummary",
    "error",
}
DEPLOYMENT_KEYS = {
    "deploymentId",
    "version",
    "status",
    "partial",
    "progress",
    "requestedBy",
    "error",
    "createdAt",
    "updatedAt",
}


@pytest.fixture
def http(versions_aws):
    app = FastAPI()
    app.include_router(router.router, prefix="/ontology")
    return TestClient(app)


def _reserve(created_by: str = "alice") -> int:
    version, _ = registry.reserve_next_version(
        NS, ontology_id="o", source="PROPOSAL", source_proposal_id="p", created_by=created_by
    )
    return version


def _published(
    version: int = 1,
    manifest_json: str | None = None,
    ontology_ttl: str = "<a> <b> <c> .\n",
    diff_text: str = "{}\n",
) -> None:
    assert _reserve() == version
    registry.mark_published(NS, version, files=FILES, diff_summary={}, source_proposals=["p"])
    storage.write_snapshot(
        NS,
        version,
        {"ontology.ttl": ontology_ttl, "diff.json": diff_text},
        manifest_json if manifest_json is not None else json.dumps(MANIFEST) + "\n",
    )


class TestList:
    def test_pages_newest_first_with_the_namespace_fields_on_every_page(self, http):
        for version in (1, 2, 3):
            _published(version)
        registry.set_active(NS, version=2, deployment_id="d-1", deployed_by="bob")
        first = http.get("/ontology/versions", params={"namespace": NS, "maxResults": 2}).json()
        assert [v["version"] for v in first["versions"]] == [3, 2]
        assert first["nextToken"]
        second = http.get(
            "/ontology/versions", params={"namespace": NS, "maxResults": 2, "nextToken": first["nextToken"]}
        ).json()
        assert [v["version"] for v in second["versions"]] == [1]
        assert "nextToken" not in second
        for page in (first, second):
            assert page["activeVersion"] == 2
            assert "latestDeployment" in page and "servingDeployment" in page and "resetAfterVersion" in page

    @pytest.mark.parametrize("token", ["not-a-token", "eyJQSyI6IngifQ"])
    def test_a_malformed_token_is_400(self, http, token):
        r = http.get("/ontology/versions", params={"namespace": NS, "nextToken": token})
        assert r.status_code == 400
        assert r.json()["detail"] == "Invalid nextToken."

    def test_a_token_from_another_namespace_is_400(self, http):
        from coa_ontology.pagination import encode_next_token

        token = encode_next_token(
            {"PK": "NAMESPACE#other", "SK": "VERSION#000001"}, scope=router.versions_list_scope("other")
        )
        r = http.get("/ontology/versions", params={"namespace": NS, "nextToken": token})
        assert r.status_code == 400

    def test_max_results_above_the_bound_is_rejected(self, http):
        r = http.get("/ontology/versions", params={"namespace": NS, "maxResults": router.LIST_VERSIONS_MAX_RESULTS + 1})
        assert r.status_code == 422

    def test_lists_versions_newest_first_with_active(self, http):
        _published()
        registry.set_active(NS, version=1, deployment_id="d-1", deployed_by="bob")
        body = http.get("/ontology/versions", params={"namespace": NS}).json()
        assert [v["version"] for v in body["versions"]] == [1]
        assert body["versions"][0]["status"] == "PUBLISHED" and body["versions"][0]["createdBy"] == "alice"
        assert body["activeVersion"] == 1 and body["latestDeployment"] is None

    def test_a_version_row_has_exactly_the_contract_members(self, http):
        _published()
        row = http.get("/ontology/versions", params={"namespace": NS}).json()["versions"][0]
        assert set(row) == VERSION_KEYS
        assert row["parentVersion"] is None and row["ontologyId"] == "o" and row["source"] == "PROPOSAL"
        assert row["sourceProposalId"] == "p" and row["error"] is None and row["diffSummary"] == {}
        assert row["createdAt"]

    def test_a_failed_publish_exposes_its_error(self, http):
        _reserve()
        registry.mark_publish_failed(NS, 1, "snapshot write failed")
        body = http.get("/ontology/versions", params={"namespace": NS}).json()
        assert body["versions"][0]["status"] == "PUBLISH_FAILED"
        assert body["versions"][0]["error"] == "snapshot write failed"

    def test_reports_the_reset_floor(self, http):
        _reserve()
        registry.mark_published(NS, 1, files=FILES, diff_summary={}, source_proposals=["p"])
        assert http.get("/ontology/versions", params={"namespace": NS}).json()["resetAfterVersion"] is None
        registry.reset_active(NS, ontology_id="o")
        body = http.get("/ontology/versions", params={"namespace": NS}).json()
        assert body["resetAfterVersion"] == 1 and body["activeVersion"] is None

    def test_empty_namespace_has_no_versions_active_or_deployment(self, http):
        body = http.get("/ontology/versions", params={"namespace": NS}).json()
        assert body == {
            "versions": [],
            "activeVersion": None,
            "latestDeployment": None,
            "servingDeployment": None,
            "resetAfterVersion": None,
        }

    def test_reports_the_latest_deployment(self, http):
        _published()
        registry.create_deployment(NS, "d-1", version=1, requested_by="bob", acknowledged_breaking_metrics=[])
        body = http.get("/ontology/versions", params={"namespace": NS}).json()
        assert set(body["latestDeployment"]) == DEPLOYMENT_KEYS
        assert body["latestDeployment"]["deploymentId"] == "d-1"
        assert body["latestDeployment"]["status"] == "DEPLOYING"
        assert body["servingDeployment"] == body["latestDeployment"]  # a running deployment may have swapped

    def test_reports_the_partial_deployment_a_later_clean_failure_left_serving_in(self, http):
        _published()
        for dep_id, partial in (("20261014T000000Z-a", True), ("20261014T000100Z-b", False)):
            registry.create_deployment(NS, dep_id, version=1, requested_by="bob", acknowledged_breaking_metrics=[])
            registry.finish_deployment(
                NS, dep_id, status=registry.DEPLOY_FAILED, partial=partial, progress={}, error="boom"
            )
        body = http.get("/ontology/versions", params={"namespace": NS}).json()
        assert body["latestDeployment"]["deploymentId"] == "20261014T000100Z-b"
        assert set(body["servingDeployment"]) == DEPLOYMENT_KEYS
        assert body["servingDeployment"]["deploymentId"] == "20261014T000000Z-a"
        assert body["servingDeployment"]["partial"] is True


class TestDetail:
    def test_returns_manifest_diff_urls_and_breaking_metrics(self, http):
        _published()
        with patch.object(deploy, "preview_breaking_metrics", return_value=[{"metricId": "m"}]):
            body = http.get("/ontology/versions/1", params={"namespace": NS}).json()
        assert body["version"]["version"] == 1 and body["version"]["status"] == "PUBLISHED"
        assert body["manifest"] == MANIFEST and body["diff"] == {}
        assert "ontologies/ns-1/versions/v1/ontology.ttl" in body["downloadUrls"]["ontology.ttl"]
        assert body["breakingMetrics"] == [{"metricId": "m"}]

    def test_passes_the_manifest_it_read_to_the_deploy_preview(self, http):
        _published()
        with patch.object(deploy, "preview_breaking_metrics", return_value=[]) as preview:
            http.get("/ontology/versions/1", params={"namespace": NS})
        preview.assert_called_once_with(NS, 1, manifest=MANIFEST)

    def test_a_published_snapshot_is_described_with_a_download_link_per_manifest_file(self, http):
        request = publish.PublishRequest(
            namespace=NS,
            ontology_id=ONTO,
            ontology_turtle=TTL,
            r2rml_turtle=R2RML,
            source_proposal_id="p-1",
            created_by="alice",
        )
        with (
            patch("coa_ontology.proposals._generate_schema_sql", return_value=None),
            patch.object(deploy, "find_breaking_metrics", return_value=[{"metricId": "m"}]) as find,
        ):
            version = publish.publish_version(request)
            body = http.get(f"/ontology/versions/{version}", params={"namespace": NS}).json()
        manifest = storage.read_manifest(NS, version)
        assert body["manifest"] == manifest
        assert set(body["downloadUrls"]) == set(manifest["files"]) and body["downloadUrls"]
        assert body["breakingMetrics"] == [{"metricId": "m"}]
        assert find.call_args.args[:2] == (NS, ONTO) and f"{EX}Patient" in find.call_args.args[2]
        url = body["downloadUrls"]["ontology.ttl"]
        assert urlparse(url).path.endswith(f"/ontologies/{NS}/versions/v{version}/ontology.ttl")
        disposition = parse_qs(urlparse(url).query)["response-content-disposition"]
        assert disposition == [f'attachment; filename="{NS}-v{version}-ontology.ttl"']
        assert body["diff"] == json.loads(storage.require_version_file(NS, version, "diff.json"))

    def test_unknown_version_is_404(self, http):
        assert http.get("/ontology/versions/9", params={"namespace": NS}).status_code == 404

    def test_published_version_without_a_manifest_is_409(self, http):
        _reserve()
        registry.mark_published(NS, 1, files=FILES, diff_summary={}, source_proposals=["p"])
        r = http.get("/ontology/versions/1", params={"namespace": NS})
        assert r.status_code == 409

    @pytest.mark.parametrize("manifest_json", ["not json", "[]", '{"version": 1}'])
    def test_published_version_with_a_malformed_manifest_is_409(self, http, manifest_json):
        _published(manifest_json=manifest_json)
        r = http.get("/ontology/versions/1", params={"namespace": NS})
        assert r.status_code == 409

    def test_published_version_with_a_corrupt_diff_is_409(self, http):
        _published(diff_text="{not json")
        with patch.object(deploy, "preview_breaking_metrics", return_value=[]):
            r = http.get("/ontology/versions/1", params={"namespace": NS})
        assert r.status_code == 409 and r.json()["detail"] == "Version 1 has a corrupt diff.json"

    def test_published_version_whose_manifest_has_no_ontology_id_is_409(self, http):
        _published(manifest_json=json.dumps({"version": 1, "files": FILES}))
        r = http.get("/ontology/versions/1", params={"namespace": NS})
        assert r.status_code == 409 and "ontologyId" in r.json()["detail"]

    def test_published_version_with_an_invalid_ontology_is_409(self, http):
        _published(ontology_ttl="this is not turtle")
        r = http.get("/ontology/versions/1", params={"namespace": NS})
        assert r.status_code == 409 and "ontology" in r.json()["detail"].lower()

    def test_a_validation_error_from_the_deploy_preview_is_409(self, http):
        _published()
        with patch.object(deploy, "preview_breaking_metrics", side_effect=snapshot.SnapshotValidationError("bad")):
            r = http.get("/ontology/versions/1", params={"namespace": NS})
        assert r.status_code == 409 and r.json()["detail"] == "bad"

    def test_refusals_are_logged(self, http, caplog):
        _published(diff_text="{not json")
        with patch.object(deploy, "preview_breaking_metrics", return_value=[]), caplog.at_level("WARNING"):
            http.get("/ontology/versions/1", params={"namespace": NS})
        assert any(r.name == "versions.router" and "corrupt diff.json" in r.getMessage() for r in caplog.records)

    def test_incomplete_snapshot_reported_by_the_deploy_preview_is_409(self, http):
        _published()
        with patch.object(deploy, "preview_breaking_metrics", side_effect=deploy.DeployConflictError("no mappings")):
            r = http.get("/ontology/versions/1", params={"namespace": NS})
        assert r.status_code == 409 and "no mappings" in r.json()["detail"]

    def test_unpublished_version_has_an_empty_manifest_and_no_deploy_preview(self, http):
        _reserve()
        with patch.object(deploy, "preview_breaking_metrics") as preview:
            body = http.get("/ontology/versions/1", params={"namespace": NS}).json()
        assert body["version"]["status"] == "PUBLISHING"
        assert set(body["version"]) == VERSION_KEYS
        assert body["manifest"] == {} and body["diff"] == {} and body["downloadUrls"] == {}
        assert body["breakingMetrics"] == []
        preview.assert_not_called()

    def test_a_failed_publish_exposes_its_error(self, http):
        _reserve()
        registry.mark_publish_failed(NS, 1, "snapshot write failed")
        body = http.get("/ontology/versions/1", params={"namespace": NS}).json()
        assert body["version"]["status"] == "PUBLISH_FAILED" and body["version"]["error"] == "snapshot write failed"
        assert body["manifest"] == {}


class TestDeploy:
    def test_starts_and_returns_202(self, http):
        with patch.object(deploy, "start_deployment", return_value="d-9") as start:
            r = http.post(
                "/ontology/versions/1/deploy",
                params={"namespace": NS},
                json={"acknowledgedBreakingMetrics": ["m"]},
                headers={"X-Coa-Principal": "bob"},
            )
        assert r.status_code == 202 and r.json() == {"deploymentId": "d-9"}
        start.assert_called_once_with(NS, 1, requested_by="bob", acknowledged_breaking_metrics=["m"])

    def test_without_a_body_or_principal_uses_defaults(self, http):
        with patch.object(deploy, "start_deployment", return_value="d-9") as start:
            r = http.post("/ontology/versions/2/deploy", params={"namespace": NS})
        assert r.status_code == 202
        start.assert_called_once_with(NS, 2, requested_by="unknown", acknowledged_breaking_metrics=[])

    @pytest.mark.parametrize(
        "error",
        [
            deploy.DeployConflictError("not published"),
            registry.DeployLockHeldError("d-1"),
            deploy.BreakingMetricsError([{"metricId": "m"}]),
        ],
    )
    def test_conflicts_are_409(self, http, error):
        with patch.object(deploy, "start_deployment", side_effect=error):
            r = http.post("/ontology/versions/1/deploy", params={"namespace": NS}, json={})
        assert r.status_code == 409

    def test_a_delete_holding_the_lock_is_409_naming_the_delete(self, http):
        _published()
        registry.acquire_deploy_lock(NS, "delete-1")
        r = http.post("/ontology/versions/1/deploy", params={"namespace": NS}, json={})
        assert r.status_code == 409
        assert r.json()["detail"] == "an ontology delete is in progress in this namespace"
        assert registry.deploy_lock_holder(NS) == "delete-1"

    def test_a_version_of_a_deleted_ontology_is_409(self, http):
        _published()
        registry.reset_active(NS, ontology_id="o")
        r = http.post("/ontology/versions/1/deploy", params={"namespace": NS}, json={})
        assert r.status_code == 409
        assert r.json()["detail"] == "Version v1 belongs to a deleted ontology; accept a proposal to publish it again."
        assert registry.deploy_lock_holder(NS) is None

    def test_a_manifest_without_an_ontology_id_is_409_and_leaves_no_lock(self, http):
        _published(manifest_json=json.dumps({"version": 1, "files": FILES}))
        r = http.post("/ontology/versions/1/deploy", params={"namespace": NS}, json={})
        assert r.status_code == 409 and "ontologyId" in r.json()["detail"]
        assert registry.deploy_lock_holder(NS) is None

    def test_an_invalid_ontology_is_409_and_leaves_no_lock(self, http):
        _published(ontology_ttl="this is not turtle")
        r = http.post("/ontology/versions/1/deploy", params={"namespace": NS}, json={})
        assert r.status_code == 409
        assert registry.deploy_lock_holder(NS) is None

    def test_breaking_metrics_conflict_lists_the_metrics(self, http):
        error = deploy.BreakingMetricsError([{"metricId": "m"}])
        with patch.object(deploy, "start_deployment", side_effect=error):
            r = http.post("/ontology/versions/1/deploy", params={"namespace": NS}, json={})
        assert r.json()["detail"]["breakingMetrics"] == [{"metricId": "m"}]


class TestDeployment:
    def test_returns_progress(self, http):
        registry.create_deployment(NS, "d-1", version=1, requested_by="bob", acknowledged_breaking_metrics=[])
        body = http.get("/ontology/deployments/d-1", params={"namespace": NS}).json()
        assert body["deploymentId"] == "d-1" and body["status"] == "DEPLOYING" and body["version"] == 1
        assert set(body) == DEPLOYMENT_KEYS
        assert body["partial"] is False and body["progress"] == {} and body["requestedBy"] == "bob"
        assert body["error"] is None and body["createdAt"] and body["updatedAt"]

    def test_a_partial_failure_exposes_progress_and_error(self, http):
        registry.create_deployment(NS, "d-1", version=1, requested_by="bob", acknowledged_breaking_metrics=[])
        registry.finish_deployment(
            NS,
            "d-1",
            status=registry.DEPLOY_FAILED,
            partial=True,
            progress={"stage": "done", "swap": "done", "embeddings": "failed"},
            error="embeddings failed",
        )
        body = http.get("/ontology/deployments/d-1", params={"namespace": NS}).json()
        assert body["status"] == "DEPLOY_FAILED" and body["partial"] is True
        assert body["progress"] == {"stage": "done", "swap": "done", "embeddings": "failed"}
        assert body["error"] == "embeddings failed"

    def test_unknown_deployment_is_404(self, http):
        assert http.get("/ontology/deployments/nope", params={"namespace": NS}).status_code == 404


def test_main_app_mounts_the_version_routes():
    from coa_ontology.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert {
        "/ontology/versions",
        "/ontology/versions/{version}",
        "/ontology/versions/{version}/deploy",
        "/ontology/deployments/{deployment_id}",
    } <= paths
