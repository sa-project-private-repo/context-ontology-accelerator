# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import logging
from unittest.mock import patch

import pytest
from coa_ontology import dynamo_store
from coa_ontology.abox import store as abox_store
from coa_ontology.studio import graph_view
from coa_ontology.studio.ops import Draft
from rdflib import OWL, RDF, RDFS, BNode, Literal, URIRef
from rdflib.namespace import XSD

from .studio_fakes import (
    CUSTOMER,
    EMAIL,
    FIBO,
    ITEM,
    NS,
    ONTO,
    ORDER,
    PARTY,
    PLACED_BY,
    PROPOSAL_TBOX,
    SCHEME,
    SHIPMENT,
    TOTAL,
    VIP,
    draft,
    publish_base,
)

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _env():
    graph_view._version_classes.cache_clear()
    with patch("coa_ontology.proposals._generate_schema_sql", return_value=None):
        yield
    graph_view._version_classes.cache_clear()


def _ids(view: dict) -> list[str]:
    return [n["iri"] for n in view["nodes"]]


def test_full_graph_has_classes_subclass_and_relationship_edges_and_attributes():
    view = graph_view.graph_view(draft(), SCHEME, revision=3, focus=None, badges=None)
    assert view["mode"] == "full" and view["totalClasses"] == 5 and view["revision"] == 3
    assert _ids(view) == sorted([PARTY, CUSTOMER, VIP, ORDER, ITEM])
    kinds = {(e["kind"], e["source"], e["target"]) for e in view["edges"]}
    assert ("subClassOf", VIP, CUSTOMER) in kinds and ("subClassOf", CUSTOMER, PARTY) in kinds
    assert ("objectProperty", ORDER, CUSTOMER) in kinds
    customer = next(n for n in view["nodes"] if n["iri"] == CUSTOMER)
    assert customer["grounded"] and customer["editable"]
    assert customer["attributes"] == [
        {"iri": EMAIL, "label": "email", "range": "http://www.w3.org/2001/XMLSchema#string"}
    ]
    assert customer["aboxCount"] is None and customer["afterDeploy"] is False
    assert FIBO not in _ids(view)


def test_edges_carry_a_stable_id_and_a_relationship_its_property_and_label():
    edges = {e["id"]: e for e in graph_view.graph_view(draft(), SCHEME, revision=0, focus=None, badges=None)["edges"]}
    assert edges[f"sub|{VIP}|{CUSTOMER}"] == {
        "id": f"sub|{VIP}|{CUSTOMER}",
        "kind": "subClassOf",
        "source": VIP,
        "target": CUSTOMER,
    }
    assert edges[f"op|{PLACED_BY}|{ORDER}|{CUSTOMER}"] == {
        "id": f"op|{PLACED_BY}|{ORDER}|{CUSTOMER}",
        "kind": "objectProperty",
        "source": ORDER,
        "target": CUSTOMER,
        "iri": PLACED_BY,
        "label": "placed by",
    }


def test_a_class_lists_at_most_max_attributes_and_counts_them_all():
    d = draft()
    for i in range(graph_view.MAX_ATTRIBUTES + 1):
        a = URIRef(f"{ONTO}item_a{i:03d}")
        d.tbox.add((a, RDF.type, OWL.DatatypeProperty))
        d.tbox.add((a, RDFS.domain, URIRef(ITEM)))
        d.tbox.add((a, RDFS.range, XSD.string))
    item = next(
        n for n in graph_view.graph_view(d, SCHEME, revision=0, focus=None, badges=None)["nodes"] if n["iri"] == ITEM
    )
    assert (
        len(item["attributes"]) == graph_view.MAX_ATTRIBUTES and item["attributeCount"] == graph_view.MAX_ATTRIBUTES + 1
    )


def _many(n: int) -> Draft:
    d = draft()
    for i in range(n):
        c = URIRef(f"{ONTO}C{i:03d}")
        d.tbox.add((c, RDF.type, OWL.Class))
        d.tbox.add((c, RDFS.label, Literal(f"C{i:03d}")))
        d.tbox.add((c, RDFS.subClassOf, URIRef(ITEM)))
    return d


def test_above_the_limit_the_view_focuses_on_the_most_connected_class():
    view = graph_view.graph_view(_many(160), SCHEME, revision=0, focus=None, badges=None)
    assert view["mode"] == "focus" and view["focus"] == ITEM and view["totalClasses"] == 165
    assert len(view["nodes"]) == graph_view.MAX_FOCUS_NODES and view["truncated"]


def test_focus_takes_two_hops():
    view = graph_view.graph_view(draft(), SCHEME, revision=0, focus=VIP, badges=None)
    assert view["mode"] == "focus" and view["focus"] == VIP
    assert set(_ids(view)) == {VIP, CUSTOMER, PARTY, ORDER}
    assert not view["truncated"]


def test_an_isolated_focus_shows_just_itself_and_an_unknown_one_is_a_key_error():
    d = draft()
    d.tbox.add((URIRef(SHIPMENT), RDF.type, OWL.Class))
    assert _ids(graph_view.graph_view(d, SCHEME, revision=0, focus=SHIPMENT, badges=None)) == [SHIPMENT]
    with pytest.raises(KeyError):
        graph_view.graph_view(d, SCHEME, revision=0, focus=f"{ONTO}Nope", badges=None)


def _abox(versions_aws, conforms: int | None, status: str = abox_store.READY) -> None:
    versions_aws.table.put_item(Item={"PK": dynamo_store._namespace_pk(NS), "SK": "ACTIVE", "version": 1})
    abox_store.put_state(
        NS,
        status=status,
        conforms_to_version=conforms,
        class_counts=[{"class_iri": CUSTOMER, "label": "Customer", "count": 12}],
    )


def test_badges_count_active_classes_and_mark_draft_only_ones(versions_aws):
    publish_base()
    _abox(versions_aws, conforms=1)
    d = draft()
    d.tbox.add((URIRef(SHIPMENT), RDF.type, OWL.Class))
    view = graph_view.graph_view(d, SCHEME, revision=0, focus=None, badges=graph_view.abox_badges(NS))
    by_iri = {n["iri"]: n for n in view["nodes"]}
    assert (by_iri[CUSTOMER]["aboxCount"], by_iri[CUSTOMER]["afterDeploy"]) == (12, False)
    assert (by_iri[ORDER]["aboxCount"], by_iri[ORDER]["afterDeploy"]) == (0, False)
    assert (by_iri[SHIPMENT]["aboxCount"], by_iri[SHIPMENT]["afterDeploy"]) == (None, True)
    assert view["abox"] == {
        "status": "READY",
        "current": True,
        "conformsToVersion": 1,
        "activeVersion": 1,
        "reason": None,
    }


def test_badges_of_an_out_of_date_abox_carry_the_reason_and_none_without_an_abox(versions_aws):
    publish_base()
    assert graph_view.abox_badges(NS) is None
    _abox(versions_aws, conforms=None, status=abox_store.BUILDING)
    badges = graph_view.abox_badges(NS)
    assert badges is not None and not badges.current
    assert "not been built" in badges.view()["reason"]


def test_badges_of_a_stale_abox_name_both_versions_and_count_the_new_classes_as_zero(versions_aws):
    publish_base()
    publish_base(PROPOSAL_TBOX, "", "p-other")  # v2 adds Shipment
    _abox(versions_aws, conforms=1)
    versions_aws.table.put_item(Item={"PK": dynamo_store._namespace_pk(NS), "SK": "ACTIVE", "version": 2})
    badges = graph_view.abox_badges(NS)
    assert badges is not None and not badges.current
    view = badges.view()
    assert (view["conformsToVersion"], view["activeVersion"]) == (1, 2)
    assert view["reason"] == "The domain data was built for v1; v2 is active"
    assert badges.badge(CUSTOMER) == {"aboxCount": 12, "afterDeploy": False}
    assert badges.badge(SHIPMENT) == {"aboxCount": 0, "afterDeploy": False}


def test_there_are_no_badges_for_an_empty_abox_nor_without_a_readable_active_version(versions_aws, caplog):
    def warnings() -> list[logging.LogRecord]:
        # Only this module's warnings count: importing coa_ontology.main sets the root level to INFO,
        # after which caplog also holds unrelated INFO records (publish_base's own log line).
        return [r for r in caplog.records if r.name == graph_view.__name__ and r.levelno >= logging.WARNING]

    publish_base()
    with caplog.at_level(logging.WARNING, logger=graph_view.__name__):
        abox_store.put_state(NS, status=abox_store.EMPTY)
        assert graph_view.abox_badges(NS) is None
        _abox(versions_aws, conforms=1)
        versions_aws.table.delete_item(Key={"PK": dynamo_store._namespace_pk(NS), "SK": "ACTIVE"})
        assert graph_view.abox_badges(NS) is None  # no active version
        assert not warnings()  # an empty ABox and a namespace with no active version are ordinary states
        versions_aws.table.put_item(Item={"PK": dynamo_store._namespace_pk(NS), "SK": "ACTIVE", "version": 7})
        assert graph_view.abox_badges(NS) is None  # v7 has no snapshot
    (record,) = warnings()
    assert record.levelno == logging.WARNING and record.name == graph_view.__name__
    assert record.getMessage() == f"graph badges: active v7 of {NS} unreadable"
    assert record.exc_info is not None


def test_term_view_carries_the_class_badge_and_the_column_of_a_datatype_property(versions_aws):
    publish_base()
    _abox(versions_aws, conforms=1)
    badges = graph_view.abox_badges(NS)
    assert graph_view.term_view(draft(), SCHEME, CUSTOMER, badges)["abox"] == {"aboxCount": 12, "afterDeploy": False}
    assert graph_view.term_view(draft(), SCHEME, CUSTOMER, None)["abox"] is None
    attr = graph_view.term_view(draft(), SCHEME, EMAIL, badges)
    assert attr["kind"] == "datatypeProperty"
    assert attr["domain"] == [{"iri": CUSTOMER, "label": "Customer", "declared": True}]
    assert attr["range"] == [{"iri": str(XSD.string), "label": "string", "declared": False}]
    assert attr["mappings"] == [
        {"triplesMap": f"{ONTO}TriplesMap_Customer", "column": "email", "parentTriplesMap": None}
    ]
    assert "abox" not in attr


def test_term_view_of_a_class_and_a_property():
    term = graph_view.term_view(draft(), SCHEME, CUSTOMER, None)
    assert term["kind"] == "class" and term["label"] == "Customer" and term["comment"] == "A buyer"
    assert [p["iri"] for p in term["parents"]] == sorted([FIBO, PARTY])
    assert [c["iri"] for c in term["children"]] == [VIP]
    assert term["matches"] == [{"target": FIBO, "kind": "exact"}]
    assert {(p["iri"], p["direction"]) for p in term["properties"]} == {(EMAIL, "outgoing"), (PLACED_BY, "incoming")}
    assert term["mappings"] == [{"triplesMap": f"{ONTO}TriplesMap_Customer", "table": "customer"}]
    prop = graph_view.term_view(draft(), SCHEME, PLACED_BY, None)
    assert prop["kind"] == "objectProperty" and prop["domain"] == [{"iri": ORDER, "label": "Order", "declared": True}]
    assert prop["mappings"][0]["parentTriplesMap"] == f"{ONTO}TriplesMap_Customer"
    with pytest.raises(KeyError):
        graph_view.term_view(draft(), SCHEME, f"{ONTO}Nope", None)


def test_a_class_term_says_whether_structured_queries_answer_it():
    """The Inspector had to guess it from "has any subclass"; the payload now carries serve's answer."""
    buyer, gold = (f"{ONTO}{n}" for n in ("Buyer", "GoldCustomer"))
    d = draft()
    for c in (buyer, gold):
        d.tbox.add((URIRef(c), RDF.type, OWL.Class))
    d.tbox.add((URIRef(buyer), OWL.equivalentClass, URIRef(CUSTOMER)))  # equivalent to a mapped class
    d.tbox.add((URIRef(gold), RDFS.subClassOf, URIRef(VIP)))  # VIP customer has no map, so neither is answered
    answered = {
        iri: graph_view.term_view(d, SCHEME, iri, None)["structuredAnswered"]
        for iri in (CUSTOMER, PARTY, buyer, VIP, gold, ITEM)
    }
    assert answered == {CUSTOMER: True, PARTY: True, buyer: True, VIP: False, gold: False, ITEM: False}
    # A property's answerability is the unmapped-properties check's, not this field.
    assert "structuredAnswered" not in graph_view.term_view(d, SCHEME, EMAIL, None)


def test_term_refs_say_whether_the_draft_declares_them():
    d = draft()
    term = graph_view.term_view(d, SCHEME, CUSTOMER, None)
    # A FIBO parent is named by the draft but not declared in it; a draft parent is.
    assert {p["iri"]: p["declared"] for p in term["parents"]} == {PARTY: True, FIBO: False}
    assert term["children"] == [{"iri": VIP, "label": "VIP customer", "declared": True}]
    props = {p["iri"]: p for p in term["properties"]}
    assert (props[EMAIL]["other"], props[EMAIL]["otherDeclared"]) == (str(XSD.string), False)  # an XSD range
    assert (props[PLACED_BY]["other"], props[PLACED_BY]["otherDeclared"]) == (ORDER, True)
    assert graph_view.term_view(d, SCHEME, EMAIL, None)["range"] == [
        {"iri": str(XSD.string), "label": "string", "declared": False}
    ]
    # A property's range can be a class the draft does not declare; a domain it does.
    d.tbox.set((URIRef(PLACED_BY), RDFS.range, URIRef(FIBO)))
    placed_by = graph_view.term_view(d, SCHEME, PLACED_BY, None)
    assert [(r["iri"], r["declared"]) for r in placed_by["range"]] == [(FIBO, False)]
    assert [(r["iri"], r["declared"]) for r in placed_by["domain"]] == [(ORDER, True)]


def test_an_anonymous_end_of_a_property_is_never_declared():
    d = draft()
    linked, anon = URIRef(f"{ONTO}linked"), BNode()
    d.tbox.add((linked, RDF.type, OWL.ObjectProperty))
    d.tbox.add((linked, RDFS.domain, URIRef(CUSTOMER)))
    d.tbox.add((linked, RDFS.range, anon))
    d.tbox.add((anon, RDF.type, OWL.Class))  # typed like a class, but it has no IRI to open
    props = {p["iri"]: p for p in graph_view.term_view(d, SCHEME, CUSTOMER, None)["properties"]}
    assert props[str(linked)]["other"] == str(anon)
    assert props[str(linked)]["otherDeclared"] is False
    with pytest.raises(KeyError):
        graph_view.term_view(d, SCHEME, str(anon), None)


def test_a_term_ref_is_declared_exactly_when_the_term_view_opens_it():
    d = draft()
    ghost, bare = URIRef(f"{ONTO}ghost"), URIRef(f"{ONTO}no_range")
    d.tbox.add((ghost, RDF.type, OWL.AnnotationProperty))  # typed, but not a term the view opens
    d.tbox.add((URIRef(CUSTOMER), RDFS.subClassOf, ghost))
    d.tbox.add((bare, RDF.type, OWL.DatatypeProperty))
    d.tbox.add((bare, RDFS.domain, URIRef(CUSTOMER)))
    customer = graph_view.term_view(d, SCHEME, CUSTOMER, None)
    assert {p["iri"]: p["declared"] for p in customer["parents"]}[str(ghost)] is False
    assert {p["iri"]: (p["other"], p["otherDeclared"]) for p in customer["properties"]}[str(bare)] == (None, False)
    for iri in (*(str(c) for c in d.tbox.subjects(RDF.type, OWL.Class)), CUSTOMER, EMAIL, PLACED_BY, TOTAL):
        view = graph_view.term_view(d, SCHEME, iri, None)
        refs = [
            *(view.get("parents") or []),
            *(view.get("children") or []),
            *(view.get("domain") or []),
            *(view.get("range") or []),
            *({"iri": p["other"], "declared": p["otherDeclared"]} for p in view.get("properties") or [] if p["other"]),
        ]
        for ref in refs:
            try:
                graph_view.term_view(d, SCHEME, ref["iri"], None)
                opens = True
            except KeyError:
                opens = False
            assert ref["declared"] is opens, ref["iri"]


def test_search_ranks_exact_then_prefix_then_substring():
    d = draft()
    hits = graph_view.search_terms(d, "customer", None, 10)
    # exact label, then substrings by label: "email" (its IRI tail is customer_email) before "vip customer"
    assert [h["iri"] for h in hits] == [CUSTOMER, EMAIL, VIP]
    assert [h["iri"] for h in graph_view.search_terms(d, "CUSTOMER", "class", 10)] == [CUSTOMER, VIP]
    assert graph_view.search_terms(d, "  ", None, 10) == []


def test_search_lists_only_terms_the_term_view_can_open_once_each():
    d = draft()
    note, fn, pun = (URIRef(f"{ONTO}{n}") for n in ("customer_note", "customer_flag", "customer_pun"))
    d.tbox.add((note, RDF.type, OWL.AnnotationProperty))
    d.tbox.add((fn, RDF.type, OWL.FunctionalProperty))
    d.tbox.add((pun, RDF.type, OWL.Class))
    d.tbox.add((pun, RDF.type, OWL.ObjectProperty))
    hits = graph_view.search_terms(d, "customer", None, 10)
    assert [(h["iri"], h["kind"]) for h in hits] == [
        (CUSTOMER, "class"),
        (str(pun), "class"),  # unlabelled: its label is its IRI tail, a prefix match
        (EMAIL, "datatypeProperty"),
        (VIP, "class"),
    ]
    for hit in hits:
        assert graph_view.term_view(d, SCHEME, hit["iri"], None)["kind"] == hit["kind"]


def test_search_caps_the_limit_and_filters_by_kind():
    d = _many(60)
    assert len(graph_view.search_terms(d, "c0", None, 500)) == graph_view.MAX_SEARCH
    assert len(graph_view.search_terms(d, "c0", None, 3)) == 3
    assert graph_view.search_terms(d, "c0", None, 0) == []
    assert graph_view.search_terms(d, "c0", None, -1) == []
    assert [h["iri"] for h in graph_view.search_terms(d, "email", "objectProperty", 10)] == []
    assert [h["iri"] for h in graph_view.search_terms(d, "placed", "objectProperty", 10)] == [PLACED_BY]


def test_the_views_only_read_the_draft():
    d = draft()
    before = (set(d.tbox), set(d.r2rml))
    graph_view.graph_view(d, SCHEME, revision=0, focus=None, badges=None)
    graph_view.graph_view(d, SCHEME, revision=0, focus=VIP, badges=None)
    for iri in (CUSTOMER, ORDER, PLACED_BY, EMAIL):
        graph_view.term_view(d, SCHEME, iri, None)
    graph_view.search_terms(d, "customer", None, 10)
    assert (set(d.tbox), set(d.r2rml)) == before


def _preview(ops: list[dict], focus: str | None = None) -> dict:
    from coa_ontology.studio.ops import USER_OPS, apply_ops
    from coa_ontology.versions import snapshot

    before = draft()
    after, _ = apply_ops(before, USER_OPS.validate_python(ops), SCHEME)
    diff = snapshot.compute_diff(before.tbox, after.tbox)
    return graph_view.preview_view(before, after, SCHEME, revision=4, focus=focus, badges=None, diff=diff)


def test_preview_marks_what_a_change_set_adds_changes_and_removes():
    supplier = f"{ONTO}Supplier"
    view = _preview(
        [
            {"op": "add_class", "label": "Supplier", "parents": [PARTY], "iri": supplier},
            {"op": "set_label", "iri": ORDER, "label": "Purchase order"},
            {"op": "delete_class", "iri": VIP, "cascade": "detach"},
            {"op": "add_datatype_property", "label": "sku", "domain": ITEM, "range": "xsd:string"},
            {"op": "update_property", "iri": PLACED_BY, "label": "ordered by"},
        ]
    )
    assert view["revision"] == 4 and view["mode"] == "full"
    marks = {n["iri"]: n["preview"] for n in view["nodes"]}
    assert marks == {
        supplier: "added",
        ORDER: "changed",
        ITEM: "changed",  # it gained an attribute
        VIP: "removed",
        PARTY: None,
        CUSTOMER: None,
    }
    edges = {e["id"]: e["preview"] for e in view["edges"]}
    assert edges[f"sub|{supplier}|{PARTY}"] == "added"
    assert edges[f"sub|{VIP}|{CUSTOMER}"] == "removed"
    assert edges[f"op|{PLACED_BY}|{ORDER}|{CUSTOMER}"] == "changed"
    assert edges[f"sub|{CUSTOMER}|{PARTY}"] is None
    vip = next(n for n in view["nodes"] if n["iri"] == VIP)
    assert vip["label"] == "VIP customer"  # a removed class keeps what it showed before


def test_preview_of_a_focus_view_drops_a_focus_the_change_set_deletes():
    view = _preview([{"op": "delete_class", "iri": VIP, "cascade": "detach"}], focus=VIP)
    assert view["mode"] == "full"  # the focus is gone, and five classes fit the full view
    assert {n["iri"]: n["preview"] for n in view["nodes"]}[VIP] == "removed"
    kept = _preview([{"op": "set_label", "iri": ORDER, "label": "PO"}], focus=ORDER)
    assert kept["mode"] == "focus" and kept["focus"] == ORDER


def test_preview_in_a_focus_view_marks_only_edges_the_change_adds():
    # Order's relationship to Customer exists already; it only enters the window around Item.
    view = _preview([{"op": "add_parent", "iri": ITEM, "parent": ORDER}], focus=ITEM)
    assert view["mode"] == "focus"
    edges = {e["id"]: e["preview"] for e in view["edges"]}
    assert edges[f"sub|{ITEM}|{ORDER}"] == "added"
    assert edges[f"op|{PLACED_BY}|{ORDER}|{CUSTOMER}"] is None
