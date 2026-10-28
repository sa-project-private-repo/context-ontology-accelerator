# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the snapshot S3 layout (coa_ontology.versions.storage)."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.exceptions import ClientError
from coa_ontology.versions import storage

pytestmark = pytest.mark.unit

NS = "ns-1"
FILES = {
    "ontology.ttl": "<a> <b> <c> .\n",
    "vkg/ontology.ttl": "<a> <b> <c> .\n",
    "vkg/schema.sql": "CREATE TABLE t (id INT);\n",
    "mappings.r2rml": "<m> <n> <o> .\n",
}
LATEST_VKG = ("ontology.ttl", "mappings.r2rml", "schema.sql")


def _keys(aws) -> list[str]:
    return sorted(o["Key"] for o in aws.s3.list_objects_v2(Bucket=aws.bucket).get("Contents", []))


def _latest_keys(aws) -> list[str]:
    return [k for k in _keys(aws) if "/latest/" in k]


def _content_type(aws, key: str) -> str:
    return aws.s3.head_object(Bucket=aws.bucket, Key=key)["ContentType"]


def _manifest(files: dict[str, str]) -> str:
    return json.dumps({"files": {name: {} for name in files}})


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Op")


class TestKeys:
    def test_layout(self):
        assert storage.version_key(NS, 2, "vkg/ontology.ttl") == "ontologies/ns-1/versions/v2/vkg/ontology.ttl"
        assert storage.latest_key(NS, "ontology.ttl") == "ontologies/ns-1/latest/ontology.ttl"

    @pytest.mark.parametrize("namespace", ["", "../x", "a/b", "-lead", "has space"])
    def test_invalid_namespace_is_rejected(self, namespace):
        with pytest.raises(ValueError):
            storage.version_key(namespace, 1, "ontology.ttl")
        with pytest.raises(ValueError):
            storage.latest_key(namespace, "ontology.ttl")

    @pytest.mark.parametrize("name", ["../ontology.ttl", "vkg/../../x", "/ontology.ttl"])
    def test_traversal_in_name_is_rejected(self, name):
        with pytest.raises(ValueError):
            storage.version_key(NS, 1, name)
        with pytest.raises(ValueError):
            storage.latest_key(NS, name)


class TestSnapshotFiles:
    def test_write_then_read_back_including_manifest(self, versions_aws):
        storage.write_snapshot(NS, 2, FILES, '{"version": 2, "files": {"ontology.ttl": {}}}\n')
        assert storage.read_version_file(NS, 2, "ontology.ttl") == FILES["ontology.ttl"]
        assert storage.require_version_file(NS, 2, "ontology.ttl") == FILES["ontology.ttl"]
        assert storage.read_manifest(NS, 2) == {"version": 2, "files": {"ontology.ttl": {}}}
        assert "ontologies/ns-1/versions/v2/manifest.json" in _keys(versions_aws)

    def test_missing_file_and_manifest_return_none(self, versions_aws):
        assert storage.read_version_file(NS, 9, "ontology.ttl") is None
        assert storage.read_manifest(NS, 9) is None

    def test_require_version_file_raises_when_missing(self, versions_aws):
        with pytest.raises(storage.SnapshotFileMissingError):
            storage.require_version_file(NS, 9, "ontology.ttl")

    def test_manifest_is_written_last(self, versions_aws, monkeypatch):
        order: list[str] = []
        real_put = storage._put
        monkeypatch.setattr(
            storage, "_put", lambda key, body, ctype: (order.append(key), real_put(key, body, ctype))[1]
        )
        storage.write_snapshot(NS, 1, FILES, "{}\n")
        assert order[-1].endswith("/manifest.json")

    def test_failed_put_mid_snapshot_leaves_it_incomplete(self, versions_aws, monkeypatch):
        real_put = storage._put
        calls: list[str] = []

        def flaky(key, body, ctype):
            calls.append(key)
            if len(calls) == 2:
                raise _client_error("InternalError")
            real_put(key, body, ctype)

        monkeypatch.setattr(storage, "_put", flaky)
        with pytest.raises(ClientError):
            storage.write_snapshot(NS, 1, FILES, _manifest(FILES))
        assert storage.read_manifest(NS, 1) is None

    def test_write_is_immutable(self, versions_aws):
        storage.write_snapshot(NS, 2, FILES, '{"version": 2}\n')
        with pytest.raises(storage.SnapshotExistsError):
            storage.write_snapshot(NS, 2, {**FILES, "ontology.ttl": "changed\n"}, '{"version": 2}\n')
        assert storage.read_version_file(NS, 2, "ontology.ttl") == FILES["ontology.ttl"]

    def test_concurrent_write_race_is_reported_as_existing(self, versions_aws):
        s3 = MagicMock()
        s3.put_object.side_effect = _client_error("ConditionalRequestConflict")
        with patch("coa_ontology.dynamo_store._get_s3", return_value=s3), pytest.raises(storage.SnapshotExistsError):
            storage.write_snapshot(NS, 1, FILES, "{}\n")

    def test_other_put_errors_propagate(self, versions_aws):
        s3 = MagicMock()
        s3.put_object.side_effect = _client_error("AccessDenied")
        with patch("coa_ontology.dynamo_store._get_s3", return_value=s3), pytest.raises(ClientError) as exc:
            storage.write_snapshot(NS, 1, FILES, "{}\n")
        assert exc.value.response["Error"]["Code"] == "AccessDenied"

    def test_content_types(self, versions_aws):
        storage.write_snapshot(NS, 1, FILES, "{}\n")
        assert _content_type(versions_aws, "ontologies/ns-1/versions/v1/ontology.ttl") == "text/turtle"
        assert _content_type(versions_aws, "ontologies/ns-1/versions/v1/mappings.r2rml") == "text/turtle"
        assert _content_type(versions_aws, "ontologies/ns-1/versions/v1/vkg/schema.sql") == "text/sql"
        assert _content_type(versions_aws, "ontologies/ns-1/versions/v1/manifest.json") == "application/json"

    def test_unknown_extension_is_octet_stream(self):
        assert storage._content_type("notes.bin") == "application/octet-stream"

    def test_non_404_client_error_propagates_from_get(self, versions_aws):
        s3 = MagicMock()
        s3.get_object.side_effect = _client_error("AccessDenied")
        with patch("coa_ontology.dynamo_store._get_s3", return_value=s3), pytest.raises(ClientError):
            storage.read_version_file(NS, 1, "ontology.ttl")

    @pytest.mark.parametrize(
        "body",
        ["not json", "[1, 2]", '"text"', "null", "7", "{}", '{"files": null}', '{"files": ["a"]}', '{"files": "a"}'],
    )
    def test_malformed_manifest_is_incomplete(self, versions_aws, body):
        storage.write_snapshot(NS, 1, {}, body)
        with pytest.raises(storage.SnapshotIncompleteError):
            storage.read_manifest(NS, 1)

    def test_presigned_url_targets_the_version_key(self, versions_aws):
        url = storage.presign_version_file(NS, 2, "vkg/ontology.ttl")
        parsed = urlparse(url)
        assert parsed.path.endswith("/ontologies/ns-1/versions/v2/vkg/ontology.ttl")
        query = parse_qs(parsed.query)
        assert query["response-content-disposition"] == ['attachment; filename="ns-1-v2-vkg-ontology.ttl"']

    def test_presigned_url_defaults_to_one_hour(self, versions_aws):
        query = parse_qs(urlparse(storage.presign_version_file(NS, 2, "ontology.ttl")).query)
        # SigV4 carries a relative X-Amz-Expires; SigV2 an absolute Expires epoch.
        if "X-Amz-Expires" in query:
            assert query["X-Amz-Expires"] == ["3600"]
        else:
            assert abs(int(query["Expires"][0]) - (time.time() + 3600)) < 60


class TestLatest:
    def test_publish_copies_vkg_files_and_removes_absent_ones(self, versions_aws):
        versions_aws.s3.put_object(Bucket=versions_aws.bucket, Key="ontologies/ns-1/latest/schema.sql", Body=b"old")
        files = {k: v for k, v in FILES.items() if k != "vkg/schema.sql"}
        storage.write_snapshot(NS, 3, files, _manifest(files))
        storage.publish_to_latest(NS, 3)
        assert storage.read_latest(NS, "ontology.ttl") == FILES["vkg/ontology.ttl"]
        assert storage.read_latest(NS, "mappings.r2rml") == FILES["mappings.r2rml"]
        assert storage.read_latest(NS, "schema.sql") is None

    def test_publish_copies_schema_sql_and_keeps_content_types(self, versions_aws):
        storage.write_snapshot(NS, 3, FILES, _manifest(FILES))
        storage.publish_to_latest(NS, 3)
        assert storage.read_latest(NS, "schema.sql") == FILES["vkg/schema.sql"]
        assert _content_type(versions_aws, "ontologies/ns-1/latest/ontology.ttl") == "text/turtle"
        assert _content_type(versions_aws, "ontologies/ns-1/latest/mappings.r2rml") == "text/turtle"
        assert _content_type(versions_aws, "ontologies/ns-1/latest/schema.sql") == "text/sql"

    def test_publish_is_idempotent_and_replaces_previous_latest(self, versions_aws):
        storage.write_snapshot(NS, 3, FILES, _manifest(FILES))
        storage.publish_to_latest(NS, 3)
        newer = {**FILES, "vkg/ontology.ttl": "<x> <y> <z> .\n"}
        storage.write_snapshot(NS, 4, newer, _manifest(newer))
        storage.publish_to_latest(NS, 4)
        storage.publish_to_latest(NS, 4)
        assert storage.read_latest(NS, "ontology.ttl") == "<x> <y> <z> .\n"

    def test_publish_leaves_other_latest_files_alone(self, versions_aws):
        versions_aws.s3.put_object(Bucket=versions_aws.bucket, Key="ontologies/ns-1/latest/examples.json", Body=b"{}")
        storage.write_snapshot(NS, 3, FILES, _manifest(FILES))
        storage.publish_to_latest(NS, 3)
        assert storage.read_latest(NS, "examples.json") == "{}"

    def test_publish_of_incomplete_snapshot_raises_and_leaves_latest_untouched(self, versions_aws):
        for name in LATEST_VKG:
            versions_aws.s3.put_object(Bucket=versions_aws.bucket, Key=f"ontologies/ns-1/latest/{name}", Body=b"old")
        # Data files written but the manifest never was (e.g. the writer died mid-snapshot).
        versions_aws.s3.put_object(
            Bucket=versions_aws.bucket, Key="ontologies/ns-1/versions/v5/vkg/ontology.ttl", Body=b"x"
        )
        with pytest.raises(storage.SnapshotIncompleteError):
            storage.publish_to_latest(NS, 5)
        assert [storage.read_latest(NS, name) for name in LATEST_VKG] == ["old", "old", "old"]

    def test_clear_latest_removes_only_vkg_files(self, versions_aws):
        versions_aws.s3.put_object(Bucket=versions_aws.bucket, Key="ontologies/ns-1/latest/examples.json", Body=b"{}")
        storage.write_snapshot(NS, 3, FILES, _manifest(FILES))
        storage.publish_to_latest(NS, 3)
        assert {k.rsplit("/", 1)[1] for k in _latest_keys(versions_aws)} == {*LATEST_VKG, "examples.json"}
        versions_before = [k for k in _keys(versions_aws) if "/versions/" in k]

        storage.clear_latest(NS)

        assert _latest_keys(versions_aws) == ["ontologies/ns-1/latest/examples.json"]
        assert [k for k in _keys(versions_aws) if "/versions/" in k] == versions_before
