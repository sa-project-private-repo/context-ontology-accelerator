# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import random

import pytest
from coa_ontology.abox import assemble
from coa_ontology.abox import mapping as mp
from coa_ontology.abox import model as m
from coa_ontology.abox.tbox import load_tbox
from coa_ontology.versions.snapshot import canonical_nt
from rdflib import Literal, URIRef
from rdflib.namespace import RDF, RDFS, SKOS

pytestmark = pytest.mark.unit

NS = "11111111-2222-3333-4444-555555555555"
TTL = """
@prefix owl: <http://www.w3.org/2002/07/owl#> . @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix o: <http://e.com/o#> .
o:Customer a owl:Class . o:Order a owl:Class . o:Contract a owl:Class .
o:placedBy a owl:ObjectProperty ; rdfs:domain o:Order ; rdfs:range o:Customer .
o:total a owl:DatatypeProperty ; rdfs:domain o:Order ; rdfs:label "total" .
"""
C, O, K = "http://e.com/o#Customer", "http://e.com/o#Order", "http://e.com/o#Contract"  # noqa: E741
PLACED, TOTAL = "http://e.com/o#placedBy", "http://e.com/o#total"


def _prov(c):
    return m.Provenance(c, "ds-1", f"raw/{c}.pdf")


def _snapshot():
    ents = {
        "e1": m.LexEntity("e1", "ACME Corp", "Company"),
        "e1b": m.LexEntity("e1b", "acme corp", "Organization"),
        "e2": m.LexEntity("e2", "PO-1", "Purchase Order"),
        "e3": m.LexEntity("e3", "$100", m.LOCAL_ENTITY_CLASS),
        "e4": m.LexEntity("e4", "Bob", "Person"),
    }
    facts = (
        m.LexFact("f1", "e1", "PLACED", "e2", _prov("c1")),  # Customer PLACED Order: placedBy inverse
        m.LexFact("f2", "e2", "TOTAL", "e3", _prov("c1")),  # attribute: total
        m.LexFact("f3", "e2", "NOTE", "e3", _prov("c2")),  # attribute: free
        m.LexFact("f4", "e1b", "PLACED", "e2", _prov("c3")),  # same instance via another class string
        m.LexFact("f5", "e4", "KNOWS", "e1", _prov("c4")),  # Person is ignored: no instance
    )
    return m.LexicalSnapshot(ents, facts)


def _rows(tb):
    rows = [
        mp.MappingRow(mp.CLASS, ("Company",), C),
        mp.MappingRow(mp.CLASS, ("Organization",), C),
        mp.MappingRow(mp.CLASS, ("Purchase Order",), O),
        mp.MappingRow(mp.CLASS, ("Person",), mp.IGNORE),
        mp.MappingRow(mp.RELATION, ("PLACED", C, O), PLACED, direction=mp.INVERSE),
        mp.MappingRow(mp.ATTRIBUTE, (O, "TOTAL"), TOTAL),
        mp.MappingRow(mp.ATTRIBUTE, (O, "NOTE"), mp.FREE),
    ]
    return {r.id: r for r in mp.effective(rows, tb)}


def _build(snapshot=None, extracted=([], [])):
    tb = load_tbox(TTL)
    return assemble.assemble(NS, tb, snapshot or _snapshot(), _rows(tb), extracted, version=3, mapping_revision="rev")


def test_instances_labels_and_links():
    g, stats = _build()
    acme = URIRef(m.instance_iri(NS, C, "ACME Corp"))
    po = URIRef(m.instance_iri(NS, O, "PO-1"))
    assert (acme, RDF.type, URIRef(C)) in g and (po, RDF.type, URIRef(O)) in g
    assert set(g.objects(acme, RDFS.label)) == {Literal("ACME Corp")}  # tie broken lexically
    assert set(g.objects(acme, SKOS.altLabel)) == {Literal("acme corp")}
    assert set(g.objects(acme, URIRef(m.LEXICAL_ENTITY_ID))) == {Literal("e1"), Literal("e1b")}
    assert Literal(m.norm_key("ACME Corp")) in set(g.objects(acme, URIRef(m.NORM_KEY)))
    assert (po, URIRef(PLACED), acme) in g  # inverse direction applied
    assert (po, URIRef(TOTAL), Literal("$100")) in g
    free = URIRef(m.free_attribute_iri(NS, "NOTE"))
    assert (po, free, Literal("$100")) in g and (free, RDF.type, URIRef(m.FREE_ATTRIBUTE)) in g
    # ACME: types read in c1, c3 and c4 (as the object of an ignored subject); PO-1: c1, c2, c3;
    # placedBy: c1, c3; total: c1; NOTE: c2.
    assert stats.counts == {"instances": 2, "relations": 1, "attributes": 2, "evidence": 10}
    assert len(set(g.subjects(RDF.type, URIRef(m.EVIDENCE)))) == 10
    assert stats.class_counts == {C: 1, O: 1}


def test_evidence_per_chunk_and_header():
    g, _ = _build()
    acme = m.instance_iri(NS, C, "ACME Corp")
    po = m.instance_iri(NS, O, "PO-1")
    evidence = {
        str(e) for e in g.subjects(URIRef(m.ABOUT), URIRef(po)) if (e, URIRef(m.PREDICATE), URIRef(PLACED)) in g
    }
    assert evidence == {
        m.evidence_iri(NS, po, PLACED, URIRef(acme).n3(), "c1"),
        m.evidence_iri(NS, po, PLACED, URIRef(acme).n3(), "c3"),
    }
    header = URIRef(m.header_iri(NS))
    assert (header, URIRef(m.CONFORMS_TO_VERSION), Literal("v3")) in g
    assert (header, URIRef(m.BUILD_ID), None) not in g  # volatile header fields are added by the builder


def test_domain_range_violations_are_dropped():
    tb = load_tbox(TTL)
    snap = m.LexicalSnapshot(
        {"a": m.LexEntity("a", "PO-2", "Purchase Order"), "b": m.LexEntity("b", "PO-3", "Purchase Order")},
        (m.LexFact("f", "a", "PLACED", "b", _prov("c9")),),
    )
    rows = _rows(tb)
    bad = mp.MappingRow(mp.RELATION, ("PLACED", O, O), PLACED)
    rows[bad.id] = bad
    g, stats = assemble.assemble(NS, tb, snap, rows, ([], []), version=3, mapping_revision="r")
    assert stats.counts["relations"] == 0


def test_extracted_instances_merge_by_iri():
    contract = m.XEntity(K, "MSA-7", (("total", "9"),), _prov("c7"))
    rel = m.XRelation((O, "PO-1"), PLACED, (C, "acme  corp"), _prov("c8"))
    g, stats = _build(extracted=([contract], [rel]))
    assert stats.class_counts == {C: 1, O: 1, K: 1}
    assert (URIRef(m.instance_iri(NS, O, "PO-1")), URIRef(PLACED), URIRef(m.instance_iri(NS, C, "ACME Corp"))) in g
    k = URIRef(m.instance_iri(NS, K, "MSA-7"))
    assert (k, URIRef(m.free_attribute_iri(NS, "total")), Literal("9")) in g  # total's domain is Order, not Contract


def test_extracted_attribute_uses_a_fitting_datatype_property():
    order = m.XEntity(O, "PO-9", (("Total", "5"),), _prov("c7"))
    g, stats = _build(extracted=([order], []))
    assert (URIRef(m.instance_iri(NS, O, "PO-9")), URIRef(TOTAL), Literal("5")) in g
    assert stats.counts["attributes"] == 3  # the two lexical ones, plus this


def test_evidence_is_one_node_per_chunk_and_capped():
    entities = {"a": m.LexEntity("a", "PO-2", "Purchase Order"), "b": m.LexEntity("b", "ACME", "Company")}
    facts = [m.LexFact(f"f{i}", "b", "PLACED", "a", _prov(f"c{i}")) for i in range(7)]
    # The same chunk read again, from a differently keyed source, is still one supporting chunk.
    facts.append(m.LexFact("fx", "b", "PLACED", "a", m.Provenance("c0", "ds-2", "raw/other.pdf")))
    g, _ = _build(m.LexicalSnapshot(entities, tuple(facts)))
    po = m.instance_iri(NS, O, "PO-2")
    acme = m.instance_iri(NS, C, "ACME")
    nodes = {e for e in g.subjects(URIRef(m.PREDICATE), URIRef(PLACED)) if (e, URIRef(m.ABOUT), URIRef(po)) in g}
    assert nodes == {URIRef(m.evidence_iri(NS, po, PLACED, URIRef(acme).n3(), f"c{i}")) for i in range(5)}
    for node in nodes:
        assert len(set(g.objects(node, URIRef(m.CHUNK_ID)))) == 1
        assert len(set(g.objects(node, URIRef(m.SOURCE_KEY)))) == 1


def test_output_is_independent_of_input_order():
    snap = _snapshot()
    shuffled = list(snap.facts)
    random.Random(7).shuffle(shuffled)
    a, _ = _build(snap)
    b, _ = _build(m.LexicalSnapshot(dict(reversed(list(snap.entities.items()))), tuple(shuffled)))
    assert canonical_nt(a) == canonical_nt(b)


def _assemble(snapshot=None, extracted=([], [])):
    tb = load_tbox(TTL)
    snapshot = snapshot or m.LexicalSnapshot({}, ())
    return assemble.assemble(NS, tb, snapshot, _rows(tb), extracted, version=3, mapping_revision="r")


def test_the_object_is_created_when_the_subjects_class_is_ignored():
    snap = m.LexicalSnapshot(
        {"b": m.LexEntity("b", "Bob", "Person"), "i": m.LexEntity("i", "Initech", "Company")},
        (m.LexFact("f", "b", "KNOWS", "i", _prov("c1")),),
    )
    g, stats = _assemble(snap)
    assert (URIRef(m.instance_iri(NS, C, "Initech")), RDF.type, URIRef(C)) in g
    assert stats.counts["instances"] == 1 and stats.counts["relations"] == 0
    assert list(g.subjects(RDF.type, URIRef(m.instance_iri(NS, "http://e.com/o#Person", "Bob")))) == []


def test_the_subject_is_created_when_the_objects_class_is_ignored_or_unmapped():
    snap = m.LexicalSnapshot(
        {
            "i": m.LexEntity("i", "Initech", "Company"),
            "b": m.LexEntity("b", "Bob", "Person"),
            "x": m.LexEntity("x", "Zed", "Unmapped"),
        },
        (m.LexFact("f1", "i", "EMPLOYS", "b", _prov("c1")), m.LexFact("f2", "i", "EMPLOYS", "x", _prov("c2"))),
    )
    g, stats = _assemble(snap)
    assert stats.counts["instances"] == 1 and stats.counts["relations"] == 0


def test_an_attribute_needs_only_its_subject():
    snap = m.LexicalSnapshot(
        {"o": m.LexEntity("o", "PO-4", "Purchase Order"), "v": m.LexEntity("v", "7", m.LOCAL_ENTITY_CLASS)},
        (m.LexFact("f", "o", "TOTAL", "v", _prov("c1")),),
    )
    g, stats = _assemble(snap)
    assert (URIRef(m.instance_iri(NS, O, "PO-4")), URIRef(TOTAL), Literal("7")) in g
    # an attribute of an ignored subject is dropped
    snap = m.LexicalSnapshot(
        {"p": m.LexEntity("p", "Bob", "Person"), "v": m.LexEntity("v", "7", m.LOCAL_ENTITY_CLASS)},
        (m.LexFact("f", "p", "AGE", "v", _prov("c1")),),
    )
    assert _assemble(snap)[1].counts["attributes"] == 0


def test_free_attribute_labels_do_not_depend_on_order():
    def entity(name, key):
        return m.XEntity(K, name, ((key, "1"),), _prov(name))

    a = _assemble(extracted=([entity("a", "NOTE"), entity("b", "Note")], []))[0]
    b = _assemble(extracted=([entity("a", "Note"), entity("b", "NOTE")], []))[0]
    for g in (a, b):
        assert set(g.objects(URIRef(m.free_attribute_iri(NS, "note")), RDFS.label)) == {Literal("NOTE")}


def test_shuffled_extracted_inputs_give_the_same_graph():
    ents = [m.XEntity(K, f"MSA-{i}", (("term", str(i)), ("Note", "n")), _prov(f"c{i % 3}")) for i in range(6)]
    ents += [m.XEntity(O, f"PO-{i}", (("total", str(i)),), _prov(f"d{i}")) for i in range(4)]
    rels = [m.XRelation((O, f"PO-{i}"), PLACED, (C, f"Co {i % 2}"), _prov(f"r{i}")) for i in range(4)]
    a = _assemble(extracted=(ents, rels))[0]
    shuffled_e, shuffled_r = list(ents), list(rels)
    random.Random(3).shuffle(shuffled_e)
    random.Random(4).shuffle(shuffled_r)
    b = _assemble(extracted=(shuffled_e, shuffled_r))[0]
    assert canonical_nt(a) == canonical_nt(b)


def test_names_with_no_visible_characters_make_no_instance():
    blank = "\u200b \u00a0"
    snap = m.LexicalSnapshot(
        {
            "a": m.LexEntity("a", blank, "Company"),
            "b": m.LexEntity("b", "ACME", "Company"),
            "v": m.LexEntity("v", blank, m.LOCAL_ENTITY_CLASS),
            "o": m.LexEntity("o", "PO-1", "Purchase Order"),
        },
        (
            m.LexFact("f1", "a", "PLACED", "o", _prov("c1")),  # blank subject: the object is still created
            m.LexFact("f2", "b", "PLACED", "a", _prov("c2")),  # blank object: the subject is still created
            m.LexFact("f3", "o", "TOTAL", "v", _prov("c3")),  # blank attribute value
        ),
    )
    extracted = (
        [m.XEntity(K, blank, (), _prov("c4")), m.XEntity(K, "MSA-1", (), _prov("c4"))],
        [m.XRelation((O, "PO-2"), PLACED, (C, blank), _prov("c5"))],  # blank endpoint: neither end is created
    )
    g, stats = _assemble(snap, extracted)
    assert stats.class_counts == {C: 1, O: 1, K: 1}
    assert stats.counts["relations"] == 0 and stats.counts["attributes"] == 0
    assert (URIRef(m.instance_iri(NS, O, "PO-2")), RDF.type, URIRef(O)) not in g


def test_display_labels_collapse_whitespace_and_drop_format_characters():
    snap = m.LexicalSnapshot(
        {
            "a": m.LexEntity("a", "ACME \u200b  Corp", "Company"),
            "b": m.LexEntity("b", " ACME Corp\ufeff", "Organization"),
            "c": m.LexEntity("c", "Acme\n\tCorp", "Company"),
        },
        (
            m.LexFact("f1", "a", "X", None, _prov("c1")),
            m.LexFact("f2", "b", "X", None, _prov("c2")),
            m.LexFact("f3", "c", "X", None, _prov("c3")),
        ),
    )
    g, _ = _assemble(snap)
    acme = URIRef(m.instance_iri(NS, C, "ACME Corp"))
    assert set(g.objects(acme, RDFS.label)) == {Literal("ACME Corp")}  # two of the three surfaces agree
    assert set(g.objects(acme, SKOS.altLabel)) == {Literal("Acme Corp")}
    assert set(g.objects(acme, URIRef(m.NORM_KEY))) == {Literal(m.norm_key("ACME Corp"))}


def test_rows_whose_target_is_not_in_the_tbox_are_ignored():
    tb = load_tbox(TTL)
    gone = "http://e.com/o#Gone"
    raw = {
        r.id: r
        for r in (
            mp.MappingRow(mp.CLASS, ("Company",), gone),
            mp.MappingRow(mp.CLASS, ("Purchase Order",), O),
            mp.MappingRow(mp.ATTRIBUTE, (O, "TOTAL"), "http://e.com/o#nowhere"),
            mp.MappingRow(mp.ATTRIBUTE, (O, "NOTE"), mp.FREE),
        )
    }
    assert all(r.status == mp.ACTIVE for r in raw.values())  # raw rows, never passed through ``effective``
    snap = m.LexicalSnapshot(
        {
            "e": m.LexEntity("e", "ACME", "Company"),
            "p": m.LexEntity("p", "PO-1", "Purchase Order"),
            "v": m.LexEntity("v", "$1", m.LOCAL_ENTITY_CLASS),
        },
        (
            m.LexFact("f1", "e", "X", None, _prov("c1")),
            m.LexFact("f2", "p", "TOTAL", "v", _prov("c1")),
            m.LexFact("f3", "p", "NOTE", "v", _prov("c1")),
        ),
    )
    g, stats = assemble.assemble(NS, tb, snap, raw, ([], []), version=1, mapping_revision="r")
    assert stats.class_counts == {O: 1}
    assert stats.counts["attributes"] == 1
    assert (None, URIRef("http://e.com/o#nowhere"), None) not in g
    assert not list(g.subjects(RDF.type, URIRef(gone)))


def test_line_separators_in_a_label_are_kept():
    snap = m.LexicalSnapshot(
        {"a": m.LexEntity("a", "ACME\u2028Corp  Ltd", "Company")}, (m.LexFact("f", "a", "X", None, _prov("c1")),)
    )
    g, _ = _assemble(snap)
    assert set(g.objects(URIRef(m.instance_iri(NS, C, "ACME Corp Ltd")), RDFS.label)) == {Literal("ACME\u2028Corp Ltd")}
