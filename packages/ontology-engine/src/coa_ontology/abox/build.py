# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Coalesced, locked and fenced domain ABox builds (spec §6).

``request_build`` records a pending request and starts a worker when no live build holds
``ABOXLOCK``. The worker loops while requests keep arriving, so requests are never lost
and at most one build runs per namespace. Each build reassembles the whole ABox from
the lexical graph, the mapping table and the extraction cache, loads it into a staging
graph and swaps it in only if the active version is unchanged and the lock still held.

Every Neptune, S3 and DynamoDB step of a build, reads included, and every embedding and k-NN
call is retried with A's ``_retry`` (spec §6.3). Best-effort steps (dropping a staging graph, the
extraction cache, heartbeats, pruning old build archives) log a failure instead, and a failed model
call costs only its batch or chunk. The lock is heartbeated throughout the long stages, at most once
per ``BEAT_SECONDS``. Every write is also checked against the lock, unthrottled: DynamoDB writes are
fenced on it and each S3 or Neptune write is sent right after a read of it. So a build that lost the
lock, to another worker or to a namespace teardown (``stop_builds``), sends no more writes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from threading import Thread
from typing import Any

import httpx
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD

from coa_ontology.stores.neptune_db_graph import _esc, _gsp_post_turtle, _iri, _sparql_query, _sparql_update
from coa_ontology.versions import deploy, registry, storage
from coa_ontology.versions.snapshot import canonical_nt, sha256_hex

from . import extract, mapping, matcher, store
from . import model as m
from .assemble import assemble
from .lexical import read_lexical
from .tbox import TBox, load_tbox

_log = logging.getLogger("abox.build")
_LOAD_BATCH_LINES = 5000
# At most one heartbeat per this many seconds; far below store.STALE_SECONDS.
BEAT_SECONDS = 60.0
# A build whose heartbeats have all failed for this long gives its lease up (``store.LockLost``)
# before another worker may take the lock over.
_LEASE_GIVE_UP_SECONDS = store.STALE_SECONDS / 2
# Workers started in a row after the previous one failed outside a build; past this the
# request stays pending for recover_if_stalled, instead of a hot loop of workers. Restart n
# (from 0) waits min(_RESTART_BACKOFF_SECONDS * 2**n, _RESTART_BACKOFF_MAX_SECONDS) first.
_MAX_RESTARTS = 3
_RESTART_BACKOFF_SECONDS = 1.0
_RESTART_BACKOFF_MAX_SECONDS = 30.0
SUPERSEDED = "SUPERSEDED"


class _Superseded(Exception):
    """The active version changed during the build; the pending request covers it."""


class _SwapFailed(Exception):
    """The MOVE ran but this build's ABox is not in the active graph; sending it again cannot help."""


_GIVE_UP: tuple[type[Exception], ...] = (store.LockLost, _Superseded, _SwapFailed)


@dataclass
class Deps:
    """The build's external calls; ``None`` means the Bedrock / AOSS default (tests inject fakes)."""

    read_lexical: Callable[..., m.LexicalSnapshot] = read_lexical
    embed: matcher.Embed | None = None
    judge: matcher.Judge | None = None
    extract_judge: matcher.Judge | None = None
    search: extract.Search | None = None
    embed_query: Callable[[str], list[float]] | None = None


def _default_deps() -> Deps:
    return Deps()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _Beat:
    """Heartbeats ``ABOXLOCK`` at most once per ``BEAT_SECONDS``; raises ``store.LockLost`` once it is gone.

    A heartbeat that fails is logged and tried again at the next beat, as A's ``_checkpoint`` does:
    the lease outlives many missed beats, and the fenced writes still catch a lock that was lost.
    Once none has succeeded for ``_LEASE_GIVE_UP_SECONDS`` the build gives up (``store.LockLost``),
    so it never runs on with a lease another worker may take over (the swap holds that back while
    a MOVE is in doubt, and renews the lease itself before it sends a MOVE).
    """

    def __init__(self, namespace: str, lock_id: str) -> None:
        self.namespace = namespace
        self.lock_id = lock_id
        self._last: float | None = None
        self._renewed = time.monotonic()

    def __call__(self) -> None:
        now = time.monotonic()
        if self._last is not None and now - self._last < BEAT_SECONDS:
            return
        self._last = now  # a failed heartbeat is throttled too
        try:
            alive = store.heartbeat(self.namespace, self.lock_id)
        except Exception as e:
            if now - self._renewed >= _LEASE_GIVE_UP_SECONDS:
                _log.error("abox[%s]: no heartbeat for %.0fs; giving the lease up", self.namespace, now - self._renewed)
                raise store.LockLost(self.lock_id) from e
            _log.warning("abox[%s]: heartbeat failed; tried again at the next beat", self.namespace, exc_info=True)
            return
        if not alive:
            raise store.LockLost(self.lock_id)
        self._renewed = now

    def check(self) -> None:
        """Read the lock now, unthrottled; raises ``store.LockLost`` unless this build holds a live lease.

        Thread-safe. A read, not a heartbeat: a heartbeat that fails is tolerated (see above), and a
        check only has to say whether the lock was lost.
        """
        if store.lock_holder(self.namespace) != self.lock_id:
            raise store.LockLost(self.lock_id)


def _checked[T](beat: _Beat, write: Callable[[], T]) -> T:
    """``write`` sent right after ``beat.check()``: an S3 or Neptune write cannot be fenced on the lock."""
    beat.check()
    return write()


def _retry[T](what: str, fn: Callable[[], T], beat: Callable[[], None]) -> T:
    """``fn`` retried like a deploy step, beating between attempts; a ``_GIVE_UP`` error is final."""
    return deploy._retry(what, fn, give_up=_GIVE_UP, before_sleep=beat)


# ── Requests and the worker ─────────────────────────────────────────────────


def _has_ontology(namespace: str) -> bool:
    """Whether the namespace has an ACTIVE row or any VERSION row, i.e. it is not being torn down."""
    return registry.get_active(namespace) is not None or bool(registry.list_versions(namespace))


def _requeue(namespace: str, reason: str) -> None:
    """Record a request again unless the namespace is gone, so a purge never leaves an orphan ``ABOXREQ``.

    Retried (both calls are idempotent): a lost re-queue would strand the build it stands for.
    """

    def once() -> None:
        if _has_ontology(namespace):
            store.request(namespace, reason)

    deploy._retry(f"abox[{namespace}] re-queue", once)


def request_build(namespace: str, reason: str) -> bool:
    """Record a build request and start a worker unless a live build holds the lock (that build picks it up).

    Returns False, recording nothing, when the namespace has neither an ACTIVE row nor a VERSION
    row (it is being torn down). A build that finds no active version clears the ABox.
    """
    if not _has_ontology(namespace):
        return False
    store.request(namespace, reason)
    _start_worker(namespace)
    return True


def _start_worker(namespace: str, restarts: int = 0) -> None:
    lock_id = store.new_build_id()
    if store.acquire_lock(namespace, lock_id):
        Thread(target=_worker, args=(namespace, lock_id, restarts), daemon=True, name=f"abox-{namespace}").start()


@dataclass
class _FollowUps:
    """The builds a worker run may still queue to reconcile an ABox a failed build left unsettled.

    One per unsettled streak, so a build that keeps failing that way cannot loop: a build that
    settles the ABox (READY or EMPTY) gives it back.
    """

    left: int = 1


def _worker(namespace: str, lock_id: str, restarts: int = 0) -> None:
    lost = crashed = False
    follow_ups = _FollowUps()
    try:
        # take_pending is fenced on ABOXLOCK: a worker that lost its lease never eats a request.
        while store.take_pending(namespace, lock_id):
            try:
                if run_build(namespace, lock_id, _default_deps(), follow_ups) in (store.READY, store.EMPTY):
                    follow_ups.left = 1
            except store.LockLost:
                lost = True
                break
            except Exception:
                _log.exception("abox[%s]: build failed", namespace)
    except store.LockLost:
        lost = True
    except Exception:
        crashed = True  # take_pending failed, so the request it read is still pending
        _log.exception("abox[%s]: build worker failed", namespace)
    try:
        store.release_lock(namespace, lock_id)
    except Exception:
        _log.exception(
            "abox[%s]: could not release the build lock; it expires after %ds", namespace, store.STALE_SECONDS
        )
    try:
        if lost:
            # Re-arm so the request this worker may have been serving is not lost; the new
            # holder (or a fresh worker below) picks it up.
            _log.warning("abox[%s]: lost the build lock; re-queuing", namespace)
            _requeue(namespace, "build lock lost")
        # A request that came after the last take_pending found the lock still held.
        if store.has_pending(namespace):
            if crashed and restarts >= _MAX_RESTARTS:
                _log.error("abox[%s]: build workers keep failing; the request waits for recovery", namespace)
                return
            if crashed:
                time.sleep(min(_RESTART_BACKOFF_SECONDS * 2**restarts, _RESTART_BACKOFF_MAX_SECONDS))
            _start_worker(namespace, restarts + 1 if crashed else 0)
    except Exception:
        _log.exception("abox[%s]: could not re-check the pending build request", namespace)


def recover_if_stalled(namespace: str) -> bool:
    """Re-queue a build whose worker died (``store.stalled``). Called on reads."""
    if store.stalled(namespace):
        return request_build(namespace, "recover a stopped build")
    return False


def stop_builds(namespace: str) -> float:
    """Stop the namespace's builds for good (namespace teardown); return the seconds to wait before deleting.

    The teardown takes ``ABOXLOCK`` (``store.fence_teardown``): a request then starts no worker,
    and a running build checks the lock right before each write, so it sends none after that.
    The wait outlasts the writes it sent before, as long as a MOVE in doubt is waited out
    (longer than Neptune's query timeout); it is zero when no build was running.
    """
    return float(store.fence_teardown(namespace, deploy._AMBIGUOUS_MOVE_WAIT_SECONDS))


# ── One build ───────────────────────────────────────────────────────────────


def _drop(graph: str) -> None:
    try:
        _sparql_update(f"DROP SILENT GRAPH {_iri(graph)}")
    except Exception:
        _log.warning("abox: could not drop %s", graph, exc_info=True)


def _leftover_builds(state: dict[str, Any]) -> list[str]:
    """The builds whose staging graph a state row may have left behind (``build_id`` and ``last_build_id``)."""
    return sorted({str(state[k]) for k in ("build_id", "last_build_id") if state.get(k)})


def _count(graph: str) -> int:
    res = _sparql_query(f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH {_iri(graph)} {{ ?s ?p ?o }} }}")
    bindings = res.get("results", {}).get("bindings", [])
    return int(bindings[0]["n"]["value"]) if bindings else 0


def _load(staging: str, nt: str, beat: _Beat, label: str) -> None:
    """Load canonical N-Triples into ``staging`` in batches and check that every triple landed."""
    _retry(f"{label} clear staging", partial(_sparql_update, f"DROP SILENT GRAPH {_iri(staging)}"), beat)
    # Split on "\n" only, as canonical_nt does: str.splitlines() also breaks on U+2028 and
    # others that N-Triples leaves unescaped inside a literal.
    lines = [line + "\n" for line in nt.split("\n") if line]
    for n, start in enumerate(range(0, len(lines), _LOAD_BATCH_LINES), 1):
        # A POST repeated after a lost reply is harmless: a graph holds each triple once.
        batch = "".join(lines[start : start + _LOAD_BATCH_LINES])
        post = partial(_gsp_post_turtle, staging, batch, retry_oom=False)  # resent only after a lock check
        _retry(f"{label} load batch {n}", partial(_checked, beat, post), beat)
        beat()
    got = _retry(f"{label} count staging", partial(_count, staging), beat)
    if got != len(lines):
        raise RuntimeError(f"staging graph holds {got} triples, expected {len(lines)}")


def _holds_header(graph: str, namespace: str, build_id: str) -> bool:
    """Whether ``graph`` holds the header triple that names ``build_id``."""
    header = f'{_iri(m.header_iri(namespace))} {_iri(m.BUILD_ID)} "{_esc(build_id)}"'
    return bool(_sparql_query(f"ASK {{ GRAPH {_iri(graph)} {{ {header} }} }}").get("boolean"))


def _landed(namespace: str, build_id: str) -> bool:
    """Whether the active graph's header names ``build_id``, i.e. this build's MOVE went through."""
    return _holds_header(store.graph_uri(namespace), namespace, build_id)


def _may_have_run(e: Exception) -> bool:
    """Whether a MOVE that raised ``e`` may have reached Neptune, so it may run or have run.

    An error status means Neptune answered and rolled it back. A connection that was never made,
    no free pooled connection, or a failure before the request went out (signing it, say) means it
    never left. Any other transport error (a read timeout, a dropped connection) leaves it in doubt.
    """
    if isinstance(e, httpx.HTTPStatusError | httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout):
        return False
    return isinstance(e, httpx.HTTPError)


@dataclass
class _SwapState:
    """What the swap knows across its attempts, which ``run_build`` needs once it gave up."""

    # A MOVE may have reached Neptune and whether it landed is unknown: it is not sent again,
    # and the staging graph is not dropped, until this is resolved.
    in_doubt: bool = False
    # That MOVE may still be running, so it is waited out before its outcome is looked for.
    running: bool = False
    # The swap gave up while the active graph may not hold what the state will say: a MOVE in
    # doubt may have landed, or one that ran left the active graph without this build's header.
    unsettled: bool = False


def _swap(
    namespace: str,
    lock_id: str,
    version: int,
    staging: str,
    build_id: str,
    beat: _Beat,
    label: str,
    state: _SwapState,
) -> None:
    """MOVE ``staging`` over the active graph once, fenced on the active version and the lock.

    A MOVE that never reached Neptune, or that Neptune refused, may be sent again (``_may_have_run``).
    Any other failure leaves it in doubt: it may still be running, and a second MOVE after it lands
    would move the emptied staging graph over the ABox. The next attempt first waits longer than
    Neptune's query timeout (not when the MOVE returned), then looks for this build's header: in
    the active graph, the MOVE landed; still in staging, it never ran; in neither, staging is gone
    and moving it would empty the active graph, so the swap fails (``_SwapFailed``). Until then
    neither fence is checked and a lost lock is held back. When the attempts run out in doubt, the
    swap looks once more before it gives up. A MOVE that returns is trusted only once the header is
    in the active graph: moving an emptied staging graph returns too.
    """

    def pulse() -> None:
        try:
            beat()
        except store.LockLost:
            if not state.in_doubt:
                raise
            # Giving up now would drop the staging graph the MOVE may still read. Once the doubt is
            # resolved, the lock fence before a MOVE, or the fenced READY write, finds the lock gone.

    def landed_once_over() -> bool:
        if state.running:
            deploy._outwait_in_flight_move(pulse)  # longer than Neptune's query timeout
            state.running = False
        return _landed(namespace, build_id)

    def once() -> None:
        if state.in_doubt:
            if landed_once_over():
                state.in_doubt = False
                return
            # It is over and did not land. Until staging is known to be intact, it stays in doubt.
            if not _holds_header(staging, namespace, build_id):
                state.in_doubt = False
                raise _SwapFailed(f"staging graph {staging} is gone but the active graph lacks build {build_id}")
            state.in_doubt = False  # it never ran: staging is intact
        if (registry.get_active(namespace) or {}).get("version") != version:
            raise _Superseded
        # Renewed, not just read: the MOVE then has a full lease to settle in before another worker
        # could take the lock and drop this staging graph. A failure here sent nothing, so it is retried.
        if not store.heartbeat(namespace, lock_id):
            raise store.LockLost(lock_id)
        state.in_doubt = state.running = True
        try:
            # Sent once: a MOVE Neptune refused for memory is resent by the retry, after the fences.
            _sparql_update(f"MOVE GRAPH {_iri(staging)} TO GRAPH {_iri(store.graph_uri(namespace))}", retry_oom=False)
        except Exception as e:
            if not _may_have_run(e):
                state.in_doubt = state.running = False
            raise
        state.running = False  # it returned; a failure of the check below is checked again, with no wait
        landed = _landed(namespace, build_id)
        state.in_doubt = False
        if not landed:
            raise _SwapFailed(f"the MOVE returned but the active graph lacks build {build_id}")

    try:
        _retry(f"{label} swap", once, pulse)
    except Exception as e:
        if not state.in_doubt:
            state.unsettled = isinstance(e, _SwapFailed)
            raise
        state.unsettled = True
        if not _retry(f"{label} check the swap", landed_once_over, pulse):
            state.in_doubt = False  # it is over and did not land
            raise
        state.in_doubt = state.unsettled = False


def _with_header(body: str, namespace: str, build_id: str, built_at: str) -> str:
    """Canonical N-Triples ``body`` with the header's ``buildId`` and ``builtAt`` lines merged in, still sorted.

    The two lines are not in ``body``, which the checksum covers, so the same inputs keep the same
    ``ntSha256``. The result equals ``canonical_nt`` of the graph with them added (no blank nodes).
    """
    header = URIRef(m.header_iri(namespace))
    extra = Graph()
    extra.add((header, URIRef(m.BUILD_ID), Literal(build_id)))
    extra.add((header, URIRef(m.BUILT_AT), Literal(built_at, datatype=XSD.dateTime)))
    lines = [line for line in (body + extra.serialize(format="nt")).split("\n") if line.strip()]
    return "\n".join(sorted(lines)) + "\n"


def _restore(namespace: str, lock_id: str, previous: dict[str, Any] | None, **overrides: Any) -> None:
    """Write back the state this build started from (``previous``), with ``overrides``.

    ``previous`` is None when the build failed before it read the state, so before it changed it:
    the overrides then go onto the row as it stands. A ``BUILDING`` status in ``previous`` belongs
    to a build that stopped before this one took the lock, so it is not written back: the row says
    ``READY`` when it holds a built ABox, else ``EMPTY``.
    """
    if previous is None:
        store.update_state(namespace, fence_build_id=lock_id, **overrides)
        return
    kept = {k: v for k, v in previous.items() if k not in ("updated_at", "building_version")}
    if kept.get("status") == store.BUILDING:
        kept["status"] = store.READY if kept.get("conforms_to_version") is not None else store.EMPTY
    fields = {"status": store.EMPTY, **kept, **overrides}
    store.put_state(namespace, fence_build_id=lock_id, **fields)


def _effective(namespace: str, tbox: TBox, beat: _Beat, label: str) -> dict[str, mapping.MappingRow]:
    raw = _retry(f"{label} read the mapping", partial(mapping.list_rows, namespace), beat)
    eff = mapping.effective(raw, tbox)
    changed = [e for e, r in zip(eff, raw, strict=True) if e.status != r.status]
    if changed:
        save = partial(mapping.persist_status, namespace, changed, fence_build_id=beat.lock_id)
        _retry(f"{label} save the mapping statuses", save, beat)
    return {r.id: r for r in eff}


def _used_classes(lex: m.LexicalSnapshot) -> set[str]:
    used: set[str] = set()
    for f in lex.facts:
        for eid in (f.subject_id, f.object_id):
            e = lex.entities.get(eid) if eid else None
            if e and e.cls and e.cls != m.LOCAL_ENTITY_CLASS:
                used.add(e.cls)
    return used


def _update_mapping(
    namespace: str,
    tbox: TBox,
    lex: m.LexicalSnapshot,
    version: int,
    deps: Deps,
    used: set[str],
    beat: _Beat,
    label: str,
) -> dict[str, mapping.MappingRow]:
    embed = deps.embed or matcher.bedrock_embed()

    def embed_and_beat(texts: list[str]) -> list[list[float]]:
        vectors = _retry(f"{label} embed", partial(embed, texts), beat)
        beat()
        return vectors

    def save(rows: list[mapping.MappingRow]) -> None:
        # Saved as each batch is decided, so a build cut short keeps the decisions it paid for.
        for row in rows:
            write = partial(mapping.save_auto, namespace, row, fence_build_id=beat.lock_id)
            _retry(f"{label} save a mapping row", write, beat)
        beat()

    emb = matcher.Embeddings(embed_and_beat)
    judge = deps.judge or matcher.bedrock_judge()
    existing = {r.id: r for r in _retry(f"{label} read the mapping", partial(mapping.list_rows, namespace), beat)}
    # A few entity values per class make its prompt clearer; they never reach the ranking or candidate_hash.
    values: dict[str, set[str]] = {cls: set() for cls in used}
    for e in lex.entities.values():
        if e.cls in values and e.value:
            values[e.cls].add(e.value)
    samples = {cls: sorted(v)[: matcher.MAX_SAMPLES] for cls, v in values.items()}
    matcher.decide(matcher.class_items(samples, tbox, emb), existing, judge=judge, version=version, on_batch=save)
    rows = _effective(namespace, tbox, beat, label)
    classes = {r.key[0]: r.target for r in rows.values() if r.kind == mapping.CLASS and r.status == mapping.ACTIVE}
    relation_keys: set[tuple[str, str, str]] = set()
    attribute_keys: set[tuple[str, str]] = set()
    for f in lex.facts:
        s = lex.entities.get(f.subject_id)
        o = lex.entities.get(f.object_id) if f.object_id else None
        if s is None or o is None or s.cls not in classes:
            continue
        if o.cls == m.LOCAL_ENTITY_CLASS:
            attribute_keys.add((classes[s.cls], f.relation))
        elif o.cls in classes:
            relation_keys.add((f.relation, classes[s.cls], classes[o.cls]))
    matcher.decide(matcher.relation_items(relation_keys, tbox, emb), rows, judge=judge, version=version, on_batch=save)
    matcher.decide(
        matcher.attribute_items(attribute_keys, tbox, emb), rows, judge=judge, version=version, on_batch=save
    )
    return _effective(namespace, tbox, beat, label)


def _reextract(
    namespace: str,
    tbox: TBox,
    rows: dict[str, mapping.MappingRow],
    deps: Deps,
    used: set[str],
    beat: _Beat,
    label: str,
) -> tuple[list[m.XEntity], list[m.XRelation]]:
    populated = {
        r.target for r in rows.values() if r.kind == mapping.CLASS and r.status == mapping.ACTIVE and r.key[0] in used
    }
    target = extract.select_targets(tbox, populated)
    if not target.classes:
        return [], []
    search = deps.search or extract.default_search()
    embed_query = deps.embed_query or extract.default_embed_query()

    def embed_query_retried(text: str) -> list[float]:
        return _retry(f"{label} embed a class query", partial(embed_query, text), beat)

    def search_and_beat(index: str, vector: list[float], k: int) -> list[dict]:
        # chunk_candidates runs one embedding and one k-NN search per target class.
        hits = _retry(f"{label} search the chunks", partial(search, index, vector, k), beat)
        beat()
        return hits

    chunks = extract.chunk_candidates(namespace, tbox, target, embed_query=embed_query_retried, search=search_and_beat)
    if not chunks:
        return [], []
    judge = deps.extract_judge or matcher.bedrock_judge(extract.MODEL_ID)

    def check_before_cache() -> None:  # on a worker thread, so retried without a beat (not thread-safe)
        deploy._retry(f"{label} check the lock", beat.check, give_up=_GIVE_UP)

    return extract.run(namespace, chunks, tbox, target, judge=judge, on_progress=beat, before_cache=check_before_cache)


def run_build(namespace: str, lock_id: str, deps: Deps | None = None, follow_ups: _FollowUps | None = None) -> str:
    """Build the ABox of the active version under ``lock_id`` and swap it in; returns the outcome status.

    Raises ``store.LockLost`` when the lock was taken over (nothing is swapped after that) and
    re-raises any other failure after recording it as ``FAILED`` with the previous ABox kept.
    Once the new ABox is live the state says ``READY``, never ``FAILED``. A failure after which
    the graph may not match what ``FAILED`` records (a swap that did not settle, a clear cut
    short) also queues one more build to reconcile it, at most once per unsettled streak (``follow_ups``).
    """
    deps = deps or _default_deps()
    follow_ups = follow_ups or _FollowUps()
    beat = _Beat(namespace, lock_id)
    build_id = store.new_build_id()
    staging = store.staging_uri(namespace, build_id)
    label = f"abox[{namespace}/{build_id}]"
    previous: dict[str, Any] | None = None  # the state this build started from, once read
    swap = _SwapState()
    moved = clearing = False
    ready: dict[str, Any] = {}
    try:
        active = _retry(f"{label} read the active version", partial(registry.get_active, namespace), beat)
        version = (active or {}).get("version")
        if version is None:
            clearing = True
            _retry(f"{label} clear", partial(clear_abox, namespace, fence_build_id=lock_id), beat)
            return store.EMPTY
        version = int(version)
        previous = _retry(f"{label} read the state", partial(store.get_state, namespace), beat) or {}
        _retry(
            f"{label} mark BUILDING",
            partial(
                store.update_state,
                namespace,
                fence_build_id=lock_id,
                status=store.BUILDING,
                build_id=build_id,
                building_version=version,
            ),
            beat,
        )
        for leftover in _leftover_builds(previous):  # a crashed or failed build's staging graph
            stale = store.staging_uri(namespace, leftover)
            _retry(f"{label} drop {stale}", partial(_sparql_update, f"DROP SILENT GRAPH {_iri(stale)}"), beat)
        ttl = _retry(
            f"{label} read the TBox", partial(storage.require_version_file, namespace, version, "ontology.ttl"), beat
        )
        tbox = load_tbox(ttl)
        beat()
        lex = _retry(f"{label} read the lexical graph", partial(deps.read_lexical, namespace, on_progress=beat), beat)
        beat()
        used = _used_classes(lex)
        rows = _update_mapping(namespace, tbox, lex, version, deps, used, beat, label)
        beat()
        extracted = _reextract(namespace, tbox, rows, deps, used, beat, label)
        beat()
        revision = mapping.revision(rows.values())
        graph, stats = assemble(namespace, tbox, lex, rows, extracted, version=version, mapping_revision=revision)
        body = canonical_nt(graph)
        built_at = _now()
        nt = _with_header(body, namespace, build_id, built_at)
        ready = {
            "status": store.READY,
            "conforms_to_version": version,
            "build_id": build_id,
            "built_at": built_at,
            "counts": stats.counts,
            "class_counts": [
                {"class_iri": c, "label": tbox.classes[c].label, "count": n}
                for c, n in sorted(stats.class_counts.items())
            ],
            "mapping_revision": revision,
            "nt_sha256": sha256_hex(body),
        }
        # Archived before the swap, so a live ABox always has its artifact.
        key = store.build_key(namespace, build_id)
        archive = partial(store.put_text, key, nt, "application/n-triples")
        _retry(f"{label} archive", partial(_checked, beat, archive), beat)
        beat()
        _load(staging, nt, beat, label)
        beat()
        _swap(namespace, lock_id, version, staging, build_id, beat, label, swap)
        moved = True
        _retry(f"{label} mark READY", partial(store.put_state, namespace, fence_build_id=lock_id, **ready), beat)
        try:  # only once READY: the live ABox's archive is then this build's
            store.prune_builds(namespace, current=build_id)
        except Exception:
            _log.warning("%s: could not prune old build archives; the next build tries again", label, exc_info=True)
        _log.info("%s: v%d ABox ready (%s)", label, version, stats.counts)
        return store.READY
    except _Superseded:
        _drop(staging)
        _requeue(namespace, "superseded by a newer ontology version")
        # last_build_id keeps track of the staging graph in case the drop above failed.
        restore = partial(_restore, namespace, lock_id, previous, last_build_id=build_id)
        _retry(f"{label} restore the state", restore, beat)
        _log.info("%s: superseded; rebuilding on the new version", label)
        return SUPERSEDED
    except store.LockLost:
        if not moved:
            _drop(staging)
        raise
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        if moved:
            # The new ABox is live: it is READY whatever happened to the state write.
            noted = {**ready, "last_error": f"The READY state write failed: {error}"[:1000], "last_error_at": _now()}
            _retry(f"{label} mark READY", partial(store.put_state, namespace, fence_build_id=lock_id, **noted), beat)
            _log.warning("%s: v%d ABox is live but its READY write failed (%s); noted", label, version, error)
            return store.READY
        if not swap.in_doubt:  # a MOVE in doubt may still read the staging graph; last_build_id tracks it
            _drop(staging)
        if (swap.unsettled or clearing) and follow_ups.left:
            # Queued before FAILED is written: should that write fail, BUILDING still lets a read recover it.
            follow_ups.left -= 1
            _requeue(namespace, "reconcile the ABox after an unsettled build")
        failed = {
            "status": store.FAILED,
            "last_error": error[:1000],
            "last_error_at": _now(),
        }
        if not clearing:  # a clear makes no staging graph, and last_build_id may name one a build kept
            failed["last_build_id"] = build_id
        _retry(f"{label} mark FAILED", partial(_restore, namespace, lock_id, previous, **failed), beat)
        raise


# ── Clean-up used by deletes ────────────────────────────────────────────────


def clear_abox(namespace: str, *, fence_build_id: str | None = None) -> None:
    """Mark the ABox EMPTY and drop its graph (the domain ontology was deleted). Idempotent.

    A build passes its lock as ``fence_build_id``. The fenced state write goes first, so a build
    that lost the lock raises ``store.LockLost`` before it touches the graph. The EMPTY row keeps
    ``build_id`` and ``last_build_id``: a staging graph a build left behind (one whose MOVE was in
    doubt, say) is not dropped here, and the next build or ``drop_graphs`` still finds it.
    """
    state = store.get_state(namespace) or {}
    tracked = {k: state[k] for k in ("build_id", "last_build_id") if state.get(k)}
    store.put_state(namespace, fence_build_id=fence_build_id, status=store.EMPTY, **tracked)
    _sparql_update(f"DROP SILENT GRAPH {_iri(store.graph_uri(namespace))}")


def drop_graphs(namespace: str) -> None:
    """Best-effort drop of the active graph and any staging graph a build left (namespace deletion)."""
    _drop(store.graph_uri(namespace))
    for build_id in _leftover_builds(store.get_state(namespace) or {}):
        _drop(store.staging_uri(namespace, build_id))
