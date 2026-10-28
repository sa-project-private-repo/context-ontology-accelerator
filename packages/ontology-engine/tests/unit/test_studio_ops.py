# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import logging
from typing import get_args
from unittest.mock import patch

import pytest
from coa_common.constants import VOCAB_URI
from coa_ontology.studio import messages, ops
from coa_ontology.versions import snapshot
from pydantic import ValidationError
from rdflib import OWL, RDF, RDFS, Literal, URIRef
from rdflib.namespace import SKOS, XSD

from .studio_fakes import (
    CUSTOMER,
    EMAIL,
    FIBO,
    ITEM,
    ONTO,
    ORDER,
    PARTY,
    PLACED_BY,
    SCHEME,
    TOTAL,
    VIP,
    draft,
)

pytestmark = [pytest.mark.unit]
U = URIRef


def _apply(*raw: dict, d: ops.Draft | None = None) -> tuple[ops.Draft, ops.Context]:
    return ops.apply_ops(d or draft(), ops.USER_OPS.validate_python(list(raw)), SCHEME)


def _nt(d: ops.Draft) -> tuple[str, str]:
    return snapshot.canonical_nt(d.tbox), snapshot.canonical_nt(d.r2rml)


def _undo(before: ops.Draft, after: ops.Draft) -> ops.Draft:
    b, a = _nt(before), _nt(after)
    inverse = ops.RestoreTriples(
        op="restore_triples", tbox=ops.line_delta(b[0], a[0]), r2rml=ops.line_delta(b[1], a[1])
    )
    restored, _ = ops.apply_ops(after, [inverse], SCHEME)
    return restored


ROUND_TRIP = [
    {"op": "add_class", "label": "Supplier", "parents": [PARTY], "comment": "Sells items"},
    {"op": "set_label", "iri": CUSTOMER, "label": "Client"},
    {"op": "set_comment", "iri": CUSTOMER, "comment": ""},
    {"op": "change_iri", "iri": CUSTOMER, "newIri": f"{ONTO}Client"},
    {"op": "delete_class", "iri": CUSTOMER, "cascade": "detach"},
    {"op": "delete_class", "iri": CUSTOMER, "cascade": "delete"},
    {"op": "merge_classes", "source": VIP, "target": CUSTOMER},
    {"op": "add_parent", "iri": ITEM, "parent": PARTY},
    {"op": "remove_parent", "iri": CUSTOMER, "parent": PARTY},
    {"op": "add_object_property", "label": "contains", "domain": ORDER, "range": ITEM},
    {"op": "add_datatype_property", "label": "sku", "domain": ITEM, "range": "xsd:string"},
    {"op": "update_property", "iri": PLACED_BY, "range": PARTY},
    {"op": "delete_property", "iri": PLACED_BY},
    {"op": "set_match", "iri": ITEM, "target": "https://schema.org/Product", "kind": "close"},
    {"op": "replace_datatype", "from": "xsd:float", "to": "xsd:double"},  # the matchConfidence literal
]


@pytest.mark.parametrize("raw", ROUND_TRIP, ids=[r["op"] + str(i) for i, r in enumerate(ROUND_TRIP)])
def test_every_op_changes_the_draft_and_its_inverse_restores_it_exactly(raw):
    before = draft()
    after, _ = _apply(raw, d=before)
    assert _nt(after) != _nt(before)
    assert _nt(_undo(before, after)) == _nt(before)


@pytest.mark.parametrize("raw", ROUND_TRIP, ids=[r["op"] + str(i) for i, r in enumerate(ROUND_TRIP)])
def test_every_op_leaves_a_publishable_mapping(raw):
    after, _ = _apply(raw)
    snapshot.check_r2rml(after.tbox, after.r2rml)


# Each op's line of the change history on the sample draft: its English text, and that sentence's code and values.
# A term is named by its label in the draft before the op (VipCustomer is "VIP customer"), else by its IRI tail.
SUMMARIES = [
    (ROUND_TRIP[0], "Added class Supplier", "ADD_CLASS", {"label": "Supplier"}),
    (ROUND_TRIP[1], "Renamed Customer to Client", "SET_LABEL", {"term": "Customer", "label": "Client"}),
    (ROUND_TRIP[2], "Changed the description of Customer", "SET_COMMENT", {"term": "Customer"}),
    (
        ROUND_TRIP[3],
        "Changed the IRI of Customer to Client",
        "CHANGE_IRI",
        {"term": "Customer", "newTerm": "Client"},
    ),
    (ROUND_TRIP[4], "Deleted class Customer (detach its properties)", "DELETE_CLASS_DETACH", {"term": "Customer"}),
    (ROUND_TRIP[5], "Deleted class Customer (delete its properties)", "DELETE_CLASS_DELETE", {"term": "Customer"}),
    (
        ROUND_TRIP[6],
        "Merged VIP customer into Customer",
        "MERGE_CLASSES",
        {"source": "VIP customer", "target": "Customer"},
    ),
    (ROUND_TRIP[7], "Made Item a subclass of Party", "ADD_PARENT", {"term": "Item", "parent": "Party"}),
    (
        ROUND_TRIP[8],
        "Removed Party from the parents of Customer",
        "REMOVE_PARENT",
        {"term": "Customer", "parent": "Party"},
    ),
    (
        ROUND_TRIP[9],
        "Added relationship contains from Order to Item",
        "ADD_OBJECT_PROPERTY",
        {"label": "contains", "domain": "Order", "range": "Item"},
    ),
    (ROUND_TRIP[10], "Added attribute sku to Item", "ADD_DATATYPE_PROPERTY", {"label": "sku", "domain": "Item"}),
    (
        ROUND_TRIP[11],
        "Changed the range of placed by",
        "UPDATE_PROPERTY",
        {"fields": "range", "term": "placed by"},
    ),
    # An attribute's range is its datatype, as the Inspector calls it.
    (
        {"op": "update_property", "iri": TOTAL, "range": "xsd:double", "label": "amount"},
        "Changed the datatype, label of total",
        "UPDATE_PROPERTY",
        {"fields": "datatype, label", "term": "total"},
    ),
    (
        {"op": "update_property", "iri": EMAIL, "domain": PARTY, "label": "mail", "comment": "Where to write"},
        "Changed the domain, label, comment of email",
        "UPDATE_PROPERTY",
        {"fields": "domain, label, comment", "term": "email"},
    ),
    (ROUND_TRIP[12], "Deleted property placed by", "DELETE_PROPERTY", {"term": "placed by"}),
    (
        ROUND_TRIP[13],
        "Grounded Item to Product (close)",
        "SET_CLOSE_MATCH",
        {"term": "Item", "target": "Product"},
    ),
    (
        {"op": "set_match", "iri": ITEM, "target": "https://schema.org/Product", "kind": "exact"},
        "Grounded Item to Product (exact)",
        "SET_EXACT_MATCH",
        {"term": "Item", "target": "Product"},
    ),
    (
        {"op": "set_match", "iri": ITEM, "kind": "none"},
        "Removed the grounding of Item",
        "REMOVE_MATCH",
        {"term": "Item"},
    ),
    (
        ROUND_TRIP[14],
        "Replaced the datatype xsd:float with xsd:double",
        "REPLACE_DATATYPE",
        {"old": "xsd:float", "new": "xsd:double"},
    ),
    ({"op": "restore_triples"}, "Reverted a change", "RESTORE_TRIPLES", {}),
]


@pytest.mark.parametrize(("raw", "text", "code", "params"), SUMMARIES, ids=[s[2] for s in SUMMARIES])
def test_every_op_gives_its_history_line_as_a_code_and_values(raw, text, code, params):
    """The history's lines were English text only, which the web app showed in every language."""
    (op,) = ops.ANY_OPS.validate_python([raw])
    assert op.summary_message(draft().tbox) == {"code": code, "params": params}
    assert messages.english(op.summary_message(draft().tbox)) == text


def test_every_user_op_has_a_coded_history_line_above():
    """An op added without a case here could leave its history line without a code."""
    names = {get_args(cls.model_fields["op"].annotation)[0] for cls in get_args(ops._USER_OP_TYPES)}
    assert len(names) == 14 and names <= {raw["op"] for raw, *_ in SUMMARIES}


def test_a_change_set_names_each_term_by_the_label_it_has_before_its_op():
    """The lines named a class by its IRI tail ("Renamed Premium-customer to Customer"), not by its label."""
    premium, vendor, unlabelled = f"{ONTO}Premium-customer", f"{ONTO}Vendor", f"{ONTO}Unlabelled"
    d = draft()
    for iri in (premium, unlabelled):
        d.tbox.add((U(iri), RDF.type, OWL.Class))
    d.tbox.add((U(premium), RDFS.label, Literal("Premium customer")))
    _, ctx = _apply(
        {"op": "set_label", "iri": premium, "label": "Customer"},
        {"op": "add_class", "label": "Supplier", "iri": vendor},
        {"op": "add_parent", "iri": vendor, "parent": PARTY},  # added by the op before, with its label
        {"op": "merge_classes", "source": VIP, "target": premium},  # relabelled by the first op
        {"op": "remove_parent", "iri": CUSTOMER, "parent": FIBO},  # the draft has no label for it
        {"op": "delete_property", "iri": PLACED_BY},
        {"op": "set_comment", "iri": unlabelled, "comment": "No label"},
        d=d,
    )
    assert ctx.summary_messages == [
        {"code": "SET_LABEL", "params": {"term": "Premium customer", "label": "Customer"}},
        {"code": "ADD_CLASS", "params": {"label": "Supplier"}},
        {"code": "ADD_PARENT", "params": {"term": "Supplier", "parent": "Party"}},
        {"code": "MERGE_CLASSES", "params": {"source": "VIP customer", "target": "Customer"}},
        {"code": "REMOVE_PARENT", "params": {"term": "Customer", "parent": "Agent"}},
        {"code": "DELETE_PROPERTY", "params": {"term": "placed by"}},
        {"code": "SET_COMMENT", "params": {"term": "Unlabelled"}},
    ]
    assert messages.english(ctx.summary_messages[0]) == "Renamed Premium customer to Customer"


def test_a_history_line_names_a_malformed_iri_by_its_text_and_the_op_refuses_it(caplog):
    (op,) = ops.USER_OPS.validate_python([{"op": "delete_class", "iri": "not an iri"}])
    with caplog.at_level(logging.WARNING):
        assert op.summary_message(draft().tbox) == {"code": "DELETE_CLASS_DETACH", "params": {"term": "not an iri"}}
    assert "does not look like a valid URI" not in caplog.text  # rdflib's warning for a URIRef it was given
    with pytest.raises(ops.ChangeSetError, match="not a valid IRI"):
        _apply({"op": "delete_class", "iri": "not an iri"})


def test_the_same_ops_on_the_same_draft_give_the_same_bytes():
    raw = [ROUND_TRIP[0], ROUND_TRIP[9], ROUND_TRIP[5]]
    a, _ = _apply(*raw)
    b, _ = _apply(*raw)
    assert snapshot.sha256_hex(_nt(a)[0]) == snapshot.sha256_hex(_nt(b)[0])
    assert _nt(a) == _nt(b)


def test_add_class_mints_from_the_label_and_reports_it():
    after, ctx = _apply({"op": "add_class", "label": "order item"}, {"op": "add_class", "label": "Order Item"})
    assert ctx.created == [f"{ONTO}OrderItem", f"{ONTO}OrderItem_2"]
    assert (U(f"{ONTO}OrderItem_2"), RDF.type, OWL.Class) in after.tbox
    assert any("also labelled" in w for w in ctx.warnings)


def test_add_class_refuses_an_existing_or_foreign_iri_and_a_bad_parent():
    for raw, match in (
        ({"op": "add_class", "label": "X", "iri": CUSTOMER}, "already exists"),
        ({"op": "add_class", "label": "X", "iri": "https://other.example/X"}, "outside"),
        ({"op": "add_class", "label": "X", "parents": [EMAIL]}, "not a class"),
        ({"op": "add_class", "label": "!!"}, "letter or digit"),
    ):
        with pytest.raises(ops.ChangeSetError, match=match):
            _apply(raw)


@pytest.mark.parametrize("label", ["", "  ", "!!!"])
def test_a_label_without_a_letter_or_digit_is_a_validation_error_for_every_adding_op(label):
    for raw in (
        {"op": "add_class", "label": label},
        {"op": "add_object_property", "label": label, "domain": ORDER, "range": ITEM},
        {"op": "add_datatype_property", "label": label, "domain": ITEM, "range": "xsd:string"},
    ):
        with pytest.raises(ops.ChangeSetError, match="letter or digit"):
            _apply(raw)


def test_set_label_replaces_only_the_same_language():
    d = draft()
    d.tbox.add((U(CUSTOMER), RDFS.label, Literal("고객", lang="ko")))
    after, _ = _apply({"op": "set_label", "iri": CUSTOMER, "label": "Client"}, d=d)
    assert set(after.tbox.objects(U(CUSTOMER), RDFS.label)) == {Literal("Client"), Literal("고객", lang="ko")}


def test_change_iri_renames_in_both_graphs_and_refuses_a_taken_iri():
    after, _ = _apply({"op": "change_iri", "iri": CUSTOMER, "newIri": f"{ONTO}Client"})
    assert not ops.mentioned(after, CUSTOMER)
    assert (None, snapshot.RR["class"], U(f"{ONTO}Client")) in after.r2rml
    assert (U(PLACED_BY), RDFS.range, U(f"{ONTO}Client")) in after.tbox
    with pytest.raises(ops.ChangeSetError, match="already exists"):
        _apply({"op": "change_iri", "iri": CUSTOMER, "newIri": ORDER})


def test_delete_class_reparents_detaches_and_drops_its_maps_and_the_joins_into_them():
    after, _ = _apply({"op": "delete_class", "iri": CUSTOMER, "cascade": "detach"})
    assert set(after.tbox.objects(U(VIP), RDFS.subClassOf)) == {U(PARTY), U(FIBO)}
    assert (U(PLACED_BY), RDF.type, OWL.ObjectProperty) in after.tbox
    assert (U(PLACED_BY), RDFS.range, None) not in after.tbox
    assert (U(EMAIL), RDFS.domain, None) not in after.tbox
    assert (U(f"{ONTO}TriplesMap_Customer"), None, None) not in after.r2rml
    assert (U(f"{ONTO}TriplesMap_Order/POM_CustomerId"), None, None) not in after.r2rml
    assert (U(f"{ONTO}TriplesMap_Order/POM_Total"), None, None) in after.r2rml
    # The restriction on Order mentioned Customer: it goes whole.
    assert not [o for o in after.tbox.objects(U(ORDER), RDFS.subClassOf)]


def test_delete_class_with_delete_cascade_removes_its_properties():
    after, _ = _apply({"op": "delete_class", "iri": CUSTOMER, "cascade": "delete"})
    assert not ops.mentioned(after, PLACED_BY) and not ops.mentioned(after, EMAIL)
    assert ops.mentioned(after, TOTAL)


def test_delete_class_gives_a_mapped_property_moved_up_to_it_the_class_its_mapping_reads():
    nickname = U(f"{ONTO}party_nickname")
    before, _ = _apply(
        {"op": "update_property", "iri": EMAIL, "domain": PARTY},
        {"op": "update_property", "iri": PLACED_BY, "range": PARTY},
        {"op": "add_datatype_property", "label": "nickname", "domain": PARTY, "range": "xsd:string"},
    )
    after, ctx = _apply({"op": "delete_class", "iri": PARTY, "cascade": "detach"}, d=before)
    assert set(after.tbox.objects(U(EMAIL), RDFS.domain)) == {U(CUSTOMER)}
    assert set(after.tbox.objects(U(PLACED_BY), RDFS.domain)) == {U(ORDER)}
    assert set(after.tbox.objects(U(PLACED_BY), RDFS.range)) == {U(CUSTOMER)}
    assert (nickname, RDF.type, OWL.DatatypeProperty) in after.tbox
    assert (nickname, RDFS.domain, None) not in after.tbox  # no mapping says where it belongs
    assert ctx.warnings == [
        "Party was the domain of customer_email, which is mapped from table customer; its domain is now Customer.",
        "Party was the range of order_placedBy, which joins to Customer in the mapping; its range is now Customer.",
    ]
    snapshot.check_r2rml(after.tbox, after.r2rml)
    assert _nt(_undo(before, after)) == _nt(before)
    # Deleting the mapped class itself takes its mapping away, so its properties are left without it.
    _, ctx = _apply({"op": "delete_class", "iri": CUSTOMER, "cascade": "detach"})
    assert ctx.warnings == []


def test_delete_class_gives_back_only_the_classes_the_mapping_still_reads():
    d = draft()
    d.r2rml.add((U(f"{ONTO}TriplesMap_Customer/SubjectMap"), snapshot.RR["class"], U(PARTY)))  # the map reads both
    before, _ = _apply({"op": "update_property", "iri": EMAIL, "domain": PARTY}, d=d)
    after, ctx = _apply({"op": "delete_class", "iri": PARTY, "cascade": "detach"}, d=before)
    assert set(after.tbox.objects(U(EMAIL), RDFS.domain)) == {U(CUSTOMER)}
    assert ctx.warnings == [
        "Party was the domain of customer_email, which is mapped from table customer; its domain is now Customer."
    ]
    # A map that reads no table is named instead.
    logical_table = next(before.r2rml.objects(U(f"{ONTO}TriplesMap_Customer"), snapshot.RR.logicalTable))
    before.r2rml.remove((logical_table, snapshot.RR.tableName, None))
    before.r2rml.add((logical_table, snapshot.RR.sqlQuery, Literal("SELECT id, email FROM customer")))
    _, ctx = _apply({"op": "delete_class", "iri": PARTY, "cascade": "detach"}, d=before)
    assert ctx.warnings == [
        "Party was the domain of customer_email, which is mapped by TriplesMap_Customer; its domain is now Customer."
    ]
    # A property that keeps another domain keeps it alone.
    d = draft()
    d.tbox.add((U(EMAIL), RDFS.domain, U(PARTY)))
    after, ctx = _apply({"op": "delete_class", "iri": PARTY, "cascade": "detach"}, d=d)
    assert set(after.tbox.objects(U(EMAIL), RDFS.domain)) == {U(CUSTOMER)} and ctx.warnings == []


def test_delete_class_warns_only_about_a_class_it_gave_back_that_the_change_set_keeps():
    """Delete selected sends one delete_class per class, so the parent can go first and the mapped class next."""
    before, _ = _apply({"op": "update_property", "iri": EMAIL, "domain": PARTY})
    delete_party = {"op": "delete_class", "iri": PARTY, "cascade": "detach"}
    after, ctx = _apply(delete_party, {"op": "delete_class", "iri": CUSTOMER, "cascade": "detach"}, d=before)
    assert (U(EMAIL), RDFS.domain, None) not in after.tbox and not ops.property_maps(after.r2rml, U(EMAIL))
    assert ctx.warnings == []
    _, ctx = _apply(delete_party, {"op": "delete_property", "iri": EMAIL}, d=before)
    assert ctx.warnings == []
    # A later op that leaves the class it gave back keeps the warning.
    _, ctx = _apply(delete_party, {"op": "delete_class", "iri": VIP, "cascade": "detach"}, d=before)
    assert ctx.warnings == [
        "Party was the domain of customer_email, which is mapped from table customer; its domain is now Customer."
    ]


def test_merge_classes_moves_references_and_keeps_the_label_as_alt_label():
    after, _ = _apply({"op": "merge_classes", "source": VIP, "target": CUSTOMER})
    assert not ops.mentioned(after, VIP)
    assert Literal("VIP customer") in set(after.tbox.objects(U(CUSTOMER), SKOS.altLabel))
    assert (U(CUSTOMER), RDFS.subClassOf, U(CUSTOMER)) not in after.tbox


SUPPLIER = f"{ONTO}Supplier"
SUPPLIER_TBOX = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<{SUPPLIER}> a owl:Class ; rdfs:label "Supplier" ; rdfs:comment "Sells goods", "판매자"@ko ;
    rdfs:subClassOf <{PARTY}>, <{FIBO}>,
        [ a owl:Restriction ; owl:onProperty <{PLACED_BY}> ; owl:someValuesFrom <{PARTY}> ] ;
    skos:exactMatch <{FIBO}> ; owl:equivalentClass <https://schema.org/Organization> ; owl:disjointWith <{ORDER}> .
"""
SUPPLIER_R2RML = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
<{ONTO}TriplesMap_Supplier> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "supplier" ] ;
    rr:subjectMap <{ONTO}TriplesMap_Supplier/SubjectMap> .
<{ONTO}TriplesMap_Supplier/SubjectMap> rr:class <{SUPPLIER}> ; rr:template "s/{{id}}" .
"""


def _with_supplier() -> ops.Draft:
    d = draft()
    d.tbox.parse(data=SUPPLIER_TBOX, format="turtle")
    d.r2rml.parse(data=SUPPLIER_R2RML, format="turtle")
    return d


def test_merge_classes_moves_the_sources_own_axioms_and_mapping_onto_the_target():
    before = _with_supplier()
    after, _ = _apply({"op": "merge_classes", "source": SUPPLIER, "target": ITEM}, d=before)
    item = U(ITEM)
    assert not ops.mentioned(after, SUPPLIER)
    # Parents, grounding and the restriction the source carried are now the target's.
    assert set(after.tbox.objects(item, RDFS.subClassOf)) >= {U(PARTY), U(FIBO)}
    assert (item, SKOS.exactMatch, U(FIBO)) in after.tbox
    assert (item, OWL.equivalentClass, U("https://schema.org/Organization")) in after.tbox
    assert (item, OWL.disjointWith, U(ORDER)) in after.tbox
    restrictions = [o for o in after.tbox.objects(item, RDFS.subClassOf) if not isinstance(o, URIRef)]
    assert [set(after.tbox.objects(r, OWL.onProperty)) for r in restrictions] == [{U(PLACED_BY)}]
    # The target had no comment, so both of the source's move; the labels become altLabels.
    assert set(after.tbox.objects(item, RDFS.comment)) == {Literal("Sells goods"), Literal("판매자", lang="ko")}
    assert set(after.tbox.objects(item, RDFS.label)) == {Literal("Item")}
    assert set(after.tbox.objects(item, SKOS.altLabel)) == {Literal("Supplier")}
    # The source's TriplesMap now maps the target, and the mapping still publishes.
    assert (U(f"{ONTO}TriplesMap_Supplier/SubjectMap"), snapshot.RR["class"], item) in after.r2rml
    snapshot.check_r2rml(after.tbox, after.r2rml)
    assert _nt(_undo(before, after)) == _nt(before)


def test_merge_classes_warns_when_the_two_classes_are_mapped_from_different_tables():
    _, ctx = _apply({"op": "merge_classes", "source": SUPPLIER, "target": CUSTOMER}, d=_with_supplier())
    assert ctx.warnings == [
        "Supplier and Customer are mapped to tables supplier and customer; "
        "structured SQL will describe them as one table."
    ]
    # Only one of them reads a table: the merged class reads that one.
    _, ctx = _apply({"op": "merge_classes", "source": SUPPLIER, "target": ITEM}, d=_with_supplier())
    assert ctx.warnings == []
    _, ctx = _apply({"op": "merge_classes", "source": VIP, "target": CUSTOMER})
    assert ctx.warnings == []


def _merge_supplier_reading(table: str, *, supplier_source: str = "", customer_source: str = "") -> list[str]:
    """The warnings of merging Supplier into Customer once Supplier's map reads ``table``.

    A non-empty ``*_source`` is the ``coa:datasourceId`` of that class's map.
    """
    d = _with_supplier()
    supplier_map = U(f"{ONTO}TriplesMap_Supplier")
    d.r2rml.set((next(d.r2rml.objects(supplier_map, snapshot.RR.logicalTable)), snapshot.RR.tableName, Literal(table)))
    for tmap, source in ((supplier_map, supplier_source), (U(f"{ONTO}TriplesMap_Customer"), customer_source)):
        if source:
            d.r2rml.add((tmap, U(f"{VOCAB_URI}datasourceId"), Literal(source)))
    _, ctx = _apply({"op": "merge_classes", "source": SUPPLIER, "target": CUSTOMER}, d=d)
    return ctx.warnings


def test_merge_classes_warns_when_the_two_tables_share_only_their_name():
    # The name in another schema: logical_table_names qualifies a name two sources share.
    assert _merge_supplier_reading('"sales"."customer"') == [
        "Supplier and Customer are mapped to tables sales.customer and customer; "
        "structured SQL will describe them as one table."
    ]
    # The bare name in another datasource: only the datasource tells the two tables apart.
    assert _merge_supplier_reading("customer", supplier_source="ds-2", customer_source="DS#ds-1") == [
        "Supplier and Customer are mapped to tables customer [ds-2] and customer [ds-1]; "
        "structured SQL will describe them as one table."
    ]


def test_merge_classes_does_not_warn_when_both_classes_read_the_same_table():
    assert _merge_supplier_reading('"customer"') == []  # quoting the name does not make another table
    assert _merge_supplier_reading("customer", supplier_source="ds-1", customer_source="DS#ds-1") == []


def test_merge_classes_drops_a_source_comment_in_a_language_the_target_already_has():
    before = _with_supplier()
    after, _ = _apply({"op": "merge_classes", "source": SUPPLIER, "target": CUSTOMER}, d=before)
    # Customer has an untagged comment: the untagged source comment goes, the Korean one is kept.
    assert set(after.tbox.objects(U(CUSTOMER), RDFS.comment)) == {Literal("A buyer"), Literal("판매자", lang="ko")}
    assert _nt(_undo(before, after)) == _nt(before)


def test_merge_deduplicates_an_axiom_both_classes_carried():
    before = _with_supplier()
    restriction = f"[ a owl:Restriction ; owl:onProperty <{PLACED_BY}> ; owl:someValuesFrom <{PARTY}> ]"
    before.tbox.parse(
        data=f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        <{ITEM}> rdfs:subClassOf {restriction} .""",
        format="turtle",
    )
    after, _ = _apply({"op": "merge_classes", "source": SUPPLIER, "target": ITEM}, d=before)
    assert (U(ITEM), SKOS.exactMatch, U(FIBO)) in after.tbox  # the rest of the source's axioms did arrive
    restrictions = [o for o in after.tbox.objects(U(ITEM), RDFS.subClassOf) if not isinstance(o, URIRef)]
    assert len(restrictions) == 1
    assert _nt(_undo(before, after)) == _nt(before)


def test_merge_that_would_make_a_cycle_is_refused():
    # Item ⊑ VipCustomer and Gadget ⊑ Item: merging VipCustomer into Gadget gives Item ⊑ Gadget ⊑ Item.
    d, _ = _apply(
        {"op": "add_parent", "iri": ITEM, "parent": VIP},
        {"op": "add_class", "label": "Gadget", "parents": [ITEM]},
    )
    with pytest.raises(ops.ChangeSetError, match="cycle"):
        _apply({"op": "merge_classes", "source": VIP, "target": f"{ONTO}Gadget"}, d=d)


def test_parents_refuse_cycles_duplicates_and_missing_edges():
    for raw, match in (
        ({"op": "add_parent", "iri": PARTY, "parent": VIP}, "cycle"),
        ({"op": "add_parent", "iri": CUSTOMER, "parent": CUSTOMER}, "cycle"),
        ({"op": "add_parent", "iri": CUSTOMER, "parent": PARTY}, "already a parent"),
        ({"op": "remove_parent", "iri": ITEM, "parent": PARTY}, "not a parent"),
    ):
        with pytest.raises(ops.ChangeSetError, match=match):
            _apply(raw)


def test_properties_need_draft_classes_and_a_datatype_range():
    for raw, match in (
        ({"op": "add_object_property", "label": "x", "domain": ORDER, "range": FIBO}, "not a class"),
        ({"op": "add_datatype_property", "label": "x", "domain": ORDER, "range": CUSTOMER}, "XSD"),
        ({"op": "update_property", "iri": TOTAL, "range": CUSTOMER}, "XSD"),
        ({"op": "update_property", "iri": PLACED_BY}, "at least one"),
        ({"op": "delete_property", "iri": CUSTOMER}, "not a relationship or attribute"),
    ):
        with pytest.raises(ops.ChangeSetError, match=match):
            _apply(raw)


def test_a_term_of_another_kind_is_refused_in_the_console_s_words():
    """The refusal named the kinds by their tokens: 'is not a objectProperty or datatypeProperty of the draft'."""
    ghost = f"{ONTO}Ghost"
    for raw, text in (
        ({"op": "delete_property", "iri": CUSTOMER}, f"iri {CUSTOMER} is not a relationship or attribute of the draft"),
        (
            {"op": "set_label", "iri": ghost, "label": "Ghost"},
            f"iri {ghost} is not a class, relationship or attribute of the draft",
        ),
        ({"op": "delete_class", "iri": EMAIL}, f"iri {EMAIL} is not a class of the draft"),
    ):
        with pytest.raises(ops.ChangeSetError) as raised:
            _apply(raw)
        assert raised.value.message == text


def test_update_property_refuses_a_domain_outside_the_table_its_mapping_reads():
    with pytest.raises(ops.ChangeSetError) as raised:
        _apply({"op": "update_property", "iri": EMAIL, "domain": ORDER})
    assert raised.value.message == (
        "customer_email is mapped from table customer; its domain must stay Customer or one of its parents."
    )


def test_update_property_refuses_a_datatype_other_than_the_mapped_columns():
    with pytest.raises(ops.ChangeSetError) as raised:
        _apply({"op": "update_property", "iri": TOTAL, "range": "xsd:integer"})
    assert raised.value.message == (
        "order_total is mapped from column total, typed decimal; its datatype follows the column."
    )


def test_update_property_refuses_a_range_outside_the_class_its_join_reaches():
    with pytest.raises(ops.ChangeSetError) as raised:
        _apply({"op": "update_property", "iri": PLACED_BY, "range": VIP})  # a subclass is narrower: refused
    assert raised.value.message == (
        "order_placedBy joins to Customer in the mapping; its range must stay Customer or one of its parents."
    )
    with pytest.raises(ops.ChangeSetError, match="joins to Customer"):
        _apply({"op": "update_property", "iri": PLACED_BY, "range": ITEM})


def test_update_property_lets_a_mapped_property_generalise_to_a_parent_with_a_warning():
    after, ctx = _apply(
        {"op": "update_property", "iri": EMAIL, "domain": PARTY},
        {"op": "update_property", "iri": PLACED_BY, "range": PARTY},
        {"op": "update_property", "iri": TOTAL, "range": "xsd:decimal", "label": "order total"},
    )
    assert (U(EMAIL), RDFS.domain, U(PARTY)) in after.tbox
    assert (U(PLACED_BY), RDFS.range, U(PARTY)) in after.tbox
    # SPARQL through the VKG offers a property on its domain and a join on its range only, so both leave the
    # mapped class there; NL-to-SQL places them by the mapping, which still reads customer.
    assert ctx.warnings == [
        "customer_email is mapped from table customer; with the domain Party, "
        "SPARQL through the VKG no longer offers it on Customer, but NL-to-SQL still does.",
        "order_placedBy joins to Customer in the mapping; with the range Party, "
        "SPARQL through the VKG no longer offers it as a join to Customer, but NL-to-SQL still does.",
    ]
    snapshot.check_r2rml(after.tbox, after.r2rml)
    # The mapped class itself is no generalisation.
    _, ctx = _apply(
        {"op": "update_property", "iri": EMAIL, "domain": CUSTOMER},
        {"op": "update_property", "iri": PLACED_BY, "range": CUSTOMER},
    )
    assert ctx.warnings == []


def test_update_property_refuses_a_datatype_other_than_a_mapped_constant():
    d = draft()
    om = U(f"{ONTO}TriplesMap_Customer/POM_Email/ObjectMap")
    d.r2rml.remove((om, snapshot.RR.column, None))
    d.r2rml.add((om, snapshot.RR.constant, Literal("unknown")))
    with pytest.raises(ops.ChangeSetError) as raised:
        _apply({"op": "update_property", "iri": EMAIL, "range": "xsd:integer"}, d=d)
    assert raised.value.message == "customer_email is mapped as string; its datatype follows the mapping."


def test_a_domain_refusal_names_the_map_when_the_map_reads_no_table():
    d = draft()
    logical_table = next(d.r2rml.objects(U(f"{ONTO}TriplesMap_Customer"), snapshot.RR.logicalTable))
    d.r2rml.remove((logical_table, snapshot.RR.tableName, None))
    d.r2rml.add((logical_table, snapshot.RR.sqlQuery, Literal("SELECT id, email FROM customer")))
    with pytest.raises(ops.ChangeSetError) as raised:
        _apply({"op": "update_property", "iri": EMAIL, "domain": ORDER}, d=d)
    assert raised.value.message == (
        "customer_email is mapped by TriplesMap_Customer; its domain must stay Customer or one of its parents."
    )


def test_remove_parent_refuses_the_parent_a_generalised_mapped_property_needs():
    with pytest.raises(ops.ChangeSetError) as raised:
        _apply(
            {"op": "update_property", "iri": EMAIL, "domain": PARTY},
            {"op": "remove_parent", "iri": CUSTOMER, "parent": PARTY},
        )
    assert raised.value.index == 1
    assert raised.value.message == (
        "Party has to stay a parent of Customer: customer_email is mapped from table customer; "
        "its domain must stay Customer or one of its parents."
    )
    ranged, _ = _apply({"op": "update_property", "iri": PLACED_BY, "range": PARTY})
    with pytest.raises(ops.ChangeSetError) as raised:
        _apply({"op": "remove_parent", "iri": CUSTOMER, "parent": PARTY}, d=ranged)
    assert raised.value.message == (
        "Party has to stay a parent of Customer: order_placedBy joins to Customer in the mapping; "
        "its range must stay Customer or one of its parents."
    )


def test_remove_parent_still_removes_a_parent_no_mapping_needs():
    # Party stays an ancestor of Customer through Person, so the generalised email still fits.
    after, _ = _apply(
        {"op": "add_class", "label": "Person", "parents": [PARTY], "iri": f"{ONTO}Person"},
        {"op": "add_parent", "iri": CUSTOMER, "parent": f"{ONTO}Person"},
        {"op": "update_property", "iri": EMAIL, "domain": PARTY},
        {"op": "remove_parent", "iri": CUSTOMER, "parent": PARTY},
    )
    assert (U(CUSTOMER), RDFS.subClassOf, U(PARTY)) not in after.tbox
    # Order is no kind of Party, so a total with the domain Party contradicted its mapping before the removal:
    # the removal does not break it, so it does not block the removal.
    d = draft()
    d.tbox.set((U(TOTAL), RDFS.domain, U(PARTY)))
    after, _ = _apply({"op": "remove_parent", "iri": CUSTOMER, "parent": PARTY}, d=d)
    assert (U(CUSTOMER), RDFS.subClassOf, U(PARTY)) not in after.tbox


def test_set_match_refuses_to_drop_the_grounding_parent_a_generalised_mapped_property_needs():
    d = draft()
    d.tbox.add((U(FIBO), RDF.type, OWL.Class))  # a reference class the draft declares, as some inductions do
    generalised, _ = _apply({"op": "update_property", "iri": EMAIL, "domain": FIBO}, d=d)
    with pytest.raises(ops.ChangeSetError) as raised:
        _apply({"op": "set_match", "iri": CUSTOMER, "kind": "none"}, d=generalised)
    assert raised.value.message == (
        "Agent has to stay a parent of Customer: customer_email is mapped from table customer; "
        "its domain must stay Customer or one of its parents."
    )
    # Grounding the class to the same target again keeps the parent.
    after, _ = _apply({"op": "set_match", "iri": CUSTOMER, "target": FIBO, "kind": "close"}, d=generalised)
    assert (U(CUSTOMER), RDFS.subClassOf, U(FIBO)) in after.tbox


def test_update_property_moves_an_unmapped_property_freely():
    after, _ = _apply(
        {"op": "add_datatype_property", "label": "nickname", "domain": CUSTOMER, "range": "xsd:string"},
        {"op": "update_property", "iri": f"{ONTO}customer_nickname", "domain": ORDER, "range": "xsd:integer"},
    )
    assert (U(f"{ONTO}customer_nickname"), RDFS.domain, U(ORDER)) in after.tbox


def _with_iri_template_join() -> ops.Draft:
    """The sample draft with placed by mapped as an IRI template, as the RIGOR inducer maps a relationship."""
    d = draft()
    om = U(f"{ONTO}TriplesMap_Order/POM_CustomerId/ObjectMap")
    for condition in list(d.r2rml.objects(om, snapshot.RR.joinCondition)):
        snapshot._drop_orphan_subtree(d.r2rml, condition)
    d.r2rml.remove((om, None, None))
    d.r2rml.add((om, snapshot.RR.termType, snapshot.RR.IRI))
    d.r2rml.add((om, snapshot.RR.template, Literal(f"{ONTO}customer/{{customer_id}}")))
    return d


def test_update_property_only_warns_about_an_iri_template_whose_target_class_is_unknown():
    after, ctx = _apply({"op": "update_property", "iri": PLACED_BY, "range": ITEM}, d=_with_iri_template_join())
    assert (U(PLACED_BY), RDFS.range, U(ITEM)) in after.tbox
    assert ctx.warnings == [
        "order_placedBy is mapped through an IRI template, so the mapping cannot confirm its range Item"
    ]


def _without_table_name() -> ops.Draft:
    """The sample draft with Customer's map reading a query, so the map names no table."""
    d = draft()
    customers = next(d.r2rml.objects(U(f"{ONTO}TriplesMap_Customer"), snapshot.RR.logicalTable))
    d.r2rml.remove((customers, snapshot.RR.tableName, None))
    d.r2rml.add((customers, snapshot.RR.sqlQuery, Literal("SELECT * FROM customer")))
    return d


def test_every_change_warning_gives_its_line_as_a_code_and_values():
    """A change's warnings were English text only, which the web app showed in every language."""
    cases = [
        (
            _with_supplier(),
            {"op": "merge_classes", "source": SUPPLIER, "target": CUSTOMER},
            "MERGE_MAPPED_TABLES",
            {"source": "Supplier", "target": "Customer", "sourceTables": "supplier", "targetTables": "customer"},
        ),
        (
            draft(),
            {"op": "update_property", "iri": EMAIL, "domain": PARTY},
            "DOMAIN_ON_PARENT_TABLE",
            {"property": "customer_email", "tables": "customer", "domain": "Party", "mappedClass": "Customer"},
        ),
        (
            _without_table_name(),
            {"op": "update_property", "iri": EMAIL, "domain": PARTY},
            "DOMAIN_ON_PARENT_MAP",
            {"property": "customer_email", "map": "TriplesMap_Customer", "domain": "Party", "mappedClass": "Customer"},
        ),
        (
            draft(),
            {"op": "update_property", "iri": PLACED_BY, "range": PARTY},
            "RANGE_ON_PARENT",
            {"property": "order_placedBy", "mappedClass": "Customer", "range": "Party"},
        ),
        (
            _with_iri_template_join(),
            {"op": "update_property", "iri": PLACED_BY, "range": ITEM},
            "RANGE_UNCONFIRMED",
            {"property": "order_placedBy", "range": "Item"},
        ),
        (
            draft(),
            {"op": "add_class", "label": "Customer"},
            "ANOTHER_CLASS_LABELLED",
            {"label": "Customer", "iri": CUSTOMER},
        ),
        (
            draft(),
            {"op": "add_object_property", "label": "placed by", "domain": ITEM, "range": CUSTOMER},
            "ANOTHER_RELATIONSHIP_LABELLED",
            {"label": "placed by", "iri": PLACED_BY},
        ),
        (
            draft(),
            {"op": "add_datatype_property", "label": "email", "domain": ITEM, "range": "xsd:string"},
            "ANOTHER_ATTRIBUTE_LABELLED",
            {"label": "email", "iri": EMAIL},
        ),
    ]
    for d, raw, code, params in cases:
        _, ctx = _apply(raw, d=d)
        assert ctx.warning_messages == [{"code": code, "params": params}], code
        assert ctx.warnings == [messages.english(m) for m in ctx.warning_messages], code
    _, ctx = _apply({"op": "update_property", "iri": EMAIL, "domain": PARTY}, d=_without_table_name())
    assert ctx.warnings == [
        "customer_email is mapped by TriplesMap_Customer; with the domain Party, "
        "SPARQL through the VKG no longer offers it on Customer, but NL-to-SQL still does."
    ]
    # The duplicate label reads as before: repr quotes the label.
    _, ctx = _apply({"op": "add_class", "label": "Customer"})
    assert ctx.warnings == [f"Another class is also labelled 'Customer': {CUSTOMER}"]
    # A property's kind is named in the console's words, not as an objectProperty or a datatypeProperty.
    _, ctx = _apply({"op": "add_object_property", "label": "placed by", "domain": ITEM, "range": CUSTOMER})
    assert ctx.warnings == [f"Another relationship is also labelled 'placed by': {PLACED_BY}"]
    _, ctx = _apply({"op": "add_datatype_property", "label": "email", "domain": ITEM, "range": "xsd:string"})
    assert ctx.warnings == [f"Another attribute is also labelled 'email': {EMAIL}"]


def test_two_maps_of_one_table_give_one_coded_warning():
    d = draft()
    second = U(f"{ONTO}TriplesMap_Customer2")
    d.r2rml.add((second, RDF.type, snapshot.RR.TriplesMap))
    for p in (snapshot.RR.logicalTable, snapshot.RR.subjectMap, snapshot.RR.predicateObjectMap):
        for o in d.r2rml.objects(U(f"{ONTO}TriplesMap_Customer"), p):
            d.r2rml.add((second, p, o))
    _, ctx = _apply({"op": "update_property", "iri": EMAIL, "domain": PARTY}, d=d)
    assert [m["code"] for m in ctx.warning_messages] == ["DOMAIN_ON_PARENT_TABLE"]
    assert len(ctx.warnings) == 1


def test_replace_datatype_refuses_a_non_xsd_target_for_a_datatype_the_mapping_uses():
    # The column of a mapped property decides first.
    assert _replace_refused(BAD, str(RDF.langString), _draft_with_bad_datatype()) == (
        "datetime cannot be replaced with langString: order_total is mapped from column total, typed datetime; "
        "its datatype follows the column."
    )
    # A map that reads its predicate from a column maps no property of the draft, and still types a column.
    d = draft()
    d.r2rml.parse(
        data=f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
<{ONTO}TriplesMap_Order> rr:predicateObjectMap <{ONTO}TriplesMap_Order/POM_Attribute> .
<{ONTO}TriplesMap_Order/POM_Attribute> rr:predicateMap [ rr:column "attribute" ] ;
    rr:objectMap [ rr:column "value" ; rr:datatype <{BAD}> ] .
""",
        format="turtle",
    )
    assert _replace_refused(BAD, str(RDF.langString), d) == (
        "datetime is the datatype of mapped columns, so only an XSD datatype can replace it, not langString"
    )
    # A datatype only the ontology uses may become any datatype the designer accepts.
    d = draft()
    d.tbox.add((U(f"{ONTO}customer_note"), RDF.type, OWL.DatatypeProperty))
    d.tbox.add((U(f"{ONTO}customer_note"), RDFS.range, U(f"{XSD}token")))
    after, _ = _apply({"op": "replace_datatype", "from": "xsd:token", "to": str(RDF.langString)}, d=d)
    assert (U(f"{ONTO}customer_note"), RDFS.range, RDF.langString) in after.tbox


def test_delete_property_drops_its_mapping():
    after, _ = _apply({"op": "delete_property", "iri": EMAIL})
    assert (U(f"{ONTO}TriplesMap_Customer/POM_Email"), None, None) not in after.r2rml
    assert (U(f"{ONTO}TriplesMap_Customer"), snapshot.RR.predicateObjectMap, None) not in after.r2rml


def test_set_match_replaces_only_foreign_grounding():
    after, _ = _apply({"op": "set_match", "iri": CUSTOMER, "target": "https://schema.org/Person", "kind": "close"})
    assert set(after.tbox.objects(U(CUSTOMER), RDFS.subClassOf)) == {U(PARTY), U("https://schema.org/Person")}
    assert set(after.tbox.objects(U(CUSTOMER), SKOS.closeMatch)) == {U("https://schema.org/Person")}
    assert (U(CUSTOMER), SKOS.exactMatch, None) not in after.tbox
    assert (U(CUSTOMER), U(f"{ONTO}matchConfidence"), None) not in after.tbox
    cleared, _ = _apply({"op": "set_match", "iri": CUSTOMER, "kind": "none"})
    assert set(cleared.tbox.objects(U(CUSTOMER), RDFS.subClassOf)) == {U(PARTY)}


def test_a_failing_op_leaves_the_draft_untouched_and_names_its_index():
    d = draft()
    before = _nt(d)
    with pytest.raises(ops.ChangeSetError) as raised:
        _apply({"op": "add_class", "label": "Supplier"}, {"op": "delete_class", "iri": f"{ONTO}Nope"}, d=d)
    assert raised.value.index == 1
    assert _nt(d) == before


def test_restore_triples_refuses_a_drifted_draft():
    inverse = ops.RestoreTriples(op="restore_triples", tbox=ops.LineDelta(remove=["<urn:x> <urn:y> <urn:z> ."]))
    with pytest.raises(ops.ChangeSetError, match="no longer matches"):
        ops.apply_ops(draft(), [inverse], SCHEME)


def test_clients_cannot_send_restore_triples_or_unknown_fields():
    with pytest.raises(ValidationError):
        ops.USER_OPS.validate_python([{"op": "restore_triples"}])
    with pytest.raises(ValidationError):
        ops.USER_OPS.validate_python([{"op": "add_class", "label": "X", "colour": "red"}])
    assert ops.ANY_OPS.validate_python([{"op": "restore_triples"}])[0].op == "restore_triples"


def test_any_ops_is_the_user_ops_plus_restore_triples():
    parsed = ops.ANY_OPS.validate_python([*ROUND_TRIP, {"op": "restore_triples"}])
    assert [type(p) for p in parsed[:-1]] == [type(p) for p in ops.USER_OPS.validate_python(ROUND_TRIP)]
    assert isinstance(parsed[-1], ops.RestoreTriples)


def test_summaries_name_the_terms():
    parsed = ops.USER_OPS.validate_python(ROUND_TRIP)
    assert messages.english(parsed[4].summary_message(draft().tbox)) == "Deleted class Customer (detach its properties)"
    assert parsed[3].model_dump(by_alias=True)["newIri"] == f"{ONTO}Client"


BAD = "http://www.w3.org/2001/XMLSchema#datetime"
GOOD = "http://www.w3.org/2001/XMLSchema#dateTime"


def _draft_with_bad_datatype() -> ops.Draft:
    """The sample draft with the malformed token on a range, a mapping, a literal and a non-datatype object."""
    d = draft()
    d.tbox.add((U(f"{ONTO}shipped_at"), RDF.type, OWL.DatatypeProperty))
    d.tbox.add((U(f"{ONTO}shipped_at"), RDFS.range, U(BAD)))
    d.tbox.add((U(f"{ONTO}shipped_at"), RDFS.comment, Literal("2024-01-15", datatype=U(BAD), normalize=False)))
    d.r2rml.add((U(f"{ONTO}TriplesMap_Order/POM_Total/ObjectMap"), snapshot.RR.datatype, U(BAD)))
    # The same IRI as the object of another predicate is not a datatype use and is left alone.
    d.tbox.add((U(CUSTOMER), RDFS.seeAlso, U(BAD)))
    return d


def test_replace_datatype_rewrites_ranges_mappings_and_literals_and_keeps_the_lexical_form():
    after, _ = _apply({"op": "replace_datatype", "from": BAD, "to": "xsd:dateTime"}, d=_draft_with_bad_datatype())
    assert (U(f"{ONTO}shipped_at"), RDFS.range, U(GOOD)) in after.tbox
    assert (U(f"{ONTO}shipped_at"), RDFS.range, U(BAD)) not in after.tbox
    assert (None, snapshot.RR.datatype, U(GOOD)) in after.r2rml
    assert (None, snapshot.RR.datatype, U(BAD)) not in after.r2rml
    (note,) = after.tbox.objects(U(f"{ONTO}shipped_at"), RDFS.comment)
    assert str(note) == "2024-01-15" and note.datatype == U(GOOD)
    assert (U(CUSTOMER), RDFS.seeAlso, U(BAD)) in after.tbox


def test_replace_datatype_takes_the_xsd_prefix_for_a_malformed_from_and_round_trips():
    before = _draft_with_bad_datatype()
    after, _ = _apply({"op": "replace_datatype", "from": "xsd:datetime", "to": "xsd:dateTime"}, d=before)
    assert (U(f"{ONTO}shipped_at"), RDFS.range, U(GOOD)) in after.tbox
    assert _nt(_undo(before, after)) == _nt(before)


def test_replace_datatype_refuses_what_it_cannot_do():
    d = _draft_with_bad_datatype()
    for raw, match in (
        ({"op": "replace_datatype", "from": "xsd:gYear", "to": "xsd:string"}, "not used"),
        ({"op": "replace_datatype", "from": BAD, "to": "xsd:datetime"}, "XSD datatype"),
        ({"op": "replace_datatype", "from": BAD, "to": "xsd:garbage"}, "XSD datatype"),
        ({"op": "replace_datatype", "from": "xsd:string", "to": "xsd:string"}, "same"),
    ):
        with pytest.raises(ops.ChangeSetError, match=match):
            _apply(raw, d=d)


def test_replace_datatype_is_a_user_op_with_from_and_to_fields():
    (op,) = ops.USER_OPS.validate_python([{"op": "replace_datatype", "from": "xsd:datetime", "to": "xsd:dateTime"}])
    assert op.model_dump(by_alias=True) == {"op": "replace_datatype", "from": "xsd:datetime", "to": "xsd:dateTime"}
    assert isinstance(ops.ANY_OPS.validate_python([op.model_dump(by_alias=True)])[0], ops.ReplaceDatatype)
    assert messages.english(op.summary_message(draft().tbox)) == "Replaced the datatype xsd:datetime with xsd:dateTime"
    with pytest.raises(ValidationError):
        ops.USER_OPS.validate_python([{"op": "replace_datatype", "from": "xsd:datetime"}])


BALANCE = f"{ONTO}customer_balance"
BALANCE_TBOX = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<{BALANCE}> a owl:DatatypeProperty ; rdfs:label "balance" ; rdfs:domain <{CUSTOMER}> ; rdfs:range xsd:decimal .
"""
BALANCE_R2RML = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<{ONTO}TriplesMap_Customer> rr:predicateObjectMap <{ONTO}TriplesMap_Customer/POM_Balance> .
<{ONTO}TriplesMap_Customer/POM_Balance> rr:predicate <{BALANCE}> ;
    rr:objectMap <{ONTO}TriplesMap_Customer/POM_Balance/ObjectMap> .
<{ONTO}TriplesMap_Customer/POM_Balance/ObjectMap> rr:column "\\"balance\\"" ; rr:datatype xsd:decimal .
"""
EMAIL_OBJECT_MAP = U(f"{ONTO}TriplesMap_Customer/POM_Email/ObjectMap")
TOTAL_OBJECT_MAP = U(f"{ONTO}TriplesMap_Order/POM_Total/ObjectMap")


def _with_balance() -> ops.Draft:
    """The sample draft with a second decimal attribute mapped from a column, customer balance."""
    d = draft()
    d.tbox.parse(data=BALANCE_TBOX, format="turtle")
    d.r2rml.parse(data=BALANCE_R2RML, format="turtle")
    return d


def _replace_refused(frm: str, to: str, d: ops.Draft | None = None) -> str:
    with pytest.raises(ops.ChangeSetError) as raised:
        _apply({"op": "replace_datatype", "from": frm, "to": to}, d=d)
    return raised.value.message


def test_replace_datatype_refuses_to_retype_the_column_of_a_mapped_property():
    assert _replace_refused("xsd:decimal", "xsd:double") == (
        "xsd:decimal cannot be replaced with xsd:double: order_total is mapped from column total, typed decimal; "
        "its datatype follows the column."
    )
    # Whatever the target: the column decides, so the refusal of a non-XSD target does not apply first.
    assert _replace_refused("xsd:decimal", str(RDF.langString)).startswith(
        "xsd:decimal cannot be replaced with langString: order_total is mapped from column total"
    )
    # The column is retyped even when the range is already the target: its datatype stays the column's.
    d = draft()
    d.r2rml.set((EMAIL_OBJECT_MAP, snapshot.RR.datatype, XSD.token))
    assert _replace_refused("xsd:token", "xsd:string", d) == (
        "xsd:token cannot be replaced with xsd:string: customer_email is mapped from column email, typed token; "
        "its datatype follows the column."
    )


def test_replace_datatype_names_every_mapped_property_it_would_retype_with_its_column():
    assert _replace_refused("xsd:decimal", "xsd:integer", _with_balance()) == (
        "xsd:decimal cannot be replaced with xsd:integer: customer_balance is mapped from column balance, "
        "typed decimal; order_total is mapped from column total, typed decimal; their datatypes follow their columns."
    )
    with patch.object(ops, "_MAX_NAMED", 1):
        assert _replace_refused("xsd:decimal", "xsd:integer", _with_balance()) == (
            "xsd:decimal cannot be replaced with xsd:integer: customer_balance is mapped from column balance, "
            "typed decimal (and 1 more); their datatypes follow their columns."
        )


def test_replace_datatype_lets_a_mapped_range_follow_its_column_and_no_other_datatype():
    d = draft()
    d.tbox.set((U(EMAIL), RDFS.range, XSD.token))  # the range contradicts the column, typed string
    after, _ = _apply({"op": "replace_datatype", "from": "xsd:token", "to": "xsd:string"}, d=d)
    assert set(after.tbox.objects(U(EMAIL), RDFS.range)) == {XSD.string}
    assert _replace_refused("xsd:token", "xsd:normalizedString", d) == (
        "xsd:token cannot be replaced with xsd:normalizedString: customer_email is mapped from column email, "
        "typed string; its datatype follows the column."
    )
    # A map with a constant and no column types the property too.
    d.r2rml.remove((EMAIL_OBJECT_MAP, snapshot.RR.column, None))
    d.r2rml.add((EMAIL_OBJECT_MAP, snapshot.RR.constant, Literal("unknown")))
    assert _replace_refused("xsd:token", "xsd:normalizedString", d) == (
        "xsd:token cannot be replaced with xsd:normalizedString: customer_email is mapped as string; "
        "its datatype follows the mapping."
    )


def test_replace_datatype_still_retypes_unmapped_attributes_and_repairs_a_malformed_column_type():
    d = draft()
    weight = U(f"{ONTO}item_weight")
    d.tbox.add((weight, RDF.type, OWL.DatatypeProperty))
    d.tbox.add((weight, RDFS.range, XSD.double))
    after, ctx = _apply({"op": "replace_datatype", "from": "xsd:double", "to": "xsd:decimal"}, d=d)
    assert set(after.tbox.objects(weight, RDFS.range)) == {XSD.decimal} and ctx.warnings == []
    # A malformed token that types both the range and the column is the repair the op is for.
    d = draft()
    d.tbox.set((U(TOTAL), RDFS.range, U(BAD)))
    d.r2rml.set((TOTAL_OBJECT_MAP, snapshot.RR.datatype, U(BAD)))
    after, _ = _apply({"op": "replace_datatype", "from": "xsd:datetime", "to": "xsd:dateTime"}, d=d)
    assert set(after.tbox.objects(U(TOTAL), RDFS.range)) == {U(GOOD)}
    assert set(after.r2rml.objects(TOTAL_OBJECT_MAP, snapshot.RR.datatype)) == {U(GOOD)}
    assert _nt(_undo(d, after)) == _nt(d)
    # A malformed range is not repaired into another datatype than its column's.
    d = draft()
    d.tbox.set((U(TOTAL), RDFS.range, U(BAD)))
    assert _replace_refused("xsd:datetime", "xsd:dateTime", d) == (
        "xsd:datetime cannot be replaced with xsd:dateTime: order_total is mapped from column total, "
        "typed decimal; its datatype follows the column."
    )


TIMESTAMP = "http://www.w3.org/2001/XMLSchema#timestamp"


def test_replace_datatype_repairs_a_range_and_a_column_spelled_differently_in_either_order():
    """Repair sends one op per token; xsd:datetime on the range and xsd:timestamp on the column both mean dateTime."""
    d = draft()
    d.tbox.set((U(TOTAL), RDFS.range, U(BAD)))
    d.r2rml.set((TOTAL_OBJECT_MAP, snapshot.RR.datatype, U(TIMESTAMP)))
    repair = [
        {"op": "replace_datatype", "from": BAD, "to": GOOD},
        {"op": "replace_datatype", "from": TIMESTAMP, "to": GOOD},
    ]
    for raw in (repair, repair[::-1]):
        after, _ = _apply(*raw, d=d)
        assert set(after.tbox.objects(U(TOTAL), RDFS.range)) == {U(GOOD)}
        assert set(after.r2rml.objects(TOTAL_OBJECT_MAP, snapshot.RR.datatype)) == {U(GOOD)}


def test_replace_datatype_repairs_a_malformed_column_type_only_into_the_datatype_it_stands_for():
    d = draft()
    d.tbox.set((U(TOTAL), RDFS.range, U(BAD)))
    d.r2rml.set((TOTAL_OBJECT_MAP, snapshot.RR.datatype, U(BAD)))
    # update_property refuses the same change, for the same reason.
    reason = "order_total is mapped from column total, typed datetime; its datatype follows the column."
    with pytest.raises(ops.ChangeSetError) as raised:
        _apply({"op": "update_property", "iri": TOTAL, "range": "xsd:integer"}, d=d)
    assert raised.value.message == reason
    assert (
        _replace_refused("xsd:datetime", "xsd:integer", d)
        == f"xsd:datetime cannot be replaced with xsd:integer: {reason}"
    )
    # A token with no canonical spelling stands for no datatype the op knows, so its column keeps it.
    garbage = U(f"{XSD}garbage")
    d.tbox.set((U(TOTAL), RDFS.range, garbage))
    d.r2rml.set((TOTAL_OBJECT_MAP, snapshot.RR.datatype, garbage))
    assert _replace_refused("xsd:garbage", "xsd:string", d) == (
        "xsd:garbage cannot be replaced with xsd:string: order_total is mapped from column total, typed garbage; "
        "its datatype follows the column."
    )
