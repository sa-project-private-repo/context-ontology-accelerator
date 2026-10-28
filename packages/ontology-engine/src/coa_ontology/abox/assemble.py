# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic assembly of the domain ABox, validated against the TBox (spec §3, §6.3 step 6).

Types and relations are enforced (only mapped classes, only object properties whose
domain/range admit both ends); attributes are free (a TBox datatype property when one
fits, else a minted ``coa:FreeAttribute``). Output depends only on the inputs, never on
their order. ``buildId``/``builtAt`` are left to the builder so the checksum stays stable.

Both ends of a fact are created whenever their own class is mapped, even if the other end is not;
a relation is linked only when both exist and its mapping fits. An attribute fact needs only its
subject. An entity whose name has no visible character is skipped. Attribute values are plain
string literals: no datatype, no language tag.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from urllib.parse import unquote

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, SKOS

from . import model as m
from .mapping import ACTIVE, ATTRIBUTE, CLASS, FREE, IGNORE, INVERSE, RELATION, MappingRow, mapping_id, valid_target
from .tbox import TBox

MAX_EVIDENCE = 5


@dataclass
class Stats:
    """What an assembly produced: counts of instances, relations, attributes and evidence nodes."""

    instances: int = 0
    relations: int = 0
    attributes: int = 0
    evidence: int = 0
    class_counts: dict[str, int] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        """The totals as stored in the ABox state row."""
        return {
            "instances": self.instances,
            "relations": self.relations,
            "attributes": self.attributes,
            "evidence": self.evidence,
        }


class _Builder:
    def __init__(self, namespace: str) -> None:
        self.ns = namespace
        self.types: dict[str, str] = {}
        self.surfaces: dict[str, Counter[str]] = defaultdict(Counter)
        self.norm_keys: dict[str, set[str]] = defaultdict(set)
        self.lexical_ids: dict[str, set[str]] = defaultdict(set)
        self.type_provs: dict[str, set[m.Provenance]] = defaultdict(set)
        self.links: dict[tuple[str, str, str], set[m.Provenance]] = defaultdict(set)
        self.values: dict[tuple[str, str, str], set[m.Provenance]] = defaultdict(set)
        self.free: dict[str, str] = {}

    def instance(self, cls: str, name: str, prov: m.Provenance, lexical_id: str | None = None) -> str:
        iri = m.instance_iri(self.ns, cls, name)
        self.types[iri] = cls
        self.surfaces[iri][_surface(name)] += 1
        self.norm_keys[iri].add(m.norm_key(name))
        self.type_provs[iri].add(prov)
        if lexical_id:
            self.lexical_ids[iri].add(lexical_id)
        return iri

    def attribute(self, subject: str, prop: str, value: str, prov: m.Provenance) -> None:
        self.values[(subject, prop, value)].add(prov)

    def free_attribute(self, key: str) -> str:
        iri = m.free_attribute_iri(self.ns, key)
        # Keys that differ only by case share an IRI: label it with the smallest, whatever the order.
        self.free[iri] = min(self.free.get(iri, key), key)
        return iri


# Whitespace runs to collapse. The Unicode line and paragraph separators are left as written: they are
# the characters that N-Triples leaves unescaped, which the build's round trip must keep exercising.
_SPACES = re.compile(r"[^\S\u0085\u2028\u2029]+")


def _surface(name: str) -> str:
    """A name as displayed: format characters dropped, whitespace collapsed (``norm`` stays the identity)."""
    return _SPACES.sub(" ", "".join(c for c in name if unicodedata.category(c) != "Cf")).strip()


def _usable(name: str) -> bool:
    return bool(m.norm(name)) and bool(_surface(name))


def _active(rows: dict[str, MappingRow], tbox: TBox, kind: str, key: tuple[str, ...]) -> MappingRow | None:
    """The row's mapping when it applies: active, not ignored, and its target is a term of this TBox."""
    row = rows.get(mapping_id(kind, key))
    if row is None or row.status != ACTIVE or row.target == IGNORE or not valid_target(tbox, kind, row.target):
        return None
    return row


def _datatype_for(tbox: TBox, cls: str, key: str) -> str | None:
    wanted = m.norm(key)
    for p in sorted(tbox.datatype_props.values(), key=lambda p: p.iri):
        if tbox.fits(p, cls, None) and wanted in (m.norm(p.label), m.norm(unquote(m.iri_tail(p.iri)))):
            return p.iri
    return None


def _attribute_prop(
    b: _Builder, tbox: TBox, rows: dict[str, MappingRow], cls: str, key: str, from_mapping: bool
) -> str | None:
    if from_mapping:
        row = _active(rows, tbox, ATTRIBUTE, (cls, key))
        if row is None:
            return None
        if row.target != FREE:
            return row.target
    else:
        mapped = _datatype_for(tbox, cls, key)
        if mapped:
            return mapped
    return b.free_attribute(key)


def _end(
    b: _Builder,
    tbox: TBox,
    rows: dict[str, MappingRow],
    entity: m.LexEntity | None,
    prov: m.Provenance,
) -> str | None:
    """The instance for one end of a fact, created when its class is mapped and its name is visible."""
    if entity is None or not _usable(entity.value):
        return None
    row = _active(rows, tbox, CLASS, (entity.cls,))
    return b.instance(row.target, entity.value, prov, entity.id) if row else None


def _lexical(b: _Builder, tbox: TBox, lex: m.LexicalSnapshot, rows: dict[str, MappingRow]) -> None:
    for fact in sorted(lex.facts, key=lambda f: (f.id, f.prov)):
        subj = lex.entities.get(fact.subject_id)
        obj = lex.entities.get(fact.object_id) if fact.object_id else None
        # Subject first, then object, each created on its own merits.
        s = _end(b, tbox, rows, subj, fact.prov)
        if obj is None:
            continue
        if obj.cls == m.LOCAL_ENTITY_CLASS:
            if s is not None and _usable(obj.value):
                prop = _attribute_prop(b, tbox, rows, b.types[s], fact.relation, from_mapping=True)
                if prop:
                    b.attribute(s, prop, obj.value, fact.prov)
            continue
        o = _end(b, tbox, rows, obj, fact.prov)
        if s is None or o is None:
            continue
        rel = _active(rows, tbox, RELATION, (fact.relation, b.types[s], b.types[o]))
        info = tbox.object_props.get(rel.target) if rel else None
        if rel is None or info is None:
            continue
        sub, ob = (o, s) if rel.direction == INVERSE else (s, o)
        if tbox.fits(info, b.types[sub], b.types[ob]):
            b.links[(sub, rel.target, ob)].add(fact.prov)


def _extracted(
    b: _Builder,
    tbox: TBox,
    rows: dict[str, MappingRow],
    extracted: tuple[list[m.XEntity], list[m.XRelation]],
) -> None:
    entities, relations = extracted
    for e in sorted(entities, key=lambda e: (e.cls, m.norm(e.name), e.name, e.prov)):
        if e.cls not in tbox.classes or not _usable(e.name):
            continue
        s = b.instance(e.cls, e.name, e.prov)
        for key, value in e.attributes:
            if not (_usable(key) and _usable(value)):
                continue
            prop = _attribute_prop(b, tbox, rows, e.cls, key, from_mapping=False)
            if prop:
                b.attribute(s, prop, value, e.prov)
    for r in sorted(relations, key=lambda r: (r.subject, r.prop, r.object, r.prov)):
        info = tbox.object_props.get(r.prop)
        if info is None or r.subject[0] not in tbox.classes or r.object[0] not in tbox.classes:
            continue
        if not (_usable(r.subject[1]) and _usable(r.object[1])):
            continue
        if not tbox.fits(info, r.subject[0], r.object[0]):
            continue
        s = b.instance(*r.subject, r.prov)
        o = b.instance(*r.object, r.prov)
        b.links[(s, r.prop, o)].add(r.prov)


def _evidence(g: Graph, b: _Builder, s: str, p: str, o: URIRef | Literal, provs: set[m.Provenance]) -> int:
    # One node per supporting chunk: a chunk read twice keeps its first (smallest) provenance.
    by_chunk: dict[str, m.Provenance] = {}
    for prov in sorted(provs):
        by_chunk.setdefault(prov.chunk_id, prov)
    added = 0
    for prov in list(by_chunk.values())[:MAX_EVIDENCE]:
        e = URIRef(m.evidence_iri(b.ns, s, p, o.n3(), prov.chunk_id))  # n3 keeps the IRI/literal distinction
        g.add((e, RDF.type, URIRef(m.EVIDENCE)))
        g.add((e, URIRef(m.ABOUT), URIRef(s)))
        g.add((e, URIRef(m.PREDICATE), URIRef(p)))
        g.add((e, URIRef(m.OBJECT), o))
        g.add((e, URIRef(m.CHUNK_ID), Literal(prov.chunk_id)))
        if prov.doc_source_id:
            g.add((e, URIRef(m.DOC_SOURCE_ID), Literal(prov.doc_source_id)))
        if prov.source_key:
            g.add((e, URIRef(m.SOURCE_KEY), Literal(prov.source_key)))
        added += 1
    return added


def assemble(
    namespace: str,
    tbox: TBox,
    lex: m.LexicalSnapshot,
    rows: dict[str, MappingRow],
    extracted: tuple[list[m.XEntity], list[m.XRelation]],
    *,
    version: int,
    mapping_revision: str,
) -> tuple[Graph, Stats]:
    """Build the domain ABox graph (instances, relations, attributes, evidence, header) and its counts."""
    b = _Builder(namespace)
    _lexical(b, tbox, lex, rows)
    _extracted(b, tbox, rows, extracted)
    g = Graph()
    stats = Stats()
    for iri in sorted(b.types):
        node = URIRef(iri)
        g.add((node, RDF.type, URIRef(b.types[iri])))
        surfaces = sorted(b.surfaces[iri].items(), key=lambda kv: (-kv[1], kv[0]))
        g.add((node, RDFS.label, Literal(surfaces[0][0])))
        for alt, _ in surfaces[1:]:
            g.add((node, SKOS.altLabel, Literal(alt)))
        for norm_key in sorted(b.norm_keys[iri]):
            g.add((node, URIRef(m.NORM_KEY), Literal(norm_key)))
        for lexical_id in sorted(b.lexical_ids[iri]):
            g.add((node, URIRef(m.LEXICAL_ENTITY_ID), Literal(lexical_id)))
        stats.evidence += _evidence(g, b, iri, str(RDF.type), URIRef(b.types[iri]), b.type_provs[iri])
        stats.instances += 1
        stats.class_counts[b.types[iri]] = stats.class_counts.get(b.types[iri], 0) + 1
    for (s, p, o), provs in sorted(b.links.items()):
        g.add((URIRef(s), URIRef(p), URIRef(o)))
        stats.evidence += _evidence(g, b, s, p, URIRef(o), provs)
        stats.relations += 1
    for (s, p, value), provs in sorted(b.values.items()):
        g.add((URIRef(s), URIRef(p), Literal(value)))
        stats.evidence += _evidence(g, b, s, p, Literal(value), provs)
        stats.attributes += 1
    for iri, key in sorted(b.free.items()):
        g.add((URIRef(iri), RDF.type, URIRef(m.FREE_ATTRIBUTE)))
        g.add((URIRef(iri), RDFS.label, Literal(key)))
    header = URIRef(m.header_iri(namespace))
    g.add((header, RDF.type, URIRef(m.DOMAIN_ABOX)))
    g.add((header, URIRef(m.CONFORMS_TO_VERSION), Literal(f"v{version}")))
    g.add((header, URIRef(m.MAPPING_REVISION), Literal(mapping_revision)))
    return g, stats
