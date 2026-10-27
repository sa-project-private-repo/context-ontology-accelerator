# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ontology_Lookup_Tool: the determined ontology for the request namespace (Req 3.1).

:class:`OntologyLookupTool` exposes the determined ontology — the complete set of
node types, the complete set of edge types, and the directional source→target
mapping per edge type (Req 3.1) — through the uniform :class:`Tool` contract, so
the reasoning controller invokes it the same way as every other Tool. It resolves
the ontology through a configurable :class:`OntologySource` (graph or file) and
returns the result in one source-agnostic shape regardless of which backend
resolved it (Req 3.6).

Stopgap scope. The tool currently returns the WHOLE determined ontology in a
single result — the wholesale-copy stopgap the retriever ships on today (the file
source materializes the entire ontology; see ``..ontology.source``). Follow-up
task 21 will allow query-shaped lookups against the live ontology graph instead
of copying it wholesale; the Tool contract here is shaped so that capability is
purely ADDITIVE (an item-carrying :class:`ToolResult` plus the namespace-scoped
invoke already accommodate narrower, input-driven queries), so no behavior here
changes for that follow-up.

Per-request caching (Req 3.8). The tool caches the resolved ontology keyed by
namespace, so a second lookup within the same request — which is always scoped to a
single namespace — reuses the cached ontology and performs a SINGLE source load.
The cache scope is the Tool instance: the registry (and therefore each Tool) is
built once and reused across requests, so a namespace-keyed cache gives the
required per-request reuse while remaining correct across requests for the same
namespace (the determined ontology is a slowly-changing per-namespace schema, not
per-request state). Only POPULATED loads are cached — a load failure OR an empty
ontology is never cached, so a later request (once the namespace's ontology is
induced/accepted, or a transient backend recovers) re-queries and self-heals. The
controller additionally caches the ontology on its accumulated context so it need
not re-invoke this tool at all within a request (Req 3.8); the tool-level cache is
the defensive second layer that keeps a redundant invocation cheap.

Deploys. Given a reader of the ontology version Neptune serves (the graph source
gets one), an entry also records the version it was loaded under, and a lookup
under another version loads again, so the first lookup after a deploy sees the
deployed ontology.

Empty result vs load failure are distinct outcomes (Req 3.10 vs 3.11). A successful
load that yields no node types and no edge types is an EMPTY ontology: the tool
returns a normal (non-degraded) :class:`ToolResult` carrying an ontology item with
empty type sets and a ``detail`` that says ``empty``. A source that RAISES is a load
FAILURE: the tool catches it (it never lets the exception escape ``invoke`` —
Req 9.1) and returns a degraded :class:`ToolResult` carrying NO ontology item and a
``detail`` that says ``failed`` and names the source in use. The two are
programmatically separable via :func:`ontology_from_result`, which returns an
:class:`Ontology` (possibly empty) for a successful lookup and ``None`` for a
failure, giving the controller a clean signal to record the empty result (Req 3.10)
or the failure-plus-source (Req 3.11) in the trace.

Import discipline (Requirement 12.3): module load imports only the dependency-free
``.base`` contract and the sibling ``..ontology`` abstraction (which itself imports
``rdflib`` lazily and never imports ``graphrag_toolkit``) plus ``structlog``, so
importing this module never pulls the toolkit into ``sys.modules``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from ..ontology import Ontology
from .base import ToolResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ....served_version import ServedVersionReader
    from ..ontology import OntologySource
    from .registry import ToolRegistry

logger = structlog.get_logger(__name__)

TOOL_NAME = "ontology_lookup"

# Item ``type`` discriminator for the single ontology item a successful lookup
# emits; :func:`ontology_from_result` keys on it to reconstruct the Ontology.
_ONTOLOGY_ITEM_TYPE = "ontology"

_DESCRIPTION = (
    "Ontology lookup for the request namespace. Returns the determined ontology: "
    "the set of node types, the set of edge types, and the directional "
    "source->target mapping for each edge type. Consult this BEFORE choosing a "
    "graph traversal so you only traverse edge types that exist in the ontology. "
    "The ontology is loaded once per namespace and reused for the rest of the "
    "request, so a repeat lookup is free."
)

# The ontology tool takes no planner-supplied inputs: the namespace is supplied by
# the controller to every Tool's invoke. An empty input_schema is a valid, declared
# contract (the registry permits an empty dict — a tool that accepts no inputs).
_INPUT_SCHEMA: dict = {}
_OUTPUT_SCHEMA: dict = {
    "items": "list (one ontology item with node_types, edge_types, edge_map)",
}


def _ontology_item(ontology: Ontology) -> dict:
    """Normalize an :class:`Ontology` to a plain dict for ``ToolResult.items``.

    The ``edge_map`` triples are emitted as lists so the item is a plain,
    JSON-friendly dict; :func:`ontology_from_result` rebuilds the tuples.
    """
    return {
        "type": _ONTOLOGY_ITEM_TYPE,
        "node_types": list(ontology.node_types),
        "edge_types": list(ontology.edge_types),
        "edge_map": [list(triple) for triple in ontology.edge_map],
    }


def ontology_from_result(result: ToolResult) -> Ontology | None:
    """Reconstruct the :class:`Ontology` from an Ontology_Lookup_Tool result.

    Returns the resolved :class:`Ontology` (which may be empty — empty type sets
    are a valid, successful lookup) for a successful lookup, or ``None`` when the
    result carries no ontology item (a load failure). This is the clean signal the
    controller uses to tell an EMPTY ontology (Req 3.10) apart from a load FAILURE
    (Req 3.11): an empty success yields an :class:`Ontology` with empty tuples,
    while a failure yields ``None``.
    """
    for item in result.items:
        if isinstance(item, dict) and item.get("type") == _ONTOLOGY_ITEM_TYPE:
            return Ontology(
                node_types=tuple(item.get("node_types", ()) or ()),
                edge_types=tuple(item.get("edge_types", ()) or ()),
                edge_map=tuple(tuple(triple) for triple in (item.get("edge_map", ()) or ())),
            )
    return None


class OntologyLookupTool:
    """Returns the ontology in one shape regardless of source (Req 3.1, 3.6).

    The four uniform interface fields are static, graphrag-free data set at
    construction, so the Tool is registrable and describable to the planner
    without touching any backend. The resolved ontology is cached per namespace
    (Req 3.8); empty results and load failures are distinct outcomes
    (Req 3.10 vs 3.11).
    """

    def __init__(
        self,
        source: OntologySource,
        *,
        source_name: str | None = None,
        served_versions: ServedVersionReader | None = None,
    ) -> None:
        """Build the Tool over an :class:`OntologySource`.

        Args:
            source: The configured ontology backend (graph or file). Its ``load``
                returns an :class:`Ontology` and raises on a backend failure; this
                Tool turns that raise into a degraded result rather than
                propagating it (Req 9.1).
            source_name: Human-readable label for the source in use, recorded in
                the failure ``detail`` so the trace can name the source that failed
                (Req 3.11). Defaults to the source's class name.
            served_versions: Optional reader of the ontology version Neptune serves.
                When given, a cached ontology is reused only while that version is
                the one it was loaded under, so a deploy invalidates it. Without
                it (the file source) the cache lasts as long as the Tool.
        """
        self.name = TOOL_NAME
        self.description = _DESCRIPTION
        self.input_schema = dict(_INPUT_SCHEMA)
        self.output_schema = dict(_OUTPUT_SCHEMA)
        self._source = source
        self._source_name = source_name or type(source).__name__
        self._served_versions = served_versions
        # Per-namespace cache of successful loads (Req 3.8), each with the served
        # versions it was loaded under. Failures are not cached so a later attempt
        # can retry.
        self._cache: dict[str, tuple[frozenset[str] | None, Ontology]] = {}

    async def invoke(self, *, namespace: str, **_: Any) -> ToolResult:
        """Resolve the ontology for ``namespace`` and return a :class:`ToolResult`.

        Reuses the cached ontology when this namespace has already been resolved
        during the Tool's lifetime, so a repeat lookup performs no source load
        (Req 3.8). On a fresh namespace it loads through the source, caches a
        successful result, and returns it. Never raises for a load failure: the
        source's exception is caught and surfaced as a degraded result that names
        the source in use (Req 3.11, 9.1).

        Args:
            namespace: The originating request namespace; the lookup and the cache
                are scoped to it (Req 1.5).

        Returns:
            A :class:`ToolResult`. On success it carries a single ontology item
            (extractable via :func:`ontology_from_result`) and a ``detail`` that
            distinguishes a populated from an ``empty`` ontology. On failure it
            carries no ontology item and a ``detail`` that says ``failed`` and names
            the source.
        """
        served = await self._served(namespace)
        cached = self._cache.get(namespace)
        # An unreadable version (None) keeps the cached ontology, as before versions.
        if cached is not None and (served is None or cached[0] == served):
            return self._success_result(cached[1], from_cache=True)

        try:
            ontology = await self._source.load(namespace)
        except Exception as exc:  # noqa: BLE001 - a Tool must never raise (Req 9.1)
            logger.warning(
                "ontology_lookup_failed",
                tool=self.name,
                source=self._source_name,
                namespace=namespace,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # Load FAILURE (Req 3.11): no ontology item, detail names the source.
            return ToolResult(detail=f"{self.name} failed: {type(exc).__name__} (source={self._source_name})")

        # Cache ONLY a populated ontology. An empty result (0 node types AND 0 edge
        # types) is far more likely transient — the namespace's ontology hasn't been
        # induced/accepted YET, or a backend hiccup returned nothing — than a real
        # "this namespace permanently has no ontology". Caching empty would poison
        # the tool for the whole container lifetime: a query that ran BEFORE an
        # accept completed would pin 0/0 even after the ontology lands (observed
        # 2026-07-24 — a stale empty cache served 0/0 while a fresh instance saw
        # 166/360 for the same namespace). Leaving empty uncached costs one re-query
        # next time and self-heals once the ontology is populated.
        if ontology.node_types or ontology.edge_types:
            self._cache[namespace] = (served, ontology)
        return self._success_result(ontology, from_cache=False)

    async def _served(self, namespace: str) -> frozenset[str] | None:
        """Return the versions Neptune serves for ``namespace``, or ``None`` when unknown."""
        if self._served_versions is None:
            return None
        try:
            return await self._served_versions.versions(namespace)
        except Exception as exc:  # noqa: BLE001 - a Tool must never raise (Req 9.1)
            logger.warning("ontology_lookup_version_unread", namespace=namespace, error_type=type(exc).__name__)
            return None

    def _success_result(self, ontology: Ontology, *, from_cache: bool) -> ToolResult:
        """Build the :class:`ToolResult` for a successful (possibly empty) load.

        An empty ontology (no node types and no edge types) is a valid success
        (Req 3.10); its ``detail`` carries the ``empty`` marker so it is distinct
        from both a populated ontology and a load failure.
        """
        node_count = len(ontology.node_types)
        edge_count = len(ontology.edge_types)
        cache_note = " (cached)" if from_cache else ""
        if node_count == 0 and edge_count == 0:
            detail = f"{self.name}: empty ontology (0 node types, 0 edge types){cache_note}"
        else:
            detail = (
                f"{self.name}: {node_count} node types, {edge_count} edge types, "
                f"{len(ontology.edge_map)} directional edges{cache_note}"
            )
        return ToolResult(items=(_ontology_item(ontology),), detail=detail)


def build_ontology_tool(
    registry: ToolRegistry,
    source: OntologySource,
    *,
    source_name: str | None = None,
    served_versions: ServedVersionReader | None = None,
) -> str:
    """Register the single :class:`OntologyLookupTool` into ``registry``.

    Mirrors :func:`build_graph_tool` / :func:`build_vector_tool`: a thin builder
    the factory (task 17) calls. Unlike the endpoint-gated tools the ontology tool
    is ALWAYS registered (the file source needs no endpoint, and the graph source
    reuses the already-required graph client), so the agent can always consult the
    ontology. Returns the registered Tool name.

    Args:
        registry: The :class:`ToolRegistry` to register the tool into.
        source: The configured :class:`OntologySource` the tool resolves through.
        source_name: Optional human-readable source label for failure traces.
        served_versions: Optional reader of the served ontology version that the
            cache is keyed on (see :class:`OntologyLookupTool`).

    Returns:
        The registered Tool name (``"ontology_lookup"``).
    """
    tool = OntologyLookupTool(source, source_name=source_name, served_versions=served_versions)
    registry.register(tool)
    logger.info("ontology_tool_registered", tool_name=tool.name, source=tool._source_name)
    return tool.name
