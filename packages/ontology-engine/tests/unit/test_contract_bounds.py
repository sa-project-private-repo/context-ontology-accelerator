# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The bounds the ontology engine enforces must equal the ones its Smithy models declare.

Two halves:

* **Parity** — parses ``ontology-induction.smithy`` / ``ontology-graph.smithy``
  (the source of truth, not the generated OpenAPI, so this proves the model and
  the runtime agree rather than that codegen ran) and asserts every runtime
  constant matches its ``@range`` max, enum, or documented bound. Edit the model
  and forget the constant, or the reverse, and this goes red.
* **Enforcement / compatibility** — out-of-contract values are rejected (never
  clamped) without echoing them, and the new ``@pattern`` constraints accept
  what callers send today and nothing the runtime would itself refuse.
"""

from __future__ import annotations

import json
import re
import string
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from coa_ontology import induce_catalog, proposals
from coa_ontology.abox import mapping as abox_mapping
from coa_ontology.abox import router as abox_router
from coa_ontology.catalog import ingest
from coa_ontology.catalog.routers import graph, ontologies
from coa_ontology.main import app
from coa_ontology.stores.neptune_db_graph import _iri
from coa_ontology.studio import agent as studio_agent
from coa_ontology.studio import changes as studio_changes
from coa_ontology.studio import changesets as studio_changesets
from coa_ontology.studio import chat_store as studio_chat_store
from coa_ontology.studio import checks as studio_checks
from coa_ontology.studio import draft as studio_draft
from coa_ontology.studio import graph_view as studio_graph_view
from coa_ontology.studio import iris as studio_iris
from coa_ontology.studio import messages as studio_messages
from coa_ontology.studio import query_safety as studio_query_safety
from coa_ontology.studio import questions as studio_questions
from coa_ontology.studio import router as studio_router
from coa_ontology.versions import deploy as version_deploy
from coa_ontology.versions import router as versions_router
from fastapi.testclient import TestClient

from . import studio_fakes

pytestmark = pytest.mark.unit

_SMITHY_DIR = Path(__file__).resolve().parents[4] / "models" / "src" / "main" / "smithy"
_OPENAPI = (
    Path(__file__).resolve().parents[4]
    / "smithy-generated"
    / "openapi"
    / "openapi"
    / "ControlPlaneService.openapi.json"
)

client = TestClient(app)


# ── Minimal Smithy reader ────────────────────────────────────────────────────


def _model(name: str) -> str:
    path = _SMITHY_DIR / name
    assert path.is_file(), f"{name} not found at {path}"
    return path.read_text()


def _input_member_traits(text: str, operation: str, member: str) -> list[str]:
    """Trait lines (and doc lines) attached to ``member`` of ``operation``'s inline input."""
    op = re.search(rf"operation\s+{operation}\s*\{{", text)
    assert op, f"operation {operation} not found"
    inp = re.search(r"input\s*:=\s*(?:with\s*\[[^\]]*\]\s*)?\{", text[op.end() :])
    assert inp, f"{operation} input block not found"
    traits: list[str] = []
    depth = 1
    for line in text[op.end() + inp.end() :].splitlines():
        stripped = line.strip()
        depth += stripped.count("{") - stripped.count("}")
        if depth <= 0:
            break
        if stripped.startswith(("@", "///")):
            traits.append(stripped)
        elif m := re.match(r"([A-Za-z_]\w*)\s*:", stripped):
            if m.group(1) == member:
                return traits
            traits = []
    raise AssertionError(f"{operation}.{member} not found")


def _range_max(text: str, operation: str, member: str) -> int:
    for trait in _input_member_traits(text, operation, member):
        if m := re.match(r"@range\(.*max:\s*(\d+)", trait):
            return int(m.group(1))
    raise AssertionError(f"{operation}.{member} has no @range max")


def _enum_values(text: str, shape: str) -> set[str]:
    m = re.search(rf"enum\s+{shape}\s*\{{(.*?)\n\}}", text, re.S)
    assert m, f"enum {shape} not found"
    return set(re.findall(r'=\s*"([^"]+)"', m.group(1)))


_INDUCTION = _model("ontology-induction.smithy")
_GRAPH = _model("ontology-graph.smithy")
_ABOX = _model("domain-abox.smithy")
_STUDIO = _model("graph-studio.smithy")


# ── Parity ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "operation", "member", "constant"),
    [
        (_INDUCTION, "ListProposals", "maxResults", proposals.LIST_PROPOSALS_MAX_RESULTS),
        (_INDUCTION, "ListInductionJobs", "maxResults", induce_catalog.LIST_INDUCTION_JOBS_MAX_RESULTS),
        (_INDUCTION, "ListInducedDatasources", "maxResults", induce_catalog.LIST_INDUCED_DATASOURCES_MAX_RESULTS),
        (_GRAPH, "ListOntologies", "maxResults", ontologies.LIST_ONTOLOGIES_MAX_RESULTS),
        (_GRAPH, "ListOntologyVersions", "maxResults", versions_router.LIST_VERSIONS_MAX_RESULTS),
        (_GRAPH, "ListOntologies", "limit", ontologies.LIST_ONTOLOGIES_MAX_RESULTS),
        (_GRAPH, "GraphSearch", "limit", graph._MAX_SEARCH_LIMIT),
        (_GRAPH, "GraphSearch", "offset", graph._MAX_SEARCH_OFFSET),
        (_ABOX, "ListDomainInstances", "maxResults", abox_router.LIST_INSTANCES_MAX_RESULTS),
        (_ABOX, "ListAboxMappings", "maxResults", abox_router.LIST_MAPPINGS_MAX_RESULTS),
        (_STUDIO, "ListStudioHistory", "maxResults", studio_router.LIST_HISTORY_MAX_RESULTS),
        (_STUDIO, "SearchStudioTerms", "limit", studio_graph_view.MAX_SEARCH),
        (_STUDIO, "ListStudioChangeSets", "maxResults", studio_router.LIST_CHANGE_SETS_MAX_RESULTS),
        (_STUDIO, "ListStudioEvents", "limit", studio_chat_store.MAX_EVENTS_PAGE),
        (_STUDIO, "ListStudioEvents", "after", studio_chat_store.MAX_SEQ),
        (_STUDIO, "ListStudioQuestions", "maxResults", studio_router.LIST_QUESTIONS_MAX_RESULTS),
    ],
    ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "",
)
def test_runtime_page_bound_equals_the_model(text, operation, member, constant):
    assert constant == _range_max(text, operation, member)


def _shape_length_max(text: str, shape: str) -> int:
    m = re.search(rf"@length\(min:\s*\d+,\s*max:\s*(\d+)\)\s*\nstring\s+{shape}\b", text)
    assert m, f"string {shape} with @length not found"
    return int(m.group(1))


def _length_max(text: str, operation: str, member: str) -> int:
    for trait in _input_member_traits(text, operation, member):
        if m := re.match(r"@length\(.*max:\s*(\d+)", trait):
            return int(m.group(1))
    raise AssertionError(f"{operation}.{member} has no @length max")


@pytest.mark.parametrize("member", ["groundingOverrides", "novelTables"])
def test_grounding_choice_count_bound_equals_the_model(member):
    assert _length_max(_INDUCTION, "UpdateProposal", member) == proposals.MAX_GROUNDING_CHOICES


def test_grounding_key_and_target_bounds_equal_the_model():
    assert _shape_length_max(_INDUCTION, "GroundingSourceTableKey") == proposals.MAX_GROUNDING_SOURCE_TABLE_KEY_LENGTH
    assert _shape_length_max(_INDUCTION, "GroundingOverrideTargetUri") == proposals.MAX_GROUNDING_TARGET_URI_LENGTH


@pytest.mark.parametrize(
    "body",
    [
        {"groundingOverrides": {f"t{i}": "https://example.com/C" for i in range(1001)}},
        {"novelTables": [f"t{i}" for i in range(1001)]},
        {"groundingOverrides": {"": "https://example.com/C"}},
        {"groundingOverrides": {"t": "https://example.com/" + "x" * 2048}},
        {"novelTables": ["x" * 1025]},
    ],
    ids=["too-many-overrides", "too-many-novel", "empty-key", "long-target", "long-novel-key"],
)
def test_out_of_bound_grounding_choices_are_rejected(body):
    with pytest.raises(ValueError):
        proposals.UpdateProposalRequest.model_validate({"proposalId": "p-1", **body})


def test_constraint_config_bound_equals_the_documented_one():
    doc = " ".join(_input_member_traits(_INDUCTION, "CompileConstraints", "constraint_config"))
    m = re.search(r"At most (\d+)\s+bytes", doc)
    assert m, "CompileConstraints.constraint_config no longer documents its size bound"
    assert int(m.group(1)) == proposals.MAX_CONSTRAINT_CONFIG_BYTES


def test_ontology_version_bounds_equal_the_model():
    version_range = re.search(r"@range\(min:\s*1,\s*max:\s*(\d+)\)\s*\ninteger\s+OntologyVersionNumber\b", _GRAPH)
    assert version_range, "integer OntologyVersionNumber with @range not found"
    assert int(version_range.group(1)) == versions_router.MAX_VERSION_NUMBER
    assert _shape_length_max(_GRAPH, "OntologyDeploymentId") == versions_router.MAX_DEPLOYMENT_ID_LENGTH
    assert _shape_length_max(_GRAPH, "BreakingMetricId") == versions_router.MAX_BREAKING_METRIC_ID_LENGTH
    metric_list = re.search(r"@length\(max:\s*(\d+)\)\s*\nlist\s+BreakingMetricIdList\b", _GRAPH)
    assert metric_list, "list BreakingMetricIdList with @length not found"
    assert int(metric_list.group(1)) == versions_router.MAX_ACKNOWLEDGED_BREAKING_METRICS


def test_domain_abox_bounds_equal_the_model():
    assert _length_max(_ABOX, "ListDomainInstances", "q") == abox_router.MAX_QUERY_LENGTH
    assert _length_max(_ABOX, "UpdateAboxMapping", "target") == abox_router.MAX_TARGET_LENGTH
    iri = re.search(r"@length\(min:\s*1,\s*max:\s*(\d+)\)\s*\n@pattern\(.*\)\s*\nstring VertexIri\b", _GRAPH)
    assert iri, "string VertexIri with @length not found"
    assert int(iri.group(1)) == abox_router.MAX_IRI_LENGTH
    doc = " ".join(_input_member_traits(_ABOX, "ListDomainInstances", "nextToken"))
    assert re.search(r"at most (\d+) instances", doc).group(1) == str(abox_router.MAX_OFFSET)
    pattern = re.search(r'@pattern\("(.+)"\)\s*\nstring AboxMappingId\b', _ABOX)
    assert pattern, "string AboxMappingId with @pattern not found"
    assert pattern.group(1).replace("\\\\", "\\") == "^" + abox_mapping._ID.pattern + "$"


def test_ontology_designer_bounds_equal_the_model():
    revision = re.search(r"@range\(min:\s*0,\s*max:\s*(\d+)\)\s*\ninteger StudioRevision\b", _STUDIO)
    assert revision, "integer StudioRevision with @range not found"
    assert int(revision.group(1)) == studio_draft.MAX_REVISION
    ops = re.search(r"@length\(min:\s*1,\s*max:\s*(\d+)\)\s*\nlist StudioOpList\b(.*?)\n\}", _STUDIO, re.S)
    assert ops, "list StudioOpList with @length not found"
    assert int(ops.group(1)) == studio_changes.MAX_OPS
    assert re.search(r"at most (\d+) bytes", ops.group(2)).group(1) == str(studio_router.MAX_CHANGE_SET_BYTES)
    skip = re.search(r"@length\(max:\s*(\d+)\)\s*\nlist StudioOpIdList\b", _STUDIO)
    assert skip, "list StudioOpIdList with @length not found"
    assert int(skip.group(1)) == studio_router.MAX_SKIP_OPS
    assert _shape_length_max(_STUDIO, "StudioOpId") == studio_router.MAX_OP_ID_LENGTH
    iri = re.search(r"@length\(min:\s*1,\s*max:\s*(\d+)\)\s*\n@pattern\(.*\)\s*\nstring VertexIri\b", _GRAPH)
    assert int(iri.group(1)) == studio_iris.MAX_IRI_LENGTH


def test_ontology_designer_query_and_question_bounds_equal_the_model():
    assert _length_max(_STUDIO, "RunStudioQuery", "query") == studio_query_safety.MAX_QUERY_CHARS
    assert _length_max(_STUDIO, "PutStudioQuestion", "text") == studio_questions.MAX_TEXT
    assert _length_max(_STUDIO, "PutStudioQuestion", "expectation") == studio_questions.MAX_EXPECTATION
    assert _shape_length_max(_STUDIO, "StudioQuestionId") == 64
    question_id = re.search(r'@pattern\("\^(.+)\$"\)\s*\n@length\([^)]*\)\s*\nstring StudioQuestionId\b', _STUDIO)
    assert question_id, "string StudioQuestionId with @pattern not found"
    assert question_id.group(1) == studio_questions._ID.pattern


def test_ontology_designer_assistant_bounds_equal_the_model():
    assert _length_max(_STUDIO, "SendStudioMessage", "text") == studio_agent.MAX_TEXT
    assert _length_max(_STUDIO, "SendStudioMessage", "selection") == studio_agent.MAX_SELECTION
    assert _length_max(_STUDIO, "PreviewStudioChangeSet", "opIndexes") == studio_router.MAX_OP_INDEXES_QUERY
    index = re.search(r"@range\(min:\s*0,\s*max:\s*(\d+)\)\s*\ninteger StudioOpIndex\b", _STUDIO)
    assert index, "integer StudioOpIndex with @range not found"
    assert int(index.group(1)) == studio_changesets.MAX_AGENT_OPS - 1
    indexes = re.search(r"@length\(min:\s*1,\s*max:\s*(\d+)\)\s*\nlist StudioOpIndexList\b", _STUDIO)
    assert indexes, "list StudioOpIndexList with @length not found"
    assert int(indexes.group(1)) == studio_changesets.MAX_AGENT_OPS
    cs_id = re.search(
        r'@length\(min:\s*1,\s*max:\s*(\d+)\)\s*\n@pattern\("(.+)"\)\s*\nstring StudioChangeSetId\b', _STUDIO
    )
    assert cs_id, "string StudioChangeSetId with @length and @pattern not found"
    assert cs_id.group(2) == studio_changesets._ID.pattern
    assert cs_id.group(1) == re.search(r"\{1,(\d+)\}", studio_changesets._ID.pattern).group(1)
    page = re.search(r"at most (\d+) bytes of change sets", _STUDIO)
    assert page, "ListStudioChangeSets no longer documents its page byte bound"
    assert int(page.group(1)) == studio_changesets.MAX_PAGE_BYTES


def _structure_member_length_max(text: str, structure: str, member: str) -> int:
    """The ``@length`` max on ``member`` of the top-level ``structure``."""
    body = re.search(rf"\nstructure\s+{structure}\s*\{{(.*?)\n\}}", text, re.S)
    assert body, f"structure {structure} not found"
    traits: list[str] = []
    for line in body.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith(("@", "///")):
            traits.append(stripped)
        elif m := re.match(r"([A-Za-z_]\w*)\s*:", stripped):
            if m.group(1) == member:
                found = [int(t.group(1)) for trait in traits if (t := re.match(r"@length\(.*max:\s*(\d+)", trait))]
                assert found, f"{structure}.{member} has no @length max"
                return found[0]
            traits = []
    raise AssertionError(f"{structure}.{member} not found")


def _list_length_max(text: str, shape: str) -> int:
    m = re.search(rf"@length\(max:\s*(\d+)\)\s*\nlist {shape}\b", text)
    assert m, f"list {shape} with @length not found"
    return int(m.group(1))


def test_ontology_designer_structured_check_bounds_equal_the_model():
    assert _list_length_max(_STUDIO, "StudioStructuredTermList") == studio_checks.MAX_STRUCTURED_TERMS
    assert _structure_member_length_max(_STUDIO, "StudioStructuredTerm", "tables") == studio_checks.MAX_TERM_TABLES
    # Every group of the check, the deletions included, is the bounded StudioStructuredGroup.
    body = re.search(r"\nstructure StudioStructuredCheck\s*\{(.*?)\n\}", _STUDIO, re.S)
    assert body, "structure StudioStructuredCheck not found"
    groups = set(re.findall(r"@required\s*\n\s*(\w+): StudioStructuredGroup\b", body.group(1)))
    assert groups == {
        "unmappedClasses",
        "unmappedProperties",
        "labelDiffers",
        "multiTableClasses",
        "removedMappedClasses",
        "removedMappedProperties",
    }


def test_the_term_view_and_the_model_agree_on_structured_answered():
    """The Inspector reads this member for a class, so a rename on either side has to fail here."""
    output = re.search(r"\noperation GetStudioTerm \{.*?\n    output := \{(.*?)\n    \}", _STUDIO, re.S)
    assert output, "operation GetStudioTerm not found"
    assert re.search(r"\n\s+structuredAnswered: Boolean\b", output.group(1)), "structuredAnswered not declared"
    d = studio_fakes.draft()
    answered = {
        iri: studio_graph_view.term_view(d, studio_fakes.SCHEME, iri, None)["structuredAnswered"]
        for iri in (studio_fakes.CUSTOMER, studio_fakes.ITEM)
    }
    assert answered == {studio_fakes.CUSTOMER: True, studio_fakes.ITEM: False}


def _member_doc(text: str, structure: str, member: str) -> str:
    """The ``///`` lines of ``member`` of the top-level ``structure``, joined."""
    body = re.search(rf"\nstructure\s+{structure}\s*\{{(.*?)\n\}}", text, re.S)
    assert body, f"structure {structure} not found"
    doc: list[str] = []
    for line in body.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("///"):
            doc.append(stripped[3:].strip())
        elif m := re.match(r"([A-Za-z_]\w*)\s*:", stripped):
            if m.group(1) == member:
                return " ".join(doc)
            doc = []
    raise AssertionError(f"{structure}.{member} not found")


def test_ontology_designer_message_bounds_equal_the_model():
    assert _list_length_max(_STUDIO, "StudioMessageList") == studio_messages.MAX_MESSAGES
    params = re.search(r"@length\(max:\s*(\d+)\)\s*\nmap StudioMessageParams\b", _STUDIO)
    assert params, "map StudioMessageParams with @length not found"
    assert int(params.group(1)) == studio_messages.MAX_PARAMS
    assert _shape_length_max(_STUDIO, "StudioMessageCode") == studio_messages.MAX_CODE_LENGTH
    assert _shape_length_max(_STUDIO, "StudioMessageParamName") == studio_messages.MAX_PARAM_NAME_LENGTH


def test_ontology_designer_message_codes_are_the_ones_the_model_documents():
    """A client localizes by the codes the model lists; the engine may send no other."""
    codes = re.compile(r"`([A-Z][A-Z_]+)`")
    summaries = set(codes.findall(_member_doc(_STUDIO, "StudioHistoryEntry", "summaryMessages")))
    findings = set(codes.findall(_member_doc(_STUDIO, "StudioFinding", "messageCode")))
    warnings = set(codes.findall(_member_doc(_STUDIO, "StudioChangeView", "warningMessages")))
    assert not summaries & findings and not (summaries | findings) & warnings
    assert summaries | findings | warnings == set(studio_messages.ENGLISH)
    sections = {
        "StudioTablesCheck",
        "StudioAboxCheck",
        "StudioMappingsCheck",
        "StudioMetricsCheck",
        "StudioStructuredCheck",
    }
    documented = {code for shape in sections for code in codes.findall(_member_doc(_STUDIO, shape, "errorCode"))}
    assert documented == set(studio_checks.ERROR_CODES.values())
    # The accept preview's error is the metrics section's.
    preview = re.search(r"/// The code of `breakingMetricsError`, `(\w+)`.*\n\s*breakingMetricsErrorCode:", _STUDIO)
    assert preview, "GetStudioAcceptPreview breakingMetricsErrorCode not found"
    assert preview.group(1) == studio_checks.ERROR_CODES[studio_router.BREAKING_METRICS_ERROR]


def test_every_designer_sentence_fits_the_message_bounds():
    codes = {*studio_messages.ENGLISH, *studio_checks.ERROR_CODES.values()}
    assert all(re.fullmatch(r"[A-Z][A-Z_]*", c) and len(c) <= studio_messages.MAX_CODE_LENGTH for c in codes)
    for code, english in studio_messages.ENGLISH.items():
        names = {name for _, name, _, _ in string.Formatter().parse(english) if name}
        assert len(names) <= studio_messages.MAX_PARAMS, code
        assert all(len(name) <= studio_messages.MAX_PARAM_NAME_LENGTH for name in names), code


def test_ingest_format_enum_is_exactly_what_the_parser_understands():
    """Every enum value must parse as a real format, and every parser spelling be accepted."""
    assert _enum_values(_GRAPH, "OntologyFileFormat") == set(ingest._RDFLIB_FORMATS)


def test_ingest_ontology_type_enum_covers_what_the_web_app_sends():
    # ``user_uploaded``: services/ontology-engine.ts uploadOntologyFile.
    assert {"foundational", "induced", "user_created", "user_uploaded"} <= _enum_values(_GRAPH, "OntologyType")


# ── Enforcement ──────────────────────────────────────────────────────────────


@pytest.fixture()
def _graph_store():
    store = MagicMock()
    store.search_entities.return_value = []
    store.count_entities.return_value = 0
    with patch("coa_ontology.catalog.routers.graph.build_stores", return_value=(store, MagicMock())):
        yield store


@pytest.mark.parametrize("offset", [-1, graph._MAX_SEARCH_OFFSET + 1])
def test_graph_search_offset_out_of_range_is_400(_graph_store, offset):
    resp = client.get("/graph/search", params={"q": "x", "namespace": "ns", "offset": offset})
    assert resp.status_code == 400
    _graph_store.search_entities.assert_not_called()


def test_graph_search_offset_at_the_bound_is_accepted(_graph_store):
    resp = client.get("/graph/search", params={"q": "x", "namespace": "ns", "offset": graph._MAX_SEARCH_OFFSET})
    assert resp.status_code == 200


def test_oversized_constraint_config_is_400_before_any_read():
    big = {"constraints": ["x" * 1024] * (proposals.MAX_CONSTRAINT_CONFIG_BYTES // 1024 + 1)}
    with patch.object(proposals.dynamo_store, "get_proposal_by_id") as get:
        resp = client.post("/ontology/proposals/p-1/compile-constraints?namespace=ns", json={"constraint_config": big})
    assert resp.status_code == 400
    assert "xxxx" not in resp.text
    get.assert_not_called()


def test_constraint_config_within_the_bound_reaches_the_proposal_lookup():
    with patch.object(proposals.dynamo_store, "get_proposal_by_id", return_value=None) as get:
        resp = client.post("/ontology/proposals/p-1/compile-constraints?namespace=ns", json={"constraint_config": {}})
    assert resp.status_code == 404
    get.assert_called_once()


# ── New @pattern constraints vs. today's callers and the runtime ─────────────


def _openapi_pattern(operation: str, member: str) -> re.Pattern[str]:
    """The pattern the generated OpenAPI carries for one input member (query param or body field)."""
    if not _OPENAPI.is_file():
        pytest.skip(f"{_OPENAPI} not found — run `make generate`")
    spec = json.loads(_OPENAPI.read_text())
    for item in spec["paths"].values():
        for op in item.values():
            if not isinstance(op, dict) or op.get("operationId") != operation:
                continue
            for param in op.get("parameters", []):
                if param["name"] == member:
                    return re.compile(param["schema"]["pattern"])
            ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
            prop = spec["components"]["schemas"][ref]["properties"][member]
            if "$ref" in prop:
                prop = spec["components"]["schemas"][prop["$ref"].rsplit("/", 1)[-1]]
            return re.compile(prop["pattern"])
    raise AssertionError(f"{operation}.{member} not in the OpenAPI spec")


_VERTEX_IRIS_IN_USE = [
    "http://example.com/onto#Customer",
    "https://schema.org/Order",
    "https://w3id.org/fibo/ontology/FND/Accounting/Account",
    "urn:ontology:upload-1a2b3c4d#Claim",
]


@pytest.mark.parametrize("operation", ["GetGraphClass", "GetGraphObjectProperty", "GetGraphDatatypeProperty"])
def test_vertex_iri_pattern_accepts_iris_in_use_and_only_what_the_runtime_accepts(operation):
    pattern = _openapi_pattern(operation, "uri")
    for iri in _VERTEX_IRIS_IN_USE:
        assert pattern.search(iri), iri
        _iri(iri)  # the runtime accepts it too
    for bad in ["ftp://x/y", "http:/no-host", "http://x/a b", "http://x/>inject", "relative#Cls", "_:b0"]:
        assert not pattern.search(bad), bad


def test_fetch_source_url_pattern_is_https_only():
    pattern = _openapi_pattern("FetchOntology", "sourceUrl")
    assert pattern.search("https://xmlns.com/foaf/spec/index.rdf")
    assert not pattern.search("http://xmlns.com/foaf/spec/index.rdf")


def test_graph_arn_pattern_accepts_the_benchmark_sentinel_and_graph_arns():
    pattern = _openapi_pattern("StartInduction", "graphArn")
    # ``neptune-db``: benchmarks/*/setup induce requests; the rest are answered 422 UNSUPPORTED_SOURCE.
    for value in ["neptune-db", "g-abc1234567", "arn:aws:neptune-graph:us-east-1:123456789012:graph/g-abc1234567"]:
        assert pattern.search(value), value
    assert not pattern.search("arn:aws:s3:::bucket")


@pytest.mark.parametrize("field", ["sourceUrl", "source_url"])
def test_fetch_request_reads_the_contract_and_legacy_source_url_spelling(field):
    from coa_ontology.catalog.schemas import OntologyFetchRequest

    assert (
        OntologyFetchRequest.model_validate({field: "https://example.com/o.ttl"}).source_url
        == "https://example.com/o.ttl"
    )


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/ontology/versions/0/deploy", {}),
        (f"/ontology/versions/{versions_router.MAX_VERSION_NUMBER + 1}/deploy", {}),
        ("/ontology/versions/1/deploy", {"acknowledgedBreakingMetrics": [""]}),
        ("/ontology/versions/1/deploy", {"acknowledgedBreakingMetrics": ["m" * 129]}),
        ("/ontology/versions/1/deploy", {"acknowledgedBreakingMetrics": ["m"] * 1001}),
    ],
    ids=["version-zero", "version-too-large", "empty-metric-id", "long-metric-id", "too-many-metrics"],
)
def test_out_of_bound_deploy_requests_are_rejected_before_any_deploy(path, body):
    with patch.object(version_deploy, "start_deployment") as start:
        resp = client.post(f"{path}?namespace=ns", json=body)
    assert resp.status_code == 422
    start.assert_not_called()


def test_an_overlong_deployment_id_is_rejected():
    resp = client.get(f"/ontology/deployments/{'d' * 129}?namespace=ns")
    assert resp.status_code == 422
