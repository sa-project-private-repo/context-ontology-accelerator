# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit test: structured induction records how complete it was on the proposal.

Publishing treats a proposal's R2RML as the full mapping of its datasources only
when the induction was a ``table_to_ontology`` run that dropped no table, so the
proposal metadata must carry the strategy and the dropped-table count.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_ontology.inducer.services.data_catalog import CatalogTable

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_induce_catalog_globals():
    """Reset induce_catalog module globals after each test."""
    from coa_ontology import induce_catalog

    jobs_snapshot = dict(induce_catalog._jobs)
    namespaces_snapshot = dict(induce_catalog._job_namespaces)
    try:
        yield
    finally:
        induce_catalog._jobs.clear()
        induce_catalog._jobs.update(jobs_snapshot)
        induce_catalog._job_namespaces.clear()
        induce_catalog._job_namespaces.update(namespaces_snapshot)


def _induce(strategy: str, dropped_tables: list[dict[str, str]]) -> dict:
    """Run a structured induction with a fake strategy and return the proposal metadata."""
    from coa_ontology.induce_catalog import WorkbenchInductionRequest, _jobs, _run_induction

    schemas = __import__("coa_ontology.inducer.schemas", fromlist=["JobResponse", "JobStatus"])
    job = schemas.JobResponse(job_id="job-1", status=schemas.JobStatus.PENDING, created_at="2026-10-14T00:00:00Z")
    _jobs["job-1"] = job
    body = WorkbenchInductionRequest(
        datasource_ids=["ds-1"], ontology_uri_prefix="http://test.org/bird#", strategy=strategy
    )

    class _FakeStrategy:
        def induce(self, **kwargs):
            fake_graph = MagicMock()
            fake_graph.serialize.return_value = "@prefix : <#> ."
            return fake_graph, {"t0"}, [], dropped_tables

        def build_r2rml(self, **kwargs):
            fake_graph = MagicMock()
            fake_graph.serialize.return_value = "@prefix rr: <#> ."
            return fake_graph

    fake_constraint = MagicMock()
    fake_constraint.model_dump.return_value = {}

    with (
        patch(
            "coa_ontology.induce_catalog._fetch_catalog_from_smus",
            return_value={"databases": [{"name": "db"}], "sourceType": "POSTGRES"},
        ),
        patch(
            "coa_ontology.induce_catalog._catalog_to_tables",
            return_value=[
                {
                    "id": "t-t0",
                    "name": "t0",
                    "fullyQualifiedName": "db.t0",
                    "columns": [{"name": "id", "dataType": "INTEGER"}],
                }
            ],
        ),
        patch("coa_ontology.induce_catalog.get_strategy", return_value=_FakeStrategy),
        patch("coa_ontology.stores.build_stores", return_value=(MagicMock(), MagicMock())),
        patch("coa_ontology.stores.adapters.StoreOntologyCatalogAdapter", return_value=MagicMock()),
        patch("coa_ontology.validation.shapes.config.generate_config_from_db", return_value=fake_constraint),
        patch("coa_ontology.induce_catalog.os.makedirs"),
        patch("builtins.open", MagicMock()),
        patch("coa_ontology.induce_catalog.dynamo_store") as mock_dynamo,
        patch("coa_ontology.induce_catalog.emit_induction_job_metrics"),
    ):
        mock_dynamo.list_proposals.return_value = []
        mock_dynamo.list_ontologies_registry.return_value = []
        mock_dynamo.put_proposal_matches_s3.return_value = "s3://bucket/key"

        _run_induction(
            job_id="job-1",
            body=body,
            namespace="bird-ns",
            config={"catalog_source": "smus"},
            build_pipeline_fn=lambda _config: MagicMock(),
            schemas_mod=schemas,
            catalog_table_cls=CatalogTable,
        )

    assert job.status == schemas.JobStatus.COMPLETED
    assert mock_dynamo.create_proposal.call_count == 1
    metadata: dict = mock_dynamo.create_proposal.call_args.kwargs["metadata"]
    return metadata


def test_complete_induction_records_strategy_and_zero_dropped_tables() -> None:
    metadata = _induce("table_to_ontology", [])
    assert metadata["induction_strategy"] == "table_to_ontology"
    assert metadata["dropped_tables_count"] == 0


def test_partial_induction_records_its_dropped_table_count() -> None:
    metadata = _induce("rigor_ontology", [{"table_name": "t1", "reason": "parse_failed"}])
    assert metadata["induction_strategy"] == "rigor_ontology"
    assert metadata["dropped_tables_count"] == 1
