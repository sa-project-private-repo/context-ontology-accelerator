# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Replace-mode publish (Ontology designer drafts) and shape pruning."""

import dataclasses
import json
from unittest.mock import patch

import pytest
from coa_ontology.catalog import ingest
from coa_ontology.studio import ops
from coa_ontology.versions import publish, registry, snapshot, storage
from rdflib import RDFS, URIRef
from rdflib.namespace import SH

from .studio_fakes import CUSTOMER, EMAIL, NS, ONTO, ORDER, R2RML, SCHEME, TBOX, VIP, draft, publish_base

pytestmark = [pytest.mark.unit]

SHAPES = f"""@prefix sh: <http://www.w3.org/ns/shacl#> .
<{ONTO}CustomerShape> a sh:NodeShape ; sh:targetClass <{CUSTOMER}> ;
    sh:property [ sh:path <{EMAIL}> ; sh:minCount 1 ] .
<{ONTO}OrderShape> a sh:NodeShape ; sh:targetClass <{ORDER}> ;
    sh:property [ sh:path <{ONTO}order_total> ; sh:maxCount 1 ], [ sh:path <{ONTO}order_gone> ; sh:minCount 1 ] .
"""


@pytest.fixture(autouse=True)
def _schema_sql():
    with patch("coa_ontology.proposals._generate_schema_sql", return_value=None):
        yield


def _replace(tbox_nt: str, r2rml_nt: str, expected: int | None, **kw) -> publish.PublishRequest:
    return publish.PublishRequest(
        namespace=NS,
        ontology_id=ONTO,
        ontology_turtle=tbox_nt,
        r2rml_turtle=r2rml_nt,
        source_proposal_id="p-1",
        created_by="alice",
        mode=publish.REPLACE,
        expected_parent_version=expected,
        **kw,
    )


def _deleted_customer() -> tuple[str, str]:
    d, _ = ops.apply_ops(draft(), ops.USER_OPS.validate_python([{"op": "delete_class", "iri": CUSTOMER}]), SCHEME)
    return snapshot.canonical_nt(d.tbox), snapshot.canonical_nt(d.r2rml)


def test_replace_publishes_the_draft_so_deletions_reach_the_next_version(versions_aws):
    publish_base()
    assert publish.publish_version(_replace(*_deleted_customer(), expected=1)) == 2
    onto = snapshot.parse_turtle(storage.require_version_file(NS, 2, "ontology.ttl"))
    assert URIRef(CUSTOMER) not in snapshot.schema_subjects(onto)[0]
    assert '"v2"' in storage.require_version_file(NS, 2, "ontology.ttl")
    diff = json.loads(storage.require_version_file(NS, 2, "diff.json"))
    assert CUSTOMER in diff["classes"]["removed"]
    assert "TriplesMap_Customer" not in storage.require_version_file(NS, 2, "mappings.r2rml")
    assert storage.read_manifest(NS, 2)["sourceProposals"] == ["p-1", "p-base"]


CLIENT, CONTACT, PHONE = f"{ONTO}Client", f"{ONTO}client_contact", f"{ONTO}customer_phone"
# What the re-induction's constraint config compiles to: shapes on its own, the inducer's, IRIs.
REINDUCED_SHAPES = f"""@prefix sh: <http://www.w3.org/ns/shacl#> .
<{ONTO}CustomerShape> a sh:NodeShape ; sh:targetClass <{CUSTOMER}> ;
    sh:property [ sh:path <{EMAIL}> ; sh:minCount 1 ], [ sh:path <{PHONE}> ; sh:maxCount 1 ] .
<{ONTO}OrderShape> a sh:NodeShape ; sh:targetClass <{ORDER}> ;
    sh:property [ sh:path <{ONTO}order_total> ; sh:maxCount 1 ] .
"""


@pytest.fixture
def compiled_shapes():
    """A proposal's constraint config compiles to ``REINDUCED_SHAPES``."""
    with (
        patch("coa_ontology.validation.shapes.config.ConstraintConfig", side_effect=lambda **kw: kw),
        patch("coa_ontology.validation.shapes.config.compile_to_shacl", return_value=REINDUCED_SHAPES),
    ):
        yield


def _rename_customer_in_the_designer() -> None:
    """Publish the sample as v1, then as v2 a designer draft that renamed Customer and its email attribute."""
    publish_base()
    renames = [
        {"op": "change_iri", "iri": CUSTOMER, "newIri": CLIENT},
        {"op": "change_iri", "iri": EMAIL, "newIri": CONTACT},
    ]
    renamed, _ = ops.apply_ops(draft(), ops.USER_OPS.validate_python(renames), SCHEME)
    edited = _replace(snapshot.canonical_nt(renamed.tbox), snapshot.canonical_nt(renamed.r2rml), expected=1)
    assert publish.publish_version(edited) == 2


def _reinduced_customer() -> publish.PublishRequest:
    """The customer table gained a column and is re-induced: the inducer's IRIs come back under the same maps."""
    tbox = TBOX + f'<{PHONE}> a owl:DatatypeProperty ; rdfs:label "phone" ; rdfs:domain <{CUSTOMER}> .\n'
    r2rml = R2RML + (
        f"<{ONTO}TriplesMap_Customer> rr:predicateObjectMap <{ONTO}TriplesMap_Customer/POM_Phone> .\n"
        f"<{ONTO}TriplesMap_Customer/POM_Phone> rr:predicate <{PHONE}> ; "
        f'rr:objectMap [ rr:column "phone" ; rr:datatype xsd:string ] .\n'
    )
    return publish.PublishRequest(
        namespace=NS,
        ontology_id=ONTO,
        ontology_turtle=tbox,
        r2rml_turtle=r2rml,
        source_proposal_id="p-reinduce",
        created_by="alice",
        constraint_config={"classes": []},
    )


def test_a_reinduction_after_a_designer_rename_lands_on_the_renamed_class_and_attribute(versions_aws, compiled_shapes):
    _rename_customer_in_the_designer()
    assert publish.publish_version(_reinduced_customer()) == 3
    onto = snapshot.parse_turtle(storage.require_version_file(NS, 3, "ontology.ttl"))
    classes = snapshot.schema_subjects(onto)[0]
    assert URIRef(CLIENT) in classes and URIRef(CUSTOMER) not in classes
    assert not list(onto.triples((URIRef(EMAIL), None, None)))
    assert set(onto.objects(URIRef(CONTACT), RDFS.domain)) == {URIRef(CLIENT)}
    assert set(onto.objects(URIRef(PHONE), RDFS.domain)) == {URIRef(CLIENT)}
    assert set(onto.objects(URIRef(VIP), RDFS.subClassOf)) == {URIRef(CLIENT)}
    mapping = storage.require_version_file(NS, 3, "mappings.r2rml")
    assert set(ingest._class_datasource_map_from_r2rml(mapping)) == {CLIENT, ORDER}
    assert {CONTACT, PHONE} <= ingest._mapped_predicates_from_r2rml(mapping)
    # The proposal's shapes, compiled for the inducer's IRIs, constrain the renamed terms.
    shapes = snapshot.parse_turtle(storage.require_version_file(NS, 3, "shapes.ttl"))
    assert set(shapes.objects(URIRef(f"{ONTO}CustomerShape"), SH.targetClass)) == {URIRef(CLIENT)}
    paths = set(shapes.objects(None, SH.path))
    assert URIRef(CONTACT) in paths and URIRef(EMAIL) not in paths


def test_a_designer_rename_is_kept_when_other_versions_landed_since(versions_aws, compiled_shapes):
    _rename_customer_in_the_designer()
    shipment = f"@prefix owl: <http://www.w3.org/2002/07/owl#> .\n<{ONTO}Shipment> a owl:Class ."
    assert publish_base(shipment, "", proposal="p-shipping") == 3
    assert publish.publish_version(_reinduced_customer()) == 4
    mapping = storage.require_version_file(NS, 4, "mappings.r2rml")
    assert set(ingest._class_datasource_map_from_r2rml(mapping)) == {CLIENT, ORDER}
    assert {CONTACT, PHONE} <= ingest._mapped_predicates_from_r2rml(mapping)


def test_a_reinduction_that_names_a_table_afresh_without_a_designer_edit_keeps_the_proposals_iris(versions_aws):
    # An inducer can name a table's terms anew under the same maps (RIGOR's LLM picks the property IRIs). No
    # version removed the old IRIs, so no designer renamed them: the proposal merges as it is.
    publish_base()
    buyer, address = f"{ONTO}Buyer", f"{ONTO}customer_emailAddress"
    tbox = TBOX.replace(CUSTOMER, buyer).replace(EMAIL, address)
    r2rml = R2RML.replace(CUSTOMER, buyer).replace(EMAIL, address)
    assert publish_base(tbox, r2rml, proposal="p-rerun") == 2
    mapping = storage.require_version_file(NS, 2, "mappings.r2rml")
    assert set(ingest._class_datasource_map_from_r2rml(mapping)) == {buyer, ORDER}
    assert address in ingest._mapped_predicates_from_r2rml(mapping)
    onto = snapshot.parse_turtle(storage.require_version_file(NS, 2, "ontology.ttl"))
    assert {URIRef(buyer), URIRef(CUSTOMER)} <= snapshot.schema_subjects(onto)[0]  # a union keeps the old class


def test_an_unedited_designer_draft_of_the_reinduction_publishes_what_a_plain_accept_does(
    versions_aws, compiled_shapes
):
    _rename_customer_in_the_designer()
    plain = _reinduced_customer()
    tbox, r2rml = publish.union_candidate(plain, 2)
    # An accept of the unedited draft: the draft in replace mode, with the proposal's own Turtle alongside.
    unedited = dataclasses.replace(
        plain,
        ontology_turtle=snapshot.canonical_nt(tbox),
        r2rml_turtle=snapshot.canonical_nt(r2rml),
        mode=publish.REPLACE,
        expected_parent_version=2,
        authoritative_datasources=[],
        proposal_ontology_turtle=plain.ontology_turtle,
        proposal_r2rml_turtle=plain.r2rml_turtle,
    )
    files = publish._build_files(plain, 3, 2)[0]
    shapes = snapshot.parse_turtle(files["shapes.ttl"])
    assert set(shapes.objects(URIRef(f"{ONTO}CustomerShape"), SH.targetClass)) == {URIRef(CLIENT)}
    assert publish._build_files(unedited, 3, 2)[0] == files


def test_union_still_keeps_what_the_proposal_omits(versions_aws):
    publish_base()
    tbox_nt, r2rml_nt = _deleted_customer()
    union = publish.PublishRequest(
        namespace=NS,
        ontology_id=ONTO,
        ontology_turtle=tbox_nt,
        r2rml_turtle=r2rml_nt,
        source_proposal_id="p-1",
        created_by="alice",
    )
    publish.publish_version(union)
    onto = snapshot.parse_turtle(storage.require_version_file(NS, 2, "ontology.ttl"))
    assert URIRef(CUSTOMER) in snapshot.schema_subjects(onto)[0]


def test_replacing_with_the_union_candidate_publishes_what_union_does(versions_aws):
    publish_base()
    incoming = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
<{ONTO}Shipment> a owl:Class ."""
    req = publish.PublishRequest(
        namespace=NS,
        ontology_id=ONTO,
        ontology_turtle=incoming,
        r2rml_turtle="",
        source_proposal_id="p-1",
        created_by="alice",
    )
    tbox, r2rml = publish.union_candidate(req, 1)
    assert publish.publish_version(req) == 2
    assert publish.publish_version(_replace(snapshot.canonical_nt(tbox), snapshot.canonical_nt(r2rml), expected=2)) == 3
    a, b = storage.read_manifest(NS, 2)["files"], storage.read_manifest(NS, 3)["files"]
    # v3 replaces v2 with the same content: only the version stamp differs, so the mapping is identical.
    assert a["mappings.r2rml"]["sha256"] == b["mappings.r2rml"]["sha256"]
    two = storage.require_version_file(NS, 2, "ontology.ttl").replace('"v2"', '"v3"')
    assert snapshot.sha256_hex(two) == b["ontology.ttl"]["sha256"]


def test_a_stale_base_is_refused_before_reserving(versions_aws):
    publish_base()
    publish_base(proposal="p-other")
    with pytest.raises(publish.RebaseRequiredError, match="rebase the draft"):
        publish.publish_version(_replace(*_deleted_customer(), expected=1))
    assert [v["version"] for v in registry.list_versions(NS)] == [2, 1]


def test_a_publish_landing_between_the_check_and_the_reservation_fails_the_version(versions_aws):
    publish_base()
    real_reserve = publish._reserve
    raced = []

    def racing_reserve(*args, **kwargs):
        if not raced:  # the racer's own publish reserves normally
            raced.append(1)
            publish_base(proposal="p-racer")  # v2 lands after the replace publish checked v1
        return real_reserve(*args, **kwargs)

    with patch.object(publish, "_reserve", side_effect=racing_reserve), pytest.raises(publish.RebaseRequiredError):
        publish.publish_version(_replace(*_deleted_customer(), expected=1))
    assert registry.get_version(NS, 3)["status"] == registry.PUBLISH_FAILED


def test_first_version_from_a_draft_without_a_base(versions_aws):
    assert publish.publish_version(_replace(*_deleted_customer(), expected=None)) == 1
    with pytest.raises(publish.RebaseRequiredError):
        publish.publish_version(_replace(*_deleted_customer(), expected=None))


def test_replace_prunes_shapes_of_deleted_terms(versions_aws):
    publish_base()
    with (
        patch("coa_ontology.validation.shapes.config.ConstraintConfig", side_effect=lambda **kw: kw),
        patch("coa_ontology.validation.shapes.config.compile_to_shacl", return_value=SHAPES),
    ):
        publish.publish_version(_replace(*_deleted_customer(), expected=1, constraint_config={"classes": []}))
    shapes = snapshot.parse_turtle(storage.require_version_file(NS, 2, "shapes.ttl"))
    assert (URIRef(f"{ONTO}CustomerShape"), None, None) not in shapes
    paths = set(shapes.objects(None, SH.path))
    assert paths == {URIRef(f"{ONTO}order_total")}


def test_prune_shapes_drops_what_targets_absent_terms_and_keeps_the_rest():
    ghost = f"""@prefix sh: <http://www.w3.org/ns/shacl#> .
<{ONTO}GhostShape> a sh:NodeShape ; sh:targetClass <{ONTO}Ghost> ;
    sh:property [ sh:path <{ONTO}ghost_name> ; sh:minCount 1 ] .
"""
    onto = snapshot.parse_turtle(TBOX)
    pruned = snapshot.prune_shapes(snapshot.parse_turtle(SHAPES + ghost), onto)
    # Present terms keep their shapes ...
    assert (URIRef(f"{ONTO}CustomerShape"), SH.targetClass, URIRef(CUSTOMER)) in pruned
    assert (URIRef(f"{ONTO}OrderShape"), SH.targetClass, URIRef(ORDER)) in pruned
    # ... a property the draft lacks goes with its property shape, and a class it lacks takes the whole shape.
    assert set(pruned.objects(None, SH.path)) == {URIRef(EMAIL), URIRef(f"{ONTO}order_total")}
    assert (URIRef(f"{ONTO}GhostShape"), None, None) not in pruned
    assert (None, SH.path, URIRef(f"{ONTO}ghost_name")) not in pruned
