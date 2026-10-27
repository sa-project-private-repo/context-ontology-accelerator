# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
from unittest.mock import patch

import pytest
from coa_ontology import dynamo_store
from coa_ontology.studio import changes, rebase
from coa_ontology.studio import draft as store
from coa_ontology.studio.ops import mentioned
from rdflib import RDFS, XSD, Literal, URIRef

from .studio_fakes import (
    CUSTOMER,
    ITEM,
    NS,
    ONTO,
    ORDER,
    PARTY,
    R2RML,
    TBOX,
    VIP,
    opened_draft,
    publish_base,
    stored_row,
    stored_state,
)

pytestmark = [pytest.mark.unit]
WAREHOUSE = f"{ONTO}Warehouse"
SUPPLIER, VENDOR = f"{ONTO}Supplier", f"{ONTO}Vendor"
PREMIUM, BUYS_FROM = f"{ONTO}PremiumSupplier", f"{ONTO}order_buysFrom"
HEADER = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
"""
V2 = f'{HEADER}<{WAREHOUSE}> a owl:Class ; rdfs:label "Warehouse" .\n'
V3 = f'{HEADER}<{SUPPLIER}> a owl:Class ; rdfs:label "Supplier" .\n'
V4 = f'{HEADER}<{VENDOR}> a owl:Class ; rdfs:label "Vendor" .\n'
SUPPLIERS = f"""{V3}<{PREMIUM}> a owl:Class ; rdfs:label "Premium supplier" .
<{BUYS_FROM}> a owl:ObjectProperty ; rdfs:label "buys from" ; rdfs:domain <{ORDER}> ; rdfs:range <{SUPPLIER}> .
"""


@pytest.fixture(autouse=True)
def _env(versions_aws):
    store.clear_cache()
    with patch("coa_ontology.proposals._generate_schema_sql", return_value=None):
        opened_draft()
        yield versions_aws
    store.clear_cache()


def _apply(ops: list[dict], expected: int) -> dict:
    return changes.apply_change_set(NS, "p-1", ops, expected, author="alice")


def test_rebase_replays_user_ops_onto_the_new_head():
    _apply([{"op": "delete_class", "iri": CUSTOMER}], 0)  # 1
    _apply([{"op": "add_class", "label": "Supplier"}], 1)  # 2
    changes.undo(NS, "p-1", 2, author="alice")  # 3: Supplier undone, not replayed
    publish_base(V2, "", "p-other")  # v2
    result = rebase.rebase(NS, "p-1", 3, [], author="alice")
    assert result == {"revision": 4, "baseVersion": 2, "replayed": 1, "skipped": [], "canUndo": True, "canRedo": False}
    d = store.load_revision(NS, "p-1", 4).draft
    assert mentioned(d, WAREHOUSE) and not mentioned(d, CUSTOMER) and not mentioned(d, SUPPLIER)
    state = stored_state()
    assert (state.base_version, state.applied, state.redo) == (2, (4,), ())
    record = store.get_record(NS, "p-1", 4)
    assert (record["kind"], record["baseRevision"], record["baseVersion"]) == ("rebase", 3, 2)
    assert (record["fromVersion"], record["skipped"]) == (1, [])
    assert [op["op"] for op in record["ops"]] == ["delete_class"]
    # Undo right after a rebase returns to the fresh merge on the new head.
    changes.undo(NS, "p-1", 4, author="alice")
    assert mentioned(store.load_revision(NS, "p-1", 5).draft, CUSTOMER)


def test_a_rebase_gives_its_history_lines_as_codes_and_values():
    _apply([{"op": "delete_class", "iri": CUSTOMER}], 0)
    publish_base(V2, "", "p-other")  # v2
    rebase.rebase(NS, "p-1", 1, [], author="alice")
    record = store.get_record(NS, "p-1", 2)
    assert record["summary"] == ["Rebased onto v2", "Deleted class Customer (detach its properties)"]
    assert record["summaryMessages"] == [
        {"code": "REBASED", "params": {"version": "2"}},
        {"code": "DELETE_CLASS_DETACH", "params": {"term": "Customer"}},
    ]


def test_a_rebase_names_each_replayed_term_by_its_label():
    _apply([{"op": "delete_class", "iri": VIP}], 0)
    publish_base(V2, "", "p-other")  # v2
    rebase.rebase(NS, "p-1", 1, [], author="alice")
    record = store.get_record(NS, "p-1", 2)
    assert record["summary"] == ["Rebased onto v2", "Deleted class VIP customer (detach its properties)"]
    assert record["summaryMessages"][1:] == [{"code": "DELETE_CLASS_DETACH", "params": {"term": "VIP customer"}}]


def test_ops_that_no_longer_apply_are_reported_and_can_be_skipped():
    _apply(
        [
            {"op": "add_class", "label": "Warehouse", "iri": WAREHOUSE},
            {"op": "set_label", "iri": PARTY, "label": "Actor"},
        ],
        0,
    )  # 1
    publish_base(V2, "", "p-other")  # v2 also defines Warehouse
    before = stored_state()
    with pytest.raises(rebase.RebaseFailed) as raised:
        rebase.rebase(NS, "p-1", 1, [], author="alice")
    assert raised.value.extra["revision"] == 1
    assert [(f["opId"], f["op"]["op"]) for f in raised.value.extra["failures"]] == [("1:0", "add_class")]
    assert "already exists" in raised.value.extra["failures"][0]["error"]
    assert stored_state() == before and before.base_version == 1  # the draft is unchanged
    assert store._get(store.record_key(NS, "p-1", 2)) is None
    result = rebase.rebase(NS, "p-1", 1, ["1:0"], author="alice")
    assert result["replayed"] == 1 and result["skipped"] == ["1:0"]
    assert store.get_record(NS, "p-1", result["revision"])["skipped"] == ["1:0"]


def test_an_edit_that_the_new_heads_mapping_contradicts_is_reported_and_can_be_skipped():
    email = f"{ONTO}customer_email"
    _apply([{"op": "update_property", "iri": email, "domain": PARTY}], 0)  # 1: Customer is a Party, so allowed
    # v2 also maps the attribute from the orders table, whose class is no Party.
    orders_email = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
<{ONTO}TriplesMap_Order> rr:predicateObjectMap <{ONTO}TriplesMap_Order/POM_Email> .
<{ONTO}TriplesMap_Order/POM_Email> rr:predicate <{email}> ;
    rr:objectMap [ rr:column "buyer_email" ] .
"""
    publish_base(r2rml=R2RML + orders_email, proposal="p-other")
    with pytest.raises(rebase.RebaseFailed) as raised:
        rebase.rebase(NS, "p-1", 1, [], author="alice")
    (failure,) = raised.value.extra["failures"]
    assert failure["opId"] == "1:0" and failure["error"] == (
        "customer_email is mapped from table orders; its domain must stay Order or one of its parents."
    )
    assert rebase.rebase(NS, "p-1", 1, ["1:0"], author="alice")["skipped"] == ["1:0"]


def test_a_datatype_replacement_that_would_retype_a_column_of_the_new_head_is_reported_and_can_be_skipped():
    nickname, code = f"{ONTO}customer_nickname", f"{ONTO}customer_code"
    _apply([{"op": "add_datatype_property", "label": "nickname", "domain": CUSTOMER, "range": "xsd:token"}], 0)  # 1
    _apply([{"op": "replace_datatype", "from": "xsd:token", "to": "xsd:string"}], 1)  # 2: nickname is unmapped
    # v2 maps an attribute from a column typed token.
    code_tbox = (
        f'<{code}> a owl:DatatypeProperty ; rdfs:label "code" ; rdfs:domain <{CUSTOMER}> ; rdfs:range xsd:token .'
    )
    code_r2rml = f"""<{ONTO}TriplesMap_Customer> rr:predicateObjectMap <{ONTO}TriplesMap_Customer/POM_Code> .
<{ONTO}TriplesMap_Customer/POM_Code> rr:predicate <{code}> ; rr:objectMap [ rr:column "code" ; rr:datatype xsd:token ] .
"""
    publish_base(TBOX + code_tbox, R2RML + code_r2rml, "p-other")
    with pytest.raises(rebase.RebaseFailed) as raised:
        rebase.rebase(NS, "p-1", 2, [], author="alice")
    (failure,) = raised.value.extra["failures"]
    assert failure["opId"] == "2:0" and failure["error"] == (
        "xsd:token cannot be replaced with xsd:string: customer_code is mapped from column code, typed token; "
        "its datatype follows the column."
    )
    result = rebase.rebase(NS, "p-1", 2, ["2:0"], author="alice")
    assert (result["replayed"], result["skipped"]) == (1, ["2:0"])
    d = store.load_revision(NS, "p-1", result["revision"]).draft
    assert list(d.tbox.objects(URIRef(nickname), RDFS.range)) == [XSD.token]
    assert list(d.tbox.objects(URIRef(code), RDFS.range)) == [XSD.token]


def test_an_added_class_whose_minted_iri_the_new_head_now_defines_fails_instead_of_being_renamed():
    assert _apply([{"op": "add_class", "label": "Warehouse"}], 0)["created"] == [WAREHOUSE]  # 1, no IRI sent
    _apply([{"op": "set_comment", "iri": WAREHOUSE, "comment": "Stores goods"}], 1)  # 2 edits that class
    publish_base(V2, "", "p-other")  # v2 defines a Warehouse of its own
    before = stored_state()
    with pytest.raises(rebase.RebaseFailed) as raised:
        rebase.rebase(NS, "p-1", 2, [], author="alice")
    failure, dependant = raised.value.extra["failures"]
    assert (failure["opId"], failure["op"]["op"], failure["op"]["iri"]) == ("1:0", "add_class", WAREHOUSE)
    assert "already exists" in failure["error"]
    assert (dependant["opId"], dependant["error"]) == ("2:0", "depends on failed op 1:0")  # reported in the same try
    assert stored_state() == before and store._get(store.record_key(NS, "p-1", 3)) is None
    # Skipping only the add is not enough: the op that edits its class would edit the head's Warehouse.
    with pytest.raises(rebase.RebaseFailed) as raised:
        rebase.rebase(NS, "p-1", 2, ["1:0"], author="alice")
    assert raised.value.extra["failures"] == [
        {
            "opId": "2:0",
            "op": {"op": "set_comment", "iri": WAREHOUSE, "comment": "Stores goods"},
            "error": "depends on skipped op 1:0",
        }
    ]
    assert stored_state() == before and store._get(store.record_key(NS, "p-1", 3)) is None
    # The op and the op that edits its class are skipped together; the head's Warehouse is left as it is.
    result = rebase.rebase(NS, "p-1", 2, ["1:0", "2:0"], author="alice")
    assert result["replayed"] == 0 and result["skipped"] == ["1:0", "2:0"] and not result["canUndo"]
    d = store.load_revision(NS, "p-1", result["revision"]).draft
    assert mentioned(d, WAREHOUSE) and not mentioned(d, f"{ONTO}Warehouse_2")
    assert list(d.tbox.objects(URIRef(WAREHOUSE), RDFS.comment)) == []


def test_every_op_that_names_a_skipped_class_is_a_dependant_and_so_is_what_names_the_dependants_own_terms():
    _apply([{"op": "add_class", "label": "Supplier"}], 0)  # 1:0 mints Supplier
    _apply(
        [
            {"op": "add_class", "label": "Premium supplier", "parents": [SUPPLIER]},  # 2:0 names it as a parent
            {"op": "set_label", "iri": PARTY, "label": "Actor"},  # 2:1 names nothing skipped
        ],
        1,
    )
    _apply(
        [
            {"op": "add_object_property", "label": "buys from", "domain": ORDER, "range": SUPPLIER},  # 3:0 range
            {"op": "add_parent", "iri": CUSTOMER, "parent": SUPPLIER},  # 3:1 parent
        ],
        2,
    )
    _apply([{"op": "set_comment", "iri": PREMIUM, "comment": "Gold tier"}], 3)  # 4:0 names the class 2:0 adds
    _apply([{"op": "update_property", "iri": BUYS_FROM, "domain": ITEM}], 4)  # 5:0 names the property 3:0 adds
    publish_base(SUPPLIERS, "", "p-other")  # the head defines all three terms, so a dependant would edit them
    before = stored_state()
    with pytest.raises(rebase.RebaseFailed) as raised:
        rebase.rebase(NS, "p-1", 5, ["1:0"], author="alice")  # skipping Supplier alone
    failures = raised.value.extra["failures"]
    assert [f["opId"] for f in failures] == ["2:0", "3:0", "3:1", "4:0", "5:0"]  # not 2:1
    assert {f["error"] for f in failures} == {"depends on skipped op 1:0"}
    assert stored_state() == before and store._get(store.record_key(NS, "p-1", 6)) is None
    everything = ["1:0", "2:0", "3:0", "3:1", "4:0", "5:0"]
    result = rebase.rebase(NS, "p-1", 5, everything, author="alice")
    assert (result["baseVersion"], result["replayed"], result["skipped"]) == (2, 1, everything)
    d = store.load_revision(NS, "p-1", result["revision"]).draft
    assert (URIRef(PARTY), RDFS.label, Literal("Actor")) in d.tbox  # the op that depended on nothing was replayed
    assert list(d.tbox.objects(URIRef(PREMIUM), RDFS.comment)) == []  # the head's own terms are as the head has them
    assert list(d.tbox.objects(URIRef(BUYS_FROM), RDFS.domain)) == [URIRef(ORDER)]
    assert (URIRef(CUSTOMER), RDFS.subClassOf, URIRef(SUPPLIER)) not in d.tbox


def _renamed_class_history() -> None:
    """1:0 adds Supplier, 2:0 renames it to Vendor, 3:0 comments on Vendor; the new head has a Vendor of its own."""
    _apply([{"op": "add_class", "label": "Supplier"}], 0)
    _apply([{"op": "change_iri", "iri": SUPPLIER, "newIri": VENDOR}], 1)
    _apply([{"op": "set_comment", "iri": VENDOR, "comment": "Sells to us"}], 2)
    publish_base(V4, "", "p-other")


def test_skipping_an_add_also_fails_the_rename_of_its_class_and_what_edits_the_new_iri():
    _renamed_class_history()
    before = stored_state()
    with pytest.raises(rebase.RebaseFailed) as raised:
        rebase.rebase(NS, "p-1", 3, ["1:0"], author="alice")
    failures = raised.value.extra["failures"]
    assert [(f["opId"], f["error"]) for f in failures] == [
        ("2:0", "depends on skipped op 1:0"),  # names Supplier
        ("3:0", "depends on skipped op 1:0"),  # names Vendor, which only the rename of Supplier would give it
    ]
    assert stored_state() == before and store._get(store.record_key(NS, "p-1", 4)) is None
    result = rebase.rebase(NS, "p-1", 3, ["1:0", "2:0", "3:0"], author="alice")
    d = store.load_revision(NS, "p-1", result["revision"]).draft
    assert result["replayed"] == 0 and not mentioned(d, SUPPLIER)
    assert mentioned(d, VENDOR) and list(d.tbox.objects(URIRef(VENDOR), RDFS.comment)) == []  # the head's Vendor


def test_an_op_that_fails_on_its_own_fails_what_names_its_iri_in_the_same_list():
    _renamed_class_history()  # the head has Vendor but not Supplier, so the rename 2:0 cannot apply
    with pytest.raises(rebase.RebaseFailed) as raised:
        rebase.rebase(NS, "p-1", 3, [], author="alice")
    failures = raised.value.extra["failures"]
    assert [f["opId"] for f in failures] == ["2:0", "3:0"]  # one round trip
    assert "already exists" in failures[0]["error"]
    assert failures[1]["error"] == "depends on failed op 2:0"
    result = rebase.rebase(NS, "p-1", 3, ["2:0", "3:0"], author="alice")
    d = store.load_revision(NS, "p-1", result["revision"]).draft
    assert result["replayed"] == 1 and mentioned(d, SUPPLIER)  # the add of Supplier still applies
    assert list(d.tbox.objects(URIRef(VENDOR), RDFS.comment)) == []


def test_a_label_or_comment_that_reads_like_a_skipped_iri_is_not_a_dependency():
    _apply([{"op": "add_class", "label": "Supplier"}], 0)  # 1:0
    _apply([{"op": "set_label", "iri": PARTY, "label": SUPPLIER}], 1)  # 2:0
    _apply([{"op": "set_comment", "iri": PARTY, "comment": SUPPLIER}], 2)  # 3:0
    publish_base(V2, "", "p-other")
    result = rebase.rebase(NS, "p-1", 3, ["1:0"], author="alice")
    d = store.load_revision(NS, "p-1", result["revision"]).draft
    assert result["replayed"] == 2
    assert (URIRef(PARTY), RDFS.label, Literal(SUPPLIER)) in d.tbox
    assert (URIRef(PARTY), RDFS.comment, Literal(SUPPLIER)) in d.tbox


def test_an_op_before_a_skipped_add_does_not_depend_on_it():
    _apply([{"op": "set_label", "iri": VIP, "label": "Gold"}], 0)  # 1:0 names VIP, which the base has
    _apply([{"op": "delete_class", "iri": VIP}], 1)  # 2:0
    _apply([{"op": "add_class", "label": "VIP customer", "iri": VIP}], 2)  # 3:0 adds the same IRI again
    _apply([{"op": "set_comment", "iri": VIP, "comment": "Top tier"}], 3)  # 4:0 names the class 3:0 adds
    publish_base(V2, "", "p-other")
    with pytest.raises(rebase.RebaseFailed) as raised:
        rebase.rebase(NS, "p-1", 4, ["3:0"], author="alice")
    assert [(f["opId"], f["error"]) for f in raised.value.extra["failures"]] == [
        ("4:0", "depends on skipped op 3:0")  # 1:0 and 2:0 come before the skipped add
    ]
    result = rebase.rebase(NS, "p-1", 4, ["3:0", "4:0"], author="alice")
    assert result["replayed"] == 2 and not mentioned(store.load_revision(NS, "p-1", result["revision"]).draft, VIP)


def test_an_add_is_not_a_dependency_of_itself_and_a_term_added_again_is_present_again():
    _apply([{"op": "add_class", "label": "Supplier"}], 0)  # 1:0, skipped below
    _apply([{"op": "delete_class", "iri": SUPPLIER}], 1)  # 2:0 names the class 1:0 adds
    _apply([{"op": "add_class", "label": "Supplier", "iri": SUPPLIER}], 2)  # 3:0 adds the same IRI again
    _apply([{"op": "set_comment", "iri": SUPPLIER, "comment": "Back again"}], 3)  # 4:0 names the class 3:0 adds
    publish_base(V2, "", "p-other")
    with pytest.raises(rebase.RebaseFailed) as raised:
        rebase.rebase(NS, "p-1", 4, ["1:0"], author="alice")
    assert [(f["opId"], f["error"]) for f in raised.value.extra["failures"]] == [("2:0", "depends on skipped op 1:0")]
    result = rebase.rebase(NS, "p-1", 4, ["1:0", "2:0"], author="alice")
    d = store.load_revision(NS, "p-1", result["revision"]).draft
    assert result["replayed"] == 2
    assert list(d.tbox.objects(URIRef(SUPPLIER), RDFS.comment)) == [Literal("Back again")]


def test_skipped_ops_are_listed_in_the_order_of_the_replay_not_as_text():
    for n in range(10):
        _apply([{"op": "set_label", "iri": PARTY, "label": f"Actor {n}"}], n)  # 1:0 .. 10:0
    publish_base(V2, "", "p-other")
    result = rebase.rebase(NS, "p-1", 10, ["10:0", "2:0"], author="alice")
    assert (result["replayed"], result["skipped"]) == (8, ["2:0", "10:0"])
    assert store.get_record(NS, "p-1", result["revision"])["skipped"] == ["2:0", "10:0"]


def test_a_second_rebase_replays_the_ops_the_first_one_stored():
    _apply([{"op": "add_class", "label": "Supplier"}], 0)  # 1
    publish_base(V2, "", "p-other")  # v2
    assert rebase.rebase(NS, "p-1", 1, [], author="alice")["revision"] == 2
    assert store.get_record(NS, "p-1", 2)["ops"][0]["iri"] == SUPPLIER
    publish_base(V3, "", "p-third")  # v3 defines Supplier
    with pytest.raises(rebase.RebaseFailed) as raised:
        rebase.rebase(NS, "p-1", 2, [], author="alice")
    assert [f["opId"] for f in raised.value.extra["failures"]] == ["2:0"]
    result = rebase.rebase(NS, "p-1", 2, ["2:0"], author="alice")
    assert (result["baseVersion"], result["replayed"], result["skipped"]) == (3, 0, ["2:0"])


def test_nothing_to_rebase_and_unknown_skips():
    with pytest.raises(rebase.NothingToRebase):
        rebase.rebase(NS, "p-1", 0, [], author="alice")
    publish_base(V2, "", "p-other")
    with pytest.raises(changes.InvalidChange, match="Unknown op ids"):
        rebase.rebase(NS, "p-1", 0, ["9:0"], author="alice")
    with pytest.raises(store.DraftConflict):
        rebase.rebase(NS, "p-1", 7, [], author="alice")


def test_a_draft_of_another_ontology_than_the_head_is_refused_before_nothing_to_rebase():
    table = dynamo_store._get_table()
    table.update_item(  # the draft is built on the head's version, but not of the head's ontology
        Key=store._key(NS, "p-1"),
        UpdateExpression="SET #st.#o = :other",
        ExpressionAttributeNames={"#st": "studio", "#o": "ontology_id"},
        ExpressionAttributeValues={":other": f"{ONTO[:-1]}-other#"},
    )
    with pytest.raises(store.StudioError, match="publishes versions of ontology") as raised:
        rebase.rebase(NS, "p-1", 0, [], author="alice")
    assert not isinstance(raised.value, rebase.NothingToRebase)


def test_rebase_without_edits_just_moves_the_base():
    publish_base(V2, "", "p-other")
    result = rebase.rebase(NS, "p-1", 0, [], author="alice")
    assert result["replayed"] == 0 and not result["canUndo"]
    assert stored_state().applied == () and stored_state().base_version == 2


@pytest.mark.parametrize("status", ["accepting", "accepted", "cancelled", "rejected"])
def test_a_proposal_that_left_review_cannot_be_rebased(status):
    publish_base(V2, "", "p-other")  # there is something to rebase onto
    dynamo_store.update_proposal("p-1", namespace=NS, status=status)
    with pytest.raises(store.NotEditable) as raised:
        rebase.rebase(NS, "p-1", 0, [], author="alice")
    assert (raised.value.status, raised.value.detail()["code"]) == (409, "ProposalNotEditable")
    assert stored_row()["status"] == status  # a refused write does not turn the proposal back into "updated"
    assert (stored_state().revision, stored_state().base_version) == (0, 1)
    assert store._get(store.record_key(NS, "p-1", 1)) is None


@pytest.mark.parametrize("status", ["pending", "updated", "accept_failed"])
def test_a_proposal_still_in_review_can_be_rebased(status):
    publish_base(V2, "", "p-other")
    dynamo_store.update_proposal("p-1", namespace=NS, status=status)
    assert rebase.rebase(NS, "p-1", 0, [], author="alice")["baseVersion"] == 2
    assert stored_row()["status"] == "updated"
