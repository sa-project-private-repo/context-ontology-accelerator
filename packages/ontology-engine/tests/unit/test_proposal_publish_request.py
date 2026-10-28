# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""``proposals.publish_request_for`` builds exactly the request an accept hands to publish."""

from unittest.mock import MagicMock, patch

import pytest
from coa_ontology import dynamo_store, proposals
from coa_ontology.versions import deploy, publish
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .abox_fakes import InlineThread
from .studio_fakes import NS, ONTO, PROPOSAL_TBOX

pytestmark = [pytest.mark.unit]


def test_publish_request_for_is_the_request_accept_hands_to_publish(versions_aws):
    dynamo_store.create_proposal(
        proposal_id="p-1",
        job_id="p-1",
        namespace=NS,
        ontology_id=ONTO,
        proposal_type="induction",
        ontology_turtle=PROPOSAL_TBOX,
        r2rml_turtle="",
        metadata={"datasource_ids": ["ds-1"], "induction_strategy": "table_to_ontology", "dropped_tables_count": 0},
    )
    app = FastAPI()
    app.include_router(proposals.router, prefix="/ontology/proposals")
    with (
        patch.object(proposals, "Thread", InlineThread),
        patch.object(publish, "publish_version", MagicMock(return_value=1)) as fake,
        patch.object(deploy, "start_deployment", MagicMock(return_value="d-1")),
    ):
        r = TestClient(app).post(
            f"/ontology/proposals/p-1/accept?namespace={NS}",
            json={"deploy": False},
            headers={"X-Coa-Principal": "alice"},
        )
    assert r.status_code == 202, r.text
    sent = fake.call_args.args[0]
    item = dynamo_store.get_proposal_by_id("p-1", namespace=NS)
    assert proposals.publish_request_for(item, NS, "p-1", ONTO, "alice") == sent
    assert sent.mode == publish.UNION and sent.authoritative_datasources == ["ds-1"]
    assert sent.ontology_turtle == PROPOSAL_TBOX and sent.source_proposal_id == "p-1"
