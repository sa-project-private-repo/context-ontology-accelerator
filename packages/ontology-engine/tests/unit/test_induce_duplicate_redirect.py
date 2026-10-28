# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit test: duplicate-induction sets ``duplicate_of`` on the in-memory job.

Regression for the "proposal does not exist" redirect: re-inducing a source
whose structural fingerprint matches an already-accepted proposal short-circuits
the run WITHOUT creating a new proposal. The client polls ``/induce/jobs/{id}``,
which returns the in-memory ``JobResponse`` while the worker thread is alive, and
redirects to ``duplicate_of``. If that field is only written to the DDB job row
(not the in-memory object), the client never sees it, falls back to the job id —
which has no proposal row — and 404s. This test pins ``duplicate_of`` onto the
in-memory job object and asserts the phantom stub is cleaned up.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogTable

pytestmark = pytest.mark.unit


def _tbl(name: str, cols: list[CatalogColumn]) -> CatalogTable:
    return CatalogTable(id=f"t-{name}", name=name, fullyQualifiedName=f"db.{name}", columns=cols)


@pytest.mark.unit
def test_duplicate_induction_sets_duplicate_of_on_inmemory_job() -> None:
    from coa_ontology.induce_catalog import (
        WorkbenchInductionRequest,
        _compute_structural_fingerprint,
        _jobs,
        _run_induction,
    )
    from coa_ontology.inducer.schemas import InductionReport, JobResponse, JobStatus

    job = JobResponse(job_id="job-dup", status=JobStatus.PENDING, created_at="2026-01-01T00:00:00Z")
    _jobs["job-dup"] = job

    tables = [_tbl("customers", [CatalogColumn(name="id", dataType="INTEGER")])]
    fingerprint = _compute_structural_fingerprint(tables)

    existing_accepted = {
        "proposal_id": "accepted-proposal-123",
        "ontology_id": "http://test.org/ontology/induced#",
        "metadata": {"structural_fingerprint": fingerprint},
    }

    body = WorkbenchInductionRequest(
        datasource_ids=["ds-1"],
        ontology_uri_prefix="http://test.org/ontology/induced#",
        grounding_ontologies=[],
    )

    with (
        patch(
            "coa_ontology.induce_catalog._fetch_catalog_from_smus",
            return_value={"databases": [{"name": "db"}], "sourceType": "POSTGRES"},
        ),
        patch(
            "coa_ontology.induce_catalog._catalog_to_tables",
            return_value=[
                {
                    "id": "t-customers",
                    "name": "customers",
                    "fullyQualifiedName": "db.customers",
                    "columns": [{"name": "id", "dataType": "INTEGER"}],
                }
            ],
        ),
        patch("coa_ontology.induce_catalog.dynamo_store") as mock_dynamo,
        patch("coa_ontology.induce_catalog.emit_induction_job_metrics"),
    ):
        mock_dynamo.list_proposals.return_value = [existing_accepted]
        mock_dynamo.update_job_status.return_value = None
        mock_dynamo.delete_proposal.return_value = True

        _run_induction(
            job_id="job-dup",
            body=body,
            namespace="test-ns",
            config={"catalog_source": "smus"},
            build_pipeline_fn=MagicMock(),
            schemas_mod=__import__("coa_ontology.inducer.schemas", fromlist=["JobStatus", "InductionReport"]),
            catalog_table_cls=CatalogTable,
        )

    # The in-memory job must carry duplicate_of so the poll response reaches the client.
    assert job.status == JobStatus.COMPLETED
    assert job.duplicate_of == "accepted-proposal-123"
    # report.induced_ontology_id points at the existing proposal, NOT the job id.
    assert isinstance(job.report, InductionReport)
    assert job.report.induced_ontology_id == "accepted-proposal-123"
    # The phantom "inducing" stub (proposal_id == job_id) must be deleted.
    mock_dynamo.delete_proposal.assert_called_once_with("test-ns", "job-dup")
    # DDB job row also gets duplicate_of.
    _, kwargs = _find_completed_update(mock_dynamo.update_job_status.call_args_list)
    assert kwargs.get("duplicate_of") == "accepted-proposal-123"

    _jobs.pop("job-dup", None)


def _find_completed_update(call_args_list):
    for call in call_args_list:
        args, kwargs = call
        if len(args) >= 2 and args[1] == "completed":
            return args, kwargs
    raise AssertionError("no update_job_status(..., 'completed', ...) call found")
