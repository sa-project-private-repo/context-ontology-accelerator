# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Typed change operations on an Ontology designer draft.

A draft is two rdflib graphs: the TBox and its R2RML mapping. Every op checks its inputs
against the draft first and raises :class:`OpError` (nothing changed) when it cannot apply,
then edits both graphs so the mapping never names a term the TBox lost. ``restore_triples``
is the internal inverse: it puts back exact canonical N-Triples lines and is never accepted
from a client.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from coa_common.constants import split_sql_ident_path
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from rdflib import OWL, RDF, RDFS, BNode, Graph, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.namespace import SKOS
from rdflib.term import Node

from coa_ontology.abox.model import iri_tail, norm
from coa_ontology.datatype_canonicalizer import _canonical_for
from coa_ontology.versions import snapshot

from . import iris
from .iris import IriError, IriScheme
from .messages import Message, english, message

RR = snapshot.RR
CLASS = "class"
OBJECT_PROPERTY = "objectProperty"
DATATYPE_PROPERTY = "datatypeProperty"
PROPERTY_KINDS = (OBJECT_PROPERTY, DATATYPE_PROPERTY)
MAX_LABEL = 200
MAX_COMMENT = 4000
_MATCH_PREDICATES = (SKOS.exactMatch, SKOS.closeMatch, SKOS.broadMatch, SKOS.narrowMatch, SKOS.relatedMatch)
# The edges a grounding override replaces (class_grounding._GROUNDING_PREDICATES).
GROUNDING_PREDICATES = (RDFS.subClassOf, *_MATCH_PREDICATES)
_LANG = re.compile(r"^[A-Za-z]{1,8}(-[A-Za-z0-9]{1,8})*$")


class OpError(ValueError):
    """An op cannot be applied to the draft as it is; the draft is unchanged."""


@dataclass
class Draft:
    """The TBox and the R2RML mapping a designer draft edits together."""

    tbox: Graph
    r2rml: Graph

    def copy(self) -> Draft:
        """Return an independent copy (ops mutate the draft they are given)."""
        return Draft(snapshot._copy(self.tbox), snapshot._copy(self.r2rml))


_Triple = tuple[Node, Node, Node]


@dataclass
class Context:
    """What an op needs besides the draft, and what it reports back.

    ``warning_messages`` gives each line of ``warnings`` as a code and its parameters, in the same order
    (:meth:`warn` adds both).
    """

    scheme: IriScheme
    warnings: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    labelled: list[URIRef] = field(default_factory=list)
    warning_messages: list[Message] = field(default_factory=list)
    # The history line of each op, in op order (a code and its parameters; ``english(m)`` is its English), naming
    # terms by their labels in the draft before the op.
    summary_messages: list[Message] = field(default_factory=list)
    # A domain or range ``delete_class`` gave back (``_back_on_mapped_classes``): its warning and the triples it
    # added. ``apply_ops`` reports the warning only if a later op of the change set did not take them away.
    given_back: list[tuple[Message, list[_Triple]]] = field(default_factory=list)

    def warn(self, m: Message) -> None:
        """Report the warning ``m``: its English line goes to ``warnings``, its code to ``warning_messages``."""
        self.warnings.append(english(m))
        self.warning_messages.append(m)


# ── Graph helpers ───────────────────────────────────────────────────────────


def kind_of(g: Graph, term: Node) -> str | None:
    """``class``, ``objectProperty``, ``datatypeProperty`` or None for anything else."""
    if (term, RDF.type, OWL.Class) in g or (term, RDF.type, RDFS.Class) in g:
        return CLASS
    if (term, RDF.type, OWL.DatatypeProperty) in g:
        return DATATYPE_PROPERTY
    if (term, RDF.type, OWL.ObjectProperty) in g:
        return OBJECT_PROPERTY
    return None


def label_of(g: Graph, term: Node) -> str:
    """The term's ``rdfs:label`` (else ``skos:prefLabel``, else its IRI tail), smallest first."""
    for pred in (RDFS.label, SKOS.prefLabel):
        values = sorted(str(o) for o in g.objects(term, pred))
        if values:
            return values[0]
    return iri_tail(str(term)) or str(term)


def mentioned(d: Draft, iri: str) -> bool:
    """Whether ``iri`` appears anywhere in the draft, in any position of either graph."""
    t = URIRef(iri)
    return any((t, None, None) in g or (None, t, None) in g or (None, None, t) in g for g in (d.tbox, d.r2rml))


def named_parents(g: Graph, c: Node) -> list[URIRef]:
    """The named ``rdfs:subClassOf`` objects of ``c``, sorted."""
    return sorted((p for p in g.objects(c, RDFS.subClassOf) if isinstance(p, URIRef) and p != c), key=str)


def ancestors(g: Graph, c: Node) -> set[URIRef]:
    """Every named class reachable from ``c`` through ``rdfs:subClassOf`` (``c`` itself only on a cycle)."""
    seen: set[URIRef] = set()
    stack = list(named_parents(g, c))
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(named_parents(g, n))
    return seen


def _check_acyclic(g: Graph, child: URIRef, parent: URIRef) -> None:
    if child == parent or child in ancestors(g, parent):
        raise OpError(f"{iri_tail(str(child))} subClassOf {iri_tail(str(parent))} would make a subClassOf cycle")


# What the console calls each kind, for the messages and the assistant's tool results that name one.
KIND_WORDS = {CLASS: "class", OBJECT_PROPERTY: "relationship", DATATYPE_PROPERTY: "attribute"}


def _term(ctx: Context, g: Graph, value: str, what: str, kinds: Sequence[str]) -> URIRef:
    """A term of the draft in the namespace's IRI space, of one of ``kinds``."""
    iris.check_in_space(ctx.scheme, value, what)
    t = URIRef(value)
    if kind_of(g, t) not in kinds:
        words = [KIND_WORDS[k] for k in kinds]
        named = words[0] if len(words) == 1 else f"{', '.join(words[:-1])} or {words[-1]}"
        raise OpError(f"{what} {value} is not a {named} of the draft")
    return t


def _class(g: Graph, value: str, what: str) -> URIRef:
    """A class of the draft, wherever its IRI lives (a parent, domain or range)."""
    iris.check_iri(value, what)
    t = URIRef(value)
    if kind_of(g, t) != CLASS:
        raise OpError(f"{what} {value} is not a class of the draft")
    return t


def _clean_label(label: str) -> str:
    text = " ".join(label.split())
    if not iris.has_letter_or_digit(text):
        raise OpError("A label needs at least one letter or digit")
    if len(text) > MAX_LABEL:
        raise OpError(f"A label is at most {MAX_LABEL} characters")
    return text


def _replace_label(g: Graph, t: URIRef, label: str, lang: str | None) -> None:
    if lang is not None and not _LANG.match(lang):
        raise OpError(f"{lang!r} is not a language tag")
    uses_pref = any(True for _ in g.objects(t, SKOS.prefLabel)) and not any(True for _ in g.objects(t, RDFS.label))
    pred = SKOS.prefLabel if uses_pref else RDFS.label
    for o in list(g.objects(t, pred)):
        if isinstance(o, RdfLiteral) and o.language == lang:
            g.remove((t, pred, o))
    g.add((t, pred, RdfLiteral(label, lang=lang)))


def _set_comment(g: Graph, t: URIRef, comment: str | None) -> None:
    text = (comment or "").strip()
    if len(text) > MAX_COMMENT:
        raise OpError(f"A comment is at most {MAX_COMMENT} characters")
    g.remove((t, RDFS.comment, None))
    if text:
        g.add((t, RDFS.comment, RdfLiteral(text)))


def _rename(g: Graph, old: URIRef, new: URIRef) -> None:
    hits = set(g.triples((old, None, None))) | set(g.triples((None, old, None))) | set(g.triples((None, None, old)))
    for s, p, o in hits:
        g.remove((s, p, o))
        g.add((new if s == old else s, new if p == old else p, new if o == old else o))


def _lang(value: Node) -> str:
    """The lower-cased language tag of a literal; empty for an untagged literal or a non-literal."""
    return ((value.language if isinstance(value, RdfLiteral) else None) or "").lower()


def _remove_subject(g: Graph, s: URIRef) -> None:
    """Remove every triple about ``s`` and the blank-node axioms only ``s`` owned."""
    blanks = [o for o in g.objects(s) if isinstance(o, BNode)]
    g.remove((s, None, None))
    for b in blanks:
        snapshot._drop_orphan_subtree(g, b)


def _drop_axioms_mentioning(g: Graph, term: URIRef) -> None:
    """Remove, whole, every blank-node axiom (restriction, list, union) that mentions ``term``."""
    for s, _ in list(g.subject_predicates(term)):
        if not isinstance(s, BNode):
            continue
        seen: set[Node] = set()
        pending: list[Node] = [s]
        roots: set[Node] = set()
        links: set[tuple[Node, Node, Node]] = set()
        while pending:
            n = pending.pop()
            if n in seen:
                continue
            seen.add(n)
            owners = list(g.subject_predicates(n))
            if not owners:
                roots.add(n)
            for owner, pred in owners:
                if isinstance(owner, BNode):
                    pending.append(owner)
                else:
                    links.add((owner, pred, n))
                    roots.add(n)
        for link in links:
            g.remove(link)
        for root in roots:
            snapshot._drop_orphan_subtree(g, root)


def _remove_mentions(g: Graph, term: URIRef) -> None:
    """Remove the blank-node axioms and the named triples that name ``term`` as their object."""
    _drop_axioms_mentioning(g, term)
    g.remove((None, None, term))


# ── R2RML helpers ───────────────────────────────────────────────────────────


def _drop_maps(r2rml: Graph, roots: set[Node]) -> None:
    """Remove term maps ``roots``, what they own, and the links to them; shared parts stay (as merge_r2rml)."""
    if not roots:
        return
    owned = snapshot._owned_closure(r2rml, roots)
    shared = {n for n in owned - roots if any(s not in owned for s in r2rml.subjects(None, n))}
    for node in (owned - snapshot._owned_closure(r2rml, shared)) | roots:
        r2rml.remove((node, None, None))
    for node in roots:
        r2rml.remove((None, RR.predicateObjectMap, node))


def _poms(r2rml: Graph, test: Callable[[Node, Node], bool]) -> set[Node]:
    """PredicateObjectMaps owning a term map with a ``(predicate, object)`` that passes ``test``."""
    out: set[Node] = set()
    for pom in set(r2rml.objects(None, RR.predicateObjectMap)):
        for n in snapshot._owned_closure(r2rml, [pom]):
            if any(test(p, o) for p, o in r2rml.predicate_objects(n)):
                out.add(pom)
                break
    return out


def poms_mentioning(r2rml: Graph, term: URIRef) -> set[Node]:
    """The PredicateObjectMaps that name ``term`` (``rr:predicate``, ``rr:class``, ``rr:constant``...)."""
    return _poms(r2rml, lambda p, o: o == term)


def maps_of_class(r2rml: Graph, c: URIRef) -> set[Node]:
    """The TriplesMaps whose subject map declares ``rr:class c``."""
    return {t for sm in r2rml.subjects(RR["class"], c) for t in r2rml.subjects(RR.subjectMap, sm)}


def triples_maps_of_class(r2rml: Graph, c: URIRef) -> set[Node]:
    """The nodes typed ``rr:TriplesMap`` whose subject map declares ``rr:class c``; ``c`` is mapped if any."""
    return {t for t in maps_of_class(r2rml, c) if (t, RDF.type, RR.TriplesMap) in r2rml}


def property_maps(r2rml: Graph, p: Node) -> list[tuple[Node, Node]]:
    """``(TriplesMap, PredicateObjectMap)`` for every PredicateObjectMap that maps ``p``, sorted.

    A PredicateObjectMap maps ``p`` when it names it with ``rr:predicate``, or with an ``rr:predicateMap``
    whose ``rr:constant`` is ``p``: ``p`` is mapped when there is one.
    """
    poms = set(r2rml.subjects(RR.predicate, p))
    poms |= {pom for pm in r2rml.subjects(RR.constant, p) for pom in r2rml.subjects(RR.predicateMap, pm)}
    pairs = {(t, pom) for pom in poms for t in r2rml.subjects(RR.predicateObjectMap, pom)}
    return sorted(pairs, key=lambda pair: (str(pair[0]), str(pair[1])))


def sql_name(value: Node) -> str:
    """The unquoted last segment of an ``rr:tableName`` or ``rr:column`` (``"sales"."orders"`` gives ``orders``)."""
    return split_sql_ident_path(str(value))[-1]


Table = tuple[str, tuple[str, ...]]
"""A table a TriplesMap reads: its datasource (the bare ``coa:datasourceId`` values, comma-joined; empty
when it has none) and the unquoted segments of its ``rr:tableName``."""


def map_tables(r2rml: Graph, tmap: Node) -> set[Table]:
    """The tables a TriplesMap reads.

    Two maps read the same table only when the datasource and every segment of the name agree: a bare
    name two datasources share, or one name in two schemas (``logical_table_names`` qualifies it then),
    is two tables.
    """
    source = ",".join(sorted(snapshot._datasources(r2rml, tmap)))
    return {
        (source, tuple(split_sql_ident_path(str(name))))
        for lt in r2rml.objects(tmap, RR.logicalTable)
        for name in r2rml.objects(lt, RR.tableName)
    }


def class_tables(r2rml: Graph, c: URIRef) -> set[Table]:
    """The tables the TriplesMaps of class ``c`` read."""
    return {table for t in triples_maps_of_class(r2rml, c) for table in map_tables(r2rml, t)}


def table_names(tables: Iterable[Table], among: Iterable[Table] = ()) -> list[str]:
    """The names a user reads for ``tables``, sorted.

    Each is the dotted name, followed by the datasource in brackets when another table of ``tables`` or
    ``among`` has that name in another datasource.
    """
    named = set(tables)
    every = named | set(among)
    return sorted(
        ".".join(path) + (f" [{source}]" if source and any(p == path and s != source for s, p in every) else "")
        for source, path in named
    )


def _delete_class_maps(r2rml: Graph, c: URIRef) -> None:
    doomed: set[Node] = set()
    for sm in set(r2rml.subjects(RR["class"], c)):
        r2rml.remove((sm, RR["class"], c))
        if not any(True for _ in r2rml.objects(sm, RR["class"])):
            doomed |= set(r2rml.subjects(RR.subjectMap, sm))
    if doomed:
        # A join into a dropped map would fail the publish check (check_r2rml), so it goes too.
        _drop_maps(r2rml, _poms(r2rml, lambda p, o: p == RR.parentTriplesMap and o in doomed))
        _drop_maps(r2rml, doomed)
    _drop_maps(r2rml, poms_mentioning(r2rml, c))


def _delete_property(d: Draft, p: URIRef) -> None:
    _drop_maps(d.r2rml, poms_mentioning(d.r2rml, p))
    _remove_subject(d.tbox, p)
    _remove_mentions(d.tbox, p)
    d.tbox.remove((None, p, None))


def _new_term(d: Draft, ctx: Context, explicit: str | None, mint: Callable[[Callable[[str], bool]], str]) -> URIRef:
    if explicit:
        iris.check_in_space(ctx.scheme, explicit, "iri")
        if mentioned(d, explicit):
            raise OpError(f"{explicit} already exists in the draft")
        iri = explicit
    else:
        iri = mint(lambda candidate: mentioned(d, candidate))
    ctx.created.append(iri)
    return URIRef(iri)


def _tail(iri: str | None) -> str:
    return iri_tail(iri or "") or (iri or "")


def _name(g: Graph, iri: str | None) -> str:
    """How a history line names the term ``iri``: by its label in ``g``, the TBox before the op, else its IRI tail.

    A malformed IRI, which the op refuses, is not looked up.
    """
    try:
        return label_of(g, URIRef(iris.check_iri(iri or "", "iri")))
    except IriError:
        return _tail(iri)


# ── Ops ─────────────────────────────────────────────────────────────────────


class _Op(BaseModel):
    """Common configuration: camelCase aliases accepted, unknown fields rejected."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    def apply(self, d: Draft, ctx: Context) -> None:  # pragma: no cover - every op overrides it
        """Edit ``d`` in place or raise :class:`OpError`."""
        raise NotImplementedError

    def summary_message(self, g: Graph) -> Message:  # pragma: no cover - every op overrides it
        """The op's line of the change history, naming terms by their labels in ``g``, the TBox before the op.

        It is a code and its parameters, which the web app writes in the user's language; ``english`` gives its English.
        """
        raise NotImplementedError


class AddClass(_Op):
    """Add an ``owl:Class``; its IRI is minted from the label unless ``iri`` is given."""

    op: Literal["add_class"]
    label: str
    comment: str | None = None
    parents: list[str] = Field(default_factory=list)
    iri: str | None = None

    def apply(self, d: Draft, ctx: Context) -> None:
        """Add the class, its label, comment and parents."""
        label = _clean_label(self.label)
        parents = [_class(d.tbox, p, "parent") for p in self.parents]
        c = _new_term(d, ctx, self.iri, lambda taken: iris.mint_class_iri(ctx.scheme, label, taken))
        d.tbox.add((c, RDF.type, OWL.Class))
        d.tbox.add((c, RDFS.label, RdfLiteral(label)))
        _set_comment(d.tbox, c, self.comment)
        for p in parents:
            d.tbox.add((c, RDFS.subClassOf, p))
        ctx.labelled.append(c)

    def summary_message(self, g: Graph) -> Message:
        """Describe the op."""
        return message("ADD_CLASS", label=self.label)


class SetLabel(_Op):
    """Replace the term's label in one language (``rdfs:label``, or ``skos:prefLabel`` when that is what it uses)."""

    op: Literal["set_label"]
    iri: str
    label: str
    lang: str | None = None

    def apply(self, d: Draft, ctx: Context) -> None:
        """Replace the label; the IRI is unchanged."""
        t = _term(ctx, d.tbox, self.iri, "iri", (CLASS, *PROPERTY_KINDS))
        _replace_label(d.tbox, t, _clean_label(self.label), self.lang)
        ctx.labelled.append(t)

    def summary_message(self, g: Graph) -> Message:
        """Describe the op."""
        return message("SET_LABEL", term=_name(g, self.iri), label=self.label)


class SetComment(_Op):
    """Replace the term's ``rdfs:comment``; an empty comment removes it."""

    op: Literal["set_comment"]
    iri: str
    comment: str

    def apply(self, d: Draft, ctx: Context) -> None:
        """Replace the comment."""
        _set_comment(d.tbox, _term(ctx, d.tbox, self.iri, "iri", (CLASS, *PROPERTY_KINDS)), self.comment)

    def summary_message(self, g: Graph) -> Message:
        """Describe the op."""
        return message("SET_COMMENT", term=_name(g, self.iri))


class ChangeIri(_Op):
    """Replace a term's IRI in every triple of the TBox and the mapping."""

    op: Literal["change_iri"]
    iri: str
    new_iri: str = Field(alias="newIri")

    def apply(self, d: Draft, ctx: Context) -> None:
        """Rename the term everywhere; a new IRI already in the draft is refused."""
        old = _term(ctx, d.tbox, self.iri, "iri", (CLASS, *PROPERTY_KINDS))
        iris.check_in_space(ctx.scheme, self.new_iri, "newIri")
        if self.new_iri == self.iri:
            raise OpError("newIri is the same as iri")
        if mentioned(d, self.new_iri):
            raise OpError(f"{self.new_iri} already exists in the draft")
        new = URIRef(self.new_iri)
        _rename(d.tbox, old, new)
        _rename(d.r2rml, old, new)

    def summary_message(self, g: Graph) -> Message:
        """Describe the op."""
        return message("CHANGE_IRI", term=_name(g, self.iri), newTerm=_tail(self.new_iri))


class DeleteClass(_Op):
    """Delete a class, re-parent its subclasses, and detach or delete the properties that use it.

    A detached property that its mapping still maps, and that is left without a domain or range, gets the
    classes its mapping reads instead (``_back_on_mapped_classes``).
    """

    op: Literal["delete_class"]
    iri: str
    cascade: Literal["detach", "delete"] = "detach"

    def apply(self, d: Draft, ctx: Context) -> None:
        """Delete the class from the TBox and its maps, joins and object maps from the mapping."""
        g = d.tbox
        c = _term(ctx, g, self.iri, "iri", (CLASS,))
        parents = named_parents(g, c)
        for child in sorted((s for s in g.subjects(RDFS.subClassOf, c) if isinstance(s, URIRef) and s != c), key=str):
            g.remove((child, RDFS.subClassOf, c))
            for p in parents:
                if p != child:
                    g.add((child, RDFS.subClassOf, p))
        users = {s for s in g.subjects(RDFS.domain, c)} | {s for s in g.subjects(RDFS.range, c)}
        detached: list[tuple[URIRef, bool, bool]] = []  # (property, lost a domain, lost a range)
        for prop in sorted((u for u in users if isinstance(u, URIRef)), key=str):
            if self.cascade == "delete":
                _delete_property(d, prop)
            else:
                detached.append((prop, (prop, RDFS.domain, c) in g, (prop, RDFS.range, c) in g))
                g.remove((prop, RDFS.domain, c))
                g.remove((prop, RDFS.range, c))
        _delete_class_maps(d.r2rml, c)
        _remove_subject(g, c)
        _remove_mentions(g, c)
        for prop, domain, rng in detached:
            ctx.given_back.extend(_back_on_mapped_classes(d, prop, c, domain=domain, rng=rng))

    def summary_message(self, g: Graph) -> Message:
        """Describe the op."""
        code = "DELETE_CLASS_DETACH" if self.cascade == "detach" else "DELETE_CLASS_DELETE"
        return message(code, term=_name(g, self.iri))


class MergeClasses(_Op):
    """Move ``source`` onto ``target``: every reference and every axiom of the source becomes the target's.

    The source's labels become altLabels of the target. Its comments move only in the languages the
    target has no comment in. All its other triples (parents and grounding, restrictions,
    ``owl:equivalentClass``, ``owl:disjointWith``...) are renamed onto the target and deduplicated.
    """

    op: Literal["merge_classes"]
    source: str
    target: str

    def apply(self, d: Draft, ctx: Context) -> None:
        """Merge the classes in the TBox and the mapping."""
        g = d.tbox
        s = _term(ctx, g, self.source, "source", (CLASS,))
        t = _term(ctx, g, self.target, "target", (CLASS,))
        if s == t:
            raise OpError("source and target are the same class")
        source_tables, target_tables = class_tables(d.r2rml, s), class_tables(d.r2rml, t)
        if source_tables and target_tables and source_tables != target_tables:
            # One class keeps one NL-to-SQL document, which names one table and one datasource.
            both = source_tables | target_tables
            ctx.warn(
                message(
                    "MERGE_MAPPED_TABLES",
                    source=_tail(self.source),
                    target=_tail(self.target),
                    sourceTables=", ".join(table_names(source_tables, both)),
                    targetTables=", ".join(table_names(target_tables, both)),
                )
            )
        kept = {norm(str(o)) for pred in (RDFS.label, SKOS.prefLabel) for o in g.objects(t, pred)}
        for pred in (RDFS.label, SKOS.prefLabel, SKOS.altLabel):
            for value in list(g.objects(s, pred)):
                g.remove((s, pred, value))
                if isinstance(value, RdfLiteral) and norm(str(value)) not in kept:
                    g.add((t, SKOS.altLabel, value))
        target_langs = {_lang(o) for o in g.objects(t, RDFS.comment)}
        for comment in list(g.objects(s, RDFS.comment)):
            if _lang(comment) in target_langs:
                g.remove((s, RDFS.comment, comment))
        _rename(g, s, t)
        _rename(d.r2rml, s, t)
        for pred in (RDFS.subClassOf, OWL.equivalentClass, OWL.disjointWith, *_MATCH_PREDICATES):
            g.remove((t, pred, t))
        snapshot._dedupe_bnode_subtrees(g)
        if t in ancestors(g, t):
            raise OpError(f"Merging {_tail(self.source)} into {_tail(self.target)} would make a subClassOf cycle")

    def summary_message(self, g: Graph) -> Message:
        """Describe the op."""
        return message("MERGE_CLASSES", source=_name(g, self.source), target=_name(g, self.target))


class AddParent(_Op):
    """Add ``iri rdfs:subClassOf parent``; a cycle is refused."""

    op: Literal["add_parent"]
    iri: str
    parent: str

    def apply(self, d: Draft, ctx: Context) -> None:
        """Add the subclass edge."""
        c = _term(ctx, d.tbox, self.iri, "iri", (CLASS,))
        p = _class(d.tbox, self.parent, "parent")
        if (c, RDFS.subClassOf, p) in d.tbox:
            raise OpError(f"{_tail(self.parent)} is already a parent of {_tail(self.iri)}")
        _check_acyclic(d.tbox, c, p)
        d.tbox.add((c, RDFS.subClassOf, p))

    def summary_message(self, g: Graph) -> Message:
        """Describe the op."""
        return message("ADD_PARENT", term=_name(g, self.iri), parent=_name(g, self.parent))


class RemoveParent(_Op):
    """Remove ``iri rdfs:subClassOf parent``; refused while a mapped property's domain or range needs the edge."""

    op: Literal["remove_parent"]
    iri: str
    parent: str

    def apply(self, d: Draft, ctx: Context) -> None:
        """Remove the subclass edge."""
        c = _term(ctx, d.tbox, self.iri, "iri", (CLASS,))
        p = URIRef(iris.check_iri(self.parent, "parent"))
        if (c, RDFS.subClassOf, p) not in d.tbox:
            raise OpError(f"{_tail(self.parent)} is not a parent of {_tail(self.iri)}")
        uses = _mapped_uses_of(d, p)
        d.tbox.remove((c, RDFS.subClassOf, p))
        _keep_mapping_fit(d, uses, p, c)

    def summary_message(self, g: Graph) -> Message:
        """Describe the op."""
        return message("REMOVE_PARENT", term=_name(g, self.iri), parent=_name(g, self.parent))


def _add_property(
    d: Draft,
    ctx: Context,
    op: AddObjectProperty | AddDatatypeProperty,
    rdf_type: URIRef,
    check_range: Callable[[Graph], URIRef],
) -> None:
    """The body both property-add ops share; ``check_range`` is the one step that differs.

    ``rdf_type`` is ``owl:ObjectProperty`` or ``owl:DatatypeProperty``; its local name is also
    the ``kind`` the IRI is minted with.
    """
    label = _clean_label(op.label)
    dom = _class(d.tbox, op.domain, "domain")
    rng = check_range(d.tbox)
    kind = iri_tail(str(rdf_type))
    p = _new_term(d, ctx, op.iri, lambda taken: iris.mint_property_iri(ctx.scheme, label, kind, op.domain, taken))
    d.tbox.add((p, RDF.type, rdf_type))
    d.tbox.add((p, RDFS.label, RdfLiteral(label)))
    d.tbox.add((p, RDFS.domain, dom))
    d.tbox.add((p, RDFS.range, rng))
    _set_comment(d.tbox, p, op.comment)
    ctx.labelled.append(p)


class AddObjectProperty(_Op):
    """Add an ``owl:ObjectProperty`` from ``domain`` to ``range``."""

    op: Literal["add_object_property"]
    label: str
    domain: str
    range: str
    comment: str | None = None
    iri: str | None = None

    def apply(self, d: Draft, ctx: Context) -> None:
        """Add the property with its label, domain, range and comment."""
        _add_property(d, ctx, self, OWL.ObjectProperty, lambda g: _class(g, self.range, "range"))

    def summary_message(self, g: Graph) -> Message:
        """Describe the op."""
        return message(
            "ADD_OBJECT_PROPERTY", label=self.label, domain=_name(g, self.domain), range=_name(g, self.range)
        )


class AddDatatypeProperty(_Op):
    """Add an ``owl:DatatypeProperty`` of ``domain`` whose range is an XSD datatype."""

    op: Literal["add_datatype_property"]
    label: str
    domain: str
    range: str
    comment: str | None = None
    iri: str | None = None

    def apply(self, d: Draft, ctx: Context) -> None:
        """Add the property with its label, domain, datatype and comment."""
        _add_property(d, ctx, self, OWL.DatatypeProperty, lambda _: URIRef(iris.datatype_iri(self.range)))

    def summary_message(self, g: Graph) -> Message:
        """Describe the op."""
        return message("ADD_DATATYPE_PROPERTY", label=self.label, domain=_name(g, self.domain))


class UpdateProperty(_Op):
    """Change a property's domain, range, label or comment.

    A mapped property keeps a domain, range and datatype its mapping can serve (see ``_check_mapping_fit``).
    """

    op: Literal["update_property"]
    iri: str
    domain: str | None = None
    range: str | None = None
    label: str | None = None
    comment: str | None = None

    def apply(self, d: Draft, ctx: Context) -> None:
        """Replace the given fields."""
        g = d.tbox
        p = _term(ctx, g, self.iri, "iri", PROPERTY_KINDS)
        if self.domain is None and self.range is None and self.label is None and self.comment is None:
            raise OpError("update_property needs at least one of domain, range, label or comment")
        kind = kind_of(g, p)
        dom = _class(g, self.domain, "domain") if self.domain is not None else None
        rng = None
        if self.range is not None:
            rng = _class(g, self.range, "range") if kind == OBJECT_PROPERTY else URIRef(iris.datatype_iri(self.range))
        for warning in _check_mapping_fit(d, p, kind, dom, rng):
            ctx.warn(warning)
        if dom is not None:
            for o in list(g.objects(p, RDFS.domain)):
                g.remove((p, RDFS.domain, o))
                snapshot._drop_orphan_subtree(g, o)
            g.add((p, RDFS.domain, dom))
        if rng is not None:
            for o in list(g.objects(p, RDFS.range)):
                g.remove((p, RDFS.range, o))
                snapshot._drop_orphan_subtree(g, o)
            g.add((p, RDFS.range, rng))
        if self.label is not None:
            _replace_label(g, p, _clean_label(self.label), None)
            ctx.labelled.append(p)
        if self.comment is not None:
            _set_comment(g, p, self.comment)

    def summary_message(self, g: Graph) -> Message:
        """Describe the op. An attribute's range is its datatype, as the Inspector calls it."""
        try:
            attribute = kind_of(g, URIRef(iris.check_iri(self.iri, "iri"))) == DATATYPE_PROPERTY
        except IriError:
            attribute = False  # the op refuses it
        changed = [
            "datatype" if name == "range" and attribute else name
            for name in ("domain", "range", "label", "comment")
            if getattr(self, name) is not None
        ]
        return message("UPDATE_PROPERTY", fields=", ".join(changed), term=_name(g, self.iri))


def _map_classes(r2rml: Graph, tmap: Node) -> set[Node]:
    return {c for sm in r2rml.objects(tmap, RR.subjectMap) for c in r2rml.objects(sm, RR["class"])}


def _fits(g: Graph, term: URIRef, classes: set[Node]) -> bool:
    """Whether ``term`` is one of ``classes`` or an ancestor of one: generalising to a parent keeps a mapping valid."""
    return any(term == c or term in ancestors(g, c) for c in classes)


def _check_mapping_fit(
    d: Draft, p: URIRef, kind: str | None, domain: URIRef | None, rng: URIRef | None
) -> list[Message]:
    """Refuse a domain, range or datatype that a mapping of ``p`` contradicts; return the warnings it cannot check.

    For every PredicateObjectMap that maps ``p``, the domain must be a class of its TriplesMap or a parent
    of one, a datatype must be the object map's ``rr:datatype``, and an object range must be a class of the
    map it joins or a parent of one. A parent keeps the mapping valid, but the VKG's SPARQL context offers a
    property on its domain and a join on its range only (not on their subclasses), so a parent is a warning;
    NL-to-SQL places a mapped property by its mapping and keeps it. The target class of an IRI-template
    object map is unknown, so a range change there is only a warning.
    """
    prop = _tail(str(p))
    warnings: list[Message] = []
    for tmap, pom in property_maps(d.r2rml, p):
        classes = _map_classes(d.r2rml, tmap)
        if domain is not None and classes:
            tables = table_names(map_tables(d.r2rml, tmap))
            where = f"from table {', '.join(tables)}" if tables else f"by {_tail(str(tmap))}"
            cls = _tail(str(min(classes, key=str)))
            if not _fits(d.tbox, domain, classes):
                raise OpError(f"{prop} is mapped {where}; its domain must stay {cls} or one of its parents.")
            if domain not in classes:
                on = {"tables": ", ".join(tables)} if tables else {"map": _tail(str(tmap))}
                code = "DOMAIN_ON_PARENT_TABLE" if tables else "DOMAIN_ON_PARENT_MAP"
                warnings.append(message(code, property=prop, **on, domain=_tail(str(domain)), mappedClass=cls))
        if rng is None:
            continue
        for om in sorted(d.r2rml.objects(pom, RR.objectMap), key=str):
            if kind == OBJECT_PROPERTY:
                joined = sorted(d.r2rml.objects(om, RR.parentTriplesMap), key=str)
                for parent_map in joined:
                    targets = _map_classes(d.r2rml, parent_map)
                    if not targets:
                        continue
                    cls = _tail(str(min(targets, key=str)))
                    if not _fits(d.tbox, rng, targets):
                        raise OpError(
                            f"{prop} joins to {cls} in the mapping; its range must stay {cls} or one of its parents."
                        )
                    if rng not in targets:
                        warnings.append(
                            message("RANGE_ON_PARENT", property=prop, mappedClass=cls, range=_tail(str(rng)))
                        )
                if not joined and (om, RR.template, None) in d.r2rml:
                    warnings.append(message("RANGE_UNCONFIRMED", property=prop, range=_tail(str(rng))))
                continue
            for dt in sorted(d.r2rml.objects(om, RR.datatype), key=str):
                if dt == rng:
                    continue
                column = min((sql_name(c) for c in d.r2rml.objects(om, RR.column)), default=None)
                if column is None:
                    raise OpError(f"{prop} is mapped as {_tail(str(dt))}; its datatype follows the mapping.")
                raise OpError(
                    f"{prop} is mapped from column {column}, typed {_tail(str(dt))}; its datatype follows the column."
                )
    return [w for n, w in enumerate(warnings) if w not in warnings[:n]]  # two maps of one table give the same warning


# A domain or object range ``_check_mapping_fit`` checks: (property, kind, domain, range).
_Use = tuple[URIRef, str | None, URIRef | None, URIRef | None]


def _fits_mapping(d: Draft, use: _Use) -> bool:
    try:
        _check_mapping_fit(d, *use)
    except OpError:
        return False
    return True


def _mapped_uses_of(d: Draft, parent: URIRef) -> list[_Use]:
    """Each domain or object range of a mapped property that is ``parent`` or one of its ancestors and fits now.

    Removing an edge to ``parent`` can only take these out of a mapped class's ancestors; ``_keep_mapping_fit``
    checks them again afterwards.
    """
    g = d.tbox
    uses: list[_Use] = []
    for c in sorted({parent} | ancestors(g, parent), key=str):
        for p in sorted({s for s in g.subjects(RDFS.domain, c) if isinstance(s, URIRef)}, key=str):
            uses.append((p, kind_of(g, p), c, None))
        for p in sorted({s for s in g.subjects(RDFS.range, c) if isinstance(s, URIRef)}, key=str):
            if kind_of(g, p) == OBJECT_PROPERTY:
                uses.append((p, OBJECT_PROPERTY, None, c))
    # A use that contradicted its mapping already is not what the edit breaks.
    return [use for use in uses if property_maps(d.r2rml, use[0]) and _fits_mapping(d, use)]


def _keep_mapping_fit(d: Draft, uses: list[_Use], parent: Node, child: Node) -> None:
    """Refuse an edit that took ``parent`` out of ``child``'s ancestors while a mapped use needed it there."""
    for use in uses:
        try:
            _check_mapping_fit(d, *use)
        except OpError as e:
            raise OpError(f"{_tail(str(parent))} has to stay a parent of {_tail(str(child))}: {e}") from e


def _back_on_mapped_classes(
    d: Draft, p: URIRef, deleted: URIRef, *, domain: bool, rng: bool
) -> list[tuple[Message, list[_Triple]]]:
    """Give a mapped ``p`` that lost its last domain (or range) with class ``deleted`` the classes its mapping reads.

    That is a domain or range moved up to ``deleted``, a parent of the mapped class (``_check_mapping_fit``
    allows it). Its maps outlive the parent, so rather than leave its mapped class, ``p`` gets the classes of
    the TriplesMaps that map it as its domain, and those of the maps they join as its range; each returned
    warning says so, with the triples it added. A ``p`` whose maps went with ``deleted`` is no longer mapped
    and is left as it is.
    """
    pairs = property_maps(d.r2rml, p)
    gone, prop = _tail(str(deleted)), _tail(str(p))
    warnings: list[tuple[Message, list[_Triple]]] = []
    if domain and (p, RDFS.domain, None) not in d.tbox:
        classes = sorted({c for tmap, _ in pairs for c in _map_classes(d.r2rml, tmap)}, key=str)
        if classes:
            added: list[_Triple] = [(p, RDFS.domain, c) for c in classes]
            for triple in added:
                d.tbox.add(triple)
            tables = table_names({t for tmap, _ in pairs for t in map_tables(d.r2rml, tmap)})
            maps = sorted({_tail(str(tmap)) for tmap, _ in pairs})
            names = ", ".join(_tail(str(c)) for c in classes)
            on = {"tables": ", ".join(tables)} if tables else {"map": ", ".join(maps)}
            code = "DOMAIN_GIVEN_BACK_TABLE" if tables else "DOMAIN_GIVEN_BACK_MAP"
            warnings.append((message(code, deleted=gone, property=prop, **on, classes=names), added))
    if rng and (p, RDFS.range, None) not in d.tbox:
        joined = {
            m
            for _, pom in pairs
            for om in d.r2rml.objects(pom, RR.objectMap)
            for m in d.r2rml.objects(om, RR.parentTriplesMap)
        }
        targets = sorted({c for m in joined for c in _map_classes(d.r2rml, m)}, key=str)
        if targets:
            added = [(p, RDFS.range, c) for c in targets]
            for triple in added:
                d.tbox.add(triple)
            names = ", ".join(_tail(str(c)) for c in targets)
            warnings.append((message("RANGE_GIVEN_BACK", deleted=gone, property=prop, classes=names), added))
    return warnings


class DeleteProperty(_Op):
    """Delete a property and the PredicateObjectMaps that map it."""

    op: Literal["delete_property"]
    iri: str

    def apply(self, d: Draft, ctx: Context) -> None:
        """Delete the property everywhere."""
        _delete_property(d, _term(ctx, d.tbox, self.iri, "iri", PROPERTY_KINDS))

    def summary_message(self, g: Graph) -> Message:
        """Describe the op."""
        return message("DELETE_PROPERTY", term=_name(g, self.iri))


class SetMatch(_Op):
    """Set a class's reference-ontology grounding, with the meaning of a grounding override.

    The class's grounding edges to terms outside the namespace (``rdfs:subClassOf`` and the SKOS
    match predicates) and its ``matchConfidence`` go; ``exact`` and ``close`` then add
    ``rdfs:subClassOf target`` and ``skos:exactMatch`` / ``skos:closeMatch``, as the inducers do.
    """

    op: Literal["set_match"]
    iri: str
    target: str | None = None
    kind: Literal["exact", "close", "none"]

    def apply(self, d: Draft, ctx: Context) -> None:
        """Replace the class's grounding; refused while a mapped property needs a grounding parent it removes."""
        g = d.tbox
        c = _term(ctx, g, self.iri, "iri", (CLASS,))
        target: URIRef | None = None
        if self.kind != "none":
            if not self.target:
                raise OpError("set_match needs a target unless kind is none")
            target = URIRef(iris.check_iri(self.target, "target"))
            if target == c:
                raise OpError("A class cannot be grounded to itself")
        foreign_parents = [p for p in named_parents(g, c) if not ctx.scheme.in_space(str(p))]
        uses = {p: _mapped_uses_of(d, p) for p in foreign_parents}
        for pred in GROUNDING_PREDICATES:
            for o in list(g.objects(c, pred)):
                if isinstance(o, URIRef) and not ctx.scheme.in_space(str(o)):
                    g.remove((c, pred, o))
        g.remove((c, URIRef(ctx.scheme.match_confidence), None))
        if target is not None:
            if kind_of(g, target) == CLASS:
                _check_acyclic(g, c, target)
            g.add((c, RDFS.subClassOf, target))
            g.add((c, SKOS.exactMatch if self.kind == "exact" else SKOS.closeMatch, target))
        for p in foreign_parents:
            _keep_mapping_fit(d, uses[p], p, c)

    def summary_message(self, g: Graph) -> Message:
        """Describe the op."""
        if self.kind == "none":
            return message("REMOVE_MATCH", term=_name(g, self.iri))
        code = "SET_EXACT_MATCH" if self.kind == "exact" else "SET_CLOSE_MATCH"
        return message(code, term=_name(g, self.iri), target=_name(g, self.target))


# At most this many mapped properties are named when ``replace_datatype`` is refused.
_MAX_NAMED = 10


def _pom_predicates(r2rml: Graph, pom: Node) -> set[Node]:
    """The properties a PredicateObjectMap maps, read as ``property_maps`` reads them."""
    constants = {c for pm in r2rml.objects(pom, RR.predicateMap) for c in r2rml.objects(pm, RR.constant)}
    return set(r2rml.objects(pom, RR.predicate)) | constants


def _stands_for(token: Node) -> Node:
    """The datatype a token means: the canonical spelling of a malformed one (``xsd:timestamp`` is dateTime)."""
    return _canonical_for(URIRef(str(token))) or token


def _column_conflicts(d: Draft, old: URIRef, new: URIRef) -> list[tuple[str, str | None, str]]:
    """``(property, column, datatype)`` of each mapped property that replacing ``old`` sets against its column.

    A mapped property's datatype follows its column: the ``rr:datatype`` of an object map that maps it
    (``_check_mapping_fit``), read as the datatype its token stands for, as a publish aligns the VKG's
    range with it. So the op may rewrite that ``rr:datatype`` only to repair its spelling (``xsd:datetime``
    to ``xsd:dateTime``; a token with no canonical spelling stays), and may give a property whose range is
    ``old`` no other datatype than the one its column stands for (``xsd:dateTime`` for a column typed
    ``xsd:timestamp``, which a repair rewrites with an op of its own). ``column`` is None for a map with no
    ``rr:column`` (a constant).
    """
    repairs = _stands_for(old) == new
    typed = {pom for om in d.r2rml.subjects(RR.datatype, old) for pom in d.r2rml.subjects(RR.objectMap, om)}
    props = set(d.tbox.subjects(RDFS.range, old)) | {p for pom in typed for p in _pom_predicates(d.r2rml, pom)}
    found: list[tuple[str, str | None, str]] = []
    for p in sorted(props, key=str):
        ranged = (p, RDFS.range, old) in d.tbox
        for _, pom in property_maps(d.r2rml, p):
            for om in sorted(d.r2rml.objects(pom, RR.objectMap), key=str):
                for dt in sorted(d.r2rml.objects(om, RR.datatype), key=str):
                    if (dt == old and not repairs) or (ranged and _stands_for(dt) != new):
                        column = min((sql_name(c) for c in d.r2rml.objects(om, RR.column)), default=None)
                        found.append((_tail(str(p)), column, _tail(str(dt))))
    return list(dict.fromkeys(found))


def _follow_columns(conflicts: list[tuple[str, str | None, str]]) -> str:
    """Why ``conflicts`` refuse the op, in ``_check_mapping_fit``'s words, naming the first ``_MAX_NAMED``."""
    named = "; ".join(
        f"{p} is mapped from column {column}, typed {dt}" if column is not None else f"{p} is mapped as {dt}"
        for p, column, dt in conflicts[:_MAX_NAMED]
    )
    if len(conflicts) > _MAX_NAMED:
        named += f" (and {len(conflicts) - _MAX_NAMED} more)"
    columns = all(column is not None for _, column, _ in conflicts)
    if len(conflicts) == 1:
        return f"{named}; its datatype follows the {'column' if columns else 'mapping'}."
    return f"{named}; their datatypes follow {'their columns' if columns else 'the mapping'}."


def _replace_datatype(g: Graph, old: URIRef, new: URIRef) -> int:
    """Move ``old`` to ``new`` as a range, an ``rr:datatype`` and the datatype of a literal; return how many."""
    n = 0
    for pred in (RDFS.range, RR.datatype):
        for s in list(g.subjects(pred, old)):
            g.remove((s, pred, old))
            g.add((s, pred, new))
            n += 1
    for s, p, o in list(g.triples((None, None, None))):
        if isinstance(o, RdfLiteral) and o.datatype == old:
            g.remove((s, p, o))
            g.add((s, p, RdfLiteral(str(o), datatype=new, normalize=False)))  # the lexical form stays as written
            n += 1
    return n


class ReplaceDatatype(_Op):
    """Replace one datatype IRI by another wherever the draft uses it: ranges, ``rr:datatype`` and typed literals.

    ``from`` is any text, a malformed token such as ``xsd:datetime`` included (``xsd:`` is expanded), and
    is not checked against the datatypes the designer accepts; ``to`` has to be one of them, and an XSD
    datatype when the mapping uses ``from`` as an ``rr:datatype``. It is how a malformed token that came
    with the proposal is repaired. Like ``update_property``, it never retypes a mapped property against its
    column (``_column_conflicts``); a property no mapping maps is retyped freely.
    """

    op: Literal["replace_datatype"]
    from_: str = Field(alias="from")
    to: str

    def apply(self, d: Draft, ctx: Context) -> None:
        """Rewrite every use of ``from`` in both graphs, or refuse when there is none."""
        old = URIRef(iris.expand_xsd(self.from_))
        new = URIRef(iris.datatype_iri(self.to))
        if old == new:
            raise OpError("from and to are the same datatype")
        conflicts = _column_conflicts(d, old, new)
        if conflicts:
            raise OpError(f"{_tail(self.from_)} cannot be replaced with {_tail(self.to)}: {_follow_columns(conflicts)}")
        if not str(new).startswith(iris.XSD) and (None, RR.datatype, old) in d.r2rml:
            # rr:datatype types the literals made from a column, so it has to stay an XSD datatype.
            raise OpError(
                f"{_tail(self.from_)} is the datatype of mapped columns, "
                f"so only an XSD datatype can replace it, not {_tail(self.to)}"
            )
        if _replace_datatype(d.tbox, old, new) + _replace_datatype(d.r2rml, old, new) == 0:
            raise OpError(f"The datatype {self.from_} is not used in the draft")

    def summary_message(self, g: Graph) -> Message:
        """Describe the op."""
        return message("REPLACE_DATATYPE", old=_tail(self.from_), new=_tail(self.to))


class LineDelta(BaseModel):
    """Canonical N-Triples lines to add to and remove from one graph."""

    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class RestoreTriples(_Op):
    """Internal inverse of a change set: exact canonical N-Triples lines to put back and take out."""

    op: Literal["restore_triples"]
    tbox: LineDelta = Field(default_factory=LineDelta)
    r2rml: LineDelta = Field(default_factory=LineDelta)

    def apply(self, d: Draft, ctx: Context) -> None:
        """Patch both graphs; every line to remove must be present, else the draft drifted."""
        d.tbox = _patched(d.tbox, self.tbox)
        d.r2rml = _patched(d.r2rml, self.r2rml)

    def summary_message(self, g: Graph) -> Message:
        """Describe the op."""
        return message("RESTORE_TRIPLES")


def parse_nt(text: str) -> Graph:
    """Parse canonical N-Triples (an empty text gives an empty graph)."""
    g = Graph()
    if text.strip():
        g.parse(data=text, format="nt")
    return g


def nt_lines(text: str) -> set[str]:
    """The non-empty lines of an N-Triples text (split on newlines only, as ``canonical_nt`` joins them)."""
    return {line for line in text.split("\n") if line.strip()}


def _patched(g: Graph, delta: LineDelta) -> Graph:
    if not delta.add and not delta.remove:
        return g
    lines = nt_lines(snapshot.canonical_nt(g))
    if not set(delta.remove) <= lines:
        raise OpError("The draft no longer matches this change, so it cannot be reversed")
    lines = (lines - set(delta.remove)) | set(delta.add)
    return parse_nt("\n".join(sorted(lines)) + "\n")


def line_delta(before: str, after: str) -> LineDelta:
    """The delta that turns canonical text ``after`` back into ``before``."""
    b, a = nt_lines(before), nt_lines(after)
    return LineDelta(add=sorted(b - a), remove=sorted(a - b))


# The 14 ops a client may send. ``AnyOp`` adds the internal inverse, so the members are listed once.
_USER_OP_TYPES = (
    AddClass
    | SetLabel
    | SetComment
    | ChangeIri
    | DeleteClass
    | MergeClasses
    | AddParent
    | RemoveParent
    | AddObjectProperty
    | AddDatatypeProperty
    | UpdateProperty
    | DeleteProperty
    | SetMatch
    | ReplaceDatatype
)
UserOp = Annotated[_USER_OP_TYPES, Field(discriminator="op")]
AnyOp = Annotated[_USER_OP_TYPES | RestoreTriples, Field(discriminator="op")]
USER_OPS: TypeAdapter[list[UserOp]] = TypeAdapter(list[UserOp])
ANY_OPS: TypeAdapter[list[AnyOp]] = TypeAdapter(list[AnyOp])


class ChangeSetError(Exception):
    """Op ``index`` of a change set failed; nothing was applied."""

    def __init__(self, index: int, message: str) -> None:
        """Record which op failed and why."""
        super().__init__(f"Op {index}: {message}")
        self.index = index
        self.message = message


_ANOTHER_LABELLED = {
    CLASS: "ANOTHER_CLASS_LABELLED",
    OBJECT_PROPERTY: "ANOTHER_RELATIONSHIP_LABELLED",
    DATATYPE_PROPERTY: "ANOTHER_ATTRIBUTE_LABELLED",
}


def _duplicate_label_warnings(g: Graph, terms: Iterable[URIRef]) -> list[Message]:
    out: list[Message] = []
    for t in dict.fromkeys(terms):
        kind = kind_of(g, t)
        if kind is None:
            continue
        key = norm(label_of(g, t))
        same = [
            s
            for s in g.subjects(RDF.type, None)
            if isinstance(s, URIRef) and s != t and kind_of(g, s) == kind and norm(label_of(g, s)) == key
        ]
        if same:
            out.append(message(_ANOTHER_LABELLED[kind], label=label_of(g, t), iri=sorted(str(s) for s in set(same))[0]))
    return out


def apply_ops(draft: Draft, ops: Sequence[Any], scheme: IriScheme) -> tuple[Draft, Context]:
    """Apply ``ops`` in order to a copy of ``draft``: all of them, or none (:class:`ChangeSetError`)."""
    working = draft.copy()
    ctx = Context(scheme=scheme)
    for index, op in enumerate(ops):
        line = op.summary_message(working.tbox)  # before the op: a relabel, a merge or a delete drops the label
        try:
            op.apply(working, ctx)
        except (OpError, IriError) as e:
            raise ChangeSetError(index, str(e)) from e
        ctx.summary_messages.append(line)
    # A domain or range given back that a later op took away (with the class or the property) needs no warning.
    for given, added in ctx.given_back:
        if all(t in working.tbox for t in added):
            ctx.warn(given)
    for warning in _duplicate_label_warnings(working.tbox, ctx.labelled):
        ctx.warn(warning)
    return working, ctx
