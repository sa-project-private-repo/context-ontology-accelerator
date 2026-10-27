# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The proposal page's own edit paths stand aside once the Ontology designer owns the draft.

Datatype repair is the exception that works on the draft: as a change set of ``replace_datatype`` ops.
"""

from unittest.mock import MagicMock, patch

import pytest
from coa_ontology import dynamo_store, proposals
from coa_ontology.studio import changes, rebase
from coa_ontology.studio import draft as store
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .abox_fakes import InlineThread
from .studio_fakes import NS, ONTO, SHIPMENT, publish_base, seed_proposal, stored_row, stored_state

pytestmark = [pytest.mark.unit]

# The bad token is in the proposal the draft is opened from: a designer op cannot create it,
# because the designer's datatypes are XSD built-ins only.
BAD = "http://www.w3.org/2001/XMLSchema#datetime"
GOOD = "http://www.w3.org/2001/XMLSchema#dateTime"
BAD_PROPOSAL = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<{ONTO[:-1]}> a owl:Ontology .
<{SHIPMENT}> a owl:Class ; rdfs:label "Shipment" .
<{ONTO}shipment_shippedAt> a owl:DatatypeProperty ; rdfs:domain <{SHIPMENT}> ; rdfs:range xsd:datetime .
"""
BAD_R2RML = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<{ONTO}TriplesMap_Shipment> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "shipment" ] ;
    rr:subjectMap <{ONTO}TriplesMap_Shipment/SubjectMap> ;
    rr:predicateObjectMap <{ONTO}TriplesMap_Shipment/POM_ShippedAt> .
<{ONTO}TriplesMap_Shipment/SubjectMap> rr:class <{SHIPMENT}> ; rr:template "s/{{id}}" .
<{ONTO}TriplesMap_Shipment/POM_ShippedAt> rr:predicate <{ONTO}shipment_shippedAt> ;
    rr:objectMap <{ONTO}TriplesMap_Shipment/POM_ShippedAt/ObjectMap> .
<{ONTO}TriplesMap_Shipment/POM_ShippedAt/ObjectMap> rr:column "shipped_at" ; rr:datatype xsd:datetime .
"""
OTHER = "<urn:a> <urn:b> <urn:c> ."


@pytest.fixture(autouse=True)
def _no_source_ddl():
    """The sample mapping makes a publish build source DDL, which needs a source catalog."""
    with patch("coa_ontology.proposals._generate_schema_sql", return_value=None):
        yield


@pytest.fixture
def client(versions_aws):
    """A proposal open in the designer at revision 0, whose draft holds the proposal's bad range and bad mapping."""
    store.clear_cache()
    app = FastAPI()
    app.include_router(proposals.router, prefix="/ontology/proposals")
    publish_base()
    seed_proposal(turtle=BAD_PROPOSAL, r2rml=BAD_R2RML)
    store.open_draft(NS, "p-1", "alice")
    yield TestClient(app)
    store.clear_cache()


def test_turtle_and_grounding_edits_are_refused(client):
    for path, body in (
        ("/ontology/proposals/update", {"proposal_id": "p-1", "ontology_turtle": OTHER}),
        (
            "/ontology/proposals/update",
            {"proposal_id": "p-1", "grounding_overrides": {"orders": "https://example.com/onto#Order"}},
        ),
        ("/ontology/proposals/p-1/upload-url", None),
    ):
        r = client.post(f"{path}?namespace={NS}", json=body)
        assert r.status_code == 409, (path, r.text)
        assert r.json()["detail"]["code"] == "StudioDraftOpen"
    # Nothing was written: the proposal keeps its status and the draft its revision.
    assert stored_row()["status"] == "pending" and stored_state().revision == 0


def test_the_datatype_preview_reads_the_draft(client):
    r = client.post(f"/ontology/proposals/p-1/repair-datatypes?namespace={NS}", json={"dry_run": True})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "proposal_id": "p-1",
        "repaired_count": 2,  # the range in the ontology, the rr:datatype in the mapping
        "changes": [{"found": BAD, "canonical": GOOD}] * 2,
        "dry_run": True,
    }
    assert stored_row()["status"] == "pending" and stored_state().revision == 0


def test_the_preview_follows_the_draft_not_the_proposal_turtle(client):
    """Both start with the bad token, so make the stored proposal Turtle clean: only the draft still has it."""
    dynamo_store._put_artifact_s3(NS, "p-1", "ontology", BAD_PROPOSAL.replace("xsd:datetime", "xsd:dateTime"))
    dynamo_store._put_artifact_s3(NS, "p-1", "r2rml", BAD_R2RML.replace("xsd:datetime", "xsd:dateTime"))
    r = client.post(f"/ontology/proposals/p-1/repair-datatypes?namespace={NS}", json={"dry_run": True})
    assert r.status_code == 200, r.text
    assert r.json()["repaired_count"] == 2


def test_validation_reads_the_draft(client):
    with (
        patch.object(proposals, "Thread", InlineThread),
        patch.object(proposals, "_run_proposal_validation", MagicMock()) as run,
    ):
        r = client.post(f"/ontology/proposals/p-1/validate?namespace={NS}", json={})
    assert r.status_code == 200, r.text
    run.assert_called_once()
    turtle = run.call_args.args[1]
    assert turtle == store.revision_text(NS, "p-1", 0)[0]
    assert run.call_args.kwargs["r2rml_turtle"] == store.revision_text(NS, "p-1", 0)[1]


def _repair(http, body: dict | None):
    return http.post(f"/ontology/proposals/p-1/repair-datatypes?namespace={NS}", json=body)


def _texts(revision: int) -> str:
    return "\n".join(store.revision_text(NS, "p-1", revision))


def test_a_repair_replaces_the_token_in_the_draft_as_one_change_set(client):
    r = _repair(client, {"expectedRevision": 0})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "proposal_id": "p-1",
        "repaired_count": 2,
        "changes": [{"found": BAD, "canonical": GOOD}] * 2,
        "dry_run": False,
        "revision": 1,
    }
    ontology_nt, r2rml_nt = store.revision_text(NS, "p-1", 1)
    assert f"<{GOOD}>" in ontology_nt and f"<{BAD}>" not in ontology_nt
    assert f"<{GOOD}>" in r2rml_nt and f"<{BAD}>" not in r2rml_nt
    record = store.get_record(NS, "p-1", 1)
    assert record["ops"] == [{"op": "replace_datatype", "from": BAD, "to": GOOD}]  # the two uses are one op
    assert record["summary"] == ["Replaced the datatype datetime with dateTime"]
    state = stored_state()
    assert state.revision == 1 and state.applied == (1,) and stored_row()["status"] == "updated"
    # The proposal's own Turtle is not touched: the draft is the only thing that changes.
    assert "xsd:datetime" in dynamo_store._get_artifact_s3(NS, "p-1", "ontology")
    assert "xsd:datetime" in dynamo_store._get_artifact_s3(NS, "p-1", "r2rml")
    # Nothing is left to repair, and asking again writes nothing.
    assert _repair(client, {"dry_run": True}).json()["repaired_count"] == 0
    again = _repair(client, {"expectedRevision": 1})
    assert again.status_code == 200 and again.json()["repaired_count"] == 0 and again.json()["revision"] == 1
    assert stored_state().revision == 1


def test_a_repair_does_not_give_a_mapped_attribute_another_datatype_than_its_column(versions_aws):
    """The range has the wrong token, but the column is typed date: dateTime would contradict the column."""
    store.clear_cache()
    app = FastAPI()
    app.include_router(proposals.router, prefix="/ontology/proposals")
    publish_base()
    seed_proposal(turtle=BAD_PROPOSAL, r2rml=BAD_R2RML.replace("rr:datatype xsd:datetime", "rr:datatype xsd:date"))
    store.open_draft(NS, "p-1", "alice")
    r = _repair(TestClient(app), {"expectedRevision": 0})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {
        "code": "InvalidChange",
        "message": "Op 0: datetime cannot be replaced with dateTime: shipment_shippedAt is mapped from column "
        "shipped_at, typed date; its datatype follows the column.",
        "opIndex": 0,
    }
    assert stored_state().revision == 0 and stored_row()["status"] == "pending"
    store.clear_cache()


def test_a_repair_writes_the_datatype_a_range_and_a_column_spelled_differently_both_stand_for(versions_aws):
    """The range says xsd:datetime and the column xsd:timestamp: both are dateTime, which the repair writes."""
    timestamp = "http://www.w3.org/2001/XMLSchema#timestamp"
    store.clear_cache()
    app = FastAPI()
    app.include_router(proposals.router, prefix="/ontology/proposals")
    publish_base()
    seed_proposal(turtle=BAD_PROPOSAL, r2rml=BAD_R2RML.replace("rr:datatype xsd:datetime", "rr:datatype xsd:timestamp"))
    store.open_draft(NS, "p-1", "alice")
    r = _repair(TestClient(app), {"expectedRevision": 0})
    assert r.status_code == 200, r.text
    assert r.json()["changes"] == [{"found": BAD, "canonical": GOOD}, {"found": timestamp, "canonical": GOOD}]
    assert r.json()["revision"] == 1
    ontology_nt, r2rml_nt = store.revision_text(NS, "p-1", 1)
    assert f"<{ONTO}shipment_shippedAt> <http://www.w3.org/2000/01/rdf-schema#range> <{GOOD}> ." in ontology_nt
    assert f"<http://www.w3.org/ns/r2rml#datatype> <{GOOD}> ." in r2rml_nt
    assert BAD not in ontology_nt and timestamp not in r2rml_nt
    store.clear_cache()


def test_a_repair_needs_the_revision_the_user_reviewed(client):
    missing = _repair(client, {})
    assert missing.status_code == 422 and missing.json()["detail"]["code"] == "ExpectedRevisionRequired"
    assert _repair(client, None).status_code == 422
    assert _repair(client, {"expectedRevision": 0}).status_code == 200
    stale = _repair(client, {"expectedRevision": 0})
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "DraftConflict",
        "message": "Another change was applied first; the draft is now at revision 1.",
        "revision": 1,
    }
    assert stored_state().revision == 1


def test_a_repair_is_refused_once_the_proposal_left_review(client):
    dynamo_store.update_proposal("p-1", namespace=NS, status="cancelled")
    r = _repair(client, {"expectedRevision": 0})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ProposalNotEditable"
    assert stored_state().revision == 0


def test_undoing_a_repair_brings_the_bad_token_back(client):
    before = _texts(0)
    assert _repair(client, {"expectedRevision": 0}).status_code == 200
    changes.undo(NS, "p-1", 1, author="alice")
    assert BAD in _texts(2) and GOOD not in _texts(2)
    assert _texts(2) == before


def test_a_rebase_after_a_repair_replays_it(client):
    assert _repair(client, {"expectedRevision": 0}).status_code == 200
    publish_base(proposal="p-other")  # v2 lands; the draft is on v1
    result = rebase.rebase(NS, "p-1", 1, [], author="alice")
    assert result["replayed"] == 1 and result["baseVersion"] == 2
    assert BAD not in _texts(result["revision"]) and GOOD in _texts(result["revision"])
