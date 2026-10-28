# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Ontology designer chat turns: the turn lease, the chat row and the event files.

A turn is one message of the steward and the agent's work on it. Its state lives in three places:

- ``STUDIOTURN#{proposalId}`` under ``NAMESPACE#{ns}``: the lease of the one running turn, heartbeated
  by its worker and stale after ``STALE_SECONDS``. Kept under the namespace so ``count_active_jobs``
  finds the live turns with one query, and the namespace purge removes it.
- ``STUDIOCHAT`` under ``{ns}#PROPOSAL#{id}``: ``last_seq``, ``active_turn``, the ``turns`` (a map by turn
  id: ``first_seq``, ``last_seq``, ``status``, ``author``, ``started_at``, ``ended_at``, ``swept``) and the
  conversation ``memory``.
- ``proposals/{ns}/{id}/studio/chat/{turnId}.jsonl``: the turn's events, one JSON object per line.

``seq`` counts events across every turn of the proposal. An event is written to the turn's file first
and is committed by the conditional update of ``last_seq`` on the chat row, made only while the turn is
still the active one. Readers trust the row: they never return an event past the committed
``last_seq`` of its turn. A turn whose lease went stale (its worker died) is swept to ``failed``: the
sweep takes one more seq for the turn's ``turn_ended`` event, which readers add, and the worker's next
write fails with :class:`TurnLost`.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from coa_ontology import dynamo_store
from coa_ontology.abox.store import from_dynamo

from . import draft as store

CHAT_SK = "STUDIOCHAT"
TURN_SK_PREFIX = "STUDIOTURN#"
RUNNING, COMPLETED, LIMITED, FAILED = "running", "completed", "limited", "failed"
# A worker heartbeats its lease every BEAT_SECONDS; one silent for STALE_SECONDS is dead.
STALE_SECONDS = int(os.getenv("STUDIO_TURN_STALE_SECONDS", "90"))
BEAT_SECONDS = 20.0
MAX_TURNS = 300
MAX_EVENTS_PAGE = 500
# The model's bound on ``after`` of ``ListStudioEvents``: far above the seqs ``MAX_TURNS`` turns can use.
MAX_SEQ = 1_000_000_000
DEAD_TURN_MESSAGE = "The assistant stopped unexpectedly. Send your message again."
_SWEEP_ATTEMPTS = 3


class TurnInProgress(store.StudioError):
    """Another turn of this proposal is still running."""

    code = "TurnInProgress"


class ConversationFull(store.StudioError):
    """The conversation has ``MAX_TURNS`` turns."""

    code = "ConversationFull"


class TurnLost(Exception):
    """The turn is no longer the proposal's active turn (it was swept, or the proposal was deleted)."""

    def __init__(self, turn_id: str) -> None:
        """Record which turn was lost."""
        super().__init__(f"turn {turn_id} is no longer the active turn")
        self.turn_id = turn_id


def _now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    """A unique id that sorts by creation time (turns and change sets)."""
    return f"{_now():%Y%m%dT%H%M%S%fZ}-{uuid.uuid4().hex[:8]}"


def _lock_key(namespace: str, proposal_id: str) -> dict[str, str]:
    store._checked(namespace, proposal_id)
    return {"PK": dynamo_store._namespace_pk(namespace), "SK": f"{TURN_SK_PREFIX}{proposal_id}"}


def _chat_key(namespace: str, proposal_id: str) -> dict[str, str]:
    store._checked(namespace, proposal_id)
    return {"PK": dynamo_store._proposal_pk(namespace, proposal_id), "SK": CHAT_SK}


def _conditional_failed(e: ClientError) -> bool:
    return e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


# ── Lease ───────────────────────────────────────────────────────────────────


def acquire_turn_lock(namespace: str, proposal_id: str, turn_id: str) -> bool:
    """Take the proposal's turn lease for ``turn_id``; False while a live turn holds it."""
    now = int(time.time())
    try:
        dynamo_store._get_table().put_item(
            Item={
                **_lock_key(namespace, proposal_id),
                "turn_id": turn_id,
                "heartbeat_ts": now,
                "updated_at": _now().isoformat(),
            },
            ConditionExpression="attribute_not_exists(PK) OR heartbeat_ts < :stale",
            ExpressionAttributeValues={":stale": now - STALE_SECONDS},
        )
    except ClientError as e:
        if not _conditional_failed(e):
            raise
        # botocore may have retried a put that had landed: then the lease is ours.
        held = dynamo_store._get_table().get_item(Key=_lock_key(namespace, proposal_id), ConsistentRead=True)
        return (held.get("Item") or {}).get("turn_id") == turn_id
    return True


def heartbeat(namespace: str, proposal_id: str, turn_id: str) -> bool:
    """Renew the lease; False when ``turn_id`` no longer holds it."""
    try:
        dynamo_store._get_table().update_item(
            Key=_lock_key(namespace, proposal_id),
            UpdateExpression="SET heartbeat_ts = :ts, updated_at = :now",
            ConditionExpression="turn_id = :t",
            ExpressionAttributeValues={":ts": int(time.time()), ":now": _now().isoformat(), ":t": turn_id},
        )
    except ClientError as e:
        if _conditional_failed(e):
            return False
        raise
    return True


def release_turn_lock(namespace: str, proposal_id: str, turn_id: str) -> None:
    """Drop the lease if ``turn_id`` holds it; a no-op otherwise."""
    try:
        dynamo_store._get_table().delete_item(
            Key=_lock_key(namespace, proposal_id),
            ConditionExpression="turn_id = :t",
            ExpressionAttributeValues={":t": turn_id},
        )
    except ClientError as e:
        if not _conditional_failed(e):
            raise


def turn_lock_holder(namespace: str, proposal_id: str) -> str | None:
    """The turn holding a live lease, or None (no lease, or a stale one)."""
    item = dynamo_store._get_table().get_item(Key=_lock_key(namespace, proposal_id), ConsistentRead=True).get("Item")
    if not item or int(item.get("heartbeat_ts", 0)) < int(time.time()) - STALE_SECONDS:
        return None
    return str(item["turn_id"])


def live_turns(namespace: str) -> int:
    """How many proposals of the namespace have a turn running now (for ``count_active_jobs``)."""
    stale = int(time.time()) - STALE_SECONDS
    query: dict[str, Any] = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
        "ExpressionAttributeValues": {":pk": dynamo_store._namespace_pk(namespace), ":sk": TURN_SK_PREFIX},
        "ConsistentRead": True,
    }
    count = 0
    while True:
        page = dynamo_store._get_table().query(**query)
        count += sum(1 for item in page.get("Items", []) if int(item.get("heartbeat_ts", 0)) >= stale)
        if "LastEvaluatedKey" not in page:
            return count
        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# ── Chat row and event files ────────────────────────────────────────────────


def get_chat(namespace: str, proposal_id: str) -> dict[str, Any] | None:
    """The chat row without its keys, or None before the first message."""
    item = dynamo_store._get_table().get_item(Key=_chat_key(namespace, proposal_id), ConsistentRead=True).get("Item")
    if item is None:
        return None
    row: dict[str, Any] = from_dynamo({k: v for k, v in item.items() if k not in ("PK", "SK")})
    return row


def memory_of(row: dict[str, Any] | None) -> dict[str, Any]:
    """The conversation memory of a chat row: ``{summary, recent}``."""
    memory = (row or {}).get("memory") or {}
    return {"summary": str(memory.get("summary") or ""), "recent": list(memory.get("recent") or [])}


def events_key(namespace: str, proposal_id: str, turn_id: str) -> str:
    """S3 key of one turn's events."""
    return f"{store.prefix(namespace, proposal_id)}/chat/{turn_id}.jsonl"


def read_events(namespace: str, proposal_id: str, turn_id: str) -> list[dict[str, Any]]:
    """Every event in a turn's file (an empty list when there is none)."""
    text = store._get(events_key(namespace, proposal_id, turn_id))
    return [json.loads(line) for line in (text or "").splitlines() if line.strip()]


def _write_events(namespace: str, proposal_id: str, turn_id: str, events: list[dict[str, Any]]) -> None:
    body = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in events)
    store._put(events_key(namespace, proposal_id, turn_id), body, "application/x-ndjson")


def _event(seq: int, turn_id: str, type_: str, fields: dict[str, Any]) -> dict[str, Any]:
    return {"seq": seq, "turnId": turn_id, "type": type_, "at": _now().isoformat(), **fields}


class EventLog:
    """The events of one running turn: each append writes the file, then commits ``last_seq``.

    Thread-safe. Every write is conditional on the turn still being the active one, so a turn that
    was swept raises :class:`TurnLost` at its next append.
    """

    def __init__(self, namespace: str, proposal_id: str, turn_id: str, events: list[dict[str, Any]]) -> None:
        """Continue the turn ``turn_id`` whose events so far are ``events``."""
        self.namespace = namespace
        self.proposal_id = proposal_id
        self.turn_id = turn_id
        self.events = list(events)
        self._lock = threading.Lock()

    def _landed(self, event: dict[str, Any], *, ended: bool) -> bool:
        """Whether the chat row already holds ``event``: botocore retried a write that had landed."""
        row = get_chat(self.namespace, self.proposal_id) or {}
        turn = (row.get("turns") or {}).get(self.turn_id) or {}
        if int(turn.get("last_seq") or 0) != event["seq"] or turn.get("swept"):
            return False
        if ended:
            return "active_turn" not in row and turn.get("status") == event["status"]
        return row.get("active_turn") == self.turn_id

    def _commit(self, event: dict[str, Any], sets: list[str], values: dict[str, Any], removes: str = "") -> None:
        prev = self.events[-1]["seq"]
        _write_events(self.namespace, self.proposal_id, self.turn_id, [*self.events, event])
        try:
            dynamo_store._get_table().update_item(
                Key=_chat_key(self.namespace, self.proposal_id),
                UpdateExpression="SET " + ", ".join(["last_seq = :s", "#turns.#t.last_seq = :s", *sets]) + removes,
                ConditionExpression="active_turn = :t AND last_seq = :prev",
                ExpressionAttributeNames={"#turns": "turns", "#t": self.turn_id, **({"#st": "status"} if sets else {})},
                ExpressionAttributeValues={":s": event["seq"], ":t": self.turn_id, ":prev": prev, **values},
            )
        except ClientError as e:
            if not _conditional_failed(e):
                raise
            if not self._landed(event, ended=bool(removes)):
                raise TurnLost(self.turn_id) from e
        self.events.append(event)

    def append(self, type_: str, **fields: Any) -> dict[str, Any]:
        """Add one event of the running turn and return it."""
        with self._lock:
            event = _event(self.events[-1]["seq"] + 1, self.turn_id, type_, fields)
            self._commit(event, [], {})
            return event

    def finish(self, status: str, *, message: str | None = None, memory: dict[str, Any] | None = None) -> None:
        """End the turn: its ``turn_ended`` event, its status and the new memory in one row update.

        Then the lease is released, so the next message can start.
        """
        with self._lock:
            fields = {"status": status, "message": message}
            event = _event(self.events[-1]["seq"] + 1, self.turn_id, "turn_ended", fields)
            sets = ["#turns.#t.#st = :status", "#turns.#t.ended_at = :ended"]
            values: dict[str, Any] = {":status": status, ":ended": event["at"]}
            if memory is not None:
                sets.append("memory = :memory")
                values[":memory"] = memory
            self._commit(event, sets, values, " REMOVE active_turn")
        release_turn_lock(self.namespace, self.proposal_id, self.turn_id)


def _ensure_row(namespace: str, proposal_id: str) -> None:
    try:
        dynamo_store._get_table().put_item(
            Item={**_chat_key(namespace, proposal_id), "last_seq": 0, "turns": {}, "memory": memory_of(None)},
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as e:
        if not _conditional_failed(e):
            raise


def start_turn(
    namespace: str, proposal_id: str, *, author: str, text: str, selection: list[dict[str, Any]]
) -> tuple[EventLog, dict[str, Any]]:
    """Begin a turn with the steward's message; return its log and the memory it starts from.

    Takes the turn lease first (:class:`TurnInProgress` while a live turn holds it), after sweeping
    a dead one. The lease is released again when anything here fails.
    """
    sweep_stalled(namespace, proposal_id)
    turn_id = new_id()
    if not acquire_turn_lock(namespace, proposal_id, turn_id):
        holder = turn_lock_holder(namespace, proposal_id)
        raise TurnInProgress("The assistant is still working on the previous message", turnId=holder)
    try:
        _ensure_row(namespace, proposal_id)
        row = get_chat(namespace, proposal_id) or {}
        if row.get("active_turn"):
            raise TurnInProgress("The assistant is still working on the previous message", turnId=row["active_turn"])
        if len(row.get("turns") or {}) >= MAX_TURNS:
            raise ConversationFull(f"This conversation has {MAX_TURNS} messages, the most it can hold")
        prev = int(row.get("last_seq") or 0)
        event = _event(prev + 1, turn_id, "user_message", {"text": text, "author": author, "selection": selection})
        _write_events(namespace, proposal_id, turn_id, [event])
        entry = {
            "first_seq": event["seq"],
            "last_seq": event["seq"],
            "status": RUNNING,
            "author": author,
            "started_at": event["at"],
            "ended_at": None,
        }
        try:
            dynamo_store._get_table().update_item(
                Key=_chat_key(namespace, proposal_id),
                UpdateExpression="SET active_turn = :t, last_seq = :s, #turns.#t = :entry",
                ConditionExpression="attribute_not_exists(active_turn) AND last_seq = :prev",
                ExpressionAttributeNames={"#turns": "turns", "#t": turn_id},
                ExpressionAttributeValues={":t": turn_id, ":s": event["seq"], ":entry": entry, ":prev": prev},
            )
        except ClientError as e:
            if not _conditional_failed(e):
                raise
            again = get_chat(namespace, proposal_id) or {}
            landed = again.get("active_turn") == turn_id and int(again.get("last_seq") or 0) == event["seq"]
            if not landed:  # else botocore retried an update that had landed
                raise TurnInProgress("Another message was sent at the same time") from e
    except BaseException:
        release_turn_lock(namespace, proposal_id, turn_id)
        raise
    return EventLog(namespace, proposal_id, turn_id, [event]), memory_of(row)


def sweep_stalled(namespace: str, proposal_id: str) -> bool:
    """End, as ``failed``, an active turn whose lease has no live holder; True when one was swept."""
    for _ in range(_SWEEP_ATTEMPTS):
        row = get_chat(namespace, proposal_id)
        turn_id = (row or {}).get("active_turn")
        if not row or not turn_id or turn_lock_holder(namespace, proposal_id) == turn_id:
            return False
        prev = int(row["last_seq"])
        try:
            dynamo_store._get_table().update_item(
                Key=_chat_key(namespace, proposal_id),
                UpdateExpression=(
                    "SET last_seq = :s, #turns.#t.last_seq = :s, #turns.#t.#st = :failed, "
                    "#turns.#t.ended_at = :now, #turns.#t.swept = :swept REMOVE active_turn"
                ),
                ConditionExpression="active_turn = :t AND last_seq = :prev",
                ExpressionAttributeNames={"#turns": "turns", "#t": turn_id, "#st": "status"},
                ExpressionAttributeValues={
                    ":s": prev + 1,
                    ":t": turn_id,
                    ":prev": prev,
                    ":failed": FAILED,
                    ":now": _now().isoformat(),
                    ":swept": True,
                },
            )
        except ClientError as e:
            if _conditional_failed(e):
                continue  # the worker wrote meanwhile: look again
            raise
        return True
    return False


def turn_view(turn_id: str, turn: dict[str, Any]) -> dict[str, Any]:
    """A turn as the API shows it."""
    return {
        "turnId": turn_id,
        "status": turn.get("status"),
        "author": turn.get("author"),
        "startedAt": turn.get("started_at"),
        "endedAt": turn.get("ended_at"),
    }


def _dead_turn_event(turn_id: str, turn: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": int(turn["last_seq"]),
        "turnId": turn_id,
        "type": "turn_ended",
        "at": turn.get("ended_at"),
        "status": FAILED,
        "message": DEAD_TURN_MESSAGE,
    }


def list_events(namespace: str, proposal_id: str, *, after: int, limit: int) -> dict[str, Any]:
    """The committed events with a seq above ``after``, oldest first, at most ``limit``; sweeps a dead turn.

    Only the files of turns with such events are read. ``turn`` is the newest turn, or None.
    """
    sweep_stalled(namespace, proposal_id)
    row = get_chat(namespace, proposal_id)
    if not row:
        return {"events": [], "lastSeq": 0, "more": False, "turn": None}
    turns = sorted((row.get("turns") or {}).items(), key=lambda kv: int(kv[1]["first_seq"]))
    events: list[dict[str, Any]] = []
    more = False
    for turn_id, turn in turns:
        last = int(turn["last_seq"])
        if last <= after:
            continue
        if len(events) >= limit:
            more = True
            break
        swept = bool(turn.get("swept"))
        top = last - 1 if swept else last
        events.extend(e for e in read_events(namespace, proposal_id, turn_id) if after < int(e["seq"]) <= top)
        if swept:
            events.append(_dead_turn_event(turn_id, turn))
    if len(events) > limit:
        events, more = events[:limit], True
    newest = turn_view(*turns[-1]) if turns else None
    return {"events": events, "lastSeq": int(row.get("last_seq") or 0), "more": more, "turn": newest}
