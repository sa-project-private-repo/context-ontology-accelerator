# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Only the query path builds the Tier 3 lexical retriever and the deep-reasoning tool registry.

Building them imports graphrag-toolkit, which takes seconds, and a data-layer or MCP request
carries no runtime session id, so each one starts a new runtime session that initializes serve
from scratch. The isolated actions (the query console's ``sparqlQuery`` among them) use neither.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coa_serve.config import ServiceConfig
from coa_serve.models import ConfidenceScore, InvokeResponse, QueryResult, TraceStep
from coa_serve.orchestrator import Orchestrator
from coa_serve.role_resolver import ResolvedProfile
from coa_serve.tier2.ontop.sparql_validator import SPARQLValidator
from coa_serve.tier3.agentic.tools.registry import ToolRegistry

pytestmark = pytest.mark.unit

NS = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
UNKNOWN = "http://example.org/shop#NoSuchClass"
REFUSAL = f"<{UNKNOWN}> is not a class or property of the deployed ontology."
_CONFIG = ServiceConfig(
    guardrail_id="",
    vkg_endpoint="http://vkg.local:8080",
    neptune_endpoint="neptune.local",
    opensearch_endpoint="aoss.local",
    bedrock_model_id="test-model",
    bedrock_region="us-east-1",
    data_sources_table="",
    metric_definitions_table="",
    memory_id="",
    session_metadata_table="",
)
# The module globals the two init steps set, as a new runtime session finds them.
_UNSET = dict.fromkeys(
    (
        "_config",
        "_guardrail_provider",
        "_orchestrator",
        "_session_manager",
        "_session_metadata",
        "_sources_registry",
        "_nl_to_sparql",
        "_vkg_translator",
        "_sparql_validator",
        "_query_path_builder",
    )
)


async def _invoke(payload: dict) -> dict:
    from coa_serve.main import invoke

    return [item async for item in invoke(payload, None)][0]


def _answer(_orchestrator, request, **_kwargs) -> InvokeResponse:
    return InvokeResponse(
        result=QueryResult(
            tier=2,
            confidence=ConfidenceScore(score=0.9, rationale="ok"),
            trace=[TraceStep(step="s", status="success", duration_ms=0)],
            metadata={},
        )
    )


@pytest.fixture
def new_session():
    """Serve as a new runtime session starts it: nothing built yet, the two builders recorded, no AWS call."""
    from coa_serve import main

    builders = SimpleNamespace(
        lexical=MagicMock(name="build_lexical_retriever", return_value=MagicMock(name="lexical_retriever")),
        registry=MagicMock(name="build_tool_registry", return_value=ToolRegistry()),
    )
    with (
        patch.multiple(main, **_UNSET),
        patch.dict(os.environ, {"NAMESPACES_TABLE": "", "ONTOLOGY_BUCKET": "", "OSS_ONTOLOGY_INDEX": ""}),
        patch.object(main, "load_config", return_value=_CONFIG),
        patch.object(main, "build_guardrail_config_provider", return_value=MagicMock(name="guardrail_provider")),
        patch("coa_serve.tier1.metric_resolver.MetricResolver.start", AsyncMock()),
        patch("coa_serve.clients.factory.build_lexical_retriever", builders.lexical),
        patch("coa_serve.tier3.agentic.factory.build_tool_registry", builders.registry),
        patch.object(
            main, "_authorize_namespace_access", AsyncMock(return_value=(ResolvedProfile(user_id="alice"), None))
        ),
    ):
        yield builders


async def test_a_console_query_on_an_unknown_class_is_refused_without_building_either(new_session):
    with patch.object(SPARQLValidator, "refuse_vkg_terms", AsyncMock(return_value=REFUSAL)):
        result = await _invoke(
            {"action": "sparqlQuery", "namespace": NS, "sparql": f"SELECT ?s WHERE {{ ?s a <{UNKNOWN}> }}"}
        )
    assert result["statusCode"] == 400 and result["message"] == REFUSAL
    new_session.lexical.assert_not_called()
    new_session.registry.assert_not_called()


@pytest.mark.parametrize(
    ("action", "handler", "fields"),
    [
        ("translate", "_handle_translate", {"query": "How many orders shipped?"}),
        ("kbSearch", "_handle_kb_search", {"query": "How many orders shipped?"}),
        ("graphTraverse", "_handle_graph_traverse", {"options": {"startUri": "http://example.org/shop#Order"}}),
    ],
)
async def test_the_other_isolated_actions_build_neither(new_session, action, handler, fields):
    from coa_serve import main

    answer = {"statusCode": 200}
    with patch.object(main, handler, AsyncMock(return_value=answer)) as handle:
        result = await _invoke({"action": action, "namespace": NS, **fields})
    assert result == answer
    handle.assert_awaited_once()
    new_session.lexical.assert_not_called()
    new_session.registry.assert_not_called()


async def test_a_query_builds_both_once_and_is_resolved_with_them(new_session):
    """Also after an isolated action has initialized the session without them."""
    with patch.object(SPARQLValidator, "refuse_vkg_terms", AsyncMock(return_value=REFUSAL)):
        await _invoke({"action": "sparqlQuery", "namespace": NS, "sparql": f"SELECT ?s WHERE {{ ?s a <{UNKNOWN}> }}"})
    with patch.object(Orchestrator, "resolve", autospec=True, side_effect=_answer) as resolve:
        first = await _invoke({"query": "How many orders shipped?", "namespace": NS})
        second = await _invoke({"query": "How many orders shipped?", "namespace": NS})
    assert first["result"]["tier"] == 2 and second["result"]["tier"] == 2
    assert new_session.lexical.call_count == 1 and new_session.registry.call_count == 1
    orchestrator = resolve.call_args.args[0]
    assert orchestrator._knowledge_retriever.lexical_enabled
    assert orchestrator._agentic_retriever is not None


async def test_a_failed_build_is_tried_again_by_the_next_query(new_session):
    new_session.lexical.side_effect = [RuntimeError("graph store unreachable"), new_session.lexical.return_value]
    with patch.object(Orchestrator, "resolve", autospec=True, side_effect=_answer):
        failed = await _invoke({"query": "How many orders shipped?", "namespace": NS})
        answered = await _invoke({"query": "How many orders shipped?", "namespace": NS})
    assert failed == {"error": "InternalError", "message": "Initialization failed", "statusCode": 500}
    assert answered["result"]["tier"] == 2
    assert new_session.lexical.call_count == 2 and new_session.registry.call_count == 1
