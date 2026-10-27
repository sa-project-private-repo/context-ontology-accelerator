# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Competency questions of a namespace.

A question is a row ``STUDIOCQ#{questionId}`` under ``NAMESPACE#{ns}``, so every draft of the
namespace shares the questions and the namespace purge removes them. The client mints the id.
``lastCheck`` holds the latest verdict of the assistant: the proposal and revision it checked, the
status (``answerable``, ``expressible`` or ``not_expressible``), the query it ran, a sample of the
rows and its explanation. Changing a question's text clears it, since the verdict was about the old text.
"""

from __future__ import annotations

import re
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from coa_common import validate_namespace_name

from coa_ontology import dynamo_store
from coa_ontology.abox.store import from_dynamo

from . import draft as store

SK_PREFIX = "STUDIOCQ#"
MAX_QUESTIONS = 200
MAX_TEXT = 1000
MAX_EXPECTATION = 500
MAX_SAMPLE_ROWS = 5
MAX_SAMPLE_VALUE = 200
STATUSES = ("answerable", "expressible", "not_expressible")
_ID = re.compile(r"[0-9A-Za-z-]{1,64}")


class InvalidQuestion(store.StudioError):
    """A question whose id, text or expectation breaks the limits."""

    status = 422
    code = "InvalidQuestion"


class QuestionNotFound(store.StudioError):
    """No question with that id in the namespace."""

    status = 404
    code = "QuestionNotFound"


class QuestionChanged(store.StudioError):
    """The question's text changed while it was being checked."""

    code = "QuestionChanged"


class TooManyQuestions(store.StudioError):
    """The namespace already holds ``MAX_QUESTIONS`` questions."""

    code = "TooManyQuestions"


def check_id(question_id: str) -> str:
    """``question_id`` if it is 1-64 letters, digits or hyphens; :class:`InvalidQuestion` otherwise."""
    if not _ID.fullmatch(question_id):
        raise InvalidQuestion("A question id is 1-64 letters, digits or hyphens")
    return question_id


def is_question_id(question_id: str) -> bool:
    """Whether ``question_id`` is 1-64 letters, digits or hyphens."""
    return bool(_ID.fullmatch(question_id))


def partition(namespace: str) -> str:
    """The partition key of the namespace's questions."""
    return dynamo_store._namespace_pk(namespace)


def _key(namespace: str, question_id: str) -> dict[str, str]:
    validate_namespace_name(namespace)
    return {"PK": dynamo_store._namespace_pk(namespace), "SK": SK_PREFIX + check_id(question_id)}


def _shape(item: dict[str, Any]) -> dict[str, Any]:
    item = from_dynamo(item)
    return {
        "questionId": item["SK"][len(SK_PREFIX) :],
        "text": item["text"],
        "expectation": item.get("expectation"),
        "lastCheck": item.get("lastCheck"),
        "createdAt": item.get("createdAt"),
        "updatedAt": item.get("updatedAt"),
        "updatedBy": item.get("updatedBy"),
    }


def _items(namespace: str) -> list[dict[str, Any]]:
    validate_namespace_name(namespace)
    table = dynamo_store._get_table()
    condition = Key("PK").eq(dynamo_store._namespace_pk(namespace)) & Key("SK").begins_with(SK_PREFIX)
    query: dict[str, Any] = {"KeyConditionExpression": condition, "ConsistentRead": True}
    items: list[dict[str, Any]] = []
    while True:
        page = table.query(**query)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _order(question: dict[str, Any]) -> tuple[str, str]:
    return (question["createdAt"] or "", question["questionId"])


def list_questions(namespace: str, *, limit: int, after: tuple[str, str] | None = None) -> dict[str, Any]:
    """One page of the namespace's questions, oldest first.

    The namespace holds at most ``MAX_QUESTIONS``, so every row is read and sorted. ``after`` is the
    ``(createdAt, questionId)`` of the last question of an earlier page; the page holds the next
    ``limit`` questions after it, so a question deleted meanwhile shifts nothing. ``next`` is the
    ``(createdAt, questionId)`` to resume after, or None at the end.
    """
    ordered = sorted((_shape(i) for i in _items(namespace)), key=_order)
    if after is not None:
        ordered = [q for q in ordered if _order(q) > after]
    page = ordered[:limit]
    return {"questions": page, "next": _order(page[-1]) if len(ordered) > limit else None}


def get_question(namespace: str, question_id: str) -> dict[str, Any]:
    """One question, or :class:`QuestionNotFound`."""
    item = dynamo_store._get_table().get_item(Key=_key(namespace, question_id), ConsistentRead=True).get("Item")
    if not item:
        raise QuestionNotFound(f"Question '{question_id}' not found")
    return _shape(item)


def _clean(text: str, expectation: str | None) -> tuple[str, str | None]:
    text = text.strip()
    if not 1 <= len(text) <= MAX_TEXT:
        raise InvalidQuestion(f"A question is 1-{MAX_TEXT} characters")
    expectation = (expectation or "").strip() or None
    if expectation is not None and len(expectation) > MAX_EXPECTATION:
        raise InvalidQuestion(f"An expectation is at most {MAX_EXPECTATION} characters")
    return text, expectation


def put_question(
    namespace: str, question_id: str, text: str, expectation: str | None, *, author: str
) -> dict[str, Any]:
    """Create or replace a question; a changed text clears its last check."""
    key = _key(namespace, question_id)
    text, expectation = _clean(text, expectation)
    table = dynamo_store._get_table()
    existing = table.get_item(Key=key, ConsistentRead=True).get("Item")
    if existing is None and len(_items(namespace)) >= MAX_QUESTIONS:
        raise TooManyQuestions(f"A namespace holds at most {MAX_QUESTIONS} questions")
    now = store._now()
    sets = ["#t = :t", "updatedAt = :now", "updatedBy = :by", "createdAt = if_not_exists(createdAt, :now)"]
    removes = []
    values: dict[str, Any] = {":t": text, ":now": now, ":by": author}
    if expectation is None:
        removes.append("expectation")
    else:
        sets.append("expectation = :e")
        values[":e"] = expectation
    if existing is not None and existing.get("text") != text:
        removes.append("lastCheck")
    expression = "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else "")
    item = table.update_item(
        Key=key,
        UpdateExpression=expression,
        ExpressionAttributeNames={"#t": "text"},
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )["Attributes"]
    return _shape(item)


def delete_question(namespace: str, question_id: str) -> None:
    """Delete a question; deleting one that is gone is a no-op."""
    dynamo_store._get_table().delete_item(Key=_key(namespace, question_id))


def _sample(rows: list[list[Any]]) -> list[list[Any]]:
    def cell(c: Any) -> Any:
        if isinstance(c, dict) and isinstance(c.get("value"), str):
            return {**c, "value": c["value"][:MAX_SAMPLE_VALUE]}
        return c

    return [[cell(c) for c in row] for row in rows[:MAX_SAMPLE_ROWS]]


def record_check(
    namespace: str,
    question_id: str,
    *,
    proposal_id: str,
    revision: int,
    status: str,
    query: str | None,
    row_count: int,
    rows: list[list[Any]],
    explanation: str | None = None,
    checked_text: str,
) -> dict[str, Any]:
    """Store the verdict of a check as the question's ``lastCheck``, if its text is still ``checked_text``.

    :class:`QuestionNotFound` if the question is gone, :class:`QuestionChanged` if its text changed:
    the verdict was about the old text.
    """
    if status not in STATUSES:
        raise ValueError(f"Unknown verdict {status!r}")
    last = {
        "proposalId": proposal_id,
        "revision": revision,
        "status": status,
        "query": query,
        "rowCount": row_count,
        "sample": _sample(rows),
        "explanation": explanation,
        "checkedAt": store._now(),
    }
    try:
        item = dynamo_store._get_table().update_item(
            Key=_key(namespace, question_id),
            UpdateExpression="SET lastCheck = :c",
            ConditionExpression="attribute_exists(PK) AND #t = :t",
            ExpressionAttributeNames={"#t": "text"},
            ExpressionAttributeValues={":c": last, ":t": checked_text},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        get_question(namespace, question_id)  # QuestionNotFound when it is gone
        raise QuestionChanged("The question changed while it was checked; check it again") from e
    return _shape(item)
