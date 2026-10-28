# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The ``sparqlQuery`` action: a caller's own SPARQL over the VKG (the Ontology designer query console)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coa_serve.clients.base import QueryResult
from coa_serve.query_utils import IS_MAPPED_IRI
from coa_serve.role_resolver import ResolvedProfile
from coa_serve.tier2.ontop.sparql_guard import MAX_SPARQL_CHARS, SparqlRefused, check_vkg_sparql, vkg_terms
from coa_serve.tier2.ontop.sparql_validator import SPARQLValidator
from coa_serve.tier2.ontop.vkg_translator import VKG_NO_MAPPING, Tier2Result, Tier2Status, Tier2Step, TraceStep
from coa_serve.tier2.sql_firewall import FirewallResult
from rdflib import OWL, RDF, RDFS, URIRef

from . import deployed_graph

pytestmark = pytest.mark.unit
NS = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
SELECT = "SELECT ?p ?name WHERE { ?p <http://e/o#name> ?name }"


@pytest.mark.parametrize(("query", "form"), [(SELECT, "SELECT"), ("ASK { ?s ?p ?o }", "ASK")])
def test_select_and_ask_pass_the_guard(query, form):
    assert check_vkg_sparql(query) == form


@pytest.mark.parametrize(
    "query",
    [
        "CONSTRUCT WHERE { ?s ?p ?o }",
        "DESCRIBE <http://e/x>",
        "SELECT * FROM <http://other> WHERE { ?s ?p ?o }",
        "SELECT * FROM NAMED <http://other> WHERE { GRAPH ?g { ?s ?p ?o } }",
        "SELECT * WHERE { GRAPH ?g { ?s ?p ?o } }",
        "SELECT * WHERE { SERVICE <http://evil/sparql> { ?s ?p ?o } }",
        "INSERT DATA { <http://e/a> <http://e/b> <http://e/c> }",
        "not sparql",
        "",
        None,
        "SELECT * WHERE { ?s ?p ?o }" + " " * MAX_SPARQL_CHARS,
        'SELECT * WHERE { ?s ?p "x" , "\\u0022 . GRAPH ?g { ?a ?b ?c } . ?s ?p \\u0022" . }',
    ],
)
def test_the_guard_refuses_other_forms_own_graphs_and_updates(query):
    with pytest.raises(SparqlRefused):
        check_vkg_sparql(query)


async def _invoke(payload):
    from coa_serve.main import invoke

    return [item async for item in invoke(payload, None)][0]


@pytest.fixture
def translator():
    t = MagicMock()
    t.resolve = AsyncMock()
    with (
        patch("coa_serve.main._ensure_initialized", AsyncMock(return_value=None)),
        patch("coa_serve.main._orchestrator", MagicMock()),
        patch("coa_serve.main._sources_registry", None),
        patch("coa_serve.main._vkg_translator", t),
        patch("coa_serve.main._sparql_validator", None),
    ):
        yield t


@pytest.fixture
def allowed(translator):
    profile = ResolvedProfile(user_id="alice", email="alice@example.com", table_allowlist=["patients"])
    gate = AsyncMock(return_value=(profile, None))
    with patch("coa_serve.main._authorize_namespace_access", gate):
        yield translator


def _payload(sparql: str = SELECT, namespace: str = NS) -> dict:
    return {"action": "sparqlQuery", "sparql": sparql, "namespace": namespace, "profile": {"tableAllowlist": ["*"]}}


def _rows(n: int, truncated: bool = False) -> Tier2Result:
    rows = [{"p": f"http://e/p/{i}", "name": f"P{i}"} for i in range(n)]
    return Tier2Result(
        query_result=QueryResult(rows=rows, columns=["p", "name"], row_count=n, truncated=truncated),
        firewall_result=FirewallResult(denied=False, authorized_sql="SELECT ..."),
        executed_sql="SELECT p, name FROM patients",
    )


async def test_a_select_runs_with_the_caller_s_resolved_profile(allowed):
    allowed.resolve.return_value = _rows(2)
    result = await _invoke(_payload())
    assert result["statusCode"] == 200
    assert result["columns"] == ["p", "name"] and result["rows"] == [["http://e/p/0", "P0"], ["http://e/p/1", "P1"]]
    assert result["rowCount"] == 2 and result["truncated"] is False and result["form"] == "SELECT"
    # The executed SQL names the physical schema: withheld from a caller without a schema role.
    assert result["executedSql"] is None
    call = allowed.resolve.call_args
    assert call.args == (SELECT,)
    assert call.kwargs["namespace"] == NS and call.kwargs["max_rows"] == 1001 and call.kwargs["timeout_seconds"] == 20
    # The grants come from the resolved profile, never from the payload.
    assert call.kwargs["profile"]["userId"] == "alice" and call.kwargs["profile"]["tableAllowlist"] == ["patients"]


@pytest.mark.parametrize(
    ("roles", "can_view", "sees_sql"),
    [
        ({"global_roles": ["platform-admin"]}, True, True),
        ({"resource_roles": [{"resourceUID": NS, "role": "namespace-maintainer"}]}, True, True),
        ({"resource_roles": [{"resourceUID": NS, "role": "data-steward"}]}, True, True),
        # A schema role without viewNamespace (for example a namespace policy forbids it).
        ({"resource_roles": [{"resourceUID": NS, "role": "namespace-owner"}]}, False, False),
        ({"resource_roles": [{"resourceUID": "other", "role": "namespace-owner"}]}, True, False),
        # viewNamespace without a schema role, as a data analyst holds.
        ({"resource_roles": [{"resourceUID": NS, "role": "data-analyst"}]}, True, False),
    ],
)
async def test_the_executed_sql_needs_a_schema_role_and_view_namespace(translator, roles, can_view, sees_sql):
    from coa_serve import main

    profile = ResolvedProfile(user_id="alice", table_allowlist=["patients"], **roles)
    translator.resolve.return_value = _rows(1)
    main._orchestrator.can_view_namespace.return_value = can_view
    with patch("coa_serve.main._authorize_namespace_access", AsyncMock(return_value=(profile, None))):
        result = await _invoke(_payload())
    assert result["statusCode"] == 200
    assert result["executedSql"] == ("SELECT p, name FROM patients" if sees_sql else None)


async def test_the_executed_sql_is_withheld_when_view_namespace_cannot_be_evaluated(translator):
    """The real firewall answers False when Cedar raises: the console fails closed with it."""
    from coa_serve import main
    from coa_serve.orchestrator import Orchestrator
    from coa_serve.tier2.sql_firewall import SQLFirewall

    cedar = MagicMock()
    cedar.can_view_namespace.side_effect = RuntimeError("policy store unavailable")
    orch = Orchestrator.__new__(Orchestrator)
    orch._firewall = SQLFirewall(cedar)
    profile = ResolvedProfile(user_id="alice", global_roles=["platform-admin"], table_allowlist=["patients"])
    translator.resolve.return_value = _rows(1)
    with (
        patch.object(main, "_orchestrator", orch),
        patch("coa_serve.main._authorize_namespace_access", AsyncMock(return_value=(profile, None))),
    ):
        result = await _invoke(_payload())
    assert result["statusCode"] == 200 and result["executedSql"] is None
    cedar.can_view_namespace.assert_called_once()


@pytest.mark.parametrize("sparql", [None, "", "   ", 42, "SELECT * WHERE { ?s ?p ?o }" + " " * MAX_SPARQL_CHARS])
async def test_a_missing_or_oversized_query_is_refused_before_the_namespace_gate(translator, sparql):
    gate = AsyncMock()
    with patch("coa_serve.main._authorize_namespace_access", gate):
        result = await _invoke(_payload(sparql))
    assert result["statusCode"] == 400 and result["details"] == [{"field": "sparql", "type": "value_error"}]
    gate.assert_not_called()
    translator.resolve.assert_not_called()


@pytest.mark.parametrize(("matched", "truncated"), [(1901, True), (1000, False)])
async def test_a_result_the_row_cap_cut_is_reported_truncated(allowed, matched, truncated):
    """Athena and Redshift cap the SQL at the rows asked for, and then see no more rows."""

    async def capped(sparql, *, max_rows, **kwargs):
        return _rows(min(matched, max_rows))  # as an executor answers the LIMIT it injected

    allowed.resolve.side_effect = capped
    result = await _invoke(_payload())
    assert result["rowCount"] == len(result["rows"]) == min(matched, 1000) and result["truncated"] is truncated


async def test_ask_answers_one_boolean_and_truncation_is_reported(allowed):
    allowed.resolve.return_value = _rows(1)
    result = await _invoke(_payload("ASK { ?s ?p ?o }"))
    assert result["columns"] == ["result"] and result["rows"] == [[True]] and result["form"] == "ASK"
    allowed.resolve.return_value = _rows(3, truncated=True)
    assert (await _invoke(_payload()))["truncated"] is True


async def test_a_refused_query_never_reaches_the_vkg(allowed):
    result = await _invoke(_payload("SELECT * WHERE { GRAPH ?g { ?s ?p ?o } }"))
    assert result["statusCode"] == 400 and result["error"] == "ValidationError" and "GRAPH" in result["message"]
    allowed.resolve.assert_not_called()


@pytest.mark.parametrize(
    ("outcome", "status", "error"),
    [
        (
            Tier2Result(
                query_result=None,
                firewall_result=FirewallResult(denied=True, authorized_sql="", reason="table orders"),
                error="Firewall denied: table orders",
            ),
            403,
            "AccessDeniedError",
        ),
        (Tier2Result(query_result=None, error="vkg_translation_error"), 400, "QueryTranslationError"),
        (Tier2Result(query_result=None, error="query_qualification_error"), 502, "DataSourceUnavailableError"),
    ],
)
async def test_vkg_failures_map_to_denied_untranslatable_or_unavailable(allowed, outcome, status, error):
    allowed.resolve.return_value = outcome
    result = await _invoke(_payload())
    assert result["statusCode"] == status and result["error"] == error
    assert "orders" not in result["message"]  # a data-level deny reason stays internal


async def test_the_executor_s_own_timeout_is_a_504_too(allowed):
    allowed.resolve.return_value = Tier2Result(query_result=None, error="query_timeout")
    result = await _invoke(_payload())
    assert result["statusCode"] == 504
    assert "did not finish" in result["message"]


async def test_a_slow_query_is_a_504(allowed):
    async def slow(*args, trace_steps, **kwargs):
        trace_steps.append(TraceStep(Tier2Step.VKG_COMPILE, Tier2Status.OK, 5))  # translated: the SQL is slow
        await asyncio.sleep(1)

    allowed.resolve.side_effect = slow
    with patch("coa_serve.main.SPARQL_TIMEOUT_S", -4.9):
        result = await _invoke(_payload())
    assert result["statusCode"] == 504


async def test_a_vkg_that_is_not_running_is_a_503_not_a_translation_error(allowed):
    """A VKG scaled to zero, or reloading, sent the user to fix a correct query."""
    allowed.resolve.return_value = Tier2Result(query_result=None, error="vkg_unavailable")
    result = await _invoke(_payload())
    assert result["statusCode"] == 503 and "VKG is not running" in result["message"]
    assert "translate" not in result["message"]


async def test_a_vkg_still_on_the_previous_version_is_a_503_that_names_both(allowed):
    """Right after a deploy, or after a failed reload, the VKG still maps the old version.

    The query would run against that mapping, so it is refused as not ready yet,
    with the versions, rather than answered from the previous ontology.
    """
    reason = "The VKG still serves v1; the ontology is v2."
    step = TraceStep(Tier2Step.VKG_COMPILE, Tier2Status.ERROR, 5, {"error": "vkg_version_skew", "reason": reason})

    async def skewed(*args, trace_steps, **kwargs):
        trace_steps.append(step)
        return Tier2Result(query_result=None, trace_steps=trace_steps, error="vkg_version_skew")

    allowed.resolve.side_effect = skewed
    result = await _invoke(_payload())
    assert result["statusCode"] == 503
    assert result["message"] == f"{reason} Try again once it has loaded the deployed version."


async def test_a_vkg_that_does_not_answer_is_a_503_not_a_slow_query(allowed):
    """The deadline answered 'did not finish within 20 seconds' for a query that never ran."""

    async def unanswered(*args, **kwargs):
        await asyncio.sleep(1)  # still waiting for the VKG's translation: no step done

    allowed.resolve.side_effect = unanswered
    with patch("coa_serve.main.SPARQL_TIMEOUT_S", -4.9):
        result = await _invoke(_payload())
    assert result["statusCode"] == 503 and "VKG is not running" in result["message"]


async def test_a_denied_namespace_never_reaches_the_vkg(translator):
    from coa_serve.exceptions import AccessDeniedError

    denied = AsyncMock(return_value=(None, AccessDeniedError("no").to_dict("r")))
    with patch("coa_serve.main._authorize_namespace_access", denied):
        result = await _invoke(_payload())
    assert result["statusCode"] == 403
    translator.resolve.assert_not_called()


async def test_a_query_without_a_namespace_is_refused(allowed):
    result = await _invoke(_payload(namespace=""))
    assert result["statusCode"] == 400
    allowed.resolve.assert_not_called()


# ── Terms the VKG cannot answer ─────────────────────────────────────────────

_IND = f"PREFIX ind: <{deployed_graph.PREFIX}>\nPREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"


def test_the_terms_of_a_query_are_the_classes_it_types_and_the_predicates_it_uses():
    query = (
        "PREFIX ind: <http://e/o#>\n"
        "SELECT ?c WHERE { ?c a ind:Customers ; ind:email ?e ; ind:placed/^ind:of ?o . "
        "?o ind:of <http://e/o#customers/1> . FILTER(?c != <http://e/o#customers/1>) "
        "VALUES ?o { <http://e/o#orders/7> } ?o !ind:hidden ?x . ?c ?p ?v }"
    )
    classes, properties = vkg_terms(query)
    assert classes == ["http://e/o#Customers"]
    # Rows such as customers/1 are values, not terms; a negated path names what it excludes.
    assert properties == ["http://e/o#email", "http://e/o#of", "http://e/o#placed", str(RDF.type)]
    assert vkg_terms("not sparql") == ([], [])  # no algebra: the VKG decides


@pytest.mark.parametrize(
    ("query", "classes", "properties"),
    [
        # OPTIONAL and MINUS: the left side alone decides whether there is an answer.
        ("SELECT * WHERE { ?c a ind:A OPTIONAL { ?c ind:t ?t } }", ["A"], []),
        ("SELECT * WHERE { ?c a ind:A MINUS { ?c a ind:Gone ; ind:q ?z } }", ["A"], []),
        # The expression of a FILTER, a BIND or a projection, EXISTS and NOT EXISTS included.
        ("SELECT * WHERE { ?c a ind:A FILTER NOT EXISTS { ?c a ind:Gone ; ind:q ?z } }", ["A"], []),
        ("SELECT * WHERE { ?c ind:p ?v FILTER EXISTS { ?c a ind:B , ind:C } }", [], ["p"]),
        (
            "SELECT ?c (EXISTS { ?c ind:ex ?z } AS ?has) WHERE { ?c a ind:A } ORDER BY (EXISTS { ?c ind:ob ?z })",
            ["A"],
            [],
        ),
        # A UNION needs what every branch needs.
        ("SELECT * WHERE { { ?x a ind:U1 ; ind:p ?v } UNION { ?x a ind:U2 ; ind:p ?w } }", [], ["p"]),
        ("SELECT * WHERE { { ?x a ind:A ; ind:p ?v } UNION { ?x a ind:A } }", ["A"], []),
        # A subquery is left to the VKG: one that aggregates without GROUP BY has a row whatever it matches.
        ("SELECT * WHERE { ?y a ind:B { SELECT (COUNT(?x) AS ?n) WHERE { ?x a ind:S } } }", ["B"], []),
        # Paths: an alternative needs what every alternative needs; * and ? also match with no step.
        (
            "SELECT * WHERE { ?x (ind:c/ind:k|ind:d/ind:k) ?z . ?x ind:e* ?w . ?x ind:f+ ?v . ?x ind:g? ?u }",
            [],
            ["f", "k"],
        ),
        # What the query aggregates, groups or orders is still needed.
        (
            "SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE { ?x a ind:A ; ind:p ?v } GROUP BY ?v ORDER BY ?n LIMIT 3",
            ["A"],
            ["p"],
        ),
        ("ASK { ?x a ind:A }", ["A"], []),
    ],
)
def test_only_the_terms_every_answer_needs_are_checked(query, classes, properties):
    found_classes, found_properties = vkg_terms("PREFIX ind: <http://e/o#>\n" + query)
    assert found_classes == [f"http://e/o#{name}" for name in classes]
    assert [p for p in found_properties if p != str(RDF.type)] == [f"http://e/o#{name}" for name in properties]


def _deployed(ds=None, sent: list[str] | None = None) -> SPARQLValidator:
    """The console's check over the graph deploy wrote for v2, read through the real Neptune client."""
    graph = deployed_graph.neptune_over(deployed_graph.designer_v2_dataset() if ds is None else ds)
    if sent is not None:
        query = graph.query

        async def recorded(sparql: str, **kwargs):
            sent.append(sparql)
            return await query(sparql, **kwargs)

        graph.query = recorded  # type: ignore[method-assign]
    return SPARQLValidator(graph, graph_uri_template=deployed_graph.TEMPLATE)


async def _console(allowed, sparql: str, validator: SPARQLValidator | None = None) -> dict:
    allowed.resolve.return_value = _rows(1)
    with patch("coa_serve.main._sparql_validator", validator or _deployed()):
        return await _invoke(_payload(_IND + sparql, namespace=deployed_graph.NS))


@pytest.mark.parametrize(
    ("sparql", "message"),
    [
        # v2 deleted the property mapped from customers.signup_date; no version had ind:Nope.
        ("SELECT ?c ?d WHERE { ?c ind:customers_signupDate ?d }", "<{p}customers_signupDate> is not a class"),
        ("SELECT ?x WHERE { ?x a ind:Nope }", "<{p}Nope> is not a class"),
        # A class the designer added, which no table maps, and an attribute deploy marked unmapped.
        ("SELECT ?c WHERE { ?c a ind:PremiumCustomer }", "<{p}PremiumCustomer> has no table mapping"),
        ("SELECT ?c ?t WHERE { ?c ind:customers_loyaltyTier ?t }", "<{p}customers_loyaltyTier> has no table mapping"),
    ],
)
async def test_a_term_the_vkg_cannot_answer_is_refused_by_name_before_translation(allowed, sparql, message):
    result = await _console(allowed, sparql)
    assert result["statusCode"] == 400 and result["error"] == "ValidationError"
    sentence = message.format(p=deployed_graph.PREFIX)
    assert result["message"] == sentence + (
        " or property of the deployed ontology." if "is not" in sentence else ", so the VKG cannot answer it."
    )
    allowed.resolve.assert_not_called()


async def test_every_refused_term_is_named_unknown_ones_first(allowed):
    sparql = (
        "SELECT ?c WHERE { ?c a ind:Customers ; ind:customers_loyaltyTier ?t ; ind:nothing ?n . "
        "?l a ind:LoyaltyProgramme }"
    )
    result = await _console(allowed, sparql)
    p = deployed_graph.PREFIX
    assert result["message"] == (
        f"<{p}nothing> is not a class or property of the deployed ontology. "
        f"<{p}LoyaltyProgramme> has no table mapping, so the VKG cannot answer it. "
        f"<{p}customers_loyaltyTier> has no table mapping, so the VKG cannot answer it."
    )


async def test_a_mapped_query_goes_to_the_vkg_as_before(allowed):
    sparql = (
        "SELECT ?c ?e ?l WHERE { ?c a ind:Customers ; ind:customers_email ?e ; rdfs:label ?l . "
        "?o ind:orders_customerId ?c }"
    )
    result = await _console(allowed, sparql)
    assert result["statusCode"] == 200 and result["rows"] == [["http://e/p/0", "P0"]]
    assert allowed.resolve.call_args.kwargs["refuse_without_table"] is True


@pytest.mark.parametrize(
    "sparql",
    [
        # Ontop 5.5.0 answers each from the customers or orders table: no answer needs the other term.
        "SELECT ?c ?t WHERE { ?c a ind:Customers OPTIONAL { ?c ind:customers_loyaltyTier ?t } }",
        "SELECT ?x WHERE { { ?x a ind:PurchaseOrder } UNION { ?x a ind:Nope } }",
        "SELECT ?c WHERE { ?c a ind:Customers FILTER NOT EXISTS { ?c a ind:PremiumCustomer } }",
        "SELECT ?c WHERE { ?c a ind:Customers MINUS { ?c a ind:PremiumCustomer } }",
    ],
)
async def test_a_term_no_answer_needs_is_left_to_the_vkg(allowed, sparql):
    assert (await _console(allowed, sparql))["statusCode"] == 200
    allowed.resolve.assert_called_once()


async def test_a_union_is_refused_when_every_branch_needs_the_term(allowed):
    sparql = (
        "SELECT ?c WHERE { { ?c a ind:PremiumCustomer ; ind:customers_name ?n } "
        "UNION { ?c a ind:PremiumCustomer ; ind:customers_email ?e } }"
    )
    result = await _console(allowed, sparql)
    p = deployed_graph.PREFIX
    assert result["message"] == f"<{p}PremiumCustomer> has no table mapping, so the VKG cannot answer it."
    allowed.resolve.assert_not_called()


@pytest.mark.parametrize("declared", [True, False], ids=["a class of the namespace", "a grounding parent"])
async def test_a_parent_of_a_mapped_class_goes_to_the_vkg(allowed, declared):
    """No table maps the parent, but Ontop answers it with the rows of its mapped subclass.

    A grounding parent from a reference ontology is no subject of the namespace's graphs; that a
    mapped class names it as its parent makes it a term.
    """
    ds = deployed_graph.designer_v2_dataset()
    party = URIRef(deployed_graph.PREFIX + "Party" if declared else "https://spec.example.org/agents#Party")
    graph = ds.graph(URIRef(deployed_graph.GRAPH))
    if declared:
        graph.add((party, RDF.type, OWL.Class))
    graph.add((URIRef(deployed_graph.CUSTOMERS), RDFS.subClassOf, party))
    result = await _console(allowed, f"SELECT ?x WHERE {{ ?x a <{party}> }}", _deployed(ds))
    assert result["statusCode"] == 200


@pytest.mark.parametrize(
    "sparql", ["SELECT ?c WHERE { ?c a ind:PremiumCustomer }", "SELECT ?x WHERE { ?x a ind:Nope }"]
)
async def test_a_namespace_without_markers_keeps_today_s_checks(allowed, sparql):
    ds = deployed_graph.designer_v2_dataset()
    for graph in ds.graphs():
        graph.remove((None, URIRef(IS_MAPPED_IRI), None))
    result = await _console(allowed, sparql, _deployed(ds))
    assert result["statusCode"] == 200
    allowed.resolve.assert_called_once()


async def test_the_terms_are_checked_in_one_bounded_neptune_query(allowed):
    sent: list[str] = []
    names = " ; ".join(f"ind:p{n:02d} ?v{n}" for n in range(60))
    result = await _console(allowed, f"SELECT ?c WHERE {{ ?c {names} }}", _deployed(sent=sent))
    assert len(sent) == 1
    assert sent[0].count("VALUES ?term") == 3 and "LIMIT 1" in sent[0]
    assert result["message"].count("is not a class or property of the deployed ontology.") == 50  # the first 50


async def test_a_graph_that_fails_or_does_not_answer_in_time_leaves_the_query_to_the_vkg(allowed):
    failing = MagicMock()
    failing.query = AsyncMock(side_effect=RuntimeError("neptune down"))
    validator = SPARQLValidator(failing, graph_uri_template=deployed_graph.TEMPLATE)
    assert (await _console(allowed, "SELECT ?x WHERE { ?x a ind:Nope }", validator))["statusCode"] == 200

    async def unanswered(*args, **kwargs):
        await asyncio.sleep(1)

    failing.query = AsyncMock(side_effect=unanswered)
    with patch("coa_serve.main.SPARQL_TERMS_TIMEOUT_S", 0.01):
        assert (await _console(allowed, "SELECT ?x WHERE { ?x a ind:Nope }", validator))["statusCode"] == 200
    assert allowed.resolve.call_count == 2


async def test_the_term_check_s_time_comes_out_of_the_query_s_budget(allowed):
    """Serve still answers within the 25 s it waited before the check, under the 29 s the API waits."""
    slow = MagicMock()

    async def checked(*args):
        await asyncio.sleep(0.2)

    slow.refuse_vkg_terms = AsyncMock(side_effect=checked)
    waits: list[float] = []
    wait_for = asyncio.wait_for

    async def recorded(awaitable, timeout):
        waits.append(timeout)
        return await wait_for(awaitable, timeout)

    with patch("coa_serve.main.asyncio.wait_for", side_effect=recorded):
        assert (await _console(allowed, "SELECT ?x WHERE { ?x a ind:Customers }", slow))["statusCode"] == 200
    check, query = waits
    assert check == 3 and 20 < query <= 25 - 0.2


async def test_a_query_no_table_mapping_matches_is_a_400_not_a_502(allowed):
    """Ontop's empty plan or its no-table placeholder, for a pattern no mapping has (the VKG refuses both)."""
    allowed.resolve.return_value = Tier2Result(query_result=None, error=VKG_NO_MAPPING)
    result = await _invoke(_payload())
    assert result["statusCode"] == 400 and result["error"] == "ValidationError"
    assert result["message"] == "No table mapping matches this query, so the VKG cannot answer it."


@pytest.mark.parametrize("error", ["query_execution_error", "firewall_error"])
async def test_a_query_that_cannot_run_is_still_a_502(allowed, error):
    allowed.resolve.return_value = Tier2Result(query_result=None, error=error)
    result = await _invoke(_payload())
    assert result["statusCode"] == 502 and result["error"] == "DataSourceUnavailableError"
