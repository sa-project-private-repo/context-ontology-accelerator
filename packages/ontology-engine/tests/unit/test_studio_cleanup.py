# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Designer drafts live under proposals/{ns}/{id}/studio/ and go with the proposal and the namespace."""

import pytest
from coa_ontology import dynamo_store
from coa_ontology.studio import draft as store

from .studio_fakes import NS

pytestmark = [pytest.mark.unit]


def _keys(aws, prefix: str) -> list[str]:
    return [o["Key"] for o in aws.s3.list_objects_v2(Bucket=aws.bucket, Prefix=prefix).get("Contents", [])]


def test_namespace_purge_sweeps_every_proposal_artifact_including_drafts(versions_aws):
    for key in (
        store.revision_key(NS, "p-1", 0, "ontology.nt"),
        store.record_key(NS, "p-1", 0),
        f"proposals/{NS}/p-1/latest/ontology.ttl",
        f"proposals/{NS}x/p-9/latest/ontology.ttl",  # another namespace sharing the prefix text
    ):
        versions_aws.s3.put_object(Bucket=versions_aws.bucket, Key=key, Body=b"x")
    dynamo_store.delete_all_namespace_items(NS)
    assert _keys(versions_aws, f"proposals/{NS}/") == []
    assert _keys(versions_aws, f"proposals/{NS}x/") == [f"proposals/{NS}x/p-9/latest/ontology.ttl"]


def test_deleting_a_proposal_removes_its_draft(versions_aws):
    versions_aws.s3.put_object(Bucket=versions_aws.bucket, Key=store.record_key(NS, "p-1", 3), Body=b"{}")
    dynamo_store.delete_proposal(NS, "p-1")
    assert _keys(versions_aws, store.prefix(NS, "p-1")) == []


def test_deleting_a_proposal_leaves_the_rows_it_is_asked_to_keep(versions_aws):
    pk = dynamo_store._proposal_pk(NS, "p-1")
    for sk in ("META", "STUDIOCHAT", "STUDIOGRAPH"):
        versions_aws.table.put_item(Item={"PK": pk, "SK": sk})

    def partition() -> list[str]:
        query = versions_aws.table.query(KeyConditionExpression="PK = :pk", ExpressionAttributeValues={":pk": pk})
        return [i["SK"] for i in query["Items"]]

    assert dynamo_store.delete_proposal(NS, "p-1", keep=("STUDIOGRAPH",)) is True
    assert partition() == ["STUDIOGRAPH"]
    assert dynamo_store.delete_proposal(NS, "p-1") is False  # without keep, every row goes
    assert partition() == []
