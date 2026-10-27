# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import pytest
from coa_ontology.studio import iris

pytestmark = [pytest.mark.unit]

NS = "11111111-2222-3333-4444-555555555555"
PREFIX = f"http://coa.amazon.com/namespace/{NS}/ontology/induced#"
DB = iris.scheme_for(NS, PREFIX, "STRUCTURED")
DOC = iris.scheme_for(NS, PREFIX, "UNSTRUCTURED")


def _never(_: str) -> bool:
    return False


def test_database_style_uses_the_inducers_pascal_and_camel_case():
    assert iris.mint_class_iri(DB, "order item", _never) == f"{PREFIX}OrderItem"
    customer = f"{PREFIX}Customer"
    assert iris.mint_property_iri(DB, "e-mail address", "DatatypeProperty", customer, _never) == (
        f"{PREFIX}customer_eMailAddress"
    )


def test_document_style_uses_the_document_inducers_path_template():
    base = f"http://coa.amazon.com/namespace/{NS}/ontology/unstructured/"
    assert iris.mint_class_iri(DOC, "Financial Instrument", _never) == f"{base}class/Financial-Instrument"
    company = f"{base}class/Company"
    assert iris.mint_property_iri(DOC, "works for", "ObjectProperty", company, _never) == (
        f"{base}Company/ObjectProperty/works-for"
    )


def test_a_korean_label_keeps_its_letters_and_a_taken_iri_gets_a_suffix():
    first = iris.mint_class_iri(DB, "고객", _never)
    assert first == f"{PREFIX}고객"
    assert iris.mint_class_iri(DB, "고객", lambda iri: iri == first) == f"{PREFIX}고객_2"
    assert iris.mint_class_iri(DB, "고객", lambda iri: iri in {first, f"{first}_2"}) == f"{PREFIX}고객_3"


def test_the_space_covers_both_inducers_and_a_custom_ontology_prefix():
    custom = iris.scheme_for(NS, "https://example.com/onto/sales", "STRUCTURED")
    assert custom.ontology_prefix == "https://example.com/onto/sales#"
    assert custom.in_space("https://example.com/onto/sales#Order")
    assert custom.in_space(f"http://coa.amazon.com/namespace/{NS}/ontology/unstructured/class/X")
    assert not custom.in_space("https://spec.edmcouncil.org/fibo/ontology/FND/Agent")
    assert not custom.in_space(f"http://coa.amazon.com/namespace/{NS}-other/ontology/induced#X")
    with pytest.raises(iris.IriError, match="outside"):
        iris.check_in_space(custom, "https://other.example/onto#Order", "iri")
    with pytest.raises(iris.IriError, match="not a valid IRI"):
        iris.check_in_space(custom, "https://example.com/onto/sales#Or der", "iri")


def test_datatype_ranges_accept_xsd_builtins_by_prefix_or_full_iri():
    assert iris.datatype_iri("xsd:dateTime") == "http://www.w3.org/2001/XMLSchema#dateTime"
    assert iris.datatype_iri("http://www.w3.org/2001/XMLSchema#dateTime") == "http://www.w3.org/2001/XMLSchema#dateTime"
    assert iris.datatype_iri("http://www.w3.org/2000/01/rdf-schema#Literal").endswith("#Literal")
    assert iris.datatype_iri("http://www.w3.org/1999/02/22-rdf-syntax-ns#langString").endswith("#langString")
    assert iris.datatype_iri("http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral").endswith("#PlainLiteral")


def test_datatype_ranges_are_case_sensitive():
    with pytest.raises(iris.IriError, match="not an XSD datatype"):
        iris.datatype_iri("xsd:datetime")


def test_datatype_ranges_reject_names_that_are_not_xsd_builtins():
    for bad in ("xsd:foo", "xsd:", f"{PREFIX}Customer", "string"):
        with pytest.raises(iris.IriError, match="not an XSD datatype"):
            iris.datatype_iri(bad)


def test_labels_need_a_letter_or_digit():
    assert iris.has_letter_or_digit("고객") and iris.has_letter_or_digit("A1")
    assert not iris.has_letter_or_digit(" !!! ")


def test_minting_refuses_a_label_with_no_letter_or_digit_instead_of_falling_back():
    customer = f"{PREFIX}Customer"
    for scheme in (DB, DOC):
        for label in ("", "  ", "!!!"):
            with pytest.raises(iris.IriError, match="letter or digit"):
                iris.mint_class_iri(scheme, label, _never)
            with pytest.raises(iris.IriError, match="letter or digit"):
                iris.mint_property_iri(scheme, label, "DatatypeProperty", customer, _never)
