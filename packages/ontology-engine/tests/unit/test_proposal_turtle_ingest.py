# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Proposal review routes: cancel, reject, update and validate.

Accept now publishes an ontology version; its behaviour is covered by
``test_accept_publishes_version.py`` and the ``test_versions_*`` modules.
DynamoDB and GraphStore are stubbed; no AWS traffic.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from coa_ontology import proposals
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


class _InlineThread:
    """Stand-in for ``threading.Thread`` that runs the target inline.

    The accept handler returns 202 *before* the work runs, so tests would
    otherwise need to poll DDB. Running the worker synchronously inside
    ``start()`` collapses the timeline into a single call without adding
    sleep loops or wait conditions.
    """

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


TURTLE = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex:   <https://example.com/onto#> .

<https://example.com/onto> a owl:Ontology ;
    rdfs:label "Test Ontology" .

ex:Patient a owl:Class ; rdfs:label "Patient" .
ex:Doctor  a owl:Class ; rdfs:label "Doctor" .

ex:treats a owl:ObjectProperty ;
    rdfs:label "treats" ;
    rdfs:domain ex:Doctor ;
    rdfs:range  ex:Patient .

ex:age a owl:DatatypeProperty ;
    rdfs:label "age" ;
    rdfs:domain ex:Patient .
"""


@pytest.fixture()
def accept_client():
    """FastAPI test client with Dynamo + GraphStore stubbed."""
    os.environ["WORKBENCH_BACKEND"] = "opensearch_neptune"
    os.environ.setdefault(
        "NDB_ENDPOINT",
        "https://<test-ndb-cluster>.cluster-<id>.us-east-1.neptune.amazonaws.com:8182",
    )
    os.environ.setdefault(
        "OSS_ENDPOINT",
        "https://<test-aoss-collection>.<region>.aoss.amazonaws.com",
    )

    # In-memory Dynamo stand-in
    dynamo = {
        ("default#PROPOSAL#p-1", "META"): {
            "PK": "default#PROPOSAL#p-1",
            "SK": "META",
            "proposal_id": "p-1",
            "job_id": "job-1",
            "namespace": "default",
            "ontology_id": "https://example.com/onto",
            "status": "pending",
            "ontology_turtle": TURTLE,
            "metadata": {"label": "Test Ontology"},
        },
    }

    mock_table = MagicMock()
    mock_table.get_item = MagicMock(
        side_effect=lambda Key: {"Item": dynamo.get((Key["PK"], Key["SK"]))} if (Key["PK"], Key["SK"]) in dynamo else {}
    )

    def fake_update_item(Key, UpdateExpression, **kwargs):
        item = dynamo.get((Key["PK"], Key["SK"]), {**Key})
        values = kwargs.get("ExpressionAttributeValues", {})
        names = kwargs.get("ExpressionAttributeNames", {})
        # Honor the two status ConditionExpressions dynamo_store emits so the
        # conditional writes behave like real DDB:
        #   ``#s = :expected_status``  — the accept race guard
        #   ``attribute_not_exists(#s) OR #s <> :forbidden_status``
        #                              — _accept_fail's "never downgrade an
        #                                already-accepted proposal" guard
        # Any other shape is a new call site that must be modelled here.
        cond = kwargs.get("ConditionExpression")
        if cond:
            current = item.get("status")
            if cond == "#s = :expected_status":
                unmet = current != values.get(":expected_status")
            elif cond == "attribute_not_exists(#s) OR #s <> :forbidden_status":
                unmet = current is not None and current == values.get(":forbidden_status")
            else:
                # Message hoisted into a local rather than inlined on the assert:
                # ruff 0.8 (the pinned pre-commit hook) and ruff 0.15 (the
                # project / CI formatter via `nx run ontology-engine:lint`) wrap
                # an assert-with-inline-message differently, so an inline message
                # here cannot satisfy both. A plain single-expression assert
                # formats identically under both.
                cond_msg = f"fake_update_item does not model this ConditionExpression: {cond}"
                raise AssertionError(cond_msg)
            if unmet:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "ConditionalCheckFailedException",
                            "Message": "The conditional request failed",
                        }
                    },
                    "UpdateItem",
                )
        for part in UpdateExpression.replace("SET ", "").split(", "):
            field, val_ref = [s.strip() for s in part.split("=")]
            for alias, real in (names or {}).items():
                field = field.replace(alias, real)
            item[field] = values.get(val_ref, val_ref)
        dynamo[(Key["PK"], Key["SK"])] = item

    mock_table.update_item = MagicMock(side_effect=fake_update_item)

    # Async validate/infer job rows go through ``dynamo_store._get_dao()``, not
    # the table above; back the DAO with the same dict so they never reach AWS.
    def fake_dao_put(item, **kwargs):
        dynamo[(item["PK"], item["SK"])] = item

    mock_dao = MagicMock()
    mock_dao.put = MagicMock(side_effect=fake_dao_put)
    mock_dao.get = MagicMock(side_effect=lambda key, **kwargs: dynamo.get((key["PK"], key["SK"])))

    # In-memory S3 stand-in: proposal Turtle/matches are offloaded to S3 and
    # only the key is stored on the DynamoDB item (avoids the 400 KB limit).
    s3_objects: dict[str, bytes] = {}

    class _NoSuchKey(Exception):
        pass

    def fake_put_object(Bucket, Key, Body, **kwargs):
        s3_objects[Key] = Body if isinstance(Body, bytes) else str(Body).encode("utf-8")
        return {}

    def fake_get_object(Bucket, Key, **kwargs):
        if Key not in s3_objects:
            raise _NoSuchKey()
        return {"Body": MagicMock(read=lambda: s3_objects[Key])}

    mock_s3 = MagicMock()
    mock_s3.put_object = MagicMock(side_effect=fake_put_object)
    mock_s3.get_object = MagicMock(side_effect=fake_get_object)
    mock_s3.copy_object = MagicMock(return_value={})
    mock_s3.exceptions.NoSuchKey = _NoSuchKey

    # Mock GraphStore — record every call
    mock_graph = MagicMock()
    mock_graph.get_ontology_by_uri = MagicMock(return_value=None)
    mock_graph.create_ontology = MagicMock(return_value={"uri": "https://example.com/onto"})
    mock_graph.store_class = MagicMock(return_value={})
    mock_graph.store_property = MagicMock(return_value={})
    mock_graph.update_ontology = MagicMock(return_value={})
    mock_graph.load_turtle = MagicMock(
        return_value={
            "status": "ok",
            "graph_uri": "https://example.com/onto",
            "bytes_loaded": len(TURTLE.encode("utf-8")),
        }
    )

    mock_vec = MagicMock()
    # Model a consistent vector index: whatever store_embeddings_batch writes
    # is immediately searchable. The accept path waits for every written
    # embedding to become searchable via list_searchable_entity_uris, so the
    # mock must report the inserted URIs back or the wait spins until timeout.
    _indexed: list[dict] = []

    def _store_batch(items):
        _indexed.extend(items)
        return items

    mock_vec.store_embeddings_batch = MagicMock(side_effect=_store_batch)
    mock_vec.list_searchable_entity_uris = MagicMock(
        side_effect=lambda ontology_id, namespace=None, **kw: [
            row["entity_uri"] for row in _indexed if row.get("ontology_id") == ontology_id and row.get("entity_uri")
        ]
    )

    # Mock Bedrock so we don't call AWS during unit tests.
    mock_bedrock = MagicMock()
    mock_bedrock.model_id = "fake-titan-v2"
    mock_bedrock.embed_texts = MagicMock(side_effect=lambda texts: [[float(len(t))] * 4 for t in texts])

    # Pre-import the ``proposals`` submodule before the patch context so
    # ``unittest.mock.patch("...proposals.Thread")`` can resolve the
    # attribute path. Without this the fixture fails with
    # ``AttributeError: module 'coa_ontology' has no
    # attribute 'proposals'`` because patch's path resolver walks the
    # parent package's __dict__ which doesn't auto-load submodules.
    import coa_ontology.proposals  # noqa: F401

    with (
        patch("coa_ontology.dynamo_store._get_table", return_value=mock_table),
        patch("coa_ontology.dynamo_store._get_s3", return_value=mock_s3),
        patch("coa_ontology.dynamo_store._get_dao", return_value=mock_dao),
        patch("coa_ontology.stores.build_stores", return_value=(mock_graph, mock_vec)),
        patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient", return_value=mock_bedrock),
        patch("coa_ontology.proposals.Thread", _InlineThread),
    ):
        from coa_ontology.main import app

        yield TestClient(app), dynamo, mock_graph, mock_vec, mock_bedrock, s3_objects


def _final_status(dynamo) -> dict:
    """Return the proposal META row after the inline worker has run."""
    return dynamo[("default#PROPOSAL#p-1", "META")]


class TestProposalCancel:
    def test_cancel_pending_proposal(self, accept_client):
        http, dynamo, *_ = accept_client
        resp = http.post("/ontology/proposals/p-1/cancel")
        assert resp.status_code == 200
        assert resp.json() == {"proposal_id": "p-1", "status": "cancelled"}
        assert _final_status(dynamo)["status"] == "cancelled"

    def test_cancel_not_found(self, accept_client):
        http, *_ = accept_client
        resp = http.post("/ontology/proposals/nonexistent/cancel")
        assert resp.status_code == 404

    def test_cancel_accepted_fails(self, accept_client):
        http, dynamo, *_ = accept_client
        dynamo[("default#PROPOSAL#p-1", "META")]["status"] = "accepted"
        resp = http.post("/ontology/proposals/p-1/cancel")
        assert resp.status_code == 409


class TestProposalReject:
    def test_reject_pending_proposal(self, accept_client):
        http, dynamo, *_ = accept_client
        resp = http.post(
            "/ontology/proposals/reject",
            json={"proposal_ids": ["p-1"], "reason": "Not relevant"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["status"] == "rejected"
        assert _final_status(dynamo)["status"] == "rejected"

    def test_reject_already_accepted(self, accept_client):
        http, dynamo, *_ = accept_client
        dynamo[("default#PROPOSAL#p-1", "META")]["status"] = "accepted"
        resp = http.post(
            "/ontology/proposals/reject",
            json={"proposal_ids": ["p-1"], "reason": "Too late"},
        )
        assert resp.status_code == 200
        assert resp.json()[0]["error"] == "already accepted"

    def test_reject_not_found(self, accept_client):
        http, *_ = accept_client
        resp = http.post(
            "/ontology/proposals/reject",
            json={"proposal_ids": ["ghost"], "reason": "Gone"},
        )
        assert resp.status_code == 200
        assert resp.json()[0]["error"] == "not found"


class TestProposalUpdate:
    def test_update_ontology_turtle(self, accept_client):
        http, dynamo, mock_graph, mock_vec, mock_bedrock, s3_objects = accept_client
        new_turtle = TURTLE.replace("Test Ontology", "Updated Ontology")
        resp = http.post(
            "/ontology/proposals/update",
            json={"proposal_id": "p-1", "ontology_turtle": new_turtle},
        )
        assert resp.status_code == 200
        final = _final_status(dynamo)
        assert final["status"] == "updated"
        # Turtle is offloaded to S3 (the DDB item carries only the key, not the
        # inline body — guarding the 400 KB item-size limit).
        assert "ontology_s3_key" in final
        s3_key = final["ontology_s3_key"]
        assert "Updated Ontology" in s3_objects[s3_key].decode("utf-8")

    def test_update_invalid_turtle_422(self, accept_client):
        http, *_ = accept_client
        resp = http.post(
            "/ontology/proposals/update",
            json={"proposal_id": "p-1", "ontology_turtle": "not valid {{{"},
        )
        assert resp.status_code == 422

    def test_update_accepted_proposal_409(self, accept_client):
        http, dynamo, *_ = accept_client
        dynamo[("default#PROPOSAL#p-1", "META")]["status"] = "accepted"
        resp = http.post(
            "/ontology/proposals/update",
            json={"proposal_id": "p-1", "ontology_turtle": TURTLE},
        )
        assert resp.status_code == 409

    def test_update_not_found_404(self, accept_client):
        http, *_ = accept_client
        resp = http.post(
            "/ontology/proposals/update",
            json={"proposal_id": "ghost", "ontology_turtle": TURTLE},
        )
        assert resp.status_code == 404

    def test_update_empty_body_400(self, accept_client):
        http, *_ = accept_client
        resp = http.post(
            "/ontology/proposals/update",
            json={"proposal_id": "p-1"},
        )
        assert resp.status_code == 400


class TestProposalValidate:
    @pytest.fixture(autouse=True)
    def _no_oops_http(self):
        """The inline worker runs tier 3; its OoPS! check would POST to oops.linkeddata.es."""
        with patch("coa_ontology.validation.validators.tier3.OoPSValidator.validate", return_value=[]):
            yield

    def test_validate_starts_job(self, accept_client):
        http, dynamo, *_ = accept_client
        # The validation worker requires Java (HermiT). We only test that
        # the endpoint accepts the request and returns a job — the worker
        # runs on a real thread and may fail due to missing JRE; we don't
        # wait for it.
        resp = http.post("/ontology/proposals/p-1/validate")
        assert resp.status_code == 200
        body = resp.json()
        assert body["proposal_id"] == "p-1"
        assert "job_id" in body

    def test_validate_not_found(self, accept_client):
        http, *_ = accept_client
        resp = http.post("/ontology/proposals/ghost/validate")
        assert resp.status_code == 404

    def test_validate_no_turtle_409(self, accept_client):
        http, dynamo, *_ = accept_client
        dynamo[("default#PROPOSAL#p-1", "META")]["ontology_turtle"] = ""
        resp = http.post("/ontology/proposals/p-1/validate")
        assert resp.status_code == 409

    def test_get_validation_job(self, accept_client):
        http, *_ = accept_client
        # Start a validation job first
        r = http.post("/ontology/proposals/p-1/validate")
        job_id = r.json()["job_id"]
        # Poll it
        resp = http.get(f"/ontology/proposals/p-1/validate/jobs/{job_id}")
        assert resp.status_code == 200
        assert resp.json()["job_id"] == job_id

    def test_get_validation_job_not_found(self, accept_client):
        http, *_ = accept_client
        resp = http.get("/ontology/proposals/p-1/validate/jobs/fake-id")
        assert resp.status_code == 404


class TestStatusSetsComeFromSmithyEnum:
    """The stored statuses are the Smithy-generated ``ProposalStatus`` values.

    Guards against drift between the API contract, the values the worker writes,
    and the literals ``dynamo_store`` needs for its sweep (which cannot import
    proposals.py — circular — so it imports the same generated enum).
    """

    def test_accept_status_constants_match_the_generated_enum(self):
        from coa_control_plane_server.models.proposal_status import ProposalStatus

        assert ProposalStatus.EMBEDDINGS_SYNC.value == proposals.PROPOSAL_STATUS_EMBEDDINGS_SYNC
        assert ProposalStatus.ACCEPT_FAILED.value == proposals.PROPOSAL_STATUS_ACCEPT_FAILED

    def test_dynamo_store_sweep_set_matches_the_accept_guard(self):
        from coa_ontology import dynamo_store

        assert frozenset(proposals._ACCEPT_IN_PROGRESS_STATUSES) == dynamo_store._PROPOSAL_IN_PROGRESS_STATUSES
        assert dynamo_store._PROPOSAL_ACCEPT_FAILED_STATUS == proposals.PROPOSAL_STATUS_ACCEPT_FAILED


class TestInductionBlockingStatuses:
    """A new induction is blocked by unreviewed work AND by an in-flight accept.

    ``acquire_induction_lock`` is held only for the duration of an INDUCTION, so
    without the accept-in-progress states a user could trigger a new induction
    while a worker is mid-merge and leave the namespace holding two competing
    structured proposals.
    """

    def test_blocks_on_unreviewed_work(self):
        for status in ("pending", "updated", "accept_failed"):
            assert status in proposals.PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION

    def test_blocks_while_an_accept_is_merging(self):
        for status in ("accepting", "embeddings_sync"):
            assert status in proposals.PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION

    def test_does_not_block_on_resolved_states(self):
        for status in ("accepted", "rejected", "cancelled", "failed"):
            assert status not in proposals.PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION


class TestNaReadTimeoutBounded:
    """#456/#466: the NA execute_query client must carry a bounded read_timeout
    so a hung Neptune Analytics call can't block the accept worker forever.

    Asserts on the CONSTRUCTED boto client config, not just the module constant —
    a test that only checks the constant would still pass if the ``read_timeout=``
    kwarg were dropped from the client, which is the actual regression to catch.
    """

    def _build_client_config(self, monkeypatch, env_value=None):
        """Reload na_store (so the env-derived constant is re-read), build the
        client with boto3 patched, and return the botocore Config it was given."""
        import importlib

        from coa_ontology.catalog import na_store

        if env_value is None:
            monkeypatch.delenv("NA_READ_TIMEOUT", raising=False)
        else:
            monkeypatch.setenv("NA_READ_TIMEOUT", env_value)
        importlib.reload(na_store)
        na_store._client = None  # force a rebuild (module caches the client)
        with patch.object(na_store.boto3, "client") as mock_boto:
            na_store._get_client()
        assert mock_boto.called, "expected na_store to construct a boto3 client"
        return na_store, mock_boto.call_args.kwargs["config"]

    def test_default_read_timeout_is_bounded_on_the_boto_config(self, monkeypatch):
        na_store, cfg = self._build_client_config(monkeypatch)
        # Finite, positive — NOT None, which was the previous unbounded default.
        assert cfg.read_timeout is not None
        assert cfg.read_timeout > 0
        assert cfg.read_timeout == na_store.NA_READ_TIMEOUT

    def test_read_timeout_is_env_tunable(self, monkeypatch):
        _na_store, cfg = self._build_client_config(monkeypatch, env_value="45")
        assert cfg.read_timeout == 45

    def test_zero_restores_unbounded_escape_hatch(self, monkeypatch):
        """NA_READ_TIMEOUT=0 is the documented escape hatch back to the old
        unbounded behavior (read_timeout=None), for the rare case a legitimately
        very long NA query must not be cut off."""
        _na_store, cfg = self._build_client_config(monkeypatch, env_value="0")
        assert cfg.read_timeout is None

    def test_sdk_retries_stay_disabled(self, monkeypatch):
        """Bounding the timeout must not re-enable SDK retries — NA rejects
        retried execute_query requests with UnprocessableException, so retries
        are handled at our own layer."""
        _na_store, cfg = self._build_client_config(monkeypatch)
        assert cfg.retries["total_max_attempts"] == 1
