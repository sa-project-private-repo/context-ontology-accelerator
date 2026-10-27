# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deletion pipeline step: delete all ontology data + S3 artifacts.

Reuses the existing ontology-engine HTTP teardown and S3 sweep from
``cleanup.py`` — this step just runs them as their own Step Functions task
so the ontology-engine's slow, best-effort HTTP call (up to 300s) has its
own retry policy separate from the rest of the pipeline.
"""

from __future__ import annotations

from typing import Any

import structlog

from coa_control_plane.namespace.cleanup import (
    delete_all_ontologies,
    delete_ontology_artifacts,
)

logger = structlog.get_logger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Step Functions task: delete all ontology data for the namespace.

    Input: {"namespaceId": str}
    Output: {"namespaceId": str}

    ``delete_all_ontologies`` RAISES on failure and is not caught here: it owns
    the AOSS vector index, which is one index per namespace against a
    collection with a hard 1000-index cap. Because a later step deletes the
    namespace record, a swallowed failure here orphans that index forever —
    nothing can target a namespace that no longer exists — and the deployment
    eventually fails every embedding write with ``index_limit_breached``.
    Letting it raise means the step's own retry policy retries it and, on
    exhaustion, the pipeline catch lands the namespace in DELETE_FAILED, which
    is recoverable. A silent, unreclaimable leak is not.

    ``delete_ontology_artifacts`` (S3) stays best-effort — orphaned S3 objects
    cost storage but consume no quota that can wedge the deployment.
    """
    namespace_id = event["namespaceId"]
    delete_all_ontologies(namespace_id)
    delete_ontology_artifacts(namespace_id)
    return {"namespaceId": namespace_id}
