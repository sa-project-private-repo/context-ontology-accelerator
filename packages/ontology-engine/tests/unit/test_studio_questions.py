# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Competency questions of a namespace."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from coa_ontology import dynamo_store
from coa_ontology.studio import questions
from coa_ontology.studio import router as studio_router
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .studio_fakes import NS

pytestmark = [pytest.mark.unit]
OTHER = "99999999-2222-3333-4444-555555555555"
TEXT = "Which customers placed more than three orders last month?"


def test_a_question_is_created_listed_edited_and_deleted(versions_aws):
    q = questions.put_question(NS, "q-1", f"  {TEXT}  ", "Customer names", author="alice")
    assert q["questionId"] == "q-1" and q["text"] == TEXT and q["expectation"] == "Customer names"
    assert q["createdAt"] == q["updatedAt"] and q["updatedBy"] == "alice" and q["lastCheck"] is None
    questions.put_question(NS, "q-2", "Which orders ship today?", None, author="bob")
    assert [x["questionId"] for x in questions.list_questions(NS, limit=50)["questions"]] == ["q-1", "q-2"]

    edited = questions.put_question(NS, "q-1", TEXT, "", author="bob")
    assert edited["expectation"] is None and edited["createdAt"] == q["createdAt"] and edited["updatedBy"] == "bob"
    questions.delete_question(NS, "q-1")
    questions.delete_question(NS, "q-1")  # already gone: a no-op
    assert [x["questionId"] for x in questions.list_questions(NS, limit=50)["questions"]] == ["q-2"]
    with pytest.raises(questions.QuestionNotFound):
        questions.get_question(NS, "q-1")


@pytest.mark.parametrize(
    ("question_id", "text", "expectation"),
    [
        ("q 1", TEXT, None),
        ("q/1", TEXT, None),
        ("x" * 65, TEXT, None),
        ("", TEXT, None),
        ("q-1", "   ", None),
        ("q-1", "x" * (questions.MAX_TEXT + 1), None),
        ("q-1", TEXT, "x" * (questions.MAX_EXPECTATION + 1)),
    ],
)
def test_ids_texts_and_expectations_are_limited(versions_aws, question_id, text, expectation):
    with pytest.raises(questions.InvalidQuestion) as raised:
        questions.put_question(NS, question_id, text, expectation, author="a")
    assert raised.value.status == 422


def test_a_namespace_holds_at_most_the_question_limit(versions_aws):
    with patch.object(questions, "MAX_QUESTIONS", 3):
        for n in range(3):
            questions.put_question(NS, f"q-{n}", TEXT, None, author="a")
        with pytest.raises(questions.TooManyQuestions):
            questions.put_question(NS, "q-9", TEXT, None, author="a")
        questions.put_question(NS, "q-0", "An edit is not a new question", None, author="a")


def test_questions_belong_to_their_namespace(versions_aws):
    questions.put_question(NS, "q-1", TEXT, None, author="a")
    assert questions.list_questions(OTHER, limit=50) == {"questions": [], "next": None}
    with pytest.raises(questions.QuestionNotFound):
        questions.get_question(OTHER, "q-1")
    with pytest.raises(questions.QuestionNotFound):
        questions.record_check(
            OTHER,
            "q-1",
            proposal_id="p-1",
            revision=0,
            status="expressible",
            query=None,
            row_count=0,
            rows=[],
            checked_text=TEXT,
        )


def test_a_check_is_stored_and_a_new_text_clears_it(versions_aws):
    questions.put_question(NS, "q-1", TEXT, None, author="a")
    rows = [[{"type": "literal", "value": "x" * 500}]] * 9
    q = questions.record_check(
        NS,
        "q-1",
        proposal_id="p-1",
        revision=4,
        status="answerable",
        query="SELECT ...",
        row_count=9,
        rows=rows,
        checked_text=TEXT,
    )
    last = q["lastCheck"]
    assert last["proposalId"] == "p-1" and last["revision"] == 4 and last["status"] == "answerable"
    assert last["rowCount"] == 9 and len(last["sample"]) == questions.MAX_SAMPLE_ROWS
    assert len(last["sample"][0][0]["value"]) == questions.MAX_SAMPLE_VALUE
    assert questions.put_question(NS, "q-1", TEXT, "same text", author="a")["lastCheck"] == last
    assert questions.put_question(NS, "q-1", TEXT + " (edited)", None, author="a")["lastCheck"] is None
    with pytest.raises(ValueError):
        questions.record_check(
            NS,
            "q-1",
            proposal_id="p-1",
            revision=4,
            status="maybe",
            query=None,
            row_count=0,
            rows=[],
            checked_text=TEXT,
        )


def test_the_namespace_purge_removes_the_questions(versions_aws):
    questions.put_question(NS, "q-1", TEXT, None, author="a")
    dynamo_store.delete_all_namespace_items(NS)
    assert questions.list_questions(NS, limit=50) == {"questions": [], "next": None}


def test_purging_a_namespace_keeps_the_rows_of_one_whose_name_it_begins(versions_aws):
    """Purging ``default`` matched ``NAMESPACE#default2`` by prefix and deleted its rows."""
    for namespace in ("default", "default2"):
        questions.put_question(namespace, "q-1", TEXT, None, author="a")
        dynamo_store.put_ontology_registry(namespace, "http://e/o#", {"uri": "http://e/o#"})
    dynamo_store.delete_all_namespace_items("default")
    assert questions.list_questions("default", limit=50)["questions"] == []
    assert dynamo_store.get_ontology_registry("default", "http://e/o#") is None
    assert [q["questionId"] for q in questions.list_questions("default2", limit=50)["questions"]] == ["q-1"]
    assert dynamo_store.get_ontology_registry("default2", "http://e/o#") is not None


def test_the_questions_routes(versions_aws):
    app = FastAPI()
    app.include_router(studio_router.questions_router, prefix="/ontology/studio/questions")
    http = TestClient(app)
    base = "/ontology/studio/questions"
    r = http.put(f"{base}/q-1?namespace={NS}", json={"text": TEXT}, headers={"X-Coa-Principal": "alice%40example.com"})
    assert r.status_code == 200, r.text
    assert r.json()["updatedBy"] == "alice@example.com"
    assert [q["questionId"] for q in http.get(f"{base}?namespace={NS}").json()["questions"]] == ["q-1"]
    r = http.put(f"{base}/bad%20id?namespace={NS}", json={"text": TEXT})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "InvalidQuestion"
    assert http.get(f"{base}?namespace=..").status_code == 422
    assert http.delete(f"{base}/q-1?namespace={NS}").status_code == 204
    assert http.get(f"{base}?namespace={NS}").json() == {"questions": []}


def test_the_questions_list_pages_oldest_first_and_a_deletion_shifts_nothing(versions_aws):
    for n in range(5):
        with patch.object(questions.store, "_now", return_value=f"2026-01-0{n + 1}T00:00:00Z"):
            questions.put_question(NS, f"z-{4 - n}", TEXT, None, author="a")
    first = questions.list_questions(NS, limit=2)
    assert [q["questionId"] for q in first["questions"]] == ["z-4", "z-3"]
    assert first["next"] == ("2026-01-02T00:00:00Z", "z-3")
    questions.delete_question(NS, "z-3")
    second = questions.list_questions(NS, limit=2, after=first["next"])
    assert [q["questionId"] for q in second["questions"]] == ["z-2", "z-1"]
    last = questions.list_questions(NS, limit=2, after=second["next"])
    assert [q["questionId"] for q in last["questions"]] == ["z-0"] and last["next"] is None


def test_the_questions_route_pages_with_a_token_bound_to_the_namespace(versions_aws):
    app = FastAPI()
    app.include_router(studio_router.questions_router, prefix="/ontology/studio/questions")
    http = TestClient(app)
    base = "/ontology/studio/questions"
    for n in range(3):
        questions.put_question(NS, f"q-{n}", TEXT, None, author="a")
    seen: list[str] = []
    token = None
    while True:
        r = http.get(base, params={"namespace": NS, "maxResults": 2, **({"nextToken": token} if token else {})})
        assert r.status_code == 200, r.text
        seen += [q["questionId"] for q in r.json()["questions"]]
        token = r.json().get("nextToken")
        if token is None:
            break
    assert sorted(seen) == ["q-0", "q-1", "q-2"] and len(seen) == 3
    first = http.get(base, params={"namespace": NS, "maxResults": 1}).json()["nextToken"]
    assert http.get(base, params={"namespace": OTHER, "nextToken": first}).status_code == 400
    assert http.get(base, params={"namespace": NS, "nextToken": "not-a-token"}).status_code == 400
    assert http.get(base, params={"namespace": NS, "maxResults": 101}).status_code == 422
