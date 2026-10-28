# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the pure snapshot builders in coa_ontology.versions.snapshot."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

import pytest
from coa_common.constants import VOCAB_URI
from coa_ontology.catalog.ingest import SUPERSEDED_COMMENT
from coa_ontology.stores import neptune_db_graph
from coa_ontology.versions import snapshot
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import DCTERMS

pytestmark = pytest.mark.unit

ONTO = "https://example.com/onto"
EX = "https://example.com/onto#"

PARENT_TTL = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <{EX}> .
<{ONTO}> a owl:Ontology .
ex:Patient a owl:Class ; rdfs:label "Patient" ; rdfs:comment "AI description" .
ex:Doctor a owl:Class ; rdfs:label "Doctor" .
ex:treats a owl:ObjectProperty ; rdfs:domain ex:Doctor ; rdfs:range ex:Patient .
"""

INCOMING_TTL = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix coa: <{VOCAB_URI}> .
@prefix ex: <{EX}> .
<{ONTO}> a owl:Ontology .
ex:Patient a owl:Class ; rdfs:label "Patient" ; rdfs:comment "Steward description" ; coa:isMapped true .
ex:Ward a owl:Class ; rdfs:label "Ward" ; rdfs:subClassOf [ ] .
"""

R2RML_V1 = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ex: <{EX}> .
<{EX}PatientMap> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "patient" ] ;
    rr:subjectMap [ rr:class ex:Patient ; rr:template "p/{{id}}" ] .
"""

R2RML_V2 = R2RML_V1.replace('"patient"', '"patient_v2"')

# The real inducers (strategies/base.py, rigor_ontology.py) name the SubjectMap,
# PredicateObjectMap and ObjectMap with IRIs; only rr:logicalTable is a blank node.
R2RML_NAMED_V1 = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ex: <{EX}> .
<{EX}PatientMap> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "patient" ] ;
    rr:subjectMap <{EX}PatientMap/SubjectMap> ;
    rr:predicateObjectMap <{EX}PatientMap/POM_Name> .
<{EX}PatientMap/SubjectMap> rr:template "p/{{id}}" ; rr:class ex:Patient .
<{EX}PatientMap/POM_Name> rr:predicate ex:name ; rr:objectMap <{EX}PatientMap/POM_Name/ObjectMap> .
<{EX}PatientMap/POM_Name/ObjectMap> rr:column "name" .
"""

R2RML_NAMED_V2 = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ex: <{EX}> .
<{EX}PatientMap> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "patient" ] ;
    rr:subjectMap <{EX}PatientMap/SubjectMap> ;
    rr:predicateObjectMap <{EX}PatientMap/POM_Name> .
<{EX}PatientMap/SubjectMap> rr:template "p2/{{pid}}" ; rr:class ex:Patient .
<{EX}PatientMap/POM_Name> rr:predicate ex:name ; rr:objectMap <{EX}PatientMap/POM_Name/ObjectMap> .
<{EX}PatientMap/POM_Name/ObjectMap> rr:template "n/{{name}}" ; rr:termType rr:IRI .
"""

# A TriplesMap that joins to another one through rr:parentTriplesMap.
R2RML_JOINED = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ex: <{EX}> .
<{EX}PatientMap> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "patient" ] ;
    rr:subjectMap <{EX}PatientMap/SubjectMap> ;
    rr:predicateObjectMap <{EX}PatientMap/POM_Ward> .
<{EX}PatientMap/SubjectMap> rr:template "p/{{id}}" ; rr:class ex:Patient .
<{EX}PatientMap/POM_Ward> rr:predicate ex:inWard ; rr:objectMap <{EX}PatientMap/POM_Ward/ObjectMap> .
<{EX}PatientMap/POM_Ward/ObjectMap> rr:parentTriplesMap <{EX}WardMap> ;
    rr:joinCondition [ rr:child "ward_id" ; rr:parent "id" ] .
<{EX}WardMap> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "ward" ] ;
    rr:subjectMap [ rr:class ex:Ward ; rr:template "w/{{id}}" ] .
"""

# Canonical form of one TriplesMap with a blank-node logicalTable (see TestCanonicalNt). The
# blank node is a pure tree, so its label is a hash of its content, not an rdflib label.
GOLDEN_NT = (
    f"<{EX}PatientMap> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/ns/r2rml#TriplesMap> .\n"
    f"<{EX}PatientMap> <http://www.w3.org/ns/r2rml#logicalTable> _:h2e164ede223dd700d1c6262665a8a556 .\n"
    '_:h2e164ede223dd700d1c6262665a8a556 <http://www.w3.org/ns/r2rml#tableName> "patient" .\n'
)

DOCTOR_TTL = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <{EX}> .
ex:Doctor a owl:Class ;
    rdfs:subClassOf [ a owl:Restriction ; owl:onProperty ex:treats ; owl:someValuesFrom ex:Patient ] .
ex:Patient a owl:Class .
"""

# Re-emitted by every re-induction: a restriction whose filler is itself a blank-node list.
RESTRICTION_TTL = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <{EX}> .
ex:Patient rdfs:subClassOf [
    a owl:Restriction ;
    owl:onProperty ex:treatedBy ;
    owl:someValuesFrom [ a owl:Class ; owl:unionOf ( ex:Doctor ex:Nurse ) ]
] .
"""


# Two restrictions with the same shape, but _:shared is also linked from ex:Ward, so
# the first is not a self-contained tree and collapsing it would cut that link.
SHARED_BNODE_TTL = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <{EX}> .
ex:Patient rdfs:subClassOf _:x , _:y .
_:x a owl:Restriction ; owl:onProperty ex:p ; owl:someValuesFrom _:shared .
_:shared a owl:Class .
ex:Ward ex:q _:shared .
_:y a owl:Restriction ; owl:onProperty ex:p ; owl:someValuesFrom _:inner .
_:inner a owl:Class .
"""

# Two identical two-node blank-node cycles under one predicate.
CYCLE_TTL = f"""@prefix ex: <{EX}> .
ex:Patient ex:link _:b1 , _:b3 .
_:b1 ex:next _:b2 .
_:b2 ex:next _:b1 .
_:b3 ex:next _:b4 .
_:b4 ex:next _:b3 .
"""

# Two maps sharing one named logical table.
R2RML_SHARED_VIEW = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ex: <{EX}> .
<{EX}AMap> a rr:TriplesMap ; rr:logicalTable ex:SharedView ;
    rr:subjectMap [ rr:class ex:Patient ; rr:template "a/{{id}}" ] .
<{EX}BMap> a rr:TriplesMap ; rr:logicalTable ex:SharedView ;
    rr:subjectMap [ rr:class ex:Ward ; rr:template "b/{{id}}" ] .
ex:SharedView rr:sqlQuery "SELECT 1" .
"""

R2RML_A_ON_OWN_TABLE = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ex: <{EX}> .
<{EX}AMap> a rr:TriplesMap ; rr:logicalTable [ rr:tableName "a2" ] ;
    rr:subjectMap [ rr:class ex:Patient ; rr:template "a2/{{id}}" ] .
"""

R2RML_A_REDEFINES_SHARED_VIEW = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ex: <{EX}> .
<{EX}AMap> a rr:TriplesMap ; rr:logicalTable ex:SharedView ;
    rr:subjectMap [ rr:class ex:Patient ; rr:template "a2/{{id}}" ] .
ex:SharedView rr:sqlQuery "SELECT 2" .
"""

# Tree-shaped blank nodes (a restriction over an RDF list) next to shared ones (_:t1 is
# linked from _:s1 and from ex:Ward) and a cycle (_:b1 <-> _:b2).
MIXED_TTL = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <{EX}> .
ex:Patient rdfs:subClassOf [ a owl:Restriction ; owl:onProperty ex:p ;
    owl:someValuesFrom [ a owl:Class ; owl:unionOf ( ex:A ex:B ) ] ] ;
  ex:link _:b1 , _:s1 .
_:b1 ex:next _:b2 .
_:b2 ex:next _:b1 .
_:s1 ex:q _:t1 .
ex:Ward ex:q _:t1 .
_:t1 a owl:Class .
"""

# Two identical subtrees under a blank parent (they collapse into one when canonicalized).
DUPLICATE_SUBTREES_TTL = f"""@prefix ex: <{EX}> .
ex:O ex:p [ ex:q [ ex:v "1" ] , [ ex:v "1" ] ] .
"""

# Ownerless axioms: blank-node roots that nothing links to.
OWNERLESS_TTL = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix ex: <{EX}> .
[ a owl:AllDisjointClasses ; owl:members ( ex:A ex:B ex:C ) ] .
[ a owl:AllDisjointClasses ; owl:members ( ex:D ex:E ) ] .
"""

# Runs in a child interpreter so the hash seed can differ between runs.
_DEDUPE_SCRIPT = """
import json, sys
from coa_ontology.versions import snapshot

request = json.load(sys.stdin)
payloads = request["payloads"]
merged = snapshot.parse_turtle(payloads[0])
for payload in payloads[1:]:
    merged = snapshot.merge_ontology(merged, snapshot.clean_incoming(payload))
r2rml = snapshot.merge_r2rml(snapshot.parse_turtle(request["r2rml"][0]), snapshot.parse_turtle(request["r2rml"][1]))
sys.stdout.write("\\0" + snapshot.canonical_nt(merged) + "\\0" + snapshot.canonical_nt(r2rml))
"""


# Doctor (ds-2) joins to Ward, which a test may or may not define.
DOCTOR_JOINS_WARD = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ex: <{EX}> .
<{EX}Doctor> rr:predicateObjectMap <{EX}Doctor/POM_Ward> .
<{EX}Doctor/POM_Ward> rr:predicate ex:inWard ; rr:objectMap <{EX}Doctor/POM_Ward/ObjectMap> .
<{EX}Doctor/POM_Ward/ObjectMap> rr:parentTriplesMap <{EX}Ward> .
"""


def _ds_maps(*maps: tuple[str, str | None]) -> str:
    """R2RML for ``(name, datasource id)`` maps, each on table ``name`` with a named SubjectMap."""
    prefixes = f"@prefix rr: <http://www.w3.org/ns/r2rml#> . @prefix coa: <{VOCAB_URI}> . @prefix ex: <{EX}> .\n"
    body = ""
    for name, datasource in maps:
        marker = f'coa:datasourceId "{datasource}" ;' if datasource else ""
        body += (
            f'<{EX}{name}> a rr:TriplesMap ; {marker} rr:logicalTable [ rr:tableName "{name.lower()}" ] ;\n'
            f"    rr:subjectMap <{EX}{name}/SubjectMap> .\n"
            f'<{EX}{name}/SubjectMap> rr:class ex:{name} ; rr:template "{name.lower()}/{{id}}" .\n'
        )
    return prefixes + body


def _merged() -> Graph:
    return snapshot.merge_ontology(snapshot.parse_turtle(PARENT_TTL), snapshot.clean_incoming(INCOMING_TTL))


class TestCanonicalNt:
    def test_is_byte_identical_across_parses_with_blank_nodes(self):
        outputs = {snapshot.canonical_nt(snapshot.parse_turtle(R2RML_V1)) for _ in range(3)}
        assert len(outputs) == 1

    def test_output_parses_back_as_turtle(self):
        text = snapshot.canonical_nt(snapshot.parse_turtle(PARENT_TTL))
        assert len(snapshot.parse_turtle(text)) == len(snapshot.parse_turtle(PARENT_TTL))

    def test_empty_graph_is_empty_string(self):
        assert snapshot.canonical_nt(snapshot.parse_turtle("")) == ""

    def test_literal_with_unicode_line_separators_round_trips(self):
        # str.splitlines() also breaks on U+2028, U+2029, U+0085, \x0b, \x0c and \x1c-\x1e,
        # which N-Triples leaves unescaped inside a literal; only "\n" delimits a statement.
        g = Graph()
        separators = "\u2028 \u2029 \x85 \x0b \x0c \x1c \x1d \x1e"
        g.add((URIRef(EX + "a"), RDFS.label, Literal(f"one {separators} two")))
        g.add((URIRef(EX + "b"), RDFS.label, Literal("plain")))
        text = snapshot.canonical_nt(g)
        assert text.count("\n") == 2
        assert set(snapshot.parse_turtle(text)) == set(g)

    def test_output_is_pinned_for_a_blank_node_logical_table(self):
        # Golden bytes: an rdflib upgrade that changes canonical blank-node labels
        # would silently change every published file hash, so it must fail here.
        g = snapshot.parse_turtle(
            f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
<{EX}PatientMap> a rr:TriplesMap ; rr:logicalTable [ rr:tableName "patient" ] .
"""
        )
        assert snapshot.canonical_nt(g) == GOLDEN_NT

    def test_pure_tree_blank_nodes_get_content_hash_labels(self):
        text = snapshot.canonical_nt(snapshot.parse_turtle(R2RML_V1))
        labels = re.findall(r"_:(\w+)", text)
        assert len(set(labels)) == 2
        assert all(re.fullmatch(r"h[0-9a-f]{32}", label) for label in labels)

    def test_blank_nodes_that_are_not_trees_keep_rdflibs_canonical_labels(self):
        text = snapshot.canonical_nt(
            snapshot.parse_turtle(f"<{EX}a> <{EX}p> _:x . <{EX}b> <{EX}p> _:x . _:x <{EX}q> 1 .")
        )
        assert text.count("_:cb0") == 3

    @pytest.mark.parametrize(
        "ttl",
        [
            R2RML_V1,
            R2RML_NAMED_V1,
            R2RML_JOINED,
            DOCTOR_TTL,
            RESTRICTION_TTL,
            SHARED_BNODE_TTL,
            CYCLE_TTL,
            MIXED_TTL,
            OWNERLESS_TTL,
        ],
    )
    def test_output_is_isomorphic_to_the_input(self, ttl):
        g = snapshot.parse_turtle(ttl)
        assert isomorphic(snapshot.parse_turtle(snapshot.canonical_nt(g)), g)

    @pytest.mark.parametrize(
        "ttl",
        [
            R2RML_V1,
            R2RML_NAMED_V1,
            R2RML_JOINED,
            DOCTOR_TTL,
            RESTRICTION_TTL,
            SHARED_BNODE_TTL,
            CYCLE_TTL,
            MIXED_TTL,
            DUPLICATE_SUBTREES_TTL,
            OWNERLESS_TTL,
            f"<{EX}a> <{EX}p> [ <{EX}q> 1 ] , [ <{EX}q> 1 ] .",
            f"[ <{EX}q> 1 ] . [ <{EX}q> 1 ] .",
        ],
    )
    def test_is_idempotent(self, ttl):
        once = snapshot.canonical_nt(snapshot.parse_turtle(ttl))
        assert snapshot.canonical_nt(snapshot.parse_turtle(once)) == once

    def test_ownerless_axioms_get_content_hash_labels_too(self):
        text = snapshot.canonical_nt(snapshot.parse_turtle(OWNERLESS_TTL))
        assert "_:cb" not in text
        # Two axioms, three list nodes in one and two in the other.
        assert len(set(re.findall(r"_:h[0-9a-f]{32}", text))) == 2 + 3 + 2

    def test_identical_ownerless_trees_collapse_to_one(self):
        g = snapshot.parse_turtle(f"[ <{EX}q> 1 ] . [ <{EX}q> 1 ] .")
        assert len(g) == 2
        assert len(snapshot.parse_turtle(snapshot.canonical_nt(g))) == 1

    def test_same_tree_under_different_owners_stays_two_blank_nodes(self):
        g = snapshot.parse_turtle(f"<{EX}a> <{EX}p> [ <{EX}q> 1 ] . <{EX}b> <{EX}p> [ <{EX}q> 1 ] .")
        parsed = snapshot.parse_turtle(snapshot.canonical_nt(g))
        assert len(set(parsed.objects(None, URIRef(EX + "p")))) == 2

    def test_same_tree_under_different_predicates_stays_two_blank_nodes(self):
        g = snapshot.parse_turtle(f"<{EX}a> <{EX}p> [ <{EX}q> 1 ] ; <{EX}r> [ <{EX}q> 1 ] .")
        parsed = snapshot.parse_turtle(snapshot.canonical_nt(g))
        assert len({o for o in parsed.objects(URIRef(EX + "a"), None) if isinstance(o, BNode)}) == 2

    def test_same_subtree_under_two_predicates_of_a_nested_node_stays_two_blank_nodes(self):
        g = snapshot.parse_turtle(f"<{EX}a> <{EX}p> [ <{EX}x> [ <{EX}q> 1 ] ; <{EX}y> [ <{EX}q> 1 ] ] .")
        parsed = snapshot.parse_turtle(snapshot.canonical_nt(g))
        assert len(parsed) == len(g)
        assert isomorphic(parsed, g)

    def test_identical_trees_under_one_owner_and_predicate_collapse_to_one(self):
        # They are isomorphic duplicates; merge_ontology dedupes them already, and the
        # N-Triples set semantics collapse any that reach canonical_nt.
        g = snapshot.parse_turtle(f"<{EX}a> <{EX}p> [ <{EX}q> 1 ] , [ <{EX}q> 1 ] .")
        assert len(g) == 4
        assert len(snapshot.parse_turtle(snapshot.canonical_nt(g))) == 2

    def test_a_long_rdf_list_does_not_hit_the_recursion_limit(self):
        items = " ".join(str(i) for i in range(3000))
        g = snapshot.parse_turtle(f"<{EX}a> <{EX}list> ( {items} ) .")
        parsed = snapshot.parse_turtle(snapshot.canonical_nt(g))
        assert len(parsed) == len(g)


def _inducer_shaped_r2rml(tables: int) -> Graph:
    """R2RML shaped like the inducers' output: a blank logicalTable and a blank joinCondition per table."""
    rr = snapshot.RR
    g = Graph()
    for i in range(tables):
        tmap = URIRef(f"{EX}TriplesMap_T{i}")
        g.add((tmap, RDF.type, rr.TriplesMap))
        logical_table = BNode()
        g.add((tmap, rr.logicalTable, logical_table))
        g.add((logical_table, rr.tableName, Literal(f"t{i}")))
        subject_map = URIRef(f"{tmap}/SubjectMap")
        g.add((tmap, rr.subjectMap, subject_map))
        g.add((subject_map, rr.template, Literal(f"t{i}/{{id}}")))
        g.add((subject_map, rr["class"], URIRef(f"{EX}T{i}")))
        for column in ("Name", "Parent"):
            pom = URIRef(f"{tmap}/POM_{column}")
            object_map = URIRef(f"{pom}/ObjectMap")
            g.add((tmap, rr.predicateObjectMap, pom))
            g.add((pom, rr.predicate, URIRef(f"{EX}t{i}{column}")))
            g.add((pom, rr.objectMap, object_map))
            if column == "Name":
                g.add((object_map, rr.column, Literal("name")))
            else:
                g.add((object_map, rr.parentTriplesMap, URIRef(f"{EX}TriplesMap_T{(i + 1) % tables}")))
                join = BNode()
                g.add((object_map, rr.joinCondition, join))
                g.add((join, rr.child, Literal("parent_id")))
                g.add((join, rr.parent, Literal("id")))
    return g


class TestCanonicalNtScaling:
    def test_inducer_shaped_r2rml_round_trips(self):
        g = _inducer_shaped_r2rml(30)
        assert isomorphic(snapshot.parse_turtle(snapshot.canonical_nt(g)), g)

    def test_three_thousand_tables_canonicalize_quickly(self):
        # About 0.5 s on a laptop; the limit is generous so a slow CI runner does not flake,
        # and still far below the minutes the whole-graph rdflib canonicalization needed.
        tables = 3000
        g = _inducer_shaped_r2rml(tables)
        started = time.perf_counter()
        text = snapshot.canonical_nt(g)
        elapsed = time.perf_counter() - started
        labels = len(set(re.findall(r"_:h[0-9a-f]+", text)))
        assert elapsed < 20, f"canonical_nt took {elapsed:.1f}s for {tables} tables"
        assert labels == 2 * tables, f"{labels} blank-node labels for {tables} tables"

    def test_two_thousand_ownerless_axioms_canonicalize_quickly_and_deterministically(self):
        axioms = 2000
        ttl = f"@prefix owl: <http://www.w3.org/2002/07/owl#> .\n@prefix ex: <{EX}> .\n" + "".join(
            f"[ a owl:AllDisjointClasses ; owl:members ( ex:A{i} ex:B{i} ex:C{i} ) ] .\n" for i in range(axioms)
        )
        started = time.perf_counter()
        text = snapshot.canonical_nt(snapshot.parse_turtle(ttl))
        elapsed = time.perf_counter() - started
        labels = len(set(re.findall(r"_:h[0-9a-f]+", text)))
        assert elapsed < 20, f"canonical_nt took {elapsed:.1f}s for {axioms} axioms"
        assert "_:cb" not in text
        assert labels == 4 * axioms, f"{labels} blank-node labels for {axioms} axioms"
        # A second parse names every blank node differently; the bytes must not change.
        assert snapshot.canonical_nt(snapshot.parse_turtle(ttl)) == text


class TestCleanIncoming:
    def test_strips_ismapped_and_dangling_blank_nodes(self):
        g = snapshot.clean_incoming(INCOMING_TTL)
        assert not list(g.triples((None, URIRef(f"{VOCAB_URI}isMapped"), None)))
        assert not list(g.objects(URIRef(EX + "Ward"), RDFS.subClassOf))

    def test_strips_a_property_marker_too(self):
        # Deploy derives the property marker from the R2RML, like the class marker; a proposal cannot carry one.
        g = snapshot.clean_incoming(INCOMING_TTL + f"<{EX}tier> a owl:DatatypeProperty ; coa:isMapped false .\n")
        assert not list(g.triples((None, URIRef(f"{VOCAB_URI}isMapped"), None)))
        assert (URIRef(EX + "tier"), RDF.type, OWL.DatatypeProperty) in g

    def test_strips_physical_names(self):
        # Deploy names mapped tables and columns from the R2RML too; a proposal's own names are dropped.
        g = snapshot.clean_incoming(
            INCOMING_TTL + f'<{EX}Ward> coa:physicalName "wards" .\n<{EX}tier> coa:physicalName "tier" .\n'
        )
        assert not list(g.triples((None, URIRef(f"{VOCAB_URI}physicalName"), None)))

    def test_strips_where_the_mapping_puts_a_property(self):
        # Deploy records the classes a mapping puts a property on and joins it to; a proposal cannot.
        g = snapshot.clean_incoming(
            INCOMING_TTL + f"<{EX}tier> coa:mappedDomain <{EX}Ward> ; coa:mappedRange <{EX}Ward> .\n"
        )
        for marker in ("mappedDomain", "mappedRange"):
            assert not list(g.triples((None, URIRef(f"{VOCAB_URI}{marker}"), None)))

    def test_strips_created_and_modified_timestamps_from_the_ontology_node(self):
        g = snapshot.clean_incoming(f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix ex: <{EX}> .
<{ONTO}> a owl:Ontology ; dct:created "2026-01-01" ; dct:modified "2026-01-02" ; dct:title "T" .
ex:Patient a owl:Class ; dct:created "2026-02-02" .
""")
        subject = URIRef(ONTO)
        assert not list(g.objects(subject, DCTERMS.created))
        assert not list(g.objects(subject, DCTERMS.modified))
        assert [str(o) for o in g.objects(subject, DCTERMS.title)] == ["T"]
        assert [str(o) for o in g.objects(URIRef(EX + "Patient"), DCTERMS.created)] == ["2026-02-02"]

    def test_rejects_invalid_turtle(self):
        with pytest.raises(snapshot.SnapshotValidationError, match="Invalid ontology Turtle"):
            snapshot.clean_incoming("this is not turtle <<<")

    def test_rejects_empty_payload(self):
        with pytest.raises(snapshot.SnapshotValidationError, match="no ontology triples"):
            snapshot.clean_incoming("   ")


class TestMergeOntology:
    def test_incoming_subject_annotation_replaces_parent_and_parent_value_moves_to_history(self):
        merged = _merged()
        patient = URIRef(EX + "Patient")
        assert {str(o) for o in merged.objects(patient, RDFS.comment)} == {"Steward description"}
        assert {str(o) for o in merged.objects(patient, SUPERSEDED_COMMENT)} == {"AI description"}

    def test_parent_axioms_and_untouched_subjects_survive(self):
        merged = _merged()
        treats = URIRef(EX + "treats")
        assert set(merged.objects(treats, RDFS.domain)) == {URIRef(EX + "Doctor")}
        assert (URIRef(EX + "Doctor"), RDFS.label, None) in merged
        assert (URIRef(EX + "Ward"), None, OWL.Class) in merged

    def test_value_the_incoming_graph_reasserts_stays_live_without_a_history_copy(self):
        once = _merged()
        twice = snapshot.merge_ontology(once, snapshot.clean_incoming(INCOMING_TTL))
        patient = URIRef(EX + "Patient")
        assert {str(o) for o in twice.objects(patient, RDFS.comment)} == {"Steward description"}
        assert {str(o) for o in twice.objects(patient, SUPERSEDED_COMMENT)} == {"AI description"}
        assert set(twice) == set(once)

    def test_merging_the_same_blank_node_axioms_twice_adds_nothing(self):
        parent = snapshot.parse_turtle(PARENT_TTL)
        once = snapshot.merge_ontology(parent, snapshot.clean_incoming(RESTRICTION_TTL))
        twice = snapshot.merge_ontology(once, snapshot.clean_incoming(RESTRICTION_TTL))
        assert len(twice) == len(once)
        assert snapshot.canonical_nt(twice) == snapshot.canonical_nt(once)

    def test_blank_nodes_linked_from_outside_their_subtree_are_never_collapsed(self):
        incoming = snapshot.clean_incoming(SHARED_BNODE_TTL)
        merged = snapshot.merge_ontology(Graph(), incoming)
        assert len(merged) == len(incoming)
        shared = next(merged.objects(URIRef(EX + "Ward"), URIRef(EX + "q")))
        assert (None, OWL.someValuesFrom, shared) in merged
        assert len(list(merged.objects(URIRef(EX + "Patient"), RDFS.subClassOf))) == 2

    def test_a_dropped_duplicate_leaves_no_blank_node_cycle_behind(self):
        merged = snapshot.merge_ontology(Graph(), snapshot.clean_incoming(CYCLE_TTL))
        link = URIRef(EX + "link")
        assert len(list(merged.objects(URIRef(EX + "Patient"), link))) == 1
        assert len(merged) == 3  # the link plus one two-node cycle

    def test_result_does_not_depend_on_the_hash_seed(self):
        payloads = [
            PARENT_TTL,
            RESTRICTION_TTL,
            RESTRICTION_TTL,
            SHARED_BNODE_TTL,
            CYCLE_TTL,
            MIXED_TTL,
            OWNERLESS_TTL,
            DUPLICATE_SUBTREES_TTL,
            INCOMING_TTL,
        ]
        request = {"payloads": payloads, "r2rml": [R2RML_JOINED, R2RML_NAMED_V2]}

        def run(seed: int) -> tuple[str, str]:
            result = subprocess.run(
                [sys.executable, "-c", _DEDUPE_SCRIPT],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONHASHSEED": str(seed)},
                timeout=120,
                check=True,
            )
            ontology_nt, r2rml_nt = result.stdout.split("\0")[-2:]
            return ontology_nt, r2rml_nt

        outputs = {run(seed) for seed in (1, 2)}
        assert len(outputs) == 1
        ontology_nt, r2rml_nt = outputs.pop()
        assert ontology_nt.startswith("<")
        assert r2rml_nt.startswith("<")

    def test_distinct_blank_node_axioms_on_one_subject_both_survive(self):
        other = RESTRICTION_TTL.replace("ex:Nurse", "ex:Surgeon")
        merged = snapshot.merge_ontology(
            snapshot.merge_ontology(snapshot.parse_turtle(PARENT_TTL), snapshot.clean_incoming(RESTRICTION_TTL)),
            snapshot.clean_incoming(other),
        )
        assert len(list(merged.objects(URIRef(EX + "Patient"), RDFS.subClassOf))) == 2

    def test_duplicates_that_only_become_isomorphic_after_inner_dedupe_collapse(self):
        # The first restriction carries two identical fillers; the second carries one.
        doubled = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <{EX}> .
ex:Patient rdfs:subClassOf [ a owl:Restriction ; owl:onProperty ex:p ;
    owl:someValuesFrom [ a owl:Class ] , [ a owl:Class ] ] .
"""
        single = doubled.replace("[ a owl:Class ] , [ a owl:Class ]", "[ a owl:Class ]")
        merged = snapshot.merge_ontology(snapshot.clean_incoming(doubled), snapshot.clean_incoming(single))
        assert len(list(merged.objects(URIRef(EX + "Patient"), RDFS.subClassOf))) == 1


class TestMergeR2rml:
    def test_triplesmap_with_same_iri_is_replaced_not_unioned(self):
        merged = snapshot.merge_r2rml(snapshot.parse_turtle(R2RML_V1), snapshot.parse_turtle(R2RML_V2))
        tables = {str(o) for o in merged.objects(None, snapshot.RR.tableName)}
        assert tables == {"patient_v2"}
        assert len(list(merged.objects(URIRef(EX + "PatientMap"), snapshot.RR.logicalTable))) == 1

    def test_named_term_maps_are_replaced_with_the_triplesmap(self):
        merged = snapshot.merge_r2rml(snapshot.parse_turtle(R2RML_NAMED_V1), snapshot.parse_turtle(R2RML_NAMED_V2))
        assert isomorphic(merged, snapshot.parse_turtle(R2RML_NAMED_V2))
        assert {str(o) for o in merged.objects(None, snapshot.RR.template)} == {"p2/{pid}", "n/{name}"}
        assert not list(merged.triples((None, snapshot.RR.column, None)))

    def test_map_is_identified_by_its_logical_table_even_without_a_type_triple(self):
        untyped = R2RML_NAMED_V2.replace("a rr:TriplesMap ;", "")
        merged = snapshot.merge_r2rml(snapshot.parse_turtle(R2RML_NAMED_V1), snapshot.parse_turtle(untyped))
        assert {str(o) for o in merged.objects(None, snapshot.RR.template)} == {"p2/{pid}", "n/{name}"}
        assert not list(merged.triples((None, snapshot.RR.column, None)))

    def test_replacing_a_map_leaves_the_map_it_joins_to_untouched(self):
        joined = snapshot.parse_turtle(R2RML_JOINED)
        merged = snapshot.merge_r2rml(joined, snapshot.parse_turtle(R2RML_NAMED_V2))
        ward_map = URIRef(EX + "WardMap")
        assert {str(o) for o in merged.objects(ward_map, snapshot.RR.logicalTable / snapshot.RR.tableName)} == {"ward"}
        assert (ward_map, snapshot.RR.subjectMap, None) in merged
        # The replaced map's old join and predicate-object map are gone.
        assert not list(merged.triples((None, snapshot.RR.joinCondition, None)))
        assert not list(merged.triples((None, snapshot.RR.parentTriplesMap, None)))
        assert not list(merged.triples((URIRef(EX + "PatientMap/POM_Ward"), None, None)))

    def test_replacing_a_map_keeps_a_named_logical_table_another_map_still_uses(self):
        merged = snapshot.merge_r2rml(
            snapshot.parse_turtle(R2RML_SHARED_VIEW), snapshot.parse_turtle(R2RML_A_ON_OWN_TABLE)
        )
        shared_view = URIRef(EX + "SharedView")
        assert (shared_view, snapshot.RR.sqlQuery, Literal("SELECT 1")) in merged
        assert (URIRef(EX + "BMap"), snapshot.RR.logicalTable, shared_view) in merged
        assert not list(merged.triples((URIRef(EX + "AMap"), snapshot.RR.logicalTable, shared_view)))
        assert {str(o) for o in merged.objects(None, snapshot.RR.template)} == {"a2/{id}", "b/{id}"}

    def test_replacing_a_map_replaces_a_shared_table_that_the_incoming_graph_redefines(self):
        merged = snapshot.merge_r2rml(
            snapshot.parse_turtle(R2RML_SHARED_VIEW), snapshot.parse_turtle(R2RML_A_REDEFINES_SHARED_VIEW)
        )
        shared_view = URIRef(EX + "SharedView")
        assert {str(o) for o in merged.objects(shared_view, snapshot.RR.sqlQuery)} == {"SELECT 2"}
        assert (URIRef(EX + "BMap"), snapshot.RR.logicalTable, shared_view) in merged


class TestMergeR2rmlPerDatasource:
    def test_authoritative_datasource_loses_every_map_the_incoming_graph_omits(self):
        parent = snapshot.parse_turtle(_ds_maps(("Patient", "ds-1"), ("Ward", "ds-1"), ("Doctor", "ds-2")))
        incoming = snapshot.parse_turtle(_ds_maps(("Patient", "ds-1")).replace('"patient"', '"patient_v2"'))
        merged = snapshot.merge_r2rml(parent, incoming, authoritative_datasources={"ds-1"})
        expected = snapshot.parse_turtle(_ds_maps(("Doctor", "ds-2")))
        for triple in incoming:
            expected.add(triple)
        assert isomorphic(merged, expected)
        # The dropped map takes its term maps and table with it.
        assert not list(merged.triples((URIRef(EX + "Ward"), None, None)))
        assert not list(merged.triples((URIRef(EX + "Ward/SubjectMap"), None, None)))
        assert "ward" not in {str(o) for o in merged.objects(None, snapshot.RR.tableName)}

    def test_authoritative_datasource_loses_its_maps_even_if_the_incoming_graph_has_none_for_it(self):
        parent = snapshot.parse_turtle(_ds_maps(("Patient", "ds-1"), ("Ward", "ds-1"), ("Doctor", "ds-2")))
        incoming = snapshot.parse_turtle(_ds_maps(("Doctor", "ds-2")))
        merged = snapshot.merge_r2rml(parent, incoming, authoritative_datasources=["ds-1"])
        assert isomorphic(merged, incoming)

    def test_datasource_that_is_not_authoritative_keeps_the_maps_the_incoming_graph_omits(self):
        # RIGOR maps only novel tables, so an omitted table is not a removed table.
        parent = snapshot.parse_turtle(_ds_maps(("Patient", "ds-1"), ("Ward", "ds-1")))
        incoming = snapshot.parse_turtle(_ds_maps(("Patient", "ds-1")).replace('"patient"', '"patient_v2"'))
        merged = snapshot.merge_r2rml(parent, incoming)
        expected = snapshot.parse_turtle(_ds_maps(("Ward", "ds-1")))
        for triple in incoming:
            expected.add(triple)
        assert isomorphic(merged, expected)

    @pytest.mark.parametrize(
        ("map_spelling", "argument_spelling"),
        [("DS#ds-1", "ds-1"), ("ds-1", "DS#ds-1"), ("SRC#ds-1", "DS#ds-1")],
    )
    def test_datasource_id_spellings_are_normalized_on_both_sides(self, map_spelling, argument_spelling):
        parent = snapshot.parse_turtle(_ds_maps(("Ward", map_spelling), ("Doctor", "ds-2")))
        incoming = snapshot.parse_turtle(_ds_maps(("Doctor", "ds-2")))
        merged = snapshot.merge_r2rml(parent, incoming, authoritative_datasources=[argument_spelling])
        assert isomorphic(merged, incoming)

    def test_parent_map_without_a_datasource_id_is_kept(self):
        parent = snapshot.parse_turtle(_ds_maps(("Patient", "ds-1"), ("Legacy", None)))
        incoming = snapshot.parse_turtle(_ds_maps(("Patient", "ds-1")))
        merged = snapshot.merge_r2rml(parent, incoming, authoritative_datasources={"ds-1"})
        assert (URIRef(EX + "Legacy"), snapshot.RR.logicalTable, None) in merged

    def test_remerging_the_same_proposal_is_idempotent(self):
        parent = snapshot.parse_turtle(_ds_maps(("Patient", "ds-1"), ("Ward", "ds-1"), ("Doctor", "ds-2")))
        incoming = snapshot.parse_turtle(_ds_maps(("Patient", "ds-1")))
        once = snapshot.merge_r2rml(parent, incoming, authoritative_datasources={"ds-1"})
        twice = snapshot.merge_r2rml(once, incoming, authoritative_datasources={"ds-1"})
        assert snapshot.canonical_nt(twice) == snapshot.canonical_nt(once)


def _table_map(cls: str, attribute: str, table: str = "customer", *, also: str = "") -> tuple[Graph, Graph]:
    """The TBox and mapping of ``table`` as class ``cls`` with one attribute; ``also`` adds TBox lines."""
    tmap = f"{EX}TriplesMap_{table.title()}"
    tbox = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{EX}{cls}> a owl:Class ; rdfs:label "{table}" .
<{EX}{attribute}> a owl:DatatypeProperty ; rdfs:label "email" ; rdfs:domain <{EX}{cls}> .
{also}"""
    r2rml = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
<{tmap}> a rr:TriplesMap ; rr:logicalTable [ rr:tableName "{table}" ] ;
    rr:subjectMap <{tmap}/SubjectMap> ; rr:predicateObjectMap <{tmap}/POM_Email> .
<{tmap}/SubjectMap> rr:class <{EX}{cls}> ; rr:template "{table}/{{id}}" .
<{tmap}/POM_Email> rr:predicate <{EX}{attribute}> ; rr:objectMap [ rr:column "email" ] .
"""
    return snapshot.parse_turtle(tbox), snapshot.parse_turtle(r2rml)


class TestRenameCandidates:
    """A designer rename keeps the inducer's map IRIs, so a re-induction of the same table names the old IRIs there.

    Publish keeps a candidate only when a version of the lineage removed its old IRI (``test_versions_replace``).
    """

    def test_a_renamed_class_and_attribute_keep_their_new_iris(self):
        head = _table_map("Client", "client_contact")
        reinduced = _table_map("Customer", "customer_email")
        assert snapshot.rename_candidates(*head, *reinduced) == {
            URIRef(EX + "Customer"): URIRef(EX + "Client"),
            URIRef(EX + "customer_email"): URIRef(EX + "client_contact"),
        }

    def test_an_unchanged_reinduction_renames_nothing(self):
        head = _table_map("Customer", "customer_email")
        assert snapshot.rename_candidates(*head, *_table_map("Customer", "customer_email")) == {}

    def test_an_iri_the_head_still_has_is_not_renamed(self):
        head = _table_map("Client", "customer_email", also=f"<{EX}Customer> a <{OWL}Class> .")
        assert snapshot.rename_candidates(*head, *_table_map("Customer", "customer_email")) == {}

    def test_an_iri_the_reinduction_defines_itself_is_not_a_rename_target(self):
        head = _table_map("Client", "customer_email")
        reinduced = _table_map("Customer", "customer_email", also=f"<{EX}Client> a <{OWL}Class> .")
        assert snapshot.rename_candidates(*head, *reinduced) == {}

    def test_a_merged_class_also_mapped_from_a_table_the_reinduction_omits_is_not_renamed(self):
        # Client (table client) was merged into Customer (table customer); the client table is re-induced.
        head_tbox, head_r2rml = _table_map("Customer", "customer_email")
        client_tbox, client_r2rml = _table_map("Customer", "client_email", "client")
        head_tbox += client_tbox
        head_r2rml += client_r2rml
        reinduced = _table_map("Client", "client_email", "client")
        assert snapshot.rename_candidates(head_tbox, head_r2rml, *reinduced) == {}

    def test_an_old_iri_two_renamed_terms_claim_is_not_renamed(self):
        # The customer table's class became Client and the client table's Buyer; a re-induction names both Customer.
        client_tbox, client_r2rml = _table_map("Client", "client_contact")
        buyer_tbox, buyer_r2rml = _table_map("Buyer", "buyer_contact", "client")
        customer_tbox, customer_r2rml = _table_map("Customer", "customer_email")
        again_tbox, again_r2rml = _table_map("Customer", "customer_email", "client")
        head = (client_tbox + buyer_tbox, client_r2rml + buyer_r2rml)
        assert snapshot.rename_candidates(*head, customer_tbox + again_tbox, customer_r2rml + again_r2rml) == {}

    def test_rename_term_replaces_the_iri_in_every_position(self):
        g = snapshot.parse_turtle(f"<{EX}a> <{EX}a> <{EX}a> . <{EX}b> <{EX}p> <{EX}a> .")
        snapshot.rename_term(g, URIRef(EX + "a"), URIRef(EX + "z"))
        assert set(g) == {
            (URIRef(EX + "z"), URIRef(EX + "z"), URIRef(EX + "z")),
            (URIRef(EX + "b"), URIRef(EX + "p"), URIRef(EX + "z")),
        }


class TestCheckR2rml:
    def test_passes_when_every_mapped_class_exists(self):
        snapshot.check_r2rml(_merged(), snapshot.parse_turtle(R2RML_V1))

    def test_raises_for_a_class_missing_from_the_ontology(self):
        unknown = R2RML_V1.replace("ex:Patient", "ex:Ghost")
        with pytest.raises(snapshot.SnapshotValidationError, match="Ghost"):
            snapshot.check_r2rml(_merged(), snapshot.parse_turtle(unknown))

    def test_passes_when_every_join_targets_a_map_in_the_mapping(self):
        snapshot.check_r2rml(_merged(), snapshot.parse_turtle(R2RML_JOINED))

    def test_names_both_maps_and_their_datasources_when_a_join_target_was_dropped(self):
        # Doctor (ds-2) joins to Ward (ds-1); re-inducing ds-1 without Ward drops Ward's map.
        parent = snapshot.parse_turtle(
            _ds_maps(("Patient", "ds-1"), ("Ward", "ds-1"), ("Doctor", "ds-2")) + DOCTOR_JOINS_WARD
        )
        snapshot.check_r2rml(_merged(), parent)
        incoming = snapshot.parse_turtle(_ds_maps(("Patient", "ds-1")))
        merged = snapshot.merge_r2rml(parent, incoming, authoritative_datasources={"ds-1"})
        with pytest.raises(snapshot.SnapshotValidationError) as raised:
            snapshot.check_r2rml(_merged(), merged, previous=parent)
        assert (
            f"{EX}Ward (datasource ds-1) is no longer mapped but {EX}Doctor (datasource ds-2) joins to it; "
            "re-induce ds-2 or keep that table mapped"
        ) in str(raised.value)

    def test_still_names_the_maps_when_the_previous_mapping_is_not_given(self):
        parent = snapshot.parse_turtle(_ds_maps(("Doctor", "ds-2")) + DOCTOR_JOINS_WARD)
        with pytest.raises(snapshot.SnapshotValidationError) as raised:
            snapshot.check_r2rml(_merged(), parent)
        assert f"{EX}Ward is not mapped but {EX}Doctor (datasource ds-2) joins to it" in str(raised.value)

    def test_reports_a_missing_class_and_a_missing_join_target_together(self):
        ghost = snapshot.parse_turtle(_ds_maps(("Ghost", "ds-2")) + DOCTOR_JOINS_WARD.replace("Doctor", "Ghost"))
        with pytest.raises(snapshot.SnapshotValidationError) as raised:
            snapshot.check_r2rml(_merged(), ghost)
        message = str(raised.value)
        assert f"{EX}Ghost" in message
        assert "class(es) missing from the ontology" in message
        assert "parent map(s) missing from the mapping" in message
        assert f"{EX}Ward is not mapped but {EX}Ghost (datasource ds-2) joins to it" in message


class TestStampVersion:
    def test_replaces_any_previous_version_info(self):
        g = _merged()
        snapshot.stamp_version(g, ONTO, 1)
        snapshot.stamp_version(g, ONTO, 2)
        assert [str(o) for o in g.objects(URIRef(ONTO), OWL.versionInfo)] == ["v2"]

    def test_leaves_exactly_one_version_info_even_when_another_ontology_node_has_one(self):
        g = snapshot.parse_turtle(f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
<{ONTO}> a owl:Ontology ; owl:versionInfo "old" .
<https://example.com/imported> a owl:Ontology ; owl:versionInfo "1.0" .
""")
        snapshot.stamp_version(g, ONTO, 4)
        assert [(str(s), str(o)) for s, o in g.subject_objects(OWL.versionInfo)] == [(ONTO, "v4")]


class TestStripProjection:
    def test_removes_workbench_projection_markers_and_timestamps(self):
        g = snapshot.parse_turtle(f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix wb: <https://ontology-workbench.local/vocab#> .
@prefix coa: <{VOCAB_URI}> .
<{ONTO}> a owl:Ontology ; dct:created "2026-01-01" ; dct:modified "2026-01-02" ;
    dct:title "T" ; dct:description "D" .
<{EX}Patient> a owl:Class ; rdfs:label "Patient" ; wb:definedBy <{ONTO}> ; coa:isMapped true ;
    dct:title "Patient class" .
""")
        snapshot.strip_projection(g, ONTO)
        predicates = {str(p) for p in g.predicates()}
        assert not any(p.startswith("https://ontology-workbench.local/vocab#") for p in predicates)
        assert f"{VOCAB_URI}isMapped" not in predicates
        assert "http://purl.org/dc/terms/created" not in predicates
        assert "http://purl.org/dc/terms/modified" not in predicates
        assert list(g.objects(URIRef(ONTO), DCTERMS.title)) == []
        assert list(g.objects(URIRef(ONTO), DCTERMS.description)) == []
        assert [str(o) for o in g.objects(URIRef(EX + "Patient"), DCTERMS.title)] == ["Patient class"]
        assert str(RDFS.label) in predicates

    def test_removes_the_property_marker_deploy_derives(self):
        g = snapshot.parse_turtle(f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix coa: <{VOCAB_URI}> .
<{EX}tier> a owl:DatatypeProperty ; coa:isMapped false .
""")
        snapshot.strip_projection(g, ONTO)
        assert set(g) == {(URIRef(EX + "tier"), RDF.type, OWL.DatatypeProperty)}

    def test_removes_the_physical_names_deploy_derives(self):
        g = snapshot.parse_turtle(f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix coa: <{VOCAB_URI}> .
<{EX}Ward> a owl:Class ; coa:isMapped true ; coa:physicalName "wards" .
<{EX}bed> a owl:DatatypeProperty ; coa:physicalName "bed_no" .
""")
        snapshot.strip_projection(g, ONTO)
        assert set(g) == {
            (URIRef(EX + "Ward"), RDF.type, OWL.Class),
            (URIRef(EX + "bed"), RDF.type, OWL.DatatypeProperty),
        }

    def test_removes_where_deploy_recorded_the_mapping_puts_a_property(self):
        g = snapshot.parse_turtle(f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix coa: <{VOCAB_URI}> .
<{EX}bed> a owl:DatatypeProperty ; coa:mappedDomain <{EX}Ward> ; coa:mappedRange <{EX}Ward> .
""")
        snapshot.strip_projection(g, ONTO)
        assert set(g) == {(URIRef(EX + "bed"), RDF.type, OWL.DatatypeProperty)}


class TestWorkbenchPrefix:
    def test_matches_the_neptune_store_constant(self):
        # snapshot.py keeps its own copy so this pure module does not import the HTTP adapter.
        assert snapshot._WB == neptune_db_graph.WB


class TestComputeDiff:
    def test_reports_added_removed_and_changed_iris(self):
        parent = snapshot.parse_turtle(PARENT_TTL)
        child = snapshot.merge_ontology(parent, snapshot.clean_incoming(INCOMING_TTL))
        child.remove((URIRef(EX + "Doctor"), None, None))
        diff = snapshot.compute_diff(parent, child)
        assert diff["classes"]["added"] == [EX + "Ward"]
        assert diff["classes"]["removed"] == [EX + "Doctor"]
        assert diff["classes"]["changed"] == [EX + "Patient"]
        assert snapshot.diff_summary(diff)["classes"] == {"added": 1, "removed": 1, "changed": 1}

    def test_remerging_an_identical_proposal_reports_no_change(self):
        parent = snapshot.parse_turtle(PARENT_TTL)
        once = snapshot.merge_ontology(parent, snapshot.clean_incoming(INCOMING_TTL + RESTRICTION_TTL))
        twice = snapshot.merge_ontology(once, snapshot.clean_incoming(INCOMING_TTL + RESTRICTION_TTL))
        diff = snapshot.compute_diff(once, twice)
        assert all(entry == {"added": [], "removed": [], "changed": []} for entry in diff.values())

    def test_history_annotations_alone_do_not_count_as_a_change(self):
        parent = snapshot.parse_turtle(PARENT_TTL)
        child = snapshot.parse_turtle(PARENT_TTL)
        child.add((URIRef(EX + "Patient"), SUPERSEDED_COMMENT, Literal("older description")))
        assert snapshot.compute_diff(parent, child)["classes"]["changed"] == []

    def test_a_change_inside_a_restriction_marks_the_subject_changed(self):
        every = DOCTOR_TTL.replace("someValuesFrom", "allValuesFrom")
        diff = snapshot.compute_diff(snapshot.parse_turtle(DOCTOR_TTL), snapshot.parse_turtle(every))
        assert diff["classes"]["changed"] == [EX + "Doctor"]

    def test_the_same_restriction_parsed_twice_is_not_a_change(self):
        diff = snapshot.compute_diff(snapshot.parse_turtle(DOCTOR_TTL), snapshot.parse_turtle(DOCTOR_TTL))
        assert diff["classes"]["changed"] == []

    def test_without_parent_everything_is_added(self):
        diff = snapshot.compute_diff(None, snapshot.parse_turtle(PARENT_TTL))
        assert diff["classes"]["added"] == [EX + "Doctor", EX + "Patient"]
        assert diff["objectProperties"]["added"] == [EX + "treats"]
        assert diff["classes"]["removed"] == [] and diff["classes"]["changed"] == []


class TestManifestAndDeterminism:
    def test_manifest_lists_sha256_per_file(self):
        manifest = snapshot.build_manifest(
            namespace="ns",
            version=3,
            parent_version=2,
            source="PROPOSAL",
            source_proposal_id="p-1",
            source_proposals=["p-0", "p-1"],
            ontology_id=ONTO,
            created_by="alice",
            created_at="2026-10-14T00:00:00+00:00",
            triple_count=10,
            files={"ontology.ttl": "a", "diff.json": "b"},
        )
        assert list(manifest["files"]) == ["diff.json", "ontology.ttl"]
        assert manifest["files"]["ontology.ttl"]["sha256"] == snapshot.sha256_hex("a")
        assert manifest["formatVersion"] == snapshot.FORMAT_VERSION

    def test_same_parent_and_proposal_produce_identical_bytes(self):
        def build() -> str:
            g = _merged()
            snapshot.stamp_version(g, ONTO, 7)
            return snapshot.canonical_nt(g)

        assert build() == build()


SH = "http://www.w3.org/ns/shacl#"
XSD = "http://www.w3.org/2001/XMLSchema#"


def _shapes_ttl(body: str) -> Graph:
    return snapshot.parse_turtle(f"@prefix sh: <{SH}> . @prefix xsd: <{XSD}> . @prefix ex: <{EX}> .\n{body}")


class TestMergeShapes:
    def test_redefined_shape_replaces_the_parents_property_shapes(self):
        parent = _shapes_ttl(
            "ex:PatientShape a sh:NodeShape ; sh:targetClass ex:Patient ;"
            " sh:property [ sh:path ex:age ; sh:datatype xsd:string ] ."
        )
        incoming = _shapes_ttl(
            "ex:PatientShape a sh:NodeShape ; sh:targetClass ex:Patient ;"
            " sh:property [ sh:path ex:age ; sh:datatype xsd:integer ] ."
        )
        merged = snapshot.merge_shapes(parent, incoming)
        assert list(merged.objects(None, URIRef(SH + "datatype"))) == [URIRef(XSD + "integer")]
        assert isomorphic(merged, incoming)

    def test_shapes_the_proposal_does_not_define_are_kept(self):
        parent = _shapes_ttl(
            "ex:PatientShape a sh:NodeShape ; sh:property [ sh:path ex:age ; sh:minCount 1 ] .\n"
            "ex:DoctorShape a sh:NodeShape ; sh:property [ sh:path ex:name ; sh:minCount 1 ] ."
        )
        incoming = _shapes_ttl("ex:PatientShape a sh:NodeShape ; sh:property [ sh:path ex:age ; sh:maxCount 1 ] .")
        merged = snapshot.merge_shapes(parent, incoming)
        paths = {str(o) for o in merged.objects(None, URIRef(SH + "path"))}
        assert paths == {EX + "age", EX + "name"}
        assert len(list(merged.objects(None, URIRef(SH + "minCount")))) == 1  # only DoctorShape's

    def test_nested_lists_of_a_replaced_property_shape_go_with_it(self):
        parent = _shapes_ttl('ex:PatientShape a sh:NodeShape ; sh:property [ sh:path ex:status ; sh:in ("a" "b") ] .')
        incoming = _shapes_ttl("ex:PatientShape a sh:NodeShape .")
        merged = snapshot.merge_shapes(parent, incoming)
        assert not list(merged.triples((None, RDF.first, None)))
        assert len(merged) == 1

    def test_a_property_shape_another_shape_still_uses_stays(self):
        parent = Graph()
        shared = BNode()
        for shape in ("PatientShape", "DoctorShape"):
            parent.add((URIRef(EX + shape), RDF.type, URIRef(SH + "NodeShape")))
            parent.add((URIRef(EX + shape), URIRef(SH + "property"), shared))
        parent.add((shared, URIRef(SH + "path"), URIRef(EX + "name")))
        incoming = _shapes_ttl("ex:PatientShape a sh:NodeShape .")
        merged = snapshot.merge_shapes(parent, incoming)
        assert (URIRef(EX + "DoctorShape"), URIRef(SH + "property"), shared) in merged
        assert (shared, URIRef(SH + "path"), URIRef(EX + "name")) in merged
        assert not list(merged.objects(URIRef(EX + "PatientShape"), URIRef(SH + "property")))
