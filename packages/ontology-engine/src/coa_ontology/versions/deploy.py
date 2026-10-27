# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deploy: materialize one published version as the namespace's live domain ontology.

Steps, each retried, checkpointed on the DEPLOY row, and safe to re-run:

    stage       load the snapshot, its projection and a marker triple into a staging
                graph that lives outside the ``{base}/{ns}/`` prefix consumers enumerate
    swap        ``MOVE`` staging onto the domain graph in one SPARQL update; flip ACTIVE
    embeddings  compute the new AOSS embeddings, then replace the ontology's old ones
    registry    rewrite the ONTOLOGY# registry row and namespace META
    latest      copy the snapshot's VKG files to ``latest/``
    notify      emit ``ontology.deployed`` so VKG reloads

Fencing: only the holder of the namespace deploy lock may change what is served.
Each checkpoint refreshes the lock and raises :class:`registry.DeployLockLostError`
once another deployment has taken it over. Every step after ``stage`` checks before
each attempt (the checkpoint after a step is best-effort), the embedding step checks
again before each write, and the ACTIVE flip is itself fenced. A worker that lost the
lock records DEPLOY_FAILED and stops.

A failure while no MOVE can have landed leaves serving untouched and drops the
staging graph. Once a MOVE may have landed (it was sent and Neptune did not refuse
it), a failure is recorded with ``partial=True`` and the staging graph is left
alone; deploying the same version again converges. Rollback is a deploy of an older
version.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Thread
from typing import Any, cast

import boto3
import httpx
from coa_common import DEFAULT_EMBED_MODEL_ID
from coa_common.constants import EVENT_SOURCE_PREFIX, GOVERNED_METRICS_ONTOLOGY_ID, URN_PREFIX
from rdflib import RDFS, Graph, URIRef

from coa_ontology import dynamo_store
from coa_ontology.catalog.ingest import (
    PhysicalNames,
    _accumulate_embeddings,
    _class_datasource_map_from_r2rml,
    _first_literal,
    _graph_uri_prefix_for_response,
    _is_index_not_found,
    _mapped_predicates_from_r2rml,
    _parse_r2rml,
    _physical_names_from_r2rml,
    projection_specs,
    wait_for_embeddings_deleted,
    wait_for_embeddings_searchable,
)
from coa_ontology.stores import build_stores
from coa_ontology.stores.neptune_db_graph import (
    OWL,
    WB,
    NeptuneDBGraphStore,
    _esc,
    _iri,
    _ontology_graph_uri,
    _resolve_graph_base,
    _sparql_query,
    _sparql_update,
)
from coa_ontology.versions import registry, snapshot, storage

_log = logging.getLogger("versions.deploy")

DEPLOY_STEPS = ("stage", "swap", "embeddings", "registry", "latest", "notify")
_STEP_MAX_ATTEMPTS = int(os.getenv("DEPLOY_STEP_MAX_ATTEMPTS", "3"))
_STEP_BACKOFF_SECONDS = float(os.getenv("DEPLOY_STEP_BACKOFF_SECONDS", "2"))
_METRIC_VOCAB = f"urn:{URN_PREFIX}:vocab#"
# ``<staging graph> wb:deploymentId "<id>"`` is staged with the graph and moves with it.
_MARKER = WB + "deploymentId"
# Each deployment drops the staging graphs left by this many recent deployments (its own
# row included); leftovers are rare and recent, and namespace deletion drops them all.
_LEFTOVER_STAGING_LIMIT = 20
# A MOVE that got no answer may still be running on Neptune. Before looking for its marker,
# wait longer than Neptune's query timeout (120 s by default), when Neptune ends it either
# way, heartbeating the lock every slice.
_AMBIGUOUS_MOVE_WAIT_SECONDS = float(os.getenv("DEPLOY_AMBIGUOUS_MOVE_WAIT_SECONDS", "150"))
_AMBIGUOUS_MOVE_SLICE_SECONDS = 30.0


class DeployConflictError(Exception):
    """The deployment cannot start (version not published, incomplete snapshot, unsupported backend)."""


class BreakingMetricsError(Exception):
    """Governed metrics link to classes the target version removes and were not acknowledged."""

    def __init__(self, metrics: list[dict[str, str]]) -> None:
        """Carry the affected metrics for the 409 body."""
        super().__init__(f"{len(metrics)} governed metric(s) link to classes this version removes")
        self.metrics = metrics


class _StepError(Exception):
    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"step '{step}' failed: {message}")
        self.step = step


class _GraphOverrideStore(NeptuneDBGraphStore):
    """A Neptune store whose every read and write targets one fixed named graph."""

    def __init__(self, namespace: str, graph_uri: str) -> None:
        """Bind to ``namespace`` but route all graph I/O to ``graph_uri``."""
        super().__init__(namespace=namespace)
        self._graph_override = graph_uri

    def _graph_for(self, ontology_uri: str) -> str:
        return self._graph_override


class _CapturingVectorStore:
    """Keeps the embeddings ``_accumulate_embeddings`` writes in memory; anything else is the real store's.

    Deploy computes the vectors (Bedrock) before it deletes the old embeddings, so a
    Bedrock failure leaves them searchable.
    """

    def __init__(self, store: Any) -> None:
        """Wrap the real vector store."""
        self._store = store
        self.items: list[dict[str, Any]] = []

    def store_embeddings_batch(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Record ``items`` and acknowledge every one."""
        self.items.extend(items)
        return list(items)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


@dataclass(frozen=True)
class _Target:
    version: int
    manifest: dict[str, Any]
    ontology_id: str
    title: str
    ontology_text: str
    graph: Graph
    classes: set[URIRef]
    obj_props: set[URIRef]
    dt_props: set[URIRef]
    class_to_datasource: dict[str, str]
    # From the same mapping: the properties some predicate-object map maps (any other
    # property with a mapped domain, or a relationship with a mapped range, gets
    # ``coa:isMapped false``), and the physical table and column names the NL-to-SQL
    # class documents and the ``coa:physicalName`` markers use.
    mapped_predicates: set[str]
    physical_names: PhysicalNames


@dataclass
class _SwapState:
    may_have_moved: bool = False  # a MOVE was sent and Neptune did not refuse it
    moved: bool = False  # this worker saw the MOVE land


@dataclass
class _ComputedEmbeddings:
    """What one embeddings step computed, kept across its attempts so Bedrock runs once."""

    result: dict[str, Any] | None = None
    items: list[dict[str, Any]] = field(default_factory=list)


def staging_graph_uri(namespace: str, deployment_id: str) -> str:
    """Return the staging graph IRI.

    It sits under ``{base}/_staging/``; namespace names start with an
    alphanumeric, so no consumer's ``{base}/{ns}/`` prefix scan can see it.
    """
    return f"{_resolve_graph_base()}/_staging/{namespace}/{deployment_id}"


def drop_staging_graphs(namespace: str, keep: str | None = None, *, limit: int | None = None) -> list[str]:
    """Drop the staging graphs of the namespace's deployments except ``keep``; return the IRIs dropped.

    Deployments that died, or whose MOVE may have landed, leave their staging graph
    behind. A staging IRI follows from its deployment id, so the DEPLOY rows (newest
    first, at most ``limit``) name every graph to drop and Neptune is not scanned.
    Best-effort: a failure is logged and that graph (or, if the rows cannot be read,
    every graph) is skipped. Call only while no deployment of the namespace can be
    running: with its deploy lock held, or when deleting the namespace.
    """
    try:
        rows = registry.list_deployments(namespace, limit=limit)
    except Exception:  # noqa: BLE001 (best-effort; leftovers are invisible to consumers)
        _log.warning("deploy[%s]: could not list deployments to drop their staging graphs", namespace, exc_info=True)
        return []
    dropped = []
    for row in rows:
        graph_uri = staging_graph_uri(namespace, str(row["deployment_id"]))
        if graph_uri == keep or row.get("status") == registry.DEPLOYED:
            continue  # a DEPLOYED row's MOVE already took its staging graph
        try:
            _sparql_update(f"DROP SILENT GRAPH {_iri(graph_uri)}")
        except Exception:  # noqa: BLE001 (best-effort; the next cleanup retries it)
            _log.warning("deploy[%s]: could not drop staging graph %s", namespace, graph_uri, exc_info=True)
            continue
        dropped.append(graph_uri)
    return dropped


def _load_target(namespace: str, version: int, *, manifest: dict[str, Any] | None = None) -> _Target:
    """Read and parse a version's snapshot; a damaged one raises :class:`DeployConflictError`.

    ``manifest`` is an already-read manifest of this version, to avoid reading it again.
    """
    try:
        if manifest is None:
            manifest = storage.read_manifest(namespace, version)
        if manifest is None:
            raise storage.SnapshotIncompleteError(f"Snapshot {namespace} v{version} has no manifest")
        text = storage.require_version_file(namespace, version, "ontology.ttl")
    except (storage.SnapshotIncompleteError, storage.SnapshotFileMissingError) as e:
        raise DeployConflictError(f"version {version} has no complete snapshot: {e}") from e
    ontology_id = manifest.get("ontologyId")
    if not isinstance(ontology_id, str) or not ontology_id:
        raise DeployConflictError(f"version {version} has a damaged snapshot: its manifest has no ontologyId")
    try:
        graph = snapshot.parse_turtle(text, what="snapshot ontology")
    except snapshot.SnapshotValidationError as e:
        raise DeployConflictError(f"version {version} has a damaged snapshot: {e}") from e
    classes, obj_props, dt_props = snapshot.schema_subjects(graph)
    # An existing registry row keeps its title. The first deployment creates the row with
    # the title the accept recorded on the version (the proposal's label, else the
    # namespace-language default), else the ontology's own rdfs:label.
    existing = dynamo_store.get_ontology_registry(namespace, ontology_id) or {}
    recorded = (registry.get_version(namespace, version) or {}).get("title")
    # Parsed once for all three: the version detail view and the deploy preflight load the target too.
    mapping = _parse_r2rml(
        storage.read_version_file(namespace, version, "mappings.r2rml"),
        "class markers, property markers and table names",
    )
    return _Target(
        version=version,
        manifest=manifest,
        ontology_id=ontology_id,
        title=existing.get("title") or recorded or _first_literal(graph, ontology_id, RDFS.label) or "Induced Ontology",
        ontology_text=text,
        graph=graph,
        classes=classes,
        obj_props=obj_props,
        dt_props=dt_props,
        class_to_datasource=_class_datasource_map_from_r2rml(mapping),
        mapped_predicates=_mapped_predicates_from_r2rml(mapping),
        physical_names=_physical_names_from_r2rml(mapping),
    )


# ── Governed-metric compatibility ─────────────────────────────────────────


def _live_class_iris(namespace: str, ontology_id: str) -> set[str]:
    graph = _iri(_ontology_graph_uri(ontology_id, namespace))
    res = _sparql_query(
        f"SELECT DISTINCT ?c WHERE {{ GRAPH {graph} {{ "
        f"{{ ?c a {_iri(OWL + 'Class')} }} UNION {{ ?c a {_iri(str(RDFS.Class))} }} FILTER(isIRI(?c)) }} }}"
    )
    return {b["c"]["value"] for b in res.get("results", {}).get("bindings", [])}


def _metrics_linked_to(namespace: str, class_iris: Iterable[str]) -> list[dict[str, str]]:
    values = " ".join(_iri(c) for c in sorted(class_iris))
    graph = _iri(_ontology_graph_uri(GOVERNED_METRICS_ONTOLOGY_ID, namespace))
    res = _sparql_query(
        f"SELECT ?m ?name ?c WHERE {{ GRAPH {graph} {{ VALUES ?c {{ {values} }} "
        f"?m {_iri(str(RDFS.subClassOf))} {_iri(_METRIC_VOCAB + 'GovernedMetric')} . "
        f"?m {_iri(_METRIC_VOCAB + 'governedMetricFor')} ?c . "
        f"OPTIONAL {{ ?m {_iri(str(RDFS.label))} ?name }} }} }}"
    )
    metrics = []
    for b in res.get("results", {}).get("bindings", []):
        metric_id = b["m"]["value"].split(":metric:", 1)[-1]
        name = b.get("name", {}).get("value") or metric_id
        metrics.append({"metricId": metric_id, "name": name, "missingClassIri": b["c"]["value"]})
    return sorted(metrics, key=lambda m: (m["metricId"], m["missingClassIri"]))


def find_breaking_metrics(namespace: str, ontology_id: str, target_class_iris: set[str]) -> list[dict[str, str]]:
    """Return governed metrics linked to classes that are live now but absent from the target.

    Links to classes from other graphs (foundational, uploaded) are never in the
    removed set, so they are not reported.
    """
    removed = _live_class_iris(namespace, ontology_id) - target_class_iris
    return _metrics_linked_to(namespace, removed) if removed else []


def preview_breaking_metrics(
    namespace: str, version: int, *, manifest: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """Return the metrics a deploy of ``version`` would break, for the version detail view.

    Raises :class:`DeployConflictError` when the snapshot is incomplete or damaged. ``manifest`` is
    the version's manifest if the caller already read it.
    """
    target = _load_target(namespace, version, manifest=manifest)
    return find_breaking_metrics(namespace, target.ontology_id, {str(c) for c in target.classes})


# ── Retries ───────────────────────────────────────────────────────────────


def _backoff(attempt: int) -> float:
    return _STEP_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, _STEP_BACKOFF_SECONDS / 2)


def _retry[T](
    what: str,
    fn: Callable[[], T],
    *,
    give_up: tuple[type[Exception], ...] = (registry.DeployLockLostError,),
    before_sleep: Callable[[], None] | None = None,
) -> T:
    """Call ``fn`` up to ``_STEP_MAX_ATTEMPTS`` times; a ``give_up`` error is raised at once."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except give_up:
            raise
        except Exception as e:  # noqa: BLE001 (retried, then raised)
            if attempt >= _STEP_MAX_ATTEMPTS:
                raise
            _log.warning("%s: attempt %d failed (%s); retrying", what, attempt, e)
            if before_sleep is not None:
                before_sleep()
            time.sleep(_backoff(attempt))


def release_lock(namespace: str, deployment_id: str) -> None:
    """Release the deploy lock (holder-only), retrying; never raises over the error being handled."""
    try:
        _retry(
            f"deploy[{namespace}/{deployment_id}] lock release",
            lambda: registry.release_deploy_lock(namespace, deployment_id),
        )
    except Exception:  # noqa: BLE001 (the lock then expires after the stale threshold)
        _log.exception(
            "deploy[%s/%s]: could not release the deploy lock; it expires after %ds",
            namespace,
            deployment_id,
            registry.STALE_SECONDS,
        )


# ── Start ─────────────────────────────────────────────────────────────────


def _check_serves_same_ontology(namespace: str, target: _Target, active: dict[str, Any]) -> None:
    # The reset floor keeps ACTIVE in the target's lineage; this still catches ACTIVE
    # rows written before deploys kept the floor (they lost it on the next deploy).
    if active.get("version") is None:
        return
    row = registry.get_version(namespace, int(active["version"])) or {}
    served = row.get("ontology_id")
    if served and served != target.ontology_id:
        raise DeployConflictError(
            f"the namespace now serves ontology {served!r}; deploying a version of {target.ontology_id!r} "
            "would leave two domain ontologies"
        )


def start_deployment(
    namespace: str,
    version: int,
    *,
    requested_by: str,
    acknowledged_breaking_metrics: Iterable[str] = (),
) -> str:
    """Validate, lock and launch a deployment of ``version``; return its deployment id.

    Raises :class:`DeployConflictError`, :class:`registry.DeployLockHeldError` or
    :class:`BreakingMetricsError` before anything is written or served differently.
    A version at or below the reset floor belongs to a deleted ontology and is a
    :class:`DeployConflictError`, so ACTIVE always stays in the current lineage.
    """
    if os.getenv("WORKBENCH_BACKEND", "opensearch_neptune").strip().lower() != "opensearch_neptune":
        raise DeployConflictError("ontology versions require the opensearch_neptune backend")
    row = registry.get_version(namespace, version)
    if not row or row["status"] != registry.PUBLISHED:
        raise DeployConflictError(f"version {version} is not published")
    deployment_id = registry.new_deployment_id()
    registry.acquire_deploy_lock(namespace, deployment_id)
    try:
        # Read under the lock: a delete that held it may just have moved the floor.
        active = registry.get_active(namespace) or {}
        if version <= int(active.get("reset_after_version") or 0):
            raise DeployConflictError(
                f"Version v{version} belongs to a deleted ontology; accept a proposal to publish it again."
            )
        target = _load_target(namespace, version)
        _check_serves_same_ontology(namespace, target, active)
        if (dynamo_store.get_ontology_registry(namespace, target.ontology_id) or {}).get("status") == "deleting":
            raise DeployConflictError(f"ontology {target.ontology_id!r} is being deleted")
        breaking = find_breaking_metrics(namespace, target.ontology_id, {str(c) for c in target.classes})
        acknowledged = set(acknowledged_breaking_metrics)
        if any(m["metricId"] not in acknowledged for m in breaking):
            raise BreakingMetricsError(breaking)
        registry.create_deployment(
            namespace,
            deployment_id,
            version=version,
            requested_by=requested_by,
            acknowledged_breaking_metrics=sorted(acknowledged),
        )
    except Exception:
        release_lock(namespace, deployment_id)
        raise
    Thread(target=run_deployment, args=(namespace, deployment_id, version, requested_by), daemon=True).start()
    return deployment_id


# ── Worker ────────────────────────────────────────────────────────────────


def _checkpoint(namespace: str, deployment_id: str, progress: dict[str, str]) -> None:
    try:
        registry.checkpoint_deployment(namespace, deployment_id, progress=dict(progress))
    except registry.DeployLockLostError:
        raise
    except Exception:  # noqa: BLE001 (a progress-write hiccup must not fail a step that succeeded)
        _log.warning("deploy[%s/%s]: progress checkpoint failed (non-fatal)", namespace, deployment_id, exc_info=True)


def _run_step[T](
    namespace: str,
    deployment_id: str,
    progress: dict[str, str],
    step: str,
    fn: Callable[[], T],
    *,
    fenced: bool = False,
) -> T:
    """Run ``fn`` with retries, then checkpoint ``step`` as done.

    With ``fenced``, every attempt first checks that this deployment still holds the
    lock; an error in that check (other than a lost lock) counts as a failed attempt.
    Before each retry the progress is checkpointed (best-effort), which keeps the lock
    alive. :class:`registry.DeployLockLostError` is never retried.
    """

    def attempt() -> T:
        if fenced:
            registry.checkpoint_deployment(namespace, deployment_id, progress=dict(progress))
        return fn()

    try:
        result = _retry(
            f"deploy[{namespace}/{deployment_id}] step {step}",
            attempt,
            before_sleep=lambda: _checkpoint(namespace, deployment_id, progress),
        )
    except registry.DeployLockLostError:
        raise
    except Exception as e:  # noqa: BLE001 (every step error is surfaced by name)
        progress[step] = "failed"
        raise _StepError(step, f"{type(e).__name__}: {e}") from e
    progress[step] = "done"
    _checkpoint(namespace, deployment_id, progress)
    return result


def _count_triples(graph_uri: str) -> int:
    res = _sparql_query(f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH {_iri(graph_uri)} {{ ?s ?p ?o }} }}")
    bindings = res.get("results", {}).get("bindings", [])
    return int(bindings[0]["n"]["value"]) if bindings else 0


def _ask(query: str) -> bool:
    return bool(_sparql_query(query).get("boolean"))


def _marker(staging_uri: str, deployment_id: str) -> str:
    return f'{_iri(staging_uri)} {_iri(_MARKER)} "{_esc(deployment_id)}" .'


def _holds_marker(graph_uri: str, staging_uri: str, deployment_id: str) -> bool:
    return _ask(f"ASK {{ GRAPH {_iri(graph_uri)} {{ {_marker(staging_uri, deployment_id)} }} }}")


def _stage(namespace: str, deployment_id: str, staging_uri: str, target: _Target) -> None:
    _sparql_update(f"DROP SILENT GRAPH {_iri(staging_uri)}")
    store = _GraphOverrideStore(namespace, staging_uri)
    store.create_ontology(
        {
            "uri": target.ontology_id,
            "title": target.title,
            "ontology_type": "induced",
            "description": f"Deployed ontology version v{target.version}",
            "format": "turtle",
            "domain_tags": ["induced"],
        }
    )
    class_specs, prop_specs = projection_specs(
        target.graph,
        target.classes,
        target.obj_props,
        target.dt_props,
        target.class_to_datasource,
        mapped_predicates=target.mapped_predicates,
        physical_names=target.physical_names,
    )
    store.store_classes_batch(ontology_uri=target.ontology_id, classes=class_specs)
    store.store_properties_batch(ontology_uri=target.ontology_id, properties=prop_specs)
    store.load_turtle(ontology_uri=target.ontology_id, turtle=target.ontology_text)
    store.update_ontology(
        target.ontology_id,
        {
            "class_count": len(target.classes),
            "property_count": len(target.obj_props) + len(target.dt_props),
            "axiom_count": len(target.graph),
            "parse_status": "ok",
        },
    )
    _sparql_update(f"INSERT DATA {{ GRAPH {_iri(staging_uri)} {{ {_marker(staging_uri, deployment_id)} }} }}")
    staged = _count_triples(staging_uri)
    if staged < len(target.graph):
        raise RuntimeError(f"staging graph holds {staged} triples, expected at least {len(target.graph)}")
    version_info = f'{_iri(target.ontology_id)} {_iri(OWL + "versionInfo")} "v{target.version}"'
    if not _ask(f"ASK {{ GRAPH {_iri(staging_uri)} {{ {version_info} }} }}"):
        raise RuntimeError(f'staging graph lacks owl:versionInfo "v{target.version}" on {target.ontology_id}')


def _outwait_in_flight_move(heartbeat: Callable[[], None]) -> None:
    deadline = time.monotonic() + _AMBIGUOUS_MOVE_WAIT_SECONDS
    while (remaining := deadline - time.monotonic()) > 0:
        time.sleep(min(remaining, _AMBIGUOUS_MOVE_SLICE_SECONDS))
        heartbeat()


def _swap(
    state: _SwapState, deployment_id: str, staging_uri: str, domain_uri: str, *, heartbeat: Callable[[], None]
) -> None:
    """``MOVE`` staging onto the domain graph once, then remove the marker it carried.

    SPARQL 1.1 MOVE drops the destination, copies the source and drops it, in one
    update. Neptune answering with an error status means that update rolled back.
    Any other failure leaves the MOVE in doubt: it may still be running. A retry then
    first waits it out and looks for the marker. In the domain graph, the MOVE landed;
    still in staging, it did not; in neither, the staging graph is gone and moving it
    would empty the domain graph, so the step fails. A MOVE that returns is only
    trusted once the marker is in the domain graph, since a duplicate that ran after
    an earlier MOVE landed moves an empty staging graph.
    """
    if not state.moved and state.may_have_moved:
        _outwait_in_flight_move(heartbeat)
        if _holds_marker(domain_uri, staging_uri, deployment_id):
            state.moved = True
        elif not _holds_marker(staging_uri, staging_uri, deployment_id):
            raise RuntimeError(f"staging graph {staging_uri} is gone but the domain graph lacks its marker")
    if not state.moved:
        in_doubt = state.may_have_moved
        state.may_have_moved = True
        try:
            _sparql_update(f"MOVE GRAPH {_iri(staging_uri)} TO GRAPH {_iri(domain_uri)}")
        except httpx.HTTPStatusError:
            state.may_have_moved = in_doubt  # this MOVE rolled back; an earlier one in doubt may still land
            raise
        if not _holds_marker(domain_uri, staging_uri, deployment_id):
            raise RuntimeError(f"MOVE returned but domain graph {domain_uri} lacks the staged marker")
        state.moved = True
    _sparql_update(f"DELETE DATA {{ GRAPH {_iri(domain_uri)} {{ {_marker(staging_uri, deployment_id)} }} }}")


def _replace_embeddings(
    namespace: str, deployment_id: str, vector_store: Any, target: _Target, computed: _ComputedEmbeddings
) -> dict[str, Any]:
    if computed.result is None:
        capture = _CapturingVectorStore(vector_store)
        result: dict[str, Any] = _accumulate_embeddings(
            vector_store=cast(Any, capture),
            graph=target.graph,
            ontology_id=target.ontology_id,
            classes=target.classes,
            obj_props=target.obj_props,
            dt_props=target.dt_props,
            namespace=namespace,
            class_to_datasource=target.class_to_datasource,
            physical_names=target.physical_names,
        )
        if result.get("status") == "error":
            raise RuntimeError(f"embedding accumulation failed: {result.get('error', 'unknown')}")
        computed.result, computed.items = result, capture.items
    items = computed.items
    registry.checkpoint_deployment(namespace, deployment_id)  # fence: computing the vectors can take long
    try:
        old = vector_store.list_searchable_entity_uris(target.ontology_id, namespace=namespace)
    except Exception as e:  # noqa: BLE001
        if not _is_index_not_found(e):
            raise
        old = []
    vector_store.delete_embeddings_for_ontology(target.ontology_id, namespace=namespace)
    if not wait_for_embeddings_deleted(
        vector_store=vector_store, ontology_id=target.ontology_id, namespace=namespace, deleted_uris=old
    ):
        raise RuntimeError("previous embeddings are still searchable")
    registry.checkpoint_deployment(namespace, deployment_id)  # fence before the write
    stored = vector_store.store_embeddings_batch(items) if items else []
    acked: list[Any] = stored if isinstance(stored, list) else items
    if len(acked) < len(items):
        raise RuntimeError(f"vector store acknowledged {len(acked)} of {len(items)} embeddings")
    uris = [str(d["entity_uri"]) for d in acked if isinstance(d, dict) and d.get("entity_uri")]
    lost: list[registry.DeployLockLostError] = []

    def on_progress(done: int, total: int) -> None:
        # Keeps the lock alive through a long sync. The wait logs and swallows callback
        # errors, so a lost lock is remembered here and raised once the wait returns.
        try:
            registry.checkpoint_deployment(namespace, deployment_id, embeddings_progress=f"{done}/{total}")
        except registry.DeployLockLostError as e:
            lost.append(e)
            raise

    searchable = wait_for_embeddings_searchable(
        vector_store=vector_store,
        ontology_id=target.ontology_id,
        namespace=namespace,
        expected_uris=uris,
        on_progress=on_progress,
    )
    if lost:
        raise lost[0]
    if not searchable:
        raise RuntimeError("new embeddings did not become searchable")
    return {**computed.result, "count": len(acked), "entity_uris": uris}


def _write_registry(namespace: str, target: _Target, domain_uri: str, embeddings: dict[str, Any]) -> None:
    dynamo_store.put_ontology_registry(
        namespace,
        target.ontology_id,
        {
            "uri": target.ontology_id,
            "title": target.title,
            "description": f"Deployed ontology version v{target.version}",
            "ontology_type": "induced",
            "format": "turtle",
            "domain_tags": ["induced"],
            "source": "version-deploy",
            "version": f"v{target.version}",
            "active_version": target.version,
            "source_proposals": target.manifest.get("sourceProposals", []),
            "graph_uri": domain_uri,
            "embedding_index": embeddings.get("index"),
            "embedding_model_id": embeddings.get("model_id"),
            "embedding_count": embeddings.get("count"),
            "class_count": len(target.classes),
            "property_count": len(target.obj_props) + len(target.dt_props),
            "axiom_count": len(target.graph),
            "parse_status": "ok",
            "last_fetched": datetime.now(UTC).isoformat(),
        },
    )
    dynamo_store.put_namespace_meta(
        namespace,
        data={
            "backend": "opensearch_neptune",
            "embedding_model_id": embeddings.get("model_id")
            or os.getenv("BEDROCK_EMBED_MODEL_ID", DEFAULT_EMBED_MODEL_ID),
            "embedding_dimensions": int(os.getenv("BEDROCK_EMBED_DIMENSIONS", "1024")),
            "embedding_index": embeddings.get("index"),
            "graph_uri_prefix": _graph_uri_prefix_for_response(domain_uri),
        },
    )


def emit_ontology_deployed(namespace: str, version: int | None, deployment_id: str | None) -> None:
    """Emit ``ontology.deployed`` so VKG reloads ``latest/``; ``version=None`` means nothing is served.

    Raises on any failed entry so the ``notify`` step retries.
    """
    events = boto3.client("events", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    resp = events.put_events(
        Entries=[
            {
                "Source": f"{EVENT_SOURCE_PREFIX}.ontology",
                "DetailType": "ontology.deployed",
                "Detail": json.dumps(
                    {
                        "namespace": namespace,
                        "version": f"v{version}" if version else None,
                        "deploymentId": deployment_id,
                        "s3Bucket": dynamo_store._S3_BUCKET,
                        "s3Prefix": f"{storage.PREFIX}/{namespace}/latest/",
                    }
                ),
            }
        ]
    )
    if resp.get("FailedEntryCount", 0):
        raise RuntimeError(f"put_events reported FailedEntryCount={resp['FailedEntryCount']}: {resp.get('Entries')}")


def _request_abox_build(namespace: str, version: int) -> None:
    """Best-effort: the domain ABox follows a deploy, but never fails one that already swapped.

    A failed request is sent once more after a short backoff, since a lost one leaves the
    domain facts off until someone rebuilds them by hand.
    """
    for attempt in (1, 2):
        try:
            from coa_ontology.abox import build as abox_build

            abox_build.request_build(namespace, f"deploy v{version}")
            return
        except Exception:  # noqa: BLE001 (the deployment is already DEPLOYED: a missed request is only logged)
            if attempt == 1:
                _log.warning("deploy[%s]: domain ABox build request failed; retrying once", namespace, exc_info=True)
                time.sleep(_backoff(attempt))
            else:
                _log.warning("deploy[%s]: could not request the domain ABox build", namespace, exc_info=True)


def _finish_deployed(namespace: str, deployment_id: str, progress: dict[str, str]) -> None:
    """Record DEPLOYED (fenced; releases the lock in the same transaction), retrying transient errors."""
    try:
        _retry(
            f"deploy[{namespace}/{deployment_id}] finish",
            lambda: registry.finish_deployment(
                namespace, deployment_id, status=registry.DEPLOYED, partial=False, progress=dict(progress), error=""
            ),
            give_up=(registry.DeployLockLostError, registry.VersionStateError),
        )
    except registry.DeployLockLostError:
        row = registry.get_deployment(namespace, deployment_id)
        if row and row.get("status") == registry.DEPLOYED:
            return  # an earlier attempt landed and only its response was lost
        raise


def _finish_failed(namespace: str, deployment_id: str, *, partial: bool, progress: dict[str, str], error: str) -> None:
    """Record DEPLOY_FAILED, retrying transient errors; never raises over the failure being recorded."""
    label = f"deploy[{namespace}/{deployment_id}]"
    try:
        _retry(
            f"{label} record failure",
            lambda: registry.finish_deployment(
                namespace,
                deployment_id,
                status=registry.DEPLOY_FAILED,
                partial=partial,
                progress=dict(progress),
                error=error,
            ),
            give_up=(registry.VersionStateError,),
        )
    except registry.VersionStateError:
        # The stale sweep (or an earlier finish) already decided this row; keep its verdict.
        _log.warning("%s: row was no longer DEPLOYING; failure not recorded", label)
    except Exception:  # noqa: BLE001 (the stale sweep marks the row failed later)
        _log.exception("%s: could not record the failure", label)


def run_deployment(namespace: str, deployment_id: str, version: int, deployed_by: str) -> None:
    """Background worker: run every deploy step for one DEPLOY row, then release the lock."""
    label = f"deploy[{namespace}/{deployment_id}]"
    staging_uri = staging_graph_uri(namespace, deployment_id)
    progress: dict[str, str] = {}
    swap_state = _SwapState()
    computed = _ComputedEmbeddings()
    try:
        drop_staging_graphs(namespace, keep=staging_uri, limit=_LEFTOVER_STAGING_LIMIT)
        target = _retry(
            f"{label} load",
            lambda: _load_target(namespace, version),
            give_up=(registry.DeployLockLostError, DeployConflictError, snapshot.SnapshotValidationError),
        )
        domain_uri = _ontology_graph_uri(target.ontology_id, namespace)

        def swap() -> None:
            _swap(
                swap_state,
                deployment_id,
                staging_uri,
                domain_uri,
                heartbeat=lambda: _checkpoint(namespace, deployment_id, progress),
            )
            registry.set_active(
                namespace,
                version=version,
                deployment_id=deployment_id,
                deployed_by=deployed_by,
                fence_deployment_id=deployment_id,
            )

        _run_step(
            namespace, deployment_id, progress, "stage", lambda: _stage(namespace, deployment_id, staging_uri, target)
        )
        _run_step(namespace, deployment_id, progress, "swap", swap, fenced=True)
        _, vector_store = build_stores(namespace=namespace)
        embeddings = _run_step(
            namespace,
            deployment_id,
            progress,
            "embeddings",
            lambda: _replace_embeddings(namespace, deployment_id, vector_store, target, computed),
            fenced=True,
        )
        _run_step(
            namespace,
            deployment_id,
            progress,
            "registry",
            lambda: _write_registry(namespace, target, domain_uri, embeddings),
            fenced=True,
        )
        _run_step(
            namespace,
            deployment_id,
            progress,
            "latest",
            lambda: storage.publish_to_latest(namespace, version),
            fenced=True,
        )
        _run_step(
            namespace,
            deployment_id,
            progress,
            "notify",
            lambda: emit_ontology_deployed(namespace, version, deployment_id),
            fenced=True,
        )
        _finish_deployed(namespace, deployment_id, progress)
        _log.info("%s: v%d deployed", label, version)
        _request_abox_build(namespace, version)
    except Exception as e:  # noqa: BLE001 (every failure must land on the DEPLOY row)
        partial = swap_state.may_have_moved
        _log.exception("%s: v%d failed (partial=%s)", label, version, partial)
        if not partial:
            with contextlib.suppress(Exception):
                _sparql_update(f"DROP SILENT GRAPH {_iri(staging_uri)}")
        _finish_failed(namespace, deployment_id, partial=partial, progress=progress, error=f"{type(e).__name__}: {e}")
        if partial:
            # The swap may have landed and ACTIVE moved, so the ABox has to follow; a build reads ACTIVE.
            _request_abox_build(namespace, version)
    finally:
        # A no-op after a successful finish or when another deployment took the lock over.
        release_lock(namespace, deployment_id)
