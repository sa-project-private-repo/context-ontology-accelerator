# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for deploying ontology versions (coa_ontology.versions.deploy)."""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable, Iterator
from unittest.mock import DEFAULT, MagicMock, patch

import httpx
import pytest
from coa_common.constants import GOVERNED_METRICS_ONTOLOGY_ID, URN_PREFIX, VOCAB_URI
from coa_ontology import dynamo_store
from coa_ontology.catalog import ingest
from coa_ontology.catalog.routers import ontologies
from coa_ontology.stores import neptune_db_graph
from coa_ontology.versions import deploy, publish, registry, router, snapshot, storage
from rdflib import OWL, RDF, Dataset, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS

pytestmark = [
    pytest.mark.unit,
    # rdflib's own SPARQL engine calls its deprecated Dataset accessors when the fake Neptune runs a request.
    pytest.mark.filterwarnings(r"ignore:Dataset\.\w+ is deprecated:DeprecationWarning"),
]

NS = "ns-1"
ONTO = "https://example.com/onto"
OTHER_ONTO = "https://other.example/onto"
EX = "https://example.com/onto#"
RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#"
OWL_NS = "http://www.w3.org/2002/07/owl#"
TTL = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{ONTO}> a owl:Ontology ; rdfs:label "Hospital" .
<{EX}Patient> a owl:Class ; rdfs:label "Patient" .
<{EX}age> a owl:DatatypeProperty ; rdfs:domain <{EX}Patient> .
"""
TTL_V2 = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{ONTO}> a owl:Ontology .
<{EX}Doctor> a owl:Class ; rdfs:label "Doctor" .
"""
TTL_OTHER = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{OTHER_ONTO}> a owl:Ontology .
<{OTHER_ONTO}#Claim> a owl:Class ; rdfs:label "Claim" .
"""
R2RML = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
<{EX}PatientMap> a rr:TriplesMap ;
    <{VOCAB_URI}datasourceId> "ds-1" ;
    rr:logicalTable [ rr:tableName "patient" ] ;
    rr:subjectMap [ rr:class <{EX}Patient> ; rr:template "p/{{id}}" ] .
"""


class _InlineThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class _Neptune:
    """In-memory Neptune: rdflib runs the SPARQL deploy sends, and every request is recorded.

    ``before_update`` runs before an update is applied and may raise (Neptune refused
    it, or it never arrived); ``after_update`` runs after and may raise (the update
    landed but its answer was lost). ``before_query`` may raise to fail a query.
    """

    def __init__(self) -> None:
        self.ds = Dataset()
        self.updates: list[str] = []
        self.queries: list[str] = []
        self.gsp_targets: list[str] = []
        self.before_update: Callable[[str], None] = lambda text: None
        self.after_update: Callable[[str], None] = lambda text: None
        self.before_query: Callable[[str], None] = lambda text: None

    def update(self, text: str) -> None:
        self.updates.append(text)
        self.before_update(text)
        self.ds.update(text)
        self.after_update(text)

    def query(self, text: str) -> dict:
        self.queries.append(text)
        self.before_query(text)
        result = self.ds.query(text)
        if result.type == "ASK":
            return {"head": {}, "boolean": bool(result.askAnswer)}
        rows = [{str(v): {"value": str(row[v])} for v in result.vars if row[v] is not None} for row in result]
        return {"results": {"bindings": rows}}

    def gsp_post(self, graph_uri: str, turtle: str) -> dict:
        self.gsp_targets.append(graph_uri)
        self.load(graph_uri, turtle)
        return {"graph_uri": graph_uri, "status": "ok"}

    def load(self, graph_uri: str, turtle: str) -> None:
        self.ds.graph(URIRef(graph_uri)).parse(data=turtle, format="turtle")

    def triples(self, graph_uri: str) -> set:
        return set(self.ds.graph(URIRef(graph_uri)))

    def moves(self) -> list[str]:
        return [u for u in self.updates if u.startswith("MOVE")]

    def dropped_after_first_move(self) -> bool:
        first = next(i for i, u in enumerate(self.updates) if u.startswith("MOVE"))
        return any(u.startswith("DROP") for u in self.updates[first:])


def _domain() -> str:
    return neptune_db_graph._ontology_graph_uri(ONTO, NS)


def _metrics_graph() -> str:
    return neptune_db_graph._ontology_graph_uri(GOVERNED_METRICS_ONTOLOGY_ID, NS)


def _metric_ttl(name: str, class_iri: str) -> str:
    metric = f"urn:{URN_PREFIX}:{NS}:metric:{name}"
    vocab = f"urn:{URN_PREFIX}:vocab#"
    return (
        f"<{metric}> <{RDFS_NS}subClassOf> <{vocab}GovernedMetric> ; "
        f'<{vocab}governedMetricFor> <{class_iri}> ; <{RDFS_NS}label> "{name}" .\n'
    )


def _publish(ttl: str = TTL, proposal_id: str = "p-1", *, ontology_id: str = ONTO, r2rml: str = R2RML) -> int:
    return publish.publish_version(
        publish.PublishRequest(
            namespace=NS,
            ontology_id=ontology_id,
            ontology_turtle=ttl,
            r2rml_turtle=r2rml,
            source_proposal_id=proposal_id,
            created_by="alice",
        )
    )


@contextlib.contextmanager
def _environment(aws, monkeypatch, *, real_store: bool) -> Iterator[dict]:
    monkeypatch.setattr(deploy, "_STEP_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(deploy, "_AMBIGUOUS_MOVE_WAIT_SECONDS", 0)
    monkeypatch.delenv("NDB_GRAPH_URI_BASE", raising=False)
    monkeypatch.delenv("NDB_GRAPH_URI", raising=False)
    neptune = _Neptune()
    staging: dict[str, str] = {}

    def make_store(namespace, graph_uri):
        staging["uri"] = graph_uri
        return store

    store = MagicMock()
    store.load_turtle.side_effect = lambda **kw: neptune.load(staging["uri"], kw["turtle"])
    vec = MagicMock()
    vec.list_searchable_entity_uris.return_value = []
    vec.store_embeddings_batch.side_effect = lambda items: list(items)
    vec._index_for_namespace = lambda namespace: f"onto-{namespace}"
    bedrock = MagicMock(model_id="m")
    bedrock.embed_texts.side_effect = lambda texts: [[0.5] * 4 for _ in texts]
    events = MagicMock()
    events.put_events.return_value = {"FailedEntryCount": 0}
    embeddings = MagicMock(wraps=ingest._accumulate_embeddings)
    deleted = MagicMock(return_value=True)
    searchable = MagicMock(return_value=True)
    abox_request = MagicMock(return_value=True)
    patches = [
        patch("coa_ontology.abox.build.request_build", abox_request),
        patch("coa_ontology.proposals._generate_schema_sql", return_value=None),
        patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient", return_value=bedrock),
        patch.object(deploy, "Thread", _InlineThread),
        patch.object(deploy, "_sparql_update", side_effect=neptune.update),
        patch.object(deploy, "_sparql_query", side_effect=neptune.query),
        patch.object(deploy, "build_stores", return_value=(MagicMock(), vec)),
        patch.object(deploy, "_accumulate_embeddings", embeddings),
        patch.object(deploy, "wait_for_embeddings_deleted", deleted),
        patch.object(deploy, "wait_for_embeddings_searchable", searchable),
        patch.object(deploy.boto3, "client", return_value=events),
    ]
    if real_store:
        patches += [
            patch.object(neptune_db_graph, "_sparql_update", side_effect=neptune.update),
            patch.object(neptune_db_graph, "_sparql_query", side_effect=neptune.query),
            patch.object(neptune_db_graph, "_gsp_post_turtle", side_effect=neptune.gsp_post),
        ]
    else:
        patches.append(patch.object(deploy, "_GraphOverrideStore", side_effect=make_store))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        version = _publish()
        yield {
            "aws": aws,
            "neptune": neptune,
            "staging": staging,
            "store": store,
            "vec": vec,
            "bedrock": bedrock,
            "events": events,
            "embeddings": embeddings,
            "deleted": deleted,
            "searchable": searchable,
            "abox_request": abox_request,
            "version": version,
        }


@pytest.fixture
def env(versions_aws, monkeypatch):
    with _environment(versions_aws, monkeypatch, real_store=False) as e:
        yield e


@pytest.fixture
def real_env(versions_aws, monkeypatch):
    """Like ``env``, but ``_GraphOverrideStore`` is the real Neptune store over the in-memory Neptune."""
    with _environment(versions_aws, monkeypatch, real_store=True) as e:
        yield e


def _deploy(version: int = 1, **kw) -> dict:
    dep_id = deploy.start_deployment(NS, version, requested_by="bob", **kw)
    return registry.get_deployment(NS, dep_id)


def _old_deployment(deployment_id: str, status: str = registry.DEPLOY_FAILED) -> str:
    """Record a finished earlier deployment; return its staging graph IRI."""
    registry.create_deployment(NS, deployment_id, version=1, requested_by="bob", acknowledged_breaking_metrics=[])
    if status == registry.DEPLOYED:
        registry.acquire_deploy_lock(NS, deployment_id)  # finishing DEPLOYED releases it
    registry.finish_deployment(NS, deployment_id, status=status, partial=False, progress={}, error="")
    return deploy.staging_graph_uri(NS, deployment_id)


def _key(sk: str) -> dict[str, str]:
    return {"PK": f"NAMESPACE#{NS}", "SK": sk}


def _take_over_lock(aws) -> None:
    """Let another deployment take the lock, as it may once this worker's heartbeat went stale."""
    aws.table.update_item(
        Key=_key("DEPLOYLOCK"), UpdateExpression="SET heartbeat_ts = :t", ExpressionAttributeValues={":t": 0}
    )
    registry.acquire_deploy_lock(NS, "intruder")


def _sweep_and_take_over(aws) -> None:
    """Age the running DEPLOY row past the stale threshold, let a reader sweep it, then start another deployment."""
    dep_id = registry.deploy_lock_holder(NS)
    aws.table.update_item(
        Key=_key(f"DEPLOY#{dep_id}"),
        UpdateExpression="SET updated_at = :t",
        ExpressionAttributeValues={":t": "2000-01-01T00:00:00+00:00"},
    )
    assert registry.get_deployment(NS, dep_id)["status"] == registry.DEPLOY_FAILED  # the sweep frees the lock
    registry.acquire_deploy_lock(NS, "intruder")


def _http_error(status: int = 500) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://neptune.example:8182/sparql")
    return httpx.HTTPStatusError(f"Neptune {status}", request=request, response=httpx.Response(status, request=request))


def _once(error: Exception, *, when: Callable[[str], bool]) -> Callable[[str], None]:
    """An update hook that raises ``error`` the first time ``when(text)`` holds."""
    raised: list[bool] = []

    def hook(text: str) -> None:
        if not raised and when(text):
            raised.append(True)
            raise error

    return hook


def _is_move(text: str) -> bool:
    return text.startswith("MOVE")


class _InFlightMove:
    """The first MOVE times out client-side while Neptune keeps running it until :meth:`commit`."""

    def __init__(self, neptune: _Neptune) -> None:
        self.neptune = neptune
        self.held = False
        self.pending: str | None = None

    def hold_first(self, text: str) -> bool:
        """Hold the first MOVE (raising its client-side timeout); return whether ``text`` is a later MOVE."""
        if not _is_move(text):
            return False
        if not self.held:
            self.held, self.pending = True, text
            raise httpx.ReadTimeout("read timeout")
        return True

    def commit(self) -> None:
        if self.pending is not None:
            text, self.pending = self.pending, None
            self.neptune.ds.update(text)


class _Clock:
    """Stands in for the ``time`` module inside ``deploy``: sleeping advances a fake clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _serves_v1(neptune: _Neptune) -> bool:
    domain = neptune.triples(_domain())
    return (URIRef(ONTO), OWL.versionInfo, Literal("v1")) in domain and (
        URIRef(EX + "Patient"),
        RDF.type,
        OWL.Class,
    ) in domain


class TestHappyPath:
    def test_runs_every_step_and_points_active_at_the_version(self, env):
        row = _deploy()
        assert row["status"] == registry.DEPLOYED and row["partial"] is False
        assert row["progress"] == {s: "done" for s in deploy.DEPLOY_STEPS}
        assert registry.get_active(NS)["version"] == 1
        assert registry.deploy_lock_holder(NS) is None
        assert storage.read_latest(NS, "ontology.ttl") == storage.read_version_file(NS, 1, "vkg/ontology.ttl")
        assert storage.read_latest(NS, "mappings.r2rml") == storage.read_version_file(NS, 1, "mappings.r2rml")
        assert dynamo_store.get_ontology_registry(NS, ONTO)["active_version"] == 1

    def test_stages_outside_the_namespace_prefix_then_moves_into_the_domain_graph(self, env):
        _deploy()
        move = next(u for u in env["neptune"].updates if u.startswith("MOVE"))
        assert "/_staging/ns-1/" in move
        assert "/ns-1/https%3A%2F%2Fexample.com%2Fonto" in move
        env["store"].store_classes_batch.assert_called_once()
        classes = env["store"].store_classes_batch.call_args.kwargs["classes"]
        assert [c["class_uri"] for c in classes] == [EX + "Patient"]
        assert classes[0]["is_mapped"] is True  # Patient is the rr:class of a TriplesMap
        assert (URIRef(ONTO), OWL.versionInfo, Literal("v1")) in env["neptune"].triples(_domain())

    def test_replaces_embeddings_for_the_ontology(self, env):
        _deploy()
        env["vec"].delete_embeddings_for_ontology.assert_called_once_with(ONTO, namespace=NS)
        assert env["embeddings"].call_args.kwargs["ontology_id"] == ONTO
        items = env["vec"].store_embeddings_batch.call_args.args[0]
        by_uri = {i["entity_uri"]: i for i in items}
        assert set(by_uri) == {EX + "Patient", EX + "age"}
        assert by_uri[EX + "Patient"]["data_source_id"] == "ds-1"
        assert "data_source_id" not in by_uri[EX + "age"]
        assert env["searchable"].call_args.kwargs["expected_uris"] == [i["entity_uri"] for i in items]

    def test_computes_the_new_vectors_before_deleting_the_old_ones(self, env):
        calls = []
        env["bedrock"].embed_texts.side_effect = lambda texts: calls.append("embed") or [[0.5] * 4 for _ in texts]
        env["vec"].delete_embeddings_for_ontology.side_effect = lambda *a, **kw: calls.append("delete")
        env["vec"].store_embeddings_batch.side_effect = lambda items: calls.append("store") or list(items)
        _deploy()
        assert calls == ["embed", "delete", "store"]

    def test_embedding_sync_progress_is_checkpointed(self, env):
        def wait(**kw):
            kw["on_progress"](1, 2)
            return True

        env["searchable"].side_effect = wait
        row = _deploy()
        assert row["status"] == registry.DEPLOYED
        assert row["embeddings_progress"] == "1/2"

    def test_writes_the_registry_row_and_namespace_meta(self, env):
        _deploy()
        row = dynamo_store.get_ontology_registry(NS, ONTO)
        assert row["graph_uri"] == _domain()
        assert row["ontology_type"] == "induced"
        assert row["source_proposals"] == ["p-1"]
        assert row["title"] == "Hospital"
        assert (row["active_version"], row["version"]) == (1, "v1")
        assert (row["class_count"], row["property_count"], row["embedding_count"]) == (1, 1, 2)
        assert row["axiom_count"] == len(snapshot.parse_turtle(storage.read_version_file(NS, 1, "ontology.ttl")))
        meta = dynamo_store.get_namespace_meta(NS)
        assert meta["backend"] == "opensearch_neptune"
        assert meta["embedding_model_id"] == "m"
        assert meta["embedding_dimensions"] == 1024
        assert meta["embedding_index"] == row["embedding_index"] == f"onto-{NS}"
        assert meta["graph_uri_prefix"] == _domain().rsplit("/", 1)[0]

    def test_keeps_the_registry_title_over_the_ontology_label(self, env):
        dynamo_store.put_ontology_registry(NS, ONTO, {"title": "Ward Records", "ontology_type": "induced"})
        _deploy()
        assert env["store"].create_ontology.call_args.args[0]["title"] == "Ward Records"
        assert dynamo_store.get_ontology_registry(NS, ONTO)["title"] == "Ward Records"

    def test_first_deploy_titles_the_registry_row_with_the_accepts_title(self, env):
        dynamo_store._get_table().update_item(
            Key=_key("VERSION#000001"),
            UpdateExpression="SET title = :t",
            ExpressionAttributeValues={":t": "Plant"},
        )
        _deploy()
        assert env["store"].create_ontology.call_args.args[0]["title"] == "Plant"
        assert dynamo_store.get_ontology_registry(NS, ONTO)["title"] == "Plant"

    def test_an_existing_registry_title_wins_over_the_accepts_title(self, env):
        dynamo_store.put_ontology_registry(NS, ONTO, {"title": "Ward Records", "ontology_type": "induced"})
        dynamo_store._get_table().update_item(
            Key=_key("VERSION#000001"),
            UpdateExpression="SET title = :t",
            ExpressionAttributeValues={":t": "Plant"},
        )
        _deploy()
        assert dynamo_store.get_ontology_registry(NS, ONTO)["title"] == "Ward Records"

    def test_emits_ontology_deployed(self, env):
        row = _deploy()
        entry = env["events"].put_events.call_args.kwargs["Entries"][0]
        assert entry["DetailType"] == "ontology.deployed"
        assert entry["Source"].endswith(".ontology")
        assert '"version": "v1"' in entry["Detail"]
        assert json.loads(entry["Detail"]) == {
            "namespace": NS,
            "version": "v1",
            "deploymentId": row["deployment_id"],
            "s3Bucket": env["aws"].bucket,
            "s3Prefix": f"ontologies/{NS}/latest/",
        }

    def test_drops_the_staging_graphs_of_earlier_deployments_without_scanning_neptune(self, env):
        leftover = _old_deployment("20260101T000000Z-dead")
        other_ns = deploy.staging_graph_uri("ns-10", "20260101T000000Z-live")
        for graph_uri in (leftover, other_ns):
            env["neptune"].load(graph_uri, f"<{EX}x> <{EX}y> <{EX}z> .")
        assert _deploy()["status"] == registry.DEPLOYED
        assert not env["neptune"].triples(leftover)
        assert env["neptune"].triples(other_ns)
        assert not any("GRAPH ?g" in q for q in env["neptune"].queries)

    def test_only_the_most_recent_deployments_are_cleaned_up(self, env):
        oldest, newest = (_old_deployment(f"20260101T0000{i:02d}Z-dead") for i in (0, 19))
        for i in range(1, 19):
            _old_deployment(f"20260101T0000{i:02d}Z-dead")
        for graph_uri in (oldest, newest):
            env["neptune"].load(graph_uri, f"<{EX}x> <{EX}y> <{EX}z> .")
        assert _deploy()["status"] == registry.DEPLOYED
        assert not env["neptune"].triples(newest)
        assert env["neptune"].triples(oldest)  # beyond the 20 newest rows; namespace deletion drops it

    def test_a_failed_cleanup_does_not_fail_the_deployment(self, env, monkeypatch):
        monkeypatch.setattr(registry, "list_deployments", MagicMock(side_effect=RuntimeError("DynamoDB 500")))
        assert _deploy()["status"] == registry.DEPLOYED


class TestAboxBuildRequest:
    def test_a_deployed_version_requests_a_domain_abox_build(self, env):
        seen: list[tuple[int | None, str | None]] = []
        env["abox_request"].side_effect = lambda *_a: seen.append(
            (registry.get_active(NS)["version"], registry.deploy_lock_holder(NS))
        )
        assert _deploy()["status"] == registry.DEPLOYED
        env["abox_request"].assert_called_once_with(NS, "deploy v1")
        assert seen == [(1, None)]  # asked for only once the deployment is recorded and its lock released

    def test_a_failing_request_does_not_fail_the_deployment(self, env, caplog):
        env["abox_request"].side_effect = RuntimeError("DynamoDB 500")
        row = _deploy()
        assert row["status"] == registry.DEPLOYED and row["partial"] is False
        assert registry.get_active(NS)["version"] == 1
        assert registry.deploy_lock_holder(NS) is None
        assert any("domain ABox build" in r.getMessage() for r in caplog.records)

    def test_a_failed_request_is_retried_once_after_a_short_backoff(self, env, monkeypatch, caplog):
        clock = _Clock()
        monkeypatch.setattr(deploy, "time", clock)
        monkeypatch.setattr(deploy, "_STEP_BACKOFF_SECONDS", 2.0)
        env["abox_request"].side_effect = [RuntimeError("DynamoDB 500"), True]
        assert _deploy()["status"] == registry.DEPLOYED
        assert env["abox_request"].call_count == 2
        assert [s for s in clock.sleeps if s] == [pytest.approx(2.0, abs=1.0)]  # the step backoff, with jitter
        assert not any("could not request the domain ABox build" in r.getMessage() for r in caplog.records)

    def test_a_request_that_fails_again_is_only_logged(self, env, caplog):
        env["abox_request"].side_effect = RuntimeError("DynamoDB 500")
        row = _deploy()
        assert row["status"] == registry.DEPLOYED and row["partial"] is False
        assert env["abox_request"].call_count == 2
        assert any("could not request the domain ABox build" in r.getMessage() for r in caplog.records)

    def test_a_deployment_that_failed_before_the_swap_requests_no_build(self, env):
        env["store"].load_turtle.side_effect = RuntimeError("GSP 503")
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is False
        env["abox_request"].assert_not_called()

    def test_a_partial_deployment_requests_a_build_for_the_version_that_swapped_in(self, env):
        env["embeddings"].return_value = {"status": "error", "error": "AOSS 500"}
        seen: list[tuple[int | None, str]] = []

        def record_state(*_args) -> None:
            holder = registry.deploy_lock_holder(NS)  # the failing deployment, until its worker releases the lock
            seen.append((registry.get_active(NS)["version"], registry.get_deployment(NS, holder)["status"]))

        env["abox_request"].side_effect = record_state
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is True
        env["abox_request"].assert_called_once_with(NS, "deploy v1")
        assert seen == [(1, registry.DEPLOY_FAILED)]  # ACTIVE moved with the swap, and the failure is recorded

    def test_a_failing_request_does_not_change_the_partial_deployment_outcome(self, env):
        env["embeddings"].return_value = {"status": "error", "error": "AOSS 500"}
        env["abox_request"].side_effect = RuntimeError("DynamoDB 500")
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is True
        assert "AOSS 500" in row["error"]
        assert registry.get_active(NS)["version"] == 1
        assert registry.deploy_lock_holder(NS) is None


class TestLoadTarget:
    def test_the_mapping_is_parsed_once_for_the_markers_and_the_documents(self, env, monkeypatch):
        # The version detail view and the deploy preflight load the target too, so a wide mapping is parsed once.
        mapping = storage.require_version_file(NS, env["version"], "mappings.r2rml")
        parsed: list[str] = []
        real_parse = Graph.parse

        def parse(self, *args, **kwargs):
            parsed.append(kwargs.get("data"))
            return real_parse(self, *args, **kwargs)

        monkeypatch.setattr(Graph, "parse", parse)
        target = deploy._load_target(NS, env["version"])
        assert parsed.count(mapping) == 1
        assert target.class_to_datasource == {f"{EX}Patient": "ds-1"}
        assert target.physical_names.tables == {f"{EX}Patient": {"patient"}}
        assert target.mapped_predicates == set()


class TestRealStore:
    def test_every_stage_write_targets_the_staging_graph(self, real_env):
        neptune = real_env["neptune"]
        row = _deploy()
        assert row["status"] == registry.DEPLOYED
        staging = deploy.staging_graph_uri(NS, row["deployment_id"])
        first_move = next(i for i, u in enumerate(neptune.updates) if u.startswith("MOVE"))
        for update in neptune.updates[:first_move]:
            assert set(re.findall(r"(?:GRAPH|WITH) <([^>]+)>", update)) == {staging}, update
        assert neptune.gsp_targets == [staging]
        assert not neptune.triples(staging)

    def test_the_domain_graph_serves_the_projection_and_the_snapshot(self, real_env):
        _deploy()
        domain = real_env["neptune"].triples(_domain())
        patient = URIRef(EX + "Patient")
        assert (patient, URIRef(VOCAB_URI + "isMapped"), Literal(True)) in domain
        assert (patient, URIRef(neptune_db_graph.WB + "definedBy"), URIRef(ONTO)) in domain
        assert (URIRef(ONTO), OWL.versionInfo, Literal("v1")) in domain
        assert (URIRef(ONTO), DCTERMS.title, Literal("Hospital")) in domain
        assert not any(p == URIRef(neptune_db_graph.WB + "deploymentId") for _, p, _ in domain)  # marker removed

    def test_a_redeploy_replaces_the_domain_graph(self, real_env):
        assert _deploy()["status"] == registry.DEPLOYED
        assert _publish(TTL_V2, "p-2", r2rml="") == 2
        assert _deploy(2)["status"] == registry.DEPLOYED
        domain = real_env["neptune"].triples(_domain())
        assert (URIRef(ONTO), OWL.versionInfo, Literal("v2")) in domain
        assert (URIRef(ONTO), OWL.versionInfo, Literal("v1")) not in domain
        assert len([t for t in domain if t[1] == DCTERMS.created]) == 1


class TestSwap:
    def test_a_move_that_landed_without_an_answer_is_not_sent_again(self, env):
        env["neptune"].after_update = _once(httpx.ReadTimeout("read timeout"), when=_is_move)
        row = _deploy()
        assert row["status"] == registry.DEPLOYED
        assert len(env["neptune"].moves()) == 1
        assert registry.get_active(NS)["version"] == 1
        domain = env["neptune"].triples(_domain())
        assert (URIRef(ONTO), OWL.versionInfo, Literal("v1")) in domain
        assert not any(p == URIRef(neptune_db_graph.WB + "deploymentId") for _, p, _ in domain)

    def test_a_move_that_never_arrived_is_sent_again(self, env):
        env["neptune"].before_update = _once(httpx.ConnectTimeout("connect timeout"), when=_is_move)
        row = _deploy()
        assert row["status"] == registry.DEPLOYED
        assert len(env["neptune"].moves()) == 2
        assert (URIRef(ONTO), OWL.versionInfo, Literal("v1")) in env["neptune"].triples(_domain())

    def test_a_move_neptune_refused_did_not_happen(self, env):
        def refuse(text):
            if _is_move(text):
                raise _http_error()

        env["neptune"].before_update = refuse
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is False
        assert len(env["neptune"].moves()) == deploy._STEP_MAX_ATTEMPTS
        assert registry.get_active(NS) is None
        assert env["neptune"].updates[-1].startswith("DROP SILENT GRAPH")
        assert not env["neptune"].triples(deploy.staging_graph_uri(NS, row["deployment_id"]))

    def test_a_vanished_staging_graph_is_never_moved_over_the_domain_graph(self, env):
        env["neptune"].load(_domain(), f"<{EX}Served> a <{OWL_NS}Class> .")

        def vanish(text):
            if _is_move(text) and not env["neptune"].moves()[1:]:
                staging = text.split("<", 1)[1].split(">", 1)[0]
                env["neptune"].ds.update(f"DROP SILENT GRAPH <{staging}>")
                raise httpx.ReadTimeout("read timeout")

        env["neptune"].before_update = vanish
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is True
        assert "lacks its marker" in row["error"]
        assert len(env["neptune"].moves()) == 1
        assert env["neptune"].triples(_domain()) == {(URIRef(EX + "Served"), RDF.type, OWL.Class)}
        assert registry.get_active(NS) is None

    def test_an_in_flight_move_that_commits_before_the_retry_is_not_duplicated(self, env):
        inflight = _InFlightMove(env["neptune"])
        env["neptune"].before_update = inflight.hold_first
        env["neptune"].before_query = lambda text: inflight.commit()
        row = _deploy()
        assert row["status"] == registry.DEPLOYED
        assert len(env["neptune"].moves()) == 1
        assert _serves_v1(env["neptune"])

    def test_a_duplicate_move_refused_while_the_first_runs_keeps_the_doubt(self, env):
        inflight = _InFlightMove(env["neptune"])

        def refuse_duplicate(text):
            if inflight.hold_first(text) and inflight.pending is not None:
                inflight.commit()  # the first MOVE commits; the duplicate was refused meanwhile
                raise _http_error(500)  # ConcurrentModificationException

        env["neptune"].before_update = refuse_duplicate
        row = _deploy()
        assert row["status"] == registry.DEPLOYED
        assert _serves_v1(env["neptune"])
        assert registry.get_active(NS)["version"] == 1

    def test_a_duplicate_move_over_the_emptied_staging_graph_is_not_reported_deployed(self, env):
        inflight = _InFlightMove(env["neptune"])

        def land_first_then_run_duplicate(text):
            if inflight.hold_first(text):
                inflight.commit()  # the duplicate then moves the now-missing staging graph: rdflib empties the domain

        env["neptune"].before_update = land_first_then_run_duplicate
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is True
        assert not env["neptune"].triples(_domain())
        assert registry.get_active(NS) is None

    def test_a_move_in_doubt_is_outwaited_with_the_lock_kept_alive(self, env, monkeypatch):
        clock = _Clock()
        monkeypatch.setattr(deploy, "time", clock)
        monkeypatch.setattr(deploy, "_AMBIGUOUS_MOVE_WAIT_SECONDS", 65.0)
        real = registry.checkpoint_deployment
        beats: list[float] = []
        marker_checks: list[float] = []

        def checkpoint(namespace, deployment_id, **fields):
            beats.append(clock.now)
            return real(namespace, deployment_id, **fields)

        monkeypatch.setattr(registry, "checkpoint_deployment", checkpoint)
        env["neptune"].after_update = _once(httpx.ReadTimeout("read timeout"), when=_is_move)
        env["neptune"].before_query = lambda text: marker_checks.append(clock.now) if "deploymentId" in text else None
        row = _deploy()
        assert row["status"] == registry.DEPLOYED
        assert [s for s in clock.sleeps if s] == [30, 30, 5]
        assert {30, 60, 65} <= set(beats)
        assert min(marker_checks) >= 65

    def test_no_wait_without_doubt(self, env, monkeypatch):
        clock = _Clock()
        monkeypatch.setattr(deploy, "time", clock)
        monkeypatch.setattr(deploy, "_AMBIGUOUS_MOVE_WAIT_SECONDS", 65.0)
        env["neptune"].before_update = _once(_http_error(), when=_is_move)  # refused, so certainly not moved
        assert _deploy()["status"] == registry.DEPLOYED
        assert not [s for s in clock.sleeps if s]


class TestFailures:
    def test_failure_before_swap_leaves_serving_untouched(self, env):
        env["store"].load_turtle.side_effect = RuntimeError("GSP 503")
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is False
        assert "GSP 503" in row["error"]
        assert registry.get_active(NS) is None
        assert not any(u.startswith("MOVE") for u in env["neptune"].updates)
        assert env["neptune"].updates[-1].startswith("DROP SILENT GRAPH")
        assert registry.deploy_lock_holder(NS) is None

    def test_failure_after_swap_is_partial_and_redeploy_converges(self, env):
        env["embeddings"].return_value = {"status": "error", "error": "AOSS 500"}
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is True
        assert registry.get_active(NS)["version"] == 1
        assert not env["neptune"].dropped_after_first_move()  # the staging graph is not dropped after the swap
        env["embeddings"].return_value = DEFAULT  # the real embedding run again
        assert _deploy()["status"] == registry.DEPLOYED

    @pytest.mark.parametrize("retried", [2, 1], ids=["redeploy", "rollback"])
    def test_a_later_failure_before_the_swap_still_reports_the_half_swapped_serving(self, env, monkeypatch, retried):
        ids = iter(range(10, 99))  # these deployments start within one second
        monkeypatch.setattr(registry, "new_deployment_id", lambda: f"20261022T0000{next(ids)}Z-abcdef00")
        assert _deploy()["status"] == registry.DEPLOYED
        assert _publish(TTL_V2, "p-2", r2rml="") == 2
        env["vec"].store_embeddings_batch.side_effect = RuntimeError("AOSS 500")
        partial = _deploy(2)
        assert partial["partial"] is True
        env["store"].load_turtle.side_effect = RuntimeError("GSP 503")
        assert _deploy(retried)["partial"] is False  # this attempt left serving as the partial one did
        listed = router.list_versions(namespace=NS, max_results=None, next_token=None)
        assert listed["latestDeployment"]["version"] == retried and listed["latestDeployment"]["partial"] is False
        assert listed["servingDeployment"]["deploymentId"] == partial["deployment_id"]
        assert listed["servingDeployment"]["partial"] is True

    def test_a_bedrock_failure_leaves_the_old_embeddings_untouched(self, env):
        env["bedrock"].embed_texts.side_effect = RuntimeError("ThrottlingException")
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is True
        assert "ThrottlingException" in row["error"]
        env["vec"].delete_embeddings_for_ontology.assert_not_called()
        env["vec"].store_embeddings_batch.assert_not_called()

    def test_a_failed_embedding_write_reuses_the_computed_vectors(self, env):
        writes = []

        def store(items):
            writes.append(len(items))
            if len(writes) == 1:
                raise RuntimeError("AOSS 500")
            return list(items)

        env["vec"].store_embeddings_batch.side_effect = store
        assert _deploy()["status"] == registry.DEPLOYED
        assert env["bedrock"].embed_texts.call_count == 1
        assert writes == [2, 2]

    def test_staging_without_the_version_stamp_is_not_swapped_in(self, env):
        def load_without_version_info(**kw):
            kept = "\n".join(line for line in kw["turtle"].splitlines() if "versionInfo" not in line)
            env["neptune"].load(env["staging"]["uri"], kept)

        env["store"].load_turtle.side_effect = load_without_version_info
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is False
        assert "versionInfo" in row["error"]
        assert not env["neptune"].moves()

    def test_unpublished_version_is_a_conflict(self, env):
        with pytest.raises(deploy.DeployConflictError):
            deploy.start_deployment(NS, 99, requested_by="bob")

    def test_a_version_that_is_not_published_is_a_conflict_even_with_a_snapshot(self, env):
        env["aws"].table.update_item(
            Key=_key("VERSION#000001"),
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": registry.PUBLISH_FAILED},
        )
        assert storage.read_manifest(NS, 1) is not None
        with pytest.raises(deploy.DeployConflictError):
            deploy.start_deployment(NS, 1, requested_by="bob")
        assert registry.deploy_lock_holder(NS) is None

    @pytest.mark.parametrize(
        ("name", "body"),
        [(storage.MANIFEST, None), (storage.MANIFEST, b"{"), ("ontology.ttl", None)],
        ids=["no-manifest", "bad-manifest", "no-ontology"],
    )
    def test_an_incomplete_snapshot_is_a_conflict(self, env, name, body):
        aws = env["aws"]
        key = storage.version_key(NS, 1, name)
        if body is None:
            aws.s3.delete_object(Bucket=aws.bucket, Key=key)
        else:
            aws.s3.put_object(Bucket=aws.bucket, Key=key, Body=body)
        with pytest.raises(deploy.DeployConflictError):
            deploy.start_deployment(NS, 1, requested_by="bob")
        assert registry.deploy_lock_holder(NS) is None

    @pytest.mark.parametrize("damage", ["no-ontology-id", "non-string-ontology-id", "invalid-turtle"])
    def test_a_damaged_snapshot_is_a_conflict_not_a_crash(self, env, damage):
        aws = env["aws"]
        manifest = storage.read_manifest(NS, 1)
        if damage == "invalid-turtle":
            name, body = "ontology.ttl", b"this is not turtle"
        else:
            if damage == "no-ontology-id":
                del manifest["ontologyId"]
            else:
                manifest["ontologyId"] = 5
            name, body = storage.MANIFEST, json.dumps(manifest).encode()
        aws.s3.put_object(Bucket=aws.bucket, Key=storage.version_key(NS, 1, name), Body=body)
        with pytest.raises(deploy.DeployConflictError):
            deploy.start_deployment(NS, 1, requested_by="bob")
        assert registry.deploy_lock_holder(NS) is None
        with pytest.raises(deploy.DeployConflictError):
            deploy.preview_breaking_metrics(NS, 1)

    def test_running_deployment_blocks_another(self, env):
        registry.acquire_deploy_lock(NS, "other")
        with pytest.raises(registry.DeployLockHeldError):
            deploy.start_deployment(NS, 1, requested_by="bob")

    def test_a_version_of_a_deleted_ontology_is_a_conflict(self, env):
        assert _deploy()["status"] == registry.DEPLOYED
        registry.reset_active(NS, ontology_id=ONTO)
        with pytest.raises(deploy.DeployConflictError) as exc:
            deploy.start_deployment(NS, 1, requested_by="bob")
        assert str(exc.value) == "Version v1 belongs to a deleted ontology; accept a proposal to publish it again."
        assert registry.deploy_lock_holder(NS) is None
        assert registry.get_active(NS)["version"] is None

    def test_a_version_published_after_the_reset_deploys(self, env):
        # The auto-deploy after an accept targets exactly such a version.
        assert _deploy()["status"] == registry.DEPLOYED
        registry.reset_active(NS, ontology_id=ONTO)
        other = _publish(TTL_OTHER, "p-2", ontology_id=OTHER_ONTO, r2rml="")
        assert _deploy(other)["status"] == registry.DEPLOYED
        with pytest.raises(deploy.DeployConflictError, match="belongs to a deleted ontology"):
            deploy.start_deployment(NS, 1, requested_by="bob")  # the floor survived the deploy
        assert registry.get_active(NS)["version"] == other

    def test_a_rollback_within_the_lineage_above_the_floor_deploys(self, env):
        assert _deploy()["status"] == registry.DEPLOYED
        registry.reset_active(NS, ontology_id=ONTO)
        v2 = _publish(TTL_OTHER, "p-2", ontology_id=OTHER_ONTO, r2rml="")
        v3 = _publish(TTL_OTHER, "p-3", ontology_id=OTHER_ONTO, r2rml="")
        assert _deploy(v3)["status"] == registry.DEPLOYED
        assert _deploy(v2)["status"] == registry.DEPLOYED
        active = registry.get_active(NS)
        assert active["version"] == v2 and active["reset_after_version"] == 1

    def test_a_version_of_another_ontology_than_the_served_one_is_a_conflict(self, env, versions_aws):
        assert _deploy()["status"] == registry.DEPLOYED
        registry.reset_active(NS)
        other = _publish(TTL_OTHER, "p-2", ontology_id=OTHER_ONTO, r2rml="")
        assert _deploy(other)["status"] == registry.DEPLOYED
        # An ACTIVE row written before deploys kept the reset floor has none.
        versions_aws.table.update_item(Key=_key("ACTIVE"), UpdateExpression="REMOVE reset_after_version")
        with pytest.raises(deploy.DeployConflictError, match="two domain ontologies"):
            deploy.start_deployment(NS, 1, requested_by="bob")
        assert registry.deploy_lock_holder(NS) is None
        assert registry.get_active(NS)["version"] == other

    def test_an_ontology_being_deleted_is_a_conflict(self, env):
        dynamo_store.put_ontology_registry(NS, ONTO, {"status": "deleting"})
        with pytest.raises(deploy.DeployConflictError, match="being deleted"):
            deploy.start_deployment(NS, 1, requested_by="bob")
        assert registry.deploy_lock_holder(NS) is None


class TestRetries:
    def test_the_worker_retries_reading_the_snapshot(self, env, monkeypatch):
        real = storage.read_manifest
        calls = []

        def flaky(namespace, version):
            calls.append(version)
            if len(calls) == 2:  # the worker's first read; start_deployment made the first
                raise RuntimeError("S3 SlowDown")
            return real(namespace, version)

        monkeypatch.setattr(storage, "read_manifest", flaky)
        assert _deploy()["status"] == registry.DEPLOYED

    @pytest.mark.parametrize("landed", [False, True])
    def test_a_transient_error_recording_success_is_retried(self, env, monkeypatch, landed):
        real = registry.finish_deployment
        statuses = []

        def flaky(namespace, deployment_id, **kw):
            statuses.append(kw["status"])
            if statuses == [registry.DEPLOYED]:
                if landed:
                    real(namespace, deployment_id, **kw)
                raise RuntimeError("TransactionConflict")
            return real(namespace, deployment_id, **kw)

        monkeypatch.setattr(registry, "finish_deployment", flaky)
        row = _deploy()
        assert row["status"] == registry.DEPLOYED
        assert registry.DEPLOY_FAILED not in statuses
        assert registry.deploy_lock_holder(NS) is None

    def test_the_lock_release_is_retried(self, env, monkeypatch):
        real = registry.release_deploy_lock
        failures = []

        def flaky(namespace, deployment_id):
            if len(failures) < 2:
                failures.append(True)
                raise RuntimeError("DynamoDB 500")
            return real(namespace, deployment_id)

        monkeypatch.setattr(registry, "release_deploy_lock", flaky)
        env["store"].load_turtle.side_effect = RuntimeError("GSP 503")  # DEPLOY_FAILED leaves the release to finally
        assert _deploy()["status"] == registry.DEPLOY_FAILED
        assert registry.deploy_lock_holder(NS) is None

    def test_a_failing_lock_release_never_hides_the_original_error(self, env, monkeypatch):
        monkeypatch.setattr(registry, "release_deploy_lock", MagicMock(side_effect=RuntimeError("DynamoDB 500")))
        env["neptune"].load(_domain(), f"<{EX}Invoice> a <{OWL_NS}Class> .")
        env["neptune"].load(_metrics_graph(), _metric_ttl("revenue", EX + "Invoice"))
        with pytest.raises(deploy.BreakingMetricsError):
            deploy.start_deployment(NS, 1, requested_by="bob")
        assert registry.release_deploy_lock.call_count == deploy._STEP_MAX_ATTEMPTS

    def test_recording_a_failure_is_retried(self, env, monkeypatch):
        real = registry.finish_deployment
        failed = []

        def flaky(namespace, deployment_id, **kw):
            if kw["status"] == registry.DEPLOY_FAILED and not failed:
                failed.append(True)
                raise RuntimeError("DynamoDB 500")
            return real(namespace, deployment_id, **kw)

        monkeypatch.setattr(registry, "finish_deployment", flaky)
        env["store"].load_turtle.side_effect = RuntimeError("GSP 503")
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED and "GSP 503" in row["error"]

    def test_a_failure_that_cannot_be_recorded_is_logged_not_raised(self, env, monkeypatch):
        monkeypatch.setattr(registry, "finish_deployment", MagicMock(side_effect=RuntimeError("DynamoDB 500")))
        env["store"].load_turtle.side_effect = RuntimeError("GSP 503")
        dep_id = deploy.start_deployment(NS, 1, requested_by="bob")  # the inline worker must not raise
        assert registry.finish_deployment.call_count == deploy._STEP_MAX_ATTEMPTS
        assert registry.deploy_lock_holder(NS) is None
        assert registry.get_deployment(NS, dep_id)["status"] == registry.DEPLOYING  # left to the stale sweep

    def test_a_step_retry_first_checks_the_lock(self, env):
        def fail_after_takeover(**kw):
            _take_over_lock(env["aws"])
            raise RuntimeError("GSP 503")

        env["store"].load_turtle.side_effect = fail_after_takeover
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED
        assert "DeployLockLostError" in row["error"]
        assert env["store"].load_turtle.call_count == 1


class TestLockLoss:
    def test_lock_taken_over_between_stage_and_swap_aborts_before_the_graph_is_touched(self, env):
        assert _deploy()["status"] == registry.DEPLOYED
        assert _publish(TTL_V2, "p-2", r2rml="") == 2
        env["store"].update_ontology.side_effect = lambda *a, **kw: _take_over_lock(env["aws"])
        moves_before = len(env["neptune"].moves())
        row = _deploy(2)
        assert len(env["neptune"].moves()) == moves_before
        assert registry.get_active(NS)["version"] == 1
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is False
        assert "DeployLockLostError" in row["error"]
        assert env["neptune"].updates[-1].startswith("DROP SILENT GRAPH")
        assert (URIRef(ONTO), OWL.versionInfo, Literal("v1")) in env["neptune"].triples(_domain())
        assert registry.deploy_lock_holder(NS) == "intruder"

    @pytest.mark.parametrize(
        ("noticed_after", "moved", "embeddings_replaced", "registry_written", "latest_written"),
        [
            ("stage", False, False, False, False),
            ("swap", True, False, False, False),
            ("embeddings", True, True, False, False),
            ("registry", True, True, True, False),
            ("latest", True, True, True, True),
        ],
    )
    def test_a_lock_loss_the_progress_checkpoint_missed_still_stops_the_next_step(
        self, env, monkeypatch, noticed_after, moved, embeddings_replaced, registry_written, latest_written
    ):
        real = registry.checkpoint_deployment
        taken = []

        def checkpoint(namespace, deployment_id, **fields):
            if not taken and fields.get("progress", {}).get(noticed_after) == "done":
                taken.append(True)
                _take_over_lock(env["aws"])
                raise RuntimeError("ProvisionedThroughputExceeded")  # the best-effort checkpoint is skipped
            return real(namespace, deployment_id, **fields)

        monkeypatch.setattr(registry, "checkpoint_deployment", checkpoint)
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is moved
        assert "DeployLockLostError" in row["error"]
        assert bool(env["neptune"].moves()) is moved
        assert (registry.get_active(NS) is not None) is moved
        assert env["bedrock"].embed_texts.called is embeddings_replaced
        assert env["vec"].delete_embeddings_for_ontology.called is embeddings_replaced
        assert (dynamo_store.get_ontology_registry(NS, ONTO) is not None) is registry_written
        assert (storage.read_latest(NS, "ontology.ttl") is not None) is latest_written
        env["events"].put_events.assert_not_called()
        assert registry.deploy_lock_holder(NS) == "intruder"

    def test_lock_lost_during_the_move_leaves_active_alone(self, env):
        env["neptune"].after_update = lambda text: _take_over_lock(env["aws"]) if _is_move(text) else None
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is True
        assert registry.get_active(NS) is None
        assert not env["neptune"].dropped_after_first_move()
        assert storage.read_latest(NS, "ontology.ttl") is None
        env["events"].put_events.assert_not_called()

    def test_lock_lost_while_computing_embeddings_leaves_the_old_ones_alone(self, env):
        def embed(texts):
            _take_over_lock(env["aws"])
            return [[0.5] * 4 for _ in texts]

        env["bedrock"].embed_texts.side_effect = embed
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is True
        assert "DeployLockLostError" in row["error"]
        env["vec"].delete_embeddings_for_ontology.assert_not_called()
        env["vec"].store_embeddings_batch.assert_not_called()
        assert dynamo_store.get_ontology_registry(NS, ONTO) is None

    def test_lock_lost_while_waiting_for_the_deletion_writes_nothing(self, env):
        def wait(**kw):
            _take_over_lock(env["aws"])
            return True

        env["deleted"].side_effect = wait
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED
        assert "DeployLockLostError" in row["error"]
        env["vec"].store_embeddings_batch.assert_not_called()

    def test_lock_lost_during_the_embedding_sync_stops_the_deployment(self, env):
        def wait(**kw):
            _take_over_lock(env["aws"])
            with contextlib.suppress(Exception):  # wait_for_embeddings_searchable swallows callback errors
                kw["on_progress"](1, 2)
            return True

        env["searchable"].side_effect = wait
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED and row["partial"] is True
        assert "DeployLockLostError" in row["error"]
        assert row["progress"] == {"stage": "done", "swap": "done"}  # the step stopped, it did not finish
        assert env["embeddings"].call_count == 1  # not retried
        assert not env["neptune"].dropped_after_first_move()
        assert dynamo_store.get_ontology_registry(NS, ONTO) is None
        assert storage.read_latest(NS, "ontology.ttl") is None
        env["events"].put_events.assert_not_called()
        assert registry.deploy_lock_holder(NS) == "intruder"

    def test_a_row_already_swept_keeps_the_sweep_verdict(self, env):
        env["store"].update_ontology.side_effect = lambda *a, **kw: _sweep_and_take_over(env["aws"])
        row = _deploy()
        assert row["status"] == registry.DEPLOY_FAILED
        assert "did not complete" in row["error"]
        assert not env["neptune"].moves()
        assert registry.get_active(NS) is None
        assert registry.deploy_lock_holder(NS) == "intruder"


class TestBreakingMetrics:
    @pytest.fixture(autouse=True)
    def _served(self, env):
        """Serve Patient and Invoice now, with metrics on Invoice and Claim; v1 has only Patient."""
        env["neptune"].load(_domain(), f"<{EX}Patient> a <{OWL_NS}Class> . <{EX}Invoice> a <{OWL_NS}Class> .")
        env["neptune"].load(_domain(), f"<{EX}Claim> a <{RDFS_NS}Class> .")
        metrics = _metric_ttl("revenue", EX + "Invoice") + _metric_ttl("claims:open", EX + "Claim")
        metrics += _metric_ttl("visits", EX + "Patient")
        env["neptune"].load(_metrics_graph(), metrics)

    EXPECTED = [
        {"metricId": "claims:open", "name": "claims:open", "missingClassIri": EX + "Claim"},
        {"metricId": "revenue", "name": "revenue", "missingClassIri": EX + "Invoice"},
    ]

    def test_metric_linked_to_a_removed_class_blocks_until_acknowledged(self, env):
        with pytest.raises(deploy.BreakingMetricsError) as exc:
            deploy.start_deployment(NS, 1, requested_by="bob")
        assert exc.value.metrics == self.EXPECTED
        assert registry.deploy_lock_holder(NS) is None
        assert deploy.preview_breaking_metrics(NS, 1) == self.EXPECTED
        row = _deploy(acknowledged_breaking_metrics=["revenue", "claims:open"])
        assert row["status"] == registry.DEPLOYED
        assert row["acknowledged_breaking_metrics"] == ["claims:open", "revenue"]

    def test_a_partial_acknowledgement_still_blocks(self, env):
        with pytest.raises(deploy.BreakingMetricsError) as exc:
            deploy.start_deployment(NS, 1, requested_by="bob", acknowledged_breaking_metrics=["revenue"])
        assert exc.value.metrics == self.EXPECTED
        assert registry.deploy_lock_holder(NS) is None
        assert not env["neptune"].moves()


class TestNoBreakingMetrics:
    def test_no_removed_classes_means_no_metric_query(self, env):
        env["neptune"].load(_domain(), f"<{EX}Patient> a <{OWL_NS}Class> .")
        assert deploy.preview_breaking_metrics(NS, 1) == []
        assert env["neptune"].queries  # the live classes were read
        assert not any("governedMetricFor" in q for q in env["neptune"].queries)

    def test_a_manifest_the_caller_already_read_is_not_read_again(self, env):
        manifest = storage.read_manifest(NS, 1)
        with patch.object(storage, "read_manifest") as read:
            assert deploy.preview_breaking_metrics(NS, 1, manifest=manifest) == []
        read.assert_not_called()


class TestDropStagingGraphs:
    def test_drops_the_staging_graph_of_every_listed_deployment_except_keep(self, env):
        a, b, c = (_old_deployment(f"20260101T00000{i}Z-x") for i in range(3))
        graphs = [a, b, c, deploy.staging_graph_uri("ns-10", "20260101T000000Z-x"), _domain()]
        for graph_uri in graphs:
            env["neptune"].load(graph_uri, f"<{EX}x> <{EX}y> <{EX}z> .")
        assert deploy.drop_staging_graphs(NS, keep=b) == [c, a]
        assert [bool(env["neptune"].triples(g)) for g in graphs] == [False, True, False, True, True]
        assert deploy.drop_staging_graphs(NS, limit=2) == [c, b]
        assert not any("GRAPH ?g" in q for q in env["neptune"].queries)

    def test_skips_deployments_whose_staging_graph_was_moved(self, env):
        failed = _old_deployment("20260101T000000Z-a")
        deployed = _old_deployment("20260101T000001Z-b", status=registry.DEPLOYED)
        assert deploy.drop_staging_graphs(NS) == [failed]
        assert not any(deployed in u for u in env["neptune"].updates)

    def test_a_failed_drop_skips_that_graph_only(self, env):
        a, b = (_old_deployment(f"20260101T00000{i}Z-x") for i in range(2))
        for graph_uri in (a, b):
            env["neptune"].load(graph_uri, f"<{EX}x> <{EX}y> <{EX}z> .")
        env["neptune"].before_update = _once(_http_error(), when=lambda text: b in text)
        assert deploy.drop_staging_graphs(NS) == [a]
        assert env["neptune"].triples(b) and not env["neptune"].triples(a)


class TestNamespaceDelete:
    def test_drops_the_domain_graph_of_a_first_deployment_that_failed_before_its_registry_step(self, real_env):
        """The deploy writes the ontology's registry row last, so a failure after the swap leaves a live graph
        that no registry row names. The purge then deletes the version rows, the last record of it."""
        real_env["vec"].store_embeddings_batch.side_effect = RuntimeError("index_limit_breached")
        assert _deploy()["partial"] is True
        assert dynamo_store.get_ontology_registry(NS, ONTO) is None
        assert real_env["neptune"].triples(_domain())
        vector = MagicMock()
        vector.delete_index.return_value = True
        with (
            patch.object(
                ontologies, "_stores", return_value=(neptune_db_graph.NeptuneDBGraphStore(namespace=NS), vector)
            ),
            patch.object(ontologies.abox_build, "stop_builds", return_value=0.0),
            patch.object(ontologies.abox_build, "drop_graphs"),
            patch.object(ontologies.studio_queries, "drop_all_draft_graphs"),
        ):
            result = ontologies.delete_ontologies(namespace=NS, ontology_id=None, x_coa_principal=None)
        assert real_env["neptune"].triples(_domain()) == set()
        assert result["graphsDropped"] == 1


class TestEmit:
    def test_null_version_after_delete(self, env):
        deploy.emit_ontology_deployed(NS, None, None)
        entry = env["events"].put_events.call_args.kwargs["Entries"][0]
        assert '"version": null' in entry["Detail"]

    def test_failed_entry_raises(self, env):
        env["events"].put_events.return_value = {"FailedEntryCount": 1, "Entries": []}
        with pytest.raises(RuntimeError, match="FailedEntryCount"):
            deploy.emit_ontology_deployed(NS, 1, "d")
