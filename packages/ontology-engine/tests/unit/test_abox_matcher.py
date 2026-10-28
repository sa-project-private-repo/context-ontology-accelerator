# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import json
import logging
import math

import pytest
from coa_common.bedrock import BedrockTruncationError
from coa_common.constants import DEFAULT_EMBED_DIMENSIONS
from coa_ontology.abox import mapping as mp
from coa_ontology.abox import matcher
from coa_ontology.abox.tbox import load_tbox

pytestmark = pytest.mark.unit

TTL = """
@prefix owl: <http://www.w3.org/2002/07/owl#> . @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix o: <http://e.com/o#> .
o:Customer a owl:Class ; rdfs:label "Customer" . o:Order a owl:Class ; rdfs:label "Order" .
o:placedBy a owl:ObjectProperty ; rdfs:domain o:Order ; rdfs:range o:Customer ; rdfs:label "placed by" .
o:total a owl:DatatypeProperty ; rdfs:domain o:Order ; rdfs:label "total" .
"""
C, O = "http://e.com/o#Customer", "http://e.com/o#Order"  # noqa: E741


def embed(texts):
    # Deterministic toy embedding: letter counts.
    return [[t.lower().count(ch) for ch in "abcdefghijklmnopqrstuvwxyz"] for t in texts]


class Judge:
    def __init__(self, choose):
        self.calls = []
        self.choose = choose

    def __call__(self, system, user):
        payload = json.loads(user)
        self.calls.append(payload)
        return {"decisions": [{"id": it["id"], "choice": self.choose(it), "rationale": "r"} for it in payload["items"]]}


def _pick(label):
    return lambda it: next((i for i, c in enumerate(it["candidates"]) if c.startswith(label)), -1)


def test_class_decision_and_cache_by_candidate_hash():
    tb = load_tbox(TTL)
    emb = matcher.Embeddings(embed)
    items = matcher.class_items(["Company", "Purchase Order"], tb, emb)
    judge = Judge(lambda it: _pick("Customer")(it) if it["text"] == "Company" else _pick("Order")(it))
    rows = matcher.decide(items, {}, judge=judge, version=2)
    got = {r.key: (r.target, r.decided_for_version) for r in rows}
    assert got == {("Company",): (C, 2), ("Purchase Order",): (O, 2)}
    assert len(judge.calls) == 1  # one batch
    existing = {r.id: r for r in rows}
    assert matcher.decide(items, existing, judge=judge, version=3) == []  # unchanged candidates: no LLM
    assert len(judge.calls) == 1


def test_manual_rows_are_skipped_and_an_explicit_none_is_ignore():
    tb = load_tbox(TTL)
    emb = matcher.Embeddings(embed)
    items = matcher.class_items(["Company", "Gizmo"], tb, emb)
    manual = mp.MappingRow(mp.CLASS, ("Company",), mp.IGNORE, origin=mp.MANUAL)
    rows = matcher.decide(items, {manual.id: manual}, judge=Judge(lambda it: -1), version=1)
    assert [(r.key, r.target) for r in rows] == [(("Gizmo",), mp.IGNORE)]


def test_relation_candidates_include_inverse_and_attribute_fallback_is_free():
    tb = load_tbox(TTL)
    emb = matcher.Embeddings(embed)
    [rel] = matcher.relation_items([("PLACED", C, O)], tb, emb)  # Customer PLACED Order: placedBy inverse
    assert [(c.iri, c.direction) for c in rel.candidates] == [("http://e.com/o#placedBy", mp.INVERSE)]
    rows = matcher.decide([rel], {}, judge=Judge(lambda it: 0), version=1)
    assert (rows[0].target, rows[0].direction) == ("http://e.com/o#placedBy", mp.INVERSE)
    [attr] = matcher.attribute_items([(C, "phone")], tb, emb)  # no datatype prop admits Customer
    judge = Judge(lambda it: 0)
    assert [(r.target, r.rationale) for r in matcher.decide([attr], {}, judge=judge, version=1)] == [
        (mp.FREE, "No compatible ontology term.")
    ]
    assert judge.calls == []


def test_candidates_are_capped_by_embedding_rank(monkeypatch):
    monkeypatch.setattr(matcher, "TOP_K", 1)
    tb = load_tbox(TTL)
    [item] = matcher.class_items(["Customer"], tb, matcher.Embeddings(embed))
    assert [c.iri for c in item.candidates] == [C]


class _FakeBedrock:
    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.invocations = []
        _FakeBedrock.instances.append(self)

    def invoke(self, system, user, **kwargs):
        self.invocations.append(kwargs)
        return type("R", (), {"result": {"decisions": []}})()


def test_bedrock_judge_leaves_temperature_unset_unless_configured(monkeypatch):
    monkeypatch.setattr("coa_common.bedrock.BedrockClient", _FakeBedrock)
    monkeypatch.delenv("ABOX_LLM_TEMPERATURE", raising=False)
    monkeypatch.delenv("ABOX_LLM_MAX_TOKENS", raising=False)
    assert matcher.bedrock_judge("m")("s", "u") == {"decisions": []}
    assert _FakeBedrock.instances[-1].kwargs == {"model_id": "m"}
    assert _FakeBedrock.instances[-1].invocations == [{"max_tokens": 8000, "temperature": None}]
    monkeypatch.setenv("ABOX_LLM_TEMPERATURE", "0.2")
    matcher.bedrock_judge("m")("s", "u")
    assert _FakeBedrock.instances[-1].invocations == [{"max_tokens": 8000, "temperature": 0.2}]


def test_bedrock_judge_takes_its_output_budget_from_the_env_once(monkeypatch):
    monkeypatch.setattr("coa_common.bedrock.BedrockClient", _FakeBedrock)
    monkeypatch.delenv("ABOX_LLM_TEMPERATURE", raising=False)
    monkeypatch.setenv("ABOX_LLM_MAX_TOKENS", "32000")
    judge = matcher.bedrock_judge("m")
    monkeypatch.setenv("ABOX_LLM_MAX_TOKENS", "100")  # read when the judge is made, not per call
    judge("s", "u")
    assert _FakeBedrock.instances[-1].invocations == [{"max_tokens": 32000, "temperature": None}]


class _ScriptedBedrock:
    """Answers each invoke with the next of ``replies``: an exception is raised, anything else returned."""

    replies: list = []

    def __init__(self, **kwargs):
        self.calls = 0

    def invoke(self, system, user, **kwargs):
        self.calls += 1
        reply = _ScriptedBedrock.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return type("R", (), {"result": reply})()


def _not_json() -> ValueError:
    return ValueError("Invalid Bedrock response format: Expecting value: line 1 column 1 (char 0)")


def test_bedrock_judge_asks_once_more_when_the_reply_is_not_json(monkeypatch):
    """One reply with prose instead of JSON failed a whole domain ABox build on dev."""
    monkeypatch.setattr("coa_common.bedrock.BedrockClient", _ScriptedBedrock)
    _ScriptedBedrock.replies = [_not_json(), {"decisions": [{"id": 0, "choice": "c0"}]}]
    assert matcher.bedrock_judge("m")("s", "u") == {"decisions": [{"id": 0, "choice": "c0"}]}
    assert _ScriptedBedrock.replies == []


def test_bedrock_judge_gives_up_after_a_second_reply_that_is_not_json(monkeypatch):
    monkeypatch.setattr("coa_common.bedrock.BedrockClient", _ScriptedBedrock)
    _ScriptedBedrock.replies = [_not_json(), _not_json(), {"decisions": []}]
    with pytest.raises(ValueError, match="Invalid Bedrock response format"):
        matcher.bedrock_judge("m")("s", "u")
    assert _ScriptedBedrock.replies == [{"decisions": []}]  # asked twice, not three times


def test_bedrock_judge_does_not_ask_again_after_a_truncated_reply(monkeypatch):
    """A truncated batch is split by ``decide``; asking the same batch again would only truncate again."""
    monkeypatch.setattr("coa_common.bedrock.BedrockClient", _ScriptedBedrock)
    truncated = BedrockTruncationError(model_id="m", output_tokens=8000, max_tokens=8000)
    _ScriptedBedrock.replies = [truncated, {"decisions": []}]
    with pytest.raises(BedrockTruncationError):
        matcher.bedrock_judge("m")("s", "u")
    assert _ScriptedBedrock.replies == [{"decisions": []}]


@pytest.mark.parametrize("bad", ["lots", "0", "-5", "1.5"])
def test_bedrock_judge_rejects_a_bad_max_tokens_when_created(monkeypatch, bad):
    monkeypatch.setattr("coa_common.bedrock.BedrockClient", _FakeBedrock)
    monkeypatch.setenv("ABOX_LLM_MAX_TOKENS", bad)
    with pytest.raises(ValueError, match="ABOX_LLM_MAX_TOKENS"):
        matcher.bedrock_judge("m")


# ---- unusable replies ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"decisions": []},
        {"decisions": "nope"},
        {"decisions": [{"id": "0", "choice": 0, "rationale": "r"}]},  # string id
        {"decisions": [{"id": True, "choice": 0}]},  # bool is not an id
        {"decisions": [{"id": 0}]},  # no choice
        {"decisions": [{"id": 0, "choice": 99}]},  # out of range
        {"decisions": [{"id": 0, "choice": -2}]},  # only -1 means "none"
        {"decisions": [{"id": 0, "choice": "0"}]},
        {"decisions": [{"id": 0, "choice": True}]},
        ["not", "a", "dict"],
        None,
    ],
)
def test_an_unusable_reply_writes_no_row(reply):
    tb = load_tbox(TTL)
    items = matcher.class_items(["Gizmo"], tb, matcher.Embeddings(embed))
    assert matcher.decide(items, {}, judge=lambda system, user: reply, version=1) == []


def test_each_item_needs_its_own_valid_decision():
    tb = load_tbox(TTL)
    items = matcher.class_items(["Company", "Gizmo", "Widget"], tb, matcher.Embeddings(embed))
    reply = {"decisions": [{"id": 0, "choice": 0, "rationale": "r"}, {"id": 1, "choice": 7}, {"id": 2, "choice": -1}]}
    rows = matcher.decide(items, {}, judge=lambda system, user: reply, version=1)
    assert [(r.key, r.target) for r in rows] == [(("Company",), C), (("Widget",), mp.IGNORE)]


# ---- partial progress ----------------------------------------------------------------------------


def _three_classes():
    tb = load_tbox(TTL)
    return tb, matcher.class_items(["Alpha", "Beta", "Gamma"], tb, matcher.Embeddings(embed))


def test_a_failing_batch_is_skipped_and_earlier_batches_are_kept(monkeypatch, caplog):
    monkeypatch.setattr(matcher, "BATCH_SIZE", 1)
    _, items = _three_classes()
    ok, calls = Judge(lambda it: 0), []

    def judge(system, user):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("throttled")
        return ok(system, user)

    saved = []
    with caplog.at_level(logging.WARNING):
        rows = matcher.decide(items, {}, judge=judge, version=1, on_batch=lambda batch: saved.append(list(batch)))
    assert [r.key for r in rows] == [("Alpha",), ("Gamma",)]
    assert [[r.key for r in batch] for batch in saved] == [[("Alpha",)], [("Gamma",)]]  # saved batch by batch
    assert "throttled" in caplog.text


def test_every_batch_failing_raises_the_last_error(monkeypatch):
    monkeypatch.setattr(matcher, "BATCH_SIZE", 1)
    tb, items = _three_classes()
    errors = iter([RuntimeError("first"), RuntimeError("second"), RuntimeError("last")])

    def judge(system, user):
        raise next(errors)

    free = matcher.attribute_items([(C, "phone")], tb, matcher.Embeddings(embed))
    saved = []
    with pytest.raises(RuntimeError, match="last"):
        matcher.decide(items + free, {}, judge=judge, version=1, on_batch=lambda batch: saved.append(list(batch)))
    assert [[r.target for r in batch] for batch in saved] == [[mp.FREE]]  # the no-candidate row was still handed over


def test_items_without_candidates_need_no_judge_and_reach_on_batch():
    tb = load_tbox(TTL)
    items = matcher.attribute_items([(C, "phone")], tb, matcher.Embeddings(embed))

    def judge(system, user):
        raise AssertionError("no item needs the model")

    saved = []
    rows = matcher.decide(items, {}, judge=judge, version=1, on_batch=lambda batch: saved.append(list(batch)))
    assert [r.target for r in rows] == [mp.FREE] and saved == [rows]


# ---- truncated replies ---------------------------------------------------------------------------


def _terms(n):
    """``n`` class items Term00, Term01, ...; each has the candidates Customer (0) and Order (1)."""
    return matcher.class_items([f"Term{i:02d}" for i in range(n)], load_tbox(TTL), matcher.Embeddings(embed))


def _truncating(limit=None, poison=frozenset()):
    """A judge whose reply is truncated for more than ``limit`` items or any item in ``poison``.

    Otherwise it picks Order for an odd term and Customer for an even one, answering in reverse order
    so that decisions can only be matched to items by id.
    """
    calls = []

    def judge(system, user):
        items = json.loads(user)["items"]
        calls.append(items)
        if (limit is not None and len(items) > limit) or any(it["text"] in poison for it in items):
            raise BedrockTruncationError(model_id="m", output_tokens=4096, max_tokens=4096)
        return {
            "decisions": [{"id": it["id"], "choice": int(it["text"][-2:]) % 2, "rationale": "r"} for it in items][::-1]
        }

    return judge, calls


@pytest.mark.parametrize(("limit", "sizes"), [(10, [20, 10, 10]), (5, [20, 10, 5, 5, 10, 5, 5])])
def test_a_truncated_batch_is_split_until_every_reply_fits(limit, sizes):
    judge, calls = _truncating(limit=limit)
    rows = matcher.decide(_terms(20), {}, judge=judge, version=1)
    assert [len(c) for c in calls] == sizes
    assert [r.key for r in rows] == [(f"Term{i:02d}",) for i in range(20)]


def test_ids_are_renumbered_per_sub_batch_and_mapped_back_to_their_items():
    judge, calls = _truncating(limit=5)
    rows = matcher.decide(_terms(20), {}, judge=judge, version=1)
    assert all([it["id"] for it in c] == list(range(len(c))) for c in calls)
    assert {r.key[0]: r.target for r in rows} == {f"Term{i:02d}": O if i % 2 else C for i in range(20)}


def test_an_item_whose_reply_truncates_even_alone_is_skipped_and_its_siblings_are_kept(caplog):
    judge, calls = _truncating(poison={"Term02"})
    with caplog.at_level(logging.WARNING, logger=matcher.__name__):
        rows = matcher.decide(_terms(4), {}, judge=judge, version=1)
    assert [r.key for r in rows] == [("Term00",), ("Term01",), ("Term03",)]
    assert [[it["text"] for it in c] for c in calls] == [
        ["Term00", "Term01", "Term02", "Term03"],
        ["Term00", "Term01"],
        ["Term02", "Term03"],
        ["Term02"],
        ["Term03"],
    ]
    assert mp.mapping_id(mp.CLASS, ("Term02",)) in caplog.text  # the skipped item, by its id


def test_on_batch_sees_every_row_once_across_split_batches(monkeypatch):
    monkeypatch.setattr(matcher, "BATCH_SIZE", 8)
    free = matcher.attribute_items([(C, "phone")], load_tbox(TTL), matcher.Embeddings(embed))
    judge, _ = _truncating(limit=3, poison={"Term05"})
    saved = []
    rows = matcher.decide(_terms(12) + free, {}, judge=judge, version=1, on_batch=lambda b: saved.append(list(b)))
    assert len(rows) == 12  # 11 judged terms (Term05 is skipped) and the no-candidate attribute
    assert [r.id for batch in saved for r in batch] == [r.id for r in rows]


def test_a_truncation_resolved_by_splitting_is_not_a_failure(monkeypatch, caplog):
    monkeypatch.setattr(matcher, "BATCH_SIZE", 2)
    truncating, calls = _truncating(limit=1)

    def judge(system, user):
        if "Term02" in user:
            calls.append(json.loads(user)["items"])
            raise RuntimeError("throttled")
        return truncating(system, user)

    with caplog.at_level(logging.WARNING, logger=matcher.__name__):
        rows = matcher.decide(_terms(4), {}, judge=judge, version=1)
    assert [r.key for r in rows] == [("Term00",), ("Term01",)]
    assert [len(c) for c in calls] == [2, 1, 1, 2]  # the throttled batch is not split
    assert "throttled" in caplog.text


def test_when_every_reply_truncates_even_for_one_item_the_truncation_is_raised():
    judge, calls = _truncating(limit=0)
    with pytest.raises(BedrockTruncationError):
        matcher.decide(_terms(3), {}, judge=judge, version=1)
    assert sorted(len(c) for c in calls) == [1, 1, 1, 2, 3]


# ---- prompt --------------------------------------------------------------------------------------


def test_system_prompt_explains_indexes_and_subject_side():
    assert "0-based" in matcher.SYSTEM and "-1" in matcher.SYSTEM
    assert "subject" in matcher.SYSTEM and "left" in matcher.SYSTEM


def test_system_prompt_asks_for_the_json_object_alone_with_short_rationales():
    assert "no other text" in matcher.SYSTEM and "at most 15 words" in matcher.SYSTEM


def test_the_prompt_version_is_kept_so_saved_rows_stay_valid():
    assert matcher.PROMPT_VERSION == "2"  # candidate_hash holds it: a bump would decide every saved row again


def test_relation_candidates_show_direction_with_the_subject_on_the_left():
    tb = load_tbox(TTL)
    emb = matcher.Embeddings(embed)
    [fwd] = matcher.relation_items([("PLACED_BY", O, C)], tb, emb)  # Order PLACED_BY Customer
    [inv] = matcher.relation_items([("PLACED", C, O)], tb, emb)  # Customer PLACED Order
    assert [c.text for c in fwd.candidates] == ["Order -placed by-> Customer"]
    assert [c.text for c in inv.candidates] == ["Customer <-placed by- Order"]


def test_descriptions_are_truncated():
    ttl = TTL.replace('rdfs:label "Customer" .', 'rdfs:label "Customer" ; rdfs:comment "%s" .' % ("x" * 1000))
    tb = load_tbox(ttl)
    [item] = matcher.class_items(["Customer"], tb, matcher.Embeddings(embed))
    text = next(c.text for c in item.candidates if c.iri == C)
    assert text == "Customer: " + "x" * 300


def test_samples_are_shown_but_do_not_change_the_hash():
    tb = load_tbox(TTL)
    emb = matcher.Embeddings(embed)
    [shown] = matcher.class_items({"Company": ["ACME", "Globex", "Initech", "Hooli"]}, tb, emb)
    [plain] = matcher.class_items(["Company"], tb, emb)
    assert shown.display == "Company (e.g. ACME, Globex, Initech)" and shown.text == "Company"
    assert plain.display == "" and plain.candidates == shown.candidates
    judge = Judge(_pick("Customer"))
    rows = matcher.decide([shown], {}, judge=judge, version=1)
    assert json.loads(json.dumps(judge.calls[0]))["items"][0]["text"] == "Company (e.g. ACME, Globex, Initech)"
    assert matcher.decide([plain], {r.id: r for r in rows}, judge=judge, version=2) == []  # same hash: no new call
    assert len(judge.calls) == 1


def test_long_samples_are_cut_and_still_do_not_change_the_hash():
    tb = load_tbox(TTL)
    emb = matcher.Embeddings(embed)
    long_value = "  " + "x" * 300
    [shown] = matcher.class_items({"Company": [long_value, "  ", "ACME"]}, tb, emb)
    [plain] = matcher.class_items(["Company"], tb, emb)
    assert shown.display == f"Company (e.g. {'x' * matcher.MAX_SAMPLE_CHARS}, ACME)"
    assert matcher.candidate_hash(shown.candidates, shown.text) == matcher.candidate_hash(plain.candidates, plain.text)


def test_renaming_a_class_label_rejudges_relation_rows():
    emb = matcher.Embeddings(embed)
    old = matcher.relation_items([("PLACED", C, O)], load_tbox(TTL), emb)
    rows = matcher.decide(old, {}, judge=Judge(lambda it: 0), version=1)
    renamed = load_tbox(TTL.replace('rdfs:label "Customer"', 'rdfs:label "Client"'))
    new = matcher.relation_items([("PLACED", C, O)], renamed, emb)
    assert matcher.decide(old, {r.id: r for r in rows}, judge=Judge(lambda it: 0), version=2) == []
    assert len(matcher.decide(new, {r.id: r for r in rows}, judge=Judge(lambda it: 0), version=2)) == 1


def test_candidate_hash_covers_the_item_text():
    [item] = matcher.class_items(["Customer"], load_tbox(TTL), matcher.Embeddings(embed))
    assert matcher.candidate_hash(item.candidates, "a") != matcher.candidate_hash(item.candidates, "b")


# ---- ranking -------------------------------------------------------------------------------------

OPEN_TTL = """
@prefix owl: <http://www.w3.org/2002/07/owl#> . @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix o: <http://e.com/o#> .
o:Customer a owl:Class ; rdfs:label "Customer" . o:Order a owl:Class ; rdfs:label "Order" .
o:placed a owl:ObjectProperty ; rdfs:label "placed" .
o:qqqq a owl:ObjectProperty ; rdfs:label "qqqq" .
"""


def test_top_k_keeps_whole_properties_not_single_directions(monkeypatch):
    monkeypatch.setattr(matcher, "TOP_K", 1)
    tb = load_tbox(OPEN_TTL)
    [item] = matcher.relation_items([("PLACED", C, O)], tb, matcher.Embeddings(embed))
    assert [(c.iri, c.direction) for c in item.candidates] == [
        ("http://e.com/o#placed", mp.FORWARD),
        ("http://e.com/o#placed", mp.INVERSE),
    ]


def test_vectors_are_unit_length():
    [v] = matcher.Embeddings(embed).vectors(["abc"])
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0)


def test_mapping_embeddings_keep_one_size_whatever_the_chunk_index_uses(monkeypatch):
    made = []

    class FakeClient:
        def __init__(self, **kwargs):
            made.append(kwargs)

        def embed_texts(self, texts):
            return []

    monkeypatch.setattr("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient", FakeClient)
    monkeypatch.setenv("BEDROCK_EMBED_DIMENSIONS", "512")
    matcher.bedrock_embed()
    assert made[0]["dimensions"] == DEFAULT_EMBED_DIMENSIONS  # ranking, so candidate_hash, never depends on it


@pytest.mark.parametrize("bad", ["hot", "2", "-0.1", "nan"])
def test_bedrock_judge_rejects_a_bad_temperature_when_created(monkeypatch, bad):
    monkeypatch.setattr("coa_common.bedrock.BedrockClient", _FakeBedrock)
    monkeypatch.setenv("ABOX_LLM_TEMPERATURE", bad)
    with pytest.raises(ValueError, match="ABOX_LLM_TEMPERATURE"):
        matcher.bedrock_judge("m")
