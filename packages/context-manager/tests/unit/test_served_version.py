# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the reader of the ontology version Neptune serves."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from coa_common.constants import GRAPH_BASE_URI
from coa_serve.served_version import ServedVersionReader, served_version_pattern
from rdflib import OWL, RDF, Literal, URIRef

from .deployed_graph import GRAPH, NS, ONTOLOGY_ID, TEMPLATE, designer_v2_dataset, neptune_over

pytestmark = pytest.mark.unit


def _counting(client):
    """Wrap ``client.query`` so a test can tell how many reads reached the graph."""
    calls: list[str] = []
    inner = client.query

    async def query(sparql, **kwargs):
        calls.append(sparql)
        return await inner(sparql, **kwargs)

    client.query = query
    return client, calls


async def test_reads_the_version_deploy_stamped_on_the_induced_graph():
    reader = ServedVersionReader(neptune_over(designer_v2_dataset()), graph_uri_template=TEMPLATE)
    assert await reader.versions(NS) == frozenset({"v2"})


@pytest.mark.parametrize("version", [None, "2026-10-15T00:00:00Z", "v2.1"])
async def test_a_namespace_without_a_published_version_has_none(version):
    reader = ServedVersionReader(neptune_over(designer_v2_dataset(version=version)), graph_uri_template=TEMPLATE)
    assert await reader.versions(NS) == frozenset()


async def test_only_the_namespace_s_own_graphs_are_read():
    ds = designer_v2_dataset()
    other = ds.graph(URIRef("https://ontology-workbench.local/another-ns/induced"))
    ontology = URIRef(f"{GRAPH_BASE_URI}/namespace/another-ns/ontology/induced")
    other.add((ontology, RDF.type, OWL.Ontology))
    other.add((ontology, OWL.versionInfo, Literal("v9")))
    other.add((ontology, URIRef("https://ontology-workbench.local/vocab#ontology_type"), Literal("induced")))

    reader = ServedVersionReader(neptune_over(ds), graph_uri_template=TEMPLATE)

    assert await reader.versions(NS) == frozenset({"v2"})
    assert await reader.versions("another-ns") == frozenset({"v9"})


async def test_a_read_is_reused_within_the_ttl():
    client, calls = _counting(neptune_over(designer_v2_dataset()))
    reader = ServedVersionReader(client, graph_uri_template=TEMPLATE)

    await reader.versions(NS)
    await reader.versions(NS)

    assert len(calls) == 1


async def test_a_fresh_read_skips_the_kept_one_and_replaces_it():
    ds = designer_v2_dataset()
    client, calls = _counting(neptune_over(ds))
    reader = ServedVersionReader(client, graph_uri_template=TEMPLATE)
    assert await reader.versions(NS) == frozenset({"v2"})

    # A deploy of v3 stamps the new version on the induced graph.
    ds.graph(URIRef(GRAPH)).set((URIRef(ONTOLOGY_ID), OWL.versionInfo, Literal("v3")))

    assert await reader.versions(NS) == frozenset({"v2"})
    assert await reader.versions(NS, fresh=True) == frozenset({"v3"})
    assert await reader.versions(NS) == frozenset({"v3"})
    assert len(calls) == 2


async def test_a_failed_read_raises_and_is_not_remembered():
    client = AsyncMock()
    client.query.side_effect = [RuntimeError("neptune down"), [{"v": "v3"}]]
    reader = ServedVersionReader(client, graph_uri_template=TEMPLATE)

    with pytest.raises(RuntimeError):
        await reader.versions(NS)
    assert await reader.versions(NS) == frozenset({"v3"})


async def test_a_slow_read_raises_a_timeout():
    client = AsyncMock()

    async def slow(*_a, **_k):
        await asyncio.sleep(1)

    client.query.side_effect = slow
    reader = ServedVersionReader(client, graph_uri_template=TEMPLATE, timeout_s=0.01)

    with pytest.raises(TimeoutError):
        await reader.versions(NS)


async def test_without_a_graph_uri_template_nothing_is_read(monkeypatch):
    monkeypatch.delenv("GRAPH_URI_TEMPLATE", raising=False)
    client = AsyncMock()
    reader = ServedVersionReader(client)

    assert await reader.versions(NS) == frozenset()
    client.query.assert_not_called()


def test_the_pattern_is_the_one_the_domain_abox_reads():
    # The domain ABox check and this reader must agree on what "the version Neptune
    # serves" is; both read this pattern.
    assert served_version_pattern("https://ontology-workbench.local/ns/") == (
        "GRAPH ?g { ?ont a <http://www.w3.org/2002/07/owl#Ontology> ; "
        "<http://www.w3.org/2002/07/owl#versionInfo> ?v ; "
        '<https://ontology-workbench.local/vocab#ontology_type> "induced" } '
        'FILTER(STRSTARTS(STR(?g), "https://ontology-workbench.local/ns/") && REGEX(?v, "^v[0-9]+$"))'
    )
