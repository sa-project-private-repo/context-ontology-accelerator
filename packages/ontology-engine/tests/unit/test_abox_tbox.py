# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import pytest
from coa_ontology.abox.tbox import load_tbox, schema_hash

pytestmark = pytest.mark.unit

PREFIXES = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix o: <http://e.com/o#> .
@prefix fibo: <http://f.com/fibo#> .
"""
ORDER, INVOICE, CUSTOMER, PARTY = (f"http://e.com/o#{n}" for n in ("Order", "Invoice", "Customer", "Party"))

TTL = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix o: <http://e.com/o#> .
o:Party a owl:Class ; rdfs:label "Party" .
o:Customer a owl:Class ; rdfs:subClassOf o:Party ; rdfs:label "Customer" ; rdfs:comment "Buys things." .
o:Order a owl:Class ; rdfs:label "Order" .
o:Invoice a owl:Class .
o:placedBy a owl:ObjectProperty ; rdfs:domain o:Order ; rdfs:range o:Party ; rdfs:label "placed by" .
o:about a owl:ObjectProperty ; rdfs:domain [ owl:unionOf ( o:Order o:Invoice ) ] .
o:total a owl:DatatypeProperty ; rdfs:domain o:Order ; rdfs:range xsd:decimal ; rdfs:label "total" .
"""


def test_classes_labels_parents():
    tb = load_tbox(TTL)
    c = tb.classes["http://e.com/o#Customer"]
    assert (c.label, c.description) == ("Customer", "Buys things.")
    assert c.parents == {"http://e.com/o#Party"}
    assert tb.classes["http://e.com/o#Invoice"].label == "Invoice"  # local-name fallback


def test_fits_honours_subclass_range_and_union_domain():
    tb = load_tbox(TTL)
    placed = tb.object_props["http://e.com/o#placedBy"]
    assert tb.fits(placed, "http://e.com/o#Order", "http://e.com/o#Customer")  # Customer ⊑ Party
    assert not tb.fits(placed, "http://e.com/o#Customer", "http://e.com/o#Order")
    about = tb.object_props["http://e.com/o#about"]
    assert about.domain == {"http://e.com/o#Order", "http://e.com/o#Invoice"}
    assert tb.fits(about, "http://e.com/o#Invoice", "http://e.com/o#Customer")  # no range: open


def test_datatype_props_and_schema_hash_is_stable():
    tb = load_tbox(TTL)
    assert "http://e.com/o#total" in tb.datatype_props
    h1 = schema_hash(tb, {"http://e.com/o#Order"}, {"http://e.com/o#placedBy"})
    assert h1 == schema_hash(load_tbox(TTL), {"http://e.com/o#Order"}, {"http://e.com/o#placedBy"})
    assert h1 != schema_hash(tb, {"http://e.com/o#Customer"}, {"http://e.com/o#placedBy"})


def test_rdfs_class_skos_label_and_percent_decoded_fallback():
    tb = load_tbox(
        PREFIXES
        + """
o:Plain a rdfs:Class .
o:Pref a owl:Class ; skos:prefLabel "Preferred" .
o:Order%20Line a owl:Class .
<http://e.com/o#고객> a owl:Class .
o:rel%20ation a owl:ObjectProperty .
"""
    )
    assert tb.classes["http://e.com/o#Plain"].label == "Plain"
    assert tb.classes["http://e.com/o#Pref"].label == "Preferred"
    # The label is the IRI tail, percent-decoded: not the sanitized local name used in instance IRIs.
    assert tb.classes["http://e.com/o#Order%20Line"].label == "Order Line"
    assert tb.classes["http://e.com/o#고객"].label == "고객"
    assert tb.object_props["http://e.com/o#rel%20ation"].label == "rel ation"


def test_subclass_cycle_terminates_and_owl_thing_is_not_a_class():
    tb = load_tbox(
        PREFIXES
        + """
o:A a owl:Class ; rdfs:subClassOf o:B .
o:B a owl:Class ; rdfs:subClassOf o:A, owl:Thing .
owl:Thing a owl:Class .
"""
    )
    assert "http://www.w3.org/2002/07/owl#Thing" not in tb.classes
    assert tb.classes["http://e.com/o#B"].parents == {"http://e.com/o#A"}
    assert {"http://e.com/o#A", "http://e.com/o#B"} <= tb.ancestors("http://e.com/o#A")


def test_ancestors_always_include_the_top_classes():
    tb = load_tbox(TTL)
    top = {"http://www.w3.org/2002/07/owl#Thing", "http://www.w3.org/2000/01/rdf-schema#Resource"}
    assert top <= tb.ancestors(ORDER)
    assert top <= tb.ancestors("http://nowhere/Unknown")


def test_owl_thing_and_rdfs_resource_never_exclude():
    tb = load_tbox(
        PREFIXES
        + """
o:Order a owl:Class . o:Customer a owl:Class .
o:anyDomain a owl:ObjectProperty ; rdfs:domain owl:Thing ; rdfs:range rdfs:Resource .
o:thingOrOrder a owl:ObjectProperty ; rdfs:domain [ owl:unionOf ( owl:Thing o:Order ) ] .
"""
    )
    any_domain = tb.object_props["http://e.com/o#anyDomain"]
    assert tb.fits(any_domain, CUSTOMER, ORDER)
    assert tb.fits(any_domain, "http://nowhere/Unknown", CUSTOMER)
    assert tb.fits(tb.object_props["http://e.com/o#thingOrOrder"], CUSTOMER, None)


def test_a_foundational_domain_or_range_outside_the_tbox_is_open():
    tb = load_tbox(
        PREFIXES
        + """
o:Order a owl:Class . o:Customer a owl:Class .
o:placedBy a owl:ObjectProperty ; rdfs:domain o:Order ; rdfs:range fibo:Agent .
o:heldBy a owl:ObjectProperty ; rdfs:domain fibo:Agent ; rdfs:range o:Customer .
o:mixed a owl:ObjectProperty ; rdfs:domain [ owl:unionOf ( o:Order fibo:Agent ) ] .
"""
    )
    placed = tb.object_props["http://e.com/o#placedBy"]
    assert placed.range == {"http://f.com/fibo#Agent"}
    assert tb.fits(placed, ORDER, CUSTOMER)  # range unknown here: open
    assert not tb.fits(placed, CUSTOMER, CUSTOMER)  # domain is still evaluated
    held = tb.object_props["http://e.com/o#heldBy"]
    assert tb.fits(held, ORDER, CUSTOMER)  # domain unknown here: open
    assert not tb.fits(held, ORDER, ORDER)  # range is still evaluated
    # One unknown member opens the whole union, even though Customer is not Order.
    assert tb.fits(tb.object_props["http://e.com/o#mixed"], CUSTOMER, None)


def test_nested_unions_are_flattened():
    tb = load_tbox(
        PREFIXES
        + """
o:Order a owl:Class . o:Invoice a owl:Class . o:Customer a owl:Class . o:Party a owl:Class .
o:p a owl:ObjectProperty ;
    rdfs:domain [ owl:unionOf ( o:Order [ owl:unionOf ( o:Invoice [ owl:unionOf ( o:Customer ) ] ) ] ) ] .
"""
    )
    p = tb.object_props["http://e.com/o#p"]
    assert p.domain == {ORDER, INVOICE, CUSTOMER}
    assert tb.fits(p, CUSTOMER, None)
    assert not tb.fits(p, PARTY, None)


def test_a_union_with_a_restriction_or_an_intersection_is_open():
    tb = load_tbox(
        PREFIXES
        + """
o:Order a owl:Class . o:Invoice a owl:Class . o:Customer a owl:Class . o:Party a owl:Class .
o:viaRestriction a owl:ObjectProperty ;
    rdfs:domain [ owl:unionOf ( o:Order [ a owl:Restriction ; owl:onProperty o:x ; owl:someValuesFrom o:Invoice ] ) ] .
o:viaNestedRestriction a owl:ObjectProperty ;
    rdfs:domain [ owl:unionOf ( o:Order [ owl:unionOf (
        o:Invoice [ a owl:Restriction ; owl:onProperty o:x ; owl:hasValue 1 ] ) ] ) ] .
o:viaIntersection a owl:ObjectProperty ;
    rdfs:domain [ owl:intersectionOf ( o:Order o:Invoice ) ] ; rdfs:range [ owl:intersectionOf ( o:Order o:Invoice ) ] .
o:viaSecondDomain a owl:ObjectProperty ;
    rdfs:domain o:Order, [ a owl:Restriction ; owl:onProperty o:x ; owl:hasValue 1 ] .
"""
    )
    for name in ("viaRestriction", "viaNestedRestriction", "viaIntersection", "viaSecondDomain"):
        p = tb.object_props[f"http://e.com/o#{name}"]
        assert p.domain == frozenset(), name
        assert tb.fits(p, PARTY, PARTY), name
    assert tb.object_props["http://e.com/o#viaIntersection"].range == frozenset()


def test_several_domain_triples_are_read_as_a_union():
    tb = load_tbox(
        PREFIXES
        + """
o:Order a owl:Class . o:Invoice a owl:Class . o:Party a owl:Class .
o:p a owl:ObjectProperty ; rdfs:domain o:Order, o:Invoice .
"""
    )
    p = tb.object_props["http://e.com/o#p"]
    assert p.domain == {ORDER, INVOICE}
    assert tb.fits(p, ORDER, None) and tb.fits(p, INVOICE, None)
    assert not tb.fits(p, PARTY, None)


def test_tbox_order_does_not_depend_on_hash_seed():
    tb = load_tbox(TTL)
    assert list(tb.classes) == sorted(tb.classes)
    assert list(tb.object_props) == sorted(tb.object_props)
    assert list(tb.datatype_props) == sorted(tb.datatype_props)
