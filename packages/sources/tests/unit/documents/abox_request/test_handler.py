# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the document pipeline's ABox request Lambda (abox_request/handler.py)."""

from __future__ import annotations

import io
import json
import logging
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest
from coa_sources.documents.abox_request import handler

pytestmark = pytest.mark.unit

EVENT = {"namespace": "11111111-2222-3333-4444-555555555555", "docSourceId": "ds-1", "reason": "documents ingested"}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ONTOLOGY_ENGINE_ENDPOINT", "http://ontology-engine.svc.local:8001/")
    monkeypatch.setattr(handler.time, "sleep", lambda s: None)


def test_posts_the_request_to_the_internal_endpoint():
    with patch.object(handler.urllib_request, "urlopen", return_value=_Resp(b'{"requested": true}')) as op:
        assert handler.handler(EVENT, None) == {"requested": True}
    req = op.call_args.args[0]
    assert req.full_url == "http://ontology-engine.svc.local:8001/internal/abox/requests"
    assert req.get_method() == "POST"
    assert json.loads(req.data) == {
        "namespace": EVENT["namespace"],
        "reason": "documents ingested",
        "docSourceId": "ds-1",
    }
    assert "X-coa-principal" not in req.headers  # internal callers never send it


def test_retries_then_raises():
    with (
        patch.object(handler.urllib_request, "urlopen", side_effect=URLError("down")) as op,
        pytest.raises(RuntimeError, match="down"),
    ):
        handler.handler(EVENT, None)
    assert op.call_count == 2


def _http_error(code: int, body: bytes) -> HTTPError:
    return HTTPError("http://ontology-engine.svc.local:8001/internal/abox/requests", code, "err", {}, io.BytesIO(body))


def test_client_error_fails_fast_and_logs_the_detail(caplog):
    err = _http_error(422, b'{"detail": "namespace must be a UUID"}')
    with (
        patch.object(handler.urllib_request, "urlopen", side_effect=err) as op,
        caplog.at_level(logging.WARNING),
        pytest.raises(RuntimeError, match="422"),
    ):
        handler.handler(EVENT, None)
    assert op.call_count == 1
    assert "namespace must be a UUID" in caplog.text


def test_client_error_detail_is_truncated(caplog):
    err = _http_error(400, b"x" * 2000)
    with (
        patch.object(handler.urllib_request, "urlopen", side_effect=err),
        caplog.at_level(logging.WARNING),
        pytest.raises(RuntimeError),
    ):
        handler.handler(EVENT, None)
    assert "x" * 500 in caplog.text
    assert "x" * 501 not in caplog.text


@pytest.mark.parametrize("code", [408, 429, 503])
def test_transient_http_error_is_retried_then_succeeds(code, caplog):
    results = [_http_error(code, b"overloaded"), _Resp(b'{"requested": true}')]
    with (
        patch.object(handler.urllib_request, "urlopen", side_effect=results) as op,
        caplog.at_level(logging.WARNING),
    ):
        assert handler.handler(EVENT, None) == {"requested": True}
    assert op.call_count == 2
    assert "overloaded" in caplog.text


def test_missing_doc_source_id_sends_null():
    event = {"namespace": EVENT["namespace"]}
    with patch.object(handler.urllib_request, "urlopen", return_value=_Resp(b'{"requested": false}')) as op:
        assert handler.handler(event, None) == {"requested": False}
    assert json.loads(op.call_args.args[0].data) == {
        "namespace": EVENT["namespace"],
        "reason": "documents",
        "docSourceId": None,
    }


def test_missing_endpoint_raises(monkeypatch):
    monkeypatch.delenv("ONTOLOGY_ENGINE_ENDPOINT")
    with pytest.raises(RuntimeError, match="ONTOLOGY_ENGINE_ENDPOINT"):
        handler.handler(EVENT, None)
