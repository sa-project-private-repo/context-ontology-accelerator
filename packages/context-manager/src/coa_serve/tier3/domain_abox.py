# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Domain facts for the Tier 3 lexical path, from the namespace's domain ABox (spec §9).

Links the lexical entities graphrag retrieved to ABox instances, by lexical entity id and
by normalized-label key, follows typed relations one or two hops and renders them as
short fact lines with their source document. When retrieval returned no entity (graphrag's
TopicBeamSearch, the Standard default, builds none), it links the instances read from the
retrieved chunks instead: those an evidence node of one of the chunks is about. Used only
when the ABox conforms to the ontology version the namespace serves (both read from
Neptune); any failure or timeout skips this step, never the query.

Every query built here passes ``NeptuneGraphClient``'s write-keyword guard. The guard rejects
INSERT, DELETE, DROP, CREATE, LOAD, CLEAR, COPY, MOVE and ADD as words of the query, but it
blanks string literals and IRIs before it looks, and values only ever sit in one of those: a
lexical entity id or a chunk id between quotes, and only when it matches ``_ID`` (so it cannot
close them), an instance IRI between angle brackets, and an entity label only as its hex
``coa:normKey``. An id or IRI that contains one of those words (an instance of a class named
``Load`` has ``/Load/`` in its IRI) is therefore linked like any other.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import structlog
from coa_common import validate_namespace_name

from ..clients.base import GraphClient
from ..clients.ttl_cache import TtlLruCache
from ..query_utils import get_graph_uri_template, namespace_graph_prefix
from ..served_version import VERSION_RE, served_version_pattern
from ..step_ids import StepId
from .graph_traverser import GraphEntity

logger = structlog.get_logger(__name__)

_COA = "urn:coa:vocab#"
_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
# Neptune query hints: https://docs.aws.amazon.com/neptune/latest/userguide/sparql-query-hints-queryTimeout.html
_HINT_PREFIX = "PREFIX hint: <http://aws.amazon.com/neptune/vocab/v01/QueryHints#>"
_MAX_SEEDS = 10
_MAX_FACTS = 30
_TWO_HOP_BELOW = 10
_MAX_KEYS = 50
_HOP_LIMIT = 60
_MAX_LABEL = 160
_ID = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
_WS = re.compile(r"\s+")
_HOP_FIELDS = ("s", "sl", "sc", "p", "o", "ol", "oc")


def context_enabled() -> bool:
    """Return whether domain ABox facts are enabled (``ABOX_CONTEXT_ENABLED``, on unless ``false``)."""
    return os.environ.get("ABOX_CONTEXT_ENABLED", "true").strip().lower() != "false"


def norm_key(name: str) -> str:
    """Return the key ontology-engine stores as ``coa:normKey`` for ``name``.

    Must give the same output as ``coa_ontology.abox.model.norm_key``: NFKC, casefold, drop
    format characters (Unicode category ``Cf``), collapse whitespace, then the first 16 hex
    digits of the SHA-256.
    """
    folded = unicodedata.normalize("NFKC", name).casefold()
    visible = "".join(c for c in folded if unicodedata.category(c) != "Cf")
    return hashlib.sha256(_WS.sub(" ", visible).strip().encode("utf-8")).hexdigest()[:16]


def _local(iri: str) -> str:
    return unquote(re.split(r"[#/]", iri.rstrip("/#"))[-1])


def _text(value: str) -> str:
    """Make a label or file name safe to sit between quotes in a fact line, at most ``_MAX_LABEL`` characters."""
    return " ".join(value.split())[:_MAX_LABEL].replace("\\", "\\\\").replace('"', '\\"')


@dataclass(frozen=True)
class DomainFacts:
    """Fact lines read from the domain ABox for one query, and how many instances they start from."""

    version: str
    facts: tuple[str, ...]
    linked: int


class DomainAboxAugmenter:
    """Reads domain facts for a query's lexical entities, or else its retrieved chunks, from the namespace's ABox."""

    def __init__(
        self,
        neptune: GraphClient,
        *,
        graph_uri_template: str | None = None,
        timeout_s: float = 2.0,
        cache_ttl_s: float = 60.0,
    ) -> None:
        """Bind the graph client and the time budgets.

        Args:
            neptune: Read-only SPARQL client for Neptune.
            graph_uri_template: Named-graph URI template; defaults to ``GRAPH_URI_TEMPLATE``.
            timeout_s: Time budget of one ``augment`` call; over it the step is skipped. Each query also
                carries it as Neptune's ``queryTimeout`` hint, so an abandoned query does not keep running.
            cache_ttl_s: How long the served-version check of a namespace is reused.
        """
        self._neptune = neptune
        self._template = graph_uri_template
        self._timeout_s = timeout_s
        # (version, why there is none): a namespace with no usable ABox is cached too.
        self._versions: TtlLruCache[str, tuple[str | None, str]] = TtlLruCache(ttl_s=cache_ttl_s, max_size=512)

    async def augment(
        self, namespace: str, entities: list[GraphEntity], *, chunk_ids: Sequence[str] = ()
    ) -> tuple[DomainFacts | None, dict[str, Any]]:
        """Return the domain facts for ``entities`` and the trace step describing the attempt.

        Without entities, the instances are linked by ``chunk_ids``, the lexical chunk ids of the
        retrieved results, best-ranked first. Never raises: facts are ``None`` when the ABox is
        absent, stale, too slow or unreadable.
        """
        start = time.monotonic()

        def step(status: str, detail: str) -> dict[str, Any]:
            return {
                "step": StepId.DOMAIN_ABOX,
                "status": status,
                "durationMs": int((time.monotonic() - start) * 1000),
                "detail": detail,
            }

        if not entities and not chunk_ids:
            return None, step("skipped", "no lexical entities or chunks to link")
        try:
            result = await asyncio.wait_for(self._augment(namespace, entities, chunk_ids), timeout=self._timeout_s)
        except TimeoutError:
            return None, step("timeout", f"over the {self._timeout_s:g}s budget")
        except Exception as e:  # noqa: BLE001 - domain facts must never fail the query
            return None, step("error", type(e).__name__)
        if isinstance(result, str):
            return None, step("skipped", result)
        return result, step(
            "success", f"{result.linked} instances linked; {len(result.facts)} facts ({result.version})"
        )

    def _select(self, select: str, where: str, tail: str = "") -> str:
        """Build a SELECT whose WHERE clause opens with Neptune's ``queryTimeout`` hint."""
        timeout_ms = max(1, int(self._timeout_s * 1000))
        return f"{_HINT_PREFIX} {select} WHERE {{ hint:Query hint:queryTimeout {timeout_ms} . {where} }} {tail}".strip()

    def _abox_graph(self, namespace: str) -> str | None:
        template = get_graph_uri_template(self._template)
        if not template:
            return None
        return f"{template.split('{namespace}', 1)[0].rstrip('/')}/_abox/{namespace}"

    async def _served_version(self, namespace: str, abox_graph: str) -> tuple[str | None, str]:
        """Return the ontology version the ABox conforms to when the namespace serves it, else why not."""
        cached = self._versions.get(namespace)
        if cached is not None:
            return cached
        prefix = namespace_graph_prefix(get_graph_uri_template(self._template), namespace)
        served = served_version_pattern(prefix)
        built = f"GRAPH <{abox_graph}> {{ ?h a <{_COA}DomainAbox> ; <{_COA}conformsToVersion> ?a }}"
        rows = await self._neptune.query(self._select("SELECT DISTINCT ?v ?a", f"{{ {served} }} UNION {{ {built} }}"))
        deployed = {r["v"] for r in rows if VERSION_RE.match(r.get("v", ""))}
        conforms = {r["a"] for r in rows if r.get("a")}
        if not deployed:
            outcome = (None, "the ontology has no deployed version (vN) in Neptune yet")
        elif not conforms:
            outcome = (None, "no domain ABox has been built for this namespace")
        elif deployed != conforms:
            outcome = (
                None,
                f"the ABox conforms to {','.join(sorted(conforms))}, Neptune serves {','.join(sorted(deployed))}",
            )
        elif len(deployed) > 1:
            outcome = (None, f"Neptune serves several ontology versions ({','.join(sorted(deployed))})")
        else:
            outcome = (next(iter(deployed)), "")
        self._versions.set(namespace, outcome)
        return outcome

    async def _match(self, abox_graph: str, prop: str, values: list[str]) -> list[str]:
        """Return the ABox instances whose ``coa:{prop}`` is one of the plain ``values`` (smallest IRIs first)."""
        listed = " ".join(f'"{v}"' for v in values)
        rows = await self._neptune.query(
            self._select(
                "SELECT DISTINCT ?i",
                f"GRAPH <{abox_graph}> {{ VALUES ?v {{ {listed} }} ?i <{_COA}{prop}> ?v }}",
                f"ORDER BY ?i LIMIT {_MAX_SEEDS}",
            )
        )
        return list(dict.fromkeys(r["i"] for r in rows if r.get("i")))

    async def _link(self, abox_graph: str, entities: list[GraphEntity]) -> list[str]:
        """Return the ABox instances the lexical entities resolve to: entity-id matches first, then label keys."""
        ids = sorted({e.uri for e in entities if _ID.match(e.uri)})[:_MAX_KEYS]
        keys = sorted({norm_key(e.label) for e in entities if e.label})[:_MAX_KEYS]
        seeds = await self._match(abox_graph, "lexicalEntityId", ids) if ids else []
        if keys and len(seeds) < _MAX_SEEDS:
            seeds += [i for i in await self._match(abox_graph, "normKey", keys) if i not in seeds]
        return seeds[:_MAX_SEEDS]

    async def _link_chunks(self, abox_graph: str, chunk_ids: Sequence[str]) -> list[str]:
        """Return the ABox instances read from the retrieved chunks: those of the best-ranked chunk first, then by IRI.

        An instance was read from a chunk when an evidence node of the chunk (its ``coa:chunkId``) is
        about it: its type, or a relation or attribute it is the subject of.
        """
        plain = [c for c in dict.fromkeys(chunk_ids) if _ID.match(c)][:_MAX_KEYS]
        if not plain:
            return []
        ranked = " ".join(f'("{c}" {rank})' for rank, c in enumerate(plain))
        rows = await self._neptune.query(
            self._select(
                "SELECT ?i (MIN(?r) AS ?rank)",
                f"GRAPH <{abox_graph}> {{ VALUES (?c ?r) {{ {ranked} }} ?e <{_COA}chunkId> ?c ; <{_COA}about> ?i }}",
                f"GROUP BY ?i ORDER BY ?rank ?i LIMIT {_MAX_SEEDS}",
            )
        )
        return list(dict.fromkeys(r["i"] for r in rows if r.get("i")))

    async def _hop(self, abox_graph: str, seeds: list[str]) -> list[tuple[str, str, str]]:
        """Return ``(fact line, subject, object)`` for the typed relations that touch ``seeds``."""
        if not seeds:
            return []
        values = " ".join(f"<{s}>" for s in seeds)
        internal = " && ".join(f"?p != <{_COA}{name}>" for name in ("about", "object", "predicate"))
        # Outgoing links are read off the seed. Incoming ones are found through their evidence nodes
        # (the object-first lookup has an index in Neptune's default set; a bare ``?s ?p ?seed`` has none).
        where = (
            f"GRAPH <{abox_graph}> {{ VALUES ?seed {{ {values} }} "
            f"{{ ?seed ?p ?o . BIND(?seed AS ?s) }} UNION "
            f"{{ ?ev <{_COA}object> ?seed ; <{_COA}about> ?s ; <{_COA}predicate> ?p . BIND(?seed AS ?o) }} "
            f"FILTER(isIRI(?o) && ?p != <{_RDF_TYPE}> && {internal}) "
            f"?s a ?sc ; <{_RDFS_LABEL}> ?sl . ?o a ?oc ; <{_RDFS_LABEL}> ?ol . "
            f"FILTER(?sc != <{_COA}Evidence> && ?oc != <{_COA}Evidence>) "
            f"OPTIONAL {{ ?e <{_COA}about> ?s ; <{_COA}predicate> ?p ; <{_COA}object> ?o ; <{_COA}sourceKey> ?src }} }}"
        )
        # One row per statement, with the smallest source key of its evidence, in a fixed order.
        rows = await self._neptune.query(
            self._select(
                "SELECT ?s ?sl ?sc ?p ?o ?ol ?oc (MIN(?src) AS ?doc)",
                where,
                f"GROUP BY ?s ?sl ?sc ?p ?o ?ol ?oc ORDER BY ?s ?p ?o LIMIT {_HOP_LIMIT}",
            )
        )
        best: dict[tuple[str, str, str], tuple[str, str, str, str, str]] = {}
        for r in rows:
            if all(r.get(f) for f in _HOP_FIELDS):
                best.setdefault((r["s"], r["p"], r["o"]), (r["sc"], r["sl"], r["oc"], r["ol"], r.get("doc", "")))
        out = []
        for (s, p, o), (sc, sl, oc, ol, doc) in sorted(best.items()):
            line = f'{_local(sc)} "{_text(sl)}" -{_local(p)}-> {_local(oc)} "{_text(ol)}"'
            if doc:
                line += f" [source: {_text(doc.rsplit('/', 1)[-1])}]"
            out.append((line, s, o))
        return out

    async def _augment(
        self, namespace: str, entities: list[GraphEntity], chunk_ids: Sequence[str]
    ) -> DomainFacts | str:
        """Return the facts, or the reason there are none to give."""
        validate_namespace_name(namespace)
        abox_graph = self._abox_graph(namespace)
        if abox_graph is None:
            return "GRAPH_URI_TEMPLATE is not configured"
        version, why = await self._served_version(namespace, abox_graph)
        if version is None:
            return why
        if entities:
            seeds = await self._link(abox_graph, entities)
        else:
            seeds = await self._link_chunks(abox_graph, chunk_ids)
        facts = await self._hop(abox_graph, seeds)
        if len(facts) < _TWO_HOP_BELOW:
            neighbours = {x for _, s, o in facts for x in (s, o)} - set(seeds)
            try:
                facts += await self._hop(abox_graph, sorted(neighbours)[:_MAX_SEEDS])
            except Exception as e:  # noqa: BLE001 - the first hop's facts are still good
                logger.warning("domain_abox_second_hop_failed", error_type=type(e).__name__)
        lines = list(dict.fromkeys(line for line, _, _ in facts))
        return DomainFacts(version, tuple(lines[:_MAX_FACTS]), len(seeds))
