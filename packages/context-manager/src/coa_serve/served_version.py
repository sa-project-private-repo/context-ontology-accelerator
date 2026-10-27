# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The ontology version Neptune serves for a namespace.

A deploy stamps ``owl:versionInfo "vN"`` on the namespace's induced ontology graph
(ontology-engine ``versions/deploy.py``), so that literal is what serve compares
against to tell whether another component is in step with Neptune: the domain ABox
(``tier3/domain_abox.py``), the VKG (``tier2/ontop/vkg_translator.py``), the legacy
isMapped bridges and the cached ontology of the deep-reasoning lookup tool. A
namespace accepted before versions existed, or a document-induced graph, carries no
``vN`` and reads as having none.
"""

from __future__ import annotations

import asyncio
import re

from .clients.base import GraphClient
from .clients.ttl_cache import TtlLruCache
from .query_utils import get_graph_uri_template, namespace_graph_prefix, validate_namespace

_OWL = "http://www.w3.org/2002/07/owl#"
# What an ontology's graph says it holds: "induced" (tables, documents, versions), "foundational" or "user_created".
ONTOLOGY_TYPE_IRI = "https://ontology-workbench.local/vocab#ontology_type"

# A published version as deploy writes it.
VERSION_RE = re.compile(r"^v[0-9]+$")


def served_version_pattern(graph_uri_prefix: str) -> str:
    """Return the SPARQL group that binds ``?v`` to each published version of the namespace.

    Anchored on ``owl:Ontology``, one subject per graph, so the prefix filter stays
    cheap even without the namespace's graphs resolved.
    """
    return (
        f'GRAPH ?g {{ ?ont a <{_OWL}Ontology> ; <{_OWL}versionInfo> ?v ; <{ONTOLOGY_TYPE_IRI}> "induced" }} '
        f'FILTER(STRSTARTS(STR(?g), "{graph_uri_prefix}") && REGEX(?v, "^v[0-9]+$"))'
    )


class ServedVersionReader:
    """Reads the published versions Neptune serves per namespace, reusing a read for a while."""

    def __init__(
        self,
        graph: GraphClient,
        *,
        graph_uri_template: str | None = None,
        timeout_s: float = 2.0,
        cache_ttl_s: float = 60.0,
    ) -> None:
        """Bind the graph client and the time budgets.

        Args:
            graph: Read-only SPARQL client for Neptune.
            graph_uri_template: Named-graph URI template; defaults to ``GRAPH_URI_TEMPLATE``.
            timeout_s: Budget of one read; over it the read raises ``TimeoutError``.
            cache_ttl_s: How long a successful read of a namespace is reused, so a deploy
                is seen within this window.
        """
        self._graph = graph
        self._template = graph_uri_template
        self._timeout_s = timeout_s
        self._versions: TtlLruCache[str, frozenset[str]] = TtlLruCache(ttl_s=cache_ttl_s, max_size=512)

    async def versions(self, namespace: str, *, fresh: bool = False) -> frozenset[str]:
        """Return the published versions (``vN``) Neptune serves for ``namespace``.

        Normally one; empty for a namespace that has none, or when no graph URI
        template is configured. Raises when Neptune cannot be read, and only a
        successful read is cached, so a transient failure is not remembered.
        ``fresh`` reads Neptune even when a read is kept, and keeps the new one: for
        a caller about to act on a kept read that may predate a deploy.
        """
        cached = None if fresh else self._versions.get(namespace)
        if cached is not None:
            return cached
        validate_namespace(namespace)
        template = get_graph_uri_template(self._template)
        if not template:
            return frozenset()
        prefix = namespace_graph_prefix(template, namespace)
        rows = await asyncio.wait_for(
            self._graph.query(f"SELECT DISTINCT ?v WHERE {{ {served_version_pattern(prefix)} }}"),
            timeout=self._timeout_s,
        )
        versions = frozenset(r["v"] for r in rows if VERSION_RE.match(r.get("v", "")))
        self._versions.set(namespace, versions)
        return versions
