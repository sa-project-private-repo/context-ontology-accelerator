# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The deployed version's TBox, read from its snapshot ``ontology.ttl`` (spec §6.3 step 2)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import unquote

from rdflib import BNode, Graph, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from coa_ontology.versions import snapshot

from .model import iri_tail

# Every class is a subclass of these, whether or not the snapshot says so.
_TOP = frozenset({str(OWL.Thing), str(RDFS.Resource)})


@dataclass(frozen=True)
class ClassInfo:
    """An ontology class: label and description (local name when unlabelled) and named parents."""

    iri: str
    label: str
    description: str
    parents: frozenset[str]


@dataclass(frozen=True)
class PropInfo:
    """An object or datatype property with its declared domain and range classes (empty means open)."""

    iri: str
    label: str
    description: str
    domain: frozenset[str]
    range: frozenset[str]


@dataclass
class TBox:
    """The classes and properties of one deployed ontology version."""

    classes: dict[str, ClassInfo]
    object_props: dict[str, PropInfo]
    datatype_props: dict[str, PropInfo]
    _ancestors: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)

    def ancestors(self, cls: str) -> frozenset[str]:
        """``cls``, every superclass reachable inside this TBox, and ``owl:Thing`` / ``rdfs:Resource``."""
        if cls not in self._ancestors:
            seen: set[str] = set()
            stack = [cls]
            while stack:
                c = stack.pop()
                if c in seen:
                    continue
                seen.add(c)
                info = self.classes.get(c)
                if info:
                    stack.extend(info.parents)
            self._ancestors[cls] = frozenset(seen) | _TOP
        return self._ancestors[cls]

    def _evaluable(self, members: frozenset[str]) -> bool:
        return all(m in self.classes or m in _TOP for m in members)

    def fits(self, prop: PropInfo, subject_cls: str, object_cls: str | None) -> bool:
        """Whether ``prop``'s domain admits the subject and its range the object.

        A domain or range is open, so it admits anything, when it is empty or when any member is
        not a class of this TBox (``owl:Thing`` and ``rdfs:Resource`` aside). The snapshot holds
        only the domain ontology, so a member such as a foundational ``fibo:Agent`` has no
        hierarchy here and cannot be evaluated. This matches how a blank-node domain is read.
        """
        if prop.domain and self._evaluable(prop.domain) and not prop.domain & self.ancestors(subject_cls):
            return False
        return not (
            object_cls is not None
            and prop.range
            and self._evaluable(prop.range)
            and not prop.range & self.ancestors(object_cls)
        )


def _text(g: Graph, s: URIRef, *preds: URIRef) -> str:
    for p in preds:
        values = sorted(str(o) for o in g.objects(s, p))
        if values:
            return values[0]
    return ""


def _fallback_label(iri: str) -> str:
    """Label of an unlabelled term: its IRI tail, percent-decoded (not the sanitized local name)."""
    return unquote(iri_tail(iri)) or iri


def _class_set(g: Graph, node: object, seen: frozenset[BNode] = frozenset()) -> set[str]:
    """Named classes a domain or range node stands for; empty when it cannot be resolved (open).

    A named class stands for itself and an ``owl:unionOf`` (nested ones included) for its members.
    A union with a member that is not a named class or a union (a restriction, an
    ``owl:intersectionOf``, ...) is open as a whole, as is any other blank node.
    """
    if isinstance(node, URIRef):
        return {str(node)}
    if not isinstance(node, BNode) or node in seen:
        return set()
    members: set[str] = set()
    for lst in g.objects(node, OWL.unionOf):
        for m in Collection(g, lst):
            resolved = _class_set(g, m, seen | {node})
            if not resolved:
                return set()
            members |= resolved
    return members


def _class_union(g: Graph, nodes: Iterable[object]) -> frozenset[str]:
    """Union of ``_class_set`` over several ``rdfs:domain`` (or ``rdfs:range``) values; open if any is.

    Strictly several domains intersect. They are read as a union instead, which is the pragmatic
    reading for a version merged from several sources.
    """
    members: set[str] = set()
    for node in nodes:
        resolved = _class_set(g, node)
        if not resolved:
            return frozenset()
        members |= resolved
    return frozenset(members)


def _props(g: Graph, kind: URIRef) -> dict[str, PropInfo]:
    out: dict[str, PropInfo] = {}
    for p in sorted({p for p in g.subjects(RDF.type, kind) if isinstance(p, URIRef)}, key=str):
        iri = str(p)
        out[iri] = PropInfo(
            iri,
            _text(g, p, RDFS.label, SKOS.prefLabel) or _fallback_label(iri),
            _text(g, p, RDFS.comment, SKOS.definition),
            _class_union(g, g.objects(p, RDFS.domain)),
            _class_union(g, g.objects(p, RDFS.range)),
        )
    return out


def load_tbox(text: str) -> TBox:
    """Parse a snapshot ``ontology.ttl`` into a ``TBox``."""
    g = snapshot.parse_turtle(text, what="ontology.ttl")
    classes: dict[str, ClassInfo] = {}
    declared = set(g.subjects(RDF.type, OWL.Class)) | set(g.subjects(RDF.type, RDFS.Class))
    # Sorted so the result does not depend on PYTHONHASHSEED.
    for c in sorted((c for c in declared if isinstance(c, URIRef) and c != OWL.Thing), key=str):
        iri = str(c)
        parents = frozenset(str(p) for p in g.objects(c, RDFS.subClassOf) if isinstance(p, URIRef) and p != OWL.Thing)
        classes[iri] = ClassInfo(
            iri,
            _text(g, c, RDFS.label, SKOS.prefLabel) or _fallback_label(iri),
            _text(g, c, RDFS.comment, SKOS.definition),
            parents,
        )
    return TBox(classes, _props(g, OWL.ObjectProperty), _props(g, OWL.DatatypeProperty))


def schema_hash(tbox: TBox, class_iris: Iterable[str], prop_iris: Iterable[str]) -> str:
    """Digest of the definitions a prompt shows; changes when any of them changes."""
    classes = [
        [c, tbox.classes[c].label, tbox.classes[c].description] for c in sorted(set(class_iris)) if c in tbox.classes
    ]
    props = []
    for p in sorted(set(prop_iris)):
        info = tbox.object_props.get(p) or tbox.datatype_props.get(p)
        if info:
            props.append([p, info.label, info.description, sorted(info.domain), sorted(info.range)])
    return hashlib.sha256(json.dumps({"classes": classes, "props": props}, sort_keys=True).encode()).hexdigest()
