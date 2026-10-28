# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pure builders for immutable ontology version snapshots.

Functions here do no I/O (importing the module still pulls in
``catalog.ingest``). Every function takes and returns strings or rdflib graphs,
so the publish contract (the same parent snapshot plus the same proposal
yields byte-identical files) is unit-testable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from coa_common.constants import VOCAB_URI
from rdflib import OWL, RDF, BNode, Graph, Literal, Namespace, URIRef
from rdflib.compare import to_canonical_graph
from rdflib.namespace import DCTERMS, SH

from coa_ontology.catalog.ingest import (
    _DERIVED_MARKERS,
    _SUPERSEDED_ANNOTATION_PREDICATES,
    _strip_dangling_bnode_objects,
    _strip_derived_markers,
    schema_subjects,
)
from coa_ontology.datasource_ids import bare_datasource_id

FORMAT_VERSION = 1
RR = Namespace("http://www.w3.org/ns/r2rml#")
# Kept in step with ``stores.neptune_db_graph.WB`` by a unit test; copied rather than
# imported so this pure module stays free of the Neptune HTTP adapter.
_WB = "https://ontology-workbench.local/vocab#"
_DATASOURCE_ID = URIRef(f"{VOCAB_URI}datasourceId")
_DIFF_KINDS = ("classes", "objectProperties", "datatypeProperties")
_HISTORY_PREDICATES = frozenset(_SUPERSEDED_ANNOTATION_PREDICATES.values())
# The R2RML predicates that lead from a map to the term maps it owns. Anything
# else (rr:parentTriplesMap, rr:class, rr:predicate, ...) points at shared or
# constant values and is never followed when a map is replaced.
_OWNED_BY_MAP = frozenset(
    {
        RR.logicalTable,
        RR.subjectMap,
        RR.predicateObjectMap,
        RR.predicateMap,
        RR.objectMap,
        RR.graphMap,
        RR.joinCondition,
    }
)
# Stands in for the root of a blank-node subtree while its key is computed, so
# two subtrees compare equal whatever their root is called.
_SUBTREE_ROOT = URIRef("urn:coa:snapshot:subtree-root")


class SnapshotValidationError(ValueError):
    """A candidate version failed a blocking publish check (bad RDF, dangling mapping)."""


def parse_turtle(text: str | None, *, what: str = "Turtle") -> Graph:
    """Parse Turtle or N-Triples; an empty or missing payload yields an empty graph."""
    g = Graph()
    if text and text.strip():
        try:
            g.parse(data=text, format="turtle")
        except Exception as e:  # noqa: BLE001 (any rdflib parse failure means a bad payload)
            raise SnapshotValidationError(f"Invalid {what}: {e}") from e
    return g


def _term_key(term: Any) -> str:
    """Return a string for a non-blank term that does not depend on the rdflib version (it is hashed)."""
    if isinstance(term, Literal):
        lexical = json.dumps(str(term))
        if term.language:
            return f"{lexical}@{term.language}"
        return f"{lexical}^^<{term.datatype}>" if term.datatype else lexical
    return f"<{term}>"


def _label_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _relabel_pure_trees(g: Graph) -> tuple[Graph, list[tuple[Any, Any, Any]], bool]:
    """Split ``g`` into the triples outside pure blank-node trees and the relabeled triples inside them.

    A pure tree is a blank node whose only parent is a named subject, or that has no
    parent at all (an ownerless axiom such as ``owl:AllDisjointClasses``), and whose
    descendants each have exactly one parent (so no sharing and no cycles). The
    inducers' R2RML is made of them: a blank ``rr:logicalTable`` or ``rr:joinCondition``
    under a named map. Each tree node gets a label hashed from its content, so
    canonicalizing them costs a hash instead of rdflib's search, which is superlinear
    in the number of blank nodes.

    A root's label also hashes its owner and predicate, and a child's hashes its
    parent's label and predicate, so equal trees in different places stay different
    nodes. Equal trees in the *same* place (including two ownerless ones) get the same
    label and collapse into one (they are isomorphic duplicates; ``merge_ontology``
    already removes those). A node's entries are deduplicated before hashing, so the
    collapse inside a blank parent does not change the parent's label and
    canonicalizing the output again gives the same bytes.

    Returns ``(rest, trees, rest_has_blank_nodes)``. Nothing in ``rest`` links to a
    tree node, so ``rest`` can be canonicalized on its own.
    """
    indegree: dict[BNode, int] = {}
    owner: dict[BNode, tuple[Any, Any]] = {}
    outgoing: dict[BNode, list[tuple[Any, Any]]] = {}
    for s, p, o in g:
        if isinstance(o, BNode):
            indegree[o] = indegree.get(o, 0) + 1
            owner[o] = (s, p)
        if isinstance(s, BNode):
            outgoing.setdefault(s, []).append((p, o))

    # Roots: blank nodes owned by exactly one named subject, and ownerless ones.
    roots = [root for root, count in indegree.items() if count == 1 and isinstance(owner[root][0], URIRef)]
    roots.extend(node for node in outgoing if node not in indegree)

    trees: list[tuple[Any, Any, Any]] = []
    tree_nodes: set[BNode] = set()
    for root in roots:
        # Parents before children. Every node reached has one parent, which also rules out cycles.
        order = [root]
        pure = True
        for node in order:
            for _, o in outgoing.get(node, ()):
                if isinstance(o, BNode):
                    if indegree[o] != 1:
                        pure = False
                        break
                    order.append(o)
            if not pure:
                break
        if not pure:
            continue

        content: dict[BNode, str] = {}
        for node in reversed(order):
            entries = sorted(
                {
                    f"{_term_key(p)} _:{content[o]}" if isinstance(o, BNode) else f"{_term_key(p)} {_term_key(o)}"
                    for p, o in outgoing.get(node, ())
                }
            )
            content[node] = _label_hash(*entries)
        owned_by = owner.get(root)
        owner_key, predicate_key = (_term_key(owned_by[0]), _term_key(owned_by[1])) if owned_by else ("", "")
        label = {root: _label_hash("root", owner_key, predicate_key, content[root])}
        for node in order:
            for p, o in outgoing.get(node, ()):
                if isinstance(o, BNode):
                    label[o] = _label_hash("child", label[node], _term_key(p), content[o])
        if owned_by:
            trees.append((owned_by[0], owned_by[1], BNode("h" + label[root][:32])))
        for node in order:
            for p, o in outgoing.get(node, ()):
                target = BNode("h" + label[o][:32]) if isinstance(o, BNode) else o
                trees.append((BNode("h" + label[node][:32]), p, target))
        tree_nodes.update(order)

    if not tree_nodes:
        return g, trees, bool(indegree) or bool(outgoing)
    rest = Graph()
    rest_has_blank_nodes = False
    for s, p, o in g:
        if s in tree_nodes or o in tree_nodes:
            continue
        rest.add((s, p, o))
        rest_has_blank_nodes = rest_has_blank_nodes or isinstance(s, BNode) or isinstance(o, BNode)
    return rest, trees, rest_has_blank_nodes


def canonical_nt(g: Graph) -> str:
    """Serialize ``g`` deterministically as sorted N-Triples with canonical blank-node labels.

    Blank nodes that form pure trees are labeled ``_:h<hex>`` from a hash of their
    content and position (see :func:`_relabel_pure_trees`); the few that are shared or
    cyclic go through rdflib's canonicalization (``_:cb<n>``), skipped when there are
    none. Identical trees under the same subject and predicate collapse into one.

    N-Triples is a subset of Turtle, so the result loads wherever a ``.ttl`` does
    (Neptune GSP, Ontop, rdflib).
    """
    if len(g) == 0:
        return ""
    rest, trees, rest_has_blank_nodes = _relabel_pure_trees(g)
    nt = (to_canonical_graph(rest) if rest_has_blank_nodes else rest).serialize(format="nt")
    if trees:
        tree_graph = Graph()
        for triple in trees:
            tree_graph.add(triple)
        nt += "\n" + tree_graph.serialize(format="nt")
    # Split on "\n" only: str.splitlines() also breaks on U+2028, U+0085 and others
    # that N-Triples leaves unescaped inside a literal.
    return "\n".join(sorted(line for line in nt.split("\n") if line.strip())) + "\n"


def sha256_hex(text: str) -> str:
    """Return the hex SHA-256 of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dumps(obj: Any) -> str:
    """Serialize ``obj`` as stable JSON (sorted keys, trailing newline)."""
    return json.dumps(obj, sort_keys=True, indent=2) + "\n"


def clean_incoming(turtle: str) -> Graph:
    """Parse a proposal's ontology Turtle and drop what is never stored verbatim.

    ``coa:isMapped``, ``coa:physicalName``, ``coa:mappedDomain`` and ``coa:mappedRange``
    are trust-boundary markers derived from R2RML at deploy time (see
    ``catalog/ingest.py``), empty blank-node objects
    (``rdfs:subClassOf [ ]``) are rejected by Neptune's GSP parser, and the ontology node's
    ``dct:created`` / ``dct:modified`` are stamped by deploy's ``create_ontology``.
    """
    g = parse_turtle(turtle, what="ontology Turtle")
    if len(g) == 0:
        raise SnapshotValidationError("Proposal has no ontology triples")
    _strip_derived_markers(g)
    _strip_dangling_bnode_objects(g)
    _drop_values(g, g.subjects(RDF.type, OWL.Ontology), (DCTERMS.created, DCTERMS.modified))
    return g


def _drop_values(g: Graph, subjects: Iterable[Any], predicates: tuple[URIRef, ...]) -> None:
    """Remove every ``(subject, predicate, *)`` triple for the given subjects and predicates."""
    for subject in list(subjects):
        for predicate in predicates:
            g.remove((subject, predicate, None))


def _copy(g: Graph) -> Graph:
    out = Graph()
    for triple in g:
        out.add(triple)
    return out


def _bnode_closure(g: Graph, root: BNode) -> set[BNode]:
    """Return ``root`` and every blank node reachable from it through object links."""
    seen = {root}
    pending = [root]
    while pending:
        node = pending.pop()
        for o in g.objects(node):
            if isinstance(o, BNode) and o not in seen:
                seen.add(o)
                pending.append(o)
    return seen


def _subtree_key(g: Graph, root: BNode) -> str:
    """Return a label-independent key for the blank-node subtree rooted at ``root``.

    Two subtrees get the same key exactly when they are isomorphic, so a
    restriction that every re-induction re-emits under a fresh blank node is
    recognised as the one already there. The union of two parses cannot do
    that: it has no way to equate blank nodes.
    """
    sub = Graph()
    for node in _bnode_closure(g, root):
        for p, o in g.predicate_objects(node):
            sub.add((_SUBTREE_ROOT if node == root else node, p, _SUBTREE_ROOT if o == root else o))
    return canonical_nt(sub)


def _tree_key(g: Graph, root: BNode) -> str | None:
    """Return ``root``'s subtree key, or ``None`` unless it is a self-contained tree.

    The key only sees edges leaving the subtree. A node that something outside also
    links to (or a root with several parents) makes the subtree look like a copy of
    another while dropping it would cut that outside link, and which copy gets
    dropped would then depend on iteration order.
    """
    nodes = _bnode_closure(g, root)
    for node in nodes:
        outside = [s for s in g.subjects(None, node) if s not in nodes]
        if len(outside) > (1 if node == root else 0):
            return None
    return _subtree_key(g, root)


def _drop_orphan_subtree(g: Graph, root: Any) -> None:
    """Remove the blank-node subtree under ``root`` that nothing outside it links to.

    Blank nodes still linked from outside the subtree, and everything they reach,
    stay. Cycles are handled: the subtree is collected first, not unwound edge by edge.
    """
    if not isinstance(root, BNode):
        return
    nodes = _bnode_closure(g, root)
    kept: set[BNode] = set()
    for node in nodes:
        if any(s not in nodes for s in g.subjects(None, node)):
            kept |= _bnode_closure(g, node)
    for node in nodes - kept:
        g.remove((node, None, None))


def _dedupe_bnode_subtrees(g: Graph) -> None:
    """Keep one blank-node object per distinct self-contained subtree for every subject and predicate.

    Repeats until nothing changes: collapsing duplicates inside a subtree can
    make two outer subtrees isomorphic that were not before. Subtrees that share
    nodes with the rest of the graph are left alone.
    """
    changed = True
    while changed:
        changed = False
        for s, p in {(s, p) for s, p, o in g if isinstance(o, BNode)}:
            bnodes = [o for o in g.objects(s, p) if isinstance(o, BNode)]
            if len(bnodes) < 2:
                continue
            kept: set[str] = set()
            for o in bnodes:
                key = _tree_key(g, o)
                if key is None:
                    continue
                if key in kept:
                    g.remove((s, p, o))
                    _drop_orphan_subtree(g, o)
                    changed = True
                else:
                    kept.add(key)


def merge_ontology(parent: Graph, incoming: Graph) -> Graph:
    """Merge ``incoming`` onto ``parent`` with the append-accept rule.

    For every schema subject ``incoming`` defines, the parent's live annotation
    values move to their history predicate (``_SUPERSEDED_ANNOTATION_PREDICATES``,
    the map append-mode ingest applies in Neptune), unless ``incoming`` asserts
    the same value again, which then simply stays live. Everything else is a set
    union, so relationship axioms contributed by earlier proposals survive;
    isomorphic blank-node axioms are collapsed to one.
    """
    merged = _copy(parent)
    classes, obj_props, dt_props = schema_subjects(incoming)
    for subject in classes | obj_props | dt_props:
        for live, history in _SUPERSEDED_ANNOTATION_PREDICATES.items():
            for value in list(merged.objects(subject, live)):
                if (subject, live, value) in incoming:
                    continue
                merged.remove((subject, live, value))
                merged.add((subject, history, value))
    for triple in incoming:
        merged.add(triple)
    _dedupe_bnode_subtrees(merged)
    return merged


def _owned_closure(g: Graph, roots: Iterable[Any]) -> set[Any]:
    """Return ``roots`` plus every term map they own, following only ``_OWNED_BY_MAP`` links."""
    seen: set[Any] = set()
    pending = list(roots)
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        pending.extend(o for p, o in g.predicate_objects(node) if p in _OWNED_BY_MAP)
    return seen


def _triples_maps(g: Graph) -> set[Any]:
    """Return the TriplesMaps of ``g``; a subject with an ``rr:logicalTable`` counts even if untyped."""
    return set(g.subjects(RDF.type, RR.TriplesMap)) | set(g.subjects(RR.logicalTable, None))


def _datasources(g: Graph, tmap: Any) -> set[str]:
    """Return the bare ``coa:datasourceId`` values of ``tmap`` (``DS#`` / ``SRC#`` stripped)."""
    return {bare_datasource_id(str(o)) for o in g.objects(tmap, _DATASOURCE_ID)}


def merge_r2rml(parent: Graph, incoming: Graph, *, authoritative_datasources: Iterable[str] = ()) -> Graph:
    """Merge R2RML so a TriplesMap in ``incoming`` replaces the parent's map with the same IRI.

    ``authoritative_datasources`` are the ids (``DS#`` / ``SRC#`` prefixes ignored) of
    datasources whose induction is known to be complete. Every parent map carrying
    one of those ``coa:datasourceId`` values is replaced whether or not ``incoming``
    has a map for it, so a table dropped from a re-induced datasource loses its
    mapping. Any other datasource keeps the parent's maps that ``incoming`` does not
    redefine, because a partial induction (RIGOR maps only novel tables and drops
    tables that fail to parse) omits tables without removing them. Maps with no
    ``coa:datasourceId`` are only ever replaced by IRI. Ontology classes are not
    removed by any of this: they are unioned, and removing a class is a later designer
    concern.

    Replacing rather than unioning keeps a re-induced datasource from leaving two
    subject or logical-table maps on one TriplesMap, which Ontop rejects. The
    inducers name the SubjectMap, PredicateObjectMaps and ObjectMaps with IRIs, so
    the term maps a replaced TriplesMap owns go with it whether named or blank.

    A term map that something outside the replaced maps still uses (a named logical
    table shared with an untouched map) stays, with whatever it owns, unless
    ``incoming`` defines it itself, in which case ``incoming`` wins.
    """
    merged = _copy(parent)
    authoritative = {bare_datasource_id(ds) for ds in authoritative_datasources}
    maps = _triples_maps(incoming) | {
        tmap for tmap in _triples_maps(merged) if authoritative & _datasources(merged, tmap)
    }
    owned = _owned_closure(merged, maps)
    redefined = set(incoming.subjects())
    shared = {
        node
        for node in owned - maps
        if node not in redefined and any(s not in owned for s in merged.subjects(None, node))
    }
    for node in (owned - _owned_closure(merged, shared)) | maps:
        merged.remove((node, None, None))
    for triple in incoming:
        merged.add(triple)
    return merged


def _mentions(g: Graph, term: Any) -> bool:
    """Whether ``term`` appears in ``g`` as a subject, predicate or object."""
    return (term, None, None) in g or (None, term, None) in g or (None, None, term) in g


def _terms_by_map(r2rml: Graph) -> dict[Any, set[Any]]:
    """Map each TriplesMap to its ``rr:class`` values, and each named PredicateObjectMap to its predicates."""
    out: dict[Any, set[Any]] = {}
    for tmap in _triples_maps(r2rml):
        out[tmap] = {c for sm in r2rml.objects(tmap, RR.subjectMap) for c in r2rml.objects(sm, RR["class"])}
    for pom in set(r2rml.objects(None, RR.predicateObjectMap)):
        if isinstance(pom, URIRef):
            out.setdefault(pom, set()).update(r2rml.objects(pom, RR.predicate))
    return out


def rename_candidates(
    parent_ontology: Graph, parent_r2rml: Graph, incoming_ontology: Graph, incoming_r2rml: Graph
) -> dict[URIRef, URIRef]:
    """Return the IRIs a re-induction names where the parent names another term, each mapped to that term.

    An IRI change in the Ontology designer renames a class or property in every triple
    but keeps the TriplesMap and PredicateObjectMap IRIs, which the inducers mint from
    the table and column. A re-induction of the same table names the old IRI again in
    those maps, and merging it as it is brings the old class back next to the renamed
    one, which then has no mapping. ``old`` is a candidate for ``new`` when every map
    naming ``new`` in the parent (the ``rr:class`` of a TriplesMap, the ``rr:predicate``
    of a PredicateObjectMap) is a map the incoming mapping redefines with ``old`` alone,
    ``old`` is nowhere in the parent ontology and ``new`` is nowhere in the incoming one.
    A class the parent also maps from a table the re-induction leaves out (a merge, or a
    partial re-induction) is therefore left alone.

    The maps alone cannot tell a designer rename from an inducer that names a term
    afresh: RIGOR's LLM picks the property IRIs, so two runs over one table can differ
    under the same maps. A publish keeps only the candidates whose old IRI a version of
    its lineage removed (``publish._kept_renames``).
    """
    incoming_terms = _terms_by_map(incoming_r2rml)
    maps_naming: dict[Any, set[Any]] = {}
    for map_node, terms in _terms_by_map(parent_r2rml).items():
        for term in terms:
            maps_naming.setdefault(term, set()).add(map_node)
    candidates: dict[URIRef, set[URIRef]] = {}
    for new, maps in maps_naming.items():
        olds = {old for m in maps for old in incoming_terms.get(m, ())}
        if not isinstance(new, URIRef) or len(olds) != 1 or any(len(incoming_terms.get(m, ())) != 1 for m in maps):
            continue
        old = olds.pop()
        if (
            old != new
            and isinstance(old, URIRef)
            and not _mentions(parent_ontology, old)
            and not _mentions(incoming_ontology, new)
        ):
            candidates.setdefault(old, set()).add(new)
    # An old IRI two parent terms claim is ambiguous; it is merged as it is.
    return {old: news.pop() for old, news in candidates.items() if len(news) == 1}


def rename_term(g: Graph, old: URIRef, new: URIRef) -> None:
    """Replace ``old`` by ``new`` in every triple of ``g``, in any position."""
    hits = set(g.triples((old, None, None))) | set(g.triples((None, old, None))) | set(g.triples((None, None, old)))
    for s, p, o in hits:
        g.remove((s, p, o))
        g.add((new if s == old else s, new if p == old else p, new if o == old else o))


def merge_shapes(parent: Graph, incoming: Graph) -> Graph:
    """Merge SHACL so a NodeShape in ``incoming`` replaces the parent's shape with the same IRI.

    The replaced shape's triples go with the blank-node property shapes (and lists)
    it owns, so a re-induction that changes a column's constraint leaves one
    constraint, not both. A blank node something else still links to stays. Parent
    shapes ``incoming`` does not redefine are kept, as classes are by ``merge_ontology``.
    """
    merged = _copy(parent)
    for shape in set(incoming.subjects(RDF.type, SH.NodeShape)):
        owned = [o for o in merged.objects(shape) if isinstance(o, BNode)]
        merged.remove((shape, None, None))
        for node in owned:
            _drop_orphan_subtree(merged, node)
    for triple in incoming:
        merged.add(triple)
    return merged


def prune_shapes(shapes: Graph, ontology: Graph) -> Graph:
    """Return ``shapes`` without what targets a class or property ``ontology`` no longer has.

    A NodeShape loses each ``sh:targetClass`` that is not a class of ``ontology`` and goes whole
    when none is left; a property shape whose ``sh:path`` is a named property ``ontology`` lacks
    goes with the blank nodes it owns. Shapes that target nothing by class are kept as they are.
    """
    pruned = _copy(shapes)
    classes, obj_props, dt_props = schema_subjects(ontology)
    props = obj_props | dt_props
    for shape in sorted(set(pruned.subjects(RDF.type, SH.NodeShape)), key=str):
        targets = list(pruned.objects(shape, SH.targetClass))
        missing = [t for t in targets if t not in classes]
        if targets and len(missing) == len(targets):
            owned = [o for o in pruned.objects(shape) if isinstance(o, BNode)]
            pruned.remove((shape, None, None))
            for node in owned:
                _drop_orphan_subtree(pruned, node)
            continue
        for t in missing:
            pruned.remove((shape, SH.targetClass, t))
        for prop_shape in list(pruned.objects(shape, SH.property)):
            paths = [p for p in pruned.objects(prop_shape, SH.path) if isinstance(p, URIRef)]
            if paths and all(p not in props for p in paths):
                pruned.remove((shape, SH.property, prop_shape))
                _drop_orphan_subtree(pruned, prop_shape)
    return pruned


def _missing_parent_maps(r2rml: Graph) -> dict[Any, set[Any]]:
    """Map each ``rr:parentTriplesMap`` target that is not a map of ``r2rml`` to the maps referring to it."""
    maps = _triples_maps(r2rml)
    owners: dict[Any, set[Any]] = {}
    for tmap in maps:
        for node in _owned_closure(r2rml, [tmap]):
            owners.setdefault(node, set()).add(tmap)
    missing: dict[Any, set[Any]] = {}
    for term_map, _, parent in r2rml.triples((None, RR.parentTriplesMap, None)):
        if parent not in maps:
            missing.setdefault(parent, set()).update(owners.get(term_map, {term_map}))
    return missing


def _describe_map(g: Graph | None, tmap: Any) -> str:
    """Return ``tmap`` with its datasource ids as ``g`` records them, when it does."""
    datasources = sorted(_datasources(g, tmap)) if g is not None else []
    return f"{tmap} (datasource {', '.join(datasources)})" if datasources else str(tmap)


def check_r2rml(ontology: Graph, r2rml: Graph, *, previous: Graph | None = None) -> None:
    """Raise unless the mapping is consistent with itself and with ``ontology``.

    Every ``rr:class`` the mapping targets must be a named class of the ontology,
    and every ``rr:parentTriplesMap`` must name a TriplesMap that is in the mapping
    (a per-datasource replacement can drop a map another datasource joins to, and
    Ontop rejects a join to a map that does not exist). Both problems are reported
    in one error. ``previous`` is the mapping before the merge; it lets the error
    name the datasource of a map that was dropped.
    """
    problems: list[str] = []
    classes, _, _ = schema_subjects(ontology)
    missing = sorted({str(c) for c in r2rml.objects(None, RR["class"]) if c not in classes})
    if missing:
        problems.append(f"R2RML maps {len(missing)} class(es) missing from the ontology: {missing[:10]}")
    parents = _missing_parent_maps(r2rml)
    if parents:
        previous_maps = _triples_maps(previous) if previous is not None else set()
        joins = []
        for parent, referrers in sorted(parents.items(), key=lambda item: str(item[0])):
            state = "is no longer mapped" if parent in previous_maps else "is not mapped"
            for referrer in sorted(referrers, key=str):
                ids = sorted(_datasources(r2rml, referrer))
                fix = f"re-induce {', '.join(ids) if ids else 'its datasource'} or keep that table mapped"
                joins.append(
                    f"{_describe_map(previous, parent)} {state} but {_describe_map(r2rml, referrer)} joins to it; {fix}"
                )
        problems.append(f"R2RML joins to {len(parents)} parent map(s) missing from the mapping: {joins[:10]}")
    if problems:
        raise SnapshotValidationError("; ".join(problems))


def stamp_version(g: Graph, ontology_id: str, version: int) -> None:
    """Set ``owl:versionInfo "v{n}"`` on the ontology node; VKG reports it on /health.

    Every other ``owl:versionInfo`` in the graph goes too (an imported ontology's,
    say): VKG reads the first one it finds, so ours must be the only one.
    """
    subject = URIRef(ontology_id)
    g.remove((None, OWL.versionInfo, None))
    g.add((subject, RDF.type, OWL.Ontology))
    g.add((subject, OWL.versionInfo, Literal(f"v{version}")))


def strip_projection(g: Graph, ontology_id: str) -> None:
    """Remove the triples deploy re-derives, leaving the ontology as authored.

    Used when bootstrapping v1 from a live Neptune graph, which also holds the
    workbench projection (``wb:*``), the markers deploy derives from the mapping
    (``coa:isMapped``, ``coa:physicalName``, ``coa:mappedDomain``, ``coa:mappedRange``)
    and the ontology node's ``dct:created`` / ``dct:modified`` timestamps. The legacy
    accept path and deploy's ``create_ontology`` both write ``dct:title`` and
    ``dct:description`` there too, so those go as well or their values would pile
    up across versions.

    Other projection leftovers that deploy re-derives identically (``rdf:type
    rdf:Property`` on properties, ``rdf:type owl:Class``, ``rdfs:comment``
    copies) are harmless and kept.
    """
    for s, p, o in list(g):
        if str(p).startswith(_WB) or p in _DERIVED_MARKERS:
            g.remove((s, p, o))
    _drop_values(g, [URIRef(ontology_id)], (DCTERMS.created, DCTERMS.modified, DCTERMS.title, DCTERMS.description))


def _facts(g: Graph, subject: URIRef) -> set[tuple[Any, Any]]:
    """Return the live ``(predicate, object)`` facts of ``subject`` that a diff compares.

    History annotations are bookkeeping, not content. A blank-node object stands
    for its subtree, so a change inside a restriction shows up as a change.
    """
    return {
        (p, ("subtree", _subtree_key(g, o)) if isinstance(o, BNode) else o)
        for p, o in g.predicate_objects(subject)
        if p not in _HISTORY_PREDICATES
    }


def compute_diff(parent: Graph | None, child: Graph) -> dict[str, dict[str, list[str]]]:
    """Return added, removed and changed schema IRIs per kind, from ``parent`` to ``child``."""
    empty: tuple[set[URIRef], set[URIRef], set[URIRef]] = (set(), set(), set())
    before = schema_subjects(parent) if parent is not None else empty
    after = schema_subjects(child)
    diff: dict[str, dict[str, list[str]]] = {}
    for kind, old, new in zip(_DIFF_KINDS, before, after, strict=True):
        changed = [str(s) for s in old & new if parent is not None and _facts(parent, s) != _facts(child, s)]
        diff[kind] = {
            "added": sorted(str(s) for s in new - old),
            "removed": sorted(str(s) for s in old - new),
            "changed": sorted(changed),
        }
    return diff


def diff_summary(diff: dict[str, dict[str, list[str]]]) -> dict[str, dict[str, int]]:
    """Collapse a diff to counts per kind and change type."""
    return {kind: {change: len(iris) for change, iris in entry.items()} for kind, entry in diff.items()}


def build_manifest(
    *,
    namespace: str,
    version: int,
    parent_version: int | None,
    source: str,
    source_proposal_id: str | None,
    source_proposals: list[str],
    ontology_id: str,
    created_by: str,
    created_at: str,
    triple_count: int,
    files: dict[str, str],
) -> dict[str, Any]:
    """Describe one snapshot; per-file SHA-256 makes its content verifiable."""
    return {
        "formatVersion": FORMAT_VERSION,
        "namespace": namespace,
        "version": version,
        "parentVersion": parent_version,
        "source": source,
        "sourceProposalId": source_proposal_id,
        "sourceProposals": source_proposals,
        "ontologyId": ontology_id,
        "createdBy": created_by,
        "createdAt": created_at,
        "tripleCount": triple_count,
        "files": {
            name: {"sha256": sha256_hex(body), "bytes": len(body.encode("utf-8"))}
            for name, body in sorted(files.items())
        },
    }
