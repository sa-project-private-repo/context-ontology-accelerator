# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The terms of a namespace's ontology that no table maps, as the Tier 2 writers are told them.

Both writers see mapped terms only: the class gate keeps every class without
``coa:isMapped true`` out of their prompts, and deploy marks ``coa:isMapped false``
an attribute or relationship of a mapped class that no column or join maps (one the
Ontology designer added, for example), which the property gate leaves out too. A
question about such a term therefore reached a writer that had never heard of it, and
the writer answered with whatever looked closest: every client for "the gold loyalty
tier", a lifetime value threshold for "premium customers", the first order date for a
sign-up date the designer had deleted. Tier 2 returned that as its answer, so Tier 3,
which reads the documents that do answer it, never ran.

The writers are now also told these terms (:class:`UnmappedTermsReader`), with the
rule that a question about one of them, or about anything else the schema does not
hold, gets ``CANNOT_ANSWER`` instead of a query that stands in for it (:func:`declined`).
A strategy whose writer declines gives no result, so the question goes on to the next
strategy and then to Tier 3.

What is listed, all from the namespace's graphs:

- every attribute and relationship deploy marked ``coa:isMapped false``;
- every class of an induced ontology (``ontology_type`` ``induced``: tables, documents
  and designer drafts; not a foundational or uploaded ontology) that has no
  ``coa:isMapped true``, is no parent or equivalent of a mapped class (structured
  queries answer a parent through its mapped subclasses) and is not named like a
  mapped class or like a column or join of one (a class ``Order`` found in documents
  beside the table ``orders``, or ``Status`` beside the column ``orders.status``, is
  the same thing, and its questions are answered from the table).

A namespace without class markers (one from before them) lists nothing, so its
prompts stay exactly as they were, and so does every namespace that has nothing to
list. At most :data:`MAX_TERMS` terms are listed; each namespace is read at most once
a minute, and a failed read lists nothing for half a minute.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from ..clients.base import GraphClient
from ..clients.ttl_cache import TtlLruCache
from ..namespace_language import current_label_preference, row_label
from ..query_utils import (
    IS_MAPPED_IRI,
    MAPPED_DOMAIN_IRI,
    PHYSICAL_NAME_IRI,
    SPARQL_PREFIXES,
    get_graph_uri_template,
    namespace_graph_prefix,
    unmapped_property_filter,
    validate_namespace,
)
from ..served_version import ONTOLOGY_TYPE_IRI

logger = structlog.get_logger(__name__)

# What a writer replies, instead of a query, when the schema cannot answer the question.
CANNOT_ANSWER = "CANNOT_ANSWER"
# The code a decline is recorded with: NLtoSQLResult.error, and the reason of the failed
# step the chain records for a StrategyDeclined.
DECLINED = "cannot_answer"
# Terms listed in one prompt. A namespace with more lists its classes under a mapped
# parent and its marked properties first, the kinds a writer is most tempted to stand
# in for, then its other classes.
MAX_TERMS = 50
# Rows read per query: several per term (one per label, domain and range).
_READ_LIMIT = 2000
_CACHE_TTL_S = 60.0
# How long a failed or timed-out read lists nothing before the namespace is read again.
# One request reads up to three times (NL-to-SQL, Ontop and Ontop's retry), so a slow
# Neptune costs a request the read budget once, not three times.
_FAILED_READ_TTL_S = 30.0
_CACHE_NAMESPACES = 512
# The budget of one read, all its queries included; past it nothing is listed.
_READ_TIMEOUT_S = 3.0

_SKOS_ALT_LABEL = "http://www.w3.org/2004/02/skos/core#altLabel"
_OWL = "http://www.w3.org/2002/07/owl#"
_RDFS_DOMAIN = "http://www.w3.org/2000/01/rdf-schema#domain"

# The first code block of a reply: what follows the opening fence and its language tag.
_FIRST_BLOCK_RE = re.compile(r"```(?:[A-Za-z][A-Za-z0-9+-]*(?=\s))?\s*(.*?)(?:```|\Z)", re.DOTALL)
# Markdown a reply may put before the word: emphasis, a heading, a quote.
_MARKUP = " \t\r\n*_#>"
_CANNOT_ANSWER_RE = re.compile(rf"\b{CANNOT_ANSWER}\b")
_NAME_SEPARATORS_RE = re.compile(r"[\s_\-]+")
# Where an IRI's local name starts a new word: PurchaseOrder is "Purchase Order".
_CASE_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# English plurals a table may be named with that no ending rule reaches, by their singular.
_IRREGULAR_PLURALS = {"people": "person", "children": "child", "men": "man", "women": "woman"}
# The order a prompt lists the kinds of term in.
_KIND_ORDER = {"class": 0, "attribute": 1, "relationship": 2}


@dataclass(frozen=True)
class TermEnd:
    """A class an unmapped term belongs to or points at."""

    label: str
    # Its ``coa:physicalName``: the table, for a mapped class read from one.
    table: str = ""

    def name(self, *, physical: bool) -> str:
        """The table for the SQL writer, which knows the schema by table; else the label."""
        return (self.table or self.label) if physical else (self.label or self.table)


@dataclass(frozen=True)
class UnmappedTerm:
    """One term that no table maps."""

    iri: str
    label: str
    kind: str  # "class", "attribute" or "relationship"
    # A class: its mapped parents. A property: its domains.
    of: tuple[TermEnd, ...] = ()
    # A relationship: its ranges.
    to: tuple[TermEnd, ...] = ()


@dataclass(frozen=True)
class UnmappedTerms:
    """The terms listed to the writers, at most :data:`MAX_TERMS`, and how many more there are."""

    terms: tuple[UnmappedTerm, ...] = ()
    omitted: int = 0

    def __bool__(self) -> bool:
        """True when there is something to list (a prompt then gets the block and its rule)."""
        return bool(self.terms)


_NONE = UnmappedTerms()


def declined(reply: str) -> bool:
    """Whether a writer's reply is the ``CANNOT_ANSWER`` its prompt asks for instead of a query.

    The prompt asks for the word on the first line and no query. A reply that starts
    with it (after Markdown emphasis) declines whatever follows, and so does one whose
    first code block starts with it, as a model that reasons first and then answers in
    the code block its system prompt asks for writes it. A reply without a code block
    that writes it further on declines too. A reply whose first code block holds
    anything else is a query, even when its prose mentions the word.
    """
    if reply.lstrip(_MARKUP).startswith(CANNOT_ANSWER):
        return True
    block = _FIRST_BLOCK_RE.search(reply)
    if block is None:
        return _CANNOT_ANSWER_RE.search(reply) is not None
    return block.group(1).lstrip(_MARKUP).startswith(CANNOT_ANSWER)


def _name_keys(name: str) -> set[str]:
    """The forms ``name`` is compared in: case and separators left out, with and without an English plural.

    Two names are the same when their forms meet: ``orders`` and ``Order``, ``addresses``
    and ``Address``, ``companies`` and ``Company``, ``people`` and ``Person``.
    """
    key = _NAME_SEPARATORS_RE.sub(" ", name.casefold()).strip()
    keys = {key}
    head, _, last = key.rpartition(" ")
    if last in _IRREGULAR_PLURALS:
        keys.add(f"{head} {_IRREGULAR_PLURALS[last]}".strip())
    if key.endswith("ies") and len(key) > 4:
        keys.add(key[:-3] + "y")
    if key.endswith("es") and len(key) > 3:
        keys.add(key[:-2])
    if key.endswith("s") and not key.endswith("ss") and len(key) > 3:
        keys.add(key[:-1])
    return keys


def _local_name(iri: str) -> str:
    return re.split(r"[#/]", iri.rstrip("/#"))[-1]


def _ends(rows: list[dict[str, Any]]) -> tuple[TermEnd, ...]:
    """The distinct classes of ``rows`` (``?end`` with its labels and table), sorted by name."""
    labels: dict[str, list[tuple[str, str]]] = {}
    tables: dict[str, str] = {}
    for row in rows:
        end = row.get("end")
        if not end:
            continue
        labels.setdefault(end, []).append(row_label(row, "endLabel"))
        tables[end] = tables.get(end) or str(row.get("endName") or "")
    preference = current_label_preference()
    ends = {
        TermEnd(label=preference.pick(candidates) or _local_name(end), table=tables[end])
        for end, candidates in labels.items()
    }
    return tuple(sorted(ends, key=lambda e: (e.label.casefold(), e.table)))


def _line(term: UnmappedTerm, *, physical: bool) -> str:
    def names(ends: tuple[TermEnd, ...]) -> str:
        return " and ".join(dict.fromkeys(e.name(physical=physical) for e in ends))

    if term.kind == "class":
        return f"- {term.label} (a kind of {names(term.of)})" if term.of else f"- {term.label}"
    if term.kind == "attribute":
        return f"- {term.label} (an attribute of {names(term.of)})" if term.of else f"- {term.label} (an attribute)"
    ends = "".join(
        part for part, present in ((f" from {names(term.of)}", term.of), (f" to {names(term.to)}", term.to)) if present
    )
    return f"- {term.label} (a relationship{ends})"


def _block(terms: UnmappedTerms, *, physical: bool, heading: str, rule: str) -> str:
    if not terms:
        return ""
    lines = [_line(t, physical=physical) for t in terms.terms]
    if terms.omitted:
        lines.append(f"- and {terms.omitted} more")
    return "\n".join([heading, *lines, "", rule])


_SQL_HEADING = (
    "## Terms with no table behind them\n"
    "The ontology has these terms, but no table or column of this database holds their data:"
)
_SQL_RULE = (
    "A question about one of these terms, or about anything else no table or column above holds, "
    "cannot be answered with SQL over this schema. Do not answer it with a different table, column "
    "or filter instead (for example a threshold on some other column to stand for a category, or "
    "the date of a different event to stand for a missing date). Reply with CANNOT_ANSWER on the "
    "first line and the missing term on the second, and write no SQL. A question that the tables "
    "and columns above do answer is answered as usual, even when its words resemble a term listed here."
)
_SPARQL_HEADING = (
    "Terms with no table mapping (the ontology has them, but the VKG holds no data for them; "
    "they are not in the schema above):"
)
_SPARQL_RULE = (
    "A question about one of these terms, or about anything else no class or property above holds, "
    "cannot be answered with SPARQL over this schema. Do not answer it with a different class, "
    "property or FILTER instead (for example a threshold on some other property to stand for a "
    "category, or the date of a different event to stand for a missing date). Reply with "
    "CANNOT_ANSWER on the first line and the missing term on the second, and write no SPARQL. A "
    "question that the classes and properties above do answer is answered as usual, even when its "
    "words resemble a term listed here."
)


def sql_prompt_block(terms: UnmappedTerms) -> str:
    """The NL-to-SQL prompt's section on ``terms``, which names classes by table; empty when there are none."""
    return _block(terms, physical=True, heading=_SQL_HEADING, rule=_SQL_RULE)


def sparql_prompt_block(terms: UnmappedTerms) -> str:
    """The NL-to-SPARQL prompt's section on ``terms``, which names classes by label; empty when there are none."""
    return _block(terms, physical=False, heading=_SPARQL_HEADING, rule=_SPARQL_RULE)


def _scoped(body: str, graph_uri_prefix: str, *, induced: bool = False) -> str:
    """``body`` in each of the namespace's ontology graphs (only the induced ones, when ``induced``).

    Anchored on ``owl:Ontology``, one subject per graph, as the served-version read is,
    so the prefix filter stays cheap without the namespace's graphs resolved.
    """
    kind = f' ; <{ONTOLOGY_TYPE_IRI}> "induced"' if induced else ""
    return f'GRAPH ?g {{ ?ont a owl:Ontology{kind} .\n{body}\n  }}\n  FILTER(STRSTARTS(STR(?g), "{graph_uri_prefix}"))'


def _select(projection: str, body: str) -> str:
    return f"{SPARQL_PREFIXES}SELECT {projection} WHERE {{\n  {body}\n}} LIMIT {_READ_LIMIT}"


# A mapped class and its names: labels, synonyms and table.
_MAPPED_NAMES_BODY = f"""    ?term <{IS_MAPPED_IRI}> true .
    OPTIONAL {{
      {{ ?term rdfs:label ?name }}
      UNION {{ ?term <{_SKOS_ALT_LABEL}> ?name }}
      UNION {{ ?term <{PHYSICAL_NAME_IRI}> ?name }}
    }}"""
# The names of the attributes and relationships of mapped classes that deploy did not
# mark unmapped: labels, synonyms and columns, the schema's columns and joins.
_MAPPED_PROPERTY_NAMES_BODY = f"""    ?term rdfs:domain|<{MAPPED_DOMAIN_IRI}> ?class .
    ?class <{IS_MAPPED_IRI}> true .
    {unmapped_property_filter("?term")}
    {{ ?term rdfs:label ?name }}
    UNION {{ ?term <{_SKOS_ALT_LABEL}> ?name }}
    UNION {{ ?term <{PHYSICAL_NAME_IRI}> ?name }}"""
# A class with no mapping that no mapped class answers for, with its mapped parents.
_CLASSES_BODY = f"""    ?term a owl:Class .
    FILTER(isIRI(?term))
    FILTER NOT EXISTS {{ ?term <{IS_MAPPED_IRI}> true }}
    FILTER NOT EXISTS {{ ?mapped (rdfs:subClassOf|owl:equivalentClass)+ ?term . ?mapped <{IS_MAPPED_IRI}> true }}
    FILTER NOT EXISTS {{ ?term owl:equivalentClass ?same . ?same <{IS_MAPPED_IRI}> true }}
    OPTIONAL {{ ?term rdfs:label ?label }}
    OPTIONAL {{
      ?term rdfs:subClassOf ?end . ?end <{IS_MAPPED_IRI}> true .
      OPTIONAL {{ ?end rdfs:label ?endLabel }}
      OPTIONAL {{ ?end <{PHYSICAL_NAME_IRI}> ?endName }}
    }}"""
# A property deploy marked unmapped, with its kind, domains and ranges.
_PROPERTIES_BODY = f"""    ?term <{IS_MAPPED_IRI}> false .
    ?term a ?kind .
    FILTER(?kind IN (owl:DatatypeProperty, owl:ObjectProperty))
    OPTIONAL {{ ?term rdfs:label ?label }}
    OPTIONAL {{
      ?term ?axis ?end .
      FILTER(?axis IN (rdfs:domain, rdfs:range) && isIRI(?end))
      OPTIONAL {{ ?end rdfs:label ?endLabel }}
      OPTIONAL {{ ?end <{PHYSICAL_NAME_IRI}> ?endName }}
    }}"""


class UnmappedTermsReader:
    """Reads the terms of a namespace that no table maps, reusing a read for a minute.

    One reader serves both Tier 2 writers, so they list the same terms.
    """

    def __init__(
        self,
        graph: GraphClient,
        *,
        graph_uri_template: str | None = None,
        timeout_s: float = _READ_TIMEOUT_S,
        cache_ttl_s: float = _CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the graph client and the time budgets.

        Args:
            graph: Read-only SPARQL client for Neptune.
            graph_uri_template: Named-graph URI template; defaults to ``GRAPH_URI_TEMPLATE``.
            timeout_s: Budget of one read; past it nothing is listed.
            cache_ttl_s: How long a successful read of a namespace is reused, so a deploy
                is seen within this window.
            clock: Monotonic clock of both kept reads; injectable for tests.
        """
        self._graph = graph
        self._template = graph_uri_template
        self._timeout_s = timeout_s
        self._terms: TtlLruCache[str, UnmappedTerms] = TtlLruCache(
            ttl_s=cache_ttl_s, max_size=_CACHE_NAMESPACES, time_fn=clock
        )
        # The namespaces whose last read failed or timed out, read again once this expires.
        self._failed: TtlLruCache[str, bool] = TtlLruCache(
            ttl_s=_FAILED_READ_TTL_S, max_size=_CACHE_NAMESPACES, time_fn=clock
        )

    async def read(self, namespace: str) -> UnmappedTerms:
        """Return the terms of ``namespace`` that no table maps. Never raises.

        Empty for a namespace without class markers, when no graph URI template is
        configured, and when Neptune cannot be read in time. A failed read lists nothing
        for the next 30 s without asking Neptune again, so the writers of one request
        wait for it once, and is then read again.
        """
        cached = self._terms.get(namespace)
        if cached is not None:
            return cached
        if self._failed.get(namespace):
            return _NONE
        try:
            terms = await asyncio.wait_for(self._read(namespace), timeout=self._timeout_s)
        except Exception as e:  # noqa: BLE001 - a writer without the list writes as it did before
            logger.warning("unmapped_terms_read_failed", namespace=namespace, error=type(e).__name__)
            self._failed.set(namespace, True)
            return _NONE
        self._terms.set(namespace, terms)
        return terms

    async def _read(self, namespace: str) -> UnmappedTerms:
        validate_namespace(namespace)
        template = get_graph_uri_template(self._template)
        if not template:
            return _NONE
        prefix = namespace_graph_prefix(template, namespace)
        mapped = await self._graph.query(
            _select("?term ?name (LANG(?name) AS ?nameLang)", _scoped(_MAPPED_NAMES_BODY, prefix))
        )
        if not mapped:
            # No class markers: a namespace from before them, whose prompts stay as they were.
            return _NONE
        class_rows, property_rows, mapped_property_rows = await asyncio.gather(
            self._graph.query(
                _select(
                    "?term ?label (LANG(?label) AS ?labelLang) ?end ?endLabel (LANG(?endLabel) AS ?endLabelLang) "
                    "?endName",
                    _scoped(_CLASSES_BODY, prefix, induced=True),
                )
            ),
            self._graph.query(
                _select(
                    "?term ?kind ?label (LANG(?label) AS ?labelLang) ?axis ?end ?endLabel "
                    "(LANG(?endLabel) AS ?endLabelLang) ?endName",
                    _scoped(_PROPERTIES_BODY, prefix),
                )
            ),
            self._graph.query(_select("DISTINCT ?name", _scoped(_MAPPED_PROPERTY_NAMES_BODY, prefix))),
        )
        for rows, query in (
            (mapped, "mapped"),
            (class_rows, "classes"),
            (property_rows, "properties"),
            (mapped_property_rows, "mapped_properties"),
        ):
            if len(rows) >= _READ_LIMIT:
                logger.warning("unmapped_terms_read_truncated", namespace=namespace, query=query, limit=_READ_LIMIT)
        # What the schema names: the mapped classes by label, synonym, table and IRI (the
        # designer can rename the IRI and keep the label), and their columns and joins.
        mapped_names = [str(row["name"]) for row in (*mapped, *mapped_property_rows) if row.get("name")]
        mapped_names += [_CASE_BOUNDARY_RE.sub(" ", _local_name(str(row["term"]))) for row in mapped if row.get("term")]
        mapped_keys = set().union(*map(_name_keys, mapped_names))
        found = [*self._classes(class_rows, mapped_keys), *self._properties(property_rows)]
        # Classes under a mapped parent and marked properties first: see MAX_TERMS.
        found.sort(key=lambda t: (t.kind == "class" and not t.of, t.label.casefold(), t.iri))
        kept = sorted(found[:MAX_TERMS], key=lambda t: (_KIND_ORDER[t.kind], t.label.casefold(), t.iri))
        terms = UnmappedTerms(terms=tuple(kept), omitted=len(found) - len(kept))
        logger.info(
            "unmapped_terms_read",
            namespace=namespace,
            classes=sum(1 for t in found if t.kind == "class"),
            properties=sum(1 for t in found if t.kind != "class"),
            listed=len(kept),
        )
        return terms

    @staticmethod
    def _grouped(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row.get("term"):
                grouped.setdefault(row["term"], []).append(row)
        return grouped

    def _classes(self, rows: list[dict[str, Any]], mapped_keys: set[str]) -> list[UnmappedTerm]:
        preference = current_label_preference()
        classes: list[UnmappedTerm] = []
        for iri, term_rows in self._grouped(rows).items():
            labels = [row_label(row, "label") for row in term_rows]
            if any(_name_keys(text) & mapped_keys for text, _ in labels if text):
                continue
            label = preference.pick(labels) or _local_name(iri)
            classes.append(UnmappedTerm(iri=iri, label=label, kind="class", of=_ends(term_rows)))
        return classes

    def _properties(self, rows: list[dict[str, Any]]) -> list[UnmappedTerm]:
        preference = current_label_preference()
        properties: list[UnmappedTerm] = []
        for iri, term_rows in self._grouped(rows).items():
            relationship = any(row.get("kind") == f"{_OWL}ObjectProperty" for row in term_rows)
            label = preference.pick(row_label(row, "label") for row in term_rows) or _local_name(iri)
            domains = [row for row in term_rows if row.get("axis") == _RDFS_DOMAIN]
            # An attribute's range is a datatype, not a class.
            ranges = [row for row in term_rows if row.get("axis") and row.get("axis") != _RDFS_DOMAIN]
            properties.append(
                UnmappedTerm(
                    iri=iri,
                    label=label,
                    kind="relationship" if relationship else "attribute",
                    of=_ends(domains),
                    to=_ends(ranges) if relationship else (),
                )
            )
        return properties


__all__ = [
    "CANNOT_ANSWER",
    "DECLINED",
    "MAX_TERMS",
    "TermEnd",
    "UnmappedTerm",
    "UnmappedTerms",
    "UnmappedTermsReader",
    "declined",
    "sparql_prompt_block",
    "sql_prompt_block",
]
