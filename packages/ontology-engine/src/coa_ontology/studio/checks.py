# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Checks of a designer draft: instant structural checks, the structured-query check and data comparison.

``instant_checks`` reads the draft's TBox alone, so it is cheap enough to run for every revision.
``structured_check`` reads the draft's TBox and mapping: what structured queries (Ontop and NL-to-SQL)
cannot answer, and the table and column names they keep using; ``removed_mapped`` lists the mapped
terms of the version the draft was built on that the draft deletes.
``data_checks`` compares the draft with what exists outside it: the source catalog the mapping
names (as the version publish checks it), the active version's domain ABox and mapping rows, and
the governed metrics. Each data section is best effort: a dependency that fails fills only that section's
``error``, with a fixed message, and ``errorCode``, its code; the details go to the log. A finding's
``message`` is English; ``messageCode`` and ``messageParams`` give the same sentence as a code of
``messages.ENGLISH`` and its parameters, which the web app writes in the user's language.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from rdflib import OWL, RDF, RDFS, Graph, URIRef
from rdflib.namespace import SKOS
from rdflib.term import Node

from coa_ontology.abox import mapping as abox_mapping
from coa_ontology.abox.model import iri_tail, norm
from coa_ontology.abox.tbox import load_tbox
from coa_ontology.catalog.ingest import _mapped_predicates_from_r2rml, schema_subjects
from coa_ontology.datasource_ids import bare_datasource_id
from coa_ontology.versions import deploy as version_deploy
from coa_ontology.versions import publish as version_publish
from coa_ontology.versions import registry, snapshot, storage

from . import draft as store
from . import graph_view, iris, ops
from .iris import IriError, IriScheme
from .messages import Message, english, message
from .ops import CLASS, DATATYPE_PROPERTY, OBJECT_PROPERTY, RR, Draft, kind_of, label_of

_log = logging.getLogger("studio.checks")
MAX_FINDINGS = 200
ERROR, WARNING, INFO = "error", "warning", "info"
_SEVERITY_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}
_DUPLICATE_LABEL = {
    CLASS: "DUPLICATE_CLASS_LABEL",
    OBJECT_PROPERTY: "DUPLICATE_RELATIONSHIP_LABEL",
    DATATYPE_PROPERTY: "DUPLICATE_ATTRIBUTE_LABEL",
}
TABLES_ERROR = "The source catalog could not be read."
ABOX_ERROR = "The domain data could not be read."
MAPPINGS_ERROR = "The document mappings could not be read."
METRICS_ERROR = "Breaking metrics could not be checked; the graph database may be unreachable."
BASE_MAPPING_ERROR = "The table mappings of the version the draft was built on could not be read."
# The model's bounds: ``StudioStructuredTermList`` and ``StudioStructuredTerm.tables``.
MAX_STRUCTURED_TERMS = 100
MAX_TERM_TABLES = 20


def _finding(code: str, severity: str, text: Message, iris_: Iterable[Node]) -> dict[str, Any]:
    """A finding of kind ``code``; ``text`` is its sentence, which ``message`` gives in English."""
    return {
        "code": code,
        "severity": severity,
        "message": english(text),
        "messageCode": text["code"],
        "messageParams": text["params"],
        "iris": sorted({str(i) for i in iris_}),
    }


def _name(g: Graph, term: Node) -> str:
    return label_of(g, term) if kind_of(g, term) is not None else (iri_tail(str(term)) or str(term))


def _is_datatype(term: URIRef) -> bool:
    try:
        iris.datatype_iri(str(term))
    except IriError:
        return False
    return True


def _axis_findings(g: Graph, scheme: IriScheme, props: Iterable[URIRef]) -> list[dict[str, Any]]:
    """Domains and ranges that name no class of the draft, and datatype ranges that are no datatype."""
    out: list[dict[str, Any]] = []
    for p in sorted(props, key=str):
        kind = kind_of(g, p)
        for d in sorted((x for x in g.objects(p, RDFS.domain) if isinstance(x, URIRef)), key=str):
            if kind_of(g, d) != CLASS:
                severity = ERROR if scheme.in_space(str(d)) else WARNING
                text = message("DANGLING_DOMAIN", property=_name(g, p), domain=_name(g, d))
                out.append(_finding("DANGLING_DOMAIN", severity, text, [p, d]))
        for r in sorted((x for x in g.objects(p, RDFS.range) if isinstance(x, URIRef)), key=str):
            if kind == OBJECT_PROPERTY and kind_of(g, r) != CLASS:
                severity = ERROR if scheme.in_space(str(r)) else WARNING
                text = message("DANGLING_RANGE", property=_name(g, p), range=_name(g, r))
                out.append(_finding("DANGLING_RANGE", severity, text, [p, r]))
            elif kind == DATATYPE_PROPERTY and not _is_datatype(r):
                if kind_of(g, r) == CLASS:
                    text = message("DATATYPE_RANGE_NOT_DATATYPE", property=_name(g, p), range=_name(g, r))
                    out.append(_finding("DATATYPE_RANGE_NOT_DATATYPE", ERROR, text, [p, r]))
                else:
                    text = message("UNKNOWN_DATATYPE_RANGE", property=_name(g, p), range=r)
                    out.append(_finding("DANGLING_RANGE", ERROR, text, [p, r]))
    return out


def _cycles(g: Graph, classes: set[URIRef]) -> list[list[URIRef]]:
    """Every ``rdfs:subClassOf`` cycle among the named classes (Tarjan's strongly connected components)."""
    edges: dict[URIRef, list[URIRef]] = {
        c: sorted({p for p in g.objects(c, RDFS.subClassOf) if isinstance(p, URIRef) and p in classes}, key=str)
        for c in classes
    }
    index: dict[URIRef, int] = {}
    low: dict[URIRef, int] = {}
    stack: list[URIRef] = []
    on_stack: set[URIRef] = set()
    found: list[list[URIRef]] = []
    counter = 0
    for root in sorted(classes, key=str):
        if root in index:
            continue
        # Iterative DFS: a deep taxonomy must not hit the recursion limit.
        work: list[tuple[URIRef, int]] = [(root, 0)]
        while work:
            node, i = work.pop()
            if i == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            if i < len(edges[node]):
                work.append((node, i + 1))
                nxt = edges[node][i]
                if nxt not in index:
                    work.append((nxt, 0))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            if low[node] == index[node]:
                component = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    component.append(w)
                    if w == node:
                        break
                if len(component) > 1 or node in edges[node]:
                    found.append(sorted(component, key=str))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return found


def _has_label(g: Graph, term: URIRef) -> bool:
    return any(True for p in (RDFS.label, SKOS.prefLabel) for _ in g.objects(term, p))


def instant_checks(d: Draft, scheme: IriScheme) -> dict[str, Any]:
    """The structural findings of a draft, worst first, at most ``MAX_FINDINGS``; ``counts`` counts them all."""
    g = d.tbox
    classes, obj_props, dt_props = schema_subjects(g)
    own_classes = {c for c in classes if kind_of(g, c) == CLASS}
    props = {p for p in obj_props | dt_props if kind_of(g, p) in (OBJECT_PROPERTY, DATATYPE_PROPERTY)}
    findings = _axis_findings(g, scheme, props)
    for cycle in _cycles(g, own_classes):
        names = " -> ".join(_name(g, c) for c in cycle)
        findings.append(_finding("SUBCLASS_CYCLE", ERROR, message("SUBCLASS_CYCLE", classes=names), cycle))
    terms = sorted(own_classes | props, key=str)
    for t in terms:
        if scheme.in_space(str(t)) and not _has_label(g, t):
            text = message("MISSING_LABEL", term=iri_tail(str(t)) or t)
            findings.append(_finding("MISSING_LABEL", WARNING, text, [t]))
    groups: dict[tuple[str, str], list[URIRef]] = {}
    for t in terms:
        if _has_label(g, t):
            groups.setdefault((kind_of(g, t) or "", norm(label_of(g, t))), []).append(t)
    for (kind, _), same in sorted(groups.items()):
        if len(same) > 1:
            code = _DUPLICATE_LABEL.get(kind, "DUPLICATE_TERM_LABEL")
            text = message(code, count=len(same), label=label_of(g, same[0]))
            findings.append(_finding("DUPLICATE_LABEL", WARNING, text, same))
    used = {o for p in props for axis in (RDFS.domain, RDFS.range) for o in g.objects(p, axis)}
    for c in sorted(own_classes, key=str):
        linked = (
            c in used
            or any(p in own_classes for p in g.objects(c, RDFS.subClassOf))
            or any(s in own_classes for s in g.subjects(RDFS.subClassOf, c))
            or (c, OWL.equivalentClass, None) in g
        )
        if scheme.in_space(str(c)) and not linked:
            findings.append(_finding("ISOLATED_CLASS", INFO, message("ISOLATED_CLASS", term=_name(g, c)), [c]))
    findings.sort(key=lambda f: (_SEVERITY_ORDER[f["severity"]], f["code"], f["iris"]))
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in (ERROR, WARNING, INFO)}
    return {"findings": findings[:MAX_FINDINGS], "counts": counts, "truncated": len(findings) > MAX_FINDINGS}


# ── Structured queries ──────────────────────────────────────────────────────


def mapped_classes(r2rml: Graph) -> set[URIRef]:
    """The classes the mapping maps: each is the ``rr:class`` of the subject map of a node typed ``rr:TriplesMap``."""
    return {
        c
        for t in r2rml.subjects(RDF.type, RR.TriplesMap)
        for sm in r2rml.objects(t, RR.subjectMap)
        for c in r2rml.objects(sm, RR["class"])
        if isinstance(c, URIRef)
    }


def _answered_classes(g: Graph, mapped: set[URIRef]) -> set[URIRef]:
    """The mapped classes, every class one of them is a subclass of (at any depth), and their equivalents.

    The VKG answers such a class through the mapped class below it, and the query console does not
    refuse it (serve's ``refuse_vkg_terms``); ``owl:equivalentClass`` counts either way.
    """
    answered = set(mapped)
    stack = list(mapped)
    while stack:
        c = stack.pop()
        equivalents = {*g.objects(c, OWL.equivalentClass), *g.subjects(OWL.equivalentClass, c)}
        for n in {*g.objects(c, RDFS.subClassOf), *equivalents}:
            if isinstance(n, URIRef) and n not in answered:
                answered.add(n)
                stack.append(n)
    return answered


def _group(terms: list[dict[str, Any]]) -> dict[str, Any]:
    """Terms sorted by label then IRI, at most ``MAX_STRUCTURED_TERMS`` of them; ``count`` counts them all."""
    terms.sort(key=lambda t: (t["label"].casefold(), t["label"], t["iri"]))
    return {"count": len(terms), "terms": terms[:MAX_STRUCTURED_TERMS]}


def _mapped_ends(g: Graph, p: URIRef, axis: URIRef, mapped: set[URIRef]) -> list[URIRef]:
    """The mapped classes among the ``axis`` (``rdfs:domain`` or ``rdfs:range``) of ``p``, sorted by IRI."""
    return sorted((x for x in g.objects(p, axis) if isinstance(x, URIRef) and x in mapped), key=str)


def _attribute_column(
    r2rml: Graph, p: URIRef, domains: list[URIRef], tables: dict[URIRef, set[ops.Table]]
) -> str | None:
    """The column NL-to-SQL names attribute ``p`` by: its ``rr:column`` in a mapped domain that reads one table."""
    for c in domains:
        if len(tables[c]) != 1:
            continue  # NL-to-SQL describes a class read from several tables by its labels
        maps = ops.triples_maps_of_class(r2rml, c)
        columns = sorted(
            ops.sql_name(column)
            for tmap, pom in ops.property_maps(r2rml, p)
            if tmap in maps
            for om in r2rml.objects(pom, RR.objectMap)
            for column in r2rml.objects(om, RR.column)
        )
        if columns:
            return columns[0]
    return None


def structured_check(d: Draft, *, base_maps_tables: bool, only: set[str] | None = None) -> dict[str, Any]:
    """What structured queries cannot answer in ``d``, and the mapped terms they know by another name.

    The definitions are the engine's (deploy marks the same terms): a class is mapped when it is the
    ``rr:class`` of a TriplesMap (:func:`mapped_classes`), a property when a PredicateObjectMap maps it
    (``ops.property_maps``), and a property is unmapped on a mapped class when it is not mapped and one
    of its domains is, or, for a relationship, one of its ranges (the NL-to-SQL foreign-key walk follows
    a relationship from both ends). Its ``domainIri`` is that mapped class: the first mapped domain by
    IRI, else the first mapped range. ``unmappedClasses`` leaves out a class that is not mapped but is a
    parent or an equivalent of a mapped one (:func:`_answered_classes`), as serve does: structured
    queries answer it through that class. A mapped class or attribute is listed in ``labelDiffers`` when its
    label is not the table or column name NL-to-SQL keeps writing; a class read from several tables (a
    table is a datasource and a qualified name, ``ops.map_tables``) is listed in ``multiTableClasses``
    instead.
    ``only`` keeps the terms whose IRI it holds (the accept preview passes the terms the diff adds or
    changes); ``mapsNoTable`` is about the whole draft: it maps no table, although the version it was
    built on (``base_maps_tables``) did. No I/O.
    """
    g, r2rml = d.tbox, d.r2rml
    classes, obj_props, dt_props = schema_subjects(g)
    mapped = mapped_classes(r2rml)
    answered = _answered_classes(g, mapped)
    tables = {c: ops.class_tables(r2rml, c) for c in mapped}
    unmapped_classes: list[dict[str, Any]] = []
    unmapped_properties: list[dict[str, Any]] = []
    label_differs: list[dict[str, Any]] = []
    multi_table: list[dict[str, Any]] = []
    for c in classes:
        if kind_of(g, c) != CLASS or (only is not None and str(c) not in only):
            continue
        term = {"iri": str(c), "label": label_of(g, c)}
        if c not in mapped:
            if c not in answered:
                unmapped_classes.append(term)
        elif len(tables[c]) > 1:
            multi_table.append({**term, "tables": ops.table_names(tables[c])[:MAX_TERM_TABLES]})
        elif tables[c]:
            ((_, path),) = tables[c]
            if term["label"] != path[-1]:
                label_differs.append({**term, "physicalName": path[-1]})
    for p in obj_props | dt_props:
        kind = kind_of(g, p)
        if kind not in (OBJECT_PROPERTY, DATATYPE_PROPERTY) or (only is not None and str(p) not in only):
            continue
        domains = _mapped_ends(g, p, RDFS.domain, mapped)
        ends = domains or (_mapped_ends(g, p, RDFS.range, mapped) if kind == OBJECT_PROPERTY else [])
        if not ends:
            continue
        term = {"iri": str(p), "label": label_of(g, p)}
        if not ops.property_maps(r2rml, p):
            unmapped_properties.append({**term, "domainIri": str(ends[0])})
        elif kind == DATATYPE_PROPERTY:
            column = _attribute_column(r2rml, p, domains, tables)
            if column is not None and term["label"] != column:
                label_differs.append({**term, "physicalName": column})
    return {
        "unmappedClasses": _group(unmapped_classes),
        "unmappedProperties": _group(unmapped_properties),
        "labelDiffers": _group(label_differs),
        "multiTableClasses": _group(multi_table),
        "mapsNoTable": base_maps_tables and not mapped,
    }


def _pom_reads(r2rml: Graph, tmap: Node, pom: Node) -> tuple[frozenset[ops.Table], frozenset[tuple[Any, ...]]]:
    """What PredicateObjectMap ``pom`` of TriplesMap ``tmap`` reads, whatever property it maps.

    The tables ``tmap`` reads, and each object map's column, template or constant, or the tables of the
    TriplesMap it joins with and the join's columns. A map that is a blank node is another node in each
    parse of the mapping; what it reads stays the same.
    """
    reads: set[tuple[Any, ...]] = {(RR.constant, o) for o in r2rml.objects(pom, RR.object)}
    for om in r2rml.objects(pom, RR.objectMap):
        reads.update(
            (kind, value) for kind in (RR.column, RR.template, RR.constant) for value in r2rml.objects(om, kind)
        )
        for parent in r2rml.objects(om, RR.parentTriplesMap):
            joins = frozenset(
                (child, column)
                for condition in r2rml.objects(om, RR.joinCondition)
                for child in r2rml.objects(condition, RR.child)
                for column in r2rml.objects(condition, RR.parent)
            )
            reads.add((RR.parentTriplesMap, frozenset(ops.map_tables(r2rml, parent)), joins))
    return frozenset(ops.map_tables(r2rml, tmap)), frozenset(reads)


def deleted_mapped_terms(d: Draft, base_r2rml: Graph) -> tuple[list[URIRef], list[URIRef]]:
    """The classes and the properties that ``base_r2rml`` maps and ``d`` deletes, each sorted by IRI.

    A class is deleted when no class of ``d`` has its IRI, and ``d``'s mapping keeps none of the class's
    TriplesMaps and reads none of its tables: a rename or a merge moves the maps with the class, and a
    table the draft still reads is still answered. A property is deleted when no property of ``d`` has
    its IRI and ``d``'s mapping keeps none of the PredicateObjectMaps that mapped it, the same node or
    one that reads the same (:func:`_pom_reads`): a rename moves them too, and a map that is a blank
    node is another node once the draft is stored. Mapped means what deploy reads:
    :func:`mapped_classes`, and every property that a PredicateObjectMap names, also a predicate the
    base ontology does not declare (:func:`removed_mapped` leaves those out). No I/O.
    """
    classes, obj_props, dt_props = schema_subjects(d.tbox)
    properties = obj_props | dt_props
    kept = set(d.r2rml.subjects())
    read = {table for t in d.r2rml.subjects(RDF.type, RR.TriplesMap) for table in ops.map_tables(d.r2rml, t)}
    reads = {_pom_reads(d.r2rml, t, pom) for t, pom in d.r2rml.subject_objects(RR.predicateObjectMap)}
    removed_classes = [
        c
        for c in mapped_classes(base_r2rml)
        if c not in classes
        and not ops.triples_maps_of_class(base_r2rml, c) & kept
        and not ops.class_tables(base_r2rml, c) & read
    ]
    removed_properties = [
        p
        for p in map(URIRef, _mapped_predicates_from_r2rml(base_r2rml))
        if p not in properties
        and not any(
            pom in kept or _pom_reads(base_r2rml, t, pom) in reads for t, pom in ops.property_maps(base_r2rml, p)
        )
    ]
    return sorted(removed_classes, key=str), sorted(removed_properties, key=str)


def removed_mapped(base: Draft, classes: list[URIRef], properties: list[URIRef]) -> dict[str, Any]:
    """``removedMappedClasses`` and ``removedMappedProperties`` for what :func:`deleted_mapped_terms` found.

    Every term is described from ``base``, the version the draft was built on: its label; for a class
    the tables it was mapped from; for a property ``domainIri``, the first (by IRI) of the classes whose
    TriplesMaps mapped it, and ``physicalName`` when every one of its PredicateObjectMaps read the same
    single column (an attribute with one column). A predicate that ``base``'s ontology does not declare
    an attribute or relationship (``rdfs:label`` read from a column, say) is no term of the designer
    and is left out; its table is listed under its class when the class is deleted. No I/O.
    """
    g, r2rml = base.tbox, base.r2rml
    removed_classes = [
        {
            "iri": str(c),
            "label": label_of(g, c),
            "tables": ops.table_names(ops.class_tables(r2rml, c))[:MAX_TERM_TABLES],
        }
        for c in classes
    ]
    removed_properties: list[dict[str, Any]] = []
    for p in properties:
        if kind_of(g, p) not in ops.PROPERTY_KINDS:
            continue
        pairs = ops.property_maps(r2rml, p)
        term: dict[str, Any] = {"iri": str(p), "label": label_of(g, p)}
        domains = sorted(
            (
                c
                for tmap, _ in pairs
                for sm in r2rml.objects(tmap, RR.subjectMap)
                for c in r2rml.objects(sm, RR["class"])
            ),
            key=str,
        )
        if domains:
            term["domainIri"] = str(domains[0])
        columns = [
            {ops.sql_name(c) for om in r2rml.objects(pom, RR.objectMap) for c in r2rml.objects(om, RR.column)}
            for _, pom in pairs
        ]
        if columns and all(len(names) == 1 for names in columns) and len(set.union(*columns)) == 1:
            (term["physicalName"],) = columns[0]
        removed_properties.append(term)
    return {
        "removedMappedClasses": _group(removed_classes),
        "removedMappedProperties": _group(removed_properties),
    }


def _version_mapping(namespace: str, version: int | None) -> Graph:
    """The mapping of version ``version``; empty when there is no version yet or it maps nothing."""
    if version is None:
        return Graph()
    r2rml = storage.read_version_file(namespace, version, "mappings.r2rml")  # absent when it maps nothing
    return snapshot.parse_turtle(r2rml, what="R2RML") if r2rml else Graph()


def structured(
    namespace: str,
    state: store.StudioState,
    d: Draft,
    only: set[str] | None = None,
    base_tbox: Graph | None = None,
) -> dict[str, Any]:
    """:func:`structured_check` of ``d``, and :func:`removed_mapped` against the version it was built on.

    That version's mapping is read for ``mapsNoTable`` and the deletions, and its ontology, unless the
    caller passes it (``base_tbox``), only when the draft deletes a mapped term: their labels are only
    there. ``only`` limits the :func:`structured_check` groups, not the deletions, which a diff lists as
    removed; with ``base_tbox`` it also keeps the classes that version answered and ``d`` does not
    (:func:`_answered_classes`), which an edit to another term can leave unanswered (a mapped class no
    longer below its parent). ``error`` says that version could not be read: ``mapsNoTable`` is then
    false and nothing is listed as deleted.
    """
    error = None
    try:
        base_r2rml = _version_mapping(namespace, state.base_version)
        classes, properties = deleted_mapped_terms(d, base_r2rml)
        if (classes or properties) and base_tbox is None and state.base_version is not None:
            ontology = storage.require_version_file(namespace, state.base_version, "ontology.ttl")
            base_tbox = snapshot.parse_turtle(ontology, what="ontology")
    except Exception:  # noqa: BLE001 - the rest is read from the draft and still holds
        _log.warning("studio checks: the mapping of v%s could not be read", state.base_version, exc_info=True)
        base_r2rml, classes, properties, error = Graph(), [], [], BASE_MAPPING_ERROR
    base = Draft(base_tbox if base_tbox is not None else Graph(), base_r2rml)
    if only is not None and base_tbox is not None:
        was_answered = _answered_classes(base_tbox, mapped_classes(base_r2rml))
        only = only | {str(c) for c in was_answered - _answered_classes(d.tbox, mapped_classes(d.r2rml))}
    return {
        **structured_check(d, base_maps_tables=bool(mapped_classes(base_r2rml)), only=only),
        **removed_mapped(base, classes, properties),
        "error": error,
    }


# ── Data comparison ─────────────────────────────────────────────────────────


def _tables(namespace: str, item: dict[str, Any], state: store.StudioState, d: Draft, r2rml_nt: str) -> dict:
    """The tables the draft's mapping names that the source catalog does not have (as the version publish checks it)."""
    from coa_ontology.proposals import SchemaSqlCoverageError, _generate_schema_sql

    if not r2rml_nt.strip():
        return {"checked": False, "missing": []}
    metadata = item.get("metadata") or {}
    datasources = {bare_datasource_id(str(x)) for x in metadata.get("datasource_ids") or []}
    datasources |= version_publish._datasource_ids(d.r2rml)
    try:
        _generate_schema_sql(
            namespace, state.ontology_id, r2rml_nt, extra_datasource_ids=sorted(datasources), scan_accepted=False
        )
    except SchemaSqlCoverageError as e:
        return {"checked": True, "missing": sorted(e.missing)}
    return {"checked": True, "missing": []}


def _moved(namespace: str, proposal_id: str, state: store.StudioState, classes: set[str]) -> dict[str, tuple[str, str]]:
    """Where each class renamed or merged away ended up (one of ``classes``), and whether it was renamed or merged.

    As in the accept preview, a class whose last IRI is no class of the draft any more was removed.
    """
    from .router import _renames

    return {
        r["fromIri"]: (r["toIri"], r["kind"]) for r in _renames(namespace, proposal_id, state) if r["toIri"] in classes
    }


def _abox(namespace: str, proposal_id: str, state: store.StudioState, d: Draft) -> dict:
    """Classes of the active version with instances that the draft removes, renames or merges away."""
    badges = graph_view.abox_badges(namespace)
    if badges is None:
        return {"available": False, "impacts": []}
    draft_classes = {str(c) for c in schema_subjects(d.tbox)[0]}
    moved = _moved(namespace, proposal_id, state, draft_classes)
    impacts: list[dict[str, Any]] = []
    for iri in sorted(badges.active_classes):
        count = badges.counts.get(iri, 0)
        if count and iri not in draft_classes:
            to, change = moved.get(iri, (None, "removed"))
            impacts.append({"classIri": iri, "instances": count, "change": change, "toIri": to})
    impacts.sort(key=lambda x: (-x["instances"], x["classIri"]))
    return {"available": True, "current": badges.current, "impacts": impacts}


def _mappings(namespace: str, tbox_nt: str) -> dict:
    """How many document mapping rows the draft would leave stale once deployed."""
    active = (registry.get_active(namespace) or {}).get("version")
    rows = abox_mapping.list_rows(namespace)
    if active is None or not rows:
        return {"available": active is not None, "stale": 0, "staleTargets": []}
    active_tbox = load_tbox(storage.require_version_file(namespace, int(active), "ontology.ttl"))
    draft_tbox = load_tbox(tbox_nt)
    before = {r.id: r.status for r in abox_mapping.effective(rows, active_tbox)}
    newly = [
        r
        for r in abox_mapping.effective(rows, draft_tbox)
        if r.status == abox_mapping.STALE and before[r.id] != abox_mapping.STALE
    ]
    # One target for each stale row, so the list matches the count until it is cut at 50.
    return {"available": True, "stale": len(newly), "staleTargets": sorted(r.target for r in newly)[:50]}


def _metrics(namespace: str, state: store.StudioState, d: Draft) -> dict:
    classes = {str(c) for c in schema_subjects(d.tbox)[0]}
    return {"breaking": version_deploy.find_breaking_metrics(namespace, state.ontology_id, classes)}


# A section's ``errorCode`` for each of its fixed errors: the web app writes it in the user's language.
ERROR_CODES = {
    TABLES_ERROR: "TABLES_UNREADABLE",
    ABOX_ERROR: "ABOX_UNREADABLE",
    MAPPINGS_ERROR: "MAPPINGS_UNREADABLE",
    METRICS_ERROR: "METRICS_UNCHECKED",
    BASE_MAPPING_ERROR: "BASE_MAPPING_UNREADABLE",
}


def with_error_code(section: dict[str, Any]) -> dict[str, Any]:
    """``section`` (the structured check) with ``errorCode``, the code of its fixed ``error``, when it has one."""
    error = section.get("error")
    return {**section, "errorCode": ERROR_CODES[error]} if error else section


def _section(what: str, error: str, run: Any) -> dict[str, Any]:
    try:
        result: dict[str, Any] = run()
    except Exception:  # noqa: BLE001 - one dependency down must not hide the other checks
        _log.warning("studio checks: the %s section failed", what, exc_info=True)
        return {"error": error, "errorCode": ERROR_CODES[error]}
    return {**result, "error": None}


def data_checks(
    namespace: str, proposal_id: str, item: dict[str, Any], state: store.StudioState, rev: store.Revision
) -> dict[str, Any]:
    """The draft compared with the source catalog, the domain ABox, its mappings and the governed metrics."""
    d = rev.draft
    return {
        "tables": _section("tables", TABLES_ERROR, lambda: _tables(namespace, item, state, d, rev.r2rml_nt)),
        "abox": _section("abox", ABOX_ERROR, lambda: _abox(namespace, proposal_id, state, d)),
        "mappings": _section("mappings", MAPPINGS_ERROR, lambda: _mappings(namespace, rev.tbox_nt)),
        "metrics": _section("metrics", METRICS_ERROR, lambda: _metrics(namespace, state, d)),
    }


def checks(namespace: str, proposal_id: str) -> dict[str, Any]:
    """Every check of the draft's current revision (``GET .../studio/checks``)."""
    item = store.get_proposal(namespace, proposal_id)
    state = store.require_state(item, editable=False)
    rev = store.load_revision(namespace, proposal_id, state.revision)
    return {
        "revision": state.revision,
        "instant": instant_checks(rev.draft, store.scheme(namespace, state)),
        "structured": with_error_code(structured(namespace, state, rev.draft)),
        "data": data_checks(namespace, proposal_id, item, state, rev),
    }
