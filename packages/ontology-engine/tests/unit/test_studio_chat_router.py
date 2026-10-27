# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The Ontology designer chat and change set routes."""

from __future__ import annotations

from unittest.mock import patch
from urllib.parse import quote

import pytest
from coa_ontology.studio import changesets, chat_store
from coa_ontology.studio import draft as store
from coa_ontology.studio import router as studio_router
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .studio_fakes import NS, ONTO, ORDER, PARTY, opened_draft

pytestmark = [pytest.mark.unit]
BASE = "/ontology/proposals/p-1/studio"
ALICE = {"X-Coa-Principal": "alice%40example.com"}
ADD = {"op": "add_class", "label": "Supplier", "parents": [PARTY], "iri": f"{ONTO}Supplier"}


@pytest.fixture
def http(versions_aws):
    store.clear_cache()
    app = FastAPI()
    app.include_router(studio_router.router, prefix="/ontology/proposals/{proposal_id}/studio")
    with patch("coa_ontology.proposals._generate_schema_sql", return_value=None):
        opened_draft("p-1")
        yield TestClient(app)
    store.clear_cache()


def _q(**params: str) -> str:
    return "?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in {"namespace": NS, **params}.items())


def _proposed(ops: list[dict] | None = None) -> dict:
    return changesets.propose(NS, "p-1", ops or [ADD], "Suppliers", turn_id="t-1", author="alice")


def test_a_message_is_accepted_with_202_and_its_turn(http):
    with patch.object(studio_router.agent, "send_message", return_value={"turnId": "t-9", "seq": 3}) as sent:
        r = http.post(f"{BASE}/messages{_q()}", json={"text": "Add a parent", "selection": [ORDER]}, headers=ALICE)
    assert r.status_code == 202 and r.json() == {"turnId": "t-9", "seq": 3}
    assert sent.call_args.args == (NS, "p-1", "Add a parent", [ORDER])
    assert sent.call_args.kwargs == {"author": "alice@example.com"}


def test_a_message_while_a_turn_runs_is_409_with_the_turn(http):
    chat_store.start_turn(NS, "p-1", author="bob", text="first", selection=[])
    r = http.post(f"{BASE}/messages{_q()}", json={"text": "second"}, headers=ALICE)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "TurnInProgress"
    assert r.json()["detail"]["turnId"]


def test_a_bad_message_is_422(http):
    assert http.post(f"{BASE}/messages{_q()}", json={"text": ""}).status_code == 422
    r = http.post(f"{BASE}/messages{_q()}", json={"text": "hi", "selection": ["not an iri"]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "InvalidIri"
    r = http.post(f"{BASE}/messages{_q()}", json={"text": "   "})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "InvalidMessage"
    r = http.post(f"{BASE}/messages{_q()}", json={"text": "hi", "selection": [ORDER] * 21})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "InvalidMessage"
    r = http.post(f"{BASE}/messages{_q()}", json={"text": "x" * 4001})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "InvalidMessage"


def test_events_after_a_seq(http):
    assert http.get(f"{BASE}/events{_q()}").json() == {"events": [], "lastSeq": 0, "more": False, "turn": None}
    log, _ = chat_store.start_turn(NS, "p-1", author="bob", text="first", selection=[])
    log.append("thinking", text="Looking")
    body = http.get(f"{BASE}/events{_q(after='1')}").json()
    assert [e["type"] for e in body["events"]] == ["thinking"] and body["turn"]["status"] == "running"
    assert http.get(f"{BASE}/events{_q(limit='1')}").json()["more"] is True
    assert http.get(f"{BASE}/events{_q(after='-1')}").status_code == 422
    assert http.get(f"{BASE}/events{_q(after=str(chat_store.MAX_SEQ + 1))}").status_code == 422
    assert http.get("/ontology/proposals/p-2/studio/events" + _q()).json()["detail"]["code"] == "ProposalNotFound"


def test_list_preview_apply_and_discard(http):
    first, second = _proposed(), _proposed([{"op": "set_label", "iri": ORDER, "label": "PO"}])
    listed = http.get(f"{BASE}/changesets{_q()}").json()
    assert listed["revision"] == 0 and [c["changeSetId"] for c in listed["changeSets"]] == [
        first["changeSetId"],
        second["changeSetId"],
    ]
    preview = http.get(f"{BASE}/changesets/{first['changeSetId']}/preview{_q(opIndexes='0', focus=ORDER)}").json()
    assert preview["applies"] and preview["opIndexes"] == [0] and preview["graph"]["mode"] == "focus"
    applied = http.post(
        f"{BASE}/changesets/{first['changeSetId']}/apply{_q()}", json={"expectedRevision": 0}, headers=ALICE
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["revision"] == 1 and applied.json()["changeSet"]["appliedBy"] == "alice@example.com"
    again = http.post(f"{BASE}/changesets/{first['changeSetId']}/apply{_q()}", json={"expectedRevision": 1})
    assert again.status_code == 409 and again.json()["detail"] == {
        **again.json()["detail"],
        "code": "ChangeSetNotPending",
        "status": "applied",
    }
    discarded = http.post(f"{BASE}/changesets/{second['changeSetId']}/discard{_q()}", headers=ALICE)
    assert discarded.status_code == 200 and discarded.json()["status"] == "discarded"
    pending = http.get(f"{BASE}/changesets{_q(status='pending')}").json()["changeSets"]
    assert pending == []
    assert http.get(f"{BASE}/changesets{_q(status='lost')}").status_code == 422


def test_change_sets_are_listed_one_page_at_a_time(http):
    sets = [_proposed([{"op": "set_label", "iri": ORDER, "label": f"PO {n}"}]) for n in range(3)]
    first = http.get(f"{BASE}/changesets{_q(maxResults='2')}").json()
    assert [c["changeSetId"] for c in first["changeSets"]] == [s["changeSetId"] for s in sets[:2]]
    rest = http.get(f"{BASE}/changesets{_q(maxResults='2', nextToken=first['nextToken'])}").json()
    assert [c["changeSetId"] for c in rest["changeSets"]] == [sets[2]["changeSetId"]]
    assert "nextToken" not in rest and rest["revision"] == 0
    # A token resumes only the listing it was minted for.
    other = http.get(f"{BASE}/changesets{_q(status='pending', nextToken=first['nextToken'])}")
    assert other.status_code == 400
    assert http.get(f"{BASE}/changesets{_q(nextToken='garbage')}").status_code == 400
    assert (
        http.get(f"{BASE}/changesets{_q(maxResults=str(studio_router.LIST_CHANGE_SETS_MAX_RESULTS + 1))}").status_code
        == 422
    )


def test_change_set_errors_map_to_their_codes(http):
    cs = _proposed()
    path = f"{BASE}/changesets/{cs['changeSetId']}"
    stale = http.post(f"{path}/apply{_q()}", json={"expectedRevision": 5})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "DraftConflict"
    for bad in ("a", "0,,1", "1,1", "9"):
        r = http.get(f"{path}/preview{_q(opIndexes=bad)}")
        assert r.status_code == 422 and r.json()["detail"]["code"] == "InvalidRequest", bad
    r = http.post(f"{path}/apply{_q()}", json={"expectedRevision": 0, "opIndexes": []})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "InvalidRequest"
    too_many = {"expectedRevision": 0, "opIndexes": list(range(changesets.MAX_AGENT_OPS + 1))}
    assert http.post(f"{path}/apply{_q()}", json=too_many).status_code == 422
    assert http.get(f"{path}/preview{_q(opIndexes='0,' * 100 + '0')}").status_code == 422
    missing = http.get(f"{BASE}/changesets/20260101T000000000000Z-00000000/preview{_q()}")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "ChangeSetNotFound"
    bad_id = http.post(f"{BASE}/changesets/bad.id/discard{_q()}")
    assert bad_id.status_code == 422 and bad_id.json()["detail"]["code"] == "InvalidRequest"
    bad_focus = http.get(f"{path}/preview{_q(focus='not an iri')}")
    assert bad_focus.status_code == 422 and bad_focus.json()["detail"]["code"] == "InvalidIri"


def test_a_question_check_is_accepted_with_202_like_a_message(http):
    reply = {"turnId": "t-9", "seq": 3}
    with patch.object(studio_router.agent, "send_question_check", return_value=reply) as sent:
        r = http.post(f"{BASE}/questions/q-1/check{_q()}", headers=ALICE)
    assert r.status_code == 202 and r.json() == reply
    assert sent.call_args.args == (NS, "p-1", "q-1") and sent.call_args.kwargs == {"author": "alice@example.com"}
    r = http.post(f"{BASE}/questions/q-9/check{_q()}", headers=ALICE)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "QuestionNotFound"
    r = http.post(f"{BASE}/questions/bad%20id/check{_q()}", headers=ALICE)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "InvalidQuestion"
