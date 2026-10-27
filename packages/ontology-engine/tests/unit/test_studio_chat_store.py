# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The designer chat's turn lease, chat row and event files."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from coa_ontology import dynamo_store
from coa_ontology.studio import chat_store as chat

from .studio_fakes import NS

pytestmark = [pytest.mark.unit]
P = "p-1"


@pytest.fixture(autouse=True)
def _aws(versions_aws):
    return versions_aws


def _start(text: str = "Add a parent", author: str = "alice"):
    return chat.start_turn(NS, P, author=author, text=text, selection=[])


def _stale_lease(turn_id: str) -> None:
    """Age the turn lease past ``STALE_SECONDS``, as a worker that died leaves it."""
    old = int(time.time()) - chat.STALE_SECONDS - 5
    dynamo_store._get_table().put_item(Item={**chat._lock_key(NS, P), "turn_id": turn_id, "heartbeat_ts": old})


def test_one_live_turn_per_proposal_and_a_stale_lease_can_be_taken():
    assert chat.acquire_turn_lock(NS, P, "t-1")
    assert not chat.acquire_turn_lock(NS, P, "t-2")
    assert chat.turn_lock_holder(NS, P) == "t-1"
    assert chat.acquire_turn_lock(NS, "p-2", "t-3")  # another proposal has its own lease
    assert chat.heartbeat(NS, P, "t-1") and not chat.heartbeat(NS, P, "t-2")
    _stale_lease("t-1")
    assert chat.turn_lock_holder(NS, P) is None
    assert chat.acquire_turn_lock(NS, P, "t-2")
    chat.release_turn_lock(NS, P, "t-1")  # not the holder: a no-op
    assert chat.turn_lock_holder(NS, P) == "t-2"
    chat.release_turn_lock(NS, P, "t-2")
    assert chat.turn_lock_holder(NS, P) is None


def test_start_turn_writes_the_user_message_and_marks_the_turn_running():
    selection = [{"iri": "http://e/o#Customer", "label": "Customer", "kind": "class"}]
    log, memory = chat.start_turn(NS, P, author="alice", text="고객에 상위 클래스를 추가해줘", selection=selection)
    assert memory == {"summary": "", "recent": []}
    assert [e["type"] for e in log.events] == ["user_message"]
    first = log.events[0]
    assert (first["seq"], first["turnId"], first["text"], first["author"]) == (
        1,
        log.turn_id,
        "고객에 상위 클래스를 추가해줘",
        "alice",
    )
    assert first["selection"] == selection
    assert chat.read_events(NS, P, log.turn_id) == log.events
    row = chat.get_chat(NS, P)
    assert row is not None
    assert (row["last_seq"], row["active_turn"]) == (1, log.turn_id)
    assert row["turns"][log.turn_id]["status"] == chat.RUNNING
    assert chat.turn_lock_holder(NS, P) == log.turn_id


def test_appends_keep_seq_order_across_turns():
    log, _ = _start()
    log.append("thinking", text="Let me look")
    log.append("answer", text="Done")
    log.finish(chat.COMPLETED)
    second, _ = _start("Next")
    assert second.events[0]["seq"] == 5  # user_message, thinking, answer, turn_ended, then this one
    assert [e["seq"] for e in chat.read_events(NS, P, log.turn_id)] == [1, 2, 3, 4]
    assert chat.get_chat(NS, P)["last_seq"] == 5


def test_an_append_after_the_turn_was_swept_raises_turn_lost():
    log, _ = _start()
    _stale_lease(log.turn_id)
    assert chat.sweep_stalled(NS, P)
    with pytest.raises(chat.TurnLost):
        log.append("thinking", text="still here")
    with pytest.raises(chat.TurnLost):
        log.finish(chat.COMPLETED)


def test_finish_turn_ends_it_stores_memory_and_frees_the_lease():
    log, _ = _start()
    memory = {"summary": "s", "recent": [{"role": "user", "text": "Add a parent"}]}
    log.finish(chat.LIMITED, message="Tool budget reached", memory=memory)
    row = chat.get_chat(NS, P)
    assert "active_turn" not in row and row["memory"] == memory
    turn = row["turns"][log.turn_id]
    assert turn["status"] == chat.LIMITED and turn["ended_at"] and turn["last_seq"] == 2
    assert chat.read_events(NS, P, log.turn_id)[-1] == {
        **log.events[-1],
        "type": "turn_ended",
        "status": chat.LIMITED,
        "message": "Tool budget reached",
    }
    assert chat.turn_lock_holder(NS, P) is None


def test_list_events_after_a_seq_reads_only_newer_turns_and_pages():
    first, _ = _start("one")
    first.append("answer", text="1")
    first.finish(chat.COMPLETED)
    second, _ = _start("two")
    second.append("answer", text="2")
    everything = chat.list_events(NS, P, after=0, limit=200)
    assert [e["seq"] for e in everything["events"]] == [1, 2, 3, 4, 5]
    assert everything["lastSeq"] == 5 and not everything["more"]
    assert everything["turn"]["turnId"] == second.turn_id and everything["turn"]["status"] == chat.RUNNING
    with patch.object(chat, "read_events", wraps=chat.read_events) as reads:
        newer = chat.list_events(NS, P, after=3, limit=200)
    assert [e["seq"] for e in newer["events"]] == [4, 5]
    assert [c.args[2] for c in reads.call_args_list] == [second.turn_id]  # the first turn's file is not read
    page = chat.list_events(NS, P, after=0, limit=2)
    assert [e["seq"] for e in page["events"]] == [1, 2] and page["more"]
    rest = chat.list_events(NS, P, after=2, limit=2)
    assert [e["seq"] for e in rest["events"]] == [3, 4] and rest["more"]
    assert chat.list_events(NS, "p-none", after=0, limit=10) == {
        "events": [],
        "lastSeq": 0,
        "more": False,
        "turn": None,
    }


def test_list_events_never_shows_an_event_the_row_has_not_committed():
    log, _ = _start()
    with (
        patch.object(chat.dynamo_store, "_get_table", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        log.append("thinking", text="written to S3 only")
    assert len(chat.read_events(NS, P, log.turn_id)) == 2
    assert [e["seq"] for e in chat.list_events(NS, P, after=0, limit=10)["events"]] == [1]


def test_a_turn_whose_lease_went_stale_is_swept_as_failed():
    log, _ = _start()
    log.append("thinking", text="Looking")
    assert not chat.sweep_stalled(NS, P)  # the lease is live
    _stale_lease(log.turn_id)
    listed = chat.list_events(NS, P, after=0, limit=10)  # a read sweeps too
    assert [e["type"] for e in listed["events"]] == ["user_message", "thinking", "turn_ended"]
    ended = listed["events"][-1]
    assert (ended["seq"], ended["status"], ended["message"]) == (3, chat.FAILED, chat.DEAD_TURN_MESSAGE)
    assert listed["turn"]["status"] == chat.FAILED and listed["lastSeq"] == 3
    assert "active_turn" not in chat.get_chat(NS, P)
    assert not chat.sweep_stalled(NS, P)


def test_a_late_write_of_a_swept_worker_never_shows():
    log, _ = _start()
    _stale_lease(log.turn_id)
    chat.sweep_stalled(NS, P)
    chat._write_events(NS, P, log.turn_id, [*log.events, {**log.events[0], "seq": 2, "type": "answer", "text": "late"}])
    assert [e["type"] for e in chat.list_events(NS, P, after=0, limit=10)["events"]] == ["user_message", "turn_ended"]


def test_a_message_after_a_dead_turn_starts_a_new_turn():
    dead, _ = _start()
    _stale_lease(dead.turn_id)
    log, _ = _start("again")
    assert log.turn_id != dead.turn_id and log.events[0]["seq"] == 3  # the dead turn's turn_ended took 2
    row = chat.get_chat(NS, P)
    assert row["turns"][dead.turn_id]["status"] == chat.FAILED and row["active_turn"] == log.turn_id


def test_a_live_turn_is_refused_with_its_id():
    log, _ = _start()
    with pytest.raises(chat.TurnInProgress) as raised:
        _start("again")
    assert raised.value.detail()["turnId"] == log.turn_id and raised.value.status == 409
    assert chat.turn_lock_holder(NS, P) == log.turn_id


def test_a_full_conversation_is_refused_and_frees_the_lease():
    log, _ = _start()
    log.finish(chat.COMPLETED)
    with patch.object(chat, "MAX_TURNS", 1), pytest.raises(chat.ConversationFull):
        _start("again")
    assert chat.turn_lock_holder(NS, P) is None


def test_live_turns_counts_only_fresh_leases():
    assert chat.live_turns(NS) == 0
    chat.acquire_turn_lock(NS, "p-1", "t-1")
    chat.acquire_turn_lock(NS, "p-2", "t-2")
    assert chat.live_turns(NS) == 2
    old = int(time.time()) - chat.STALE_SECONDS - 5
    dynamo_store._get_table().put_item(Item={**chat._lock_key(NS, "p-2"), "turn_id": "t-2", "heartbeat_ts": old})
    assert chat.live_turns(NS) == 1
    assert chat.live_turns("99999999-2222-3333-4444-555555555555") == 0


def test_ids_sort_by_time():
    first = chat.new_id()
    time.sleep(0.001)
    assert first < chat.new_id()


def test_deleting_a_proposal_deletes_its_studio_rows(versions_aws):
    from .studio_fakes import seed_proposal

    seed_proposal(P)
    _start()
    table = versions_aws.table
    table.put_item(Item={"PK": dynamo_store._proposal_pk(NS, P), "SK": "STUDIOCS#cs-1", "status": "pending"})
    assert dynamo_store.delete_proposal(NS, P)
    rows = table.query(KeyConditionExpression="PK = :pk", ExpressionAttributeValues={":pk": f"{NS}#PROPOSAL#{P}"})
    assert rows["Items"] == []
    assert not dynamo_store.delete_proposal(NS, P)


class _LandedThenFailed:
    """A table whose ``method`` lands the write and then reports a failed condition, as a retried write does."""

    def __init__(self, table, method: str) -> None:
        self._table = table
        self._method = method

    def __getattr__(self, name):
        real = getattr(self._table, name)
        if name != self._method:
            return real

        def landed(**kwargs):
            real(**kwargs)
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, name)

        return landed


def test_a_retried_write_that_had_landed_is_not_a_lost_turn(versions_aws):
    log, _ = _start()
    flaky = _LandedThenFailed(versions_aws.table, "update_item")
    with patch.object(chat.dynamo_store, "_get_table", return_value=flaky):
        log.append("thinking", text="once")
        log.finish(chat.COMPLETED)
    assert [e["type"] for e in chat.list_events(NS, P, after=0, limit=10)["events"]] == [
        "user_message",
        "thinking",
        "turn_ended",
    ]
    assert chat.get_chat(NS, P)["turns"][log.turn_id]["status"] == chat.COMPLETED


def test_a_retried_lease_or_start_that_had_landed_counts_as_taken(versions_aws):
    with patch.object(chat.dynamo_store, "_get_table", return_value=_LandedThenFailed(versions_aws.table, "put_item")):
        assert chat.acquire_turn_lock(NS, P, "t-1")
    chat.release_turn_lock(NS, P, "t-1")
    chat._ensure_row(NS, P)
    flaky = _LandedThenFailed(versions_aws.table, "update_item")
    with patch.object(chat.dynamo_store, "_get_table", return_value=flaky):
        log, _ = _start()
    assert chat.get_chat(NS, P)["active_turn"] == log.turn_id
    assert chat.turn_lock_holder(NS, P) == log.turn_id
