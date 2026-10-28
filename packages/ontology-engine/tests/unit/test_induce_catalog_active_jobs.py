# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for count_active_jobs in induce_catalog.py.

Backs the namespace-deletion precondition check (control-plane
delete_handler._check_active_induction_jobs). Counts the independent
sources of in-flight background work against a namespace's ontology data:
  - induction jobs (structured + unstructured) not yet terminal
  - async ontology-ingest jobs (upload-a-custom-ontology-file) not yet
    terminal
  - proposals currently in the "accepting" state (a publish worker in flight)
  - a running ontology-version deployment (live deploy lock)
  - a live publish (the newest version row is still PUBLISHING)
  - a running domain ABox build (a live ABOXLOCK)
  - a running Ontology designer agent turn (a live STUDIOTURN# lease)
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

import pytest

_MODULE = "coa_ontology.induce_catalog.dynamo_store"
_REGISTRY = "coa_ontology.induce_catalog.version_registry"
_ABOX_STORE = "coa_ontology.abox.store"
_CHAT_STORE = "coa_ontology.induce_catalog.chat_store"


def _patch_dynamo_store(
    jobs=None,
    ingest_jobs=None,
    proposals=None,
    deploy_lock_holder=None,
    versions=None,
    abox_lock_holder=None,
    studio_turns=0,
):
    """Patch every store call count_active_jobs makes, with sane defaults
    (empty / no lock) for whichever the test doesn't care about."""
    stack = ExitStack()
    mocks = {
        "list_jobs": stack.enter_context(patch(f"{_MODULE}.list_jobs", return_value=jobs or [])),
        "list_ingest_jobs": stack.enter_context(patch(f"{_MODULE}.list_ingest_jobs", return_value=ingest_jobs or [])),
        "list_proposals": stack.enter_context(patch(f"{_MODULE}.list_proposals", return_value=proposals or [])),
        "deploy_lock_holder": stack.enter_context(
            patch(f"{_REGISTRY}.deploy_lock_holder", return_value=deploy_lock_holder)
        ),
        "list_versions": stack.enter_context(patch(f"{_REGISTRY}.list_versions", return_value=versions or [])),
        "abox_lock_holder": stack.enter_context(patch(f"{_ABOX_STORE}.lock_holder", return_value=abox_lock_holder)),
        "live_turns": stack.enter_context(patch(f"{_CHAT_STORE}.live_turns", return_value=studio_turns)),
    }
    return stack, mocks


@pytest.mark.unit
class TestCountActiveJobs:
    def test_nothing_active_returns_zero(self):
        from coa_ontology.induce_catalog import count_active_jobs

        stack, _ = _patch_dynamo_store()
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 0}

    def test_only_terminal_induction_jobs_returns_zero(self):
        from coa_ontology.induce_catalog import count_active_jobs

        jobs = [{"status": "completed"}, {"status": "failed"}, {"status": "ingested"}]
        stack, _ = _patch_dynamo_store(jobs=jobs)
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 0}

    def test_non_terminal_induction_jobs_are_counted(self):
        from coa_ontology.induce_catalog import count_active_jobs

        jobs = [
            {"status": "pending"},
            {"status": "generating_embeddings"},
            {"status": "completed"},
            {"status": "querying_neptune"},
        ]
        stack, _ = _patch_dynamo_store(jobs=jobs)
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 3}

    def test_induction_job_with_missing_status_counts_as_active(self):
        """Defensive: a job record with no status attribute (malformed / very old
        data) should not be silently treated as terminal — err toward blocking
        deletion rather than risking a race against unknown job state."""
        from coa_ontology.induce_catalog import count_active_jobs

        stack, _ = _patch_dynamo_store(jobs=[{}])
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 1}

    def test_queries_both_structured_and_unstructured_induction_jobs(self):
        """source_type=None must be passed through so both job families are
        included in the scan (dynamo_store.list_jobs treats None as no filter)."""
        from coa_ontology.induce_catalog import count_active_jobs

        stack, mocks = _patch_dynamo_store()
        with stack:
            count_active_jobs("ns-1")

        mocks["list_jobs"].assert_called_once_with(namespace="ns-1", source_type=None, limit=1000)

    def test_accepting_proposals_are_counted(self):
        """A proposal mid-accept has a background thread publishing an ontology
        version (snapshot writes, then possibly starting a deployment); this
        must block deletion even though the induction job that produced the
        proposal is long since terminal."""
        from coa_ontology.induce_catalog import count_active_jobs

        stack, mocks = _patch_dynamo_store(proposals=[{"status": "accepting"}, {"status": "accepting"}])
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 2}
        mocks["list_proposals"].assert_called_once_with(namespace="ns-1", status="accepting", limit=1000)

    def test_pending_proposals_do_not_block(self):
        """A pending proposal is just awaiting steward review — no background
        writer, so it must not block namespace deletion indefinitely."""
        from coa_ontology.induce_catalog import count_active_jobs

        # list_proposals is called with status="accepting" so a pending
        # proposal would never be returned by the mock in the first place —
        # this test documents that expectation explicitly.
        stack, mocks = _patch_dynamo_store()
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 0}
        mocks["list_proposals"].assert_called_once_with(namespace="ns-1", status="accepting", limit=1000)

    def test_only_terminal_ingest_jobs_returns_zero(self):
        from coa_ontology.induce_catalog import count_active_jobs

        ingest_jobs = [{"status": "completed"}, {"status": "failed"}]
        stack, _ = _patch_dynamo_store(ingest_jobs=ingest_jobs)
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 0}

    def test_non_terminal_ingest_jobs_are_counted(self):
        """An in-progress "upload custom ontology" job (ingest-from-S3) has a
        background writer doing Turtle load + embeddings just like an
        induction job — pending/running/embeddings_sync must all block."""
        from coa_ontology.induce_catalog import count_active_jobs

        ingest_jobs = [
            {"status": "pending"},
            {"status": "running"},
            {"status": "embeddings_sync"},
            {"status": "completed"},
        ]
        stack, _ = _patch_dynamo_store(ingest_jobs=ingest_jobs)
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 3}

    def test_queries_ingest_jobs_for_the_namespace(self):
        from coa_ontology.induce_catalog import count_active_jobs

        stack, mocks = _patch_dynamo_store()
        with stack:
            count_active_jobs("ns-1")

        mocks["list_ingest_jobs"].assert_called_once_with(namespace="ns-1", limit=1000)

    def test_all_three_sources_are_combined(self):
        from coa_ontology.induce_catalog import count_active_jobs

        stack, _ = _patch_dynamo_store(
            jobs=[{"status": "pending"}],
            ingest_jobs=[{"status": "running"}, {"status": "completed"}],
            proposals=[{"status": "accepting"}],
        )
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 3}

    def test_running_deployment_is_counted(self):
        """A deployment writes the namespace's domain graph, embeddings and ``latest/``
        while it holds the deploy lock: deleting the namespace underneath it races."""
        from coa_ontology.induce_catalog import count_active_jobs

        stack, mocks = _patch_dynamo_store(deploy_lock_holder="d-1")
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 1}
        mocks["deploy_lock_holder"].assert_called_once_with("ns-1")

    def test_live_publish_is_counted(self):
        from coa_ontology.induce_catalog import count_active_jobs

        stack, mocks = _patch_dynamo_store(versions=[{"version": 2, "status": "PUBLISHING"}, {"version": 1}])
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 1}
        mocks["list_versions"].assert_called_once_with("ns-1")

    @pytest.mark.parametrize("status", ["PUBLISHED", "PUBLISH_FAILED"])
    def test_finished_newest_version_does_not_block(self, status):
        """Only the newest row matters: list_versions already sweeps a dead PUBLISHING row to failed."""
        from coa_ontology.induce_catalog import count_active_jobs

        stack, _ = _patch_dynamo_store(
            versions=[{"version": 2, "status": status}, {"version": 1, "status": "PUBLISHING"}]
        )
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 0}

    def test_running_abox_build_is_counted(self):
        """A domain ABox build reads the preprocessed documents and writes the ABox graph, its S3
        artifacts and ``ABOX*`` rows while it holds its lease: deleting the namespace underneath it races."""
        from coa_ontology.induce_catalog import count_active_jobs

        stack, mocks = _patch_dynamo_store(abox_lock_holder="b1")
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 1}
        mocks["abox_lock_holder"].assert_called_once_with("ns-1")

    def test_a_namespace_teardowns_own_abox_lock_is_no_build(self):
        """The teardown keeps ``ABOXLOCK`` so no build starts; were that counted, retrying a failed
        namespace delete would be refused until the lock went stale."""
        from coa_ontology.induce_catalog import count_active_jobs

        stack, _ = _patch_dynamo_store(abox_lock_holder="teardown-1a2b3c4d")
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 0}

    def test_no_abox_build_adds_nothing(self):
        from coa_ontology.induce_catalog import count_active_jobs

        stack, mocks = _patch_dynamo_store(abox_lock_holder=None)
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 0}
        mocks["abox_lock_holder"].assert_called_once_with("ns-1")

    def test_running_studio_turns_are_counted(self):
        """An agent turn writes the proposal's chat rows and event files until it ends."""
        from coa_ontology.induce_catalog import count_active_jobs

        stack, mocks = _patch_dynamo_store(studio_turns=2)
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 2}
        mocks["live_turns"].assert_called_once_with("ns-1")

    def test_every_source_is_combined(self):
        from coa_ontology.induce_catalog import count_active_jobs

        stack, _ = _patch_dynamo_store(
            jobs=[{"status": "pending"}],
            proposals=[{"status": "accepting"}],
            deploy_lock_holder="d-1",
            versions=[{"version": 3, "status": "PUBLISHING"}],
            abox_lock_holder="b1",
            studio_turns=1,
        )
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 6}
