# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the ontology-engine unit tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import boto3
import botocore.endpoint
import pytest

BUCKET = "test-ontology-artifacts"

# ── No real AWS from unit tests ─────────────────────────────────────────────
#
# Dummy credentials keep a stray client from signing as the developer's account,
# and any botocore request moto does not answer is refused. Applied at import as
# well as per test: test modules import ``coa_ontology.dynamo_store`` during
# collection, and it resolves its bucket through STS unless
# ONTOLOGY_ARTIFACTS_BUCKET is set.
_HERMETIC_ENV = {
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    "ONTOLOGY_ARTIFACTS_BUCKET": BUCKET,
}


class RealAwsRequestError(RuntimeError):
    """A unit test sent a request to a real AWS endpoint."""


_real_aws_requests: list[str] = []


def _refuse_real_aws(self: botocore.endpoint.Endpoint, request: Any) -> Any:
    # ``Endpoint._send`` only runs when no ``before-send`` handler answered the
    # request, i.e. moto is not active (or does not mock this call).
    target = f"{request.method} {request.url.split('?')[0]}"
    _real_aws_requests.append(target)
    raise RealAwsRequestError(f"unit test tried to reach real AWS: {target} (stub the client or use versions_aws)")


os.environ.update(_HERMETIC_ENV)
os.environ.pop("AWS_PROFILE", None)
botocore.endpoint.Endpoint._send = _refuse_real_aws


@pytest.fixture(autouse=True)
def _no_real_aws() -> Iterator[None]:
    """Re-pin the dummy AWS env for each test and fail any test that tried to reach AWS.

    Uses its own MonkeyPatch so a test calling ``monkeypatch.undo()`` cannot drop
    it. Failing at teardown catches attempts the code under test swallowed
    (best-effort writes, broad ``except Exception``).
    """
    seen = len(_real_aws_requests)
    with pytest.MonkeyPatch.context() as mp:
        for name, value in _HERMETIC_ENV.items():
            mp.setenv(name, value)
        mp.delenv("AWS_PROFILE", raising=False)
        mp.setattr(botocore.endpoint.Endpoint, "_send", _refuse_real_aws)
        yield
    if attempted := _real_aws_requests[seen:]:
        pytest.fail(f"test tried to reach real AWS: {attempted}")


@dataclass
class VersionsAws:
    """Moto-backed DynamoDB table and S3 bucket wired into ``coa_ontology.dynamo_store``."""

    table: Any
    s3: Any
    bucket: str


@pytest.fixture
def versions_aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[VersionsAws]:
    """Real DynamoDB/S3 semantics (condition expressions, queries) via moto."""
    from moto import mock_aws

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="ontology-engine",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        with (
            patch("coa_ontology.dynamo_store._get_table", return_value=table),
            patch("coa_ontology.dynamo_store._get_s3", return_value=s3),
            patch("coa_ontology.dynamo_store._S3_BUCKET", BUCKET),
        ):
            yield VersionsAws(table=table, s3=s3, bucket=BUCKET)
