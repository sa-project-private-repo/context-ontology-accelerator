# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The API proxy forwards the Ontology designer routes (kept apart from test_api_proxy_handler.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.unit]
STUDIO = "/namespaces/{namespaceId}/proposals/{proposalId}/studio"
SUFFIXES = [
    "",
    "/graph",
    "/term",
    "/terms",
    "/changes",
    "/undo",
    "/redo",
    "/history",
    "/rebase",
    "/accept-preview",
    "/messages",
    "/events",
    "/changesets",
    "/changesets/{changeSetId}/preview",
    "/changesets/{changeSetId}/apply",
    "/changesets/{changeSetId}/discard",
    "/checks",
    "/queries",
    "/questions/{questionId}/check",
]
PROPOSAL = "0f1e2d3c-4b5a-4968-8776-655443322110"
NAMESPACE = "11111111-1111-4111-8111-111111111111"


@pytest.fixture()
def mod(monkeypatch):
    monkeypatch.setenv("ONTOLOGY_ENGINE_ENDPOINT", "http://ontology-engine.ns:8001")
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://app.example.com")
    from coa_ontology import api_proxy_handler

    return api_proxy_handler


@pytest.mark.parametrize("suffix", SUFFIXES)
def test_every_studio_route_is_mapped(mod, suffix):
    assert mod._PATH_MAP[f"{STUDIO}{suffix}"] == f"/ontology/proposals/{{proposalId}}/studio{suffix}"


def test_the_namespace_s_questions_are_mapped(mod):
    assert mod._PATH_MAP["/namespaces/{namespaceId}/studio/questions"] == "/ontology/studio/questions"
    question = "/namespaces/{namespaceId}/studio/questions/{questionId}"
    assert mod._PATH_MAP[question] == "/ontology/studio/questions/{questionId}"


@patch("urllib.request.urlopen")
def test_a_term_iri_with_a_fragment_reaches_the_engine_intact(mock_urlopen, mod):
    resp = MagicMock(status=200)
    resp.read.return_value = b"{}"
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = resp
    event = {
        "httpMethod": "GET",
        "resource": f"{STUDIO}/term",
        "pathParameters": {"namespaceId": NAMESPACE, "proposalId": PROPOSAL},
        "queryStringParameters": {"iri": "http://e/o#Customer"},
        "requestContext": {"authorizer": {"principalId": "alice@example.com"}},
        "body": None,
    }
    mod.handler(event, None)
    url = mock_urlopen.call_args[0][0].full_url
    # ENDPOINT is read when the module is first imported, possibly by another test.
    assert url == (
        f"{mod.ENDPOINT}/ontology/proposals/{PROPOSAL}/studio/term?iri=http%3A%2F%2Fe%2Fo%23Customer&namespace={NAMESPACE}"
    )


@patch("urllib.request.urlopen")
def test_a_change_set_id_is_filled_into_the_engine_path(mock_urlopen, mod):
    resp = MagicMock(status=200)
    resp.read.return_value = b"{}"
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = resp
    event = {
        "httpMethod": "POST",
        "resource": f"{STUDIO}/changesets/{{changeSetId}}/apply",
        "pathParameters": {"namespaceId": NAMESPACE, "proposalId": PROPOSAL, "changeSetId": "20261020T0000Z-ab12cd34"},
        "queryStringParameters": None,
        "requestContext": {"authorizer": {"principalId": "alice@example.com"}},
        "body": '{"expectedRevision": 3}',
    }
    mod.handler(event, None)
    url = mock_urlopen.call_args[0][0].full_url
    path = f"/ontology/proposals/{PROPOSAL}/studio/changesets/20261020T0000Z-ab12cd34/apply"
    assert url == f"{mod.ENDPOINT}{path}?namespace={NAMESPACE}"
