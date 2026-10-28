# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Read views of a designer draft: the graph with ABox badges, one term, and term search.

The graph is the whole class graph up to ``MAX_FULL_CLASSES`` classes; above that it is a focus
view: one class and its neighbours up to ``FOCUS_HOPS`` hops, at most ``MAX_FOCUS_NODES`` classes.
Badges come from the ``ABOX`` state row (the domain ABox), with no Neptune read.

Every view only reads the draft: a loaded revision's graphs are shared through a cache.
"""

from __future__ import annotations

import functools
import logging
import os
from collections import deque
from dataclasses import dataclass
from typing import Any

from rdflib import OWL, RDF, RDFS, Graph, URIRef
from rdflib.namespace import SKOS
from rdflib.term import Node

from coa_ontology.abox import store as abox_store
from coa_ontology.abox.model import iri_tail, norm
from coa_ontology.catalog.ingest import schema_subjects
from coa_ontology.versions import registry, snapshot, storage

from .iris import IriScheme
from .ops import (
    CLASS,
    DATATYPE_PROPERTY,
    OBJECT_PROPERTY,
    RR,
    Draft,
    kind_of,
    label_of,
    maps_of_class,
    poms_mentioning,
)

_log = logging.getLogger(__name__)
MAX_FULL_CLASSES = int(os.getenv("STUDIO_GRAPH_MAX_CLASSES", "150"))
MAX_FOCUS_NODES = int(os.getenv("STUDIO_GRAPH_MAX_FOCUS_NODES", "150"))
FOCUS_HOPS = 2
MAX_ATTRIBUTES = 100
MAX_SEARCH = 50
_MATCHES = {SKOS.exactMatch: "exact", SKOS.closeMatch: "close"}


@dataclass(frozen=True)
class AboxBadges:
    """What the graph needs of the active version's domain ABox."""

    status: str
    current: bool
    conforms_to_version: int | None
    active_version: int
    counts: dict[str, int]
    active_classes: frozenset[str]

    def view(self) -> dict[str, Any]:
        """The ``abox`` block of the graph response."""
        reason = None
        if not self.current:
            reason = (
                "The domain data has not been built for the active version yet"
                if self.conforms_to_version is None
                else f"The domain data was built for v{self.conforms_to_version}; v{self.active_version} is active"
            )
        return {
            "status": self.status,
            "current": self.current,
            "conformsToVersion": self.conforms_to_version,
            "activeVersion": self.active_version,
            "reason": reason,
        }

    def badge(self, iri: str) -> dict[str, Any]:
        """``aboxCount`` for a class the active version has, else ``afterDeploy``."""
        if iri in self.active_classes:
            return {"aboxCount": self.counts.get(iri, 0), "afterDeploy": False}
        return {"aboxCount": None, "afterDeploy": True}


@functools.lru_cache(maxsize=16)
def _version_classes(namespace: str, version: int) -> frozenset[str]:
    g = snapshot.parse_turtle(storage.require_version_file(namespace, version, "ontology.ttl"))
    return frozenset(str(c) for c in schema_subjects(g)[0])


def abox_badges(namespace: str) -> AboxBadges | None:
    """The badges of the namespace's domain ABox, or None when it has none (never built, or no active version)."""
    state = abox_store.get_state(namespace)
    if not state or state.get("status", abox_store.EMPTY) == abox_store.EMPTY:
        return None
    active = (registry.get_active(namespace) or {}).get("version")
    if active is None:
        return None
    conforms = state.get("conforms_to_version")
    try:
        classes = _version_classes(namespace, int(active))
    except (storage.SnapshotFileMissingError, snapshot.SnapshotValidationError):
        # The graph still renders, without badges; the log tells an operator why they are missing.
        _log.warning("graph badges: active v%s of %s unreadable", active, namespace, exc_info=True)
        return None
    return AboxBadges(
        status=str(state.get("status")),
        current=conforms is not None and int(conforms) == int(active),
        conforms_to_version=int(conforms) if conforms is not None else None,
        active_version=int(active),
        counts={str(c["class_iri"]): int(c["count"]) for c in state.get("class_counts") or []},
        active_classes=classes,
    )


def _class_edges(g: Graph, classes: set[URIRef]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for c in sorted(classes, key=str):
        for p in sorted(g.objects(c, RDFS.subClassOf), key=str):
            if isinstance(p, URIRef) and p in classes and p != c:
                edges.append({"id": f"sub|{c}|{p}", "kind": "subClassOf", "source": str(c), "target": str(p)})
    for prop in sorted(set(g.subjects(RDF.type, OWL.ObjectProperty)), key=str):
        if not isinstance(prop, URIRef) or kind_of(g, prop) != OBJECT_PROPERTY:
            continue
        domains = sorted((d for d in g.objects(prop, RDFS.domain) if d in classes), key=str)
        ranges = sorted((r for r in g.objects(prop, RDFS.range) if r in classes), key=str)
        for d in domains:
            for r in ranges:
                edges.append(
                    {
                        "id": f"op|{prop}|{d}|{r}",
                        "kind": "objectProperty",
                        "source": str(d),
                        "target": str(r),
                        "iri": str(prop),
                        "label": label_of(g, prop),
                    }
                )
    return edges


def _attributes(g: Graph) -> dict[Node, list[dict[str, Any]]]:
    out: dict[Node, list[dict[str, Any]]] = {}
    for prop in sorted(set(g.subjects(RDF.type, OWL.DatatypeProperty)), key=str):
        if not isinstance(prop, URIRef):
            continue
        rng = next((str(r) for r in sorted(g.objects(prop, RDFS.range), key=str) if isinstance(r, URIRef)), None)
        for d in g.objects(prop, RDFS.domain):
            out.setdefault(d, []).append({"iri": str(prop), "label": label_of(g, prop), "range": rng})
    return out


def _grounded(g: Graph, c: URIRef, scheme: IriScheme) -> bool:
    return any(isinstance(o, URIRef) and not scheme.in_space(str(o)) for p in _MATCHES for o in g.objects(c, p))


def _neighbours(edges: list[dict[str, Any]]) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {}
    for e in edges:
        adj.setdefault(e["source"], set()).add(e["target"])
        adj.setdefault(e["target"], set()).add(e["source"])
    return adj


def _focus_set(focus: str, adj: dict[str, set[str]]) -> tuple[set[str], bool]:
    """Breadth-first from ``focus`` up to ``FOCUS_HOPS`` hops, neighbours in IRI order, capped."""
    picked = {focus}
    queue: deque[tuple[str, int]] = deque([(focus, 0)])
    while queue:
        node, depth = queue.popleft()
        if depth == FOCUS_HOPS:
            continue
        for nxt in sorted(adj.get(node, ())):
            if nxt in picked:
                continue
            if len(picked) >= MAX_FOCUS_NODES:
                return picked, True
            picked.add(nxt)
            queue.append((nxt, depth + 1))
    return picked, False


def graph_view(
    d: Draft, scheme: IriScheme, *, revision: int, focus: str | None, badges: AboxBadges | None
) -> dict[str, Any]:
    """The class graph of the draft (or a focus view of it) with attributes and ABox badges."""
    g = d.tbox
    classes = schema_subjects(g)[0]
    all_edges = _class_edges(g, classes)
    mode, truncated = "full", False
    visible = {str(c) for c in classes}
    if focus is not None or len(classes) > MAX_FULL_CLASSES:
        adj = _neighbours(all_edges)
        if focus is None and classes:
            # The most connected class, ties broken by IRI, so the first view is never empty.
            focus = min(visible, key=lambda c: (-len(adj.get(c, ())), c))
        if focus is not None and URIRef(focus) not in classes:
            raise KeyError(focus)
        mode = "focus"
        visible, truncated = _focus_set(focus, adj) if focus is not None else (set(), False)
    attributes = _attributes(g)
    nodes = []
    for iri in sorted(visible):
        c = URIRef(iri)
        attrs = attributes.get(c, [])
        node: dict[str, Any] = {
            "iri": iri,
            "label": label_of(g, c),
            "grounded": _grounded(g, c, scheme),
            "editable": scheme.in_space(iri),
            "attributes": attrs[:MAX_ATTRIBUTES],
            "attributeCount": len(attrs),
        }
        node.update(badges.badge(iri) if badges else {"aboxCount": None, "afterDeploy": False})
        nodes.append(node)
    return {
        "revision": revision,
        "mode": mode,
        "focus": focus if mode == "focus" else None,
        "totalClasses": len(classes),
        "truncated": truncated,
        "nodes": nodes,
        "edges": [e for e in all_edges if e["source"] in visible and e["target"] in visible],
        "abox": badges.view() if badges else None,
    }


def _ref(g: Graph, term: Node) -> dict[str, Any]:
    """A term named by another term. ``declared`` is whether ``term_view`` can open it.

    A parent from a reference ontology (FIBO) or a datatype property's XSD range is named but not
    declared by the draft, so opening it is a ``KeyError`` (404) and the client must not link it.
    """
    return {"iri": str(term), "label": label_of(g, term), "declared": kind_of(g, term) is not None}


def term_view(d: Draft, scheme: IriScheme, iri: str, badges: AboxBadges | None) -> dict[str, Any]:
    """Everything the Inspector shows of one term; KeyError when the draft has no such class or property."""
    g = d.tbox
    t = URIRef(iri)
    kind = kind_of(g, t)
    if kind is None:
        raise KeyError(iri)
    view: dict[str, Any] = {
        "iri": iri,
        "kind": kind,
        "label": label_of(g, t),
        "labels": sorted(
            ({"value": str(o), "lang": getattr(o, "language", None)} for o in g.objects(t, RDFS.label)),
            key=lambda x: (x["lang"] or "", x["value"]),
        ),
        "altLabels": sorted(str(o) for o in g.objects(t, SKOS.altLabel)),
        "comment": next((str(o) for o in sorted(g.objects(t, RDFS.comment), key=str)), ""),
        "editable": scheme.in_space(iri),
    }
    if kind == CLASS:
        named = sorted((p for p in g.objects(t, RDFS.subClassOf) if isinstance(p, URIRef)), key=str)
        view["parents"] = [_ref(g, p) for p in named]
        view["children"] = [
            _ref(g, s) for s in sorted(g.subjects(RDFS.subClassOf, t), key=str) if isinstance(s, URIRef)
        ]
        view["matches"] = [
            {"target": str(o), "kind": kind_name}
            for p, kind_name in _MATCHES.items()
            for o in sorted(g.objects(t, p), key=str)
        ]
        props = []
        for prop in sorted(set(g.subjects(RDFS.domain, t)) | set(g.subjects(RDFS.range, t)), key=str):
            pkind = kind_of(g, prop)
            if pkind not in (OBJECT_PROPERTY, DATATYPE_PROPERTY):
                continue
            outgoing = (prop, RDFS.domain, t) in g
            other = next(iter(sorted(g.objects(prop, RDFS.range if outgoing else RDFS.domain), key=str)), None)
            props.append(
                {
                    "iri": str(prop),
                    "label": label_of(g, prop),
                    "kind": pkind,
                    "direction": "outgoing" if outgoing else "incoming",
                    "other": str(other) if other is not None else None,
                    "otherLabel": label_of(g, other) if other is not None else None,
                    "otherDeclared": isinstance(other, URIRef) and kind_of(g, other) is not None,
                }
            )
        view["properties"] = props
        view["mappings"] = [
            {
                "triplesMap": str(m),
                "table": next(
                    (str(x) for lt in d.r2rml.objects(m, RR.logicalTable) for x in d.r2rml.objects(lt, RR.tableName)),
                    None,
                ),
            }
            for m in sorted(maps_of_class(d.r2rml, t), key=str)
        ]
        view["abox"] = badges.badge(iri) if badges else None
        # Imported here: checks imports this module, so a module-level import would be circular.
        from .checks import answered_classes, mapped_classes

        view["structuredAnswered"] = t in answered_classes(g, mapped_classes(d.r2rml))
    else:
        view["domain"] = [_ref(g, x) for x in sorted(g.objects(t, RDFS.domain), key=str) if isinstance(x, URIRef)]
        view["range"] = [_ref(g, x) for x in sorted(g.objects(t, RDFS.range), key=str) if isinstance(x, URIRef)]
        mappings = []
        for pom in sorted(poms_mentioning(d.r2rml, t), key=str):
            tmap = next(iter(sorted(d.r2rml.subjects(RR.predicateObjectMap, pom), key=str)), None)
            column = parent = None
            for om in d.r2rml.objects(pom, RR.objectMap):
                column = column or next((str(c) for c in d.r2rml.objects(om, RR.column)), None)
                parent = parent or next((str(p) for p in d.r2rml.objects(om, RR.parentTriplesMap)), None)
            mappings.append({"triplesMap": str(tmap) if tmap else None, "column": column, "parentTriplesMap": parent})
        view["mappings"] = mappings
    return view


def search_terms(d: Draft, q: str, kind: str | None, limit: int) -> list[dict[str, Any]]:
    """Terms whose label or IRI tail contains ``q`` (case- and width-insensitive), best matches first.

    Only terms ``term_view`` can open are listed, each under the one kind ``kind_of`` gives it.
    """
    g = d.tbox
    needle = norm(q)
    if not needle:
        return []
    classes, obj_props, dt_props = schema_subjects(g)
    pools = {CLASS: classes, OBJECT_PROPERTY: obj_props, DATATYPE_PROPERTY: dt_props}
    hits = []
    for term_kind, terms in pools.items():
        if kind is not None and kind != term_kind:
            continue
        for t in terms:
            if kind_of(g, t) != term_kind:
                continue
            label = label_of(g, t)
            key, tail = norm(label), norm(iri_tail(str(t)))
            if needle not in key and needle not in tail:
                continue
            rank = 0 if key == needle else 1 if key.startswith(needle) else 2
            hits.append((rank, key, str(t), {"iri": str(t), "label": label, "kind": term_kind}))
    hits.sort(key=lambda h: (h[0], h[1], h[2]))
    return [h[3] for h in hits[: max(0, min(limit, MAX_SEARCH))]]


def _attribute_domains(drafts: tuple[Draft, ...], props: set[str]) -> set[str]:
    """The domain classes of ``props`` in any of ``drafts``."""
    return {str(d) for g in (x.tbox for x in drafts) for p in props for d in g.objects(URIRef(p), RDFS.domain)}


def preview_view(
    before: Draft,
    after: Draft,
    scheme: IriScheme,
    *,
    revision: int,
    focus: str | None,
    badges: AboxBadges | None,
    diff: dict[str, dict[str, list[str]]],
) -> dict[str, Any]:
    """The graph of ``after`` with every node and edge marked by what the change from ``before`` did.

    ``preview`` is ``added``, ``changed``, ``removed`` or None. The view is ``after``'s, in the same
    full or focus mode as ``graph_view`` (a focus the change removes falls back to no focus). The
    classes and edges the change removes are added from ``before``'s view, edges only when both
    ends are shown. A class is changed when its own facts changed or an attribute of it was added,
    removed or changed; an edge is changed when its object property changed.
    """
    after_classes = schema_subjects(after.tbox)[0]
    view = graph_view(
        after,
        scheme,
        revision=revision,
        focus=focus if focus is not None and URIRef(focus) in after_classes else None,
        badges=badges,
    )
    chosen = view["focus"]
    old = graph_view(
        before,
        scheme,
        revision=revision,
        focus=chosen if chosen is not None and URIRef(chosen) in schema_subjects(before.tbox)[0] else None,
        badges=badges,
    )
    added = set(diff["classes"]["added"])
    removed = set(diff["classes"]["removed"])
    attributes = {iri for change in diff["datatypeProperties"].values() for iri in change}
    changed = (set(diff["classes"]["changed"]) | _attribute_domains((before, after), attributes)) - added - removed
    nodes = [
        {**n, "preview": "added" if n["iri"] in added else "changed" if n["iri"] in changed else None}
        for n in view["nodes"]
    ]
    shown = {n["iri"] for n in nodes}
    nodes += [{**n, "preview": "removed"} for n in old["nodes"] if n["iri"] in removed and n["iri"] not in shown]
    shown |= {n["iri"] for n in nodes}
    # Every edge of each draft, not only the ones its view shows: in a focus view an edge that existed
    # outside the old window is not one the change adds.
    before_edges = {e["id"] for e in _class_edges(before.tbox, schema_subjects(before.tbox)[0])}
    after_edges = {e["id"] for e in _class_edges(after.tbox, after_classes)}
    changed_props = set(diff["objectProperties"]["changed"])
    edges = [
        {
            **e,
            "preview": "added" if e["id"] not in before_edges else "changed" if e.get("iri") in changed_props else None,
        }
        for e in view["edges"]
    ]
    edges += [
        {**e, "preview": "removed"}
        for e in old["edges"]
        if e["id"] not in after_edges and e["source"] in shown and e["target"] in shown
    ]
    return {**view, "nodes": nodes, "edges": edges}
