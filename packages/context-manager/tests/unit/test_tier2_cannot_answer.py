# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Both Tier 2 writers are told the terms no table maps, and a CANNOT_ANSWER reply is no result.

The writers' prompts are built over the graph a deploy writes after designer edits
(``deployed_graph``), read through the real Neptune client, with the real reader. A
namespace without class markers, and one with nothing to list, get the prompts they
got before, byte for byte. A decline ends the strategy with a ``StrategyDeclined``, so
the chain asks the next strategy and then Tier 3 (``test_strategy.py`` covers the chain).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_serve.clients.base import ConverseResult, VectorHit
from coa_serve.tier2.nl_to_sql.sql_generator import SQLGenerator
from coa_serve.tier2.nl_to_sql.strategy import NLtoSQLStrategy
from coa_serve.tier2.ontop.nl_to_sparql import NLtoSPARQL, PipelineStep, TranslationError
from coa_serve.tier2.ontop.strategy import OntopStrategy
from coa_serve.tier2.sql_firewall import UnsafeSQLError
from coa_serve.tier2.strategy import (
    StrategyContext,
    StrategyDeclined,
    StrategyOption,
    StrategyResult,
    StructuredQueryTier,
)
from coa_serve.tier2.unmapped_terms import (
    CANNOT_ANSWER,
    DECLINED,
    TermEnd,
    UnmappedTerm,
    UnmappedTerms,
    UnmappedTermsReader,
)
from coa_serve.trace import TraceCollector
from rdflib import Dataset, URIRef

from .deployed_graph import (
    CUSTOMER_SEGMENT,
    GRAPH,
    IN_SEGMENT,
    IS_MAPPED,
    LOYALTY_PROGRAMME,
    LOYALTY_TIER,
    MEMBER,
    NS,
    PREFERRED_ORDER,
    PREFIX,
    TEMPLATE,
    designer_v2_dataset,
    neptune_over,
)

pytestmark = pytest.mark.unit

_CUSTOMERS_TEXT = (
    "Table: customers | Description: Customer accounts | Synonyms: Client | "
    "Columns: id:integer, name:string, email:string, lifetime_value:double"
)
_SQL_REPLY = "```sql\nSELECT count(*) FROM customers\n```\nConfidence: 0.9"
_DECLINE = f"{CANNOT_ANSWER}\nloyalty tier"
# A decline written after the writer's reasoning, in the code block its system prompt asks for.
_FENCED_DECLINE = f"The schema has no column for the loyalty tier.\n```sql\n{CANNOT_ANSWER}\nloyalty tier\n```"
_SPARQL_REPLY = (
    "```sparql\n"
    f"PREFIX ind: <{PREFIX}>\n"
    "SELECT (COUNT(DISTINCT ?c) AS ?clientCount) WHERE { ?c a ind:Customers . }\n"
    "```\nConfidence: 0.9"
)
_TERMS = UnmappedTerms(
    terms=(
        UnmappedTerm(iri=LOYALTY_TIER, label="loyalty tier", kind="attribute", of=(TermEnd("Client", "customers"),)),
    )
)


def _without_markers() -> Dataset:
    """The namespace accepted before versions and before deploy wrote any ``coa:isMapped`` marker."""
    ds = designer_v2_dataset(version=None)
    g = ds.graph(URIRef(GRAPH))
    g.remove((None, IS_MAPPED, None))
    return ds


def _nothing_unmapped() -> Dataset:
    """The namespace with class markers whose deploy left no term without a table."""
    ds = designer_v2_dataset()
    g = ds.graph(URIRef(GRAPH))
    unmapped = (PREFIX + "PremiumCustomer", LOYALTY_PROGRAMME, CUSTOMER_SEGMENT)
    for term in (*unmapped, LOYALTY_TIER, PREFERRED_ORDER, IN_SEGMENT, MEMBER):
        g.remove((URIRef(term), None, None))
    return ds


def _reader(ds: Dataset) -> UnmappedTermsReader:
    return UnmappedTermsReader(neptune_over(ds), graph_uri_template=TEMPLATE)


def _llm(*replies: str) -> AsyncMock:
    llm = AsyncMock()
    llm.embed.return_value = [0.1] * 8
    llm.converse.side_effect = [ConverseResult(text=r) for r in replies]
    return llm


def _vector() -> AsyncMock:
    vector = AsyncMock()
    vector.count_documents.return_value = 2
    vector.search.return_value = [
        VectorHit(id="1", text=_CUSTOMERS_TEXT, score=0.9, metadata={"entity_type": "class", "data_source_id": "ds"})
    ]
    return vector


async def _sql_call(reader: UnmappedTermsReader | None, reply: str = _SQL_REPLY):
    llm = _llm(reply)
    result = await SQLGenerator(llm, _vector(), unmapped_terms=reader).generate("q", namespace=NS)
    return result, llm.converse.call_args


def _sparql_translator(ds: Dataset, reader: UnmappedTermsReader | None, *replies: str) -> tuple[NLtoSPARQL, AsyncMock]:
    llm = _llm(*replies)
    translator = NLtoSPARQL(neptune_over(ds), llm, graph_uri_template=TEMPLATE, unmapped_terms=reader)
    return translator, llm


class TestSqlWriter:
    async def test_the_prompt_lists_the_terms_after_the_schema_and_offers_cannot_answer(self):
        result, call = await _sql_call(_reader(designer_v2_dataset()))

        prompt = call.args[0]
        assert prompt.index(_CUSTOMERS_TEXT) < prompt.index("## Terms with no table behind them")
        for line in (
            "- Premium customer (a kind of customers)",
            "- loyalty tier (an attribute of customers)",
            "- preferred order (a relationship from customers to orders)",
            "- Loyalty programme",
        ):
            assert line in prompt
        assert "Reply with CANNOT_ANSWER on the first line" in prompt
        assert "Return the SQL in a ```sql code block, or reply CANNOT_ANSWER as described above." in prompt
        assert result.unmapped_terms.terms and result.sql == "SELECT count(*) FROM customers"

    @pytest.mark.parametrize("make", [_without_markers, _nothing_unmapped])
    async def test_a_namespace_with_nothing_to_list_gets_today_s_prompt(self, make):
        _, with_reader = await _sql_call(_reader(make()))
        _, without_reader = await _sql_call(None)

        assert with_reader.args[0] == without_reader.args[0]
        assert with_reader.kwargs["system"] == without_reader.kwargs["system"]
        assert "CANNOT_ANSWER" not in with_reader.args[0]

    @pytest.mark.parametrize("reply", [_DECLINE, _FENCED_DECLINE])
    async def test_a_decline_is_no_sql(self, reply):
        result, _ = await _sql_call(_reader(designer_v2_dataset()), reply=reply)

        assert result.sql == "" and result.error == DECLINED
        assert result.unmapped_terms.terms
        assert result.trace_steps[-1]["step"] == "generate_sql" and result.trace_steps[-1]["status"] == "declined"

    async def test_without_terms_a_cannot_answer_reply_is_read_as_before(self):
        """Nothing was listed, so the prompt never offered the word: no SQL, as for any reply without one."""
        result, _ = await _sql_call(_reader(_without_markers()), reply=_DECLINE)

        assert result.sql == "" and result.error is None

    async def test_without_terms_a_fenced_cannot_answer_is_sql_as_before(self):
        """The code block's text is the SQL, as before (the firewall then refuses it): not a decline."""
        result, _ = await _sql_call(_reader(_without_markers()), reply=f"```sql\n{CANNOT_ANSWER}\n```")

        assert result.sql == CANNOT_ANSWER and result.error is None

    async def test_the_correction_is_told_the_terms_and_may_decline(self):
        llm = _llm(_DECLINE)
        generator = SQLGenerator(llm, _vector())

        sql, confidence = await generator.correct(
            "q",
            _CUSTOMERS_TEXT,
            "SELECT name FROM customers WHERE loyalty_tier = 'gold'",
            "COLUMN_NOT_FOUND",
            unmapped_terms=_TERMS,
        )

        assert (sql, confidence) == (CANNOT_ANSWER, 0.0)
        prompt = llm.converse.call_args.args[0]
        assert "- loyalty tier (an attribute of customers)" in prompt
        assert (
            "Return ONLY the corrected SQL in a ```sql code block, or reply CANNOT_ANSWER as described above." in prompt
        )

    async def test_a_correction_without_terms_is_unchanged(self):
        prompts = []
        for terms in (None, UnmappedTerms()):
            llm = _llm(_SQL_REPLY)
            await SQLGenerator(llm, _vector()).correct("q", "ddl", "SELECT 1", "boom", unmapped_terms=terms)
            prompts.append(llm.converse.call_args.args[0])

        assert prompts[0] == prompts[1]
        assert "CANNOT_ANSWER" not in prompts[0]


class TestSparqlWriter:
    async def test_the_prompt_lists_the_terms_after_the_schema_and_offers_cannot_answer(self):
        ds = designer_v2_dataset()
        translator, llm = _sparql_translator(ds, _reader(ds), _SPARQL_REPLY)

        result = await translator.translate("How many clients are there?", NS)

        assert result.valid
        prompt = llm.converse.call_args.kwargs["prompt"]
        assert prompt.index("Properties:") < prompt.index("Terms with no table mapping")
        for line in (
            "- Premium customer (a kind of Client)",
            "- loyalty tier (an attribute of Client)",
            "- preferred order (a relationship from Client to orders)",
        ):
            assert line in prompt
        assert prompt.endswith(
            "Generate a SPARQL query for the user question, or reply CANNOT_ANSWER as described above."
        )

    @pytest.mark.parametrize("make", [_without_markers, _nothing_unmapped])
    async def test_a_namespace_with_nothing_to_list_gets_today_s_prompt(self, make):
        prompts = []
        for with_reader in (True, False):
            ds = make()
            translator, llm = _sparql_translator(ds, _reader(ds) if with_reader else None, _SPARQL_REPLY)
            await translator.translate("How many clients are there?", NS)
            prompts.append((llm.converse.call_args.kwargs["prompt"], llm.converse.call_args.kwargs["system"]))

        assert prompts[0] == prompts[1]
        assert "CANNOT_ANSWER" not in prompts[0][0]

    @pytest.mark.parametrize("reply", [_DECLINE, _FENCED_DECLINE.replace("```sql", "```sparql")])
    async def test_a_decline_ends_the_translation_without_validating_or_retrying(self, reply):
        ds = designer_v2_dataset()
        translator, llm = _sparql_translator(ds, _reader(ds), reply, _SPARQL_REPLY)

        result = await translator.translate("Which clients are in the gold loyalty tier?", NS)

        assert result.error == TranslationError.CANNOT_ANSWER
        assert result.valid is False and result.sparql is None and result.tbox_context is not None
        assert llm.converse.await_count == 1
        steps = {s["step"]: s for s in result.trace_steps}
        assert steps[PipelineStep.LLM_CALL]["status"] == "skipped"
        assert steps[PipelineStep.LLM_CALL]["detail"] == TranslationError.CANNOT_ANSWER
        assert PipelineStep.VALIDATION not in steps

    async def test_without_terms_a_cannot_answer_reply_is_read_as_before(self):
        """Nothing was listed: the reply holds no SPARQL, so validation fails and is retried, as before."""
        ds = _without_markers()
        translator, llm = _sparql_translator(ds, _reader(ds), _DECLINE, _DECLINE, _DECLINE)

        result = await translator.translate("Which clients are in the gold loyalty tier?", NS)

        assert result.error == TranslationError.VALIDATION_FAILED
        assert llm.converse.await_count == 3


def _context() -> StrategyContext:
    return StrategyContext(embedding=[0.1] * 8, profile={"userId": "u"}, options={}, trace=TraceCollector())


def _executor() -> AsyncMock:
    """An executor with no dialect or placement lookup, so the strategy writes Athena SQL for one database."""
    executor = AsyncMock()
    del executor.resolve_target_dialect, executor.resolve_table_placements
    return executor


def _first_shot(sql: str = "SELECT name FROM customers", error: str | None = None) -> MagicMock:
    result = MagicMock()
    result.sql = sql
    result.error = error
    result.confidence = 0.8
    result.retrieved_tables = ["customers"]
    result.expanded_tables = ["customers"]
    result.trace_steps = []
    result.data_source_id = "ds"
    result.ddl_context = _CUSTOMERS_TEXT
    result.table_sources = {}
    result.unmapped_terms = _TERMS
    return result


class TestNlToSqlStrategy:
    @pytest.fixture
    def parts(self):
        generator, firewall, executor = AsyncMock(), MagicMock(), _executor()
        firewall.evaluate.return_value = MagicMock(denied=False, authorized_sql="SELECT name FROM customers")
        strategy = NLtoSQLStrategy(sql_generator=generator, firewall=firewall, query_executor=executor)
        return strategy, generator, firewall, executor

    async def test_a_first_shot_decline_is_a_declined_strategy_and_runs_nothing(self, parts):
        strategy, generator, firewall, executor = parts
        generator.generate.return_value = _first_shot(sql="", error=DECLINED)
        context = _context()

        assert await strategy.resolve("Which clients are in the gold loyalty tier?", NS, context) == StrategyDeclined(
            StrategyOption.NL_TO_SQL
        )
        firewall.evaluate.assert_not_called()
        executor.execute.assert_not_awaited()
        assert [(s.step, s.status, s.detail) for s in context.trace.steps] == [
            ("t2.sql.generate", "skipped", "cannot_answer")
        ]

    async def test_a_correction_decline_after_a_failed_first_shot_is_a_declined_strategy(self, parts):
        strategy, generator, _, executor = parts
        generator.generate.return_value = _first_shot(sql="SELECT name FROM customers WHERE loyalty_tier = 'gold'")
        executor.execute.side_effect = RuntimeError("COLUMN_NOT_FOUND: loyalty_tier")
        generator.correct.return_value = (CANNOT_ANSWER, 0.0)
        context = _context()

        result = await strategy.resolve("Which clients are in the gold loyalty tier?", NS, context)

        assert result == StrategyDeclined(StrategyOption.NL_TO_SQL)
        assert generator.correct.await_args.kwargs["unmapped_terms"] is _TERMS
        assert executor.execute.await_count == 1
        last = context.trace.steps[-1]
        # Keyed like the other correction steps that wrote no SQL, which the Playground reads.
        assert (last.step, last.status, last.detail) == (
            "t2.sql.generate",
            "skipped",
            {"correction_shot": 2, "error": "cannot_answer"},
        )

    async def test_a_correction_decline_after_a_clean_empty_first_shot_keeps_its_empty_answer(self, parts):
        """The correction was asked because of the zero rows, not the question: a zero-row result stays the answer."""
        strategy, generator, _, executor = parts
        generator.generate.return_value = _first_shot(sql="SELECT name FROM customers WHERE email IS NULL")
        executor.execute.return_value = MagicMock(rows=[], columns=["name"], row_count=0, truncated=False)
        generator.correct.return_value = (CANNOT_ANSWER, 0.0)
        context = _context()

        result = await strategy.resolve("Which clients have no email address?", NS, context)

        assert isinstance(result, StrategyResult)
        # The first shot's result, at its confidence (the correction's is 0.0).
        assert (result.rows, result.row_count, result.confidence) == ([], 0, 0.8)
        assert executor.execute.await_count == 1
        last = context.trace.steps[-1]
        assert (last.step, last.status, last.detail) == (
            "t2.sql.generate",
            "skipped",
            {"correction_shot": 2, "error": "cannot_answer"},
        )

    async def test_without_terms_a_correction_that_writes_the_word_is_not_a_decline(self, parts):
        """Its prompt never offered the word, so the text is SQL as before, and the firewall refuses it."""
        strategy, generator, firewall, executor = parts
        first = _first_shot()
        first.unmapped_terms = UnmappedTerms()
        generator.generate.return_value = first
        executor.execute.side_effect = RuntimeError("COLUMN_NOT_FOUND: loyalty_tier")
        generator.correct.return_value = (CANNOT_ANSWER, 0.5)
        firewall.evaluate.side_effect = [firewall.evaluate.return_value, UnsafeSQLError("not a SELECT")]

        result = await strategy.resolve("Which clients are in the gold loyalty tier?", NS, _context())

        assert result is None
        assert firewall.evaluate.call_args_list[-1].args[0] == CANNOT_ANSWER


def _translation(*, error: str | None = None, sparql: str | None = "SELECT ?x WHERE { ?x a <urn:x> }") -> MagicMock:
    result = MagicMock()
    result.valid = error is None
    result.sparql = sparql if error is None else None
    result.confidence = 0.8 if error is None else 0.0
    result.error = error
    result.trace_steps = []
    return result


class TestOntopStrategy:
    async def test_a_decline_is_a_declined_strategy_and_the_vkg_is_not_asked(self):
        nl_to_sparql, vkg = AsyncMock(), AsyncMock()
        nl_to_sparql.translate.return_value = _translation(error=TranslationError.CANNOT_ANSWER)

        result = await OntopStrategy(nl_to_sparql=nl_to_sparql, vkg_translator=vkg).resolve("q", NS, _context())

        assert result == StrategyDeclined(StrategyOption.ONTOP)
        vkg.resolve.assert_not_awaited()

    async def test_a_decline_on_the_vkg_retry_is_no_result_and_says_why(self):
        nl_to_sparql, vkg = AsyncMock(), AsyncMock()
        nl_to_sparql.translate.side_effect = [_translation(), _translation(error=TranslationError.CANNOT_ANSWER)]
        vkg.resolve.side_effect = RuntimeError("Cannot translate the SPARQL fragment")
        context = _context()

        result = await OntopStrategy(nl_to_sparql=nl_to_sparql, vkg_translator=vkg).resolve("q", NS, context)

        assert result is None
        retry = [s for s in context.trace.steps if s.step == "t2.vkg.retry"]
        assert [(s.status, s.detail) for s in retry] == [("failed", {"reason": "cannot_answer"})]


class TestThroughTheChain:
    async def test_nl_to_sql_declines_and_the_default_chain_asks_ontop(self):
        """The real NL-to-SQL writer and strategy over the deployed graph, then a stand-in for Ontop."""
        ds = designer_v2_dataset()
        generator = SQLGenerator(_llm(_DECLINE), _vector(), unmapped_terms=_reader(ds))
        executor = _executor()
        nl_to_sql = NLtoSQLStrategy(sql_generator=generator, firewall=MagicMock(), query_executor=executor)
        answer = StrategyResult(
            sql="SELECT 1", rows=[{"n": 1}], columns=["n"], confidence=0.8, strategy_name="ontop", trace_steps=[]
        )
        ontop = MagicMock()
        ontop.name = StrategyOption.ONTOP
        ontop.resolve = AsyncMock(return_value=answer)
        context = _context()

        result = await StructuredQueryTier([ontop, nl_to_sql]).resolve(
            "Which clients are in the gold loyalty tier?", NS, context, option=StrategyOption.NL_TO_SQL_FIRST
        )

        assert result is answer
        executor.execute.assert_not_awaited()
        assert [(s.step, s.status) for s in context.trace.steps] == [
            ("t2.sql.generate", "skipped"),
            ("t2.sql.failed", "failed"),
        ]
        assert context.trace.steps[-1].detail == {"strategy": "nl_to_sql", "reason": "cannot_answer"}
