# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Reads of the active domain ABox for the API (spec §7)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from coa_ontology.stores.neptune_db_graph import _esc, _iri, _sparql_query

from . import model as m
from . import store
from .lexical import chunk_texts

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
SKOS_ALT = "http://www.w3.org/2004/02/skos/core#altLabel"
_HIDDEN_TYPES = (m.EVIDENCE, m.DOMAIN_ABOX, m.FREE_ATTRIBUTE)
_INTERNAL_PREDICATES = {RDF_TYPE, RDFS_LABEL, SKOS_ALT, m.LEXICAL_ENTITY_ID, m.NORM_KEY}
_TEXT_LIMIT = 1000
# What ``instance`` returns of a busy instance; ``truncated`` says when a cap was hit.
MAX_INCOMING = 200
MAX_CHUNKS = 50


def _bindings(query: str) -> list[dict[str, dict[str, str]]]:
    bindings: list[dict[str, dict[str, str]]] = _sparql_query(query).get("results", {}).get("bindings", [])
    return bindings


def _values(var: str, iris: list[str]) -> str:
    return f"VALUES ?{var} {{ {' '.join(_iri(i) for i in iris)} }}"


def _literals(var: str, texts: list[str]) -> str:
    return f"VALUES ?{var} {{ {' '.join(f'"{_esc(t)}"' for t in texts)} }}"


def list_instances(
    namespace: str,
    *,
    class_iri: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of instances ordered by label, and whether more follow.

    ``q`` is a case-insensitive substring match on the label. Raises ``ValueError`` when
    ``class_iri`` is not a usable IRI.
    """
    graph = _iri(store.graph_uri(namespace))
    filters = [f"FILTER(?c NOT IN ({', '.join(_iri(t) for t in _HIDDEN_TYPES)}))"]
    if class_iri:
        filters.append(f"FILTER(?c = {_iri(class_iri)})")
    if q:
        filters.append(f'FILTER(CONTAINS(LCASE(STR(?l)), "{_esc(q.lower())}"))')
    rows = _bindings(
        f"SELECT ?i ?c ?l (COUNT(DISTINCT ?e) AS ?ev) WHERE {{ GRAPH {graph} {{ ?i a ?c ; {_iri(RDFS_LABEL)} ?l . "
        f"{' '.join(filters)} OPTIONAL {{ ?e {_iri(m.ABOUT)} ?i }} }} }} "
        f"GROUP BY ?i ?c ?l ORDER BY LCASE(STR(?l)) ?i LIMIT {limit + 1} OFFSET {offset}"
    )
    more = len(rows) > limit
    rows = rows[:limit]
    iris = [r["i"]["value"] for r in rows]
    alts: dict[str, list[str]] = {}
    if iris:
        for r in _bindings(f"SELECT ?i ?a WHERE {{ GRAPH {graph} {{ {_values('i', iris)} ?i {_iri(SKOS_ALT)} ?a }} }}"):
            alts.setdefault(r["i"]["value"], []).append(r["a"]["value"])
    out = [
        {
            "iri": r["i"]["value"],
            "classIri": r["c"]["value"],
            "label": r["l"]["value"],
            "altLabels": sorted(alts.get(r["i"]["value"], [])),
            "evidenceCount": int(r["ev"]["value"]),
        }
        for r in rows
    ]
    return out, more


def instance(namespace: str, iri: str, labels: Callable[[str], str]) -> dict[str, Any] | None:
    """One instance with its attributes, links in both directions and evidence; None when ``iri`` is no instance.

    The response is bounded: at most ``MAX_INCOMING`` incoming links (first by source IRI) and
    ``MAX_CHUNKS`` evidence chunks (first by chunk id), whose text alone is read from the lexical
    graph. ``truncated`` says which of the two was cut. ``labels`` names a class or property IRI.
    Raises ``ValueError`` when ``iri`` is not a usable IRI.
    """
    graph = _iri(store.graph_uri(namespace))
    subject = _iri(iri)
    hidden = ", ".join(_iri(t) for t in _HIDDEN_TYPES)
    own = _bindings(f"SELECT ?p ?o WHERE {{ GRAPH {graph} {{ {subject} ?p ?o }} }}")
    if not own:
        return None
    classes = sorted(r["o"]["value"] for r in own if r["p"]["value"] == RDF_TYPE)
    if any(c in _HIDDEN_TYPES for c in classes):
        return None
    label = next((r["o"]["value"] for r in own if r["p"]["value"] == RDFS_LABEL), iri)
    alt_labels = sorted(r["o"]["value"] for r in own if r["p"]["value"] == SKOS_ALT)
    links = [
        (r["p"]["value"], r["o"]["value"])
        for r in own
        if r["p"]["value"] not in _INTERNAL_PREDICATES and r["o"]["type"] == "uri"
    ]
    values = [
        (r["p"]["value"], r["o"]["value"])
        for r in own
        if r["p"]["value"] not in _INTERNAL_PREDICATES and r["o"]["type"] != "uri"
    ]
    # Incoming links are found through their evidence nodes: every link has at least one, and the
    # object-first lookup has an index in Neptune's default set, where a bare ``?s ?p <iri>`` has none.
    incoming_rows = _bindings(
        f"SELECT DISTINCT ?s ?p WHERE {{ GRAPH {graph} {{ ?e {_iri(m.OBJECT)} {subject} ; "
        f"{_iri(m.ABOUT)} ?s ; {_iri(m.PREDICATE)} ?p . ?s a ?sc . "
        f"FILTER(?sc NOT IN ({hidden})) }} }} ORDER BY ?s ?p LIMIT {MAX_INCOMING + 1}"
    )
    incoming = [(r["s"]["value"], r["p"]["value"]) for r in incoming_rows[:MAX_INCOMING]]
    others = sorted({o for _, o in links} | {s for s, _ in incoming})
    other_info: dict[str, dict[str, str]] = {}
    if others:
        for r in _bindings(
            f"SELECT ?x ?l ?c WHERE {{ GRAPH {graph} {{ {_values('x', others)} ?x {_iri(RDFS_LABEL)} ?l ; a ?c }} }}"
        ):
            other_info[r["x"]["value"]] = {"label": r["l"]["value"], "classIri": r["c"]["value"]}
    free: dict[str, str] = {}
    if values:  # only the predicates this instance uses can be free attributes of it
        for r in _bindings(
            f"SELECT ?p ?l WHERE {{ GRAPH {graph} {{ {_values('p', sorted({p for p, _ in values}))} "
            f"?p a {_iri(m.FREE_ATTRIBUTE)} ; {_iri(RDFS_LABEL)} ?l }} }}"
        ):
            free[r["p"]["value"]] = r["l"]["value"]
    about = f"{{ ?e {_iri(m.ABOUT)} {subject} }} UNION {{ ?e {_iri(m.OBJECT)} {subject} }}"
    chunk_rows = _bindings(
        f"SELECT DISTINCT ?chunk WHERE {{ GRAPH {graph} {{ {about} ?e {_iri(m.CHUNK_ID)} ?chunk }} }} "
        f"ORDER BY ?chunk LIMIT {MAX_CHUNKS + 1}"
    )
    chunk_ids = [r["chunk"]["value"] for r in chunk_rows[:MAX_CHUNKS]]
    by_fact: dict[tuple[str, str], list[str]] = {}
    chunks: dict[str, dict[str, str]] = {}
    if chunk_ids:
        for r in _bindings(
            f"SELECT DISTINCT ?p ?o ?chunk ?ds ?key WHERE {{ GRAPH {graph} {{ {_literals('chunk', chunk_ids)} "
            f"{about} ?e {_iri(m.PREDICATE)} ?p ; {_iri(m.OBJECT)} ?o ; {_iri(m.CHUNK_ID)} ?chunk . "
            f"OPTIONAL {{ ?e {_iri(m.DOC_SOURCE_ID)} ?ds }} OPTIONAL {{ ?e {_iri(m.SOURCE_KEY)} ?key }} }} }}"
        ):
            by_fact.setdefault((r["p"]["value"], r["o"]["value"]), []).append(r["chunk"]["value"])
            chunks[r["chunk"]["value"]] = {
                "docSourceId": r.get("ds", {}).get("value", ""),
                "sourceKey": r.get("key", {}).get("value", ""),
            }
    texts = chunk_texts(namespace, sorted(chunks))
    return {
        "iri": iri,
        "classes": [{"iri": c, "label": labels(c)} for c in classes],
        "label": label,
        "altLabels": alt_labels,
        "attributes": [
            {
                "property": p,
                "label": free.get(p) or labels(p),
                "value": v,
                "free": p in free,
                "evidence": sorted(by_fact.get((p, v), [])),
            }
            for p, v in sorted(values)
        ],
        "outgoing": [
            {
                "property": p,
                "label": labels(p),
                "target": {"iri": o, **other_info.get(o, {"label": o, "classIri": ""})},
                "evidence": sorted(by_fact.get((p, o), [])),
            }
            for p, o in sorted(links)
        ],
        "incoming": [
            {"property": p, "label": labels(p), "source": {"iri": s, **other_info.get(s, {"label": s, "classIri": ""})}}
            for s, p in sorted(incoming)
        ],
        "evidence": [
            {"chunkId": c, **meta, "text": texts.get(c, "")[:_TEXT_LIMIT]} for c, meta in sorted(chunks.items())
        ],
        "truncated": {"incoming": len(incoming_rows) > MAX_INCOMING, "evidence": len(chunk_rows) > MAX_CHUNKS},
    }
