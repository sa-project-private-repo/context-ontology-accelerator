# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""S3 layout for immutable ontology version snapshots and the VKG ``latest/`` copy.

- ``ontologies/{ns}/versions/v{n}/<file>``: immutable; ``manifest.json`` is written last.
- ``ontologies/{ns}/latest/<file>``: what the VKG entrypoint loads (the active version).
"""

from __future__ import annotations

import json
from typing import Any

from botocore.exceptions import ClientError
from coa_common import validate_namespace_name
from coa_common.constants import ONTOLOGY_ARTIFACT_MANIFEST, ONTOLOGY_ARTIFACTS_PREFIX

from coa_ontology import dynamo_store

PREFIX = ONTOLOGY_ARTIFACTS_PREFIX
MANIFEST = ONTOLOGY_ARTIFACT_MANIFEST
# snapshot file -> the latest/ file name the VKG entrypoint loads (first *.ttl, *.r2rml, *.sql)
VKG_FILES = {"vkg/ontology.ttl": "ontology.ttl", "mappings.r2rml": "mappings.r2rml", "vkg/schema.sql": "schema.sql"}
_CONTENT_TYPES = {
    ".ttl": "text/turtle",
    ".r2rml": "text/turtle",
    ".json": "application/json",
    ".sql": "text/sql",
}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"
# S3 answers a conditional put on a taken key with PreconditionFailed (412), or with
# ConditionalRequestConflict (409) when it races a concurrent write of the same key.
_WRITE_CONFLICT_CODES = ("PreconditionFailed", "ConditionalRequestConflict")


class SnapshotIncompleteError(Exception):
    """The snapshot's manifest is absent or unreadable, so the snapshot cannot be used."""


class SnapshotFileMissingError(Exception):
    """A snapshot file that must exist is not in S3."""


class SnapshotExistsError(Exception):
    """A snapshot object already exists; version snapshots are immutable."""


def _checked_name(name: str) -> str:
    if ".." in name or name.startswith("/"):
        raise ValueError(f"Invalid snapshot file name {name!r}: must be relative and must not contain '..'")
    return name


def version_key(namespace: str, version: int, name: str) -> str:
    """Return the S3 key of one snapshot file."""
    validate_namespace_name(namespace)
    return f"{PREFIX}/{namespace}/versions/v{version}/{_checked_name(name)}"


def latest_key(namespace: str, name: str) -> str:
    """Return the S3 key of one ``latest/`` file."""
    validate_namespace_name(namespace)
    return f"{PREFIX}/{namespace}/latest/{_checked_name(name)}"


def _content_type(name: str) -> str:
    return next((ctype for suffix, ctype in _CONTENT_TYPES.items() if name.endswith(suffix)), _DEFAULT_CONTENT_TYPE)


def _put(key: str, body: str, content_type: str) -> None:
    """Create one immutable snapshot object; raise SnapshotExistsError if the key is taken."""
    try:
        dynamo_store._get_s3().put_object(
            Bucket=dynamo_store._S3_BUCKET,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType=content_type,
            IfNoneMatch="*",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in _WRITE_CONFLICT_CODES:
            raise SnapshotExistsError(f"s3://{dynamo_store._S3_BUCKET}/{key} already exists") from e
        raise


def _get(key: str) -> str | None:
    try:
        resp = dynamo_store._get_s3().get_object(Bucket=dynamo_store._S3_BUCKET, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    body: bytes = resp["Body"].read()
    return body.decode("utf-8")


def write_snapshot(namespace: str, version: int, files: dict[str, str], manifest_json: str) -> None:
    """Write every snapshot file, then the manifest: its presence marks the snapshot complete.

    Every object is created with ``If-None-Match: *``, so an existing snapshot is never overwritten;
    a key that is already taken raises ``SnapshotExistsError``.
    """
    for name in sorted(files):
        _put(version_key(namespace, version, name), files[name], _content_type(name))
    _put(version_key(namespace, version, MANIFEST), manifest_json, "application/json")


def read_version_file(namespace: str, version: int, name: str) -> str | None:
    """Return one snapshot file's text, or None (use for optional files)."""
    return _get(version_key(namespace, version, name))


def require_version_file(namespace: str, version: int, name: str) -> str:
    """Return one snapshot file's text; raise ``SnapshotFileMissingError`` when it is absent."""
    text = read_version_file(namespace, version, name)
    if text is None:
        raise SnapshotFileMissingError(f"Snapshot {namespace} v{version} has no file {name!r}")
    return text


def read_manifest(namespace: str, version: int) -> dict[str, Any] | None:
    """Return the parsed manifest, or None when the snapshot is absent or incomplete.

    Raises ``SnapshotIncompleteError`` when a manifest exists but is not a JSON object with a
    ``files`` object.
    """
    text = _get(version_key(namespace, version, MANIFEST))
    if text is None:
        return None
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as e:
        raise SnapshotIncompleteError(f"Snapshot {namespace} v{version} has an unreadable manifest") from e
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise SnapshotIncompleteError(f"Snapshot {namespace} v{version} manifest is not an object with 'files'")
    return manifest


def read_latest(namespace: str, name: str) -> str | None:
    """Return one ``latest/`` file's text, or None."""
    return _get(latest_key(namespace, name))


def presign_version_file(namespace: str, version: int, name: str, expires_in: int = 3600) -> str:
    """Return a presigned GET URL that downloads one snapshot file as an attachment."""
    filename = f"{namespace}-v{version}-{name.replace('/', '-')}"
    url: str = dynamo_store._get_s3().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": dynamo_store._S3_BUCKET,
            "Key": version_key(namespace, version, name),
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
        },
        ExpiresIn=expires_in,
    )
    return url


def publish_to_latest(namespace: str, version: int) -> None:
    """Make the VKG files in ``latest/`` an exact copy of the snapshot's (absent ones are deleted).

    Only the three VKG file names (``VKG_FILES``) are managed; other ``latest/`` files such as
    ``examples.json`` are left alone. The manifest is written last, so its presence proves every
    listed source file exists: a missing manifest raises ``SnapshotIncompleteError`` before
    ``latest/`` is touched.

    Not atomic but idempotent: a failure part-way can leave ``latest/`` as a mix of the old and new
    version. Callers must retry until it succeeds and must not emit ``ontology.deployed`` after a failure.
    """
    manifest = read_manifest(namespace, version)
    if manifest is None:
        raise SnapshotIncompleteError(f"Snapshot {namespace} v{version} has no manifest")
    listed = manifest["files"]
    s3 = dynamo_store._get_s3()
    bucket = dynamo_store._S3_BUCKET
    for src, dst in VKG_FILES.items():
        if src in listed:
            s3.copy_object(
                Bucket=bucket,
                Key=latest_key(namespace, dst),
                CopySource={"Bucket": bucket, "Key": version_key(namespace, version, src)},
            )
        else:
            s3.delete_object(Bucket=bucket, Key=latest_key(namespace, dst))


def clear_latest(namespace: str) -> None:
    """Delete the three VKG files from ``latest/`` (other files such as ``examples.json`` stay).

    Callers then emit ``ontology.deployed`` with ``version`` null, on which the VKG reload
    Lambda scales the namespace's VKG service to zero; the next deploy of a version scales
    it back up.
    """
    s3 = dynamo_store._get_s3()
    for dst in VKG_FILES.values():
        s3.delete_object(Bucket=dynamo_store._S3_BUCKET, Key=latest_key(namespace, dst))
