# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Validate LLM-generated SPARQL: syntax + URI existence check.

Two-stage validation:
1. Syntax validation using rdflib's SPARQL parser
2. URI existence check — extract IRIs, verify against published ontology in Neptune

If validation fails, the pipeline falls through to Tier 3 with the failed
SPARQL as a hint for the knowledge retriever.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog
from coa_common.constants import URN_PREFIX as _URN_PREFIX
from rdflib.paths import Path
from rdflib.plugins.sparql import prepareQuery
from rdflib.term import URIRef

from ...clients.base import GraphClient
from ...clients.ttl_cache import TtlLruCache
from ...query_utils import (
    IS_MAPPED_IRI,
    SPARQL_PREFIXES,
    get_graph_uri_template,
    graph_scoped_body,
    namespace_graph_prefix,
    unmapped_property_filter,
    validate_namespace,
)

logger = structlog.get_logger(__name__)

_SAFE_URI_RE = re.compile(r"^https?://[^\s<>{}\"|\\^`'();]+\Z")  # \Z, not $, so a trailing newline is rejected

# Max URIs to validate per query. LLM-generated SPARQL can reference many IRIs;
# validating all would produce large Neptune VALUES clauses risking timeouts.
# 10 is sufficient to catch hallucinated URIs (the primary failure mode).
_MAX_URI_VALIDATION_COUNT = 10
# Max terms the query console's pre-translation check reads. Higher than the cap
# above because a miss here is not a hallucination but a real term with no mapping,
# and a multi-join query names more than ten terms; one VALUES clause keeps it cheap.
_MAX_VKG_TERM_CHECK_COUNT = 50
# The properties deploy marked unmapped are read per namespace and kept this long, so a
# validation costs no Neptune query for them and a deploy is seen within a minute (the
# served version is read on the same schedule, see ``served_version.ServedVersionReader``).
_UNMAPPED_CACHE_TTL_S = 60.0
_UNMAPPED_CACHE_NAMESPACES = 512
# Rows read for one namespace's marked properties. A namespace with more keeps the first
# ones read, and the rest are checked by existence only, as before they were marked.
_MAX_UNMAPPED_READ = 10_000
# A prefixed name's escaped local-name character (``ind:esc\-aped``), which SPARQL reads
# without its backslash; rdflib keeps the backslash in the IRI.
_PN_LOCAL_ESC_RE = re.compile(r"\\([_~.\-!$&'()*+,;=/?#@%])")

# GovernedMetric URI pattern (<urn:{prefix}:{ns}:metric:{name}>). Metric
# IRIs live under urn:{prefix}: (not the http(s) ontology base), so the existing
# http-only URI-existence check skips them — recognizing the pattern lets the
# validator treat a well-formed metric IRI as valid rather than "not found".

_GOVERNED_METRIC_URI_RE = re.compile(rf"^urn:{re.escape(_URN_PREFIX)}:[^\s:]+:metric:[^\s>]+$")

# What the query console says about a term of a caller's query that the VKG cannot answer.
NOT_A_TERM = "<{iri}> is not a class or property of the deployed ontology."
NO_TABLE_MAPPING = "<{iri}> has no table mapping, so the VKG cannot answer it."
# A class or property is linked when the graph declares another term its subclass or subproperty, or
# an equivalent of it: Ontop answers it through that term, so having no marker of its own refuses nothing.
_LINKS = "rdfs:subClassOf|owl:equivalentClass|rdfs:subPropertyOf|owl:equivalentProperty"
_EQUIVALENCES = "owl:equivalentClass|owl:equivalentProperty"

__all__ = ["SPARQLValidator", "ValidationResult"]


@dataclass
class ValidationResult:
    """Outcome of SPARQL validation."""

    valid: bool
    error: str | None = None


class SPARQLValidator:
    """Validates LLM-generated SPARQL for syntax correctness and URI existence."""

    def __init__(
        self,
        graph_client: GraphClient,
        graph_uri_template: str | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        """Bind the graph client and named-graph URI template for validation.

        Args:
            graph_client: Graph client used for URI existence checks.
            graph_uri_template: Optional named-graph URI template; a default is
                resolved when None.
            clock: Monotonic clock for the kept read of each namespace's unmapped
                properties; injectable for tests.
        """
        self._graph = graph_client
        self._graph_uri_template = get_graph_uri_template(graph_uri_template)
        self._unmapped: TtlLruCache[str, frozenset[str]] = TtlLruCache(
            ttl_s=_UNMAPPED_CACHE_TTL_S, max_size=_UNMAPPED_CACHE_NAMESPACES, time_fn=clock
        )

    async def validate(self, sparql: str, namespace: str, graph_iris: list[str] | None = None) -> ValidationResult:
        """Four-stage validation: syntax, URI existence, domain/range, determinism.

        Stages run in order: syntax → URI existence → domain/range (fail-open)
        → determinism/projection (fail-closed).

        Args:
            sparql: The SPARQL query to validate.
            namespace: Namespace for URI existence checking in Neptune.
            graph_iris: The namespace's named graphs, if the caller already resolved
                them (``TBoxContext.graph_iris``). Scopes stages 2 and 3 to those
                graphs instead of matching in every graph on the cluster; omitting
                them keeps the prefix-filter form. Because the resolved set is a
                SUBSET of the prefix-matching one, a rejection is always re-checked
                unscoped before it is returned, so the verdict is unchanged either
                way -- see :meth:`_validate_uris`.

        Returns:
            ValidationResult indicating whether the SPARQL is valid.
        """
        if not sparql or not sparql.strip():
            return ValidationResult(valid=False, error="Empty SPARQL")

        # Stage 1: Syntax validation
        syntax_result = self._validate_syntax(sparql)
        if not syntax_result.valid:
            return syntax_result

        # Stage 2: URI existence check
        validate_namespace(namespace)
        uri_result = await self._validate_uris(sparql, namespace, graph_iris=graph_iris)
        if not uri_result.valid:
            return uri_result

        # Stage 3: conservative domain/range compatibility check.
        # Catches the "property used on the wrong class" class of semantic error
        # (e.g. ins:policyNumber on a Claim when its domain is Policy). Fail-OPEN:
        # only a CLEAR, evidenced mismatch fails; any uncertainty (no triples to
        # check, Neptune error, ambiguous typing) passes through unchanged so we
        # never over-block a legitimate query.
        dr_result = await self._validate_domain_range(sparql, namespace, graph_iris=graph_iris)
        if not dr_result.valid:
            return dr_result

        # Stage 4: determinism / projection check (B1a + B1b). Fail-CLOSED on
        # DETERMINISTICALLY detectable defects only: (a) an aggregate projection with
        # no explicit `AS ?alias`, or aliased to a non-descriptive bare name that
        # drifts run-to-run (?count/?value/?result/?n) — the direct cause of the
        # ~1-in-3 KeyError for typed clients; and (b) a plain COUNT(?v) over a
        # variable typed as a class instance (?v a :Class), which counts
        # join-multiplied rows and drifts between COUNT and COUNT(DISTINCT) run to
        # run (measured 2/10 on the HAVING/GROUP-BY family). Both are wrong
        # regardless of the question. It does NOT judge LIMIT or the COUNT-vs-DISTINCT
        # choice for LITERAL-valued counts — those need the NL question's intent (a
        # prompt-side rule + the determinism regression suite own them); guessing
        # there would over-block. Column NAME drift between two correct names
        # (?id vs ?claimId) is likewise left to the prompt — both are valid, so
        # failing closed would reject correct output.
        return self._validate_projection_aliases(sparql)

    # Aggregate functions whose SELECT projection must carry a stable, explicit,
    # descriptive alias — a bare/absent alias is where B1b column-name drift
    # (?productCount vs ?count) originates.
    _AGG_FUNCS = ("COUNT", "SUM", "AVG", "MIN", "MAX", "GROUP_CONCAT", "SAMPLE")

    # Non-descriptive aliases that the LLM swaps between runs. Rejected so the
    # retry loop regenerates with the descriptive name the prompt mandates.
    # NOTE: "total" is intentionally NOT here — it is a sanctioned prompt example
    # (?total) and a prefix of legitimately descriptive names (?totalRevenue).
    _BARE_ALIASES = frozenset({"count", "value", "result", "n", "a", "b", "c", "x", "y", "z", "col", "res"})

    _AGG_NAME_RE = re.compile(r"\b(?:" + "|".join(_AGG_FUNCS) + r")\s*\(", re.IGNORECASE)
    # Capture the projection list: everything between SELECT and the graph
    # pattern. The pattern starts at `WHERE` OR — since `WHERE` is grammatically
    # OPTIONAL in SPARQL — at the opening `{`, whichever comes first. Anchoring
    # only on `WHERE` would let a `WHERE`-less `SELECT (COUNT(?p) AS ?count) {…}`
    # bypass the check entirely (rdflib accepts it, so Stage 1 does not backstop).
    _SELECT_CLAUSE_RE = re.compile(r"\bSELECT\b(?:\s+DISTINCT)?(.*?)(?:\bWHERE\b|\{)", re.IGNORECASE | re.DOTALL)
    _TRAILING_AS_RE = re.compile(r"\bAS\s+\?(\w+)\s*$", re.IGNORECASE)

    def _validate_projection_aliases(self, sparql: str) -> ValidationResult:
        """Fail-closed when an aggregate projection lacks a stable descriptive alias.

        Two rejectable shapes, both deterministically detectable from the SPARQL
        text alone (no Neptune round-trip, no NL intent needed):

        1. An aggregate projection expression with NO trailing ``AS ?alias`` — the
           engine then invents a column label that varies run-to-run.
        2. An aggregate aliased to a non-descriptive bare name in ``_BARE_ALIASES``
           — the exact ``?productCount`` → ``?count`` drift B1b documents.

        Detection walks the SELECT clause's TOP-LEVEL parenthesised projection
        groups (SPARQL only permits an aggregate inside a ``( Expression AS Var )``
        group, so one group may legitimately hold several aggregates — e.g. a
        ratio of two SUMs — under a single alias; counting raw aggregate calls
        would false-reject those). Everything else PASSES: plain-variable
        projections, descriptive aliases, and anything unparseable (conservative
        — a genuinely malformed SELECT is Stage-1 rdflib's to reject, not this
        stage's). A bare aggregate with no wrapping group at all is likewise
        invalid SPARQL caught by Stage 1; the outside-group scan below still
        flags it defensively.
        """
        # Mask string literals before locating the SELECT/WHERE boundary so a
        # quoted `{`, `}` or `WHERE` inside a projection expression (e.g.
        # `BIND(CONCAT("{", ?x) AS ?y)`) cannot be mistaken for the graph-pattern
        # boundary and truncate the captured projection list. Masking preserves
        # length/offsets, so the span captured here maps back onto the original.
        m = self._SELECT_CLAUSE_RE.search(self._mask_string_literals(sparql))
        if m is None:
            return ValidationResult(valid=True)  # not a SELECT / unparseable → pass
        # Re-slice from the ORIGINAL sparql using the matched span (mask is
        # length-preserving) so downstream alias checks see real text.
        select_clause = sparql[m.start(1) : m.end(1)]

        groups, spans = self._top_level_paren_groups(select_clause)

        # Scan for an aggregate that sits OUTSIDE every top-level projection group
        # (a stray, unwrapped aggregate). Blank the group spans first so their
        # inner aggregates don't count here.
        outside = list(select_clause)
        for a, b in spans:
            for i in range(a, b):
                outside[i] = " "
        if self._AGG_NAME_RE.search("".join(outside)):
            return ValidationResult(
                valid=False,
                error=(
                    "Aggregate projection missing explicit alias — every "
                    "COUNT/SUM/AVG/MIN/MAX must be wrapped as "
                    "`(AGG(...) AS ?descriptiveName)` so the result column name is "
                    "stable across runs."
                ),
            )

        for group in groups:
            if not self._AGG_NAME_RE.search(group):
                continue  # non-aggregate projection expr (e.g. a bound BIND-alike)
            alias_match = self._TRAILING_AS_RE.search(group.strip())
            if not alias_match:
                return ValidationResult(
                    valid=False,
                    error=(
                        "Aggregate projection missing explicit alias — every "
                        "COUNT/SUM/AVG/MIN/MAX must use `(AGG(...) AS ?descriptiveName)` "
                        "so the result column name is stable across runs."
                    ),
                )
            if alias_match.group(1).lower() in self._BARE_ALIASES:
                return ValidationResult(
                    valid=False,
                    error=(
                        f"Aggregate alias '?{alias_match.group(1)}' is non-descriptive "
                        "and drifts between runs — use a stable descriptive name like "
                        "?productCount / ?totalRevenue / ?avgPrice."
                    ),
                )
        # B1a (entity-count determinism): a plain COUNT(?v) over a variable that is
        # bound as a CLASS INSTANCE (?v a :Class / ?v rdf:type :Class) counts
        # join-multiplied rows, not entities — on 1-to-many data it silently
        # inflates and the SAME question flips between COUNT and COUNT(DISTINCT)
        # run-to-run (measured N=10: 2/10 drift, answer masked only by a dataset
        # with no duplicate rows per group). This is decidable from the SPARQL text
        # ALONE — no NL intent needed — so it meets the same fail-closed bar as the
        # alias check: counting a typed entity is wrong-shape regardless of the
        # question. COUNT(?literal) over a datatype-property value (e.g.
        # COUNT(DISTINCT ?status)) is NOT flagged — only counts of typed entity
        # subjects are.
        entity_count = self._validate_entity_count_distinct(sparql)
        if not entity_count.valid:
            return entity_count
        return ValidationResult(valid=True)

    # A COUNT( ?v ) — single variable, NOT already DISTINCT, NOT COUNT(*).
    _PLAIN_COUNT_RE = re.compile(r"\bCOUNT\s*\(\s*(?!DISTINCT\b)\??(\w+)\s*\)", re.IGNORECASE)

    # ?v is a typed class instance: `?v a <Class>` or `?v rdf:type <Class>`, where
    # the type is an IRI (<...>), a prefixed name (ind:Claims), or `a` shorthand.
    # Also matches blank-node subjects (`_:b1 a :Class`) so a typed blank node is
    # not missed. Captures the subject name (var name, or blank-node label) in one
    # of two alternation groups.
    _TYPED_SUBJECT_RE = re.compile(
        r"(?:\?(\w+)|_:(\w+))\s+(?:a|rdf:type)\s+(?:<[^>]+>|\w*:\w+)",
        re.IGNORECASE,
    )

    def _validate_entity_count_distinct(self, sparql: str) -> ValidationResult:
        """Fail-closed when a plain ``COUNT(?v)`` counts a typed class instance.

        ``?v a :Class`` / ``?v rdf:type :Class`` marks ``?v`` as an ENTITY. Counting
        it with a bare ``COUNT`` (no DISTINCT) counts join-multiplied solution rows,
        so on 1-to-many joins the count inflates and the same question drifts between
        ``COUNT`` and ``COUNT(DISTINCT)`` between runs (the B1a instance measured on
        the HAVING/GROUP-BY family). Entity counts must be ``COUNT(DISTINCT ?v)``.

        Conservative — fires ONLY when BOTH hold, both read from the SPARQL text:
          1. a plain ``COUNT(?v)`` (single var, not already DISTINCT, not ``*``), and
          2. ``?v`` is a typed subject somewhere in the query (``?v a <...>`` or
             ``?v rdf:type <...>``).
        A count of a datatype-property value (``COUNT(DISTINCT ?status)``,
        ``COUNT(?amount)``) is left alone — those variables are not typed entities.
        """
        # findall returns (var, bnode) tuples (one group empty per match); collect
        # whichever side matched so both ?var and _:bnode typed subjects are covered.
        typed_vars = {v or b for v, b in self._TYPED_SUBJECT_RE.findall(sparql)}
        typed_vars.discard("")
        if not typed_vars:
            return ValidationResult(valid=True)
        for var in self._PLAIN_COUNT_RE.findall(sparql):
            if var in typed_vars:
                return ValidationResult(
                    valid=False,
                    error=(
                        f"COUNT(?{var}) counts a typed entity (?{var} is bound via "
                        "`a <Class>`) with row multiplicity — use "
                        f"COUNT(DISTINCT ?{var}) so the count is stable and does not "
                        "inflate on 1-to-many joins."
                    ),
                )
        return ValidationResult(valid=True)

    # Matches single- or double-quoted SPARQL string literals (with backslash
    # escapes), including the triple-quoted long forms, so their contents can be
    # blanked out before structural regexes run.
    _STRING_LITERAL_RE = re.compile(
        r"'''(?:\\.|[^\\])*?'''|\"\"\"(?:\\.|[^\\])*?\"\"\"|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"",
        re.DOTALL,
    )

    @classmethod
    def _mask_string_literals(cls, text: str) -> str:
        """Replace the CONTENTS of string literals with spaces, length-preserving.

        Quote delimiters are kept; only the inner characters become spaces. This
        lets structural regexes (SELECT/WHERE boundary, brace matching) run without
        being fooled by ``{``, ``}`` or keywords embedded inside string literals,
        while every character offset still maps back onto the original text.
        """

        def _blank(m: re.Match[str]) -> str:
            s = m.group(0)
            if len(s) < 2:
                return s
            # Preserve the opening/closing quote runs; blank the interior.
            q = "'" if s[0] == "'" else '"'
            n = 3 if s[:3] in ("'''", '"""') else 1
            open_q, close_q = q * n, q * n
            interior = " " * (len(s) - 2 * n)
            return f"{open_q}{interior}{close_q}"

        return cls._STRING_LITERAL_RE.sub(_blank, text)

    @staticmethod
    def _top_level_paren_groups(text: str) -> tuple[list[str], list[tuple[int, int]]]:
        """Return (inner_texts, spans) for each TOP-LEVEL ``( ... )`` group.

        Depth-aware so nested parens (function args, casts) stay inside their
        enclosing top-level group rather than being treated as separate
        projections. ``spans`` are (start, end) indices into ``text`` covering
        the whole group including its outer parens.
        """
        groups: list[str] = []
        spans: list[tuple[int, int]] = []
        depth = 0
        start: int | None = None
        for i, ch in enumerate(text):
            if ch == "(":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == ")":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start is not None:
                        groups.append(text[start + 1 : i])
                        spans.append((start, i + 1))
                        start = None
        return groups, spans

    def _validate_syntax(self, sparql: str) -> ValidationResult:
        """Parse SPARQL with rdflib to check syntax validity."""
        try:
            prepareQuery(sparql)
            return ValidationResult(valid=True)
        except Exception:
            logger.debug("sparql_syntax_error", sparql_length=len(sparql))
            return ValidationResult(valid=False, error="SPARQL syntax error")

    async def _validate_uris(
        self, sparql: str, namespace: str, graph_iris: list[str] | None = None
    ) -> ValidationResult:
        """Verify that URIs in the SPARQL exist in the namespace's published graph.

        ``graph_iris`` scopes the check to the namespace's graphs. Those are resolved
        by an ``owl:Ontology``-anchored query, so they are a SUBSET of the graphs
        under the namespace prefix: a subject that lives only in an unmarked graph is
        missing from the scoped count but present in the namespace. A shortfall is
        therefore confirmed against the full prefix before it becomes a rejection.
        """
        try:
            # GovernedMetric IRIs use the urn:coa:{ns}:metric:{name}
            # scheme — validate them by PATTERN (they aren't http subjects in the
            # ontology graph, so the existence check below would false-reject a
            # legitimate metric reference). A urn:coa:...:metric token that does
            # NOT match the strict pattern is a malformed/hallucinated metric IRI
            # → reject. Well-formed ones are accepted and excluded from the http
            # existence check.
            metric_urns = re.findall(rf"<(urn:{re.escape(_URN_PREFIX)}:[^>]+)>", sparql)
            for urn in metric_urns:
                if ":metric:" in urn and not _GOVERNED_METRIC_URI_RE.match(urn):
                    logger.warning("malformed_governed_metric_uri", urn=urn, namespace=namespace)
                    return ValidationResult(valid=False, error="Malformed GovernedMetric URI")

            uris = self._extract_uris(sparql)
            if not uris:
                return await self._refuse_unmapped_names(sparql, namespace)

            # Validate URI format (prevent injection)
            safe_uris = []
            for uri in uris[:_MAX_URI_VALIDATION_COUNT]:
                if not _SAFE_URI_RE.match(uri):
                    return ValidationResult(valid=False, error="URI contains invalid characters")
                safe_uris.append(uri)

            # Single round-trip batch check: count URIs that exist as subjects
            values_clause = " ".join(f"(<{uri}>)" for uri in safe_uris)
            graph_uri_prefix = namespace_graph_prefix(self._graph_uri_template, namespace)
            # COUNT(DISTINCT ?s) is what makes this safe to scope: a subject in two
            # of the namespace's graphs still counts once, so the threshold below is
            # unaffected by the query form. A property deploy marked unmapped is not
            # counted, so a query naming one gets the feedback of an unknown IRI.
            body = f"VALUES (?s) {{ {values_clause} }} ?s ?p ?o . {unmapped_property_filter('?s')}"
            found = await self._count_subjects(body, graph_uri_prefix, graph_iris)
            if found < len(safe_uris) and graph_iris:
                unscoped = await self._count_subjects(body, graph_uri_prefix, None)
                if unscoped > found:
                    logger.warning(
                        "graph_scope_recheck_saved_uri_check",
                        namespace=namespace,
                        graphs=len(graph_iris),
                        scoped_found=found,
                        unscoped_found=unscoped,
                    )
                found = unscoped

            if found < len(safe_uris):
                logger.warning(
                    "uri_batch_check_failed",
                    namespace=namespace,
                    expected=len(safe_uris),
                    found=found,
                    graphs=len(graph_iris or []),
                    uris=safe_uris[:5],
                )
                return ValidationResult(valid=False, error="URI not found in ontology")

            return await self._refuse_unmapped_names(sparql, namespace)
        except Exception as e:
            logger.debug("uri_validation_error", error=str(e))
            return ValidationResult(valid=False, error="URI validation failed")

    async def _refuse_unmapped_names(self, sparql: str, namespace: str) -> ValidationResult:
        """Refuse a query that names a property deploy marked unmapped, however it spells it.

        The writer is told to use prefixed names, and a query may also name a full IRI,
        one relative to its ``BASE``, or a prefixed name with the empty prefix or with
        ``-``, ``.``, ``%HH`` or an escape in its local name. Every IRI comes from the
        parsed query, so each spelling is the IRI SPARQL gives it, and all of them are
        checked, not only the first ones. They are compared with the namespace's marked
        properties (:meth:`_unmapped_properties`), so a name that merely does not exist
        passes as it always has, and a namespace without markers is judged exactly as
        before. Fails open: when the markers cannot be read, nothing is refused for them.
        """
        named = {iri for iri in self._query_iris(sparql) if not iri.startswith(self._STANDARD_PREFIXES)}
        if not named:
            return ValidationResult(valid=True)  # nothing a deploy marks
        named &= await self._unmapped_properties(namespace)
        if named:
            logger.warning("uri_unmapped_property", namespace=namespace, uris=sorted(named)[:5])
            return ValidationResult(valid=False, error="URI not found in ontology")
        return ValidationResult(valid=True)

    async def _unmapped_properties(self, namespace: str) -> frozenset[str]:
        """The properties deploy marked unmapped in ``namespace``, read at most once a minute.

        One read serves every validation of the namespace in that window (an answer
        validates its SPARQL up to three times), and only a successful read is kept, so
        a transient failure is read again next time. Read over the namespace's graph
        prefix: the marker matches few triples, and the prefix covers every graph the
        namespace's queries are checked against. Empty when the markers cannot be read.
        """
        cached = self._unmapped.get(namespace)
        if cached is not None:
            return cached
        graph_uri_prefix = namespace_graph_prefix(self._graph_uri_template, namespace)
        body = f"?s <{IS_MAPPED_IRI}> false ."
        try:
            rows = await self._graph.query(
                f"SELECT DISTINCT ?s WHERE {{ {graph_scoped_body(body, graph_uri_prefix, None)} }} "
                f"LIMIT {_MAX_UNMAPPED_READ}"
            )
        except Exception as e:
            logger.warning("unmapped_property_check_failed", namespace=namespace, error=type(e).__name__)
            return frozenset()
        if len(rows) >= _MAX_UNMAPPED_READ:
            logger.warning("unmapped_property_read_truncated", namespace=namespace, limit=_MAX_UNMAPPED_READ)
        unmapped = frozenset(row["s"] for row in rows if row.get("s"))
        self._unmapped.set(namespace, unmapped)
        return unmapped

    @staticmethod
    def _query_iris(sparql: str) -> set[str]:
        """Every IRI the parsed query names, as SPARQL resolves it (empty when it does not parse).

        Read from the query's algebra, so string contents, comments and prefix
        declarations name nothing, and a property path names each of its IRIs.
        """
        try:
            algebra = prepareQuery(sparql).algebra
        except Exception:
            return set()
        iris: set[str] = set()
        pending: list[Any] = [algebra]
        seen: set[int] = set()
        while pending:
            node = pending.pop()
            if isinstance(node, URIRef):
                iris.add(_PN_LOCAL_ESC_RE.sub(r"\1", str(node)))
                continue
            if id(node) in seen:
                continue
            seen.add(id(node))
            if isinstance(node, Path):
                pending.extend(vars(node).values())
            elif isinstance(node, dict):
                pending.extend(node.values())
            elif isinstance(node, (list, tuple, set, frozenset)):
                pending.extend(node)
        return iris

    async def refuse_vkg_terms(self, classes: list[str], properties: list[str], namespace: str) -> str | None:
        """Why the VKG cannot answer a query naming these classes and properties; ``None`` refuses nothing.

        The query console asks this before the VKG translates a caller's own query, with one Neptune
        query over the namespace's graphs. Standard vocabularies pass, as they do in :meth:`validate`.
        A term the graphs neither describe nor name as a parent or an equivalent of one of theirs is no
        term of the deployed ontology (:data:`NOT_A_TERM`). Structured queries cannot answer a property
        deploy marked ``coa:isMapped false``, nor a class with no ``coa:isMapped true`` that no other
        term links to (:data:`NO_TABLE_MAPPING`): a parent of a mapped class is answered through it.
        A namespace without class markers keeps today's checks, so nothing is refused there. At most
        50 terms are checked. Fails open: when the graphs cannot be read, nothing is refused.
        """
        terms = sorted(
            t for t in {*classes, *properties} if not t.startswith(self._STANDARD_PREFIXES) and _SAFE_URI_RE.match(t)
        )[:_MAX_VKG_TERM_CHECK_COUNT]
        if not terms:
            return None
        prefix = namespace_graph_prefix(self._graph_uri_template, namespace)
        values = "VALUES ?term { " + " ".join(f"<{t}>" for t in terms) + " }"
        described = graph_scoped_body(f"{values} ?term ?p ?o .", prefix)
        linked = graph_scoped_body(f"{values} {{ ?x {_LINKS} ?term }} UNION {{ ?term {_EQUIVALENCES} ?x }}", prefix)
        marked = graph_scoped_body(f"{values} ?term <{IS_MAPPED_IRI}> ?mapped .", prefix)
        any_class = graph_scoped_body(f"?term <{IS_MAPPED_IRI}> true .", prefix)
        sparql = (
            f"{SPARQL_PREFIXES}SELECT DISTINCT ?term ?fact WHERE {{\n"
            f'  {{ {described} BIND("term" AS ?fact) }}\n'
            f'  UNION {{ {linked} BIND("linked" AS ?fact) }}\n'
            f"  UNION {{ {marked} BIND(STR(?mapped) AS ?fact) }}\n"
            f'  UNION {{ SELECT ?term ?fact WHERE {{ {any_class} BIND("marker" AS ?fact) }} LIMIT 1 }}\n'
            "}"
        )
        try:
            rows = await self._graph.query(sparql)
        except Exception as e:
            logger.warning("vkg_terms_check_failed", namespace=namespace, error=type(e).__name__)
            return None
        facts: dict[str, set[str]] = {}
        for row in rows:
            facts.setdefault(row.get("term", ""), set()).add(row.get("fact", ""))
        if not any("marker" in f or "true" in f for f in facts.values()):
            return None
        unknown = [t for t in terms if not facts.get(t, set()) & {"term", "linked"}]
        unmapped = [
            t
            for t in terms
            if t not in unknown
            and ((t in properties and "false" in facts[t]) or (t in classes and not facts[t] & {"true", "linked"}))
        ]
        sentences = [NOT_A_TERM.format(iri=t) for t in unknown] + [NO_TABLE_MAPPING.format(iri=t) for t in unmapped]
        if sentences:
            logger.info("vkg_terms_refused", namespace=namespace, unknown=len(unknown), unmapped=len(unmapped))
        return " ".join(sentences) or None

    async def _count_subjects(self, body: str, graph_uri_prefix: str, graph_iris: list[str] | None) -> int:
        """Count distinct subjects matching ``body``, scoped to ``graph_iris`` if given."""
        scoped = graph_scoped_body(body, graph_uri_prefix, graph_iris)
        rows = await self._graph.query(f"SELECT (COUNT(DISTINCT ?s) AS ?found) WHERE {{ {scoped} }}")
        return int(rows[0]["found"]) if rows else 0

    async def _ask_scoped(self, body: str, graph_uri_prefix: str, graph_iris: list[str] | None) -> bool:
        """ASK ``body``, scoped to ``graph_iris`` if given."""
        return await self._graph.ask(f"ASK {{ {graph_scoped_body(body, graph_uri_prefix, graph_iris)} }}")

    _STANDARD_PREFIXES = (
        "http://www.w3.org/",
        "http://xmlns.com/",
        "http://purl.org/",
    )

    def _extract_uris(self, sparql: str) -> list[str]:
        """Extract domain-specific entity URIs (excludes standard vocabs and bare namespaces).

        GovernedMetric IRIs (``urn:coa:{ns}:metric:{name}``) are
        well-formed by pattern but not http subjects in the ontology graph, so
        they are excluded from the http URI-existence check (the regex only
        matches ``http`` URIs; this comment documents the intentional skip).
        """
        all_uris = re.findall(r"<(http[^>]+)>", sparql)
        return [u for u in all_uris if not u.startswith(self._STANDARD_PREFIXES) and not u.endswith(("#", "/"))]

    async def _validate_domain_range(
        self, sparql: str, namespace: str, graph_iris: list[str] | None = None
    ) -> ValidationResult:
        """Conservative property-domain and property-range compatibility check.

        For each triple ``?v a <Class>`` paired with ``?v <prop> ?o`` in the
        query, verify the property's declared ``rdfs:domain`` is compatible with
        ``Class``. Additionally, for typed literals (e.g. ``"5"^^xsd:integer``),
        verify the property's ``rdfs:range`` is compatible with the literal's type.

        Only a CLEAR mismatch fails. Everything else — unparseable patterns,
        properties with no declared domain/range, Neptune errors — PASSES (fail-open).
        """
        try:
            pairs = self._extract_typed_property_uses(sparql)
        except Exception as e:
            logger.warning("domain_range_extract_failed", error=str(e), namespace=namespace)
            return ValidationResult(valid=True)
        if not pairs:
            return ValidationResult(valid=True)

        # Limit to keep the Neptune round-trip bounded.
        pairs = pairs[:_MAX_URI_VALIDATION_COUNT]
        for class_uri, prop_uri in pairs:
            if not (_SAFE_URI_RE.match(class_uri) and _SAFE_URI_RE.match(prop_uri)):
                continue
            try:
                compatible = await self._is_domain_compatible(class_uri, prop_uri, namespace, graph_iris)
            except Exception as e:
                logger.debug("domain_range_check_skipped", error=str(e))
                continue  # fail-open on any error
            if compatible is False:
                logger.warning("domain_range_mismatch", class_uri=class_uri, prop_uri=prop_uri)
                return ValidationResult(valid=False, error="Property used on incompatible class (domain mismatch)")

        # Range validation: check typed literals against declared rdfs:range
        try:
            range_pairs = self._extract_property_literal_types(sparql)
        except Exception as e:
            logger.warning("property_literal_type_extract_failed", error=str(e), namespace=namespace)
            return ValidationResult(valid=True)
        for prop_uri, literal_type in range_pairs[:_MAX_URI_VALIDATION_COUNT]:
            if not (_SAFE_URI_RE.match(prop_uri) and _SAFE_URI_RE.match(literal_type)):
                continue
            try:
                compatible = await self._is_range_compatible(prop_uri, literal_type, namespace, graph_iris)
            except Exception as e:
                logger.debug("range_check_skipped", error=str(e))
                continue
            if compatible is False:
                logger.warning("range_mismatch", prop_uri=prop_uri, literal_type=literal_type)
                return ValidationResult(valid=False, error="Literal type incompatible with property range")

        return ValidationResult(valid=True)

    @staticmethod
    def _parse_prefixes(sparql: str) -> dict[str, str]:
        """Parse ``PREFIX p: <uri>`` declarations into a {prefix: uri} map."""
        prefixes: dict[str, str] = {}
        for m in re.finditer(r"PREFIX\s+(\w+):\s*<([^>]+)>", sparql, re.IGNORECASE):
            prefixes[m.group(1)] = m.group(2)
        return prefixes

    @staticmethod
    def _resolve_uri_token(token: str, prefixes: dict[str, str]) -> str | None:
        """Resolve a ``<http…>`` or ``prefix:Local`` token to a full http(s) IRI."""
        if token.startswith("http"):
            return token
        if ":" in token:
            pfx, _, local = token.partition(":")
            base = prefixes.get(pfx)
            if base:
                return f"{base}{local}"
        return None

    def _extract_typed_property_uses(self, sparql: str) -> list[tuple[str, str]]:
        """Extract (class_uri, property_uri) pairs where a typed var uses a property.

        Maps each variable to its asserted ``a <Class>`` type, then pairs it with
        each property applied to that variable. Handles BOTH explicit ``<http…>``
        IRIs AND prefixed names (``ind:Foo``) — expanding the latter via the
        query's PREFIX declarations — because the LLM is instructed to emit
        prefixed names, so a full-IRI-only check would rarely fire. Conservative:
        yields nothing on anything it cannot resolve (caller treats empty as
        "pass"), so it never false-rejects.
        """
        prefixes = self._parse_prefixes(sparql)

        def _resolve(t: str) -> str | None:
            return self._resolve_uri_token(t, prefixes)

        # A term is either <IRI> or a prefixed name (pfx:Local). Used for both
        # the class (after `a`/`rdf:type`) and the property position.
        term = r"(?:<(?:http[^>]+)>|\w+:\w+)"
        type_re = re.compile(rf"\?(\w+)\s+(?:a|rdf:type)\s+({term})")
        var_to_class: dict[str, str] = {}
        for m in type_re.finditer(sparql):
            cls = _resolve(m.group(2).strip("<>"))
            if cls:
                var_to_class[m.group(1)] = cls

        prop_re = re.compile(rf"\?(\w+)\s+({term})")
        pairs: list[tuple[str, str]] = []
        for m in prop_re.finditer(sparql):
            var = m.group(1)
            raw = m.group(2).strip("<>")
            # Skip the rdf:type predicate itself ('a' won't match \w+:\w+; rdf:type would).
            if raw in ("rdf:type",):
                continue
            prop = _resolve(raw)
            cls = var_to_class.get(var)
            if cls and prop and not prop.startswith(self._STANDARD_PREFIXES):
                pairs.append((cls, prop))
        return pairs

    def _extract_property_literal_types(self, sparql: str) -> list[tuple[str, str]]:
        """Extract (property_uri, xsd_type_uri) pairs from typed literal usage.

        Looks for patterns like ``?v <prop> "value"^^<xsd:type>`` or
        ``?v prefix:prop "value"^^xsd:type``. Conservative: yields nothing for
        patterns it can't parse (including escaped quotes in literals).
        """
        prefixes = self._parse_prefixes(sparql)

        def _resolve(t: str) -> str | None:
            return self._resolve_uri_token(t, prefixes)

        term = r"(?:<(?:http[^>]+)>|\w+:\w+)"
        # Match typed literals, handling escaped quotes with (?:[^"\\]|\\.)*
        typed_literal_re = re.compile(rf'\?(\w+)\s+({term})\s+"(?:[^"\\]|\\.)*"\^\^({term})')
        pairs: list[tuple[str, str]] = []
        for m in typed_literal_re.finditer(sparql):
            prop = _resolve(m.group(2).strip("<>"))
            dtype = _resolve(m.group(3).strip("<>"))
            if prop and dtype and not prop.startswith(self._STANDARD_PREFIXES):
                pairs.append((prop, dtype))

        return pairs

    async def _is_domain_compatible(
        self, class_uri: str, prop_uri: str, namespace: str, graph_iris: list[str] | None = None
    ) -> bool | None:
        """Return False on a clear domain mismatch, True if compatible, None if unknown.

        Asks Neptune: does ``prop`` declare an ``rdfs:domain`` that is NOT
        ``class`` and NOT a superclass of ``class``? If such a declared,
        incompatible domain exists → False. If the property has a compatible
        declared domain → True. If the property declares no domain (or it can't
        be determined) → None (treated as pass by the caller).
        """
        graph_uri_prefix = namespace_graph_prefix(self._graph_uri_template, namespace)
        # Does the property have ANY declared domain compatible with class?
        compat = f"<{prop_uri}> rdfs:domain ?d . <{class_uri}> rdfs:subClassOf* ?d ."
        if await self._ask_scoped(compat, graph_uri_prefix, graph_iris):
            return True
        # No compatible domain found — does it declare a domain at all?
        if await self._ask_scoped(f"<{prop_uri}> rdfs:domain ?d .", graph_uri_prefix, graph_iris):
            if graph_iris:
                # A compatible domain published in an unmarked graph would read as a
                # mismatch; confirm over the full prefix before rejecting.
                return await self._is_domain_compatible(class_uri, prop_uri, namespace, None)
            return False  # declares a domain, but none compatible with class → mismatch
        return None  # no declared domain → cannot judge → pass

    # XSD numeric type hierarchy: subtypes that are compatible with their supertypes.
    # e.g. xsd:integer is-a xsd:decimal, so a literal typed xsd:integer is compatible
    # with a property declared rdfs:range xsd:decimal.
    _XSD = "http://www.w3.org/2001/XMLSchema#"
    _XSD_SUBTYPES: dict[str, set[str]] = {
        f"{_XSD}decimal": {f"{_XSD}integer", f"{_XSD}long", f"{_XSD}int", f"{_XSD}short", f"{_XSD}byte"},
        f"{_XSD}integer": {f"{_XSD}long", f"{_XSD}int", f"{_XSD}short", f"{_XSD}byte"},
        f"{_XSD}long": {f"{_XSD}int", f"{_XSD}short", f"{_XSD}byte"},
        f"{_XSD}int": {f"{_XSD}short", f"{_XSD}byte"},
    }

    async def _is_range_compatible(
        self, prop_uri: str, literal_type: str, namespace: str, graph_iris: list[str] | None = None
    ) -> bool | None:
        """Return False on a clear range mismatch, True if compatible, None if unknown.

        Asks Neptune: does ``prop`` declare an ``rdfs:range`` that is incompatible
        with ``literal_type``? Same fail-open semantics as _is_domain_compatible.
        Also checks XSD type hierarchy (e.g. xsd:integer is compatible with
        rdfs:range xsd:decimal).
        """
        graph_uri_prefix = namespace_graph_prefix(self._graph_uri_template, namespace)
        # Does the property declare a range that exactly matches the literal type?
        if await self._ask_scoped(f"<{prop_uri}> rdfs:range <{literal_type}> .", graph_uri_prefix, graph_iris):
            return True
        # No exact match — does it declare a range at all?
        if not await self._ask_scoped(f"<{prop_uri}> rdfs:range ?r .", graph_uri_prefix, graph_iris):
            return None  # no declared range → pass
        # Range is declared but doesn't match exactly — check XSD subtype compatibility.
        # If the declared range accepts literal_type as a subtype, it's compatible.
        for supertype, subtypes in self._XSD_SUBTYPES.items():
            if literal_type in subtypes and await self._ask_scoped(
                f"<{prop_uri}> rdfs:range <{supertype}> .", graph_uri_prefix, graph_iris
            ):
                return True
        if graph_iris:
            # Same reason as the domain check: confirm over the full prefix.
            return await self._is_range_compatible(prop_uri, literal_type, namespace, None)
        return False  # declares a range, but not compatible with literal type
