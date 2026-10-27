# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Instant checks and data comparison of a designer draft."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from coa_common.constants import VOCAB_URI
from coa_ontology.abox import mapping as abox_mapping
from coa_ontology.studio import checks
from coa_ontology.studio import draft as store
from coa_ontology.studio.graph_view import AboxBadges
from coa_ontology.studio.ops import Draft, parse_nt
from coa_ontology.versions import snapshot
from rdflib import OWL, RDF, RDFS, Literal, URIRef
from rdflib.namespace import XSD

from .studio_fakes import (
    CUSTOMER,
    EMAIL,
    ITEM,
    NS,
    ONTO,
    ORDER,
    PARTY,
    PLACED_BY,
    R2RML,
    SCHEME,
    TOTAL,
    VIP,
    draft,
    opened_draft,
)

pytestmark = [pytest.mark.unit]


def _codes(result: dict) -> list[tuple[str, str]]:
    return [(f["code"], f["severity"]) for f in result["findings"]]


def test_the_sample_draft_has_no_error_and_lists_isolated_classes():
    result = checks.instant_checks(draft(), SCHEME)
    assert result["counts"]["error"] == 0 and not result["truncated"]
    isolated = [f["iris"] for f in result["findings"] if f["code"] == "ISOLATED_CLASS"]
    assert isolated == [[ITEM]]  # Party has Customer below it; Order and Customer are related


def test_dangling_axes_and_a_class_as_a_datatype_range_are_errors():
    d = draft()
    ghost = URIRef(f"{ONTO}Ghost")
    d.tbox.add((URIRef(f"{ONTO}order_placedBy"), RDFS.range, ghost))
    d.tbox.set((URIRef(EMAIL), RDFS.range, URIRef(PARTY)))
    d.tbox.set((URIRef(TOTAL), RDFS.range, URIRef(f"{XSD}datetime")))
    d.tbox.add((URIRef(TOTAL), RDFS.domain, URIRef("https://spec.edmcouncil.org/fibo/External")))
    found = {(f["code"], f["severity"], tuple(f["iris"])) for f in checks.instant_checks(d, SCHEME)["findings"]}
    assert ("DANGLING_RANGE", "error", tuple(sorted([f"{ONTO}order_placedBy", str(ghost)]))) in found
    assert ("DATATYPE_RANGE_NOT_DATATYPE", "error", tuple(sorted([EMAIL, PARTY]))) in found
    assert ("DANGLING_RANGE", "error", tuple(sorted([TOTAL, f"{XSD}datetime"]))) in found
    # A domain outside the namespace's IRI space is a warning: it may be a reference ontology's class.
    assert ("DANGLING_DOMAIN", "warning", tuple(sorted([TOTAL, "https://spec.edmcouncil.org/fibo/External"]))) in found


def test_a_subclass_cycle_is_one_error_naming_its_classes():
    d = draft()
    d.tbox.add((URIRef(PARTY), RDFS.subClassOf, URIRef(VIP)))
    d.tbox.add((URIRef(ITEM), RDFS.subClassOf, URIRef(ITEM)))
    cycles = [f for f in checks.instant_checks(d, SCHEME)["findings"] if f["code"] == "SUBCLASS_CYCLE"]
    assert {tuple(f["iris"]) for f in cycles} == {(ITEM,), tuple(sorted([CUSTOMER, PARTY, VIP]))}
    assert all(f["severity"] == "error" for f in cycles)
    assert " -> " in cycles[0]["message"]


def test_missing_and_duplicate_labels_are_warnings():
    d = draft()
    d.tbox.remove((URIRef(ITEM), RDFS.label, None))
    d.tbox.set((URIRef(ORDER), RDFS.label, Literal("customer")))
    result = checks.instant_checks(d, SCHEME)
    assert ("MISSING_LABEL", "warning") in _codes(result)
    dup = next(f for f in result["findings"] if f["code"] == "DUPLICATE_LABEL")
    assert dup["iris"] == sorted([CUSTOMER, ORDER]) and dup["message"] == "2 classes share the label 'Customer'"


def _sentences(result: dict) -> dict[str, list[tuple[str, dict[str, str]]]]:
    """Each finding's code, then its sentence's code and parameters, after checking ``message`` is that sentence."""
    from coa_ontology.studio.messages import english

    out: dict[str, list[tuple[str, dict[str, str]]]] = {}
    for f in result["findings"]:
        assert f["message"] == english({"code": f["messageCode"], "params": f["messageParams"]})
        out.setdefault(f["code"], []).append((f["messageCode"], f["messageParams"]))
    return out


def test_each_finding_gives_its_sentence_as_a_code_and_parameters():
    """A Japanese user read the engine's English: a finding had no code for its sentence."""
    d = draft()
    d.tbox.add((URIRef(f"{ONTO}order_placedBy"), RDFS.range, URIRef(f"{ONTO}Ghost")))
    d.tbox.set((URIRef(EMAIL), RDFS.range, URIRef(PARTY)))
    d.tbox.set((URIRef(TOTAL), RDFS.range, URIRef(f"{XSD}datetime")))
    d.tbox.add((URIRef(TOTAL), RDFS.domain, URIRef("https://spec.edmcouncil.org/fibo/External")))
    d.tbox.add((URIRef(PARTY), RDFS.subClassOf, URIRef(VIP)))
    d.tbox.remove((URIRef(ITEM), RDFS.label, None))
    d.tbox.set((URIRef(ORDER), RDFS.label, Literal("customer")))
    d.tbox.add((URIRef(f"{ONTO}customer_mail"), RDF.type, OWL.DatatypeProperty))
    d.tbox.add((URIRef(f"{ONTO}customer_mail"), RDFS.label, Literal("Email")))
    sentences = _sentences(checks.instant_checks(d, SCHEME))
    assert sentences["DANGLING_RANGE"] == [
        ("DANGLING_RANGE", {"property": "placed by", "range": "Ghost"}),
        ("UNKNOWN_DATATYPE_RANGE", {"property": "total", "range": f"{XSD}datetime"}),
    ]
    assert sentences["DATATYPE_RANGE_NOT_DATATYPE"] == [
        ("DATATYPE_RANGE_NOT_DATATYPE", {"property": "email", "range": "Party"})
    ]
    assert sentences["DANGLING_DOMAIN"] == [("DANGLING_DOMAIN", {"property": "total", "domain": "External"})]
    assert sentences["SUBCLASS_CYCLE"] == [("SUBCLASS_CYCLE", {"classes": "Customer -> Party -> VIP customer"})]
    assert sentences["MISSING_LABEL"] == [("MISSING_LABEL", {"term": "Item"})]
    assert sorted(sentences["DUPLICATE_LABEL"]) == [
        ("DUPLICATE_ATTRIBUTE_LABEL", {"count": "2", "label": "email"}),  # the label of the first IRI
        ("DUPLICATE_CLASS_LABEL", {"count": "2", "label": "Customer"}),
    ]
    assert ("ISOLATED_CLASS", {"term": "Item"}) in sentences["ISOLATED_CLASS"]


def test_findings_are_capped_and_counted():
    d = draft()
    for n in range(checks.MAX_FINDINGS + 30):
        d.tbox.add((URIRef(f"{ONTO}Lonely{n}"), RDF.type, OWL.Class))  # unlabeled and isolated: two findings each
    result = checks.instant_checks(d, SCHEME)
    assert len(result["findings"]) == checks.MAX_FINDINGS and result["truncated"]
    assert result["counts"]["warning"] == checks.MAX_FINDINGS + 30
    assert result["counts"]["info"] >= checks.MAX_FINDINGS + 30
    assert result["findings"][0]["severity"] == "warning"  # worst first


@pytest.fixture
def opened(versions_aws):
    store.clear_cache()
    with patch("coa_ontology.proposals._generate_schema_sql", return_value=None):
        opened_draft("p-1")
        yield versions_aws
    store.clear_cache()


def _badges(counts: dict[str, int]) -> AboxBadges:
    return AboxBadges("READY", True, 1, 1, counts, frozenset(counts))


def test_data_checks_report_each_section(opened):
    from coa_ontology.proposals import SchemaSqlCoverageError
    from coa_ontology.studio import changes

    changes.apply_change_set(NS, "p-1", [{"op": "delete_class", "iri": VIP, "cascade": "detach"}], 0, author="a")
    rows = [abox_mapping.MappingRow(kind="class", key=("vip",), target=VIP)]
    with (
        patch(
            "coa_ontology.proposals._generate_schema_sql",
            side_effect=SchemaSqlCoverageError("missing", missing=["orders"]),
        ),
        patch.object(checks.graph_view, "abox_badges", return_value=_badges({VIP: 12, CUSTOMER: 3})),
        patch.object(checks.abox_mapping, "list_rows", return_value=rows),
        patch.object(checks.registry, "get_active", return_value={"version": 1}),  # v1 is published, not deployed
        patch.object(checks.version_deploy, "find_breaking_metrics", return_value=[{"metricId": "m1"}]),
    ):
        result = checks.checks(NS, "p-1")
    data = result["data"]
    assert result["revision"] == 1
    assert data["tables"] == {"checked": True, "missing": ["orders"], "error": None}
    assert data["abox"]["impacts"] == [{"classIri": VIP, "instances": 12, "change": "removed", "toIri": None}]
    assert data["mappings"]["stale"] == 1 and data["mappings"]["staleTargets"] == [VIP]
    assert data["metrics"]["breaking"] == [{"metricId": "m1"}]


def test_a_renamed_class_is_reported_with_where_it_went(opened):
    from coa_ontology.studio import changes

    op = {"op": "change_iri", "iri": CUSTOMER, "newIri": f"{ONTO}Client"}
    changes.apply_change_set(NS, "p-1", [op], 0, author="a")
    with (
        patch.object(checks.graph_view, "abox_badges", return_value=_badges({CUSTOMER: 3})),
        patch.object(checks.abox_mapping, "list_rows", return_value=[]),
        patch.object(checks.version_deploy, "find_breaking_metrics", return_value=[]),
    ):
        impacts = checks.checks(NS, "p-1")["data"]["abox"]["impacts"]
    assert impacts == [{"classIri": CUSTOMER, "instances": 3, "change": "renamed", "toIri": f"{ONTO}Client"}]


def _abox_impacts(counts: dict[str, int]) -> list[dict]:
    with (
        patch.object(checks.graph_view, "abox_badges", return_value=_badges(counts)),
        patch.object(checks.abox_mapping, "list_rows", return_value=[]),
        patch.object(checks.version_deploy, "find_breaking_metrics", return_value=[]),
    ):
        impacts: list[dict] = checks.checks(NS, "p-1")["data"]["abox"]["impacts"]
    return impacts


def test_a_class_renamed_and_then_deleted_is_removed(opened):
    """Checks said the class was renamed to a class the draft no longer has."""
    from coa_ontology.studio import changes

    client = f"{ONTO}Client"
    changes.apply_change_set(NS, "p-1", [{"op": "change_iri", "iri": CUSTOMER, "newIri": client}], 0, author="a")
    changes.apply_change_set(NS, "p-1", [{"op": "delete_class", "iri": client}], 1, author="a")
    assert _abox_impacts({CUSTOMER: 3}) == [{"classIri": CUSTOMER, "instances": 3, "change": "removed", "toIri": None}]


def test_a_merged_class_is_merged_into_its_target(opened):
    """A class merged into another read 'renamed to' it."""
    from coa_ontology.studio import changes

    changes.apply_change_set(NS, "p-1", [{"op": "merge_classes", "source": VIP, "target": CUSTOMER}], 0, author="a")
    assert _abox_impacts({VIP: 4}) == [{"classIri": VIP, "instances": 4, "change": "merged", "toIri": CUSTOMER}]
    # A target the draft deletes later takes the merged class with it.
    changes.apply_change_set(NS, "p-1", [{"op": "delete_class", "iri": CUSTOMER}], 1, author="a")
    impacts = _abox_impacts({VIP: 4, CUSTOMER: 3})
    assert impacts == [
        {"classIri": VIP, "instances": 4, "change": "removed", "toIri": None},
        {"classIri": CUSTOMER, "instances": 3, "change": "removed", "toIri": None},
    ]


def test_a_failing_dependency_fills_only_its_section(opened, caplog):
    with (
        patch("coa_ontology.proposals._generate_schema_sql", side_effect=RuntimeError("catalog down")),
        patch.object(checks.graph_view, "abox_badges", return_value=None),
        patch.object(checks.abox_mapping, "list_rows", side_effect=RuntimeError("ddb down")),
        patch.object(checks.version_deploy, "find_breaking_metrics", side_effect=RuntimeError("neptune down")),
    ):
        data = checks.checks(NS, "p-1")["data"]
    assert data["tables"] == {"error": checks.TABLES_ERROR, "errorCode": "TABLES_UNREADABLE"}
    assert data["abox"] == {"available": False, "impacts": [], "error": None}
    assert data["mappings"] == {"error": checks.MAPPINGS_ERROR, "errorCode": "MAPPINGS_UNREADABLE"}
    assert data["metrics"] == {"error": checks.METRICS_ERROR, "errorCode": "METRICS_UNCHECKED"}
    assert "catalog down" not in str(data)


def test_a_domain_data_section_that_fails_names_its_error_by_code(opened):
    """The web app wrote a section's error as the engine's English text in every language."""
    with (
        patch.object(checks.graph_view, "abox_badges", side_effect=RuntimeError("neptune down")),
        patch.object(checks.abox_mapping, "list_rows", return_value=[]),
        patch.object(checks.version_deploy, "find_breaking_metrics", return_value=[]),
    ):
        data = checks.checks(NS, "p-1")["data"]
    assert data["abox"] == {"error": checks.ABOX_ERROR, "errorCode": "ABOX_UNREADABLE"}
    sections = {checks.TABLES_ERROR, checks.ABOX_ERROR, checks.MAPPINGS_ERROR, checks.METRICS_ERROR}
    # Every section's error has a code, and so does the structured check's.
    assert set(checks.ERROR_CODES) == {*sections, checks.BASE_MAPPING_ERROR}


def test_stale_mappings_list_one_target_for_each_row_they_count(opened):
    """Two document strings that map to the class the draft deletes are two stale mappings."""
    from coa_ontology.studio import changes

    changes.apply_change_set(NS, "p-1", [{"op": "delete_class", "iri": VIP, "cascade": "detach"}], 0, author="a")
    rows = [abox_mapping.MappingRow(kind="class", key=(k,), target=VIP) for k in ("vip", "v.i.p.")]
    with (
        patch.object(checks.graph_view, "abox_badges", return_value=_badges({VIP: 12})),
        patch.object(checks.abox_mapping, "list_rows", return_value=rows),
        patch.object(checks.registry, "get_active", return_value={"version": 1}),
        patch.object(checks.version_deploy, "find_breaking_metrics", return_value=[]),
    ):
        mappings = checks.checks(NS, "p-1")["data"]["mappings"]
    assert mappings["stale"] == 2 and mappings["staleTargets"] == [VIP, VIP]


def test_a_draft_without_a_mapping_skips_the_table_check(opened):
    rev = store.load_revision(NS, "p-1", 0)
    item = store.get_proposal(NS, "p-1")
    state = store.require_state(item, editable=False)
    empty = store.Revision(0, rev.draft, rev.tbox_nt, "")
    assert checks._tables(NS, item, state, rev.draft, empty.r2rml_nt) == {"checked": False, "missing": []}


# ── Structured queries ──────────────────────────────────────────────────────


def _structured(d, *, base_maps_tables: bool = True, only: set[str] | None = None) -> dict:
    return checks.structured_check(d, base_maps_tables=base_maps_tables, only=only)


def _edited(*raw: dict, d=None):
    from coa_ontology.studio import ops

    after, _ = ops.apply_ops(d or draft(), ops.USER_OPS.validate_python(list(raw)), SCHEME)
    return after


def _terms(group: dict) -> list[tuple]:
    return [tuple(t.values()) for t in group["terms"]]


def test_structured_check_lists_the_classes_no_table_maps():
    group = _structured(draft())["unmappedClasses"]
    # Customer and Order are the rr:class of a TriplesMap, and Party is answered through Customer, its
    # subclass; VIP customer (under Customer, with no map of its own) and Item are not.
    assert group == {
        "count": 2,
        "terms": [
            {"iri": ITEM, "label": "Item"},
            {"iri": VIP, "label": "VIP customer"},
        ],
    }


def test_a_parent_or_an_equivalent_of_a_mapped_class_is_answered_through_it():
    """Checks listed the documents class Customer above the table class customers, which the VKG answers."""
    agent, buyer, purchase, gold = (f"{ONTO}{n}" for n in ("Agent", "Buyer", "Purchase", "GoldCustomer"))
    d = _edited(
        {"op": "add_class", "label": "Agent"},
        {"op": "add_parent", "iri": PARTY, "parent": agent},  # above Customer through Party
        {"op": "add_class", "label": "Gold customer", "parents": [VIP]},  # VIP customer has no map
    )
    for c in (buyer, purchase):
        d.tbox.add((URIRef(c), RDF.type, OWL.Class))
    d.tbox.add((URIRef(buyer), OWL.equivalentClass, URIRef(CUSTOMER)))
    d.tbox.add((URIRef(ORDER), OWL.equivalentClass, URIRef(purchase)))
    assert [t["iri"] for t in _structured(d)["unmappedClasses"]["terms"]] == [gold, ITEM, VIP]
    # The accept preview judges the terms it names on the whole draft: their mapped classes are not among them.
    assert _structured(d, only={agent, PARTY, buyer, purchase})["unmappedClasses"]["count"] == 0


def test_a_class_named_only_by_an_untyped_map_is_not_mapped():
    d = draft()
    d.r2rml.remove((URIRef(f"{ONTO}TriplesMap_Order"), RDF.type, None))
    assert [t["iri"] for t in _structured(d)["unmappedClasses"]["terms"]] == [ITEM, ORDER, VIP]


def test_structured_check_lists_attributes_and_relationships_of_mapped_classes_that_no_mapping_reads():
    d = _edited(
        {"op": "add_datatype_property", "label": "loyalty tier", "domain": CUSTOMER, "range": "xsd:string"},
        {"op": "add_object_property", "label": "preferred order", "domain": CUSTOMER, "range": ORDER},
        {"op": "add_datatype_property", "label": "sku", "domain": ITEM, "range": "xsd:string"},  # Item is unmapped
    )
    group = _structured(d)["unmappedProperties"]
    assert group == {
        "count": 2,
        "terms": [
            {"iri": f"{ONTO}customer_loyaltyTier", "label": "loyalty tier", "domainIri": CUSTOMER},
            {"iri": f"{ONTO}customer_preferredOrder", "label": "preferred order", "domainIri": CUSTOMER},
        ],
    }


def test_a_relationship_into_a_mapped_class_is_listed_under_that_class():
    # "member" goes from a new class no table maps into the mapped Customer: deploy marks it (the foreign-key
    # walk follows it back from Customer), so the check lists it, with Customer as its mapped class.
    loyalty = f"{ONTO}LoyaltyProgramme"
    d = _edited(
        {"op": "add_class", "label": "Loyalty programme"},
        {"op": "add_object_property", "label": "member", "domain": loyalty, "range": CUSTOMER},
        {"op": "add_object_property", "label": "sponsor", "domain": loyalty, "range": ITEM},  # Item is unmapped
    )
    assert _structured(d)["unmappedProperties"] == {
        "count": 1,
        "terms": [{"iri": f"{ONTO}loyaltyprogramme_member", "label": "member", "domainIri": CUSTOMER}],
    }
    assert _structured(d, only={f"{ONTO}loyaltyprogramme_member"})["unmappedProperties"]["count"] == 1


def test_a_property_mapped_through_a_predicate_map_constant_is_mapped():
    d = draft()
    pom = URIRef(f"{ONTO}TriplesMap_Customer/POM_Email")
    predicate_map = URIRef(f"{ONTO}TriplesMap_Customer/POM_Email/PredicateMap")
    d.r2rml.remove((pom, snapshot.RR.predicate, URIRef(EMAIL)))
    d.r2rml.add((pom, snapshot.RR.predicateMap, predicate_map))
    d.r2rml.add((predicate_map, snapshot.RR.constant, URIRef(EMAIL)))
    assert _structured(d)["unmappedProperties"]["count"] == 0
    d.r2rml.remove((predicate_map, snapshot.RR.constant, URIRef(EMAIL)))
    assert [t["iri"] for t in _structured(d)["unmappedProperties"]["terms"]] == [EMAIL]


def test_structured_check_lists_mapped_terms_whose_label_is_not_their_table_or_column_name():
    # The sample's class labels are capitalised; its tables are customer and orders.
    assert _terms(_structured(draft())["labelDiffers"]) == [
        (CUSTOMER, "Customer", "customer"),
        (ORDER, "Order", "orders"),
    ]
    d = _edited(
        {"op": "set_label", "iri": CUSTOMER, "label": "customer"},
        {"op": "set_label", "iri": EMAIL, "label": "e-mail"},
        {"op": "set_label", "iri": VIP, "label": "Premium"},  # VIP customer is unmapped: no physical name
    )
    assert _terms(_structured(d)["labelDiffers"]) == [(EMAIL, "e-mail", "email"), (ORDER, "Order", "orders")]


def test_physical_names_are_the_unquoted_last_segment_of_the_mapping():
    d = draft()
    table = next(d.r2rml.objects(URIRef(f"{ONTO}TriplesMap_Customer"), snapshot.RR.logicalTable))
    d.r2rml.set((table, snapshot.RR.tableName, Literal('"sales"."Customer"')))
    om = URIRef(f"{ONTO}TriplesMap_Customer/POM_Email/ObjectMap")
    d.r2rml.set((om, snapshot.RR.column, Literal('"E-mail"')))
    assert _terms(_structured(d)["labelDiffers"]) == [(EMAIL, "email", "E-mail"), (ORDER, "Order", "orders")]


def test_structured_check_lists_classes_mapped_from_several_tables():
    d = draft()  # Customer also reads table supplier, as merging a mapped Supplier into it leaves it
    d.r2rml.parse(
        data=f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
        <{ONTO}TriplesMap_Supplier> a rr:TriplesMap ; rr:logicalTable [ rr:tableName "supplier" ] ;
            rr:subjectMap [ rr:class <{CUSTOMER}> ; rr:template "s/{{id}}" ] .""",
        format="turtle",
    )
    result = _structured(d)
    assert result["multiTableClasses"] == {
        "count": 1,
        "terms": [{"iri": CUSTOMER, "label": "Customer", "tables": ["customer", "supplier"]}],
    }
    # NL-to-SQL keeps the labels of a class read from several tables, so none of them is listed as renamed.
    assert _terms(result["labelDiffers"]) == [(ORDER, "Order", "orders")]


def _also_read_from(d, name: str, table: str, source: str = "") -> None:
    """Map Customer from ``table`` too, through TriplesMap ``name`` (of datasource ``source`` when given)."""
    source_triple = f'<{VOCAB_URI}datasourceId> "{source}" ;' if source else ""
    d.r2rml.parse(
        data=f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
        <{ONTO}{name}> a rr:TriplesMap ; {source_triple} rr:logicalTable [ rr:tableName {table} ] ;
            rr:subjectMap [ rr:class <{CUSTOMER}> ; rr:template "{name}/{{id}}" ] .""",
        format="turtle",
    )


def test_tables_that_share_only_their_name_are_several_tables():
    d = draft()
    # logical_table_names qualifies a name two sources share; the sample's customer table is the crm one.
    customer_table = next(d.r2rml.objects(URIRef(f"{ONTO}TriplesMap_Customer"), snapshot.RR.logicalTable))
    d.r2rml.set((customer_table, snapshot.RR.tableName, Literal('"crm"."customer"')))
    _also_read_from(d, "TriplesMap_BillingCustomer", r'"\"billing\".\"customer\""')
    group = _structured(d)["multiTableClasses"]
    assert group["terms"] == [{"iri": CUSTOMER, "label": "Customer", "tables": ["billing.customer", "crm.customer"]}]
    # Bare names that only the datasource tells apart; the same table through a second map is one table.
    d = draft()
    d.r2rml.add((URIRef(f"{ONTO}TriplesMap_Customer"), URIRef(f"{VOCAB_URI}datasourceId"), Literal("DS#ds-1")))
    _also_read_from(d, "TriplesMap_SameCustomer", '"customer"', source="ds-1")
    assert _structured(d)["multiTableClasses"]["count"] == 0
    _also_read_from(d, "TriplesMap_BillingCustomer", '"customer"', source="ds-2")
    assert _structured(d)["multiTableClasses"]["terms"][0]["tables"] == ["customer [ds-1]", "customer [ds-2]"]


def test_a_class_read_from_more_than_20_tables_lists_20_of_them():
    d = draft()
    for n in range(checks.MAX_TERM_TABLES + 5):
        _also_read_from(d, f"TriplesMap_Part{n:02d}", f'"part{n:02d}"')
    group = _structured(d)["multiTableClasses"]
    assert group["count"] == 1
    assert group["terms"][0]["tables"] == ["customer", *(f"part{n:02d}" for n in range(checks.MAX_TERM_TABLES - 1))]


def test_structured_check_says_when_the_draft_maps_no_table_any_more():
    d = _edited({"op": "delete_class", "iri": CUSTOMER}, {"op": "delete_class", "iri": ORDER})
    assert _structured(d)["mapsNoTable"] is True
    assert _structured(d, base_maps_tables=False)["mapsNoTable"] is False  # nothing was mapped before either
    assert _structured(draft())["mapsNoTable"] is False


def test_structured_groups_list_at_most_100_terms_and_count_them_all():
    d = draft()
    for n in range(checks.MAX_STRUCTURED_TERMS + 5):
        d.tbox.add((URIRef(f"{ONTO}Extra{n:03d}"), RDF.type, OWL.Class))
    group = _structured(d)["unmappedClasses"]
    assert group["count"] == checks.MAX_STRUCTURED_TERMS + 7
    assert len(group["terms"]) == checks.MAX_STRUCTURED_TERMS
    assert group["terms"][0]["iri"] == f"{ONTO}Extra000"  # by label (the IRI tail without one), then IRI


def test_only_keeps_the_terms_it_names():
    d = _edited({"op": "add_class", "label": "Premium customer", "parents": [CUSTOMER]})
    result = _structured(d, only={f"{ONTO}PremiumCustomer", CUSTOMER})
    assert _terms(result["unmappedClasses"]) == [(f"{ONTO}PremiumCustomer", "Premium customer")]
    assert _terms(result["labelDiffers"]) == [(CUSTOMER, "Customer", "customer")]


def test_checks_report_the_structured_check_of_the_current_revision(opened):
    from coa_ontology.studio import changes

    op = {"op": "add_datatype_property", "label": "loyalty tier", "domain": CUSTOMER, "range": "xsd:string"}
    changes.apply_change_set(NS, "p-1", [op], 0, author="a")
    with (
        patch.object(checks.graph_view, "abox_badges", return_value=None),
        patch.object(checks.abox_mapping, "list_rows", return_value=[]),
        patch.object(checks.version_deploy, "find_breaking_metrics", return_value=[]),
    ):
        structured = checks.checks(NS, "p-1")["structured"]
    assert [t["label"] for t in structured["unmappedProperties"]["terms"]] == ["loyalty tier"]
    assert structured["mapsNoTable"] is False and structured["error"] is None


def test_a_base_mapping_that_cannot_be_read_only_sets_the_structured_error(opened, caplog):
    from coa_ontology.studio import changes

    changes.apply_change_set(NS, "p-1", [{"op": "delete_class", "iri": CUSTOMER}], 0, author="a")
    changes.apply_change_set(NS, "p-1", [{"op": "delete_class", "iri": ORDER}], 1, author="a")
    with (
        patch.object(checks.storage, "read_version_file", side_effect=RuntimeError("s3 down at bucket-1")),
        patch.object(checks.graph_view, "abox_badges", return_value=None),
        patch.object(checks.abox_mapping, "list_rows", return_value=[]),
        patch.object(checks.version_deploy, "find_breaking_metrics", return_value=[]),
    ):
        structured = checks.checks(NS, "p-1")["structured"]
    assert structured["error"] == checks.BASE_MAPPING_ERROR and structured["mapsNoTable"] is False
    assert structured["errorCode"] == "BASE_MAPPING_UNREADABLE"  # which the web app writes in the user's language
    # The rest is read from the draft: Party, VIP customer, Item and the proposal's Shipment.
    assert structured["unmappedClasses"]["count"] == 4
    assert "bucket-1" not in str(structured)


def test_a_draft_that_drops_every_mapped_class_of_its_base_maps_no_table(opened):
    from coa_ontology.studio import changes

    changes.apply_change_set(
        NS, "p-1", [{"op": "delete_class", "iri": CUSTOMER}, {"op": "delete_class", "iri": ORDER}], 0, author="a"
    )
    with (
        patch.object(checks.graph_view, "abox_badges", return_value=None),
        patch.object(checks.abox_mapping, "list_rows", return_value=[]),
        patch.object(checks.version_deploy, "find_breaking_metrics", return_value=[]),
    ):
        assert checks.checks(NS, "p-1")["structured"]["mapsNoTable"] is True


# ── What the draft deletes from the base version's mapping ─────────────────


def _removed(d, base=None) -> dict:
    """The two deletion groups of ``d`` against ``base`` (by default the sample draft, as v1 publishes it)."""
    base = base or draft()
    return checks.removed_mapped(base, *checks.deleted_mapped_terms(d, base.r2rml))


def test_a_deleted_mapped_class_is_listed_with_its_tables_and_its_deleted_properties_with_their_columns():
    result = _removed(_edited({"op": "delete_class", "iri": CUSTOMER, "cascade": "delete"}))
    assert result["removedMappedClasses"] == {
        "count": 1,
        "terms": [{"iri": CUSTOMER, "label": "Customer", "tables": ["customer"]}],
    }
    # email was read from column email of the customer table; placed by joins orders to it.
    assert result["removedMappedProperties"] == {
        "count": 2,
        "terms": [
            {"iri": EMAIL, "label": "email", "domainIri": CUSTOMER, "physicalName": "email"},
            {"iri": PLACED_BY, "label": "placed by", "domainIri": ORDER},
        ],
    }


def test_the_properties_a_deleted_class_detaches_stay_and_are_not_listed():
    # Their maps go with the class's table, which removedMappedClasses lists.
    result = _removed(_edited({"op": "delete_class", "iri": CUSTOMER, "cascade": "detach"}))
    assert [t["iri"] for t in result["removedMappedClasses"]["terms"]] == [CUSTOMER]
    assert result["removedMappedProperties"] == {"count": 0, "terms": []}


def test_a_deleted_mapped_attribute_names_its_class_and_column():
    result = _removed(_edited({"op": "delete_property", "iri": TOTAL}))
    assert result["removedMappedClasses"] == {"count": 0, "terms": []}
    assert result["removedMappedProperties"]["terms"] == [
        {"iri": TOTAL, "label": "total", "domainIri": ORDER, "physicalName": "total"}
    ]


def test_renamed_and_merged_terms_keep_their_maps_and_are_not_listed():
    renamed = _edited(
        {"op": "change_iri", "iri": CUSTOMER, "newIri": f"{ONTO}Client"},
        {"op": "change_iri", "iri": EMAIL, "newIri": f"{ONTO}client_mail"},
    )
    merged = _edited({"op": "merge_classes", "source": ORDER, "target": CUSTOMER})
    for d in (renamed, merged):
        result = _removed(d)
        assert result["removedMappedClasses"]["count"] == 0 and result["removedMappedProperties"]["count"] == 0
    # Renamed, then deleted: removed, by the label the base version gave it.
    d = _edited(
        {"op": "change_iri", "iri": CUSTOMER, "newIri": f"{ONTO}Client"},
        {"op": "delete_class", "iri": f"{ONTO}Client"},
    )
    assert _terms(_removed(d)["removedMappedClasses"]) == [(CUSTOMER, "Customer", ["customer"])]


def test_a_class_whose_table_the_draft_still_reads_is_not_listed():
    # Maps that are blank nodes are not the same node in two parses; the table they read still is.
    base = draft()
    blank = R2RML.replace(f"<{ONTO}TriplesMap_Customer> a", "_:customer a").replace(
        f"rr:parentTriplesMap <{ONTO}TriplesMap_Customer>", "rr:parentTriplesMap _:customer"
    )
    base.r2rml = snapshot.parse_turtle(blank)
    d = _edited(
        {"op": "change_iri", "iri": CUSTOMER, "newIri": f"{ONTO}Client"},
        d=Draft(draft().tbox, snapshot.parse_turtle(blank)),
    )
    assert _removed(d, base)["removedMappedClasses"]["count"] == 0
    d = _edited({"op": "delete_class", "iri": f"{ONTO}Client"}, d=d)
    assert [t["iri"] for t in _removed(d, base)["removedMappedClasses"]["terms"]] == [CUSTOMER]


# The sample mapping written as a mapping is often written by hand: predicate-object maps that are
# blank nodes, and the name column mapped to rdfs:label, which the ontology does not declare.
_BLANK_POMS = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<{ONTO}TriplesMap_Customer> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "customer" ] ;
    rr:subjectMap [ rr:class <{CUSTOMER}> ; rr:template "c/{{id}}" ] ;
    rr:predicateObjectMap [ rr:predicate <{EMAIL}> ; rr:objectMap [ rr:column "email" ; rr:datatype xsd:string ] ],
        [ rr:predicate rdfs:label ; rr:objectMap [ rr:column "name" ] ] .
<{ONTO}TriplesMap_Order> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "orders" ] ;
    rr:subjectMap [ rr:class <{ORDER}> ; rr:template "o/{{id}}" ] ;
    rr:predicateObjectMap [ rr:predicate <{TOTAL}> ; rr:objectMap [ rr:column "total" ; rr:datatype xsd:decimal ] ],
        [ rr:predicate <{PLACED_BY}> ; rr:objectMap [ rr:parentTriplesMap <{ONTO}TriplesMap_Customer> ;
            rr:joinCondition [ rr:child "customer_id" ; rr:parent "id" ] ] ] .
"""


def _hand_written() -> Draft:
    return Draft(draft().tbox, snapshot.parse_turtle(_BLANK_POMS))


def _stored(d: Draft) -> Draft:
    """``d`` as a revision stores it (canonical N-Triples) and reads it back: each blank node is a new node."""
    return Draft(parse_nt(snapshot.canonical_nt(d.tbox)), parse_nt(snapshot.canonical_nt(d.r2rml)))


def test_maps_that_are_blank_nodes_are_matched_by_the_column_or_join_they_read():
    base = _hand_written()
    renamed = _edited(
        {"op": "change_iri", "iri": EMAIL, "newIri": f"{ONTO}client_mail"},
        {"op": "change_iri", "iri": PLACED_BY, "newIri": f"{ONTO}order_buyer"},
        d=_hand_written(),
    )
    # Untouched or renamed, every map still reads its column or makes its join: nothing is deleted.
    for d in (_stored(_hand_written()), _stored(renamed)):
        result = _removed(d, base)
        assert result["removedMappedClasses"]["count"] == result["removedMappedProperties"]["count"] == 0
    deleted = _edited(
        {"op": "delete_property", "iri": f"{ONTO}client_mail"},
        {"op": "delete_property", "iri": f"{ONTO}order_buyer"},
        d=renamed,
    )
    # Renamed, then deleted: listed under the base version's IRI and label.
    assert _removed(_stored(deleted), base)["removedMappedProperties"]["terms"] == [
        {"iri": EMAIL, "label": "email", "domainIri": CUSTOMER, "physicalName": "email"},
        {"iri": PLACED_BY, "label": "placed by", "domainIri": ORDER},
    ]


def test_a_predicate_the_ontology_does_not_declare_is_not_listed():
    # rdfs:label read from the name column goes with the customer table, which removedMappedClasses lists.
    base = _hand_written()
    result = _removed(
        _stored(_edited({"op": "delete_class", "iri": CUSTOMER, "cascade": "detach"}, d=_hand_written())), base
    )
    assert [t["iri"] for t in result["removedMappedClasses"]["terms"]] == [CUSTOMER]
    assert result["removedMappedProperties"] == {"count": 0, "terms": []}


def _mapped_from_items_too(column: str) -> Draft:
    d = draft()
    d.r2rml.parse(
        data=f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
        <{ONTO}TriplesMap_Item> a rr:TriplesMap ; rr:logicalTable [ rr:tableName "item" ] ;
            rr:subjectMap [ rr:class <{ITEM}> ; rr:template "i/{{id}}" ] ;
            rr:predicateObjectMap [ rr:predicate <{TOTAL}> ; rr:objectMap [ rr:column "{column}" ] ] .""",
        format="turtle",
    )
    return d


@pytest.mark.parametrize(("column", "physical"), [("total", {"physicalName": "total"}), ("amount", {})])
def test_a_property_mapped_from_two_classes_names_the_first_and_one_column_only(column, physical):
    d = _edited({"op": "delete_property", "iri": TOTAL}, d=_mapped_from_items_too(column))
    # Item's map comes before Order's by IRI; the column is named only when every map reads it.
    assert _removed(d, _mapped_from_items_too(column))["removedMappedProperties"]["terms"] == [
        {"iri": TOTAL, "label": "total", "domainIri": ITEM, **physical}
    ]


def test_deletion_groups_list_at_most_100_terms_and_a_class_at_most_20_tables():
    base = draft()
    for n in range(checks.MAX_STRUCTURED_TERMS + 5):
        p = URIRef(f"{ONTO}extra{n:03d}")
        pom = URIRef(f"{ONTO}TriplesMap_Customer/POM_Extra{n:03d}")
        base.tbox.add((p, RDF.type, OWL.DatatypeProperty))
        base.r2rml.add((URIRef(f"{ONTO}TriplesMap_Customer"), snapshot.RR.predicateObjectMap, pom))
        base.r2rml.add((pom, snapshot.RR.predicate, p))
    for n in range(checks.MAX_TERM_TABLES + 5):
        _also_read_from(base, f"TriplesMap_Part{n:02d}", f'"part{n:02d}"')
    result = _removed(_edited({"op": "delete_class", "iri": CUSTOMER}), base)
    group = result["removedMappedProperties"]
    assert group["count"] == checks.MAX_STRUCTURED_TERMS + 5 and len(group["terms"]) == checks.MAX_STRUCTURED_TERMS
    assert group["terms"][0] == {"iri": f"{ONTO}extra000", "label": "extra000", "domainIri": CUSTOMER}
    (customer,) = result["removedMappedClasses"]["terms"]
    assert customer["tables"] == ["customer", *(f"part{n:02d}" for n in range(checks.MAX_TERM_TABLES - 1))]


def test_checks_list_the_mapped_terms_the_draft_deletes(opened):
    from coa_ontology.studio import changes

    ops = [{"op": "delete_property", "iri": EMAIL}, {"op": "change_iri", "iri": ORDER, "newIri": f"{ONTO}Purchase"}]
    changes.apply_change_set(NS, "p-1", ops, 0, author="a")
    with (
        patch.object(checks.graph_view, "abox_badges", return_value=None),
        patch.object(checks.abox_mapping, "list_rows", return_value=[]),
        patch.object(checks.version_deploy, "find_breaking_metrics", return_value=[]),
    ):
        structured = checks.checks(NS, "p-1")["structured"]
    assert structured["removedMappedProperties"] == {
        "count": 1,
        "terms": [{"iri": EMAIL, "label": "email", "domainIri": CUSTOMER, "physicalName": "email"}],
    }
    assert structured["removedMappedClasses"] == {"count": 0, "terms": []}  # Order was renamed
    assert structured["error"] is None


def test_the_base_ontology_is_read_only_for_the_labels_of_deleted_terms(opened):
    state = store.require_state(store.get_proposal(NS, "p-1"), editable=False)
    read = checks.storage.require_version_file
    with patch.object(checks.storage, "require_version_file", side_effect=read) as spy:
        checks.structured(NS, state, draft())
        assert spy.call_count == 0  # nothing deleted
        checks.structured(NS, state, _edited({"op": "delete_property", "iri": TOTAL}), base_tbox=draft().tbox)
        assert spy.call_count == 0  # the accept preview passes the base ontology it parsed
    with patch.object(checks.storage, "require_version_file", side_effect=read) as spy:
        result = checks.structured(NS, state, _edited({"op": "delete_property", "iri": TOTAL}))
    assert [c.args[2] for c in spy.call_args_list] == ["ontology.ttl"]
    assert result["removedMappedProperties"]["terms"][0]["label"] == "total"


def test_an_unreadable_base_version_lists_nothing_as_deleted(opened, caplog):
    state = store.require_state(store.get_proposal(NS, "p-1"), editable=False)
    d = _edited({"op": "delete_class", "iri": CUSTOMER, "cascade": "delete"})
    with patch.object(checks.storage, "require_version_file", side_effect=RuntimeError("s3 down at bucket-1")):
        result = checks.structured(NS, state, d)
    assert result["error"] == checks.BASE_MAPPING_ERROR and result["mapsNoTable"] is False
    assert result["removedMappedClasses"] == result["removedMappedProperties"] == {"count": 0, "terms": []}
    assert "bucket-1" not in str(result)
