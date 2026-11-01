# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Batch serialization must not fail on models that forbid ``temperature``.

graphrag's ``batch_inference_utils.get_request_body`` reads
``inference_parameters['temperature']`` unconditionally, but llama_index drops
``temperature`` for ``BEDROCK_NO_TEMP_MODELS`` (the default extraction model,
``claude-sonnet-5``, is one). Without the patch the serializer raises
``KeyError('temperature')`` once a worker reaches the Bedrock batch minimum.

The two batch extractors import ``get_request_body`` into their OWN namespace and
call it from ``_get_json``, and extraction runs in spawn-started workers that
re-import the toolkit fresh — so the patch must reach every binding and re-apply
in each worker. These tests drive the extractor-module bindings and a real spawn
worker, not just the utility function. Skipped where the toolkit is absent.
"""

from __future__ import annotations

import multiprocessing

import pytest

pytestmark = pytest.mark.unit

_bedrock_converse_module = pytest.importorskip(
    "llama_index.llms.bedrock_converse",
    reason="only the kg-build image installs llama_index",
)
if getattr(_bedrock_converse_module, "__coa_test_stub__", False):
    pytest.skip("real llama_index is unavailable", allow_module_level=True)
pytest.importorskip(
    "graphrag_toolkit.lexical_graph.indexing.utils.batch_inference_utils",
    reason="only the kg-build image installs graphrag",
)

from llama_index.core.base.llms.types import ChatMessage, MessageRole  # noqa: E402
from llama_index.llms.bedrock_converse import BedrockConverse  # noqa: E402

_NO_TEMP_MODEL = "us.anthropic.claude-sonnet-5"
_TEMP_MODEL = "anthropic.claude-sonnet-4-6"
_MESSAGES = [ChatMessage(role=MessageRole.USER, content="extract entities")]

# The modules whose get_request_body binding the fix must reach. The extractors
# are the real batch consumers; the utils module is the shared helper.
_EXTRACTOR_MODULES = (
    "graphrag_toolkit.lexical_graph.indexing.extract.batch_topic_extractor_sync",
    "graphrag_toolkit.lexical_graph.indexing.extract.batch_llm_proposition_extractor_sync",
)


def _spawn_probe_extractor_serializer() -> dict:
    """Runs in a SPAWN worker: prove the patch re-applies in a fresh interpreter.

    Module-level (picklable) so ProcessPoolExecutor/spawn can import it. Checks
    the topic extractor's own ``get_request_body`` binding: raises before the
    patch (fresh import = original), omits temperature after.
    """
    import importlib

    from coa_sources.documents.kg_build import batch_patches

    topic = importlib.import_module(_EXTRACTOR_MODULES[0])
    llm = BedrockConverse(model=_NO_TEMP_MODEL, region_name="us-east-1", max_tokens=100)
    params = llm._get_all_kwargs()

    out: dict = {}
    try:
        topic.get_request_body(llm, _MESSAGES, params)
        out["raised_before_patch"] = False
    except KeyError:
        out["raised_before_patch"] = True

    batch_patches._patch_graphrag_batch_request_body()
    body = topic.get_request_body(llm, _MESSAGES, params)
    out["temperature_absent_after_patch"] = "temperature" not in body
    return out


@pytest.fixture
def restore_toolkit_bindings():
    """Save/restore the patched module globals so tests stay order-independent."""
    import importlib

    from coa_sources.documents.kg_build import batch_patches
    from graphrag_toolkit.lexical_graph.indexing.utils import pipeline_utils

    saved = {path: importlib.import_module(path).get_request_body for path in batch_patches._GET_REQUEST_BODY_MODULES}
    saved_init = pipeline_utils._init_worker
    saved_flag = batch_patches._graphrag_worker_init_patched
    saved_toolkit_init = batch_patches._toolkit_init_worker
    try:
        yield batch_patches
    finally:
        for path, fn in saved.items():
            importlib.import_module(path).get_request_body = fn
        pipeline_utils._init_worker = saved_init
        batch_patches._graphrag_worker_init_patched = saved_flag
        batch_patches._toolkit_init_worker = saved_toolkit_init


class TestBatchRequestBodyBindings:
    def test_unpatched_extractor_binding_raises_for_no_temp_model(self):
        """Establishes the gap: the extractor's OWN get_request_body (not just the
        utils one) raises for sonnet-5. If this stops raising, the toolkit fixed
        it upstream and the patch can be removed."""
        import importlib

        topic = importlib.import_module(_EXTRACTOR_MODULES[0])
        llm = BedrockConverse(model=_NO_TEMP_MODEL, region_name="us-east-1", max_tokens=100)
        params = llm._get_all_kwargs()
        assert "temperature" not in params
        if getattr(topic.get_request_body, "_coa_no_temp_wrapped", False):
            pytest.skip("binding already patched in this process")
        with pytest.raises(KeyError, match="temperature"):
            topic.get_request_body(llm, _MESSAGES, params)

    def test_patch_reaches_every_binding_and_omits_temperature(self, restore_toolkit_bindings):
        import importlib

        batch_patches = restore_toolkit_bindings
        batch_patches._patch_graphrag_batch_request_body()

        llm = BedrockConverse(model=_NO_TEMP_MODEL, region_name="us-east-1", max_tokens=100)
        params = llm._get_all_kwargs()
        # Every module that binds get_request_body — utils AND both extractors —
        # must now omit temperature without raising.
        for path in batch_patches._GET_REQUEST_BODY_MODULES:
            module = importlib.import_module(path)
            assert getattr(module.get_request_body, "_coa_no_temp_wrapped", False), path
            body = module.get_request_body(llm, _MESSAGES, params)
            assert "temperature" not in body, path

    def test_patch_preserves_temperature_for_temp_model(self, restore_toolkit_bindings):
        import importlib

        batch_patches = restore_toolkit_bindings
        batch_patches._patch_graphrag_batch_request_body()

        llm = BedrockConverse(model=_TEMP_MODEL, region_name="us-east-1", max_tokens=100)
        params = llm._get_all_kwargs()
        assert "temperature" in params
        topic = importlib.import_module(_EXTRACTOR_MODULES[0])
        body = topic.get_request_body(llm, _MESSAGES, params)
        assert body["temperature"] == params["temperature"]


class TestBatchWorkerInit:
    def test_worker_init_patch_routes_pipeline_initializer(self, restore_toolkit_bindings):
        """run_pipeline's spawn initializer is rebound to our _batch_worker_init so
        the patch travels into each worker."""
        from graphrag_toolkit.lexical_graph.indexing.utils import pipeline_utils

        batch_patches = restore_toolkit_bindings
        batch_patches._graphrag_worker_init_patched = False
        batch_patches._patch_graphrag_worker_init()
        assert pipeline_utils._init_worker is batch_patches._batch_worker_init
        # The original toolkit initializer is captured so the worker init can
        # delegate to it rather than re-implementing it.
        assert batch_patches._toolkit_init_worker is not None
        assert batch_patches._toolkit_init_worker is not batch_patches._batch_worker_init

    def test_worker_init_delegates_to_toolkit_init_and_applies_patch(self, restore_toolkit_bindings):
        """_batch_worker_init runs the captured toolkit initializer AND applies the
        serializer patch — it does not reimplement the config step."""
        import importlib

        batch_patches = restore_toolkit_bindings
        calls = []
        batch_patches._graphrag_worker_init_patched = False
        batch_patches._patch_graphrag_worker_init()
        # Stand in for the toolkit initializer to prove delegation.
        batch_patches._toolkit_init_worker = lambda snapshot: calls.append(snapshot)

        batch_patches._batch_worker_init({"sentinel": 1})

        assert calls == [{"sentinel": 1}], "did not delegate to the captured toolkit initializer"
        topic = importlib.import_module(_EXTRACTOR_MODULES[0])
        assert getattr(topic.get_request_body, "_coa_no_temp_wrapped", False)

    def test_patch_applies_inside_a_spawned_worker(self, restore_toolkit_bindings):
        """The real gap-2 regression: a freshly spawned worker re-imports the
        toolkit unpatched, and the patch must take hold there too."""
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(processes=1) as pool:
            result = pool.apply(_spawn_probe_extractor_serializer)
        assert result["raised_before_patch"] is True, "fresh worker should start unpatched"
        assert result["temperature_absent_after_patch"] is True, "patch did not take effect in the worker"
