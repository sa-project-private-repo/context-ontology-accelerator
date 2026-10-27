# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Publish: turn a proposal plus the namespace's newest version into the next immutable version.

``publish_version`` writes nothing to Neptune, AOSS or ``latest/`` (serving
changes only on deploy), so a failed publish is retried by accepting again. It
reads Neptune and ``latest/`` only to bootstrap a namespace that predates
versioning (:func:`bootstrap_if_needed`).

Errors a caller sees:

- :class:`~coa_ontology.versions.snapshot.SnapshotValidationError`: the proposal
  cannot be published as is (bad RDF, a mapping the ontology does not support,
  a namespace that holds another ontology's versions).
- :class:`PublishBusyError`: retryable; accept again. Its subclass
  :class:`BootstrapPendingError` means the namespace's pre-versioning ontology is
  not captured as v1 yet; the next attempt re-runs the bootstrap.
- :class:`PublishStateError` (and :class:`BootstrapError`): the stored versions or
  the legacy ontology are damaged or inconsistent; an operator must act.
- :class:`PublishError`: a mapped table is missing from the source catalog, so
  ``vkg/schema.sql`` cannot declare it; the message lists the remedies.
- :class:`RebaseRequiredError`: a replace-mode (Ontology designer) publish whose draft was built
  on another version than the newest; rebase the draft and accept again.
- ``proposals.SchemaSqlGenerationError``: the datasource catalog could not be read
  for ``vkg/schema.sql`` (transient, or a catalog an operator must fix).

Infrastructure errors (botocore ``ClientError``, Neptune / ``httpx`` errors) pass
through unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from coa_common.constants import VOCAB_URI
from rdflib import Graph, URIRef

from coa_ontology import dynamo_store
from coa_ontology.datasource_ids import bare_datasource_id
from coa_ontology.versions import registry, snapshot, storage

_log = logging.getLogger("versions.publish")

SOURCE_PROPOSAL = "PROPOSAL"
SOURCE_BOOTSTRAP = "BOOTSTRAP"
# How a publish turns the proposal into the next version: UNION merges it onto the parent
# (an ordinary accept); REPLACE takes it as the whole next version (a designer draft).
UNION = "union"
REPLACE = "replace"
# How long a publish waits for an in-flight publish in the same namespace. Longer than
# the stale threshold, so a waiter behind a dead publish outlives the sweep that frees it.
_RESERVE_WAIT_SECONDS = float(
    os.getenv("ONTOLOGY_PUBLISH_RESERVE_WAIT_SECONDS", str(registry.PUBLISH_STALE_SECONDS + 60))
)
_RESERVE_POLL_SECONDS = float(os.getenv("ONTOLOGY_PUBLISH_RESERVE_POLL_SECONDS", "5"))
# How often a running publish refreshes its PUBLISHING row, well inside the stale threshold.
_HEARTBEAT_SECONDS: float = max(1, registry.PUBLISH_STALE_SECONDS // 6)
_DATASOURCE_ID = URIRef(f"{VOCAB_URI}datasourceId")
_MISSING_TABLES_SHOWN = 10

Progress = Callable[[str], None]


class PublishError(Exception):
    """A publish did not complete for a reason other than the proposal's content."""


class PublishBusyError(PublishError):
    """The namespace was busy or the publish lost its version row; accepting again can succeed."""


class BootstrapPendingError(PublishBusyError):
    """An earlier bootstrap failed after reserving, so the live ontology is not v1 yet; publishing again re-runs it."""


class PublishStateError(PublishError):
    """The stored versions are damaged or inconsistent; an operator must repair or delete them."""


class BootstrapError(PublishStateError):
    """The pre-versioning ontology cannot be captured as v1 because it is inconsistent."""


class RebaseRequiredError(PublishError):
    """A replace-mode publish was built on a version that is no longer the namespace's newest; rebase the draft."""


class _SweptError(PublishBusyError):
    """The version row was swept as stale before the publish finished; it is already PUBLISH_FAILED."""


@dataclass(frozen=True)
class PublishRequest:
    """Everything a proposal accept hands to :func:`publish_version`."""

    namespace: str
    ontology_id: str
    ontology_turtle: str
    r2rml_turtle: str
    source_proposal_id: str
    created_by: str
    datasource_ids: list[str] = field(default_factory=list)
    constraint_config: dict[str, Any] | None = None
    uri_prefix: str | None = None
    # Datasources whose induction is known to be complete: the proposal's R2RML is
    # their full mapping, so parent maps it omits are dropped.
    authoritative_datasources: list[str] = field(default_factory=list)
    # Title for the ontology's registry row, which the first deployment creates: the
    # proposal's label, or the namespace-language default the accept picked.
    title: str | None = None
    # UNION or REPLACE. REPLACE publishes ``ontology_turtle`` / ``r2rml_turtle`` as the whole
    # next version and only onto ``expected_parent_version`` (None: the namespace's first version).
    mode: str = UNION
    expected_parent_version: int | None = None
    # For a REPLACE publish of an Ontology designer draft: the proposal's own ontology and R2RML
    # Turtle, which ``constraint_config`` was compiled for. Its shapes then get the renames the
    # draft's base kept (:func:`union_candidate`), so an unedited draft publishes the same shapes
    # as a plain accept. Without them a shape on a renamed term is pruned.
    proposal_ontology_turtle: str = ""
    proposal_r2rml_turtle: str = ""


@dataclass(frozen=True)
class _Parent:
    """What a publish inherits from the version it builds on."""

    version: int | None
    ontology: Graph | None
    r2rml: Graph
    shapes: Graph
    source_proposals: list[str]


def publish_version(req: PublishRequest, *, on_progress: Progress | None = None) -> int:
    """Publish ``req`` as the namespace's next version and return its number.

    ``on_progress`` hears ``"waiting"`` on each poll while another publish holds the
    namespace and ``"publishing"`` on each heartbeat of this one. The heartbeat calls
    run on a background thread, so the callback must be thread-safe; an exception it
    raises there is logged and ignored. The errors raised are listed in the module
    docstring.
    """
    bootstrap_if_needed(req.namespace, req.ontology_id)
    head = registry.head_version(req.namespace)
    if head and head["ontology_id"] != req.ontology_id:
        raise snapshot.SnapshotValidationError(
            f"namespace versions belong to ontology {head['ontology_id']!r}, not {req.ontology_id!r}"
        )
    _check_replace_parent(req, head["version"] if head else None)
    version, parent = _reserve(
        req.namespace, req.ontology_id, req.source_proposal_id, req.created_by, on_progress, title=req.title
    )
    try:
        # Checked again on the reserved parent: a publish may have landed since the check above.
        _check_replace_parent(req, parent)
        if parent is None and _awaiting_bootstrap(req.namespace, req.ontology_id):
            # A bootstrap that failed after reserving left the live ontology uncaptured;
            # a parentless version would drop it from this and every later version.
            raise BootstrapPendingError("the namespace's existing ontology is not captured as v1 yet; accept again")
        with _heartbeat(req.namespace, version, on_progress) as swept:
            files, diff, triple_count, source_proposals = _build_files(req, version, parent)
            manifest = snapshot.build_manifest(
                namespace=req.namespace,
                version=version,
                parent_version=parent,
                source=SOURCE_PROPOSAL,
                source_proposal_id=req.source_proposal_id,
                source_proposals=source_proposals,
                ontology_id=req.ontology_id,
                created_by=req.created_by,
                created_at=datetime.now(UTC).isoformat(),
                triple_count=triple_count,
                files=files,
            )
            _commit(req.namespace, version, files, manifest, diff, swept)
    except _SweptError:
        raise  # the row is already PUBLISH_FAILED
    except Exception as e:
        _mark_failed(req.namespace, version, e)
        raise
    _log.info("publish: %s v%d from proposal %s (parent=%s)", req.namespace, version, req.source_proposal_id, parent)
    return version


def _check_replace_parent(req: PublishRequest, parent: int | None) -> None:
    """Refuse a replace-mode publish whose draft was built on another version than ``parent``."""
    if req.mode == REPLACE and parent != req.expected_parent_version:
        built_on = "no version" if req.expected_parent_version is None else f"v{req.expected_parent_version}"
        newest = "none" if parent is None else f"v{parent}"
        raise RebaseRequiredError(
            f"The designer draft was built on {built_on}, but the newest version is {newest}; "
            "rebase the draft in the Ontology designer and accept again."
        )


def _reserve(
    namespace: str,
    ontology_id: str,
    source_proposal_id: str,
    created_by: str,
    on_progress: Progress | None,
    *,
    title: str | None = None,
) -> tuple[int, int | None]:
    deadline = time.monotonic() + _RESERVE_WAIT_SECONDS
    while True:
        try:
            return registry.reserve_next_version(
                namespace,
                ontology_id=ontology_id,
                source=SOURCE_PROPOSAL,
                source_proposal_id=source_proposal_id,
                created_by=created_by,
                title=title,
            )
        except registry.PublishInProgressError:
            if time.monotonic() >= deadline:
                raise PublishBusyError(f"another publish is still running in namespace {namespace!r}") from None
            if on_progress is not None:
                on_progress("waiting")
            time.sleep(_RESERVE_POLL_SECONDS)


@contextmanager
def _heartbeat(namespace: str, version: int, on_progress: Progress | None) -> Iterator[threading.Event]:
    """Refresh the PUBLISHING row while the body runs; the yielded event is set once the row was swept.

    ``on_progress("publishing")`` is called from the heartbeat thread after each refresh.
    """
    swept = threading.Event()
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(_HEARTBEAT_SECONDS):
            try:
                registry.touch_version(namespace, version)
            except registry.VersionStateError:
                swept.set()
                return
            except Exception:
                # A missed beat is retried on the next one; the stale threshold allows several.
                _log.warning("publish: heartbeat for %s v%d failed", namespace, version, exc_info=True)
                continue
            if on_progress is not None:
                try:
                    on_progress("publishing")
                except Exception:
                    _log.warning("publish: progress callback failed for %s v%d", namespace, version, exc_info=True)

    thread = threading.Thread(target=beat, name=f"publish-heartbeat-{namespace}-v{version}", daemon=True)
    thread.start()
    try:
        yield swept
    finally:
        stop.set()
        thread.join()


def _swept_error(version: int) -> _SweptError:
    return _SweptError(f"version v{version} was swept as stale before it finished; accept again")


def _write_snapshot(namespace: str, version: int, files: dict[str, str], manifest: dict[str, Any]) -> None:
    try:
        storage.write_snapshot(namespace, version, files, snapshot.dumps(manifest))
    except storage.SnapshotExistsError as e:
        # Left by an earlier worker for this number; the number becomes a gap and a retry takes the next one.
        raise PublishBusyError(f"snapshot objects of v{version} already exist; accept again") from e


def _commit(
    namespace: str,
    version: int,
    files: dict[str, str],
    manifest: dict[str, Any],
    diff: dict[str, Any],
    swept: threading.Event,
) -> None:
    """Write the snapshot, then flip its row to PUBLISHED.

    Raises :class:`PublishBusyError` when the row was swept as stale meanwhile: it is
    already PUBLISH_FAILED, so the snapshot is not written, or is never used.
    """
    if swept.is_set():
        raise _swept_error(version)
    _write_snapshot(namespace, version, files, manifest)
    try:
        registry.mark_published(
            namespace,
            version,
            files=manifest["files"],
            diff_summary=snapshot.diff_summary(diff),
            source_proposals=manifest["sourceProposals"],
        )
    except registry.VersionStateError as e:
        row = registry.get_version(namespace, version)
        if row and row["status"] == registry.PUBLISHED:
            return  # an earlier attempt of this write landed and only its response was lost
        raise _swept_error(version) from e


def _mark_failed(namespace: str, version: int, error: Exception) -> None:
    """Record ``error`` on the version row; the caller re-raises ``error`` either way."""
    try:
        registry.mark_publish_failed(namespace, version, f"{type(error).__name__}: {error}")
    except registry.VersionStateError:
        _log.warning("publish: %s v%d was no longer PUBLISHING when it failed: %s", namespace, version, error)


def _vkg_ontology(ontology_nt: str, r2rml_nt: str) -> str:
    # Imported lazily: proposals imports this module.
    from coa_ontology.proposals import _clean_ontology_for_vkg

    cleaned = _clean_ontology_for_vkg(ontology_nt, r2rml_turtle=r2rml_nt or None)
    return snapshot.canonical_nt(snapshot.parse_turtle(cleaned, what="VKG ontology"))


def _compile_shapes(req: PublishRequest) -> Graph:
    if not req.constraint_config:
        return Graph()
    from pydantic import ValidationError

    from coa_ontology.validation.shapes.config import ConstraintConfig, compile_to_shacl

    try:
        config = ConstraintConfig(**req.constraint_config)
    except ValidationError as e:
        raise snapshot.SnapshotValidationError(f"Invalid constraint config: {e}") from e
    return snapshot.parse_turtle(compile_to_shacl(config, req.uri_prefix or req.ontology_id), what="SHACL")


def _parent_file(namespace: str, version: int, name: str) -> Graph:
    text = storage.require_version_file(namespace, version, name)
    try:
        return snapshot.parse_turtle(text, what=f"parent v{version} {name}")
    except snapshot.SnapshotValidationError as e:
        raise PublishStateError(f"Snapshot {namespace} v{version} {name} does not parse: {e}") from e


def _read_parent(req: PublishRequest, version: int | None) -> _Parent:
    """Read the version ``req`` builds on.

    A parent listed as PUBLISHED must have a readable manifest and every file it
    lists; anything missing or unparseable is a :class:`PublishStateError` instead of
    an empty parent.
    """
    if version is None:
        return _Parent(None, None, Graph(), Graph(), [])
    ns = req.namespace
    try:
        manifest = storage.read_manifest(ns, version)
        if manifest is None:
            raise storage.SnapshotIncompleteError(f"Snapshot {ns} v{version} has no manifest")
        if manifest.get("ontologyId") != req.ontology_id:
            raise snapshot.SnapshotValidationError(
                f"namespace versions belong to ontology {manifest.get('ontologyId')!r}, not {req.ontology_id!r}"
            )
        listed = manifest["files"]
        ontology = _parent_file(ns, version, "ontology.ttl")
        r2rml = _parent_file(ns, version, "mappings.r2rml") if "mappings.r2rml" in listed else Graph()
        shapes = _parent_file(ns, version, "shapes.ttl") if "shapes.ttl" in listed else Graph()
    except (storage.SnapshotIncompleteError, storage.SnapshotFileMissingError) as e:
        raise PublishStateError(f"Parent version v{version} of {ns} is damaged: {e}") from e
    return _Parent(version, ontology, r2rml, shapes, [str(p) for p in manifest.get("sourceProposals") or []])


def _datasource_ids(r2rml: Graph) -> set[str]:
    """Return the bare ``coa:datasourceId`` values the maps of ``r2rml`` carry."""
    return {bare_datasource_id(str(o)) for o in r2rml.objects(None, _DATASOURCE_ID)}


def _authoritative(req: PublishRequest, incoming_r2rml: Graph) -> list[str]:
    """Return the requested authoritative datasources that the proposal's R2RML actually maps.

    A datasource with no map in the proposal (a failed catalog fetch during a
    rebuild, say) must keep its parent maps rather than lose all of them.
    """
    requested = {bare_datasource_id(ds) for ds in req.authoritative_datasources}
    return sorted(requested & _datasource_ids(incoming_r2rml))


def _check_r2rml(ontology: Graph, r2rml: Graph, parent: _Parent) -> None:
    """Run the publish check; a failure the parent already had is the parent's, not the proposal's."""
    try:
        snapshot.check_r2rml(ontology, r2rml, previous=parent.r2rml)
    except snapshot.SnapshotValidationError as e:
        if parent.ontology is not None:
            try:
                snapshot.check_r2rml(parent.ontology, parent.r2rml)
            except snapshot.SnapshotValidationError as inherited:
                raise PublishStateError(f"Parent version v{parent.version} is itself inconsistent: {inherited}") from e
        raise


def _removed_by(namespace: str, version: int) -> set[str]:
    """Return the IRIs the ``diff.json`` of ``version`` lists as removed from its parent."""
    text = storage.read_version_file(namespace, version, "diff.json")
    try:
        diff = json.loads(text) if text else {}
    except json.JSONDecodeError as e:
        raise PublishStateError(f"Snapshot {namespace} v{version} diff.json does not parse: {e}") from e
    return {iri for entry in diff.values() for iri in entry.get("removed", [])}


def _removed_in_lineage(namespace: str, version: int | None, iris: set[str]) -> set[str]:
    """Return the IRIs of ``iris`` that ``version`` or one of its ancestors removed from its parent.

    A union publish keeps every class and property of its parent, so only a replace-mode
    publish (an Ontology designer accept) removes one. The walk follows ``parent_version``
    and reads a version's ``diff.json`` only when its diff summary counts a removal.
    """
    rows = {row["version"]: row for row in registry.list_versions(namespace)}
    found: set[str] = set()
    while version is not None and found != iris and (row := rows.pop(version, None)) is not None:
        summary = row.get("diff_summary") or {}
        if not summary or any(counts.get("removed") for counts in summary.values()):
            found |= iris & _removed_by(namespace, version)
        version = row.get("parent_version")
    return found


def _kept_renames(namespace: str, parent: _Parent, incoming: Graph, incoming_r2rml: Graph) -> dict[URIRef, URIRef]:
    """Return the Ontology designer renames a publish of the proposal onto ``parent`` keeps.

    :func:`snapshot.rename_candidates` finds the parent's terms whose maps the proposal
    names with another IRI. A candidate is a designer rename only when a version of the
    lineage removed its old IRI; an IRI the inducer chose afresh was never removed, so
    the proposal keeps it and merges as it is.
    """
    if parent.ontology is None:
        return {}
    candidates = snapshot.rename_candidates(parent.ontology, parent.r2rml, incoming, incoming_r2rml)
    if not candidates:
        return {}
    removed = _removed_in_lineage(namespace, parent.version, {str(old) for old in candidates})
    return {old: new for old, new in candidates.items() if str(old) in removed}


def _merge_union(req: PublishRequest, parent: _Parent) -> tuple[Graph, Graph, dict[URIRef, URIRef]]:
    """The ontology and mapping a union-mode publish of ``req`` onto ``parent`` snapshots, before the stamp.

    A class or property the parent renamed in the Ontology designer keeps its new IRI
    when the proposal re-induces its table (:func:`_kept_renames`); the renames applied
    to the proposal are returned too.
    """
    incoming = snapshot.clean_incoming(req.ontology_turtle)
    incoming_r2rml = snapshot.parse_turtle(req.r2rml_turtle, what="R2RML")
    renames = _kept_renames(req.namespace, parent, incoming, incoming_r2rml)
    for old, new in sorted(renames.items()):
        _log.info("publish: %s keeps the renamed %s for the re-induced %s", req.namespace, new, old)
        snapshot.rename_term(incoming, old, new)
        snapshot.rename_term(incoming_r2rml, old, new)
    merged = snapshot.merge_ontology(parent.ontology if parent.ontology is not None else Graph(), incoming)
    r2rml = snapshot.merge_r2rml(
        parent.r2rml, incoming_r2rml, authoritative_datasources=_authoritative(req, incoming_r2rml)
    )
    return merged, r2rml, renames


def union_candidate(req: PublishRequest, parent_version: int | None) -> tuple[Graph, Graph]:
    """Return what a union-mode publish of ``req`` onto ``parent_version`` would snapshot, before the stamp.

    The Ontology designer opens and rebases drafts with this, so accepting an unedited draft in
    replace mode publishes the same files as a plain accept would, its shapes included when the
    accept passes the proposal's own Turtle (``PublishRequest.proposal_ontology_turtle``).
    """
    merged, r2rml, _ = _merge_union(req, _read_parent(req, parent_version))
    return merged, r2rml


def _build_files(
    req: PublishRequest, version: int, parent_version: int | None
) -> tuple[dict[str, str], dict[str, Any], int, list[str]]:
    from coa_ontology.proposals import SchemaSqlCoverageError, _generate_schema_sql

    ns = req.namespace
    parent = _read_parent(req, parent_version)

    renames: dict[URIRef, URIRef] = {}
    if req.mode == REPLACE:
        # The draft already is the next version: deletions and renames carry over.
        merged = snapshot.clean_incoming(req.ontology_turtle)
        r2rml = snapshot.parse_turtle(req.r2rml_turtle, what="R2RML")
        if req.constraint_config and req.proposal_ontology_turtle:
            # The shapes are compiled for the proposal's IRIs: rename them as the draft's base renamed the proposal.
            renames = _kept_renames(
                ns,
                parent,
                snapshot.clean_incoming(req.proposal_ontology_turtle),
                snapshot.parse_turtle(req.proposal_r2rml_turtle, what="proposal R2RML"),
            )
    else:
        merged, r2rml, renames = _merge_union(req, parent)
    snapshot.stamp_version(merged, req.ontology_id, version)
    _check_r2rml(merged, r2rml, parent)

    ontology_nt = snapshot.canonical_nt(merged)
    r2rml_nt = snapshot.canonical_nt(r2rml)
    diff = snapshot.compute_diff(parent.ontology, merged)
    files = {
        "ontology.ttl": ontology_nt,
        "diff.json": snapshot.dumps(diff),
        "vkg/ontology.ttl": _vkg_ontology(ontology_nt, r2rml_nt),
    }
    if r2rml_nt:
        files["mappings.r2rml"] = r2rml_nt
        # The DDL describes exactly this snapshot's datasources, not every accepted proposal's.
        datasources = {bare_datasource_id(ds) for ds in req.datasource_ids} | _datasource_ids(r2rml)
        try:
            schema_sql = _generate_schema_sql(
                ns, req.ontology_id, r2rml_nt, extra_datasource_ids=sorted(datasources), scan_accepted=False
            )
        except SchemaSqlCoverageError as e:
            # Fail closed: Ontop rejects the whole namespace for a mapped table without DDL,
            # and a catalog that dropped a table (transient read failure, unapproved or
            # removed asset) cannot be told apart from one that never had it. The list is capped
            # so the remedies survive the 1000-character accept_error truncation.
            shown = ", ".join(e.missing[:_MISSING_TABLES_SHOWN])
            if len(e.missing) > _MISSING_TABLES_SHOWN:
                shown += f" and {len(e.missing) - _MISSING_TABLES_SHOWN} more"
            raise PublishError(
                f"Mapped table(s) {shown} are not available in the source catalog (removed, "
                "not approved, or temporarily unreachable). If the source is available, retry the accept; "
                "otherwise re-induce the datasource, or delete the domain ontology to drop its mappings."
            ) from e
        if schema_sql:
            files["vkg/schema.sql"] = schema_sql
    # The proposal's shapes replace the parent's shapes of the same IRI; the others are kept.
    # They follow the renames the union kept, so they constrain the renamed terms.
    incoming_shapes = _compile_shapes(req)
    for old, new in renames.items():
        snapshot.rename_term(incoming_shapes, old, new)
    shapes_graph = snapshot.merge_shapes(parent.shapes, incoming_shapes)
    if req.mode == REPLACE:
        # A shape about a class or property the draft deleted would constrain nothing that exists.
        shapes_graph = snapshot.prune_shapes(shapes_graph, merged)
    shapes = snapshot.canonical_nt(shapes_graph)
    if shapes:
        files["shapes.ttl"] = shapes

    source_proposals = sorted({*parent.source_proposals, req.source_proposal_id})
    return files, diff, len(merged), source_proposals


def _awaiting_bootstrap(namespace: str, ontology_id: str) -> dict[str, Any] | None:
    """Return the legacy ``induced`` registry row while the namespace's live ontology is not captured as v1."""
    if registry.head_version(namespace) is not None or registry.get_active(namespace) is not None:
        return None
    row: dict[str, Any] | None = dynamo_store.get_ontology_registry(namespace, ontology_id)
    return row if row and row.get("ontology_type") == "induced" else None


def _read_live(namespace: str, ontology_id: str) -> tuple[Graph, Graph]:
    """Read and check the live ontology and mapping; raise :class:`BootstrapError` if they are inconsistent."""
    from coa_ontology.stores import neptune_db_graph

    def inconsistent(problem: str) -> BootstrapError:
        return BootstrapError(
            f"Cannot capture the existing ontology {ontology_id!r} of namespace {namespace!r} as v1: {problem}. "
            "Its live ontology and mapping are inconsistent; delete the ontology and re-induce it."
        )

    live = neptune_db_graph._gsp_get_turtle(neptune_db_graph._ontology_graph_uri(ontology_id, namespace))
    if not live or not live.strip():
        raise inconsistent("the registry lists it as induced but Neptune has no graph for it")
    try:
        graph = snapshot.parse_turtle(live, what="live ontology")
        snapshot.strip_projection(graph, ontology_id)
        r2rml = snapshot.parse_turtle(storage.read_latest(namespace, "mappings.r2rml"), what="latest/ R2RML")
        snapshot.check_r2rml(graph, r2rml)
    except snapshot.SnapshotValidationError as e:
        raise inconsistent(str(e)) from e
    return graph, r2rml


def _bootstrap_committed(namespace: str, version: int) -> bool:
    """Whether ``version`` is PUBLISHED and ACTIVE, as a bootstrap commit that landed leaves it."""
    row = registry.get_version(namespace, version)
    active = registry.get_active(namespace)
    return bool(row and row["status"] == registry.PUBLISHED and active and active.get("version") == version)


def bootstrap_if_needed(namespace: str, ontology_id: str) -> int | None:
    """Capture a pre-versioning namespace's live ontology as active v1 so the first publish has a parent.

    Runs only when the namespace has no published version and no ACTIVE row but
    does have an accepted (``induced``) ontology. Nothing is redeployed, and v1
    approximates what is served: the live Neptune graph minus the projection deploy
    re-derives (:func:`snapshot.strip_projection` lists the harmless leftovers it
    keeps), the ``latest/`` mapping and ``schema.sql``, and a VKG ontology
    recomputed from them.

    Everything is read and checked before a version number is reserved, so an
    inconsistent legacy ontology (:class:`BootstrapError`) burns no number. Returns
    None when another worker is bootstrapping or publishing, when a version already
    exists, or when an ACTIVE row appeared meanwhile.
    """
    row = _awaiting_bootstrap(namespace, ontology_id)
    if row is None:
        return None
    graph, r2rml = _read_live(namespace, ontology_id)
    schema_sql = storage.read_latest(namespace, "schema.sql")
    source_proposals = sorted(str(p) for p in row.get("source_proposals") or [])
    try:
        version, _ = registry.reserve_next_version(
            namespace,
            ontology_id=ontology_id,
            source=SOURCE_BOOTSTRAP,
            source_proposal_id=None,
            created_by="system",
            require_first=True,
        )
    except (registry.PublishInProgressError, registry.BootstrapSupersededError):
        return None  # another worker is bootstrapping or already published; the caller's reserve waits for it
    try:
        snapshot.stamp_version(graph, ontology_id, version)
        ontology_nt = snapshot.canonical_nt(graph)
        r2rml_nt = snapshot.canonical_nt(r2rml)
        diff = snapshot.compute_diff(None, graph)
        files = {
            "ontology.ttl": ontology_nt,
            "diff.json": snapshot.dumps(diff),
            "vkg/ontology.ttl": _vkg_ontology(ontology_nt, r2rml_nt),
        }
        if r2rml_nt:
            files["mappings.r2rml"] = r2rml_nt
        if schema_sql:
            files["vkg/schema.sql"] = schema_sql
        manifest = snapshot.build_manifest(
            namespace=namespace,
            version=version,
            parent_version=None,
            source=SOURCE_BOOTSTRAP,
            source_proposal_id=None,
            source_proposals=source_proposals,
            ontology_id=ontology_id,
            created_by="system",
            created_at=datetime.now(UTC).isoformat(),
            triple_count=len(graph),
            files=files,
        )
        _write_snapshot(namespace, version, files, manifest)
        registry.commit_bootstrap_version(
            namespace,
            version,
            files=manifest["files"],
            diff_summary=snapshot.diff_summary(diff),
            source_proposals=source_proposals,
        )
    except (registry.ActiveExistsError, registry.VersionStateError) as e:
        if _bootstrap_committed(namespace, version):
            _log.info("publish: bootstrap commit of %s v%d had landed; its response was lost", namespace, version)
        else:
            # Another actor already decided ACTIVE (a deployment or a delete), or the row was swept.
            _log.warning("publish: bootstrap of %s v%d abandoned: %s", namespace, version, e)
            _mark_failed(namespace, version, e)
            return None
    except Exception as e:
        _mark_failed(namespace, version, e)
        raise
    _log.info("publish: bootstrapped %s v%d from the live graph", namespace, version)
    return version
