# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ontology foreign-key traversal — the Tier-2 ``explore_graph`` capability.

Where :class:`~coa_serve.tier2.tools.TableCatalog` finds tables by *semantic*
similarity, this walks the ontology's **foreign-key structure**: from a seed table
it expands ``k`` hops outward over the FK object-properties the ingest pipeline
induced (``owl:ObjectProperty`` with ``rdfs:domain``/``rdfs:range`` pointing at
table classes) and, optionally, returns the columns (``owl:DatatypeProperty``) on
the reachable tables.

The point is join discovery: a table two FKs away that a single vector search
would never surface. This returns graph *facts* only and makes no table-selection
decision — the consumer does, which keeps the choice AI-driven.

Two entry points onto the same walk, for the two consumers:
:meth:`OntologyGraphTool.explore_graph` is the agent *tool* (seeded by a table
name the agent chose, called when the agent decides to), while
:meth:`OntologyGraphTool.expand_from` is seeded by the classes a single
retrieval already returned and fires unconditionally — the flat NL→SQL path has
no agent to call a tool.

Structure of the deployed graph (verified against the reingested Spider 2.0 graph):

  * table  → ``:Db_Table a owl:Class ; rdfs:label "table"``
  * column → ``:db_table_col a owl:DatatypeProperty ; rdfs:domain :Db_Table``
  * FK     → ``:db_table_col a owl:ObjectProperty ;
                  rdfs:domain :Db_Table ; rdfs:range :Other_Table ;
                  ns1:fkProvenance "AI_INFERRED" ;
                  rdfs:comment "Foreign key: table.col references other.col"``

Traversal is BFS in Python over an adjacency loaded with one bounded SPARQL query
(the FK set is small — a few hundred edges — and cached for the life of the
request-scoped tool), so hop count, node-type filtering, ordering and truncation
are exact and cheap; recursive SPARQL over reified domain/range property nodes
would be both awkward and unbounded.

Labels (namespace-language LLD §2.2.5). A table or column class can carry an
untagged physical-name label and, from a trusted artifact or a later induction,
tagged labels too. ``table`` / ``column`` stay the PHYSICAL identifier the SQL
writer executes against — the untagged label when there is one, chosen the same
way on every request — and are never replaced by a display label. When the
namespace-language preference picks a different label it is added alongside as
``table_label`` / ``column_label``. Grouping is by IRI, so a table with several
labels is still one node, and a language never hides a table.

Physical names. Deploy names a mapped class read from one table, and a mapped
attribute read from one column, by that table or column (``coa:physicalName``,
from the version's R2RML). The identifier is that name when the graph has it, so
a label edited in the Ontology designer does not rename the table or column the
SQL writer is told to use; the edited label is then the ``table_label`` /
``column_label`` hint. A term without the triple (a namespace deployed before it
existed, a class read from several tables) is named by its label, as before. The
edge query brings each end's name along, so naming a reached table costs no query.

Placement. The Ontology designer may move a mapped property's domain or range up to
a parent of its mapped class; the parent's table, if it has one, holds neither the
column nor the join. Deploy then records the classes the mapping reads the property
from and joins it to (``coa:mappedDomain`` / ``coa:mappedRange``), and the walk
places the edge and the column on those classes instead of the domain and range.

Every query here is bound to the namespace's named graphs, which a cheap probe
resolves once per request (:meth:`OntologyGraphTool._resolve_graph_iris`). The
scoping matters for cost, not just correctness — see
:func:`~coa_serve.query_utils.graph_scoped_body`.

Like the other tools here this is transport-agnostic: :class:`OntologyGraphTool`
is a plain async capability, so the in-process agent and any future MCP tool or
HTTP route call the same code.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import Any

import structlog
from coa_common.label_language import LabelPreference

from ...clients.base import GraphClient
from ...namespace_language import current_label_preference, lang_projection, row_label
from ...query_utils import (
    MAPPED_DOMAIN_IRI,
    PHYSICAL_NAME_IRI,
    SPARQL_PREFIXES,
    get_graph_uri_template,
    graph_scoped_body,
    named_graphs_sparql,
    namespace_graph_prefix,
    object_properties_sparql,
    placed_by_mapping,
    unmapped_property_filter,
    validate_namespace,
)
from ..nl_to_sql.sql_generator import hit_class_iri
from .table_tools import TableCatalog

logger = structlog.get_logger(__name__)

# Same IRI-ref safety gate the Tier-3 traverser uses: permits the '#' fragment
# separator (class IRIs are "https://.../ontology#Db_Table") while excluding the
# characters dangerous inside a SPARQL <...> IRI ref.
_SAFE_URI_RE = re.compile(r"^https?://[^\s<>\"{}|\\^`;]+$|^urn:[a-zA-Z0-9][a-zA-Z0-9._:-]*$")

# Gate on a seed passed as a bare table name (the fallback when no IRI is known).
# DENY-list, not an allow-list: an earlier ``^[a-z0-9][a-z0-9_ .-]{0,127}$`` allowed
# only Latin, so a table labelled 会員 or 회원 failed validation and traversal
# reported "seed table not found" — a silent no-op on any non-Latin schema. What
# actually has to be excluded is the characters that could break out of a SPARQL
# string literal, plus control characters; everything else is a legitimate label.
_UNSAFE_LABEL_CHARS = frozenset('"\\<>{}')
_MAX_LABEL_CHARS = 128


def _is_safe_label(label: str) -> bool:
    """True when a bare table label is safe to interpolate into a SPARQL literal."""
    if not label or len(label) > _MAX_LABEL_CHARS:
        return False
    return not any(c in _UNSAFE_LABEL_CHARS or ord(c) < 0x20 or ord(c) == 0x7F for c in label)


# Bounds. hops is capped so a pathological k cannot fan out the whole graph;
# limit is capped so one call cannot return a wall of nodes into a prompt.
MAX_HOPS = 4
MAX_NODE_LIMIT = 200
DEFAULT_NODE_LIMIT = 50
_QUERY_TIMEOUT_S = 25.0
_EDGE_QUERY_LIMIT = 1000
# Resolving the namespace's named graphs gates every other query, so it gets a
# tighter budget: it matches one triple per graph and must be fast or useless.
_GRAPH_RESOLVE_TIMEOUT_S = 8.0
# A namespace holds one graph per published ontology; the cap is a backstop.
_MAX_GRAPHS = 25

VALID_NODE_TYPES = ("table", "column")

# Per-column description budget inside a walk-derived schema line (see
# :func:`_schema_text`). A whole walk can be appended to one prompt, so the
# descriptions are clipped rather than allowed to set the prompt's size.
_MAX_COL_DESC = 90


def _escape_literal(s: str) -> str:
    """Escape backslashes and double quotes for a SPARQL string literal."""
    return s.replace(chr(0x5C), chr(0x5C) + chr(0x5C)).replace(chr(0x22), chr(0x5C) + chr(0x22))


class OntologyGraphTool:
    """FK-graph traversal over one namespace's ontology graph."""

    def __init__(
        self,
        graph_client: GraphClient,
        catalog: TableCatalog | None = None,
        *,
        namespace: str,
        graph_uri_template: str | None = None,
    ):
        """Bind the tool to the graph client and the request's namespace.

        Args:
            graph_client: Read-only SPARQL client over the ontology graph.
            catalog: Optional table catalog. When given, a seed table already seen
                by ``search_tables`` is resolved to its exact class IRI, which
                avoids matching a same-named table in another database.
            namespace: Namespace to scope traversal to.
            graph_uri_template: Named-graph URI template override; defaults to the
                configured one.
        """
        self._graph = graph_client
        self._catalog = catalog
        self._namespace = namespace
        self._graph_uri_template = get_graph_uri_template(graph_uri_template)
        # Request-scoped caches: the edge set is loaded at most once per request.
        self._fk_edges: list[dict[str, str]] | None = None
        self._label_iris: dict[str, list[str]] = {}
        self._graph_iris: list[str] | None = None
        self._table_names: dict[str, str] = {}  # class IRI -> coa:physicalName ("" for none)

    @property
    def available(self) -> bool:
        """True when a named-graph URI template is configured (else traversal is a no-op)."""
        return bool(self._graph_uri_template)

    # ── SPARQL loaders ──────────────────────────────────────────────────────

    async def _query(self, sparql: str, *, what: str) -> list[dict[str, str]]:
        """Run a bounded SPARQL query, logging a timeout distinctly from other failures.

        The caller's ``except Exception`` would swallow the distinction, and an
        exhausted graph is worth telling apart from a malformed query when reading
        logs — so name it here (the same ``*_timeout`` convention Tier-3 uses) and
        re-raise for the caller's error path.
        """
        try:
            return await asyncio.wait_for(self._graph.query(sparql), timeout=_QUERY_TIMEOUT_S)
        except TimeoutError:
            logger.warning(
                "graph_tool_timeout",
                query=what,
                namespace=self._namespace,
                timeout_s=_QUERY_TIMEOUT_S,
            )
            raise

    async def _resolve_graph_iris(self, prefix: str) -> list[str]:
        """Resolve the namespace's named-graph IRIs so the loaders below can bind ``?g``.

        Cheap (one triple per published graph) and it gates everything else, so it
        gets its own tighter timeout and is best-effort: any failure or miss returns
        ``[]``, which keeps the prefix-filter fallback in
        :func:`~coa_serve.query_utils.graph_scoped_body`. The result is cached
        either way, so a namespace whose graphs do not carry the ``owl:Ontology``
        anchor — or a probe that times out — costs this once per request, not once
        per query.
        """
        if self._graph_iris is not None:
            return self._graph_iris
        sparql = named_graphs_sparql(prefix, limit=_MAX_GRAPHS)
        try:
            rows = await asyncio.wait_for(self._graph.query(sparql), timeout=_GRAPH_RESOLVE_TIMEOUT_S)
        except Exception as e:
            logger.info(
                "fk_graph_resolve_failed",
                namespace=self._namespace,
                error=f"{type(e).__name__}: {str(e)[:120]}",
            )
            self._graph_iris = []
            return []
        iris = [r["g"] for r in rows if r.get("g") and _SAFE_URI_RE.match(r["g"])]
        self._graph_iris = iris
        logger.debug("fk_graphs_resolved", namespace=self._namespace, graphs=len(iris))
        return iris

    async def _load_fk_edges(self, prefix: str) -> list[dict[str, str]]:
        """Load every FK object-property (domain/range table + labels + comment)."""
        if self._fk_edges is not None:
            return self._fk_edges
        # SHARED with the Tier-2 T-Box context builder — same edges, one definition
        # (``query_utils.object_properties_sparql``). Differences from that caller are
        # arguments, not a different query: no mapped-gate (traversal may cross into a
        # class Ontop cannot resolve, since it returns facts rather than authoring
        # SQL), a higher limit, ``?comment`` for the FK provenance string, and the
        # resolved named graphs (the T-Box builder now resolves them too, so both
        # callers are on the scoped form). For both callers the query drops a
        # relationship deploy marked unmapped, so the walk never reaches a class
        # through a join that does not exist. This caller also asks for the physical
        # form: a relationship deploy placed by its mapping (its range moved up to a
        # parent, say) joins the classes the mapping joins, and each end comes with
        # the table name :func:`_build_adjacency` names it by. NOT anchored on ``domain_iris``:
        # this caller wants the whole FK graph to walk, so it is unaffected by the
        # one-sided ?domain narrowing described in ``object_properties_sparql``.
        graphs = await self._resolve_graph_iris(prefix)
        sparql = object_properties_sparql(
            prefix, limit=_EDGE_QUERY_LIMIT, with_comment=True, graph_iris=graphs, physical=True
        )
        rows = await self._query(sparql, what="fk_edges")
        # Log the count so an empty traversal is attributable: "this namespace has
        # no induced FKs" reads differently from a query that failed or timed out.
        # ``graphs`` distinguishes a third case — the un-bound fallback, which is
        # what makes this query slow enough to time out on a busy cluster.
        logger.debug("fk_edges_loaded", namespace=self._namespace, graphs=len(graphs), edge_count=len(rows))
        self._fk_edges = rows
        return rows

    async def _load_table_names(self, prefix: str, class_iris: list[str]) -> dict[str, str]:
        """The ``coa:physicalName`` of each of the given classes that has one, by IRI.

        The edge query names every end of an edge, so this is for a seed no edge
        reaches: a flat ``VALUES ?class`` lookup, as for the columns. Each class is looked
        up once per request, and no query is sent when every class is already known (or
        none is given). Best-effort: when the lookup fails, those classes keep their
        labels, as they did before deploy wrote the names.
        """
        wanted = [u for u in dict.fromkeys(class_iris) if u not in self._table_names and _SAFE_URI_RE.match(u)]
        if wanted:
            values = " ".join(f"<{u}>" for u in wanted)
            graphs = await self._resolve_graph_iris(prefix)
            body = f"""            VALUES ?class {{ {values} }}
            ?class <{PHYSICAL_NAME_IRI}> ?name ."""
            sparql = f"""{SPARQL_PREFIXES}
        SELECT ?class ?name WHERE {{
{graph_scoped_body(body, prefix, graphs)}
        }}"""
            try:
                rows = await self._query(sparql, what="table_names")
            except Exception as e:
                logger.info(
                    "fk_table_names_failed", namespace=self._namespace, error=f"{type(e).__name__}: {str(e)[:120]}"
                )
                return {}
            names: dict[str, set[str]] = {}
            for r in rows:
                if r.get("class") and str(r.get("name") or "").strip():
                    names.setdefault(r["class"], set()).add(str(r["name"]))
            for iri in wanted:
                # Deploy writes one name per class; min() keeps a stray second one deterministic.
                self._table_names[iri] = min(names.get(iri, ()), default="")
        return {u: self._table_names[u] for u in class_iris if self._table_names.get(u)}

    async def _load_columns(self, prefix: str, class_iris: list[str]) -> dict[str, list[dict[str, str]]]:
        """Load datatype-property columns for the given (already-safe) class IRIs.

        NOT shared with ``ontop/tbox_context._fetch_class_context``, which reads the
        same datatype properties: that one is a multi-branch UNION over class-driven
        and property-driven patterns and also pulls ``coa:distinctValues`` and parent
        classes, while this is a flat ``VALUES ?class`` lookup of column + label +
        range. A common builder would have to carry both shapes, so they are kept
        apart deliberately — unlike the FK query above, which really was a copy.
        A column deploy named after its table column (``coa:physicalName``) is
        named by it; any other by its label, as before. An attribute deploy placed
        by its mapping (``coa:mappedDomain``, written when its domain was moved up
        to a parent) is a column of the classes the mapping reads it from, not of
        its domain.
        """
        safe = [u for u in class_iris if _SAFE_URI_RE.match(u)]
        if not safe:
            return {}
        values = " ".join(f"<{u}>" for u in safe)
        graphs = await self._resolve_graph_iris(prefix)
        # A column deploy marked unmapped has no table column behind it: left out.
        body = f"""            VALUES ?class {{ {values} }}
            ?col a owl:DatatypeProperty .
            {placed_by_mapping("?col", "domain", MAPPED_DOMAIN_IRI, "?class")}
            {unmapped_property_filter("?col")}
            OPTIONAL {{ ?col rdfs:label ?colLabel }}
            OPTIONAL {{ ?col <{PHYSICAL_NAME_IRI}> ?colName }}
            OPTIONAL {{ ?col rdfs:range ?range }}
            OPTIONAL {{ ?col rdfs:comment ?colComment }}"""
        sparql = f"""{SPARQL_PREFIXES}
        SELECT ?class ?col ?colLabel {lang_projection("colLabel")} ?colName ?range
               ?colComment {lang_projection("colComment")} WHERE {{
{graph_scoped_body(body, prefix, graphs)}
        }}"""
        rows = await self._query(sparql, what="columns")
        # One entry per (class, column) however many labels/comments the column
        # carries; first-seen order is kept so a single-label graph reads as before.
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for r in rows:
            cls = r.get("class", "")
            if not cls:
                continue
            # ``?col`` is always bound by the query; a row without one (a
            # hand-built fixture) is keyed by its label so it stays its own column.
            col_key = r.get("col", "") or f"label:{r.get('colLabel', '')}"
            entry = grouped.setdefault(
                (cls, col_key), {"labels": [], "comments": [], "range": r.get("range", ""), "names": set()}
            )
            label = row_label(r, "colLabel")
            if label[0]:
                entry["labels"].append(label)
            if str(r.get("colName") or "").strip():
                entry["names"].add(str(r["colName"]))
            comment = row_label(r, "colComment")
            if comment[0].strip():
                entry["comments"].append((comment[0].strip(), comment[1]))
            if not entry["range"] and r.get("range"):
                entry["range"] = r["range"]
        preference = current_label_preference()
        by_class: dict[str, list[dict[str, str]]] = {}
        for (cls, col_iri), entry in grouped.items():
            column = (
                min(entry["names"], default="")
                or _IDENTIFIER_PREFERENCE.pick(entry["labels"])
                or _localname(_col_key_iri(col_iri))
            )
            item = {
                "column": column,
                "type": _localname(entry["range"]),
                # The ingest pipeline's per-column description. Used only by
                # :func:`_schema_text` when a walk has to rebuild a schema for a
                # consumer with no retrieved text; ``explore_graph`` does not
                # return it (the agent asked for structure, not prose).
                "desc": preference.pick(entry["comments"]),
            }
            display = preference.pick(entry["labels"])
            if display and display != column:
                item["column_label"] = display
            by_class.setdefault(cls, []).append(item)
        return by_class

    async def _resolve_label_to_iris(self, prefix: str, label: str) -> list[str]:
        """Resolve a bare table label to its class IRI(s) (may be >1 across DBs).

        A class matches by a label, as it always has, or by the table deploy named it
        after (``coa:physicalName``): that is the name the walk and the NL-to-SQL
        documents give a relabelled table, so an agent seeding from one finds it. A
        class matched by its name that a label did not match is added after the
        label matches, which keep their order.

        Raises:
            ValueError: If ``label`` is not a safe literal. Callers already gate with
                :func:`_is_safe_label`; re-checking here keeps the interpolation safe
                for any future caller too.
        """
        if not _is_safe_label(label):
            raise ValueError(f"unsafe table label: {label!r}")
        # Case-folded on both sides so a seed matches a differently-cased label.
        # ``str.lower()`` and SPARQL ``LCASE`` agree on ASCII and are both no-ops on
        # scripts without case (CJK), which is the common case here; the rare
        # locale-specific disagreement (Turkish dotted İ, German ß) can only cost a
        # match, never produce a wrong one.
        key = label.lower()
        if key in self._label_iris:
            return self._label_iris[key]
        graphs = await self._resolve_graph_iris(prefix)
        body = f"""            {{ ?class a owl:Class ; rdfs:label ?label . }}
            UNION
            {{ ?class a owl:Class ; <{PHYSICAL_NAME_IRI}> ?label . BIND(true AS ?byName) }}
            FILTER(LCASE(STR(?label)) = "{_escape_literal(key)}")"""
        sparql = f"""{SPARQL_PREFIXES}
        SELECT ?class ?byName WHERE {{
{graph_scoped_body(body, prefix, graphs)}
        }}"""
        rows = await self._query(sparql, what="label_to_iris")
        safe = [r for r in rows if r.get("class") and _SAFE_URI_RE.match(r["class"])]
        iris = [r["class"] for r in safe if not r.get("byName")]
        iris += [iri for iri in dict.fromkeys(r["class"] for r in safe if r.get("byName")) if iri not in iris]
        self._label_iris[key] = iris
        return iris

    def _seed_iri_from_catalog(self, table_name: str) -> str:
        """Exact class IRI of a table the catalog already retrieved, or ""."""
        if self._catalog is None:
            return ""
        hits, _missing = self._catalog.hits_for_tables([table_name])
        for hit in hits:
            iri = hit_class_iri(hit)
            if iri:
                return iri
        return ""

    # ── traversal ────────────────────────────────────────────────────────────

    async def expand_from(
        self,
        seed_iris: list[str],
        *,
        hops: int = 1,
        max_tables: int = 8,
    ) -> list[dict[str, Any]]:
        """Tables an FK walk reaches from an ALREADY-CHOSEN set of classes.

        The flat-arm analogue of :meth:`explore_graph`. There is no agent to seed
        a walk on the flat NL→SQL path, so the seeds are the classes vector retrieval
        already returned, and the result is the ranked, *schema-bearing* set of
        tables one to ``hops`` FK hops beyond them. This is the only way that path
        can reach a table similarity did not rank in its top-k: it retrieves **once**
        and has no tools — its 2-shot ``correct()`` re-generates on an execution
        error but never re-retrieves — so a join table whose name and description
        look nothing like the question is otherwise unreachable.

        Seeds are class IRIs, never labels. 77 of the pooled Spider 2.0 namespace's
        412 classes share a bare label, so seeding by name walks out of the wrong
        database and returns a plausible schema for tables that cannot be joined. A
        seed IRI absent from the graph simply reaches nothing.

        The seeds themselves are excluded: the caller already holds their retrieved
        text, which is richer than what the graph can rebuild (it carries the sampled
        allowed-values block; see :func:`_schema_text`).

        Args:
            seed_iris: Class IRIs to walk out from; unsafe ones are dropped.
            hops: FK hops to expand outward; clamped to [1, :data:`MAX_HOPS`].
            max_tables: Cap on the tables returned; clamped to
                [0, :data:`MAX_NODE_LIMIT`].

        Returns:
            One dict per reached table (``iri``, ``table``, ``hop``, ``direction``,
            ``via``, ``schema``), nearest hop first then by table name (the
            ``coa:physicalName`` deploy wrote, else the label). ``[]`` for every
            degenerate case — no graph template, no safe seed, nothing reached — so a
            walk that finds nothing leaves the caller's prompt byte-identical to
            baseline. Load failures propagate: the caller decides whether a slow
            graph should degrade or fail the query.
        """
        validate_namespace(self._namespace)
        if not self._graph_uri_template:
            return []
        seeds = [s for s in (seed_iris or []) if s and _SAFE_URI_RE.match(s)]
        hops = max(1, min(int(hops), MAX_HOPS))
        max_tables = max(0, min(int(max_tables), MAX_NODE_LIMIT))
        if not seeds or not max_tables:
            return []

        prefix = namespace_graph_prefix(self._graph_uri_template, self._namespace)
        edges = await self._load_fk_edges(prefix)
        adjacency, label_of = _build_adjacency(edges)
        reached = _bfs(seeds, adjacency, hops)

        seed_set = set(seeds)
        # Every table reached past the seeds is an end of an edge, so the edges named it.
        display_of = _display_labels(edges, label_of, current_label_preference())
        # Same order explore_graph reports, and it matters more here: the caller
        # takes a fixed prefix of this list rather than showing an agent all of it,
        # so the ranking — not the hop bound — decides which tables reach the
        # writer's prompt.
        ordered = sorted(
            ((iri, meta) for iri, meta in reached.items() if iri not in seed_set),
            key=lambda kv: (kv[1][0], label_of.get(kv[0], kv[0])),
        )[:max_tables]
        if not ordered:
            return []

        cols_by_class = await self._load_columns(prefix, [iri for iri, _ in ordered])
        out: list[dict[str, Any]] = []
        for iri, (hop, via, direction) in ordered:
            label = label_of.get(iri, _localname(iri))
            node = {
                "iri": iri,
                "table": label,
                "hop": hop,
                "direction": direction,
                "via": via,
                # Keyed to the class the walk actually reached, so a homonym in
                # another database cannot supply the schema.
                "schema": _schema_text(label, cols_by_class.get(iri, []), display_of.get(iri, "")),
            }
            if iri in display_of:
                node["table_label"] = display_of[iri]
            out.append(node)
        return out

    async def explore_graph(
        self,
        table_name: str,
        hops: int = 1,
        node_types: list[str] | None = None,
        limit: int = DEFAULT_NODE_LIMIT,
    ) -> dict[str, Any]:
        """BFS ``hops`` FK-hops out from a seed table; return tables (+optional columns).

        The seed is resolved to an exact class IRI through the catalog when the
        table was already retrieved (avoiding a cross-database label collision),
        otherwise by its label or by the table name deploy recorded
        (``coa:physicalName``).

        Args:
            table_name: Seed table name, ideally one ``search_tables`` returned.
            hops: FK hops to expand outward; clamped to [1, :data:`MAX_HOPS`].
            node_types: Which nodes to return — any of ``"table"``, ``"column"``.
                Omit for all types.
            limit: Max nodes returned; clamped to [1, :data:`MAX_NODE_LIMIT`].
                Tables consume the budget before columns.

        Returns:
            The traversal result, or ``{"error": ...}`` when traversal is
            unavailable or the seed cannot be resolved.
        """
        validate_namespace(self._namespace)
        if not self._graph_uri_template:
            return {"error": "graph traversal unavailable (GRAPH_URI_TEMPLATE unset)"}

        seed = str(table_name).strip().lower()
        # Clamp into range: the caller's value wins, the bounds only stop a
        # pathological one (0, negative, 999). MAX_HOPS / MAX_NODE_LIMIT are the
        # ceilings; the 1s are floors, NOT defaults — the defaults are in the
        # signature (hops=1, limit=DEFAULT_NODE_LIMIT) and the caller may ask for
        # up to MAX_HOPS on any call.
        hops = max(1, min(int(hops), MAX_HOPS))
        limit = max(1, min(int(limit), MAX_NODE_LIMIT))
        types = _normalize_node_types(node_types)
        prefix = namespace_graph_prefix(self._graph_uri_template, self._namespace)

        seed_iri = self._seed_iri_from_catalog(seed)
        seed_iris: list[str] = []
        if seed_iri and _SAFE_URI_RE.match(seed_iri):
            seed_iris = [seed_iri]
        elif _is_safe_label(seed):
            seed_iris = await self._resolve_label_to_iris(prefix, seed)
        if not seed_iris:
            return {"error": f"seed table not found ({seed!r})", "tables": [], "columns": []}

        edges = await self._load_fk_edges(prefix)
        adjacency, label_of = _build_adjacency(edges)
        reached = _bfs(seed_iris, adjacency, hops)  # iri -> (hop, via, direction)
        # Only a seed no edge reaches is not named by the edges.
        label_of |= await self._load_table_names(prefix, [iri for iri in reached if iri not in label_of])
        display_of = _display_labels(edges, label_of, current_label_preference())
        ordered = sorted(reached.items(), key=lambda kv: (kv[1][0], label_of.get(kv[0], kv[0])))

        # Tables: nearest hop first, then table name; the seed(s) at hop 0 are included
        # so the consumer sees where the walk started.
        table_nodes: list[dict[str, Any]] = []
        if "table" in types:
            table_nodes = [
                {"table": label_of.get(iri, _localname(iri)), "hop": hop, "direction": direction, "via": via}
                | ({"table_label": display_of[iri]} if iri in display_of else {})
                for iri, (hop, via, direction) in ordered
            ]

        column_nodes: list[dict[str, Any]] = []
        if "column" in types:
            cols_by_class = await self._load_columns(prefix, list(reached))
            for iri, _meta in ordered:
                table = label_of.get(iri, _localname(iri))
                for col in cols_by_class.get(iri, []):
                    node = {"table": table, "column": col["column"], "type": col["type"]}
                    if "column_label" in col:
                        node["column_label"] = col["column_label"]
                    column_nodes.append(node)

        # Global truncation: tables consume the budget first, then columns.
        total = len(table_nodes) + len(column_nodes)
        tables_out = table_nodes[:limit]
        columns_out = column_nodes[: max(0, limit - len(tables_out))]
        returned = len(tables_out) + len(columns_out)
        logger.debug(
            "explore_graph",
            namespace=self._namespace,
            seed=seed,
            seed_iri_resolved=bool(seed_iri),
            hops=hops,
            returned_nodes=returned,
        )
        return {
            "seed": seed,
            "seed_matched_classes": len(seed_iris),
            "hops": hops,
            "node_types": list(types),
            "tables": tables_out,
            "columns": columns_out,
            "total_nodes_found": total,
            "returned_nodes": returned,
            "truncated": total > returned,
        }


def _normalize_node_types(node_types: list[str] | None) -> tuple[str, ...]:
    """None/empty → all types; else the valid subset (unknown values dropped)."""
    if not node_types:
        return VALID_NODE_TYPES
    wanted = {str(x).strip().lower() for x in node_types}
    return tuple(t for t in VALID_NODE_TYPES if t in wanted) or VALID_NODE_TYPES


def _schema_text(label: str, cols: list[dict[str, str]], display_label: str = "") -> str:
    """One table as the ``"Table: … | Columns: …"`` line the SQL writer reads.

    Deliberately the same shape as the class text the ingest pipeline stores in the
    vector index, so a table a walk found can be handed to the writer through the
    existing context builder instead of a second code path. The ontology supplies
    the column name, its XSD type and the ingest-time description; what it cannot
    supply is the sampled allowed-values block, so a walk-derived schema is thinner
    than a retrieved one — which is why the caller never overwrites a retrieved one.

    ``label`` and each column's ``column`` are the physical identifiers the writer
    must use. A namespace-language display label that differs is appended as a
    hint (``(label: …)`` / ``[label: …]``); when there is none the line is exactly
    what it was before labels were ranked.
    """
    head = f"Table: {label}"
    if display_label and display_label != label:
        head += f" (label: {display_label})"
    if not cols:
        return head
    parts = []
    for col in cols:
        name = col.get("column") or ""
        if not name:
            continue
        text = f"{name}:{col.get('type') or 'unknown'}"
        column_label = col.get("column_label") or ""
        if column_label and column_label != name:
            text += f" [label: {column_label}]"
        desc = (col.get("desc") or "")[:_MAX_COL_DESC]
        parts.append(f"{text} ({desc})" if desc else text)
    if not parts:
        return head
    return f"{head} | Columns: {', '.join(parts)}"


def _col_key_iri(key: str) -> str:
    """The column IRI from a :meth:`OntologyGraphTool._load_columns` group key."""
    return "" if key.startswith("label:") else key


def _localname(iri: str) -> str:
    """Last path/fragment segment of an IRI (or the input if it is not an IRI)."""
    if not iri:
        return ""
    return iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


# Physical identifiers never follow the namespace language: an untagged label
# (the physical name the ingest pipeline wrote) wins, then any label in lexical
# order, so the executable name is the same on every request and in every
# namespace. Display labels use the request's namespace-language preference.
_IDENTIFIER_PREFERENCE = LabelPreference.untagged_first()


def _build_adjacency(edges: list[dict[str, str]]) -> tuple[dict[str, list[dict[str, str]]], dict[str, str]]:
    """Build a bidirectional FK adjacency + an IRI→identifier map from FK edge rows.

    Each edge (domain --FK--> range) yields an *outgoing* neighbor from the domain
    and an *incoming* neighbor to the range, so BFS follows joins in both
    directions (a query may join parent→child or child→parent).

    A table or FK with several labels arrives as one row per label combination.
    Rows are grouped by IRI: each table gets one physical identifier, the table
    name deploy recorded (``?domainName`` / ``?rangeName``) when the row has one,
    else its label (see :data:`_IDENTIFIER_PREFERENCE`), and each
    ``(op, domain, range)`` edge appears once, in first-seen order.
    """
    labels_of: dict[str, list[tuple[str, str]]] = {}
    names: dict[str, set[str]] = {}
    edge_rows: dict[tuple[str, str, str], dict[str, list[tuple[str, str]]]] = {}
    for e in edges:
        dom, rng = e.get("domain", ""), e.get("range", "")
        if not dom or not rng:
            continue
        for iri, end in ((dom, "domain"), (rng, "range")):
            candidates = labels_of.setdefault(iri, [])
            label = row_label(e, f"{end}Label")
            if label[0]:
                candidates.append(label)
            if str(e.get(f"{end}Name") or "").strip():
                names.setdefault(iri, set()).add(str(e[f"{end}Name"]))
        # A self-referential FK (employee.manager_id → employee) adds two adjacency
        # entries that BFS can never use: the node is already reached at hop 0 by
        # the time its own neighbours are scanned. Keep its label, drop the edges.
        if dom == rng:
            continue
        edge = edge_rows.setdefault((e.get("op", ""), dom, rng), {"comments": [], "labels": []})
        if e.get("comment"):
            edge["comments"].append((str(e["comment"]), ""))
        op_label = row_label(e, "opLabel")
        if op_label[0]:
            edge["labels"].append(op_label)

    # Deploy writes one name per class; min() keeps a stray second one deterministic.
    label_of = {
        iri: min(names.get(iri, ()), default="") or _IDENTIFIER_PREFERENCE.pick(c) or _localname(iri)
        for iri, c in labels_of.items()
    }
    adjacency: dict[str, list[dict[str, str]]] = {}
    for (op, dom, rng), edge in edge_rows.items():
        via = (
            _IDENTIFIER_PREFERENCE.pick(edge["comments"])
            or _IDENTIFIER_PREFERENCE.pick(edge["labels"])
            or _localname(op)
        )
        adjacency.setdefault(dom, []).append({"to": rng, "via": via, "direction": "outgoing"})
        adjacency.setdefault(rng, []).append({"to": dom, "via": via, "direction": "incoming"})
    return adjacency, label_of


def _display_labels(
    edges: list[dict[str, str]], label_of: dict[str, str], preference: LabelPreference
) -> dict[str, str]:
    """IRI → namespace-language display label, only where it differs from the identifier."""
    rows = [
        (e[iri_var], *row_label(e, label_var))
        for e in edges
        for iri_var, label_var in (("domain", "domainLabel"), ("range", "rangeLabel"))
        if e.get(iri_var)
    ]
    return {
        iri: display
        for iri, display in preference.pick_by_resource(rows).items()
        if display and display != label_of.get(iri, _localname(iri))
    }


def _bfs(
    seeds: list[str], adjacency: dict[str, list[dict[str, str]]], max_hops: int
) -> dict[str, tuple[int, str, str]]:
    """Breadth-first expansion. Returns iri → (hop, via, direction); seeds at hop 0."""
    reached: dict[str, tuple[int, str, str]] = {}
    queue: deque[tuple[str, int]] = deque()
    for s in seeds:
        if s not in reached:
            reached[s] = (0, "", "seed")
            queue.append((s, 0))
    while queue:
        iri, hop = queue.popleft()
        if hop >= max_hops:
            continue
        for edge in adjacency.get(iri, []):
            neighbor = edge["to"]
            if neighbor not in reached:
                reached[neighbor] = (hop + 1, edge["via"], edge["direction"])
                queue.append((neighbor, hop + 1))
    return reached
