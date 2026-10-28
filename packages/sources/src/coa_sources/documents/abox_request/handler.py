# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ask ontology-engine to rebuild the namespace's domain ABox after a document pipeline run.

Invoked as the last state of the document ingestion and deletion Step Functions. The call
goes to ontology-engine's VPC-only ``/internal/abox/requests`` over Cloud Map. A failure
raises so the state's Catch records it; the pipeline itself has already succeeded.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ATTEMPTS = int(os.environ.get("ABOX_REQUEST_ATTEMPTS", "2"))
_TIMEOUT = float(os.environ.get("ABOX_REQUEST_TIMEOUT", "10"))
# 4xx responses that say "try again", unlike the rest of the 4xx range.
_RETRYABLE_CLIENT_ERRORS = frozenset({408, 429})


def _error_detail(e: HTTPError) -> str:
    """The response body of a failed request, for the log. Truncated so a proxy error page cannot flood it."""
    try:
        return e.read().decode("utf-8", errors="replace")[:500]
    except OSError:
        return ""


def handler(event: dict[str, Any], _context: Any) -> dict[str, bool]:
    """Step Functions task handler: POST the namespace's rebuild request.

    Retries transport errors, 5xx, 408 and 429; any other 4xx response raises at once.
    """
    endpoint = os.environ.get("ONTOLOGY_ENGINE_ENDPOINT", "").rstrip("/")
    if not endpoint:
        raise RuntimeError("ONTOLOGY_ENGINE_ENDPOINT is not set")
    body = json.dumps(
        {
            "namespace": event["namespace"],
            "reason": event.get("reason", "documents"),
            "docSourceId": event.get("docSourceId"),
        }
    ).encode("utf-8")
    last: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        req = urllib_request.Request(
            f"{endpoint}/internal/abox/requests",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib_request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 - VPC-internal Cloud Map endpoint
                result = json.loads(resp.read().decode("utf-8") or "{}")
            logger.info("abox request for %s: %s", event["namespace"], result)
            return {"requested": bool(result.get("requested"))}
        except HTTPError as e:
            last = e
            detail = _error_detail(e)
            if 400 <= e.code < 500 and e.code not in _RETRYABLE_CLIENT_ERRORS:
                # The request itself is wrong, so a retry would fail the same way.
                logger.error("abox request rejected: HTTP %d: %s", e.code, detail)
                raise RuntimeError(f"abox request rejected: HTTP {e.code}: {detail}") from e
            logger.warning("abox request attempt %d/%d failed: HTTP %d: %s", attempt, _ATTEMPTS, e.code, detail)
            if attempt < _ATTEMPTS:
                time.sleep(1)
        except (URLError, OSError) as e:
            last = e
            logger.warning("abox request attempt %d/%d failed: %s", attempt, _ATTEMPTS, e)
            if attempt < _ATTEMPTS:
                time.sleep(1)
    raise RuntimeError(f"abox request failed: {last}")
