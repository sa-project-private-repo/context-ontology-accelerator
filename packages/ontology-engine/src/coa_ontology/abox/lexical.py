# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Read the namespace's graphrag lexical graph over openCypher (spec §6.3 step 3).

Facts are read with their provenance path (Fact, Statement, Chunk, Source), keeping only
current Source versions (graphrag's open ``valid_to``). Pages are cut on fact ids first so
a fact with several supporting chunks never straddles a page.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from coa_common.constants import to_graphrag_tenant_id

from coa_ontology.inducer.unstructured.stores.na_lexical import (
    _extract_spc_predicate_complement,
    _extract_spo_predicate,
)
from coa_ontology.inducer.unstructured.stores.ndb_lexical import NeptuneDatabaseLexicalStore

from .model import CURRENT_VALID_TO, LOCAL_ENTITY_CLASS, LexEntity, LexFact, LexicalSnapshot, Provenance

# Fact ids come back in pages of ``PAGE_SIZE``; each page's details are fetched ``BATCH_SIZE`` ids at a time,
# which keeps a single openCypher request small and gives the caller a progress tick per batch.
PAGE_SIZE = int(os.getenv("ABOX_LEXICAL_PAGE", "20000"))
BATCH_SIZE = int(os.getenv("ABOX_LEXICAL_BATCH", "500"))

_IDS = "MATCH (f:{fact}) WHERE id(f) > $after RETURN id(f) AS fid ORDER BY fid LIMIT $limit"
# ``src.doc_source_id`` and ``src.source_s3_key`` are the preprocessing sidecar fields; graphrag's
# SourceGraphBuilder copies every sidecar key onto the ``__Source__`` node under the same name.
# ``f.relation`` is graphrag's own predicate property, but 3.18.7 never fills it (see ``_relation``),
# so ``f.value`` ("<subject> <PREDICATE> <object>") is read as well.
_DETAILS = """
MATCH (subj:{entity})-[:`__SUBJECT__`]->(f:{fact})-[:`__SUPPORTS__`]->(:{statement})
      -[:`__MENTIONED_IN__`]->(c:{chunk})-[:`__EXTRACTED_FROM__`]->(src:{source})
WHERE id(f) IN $ids AND coalesce(src.`__aws__versioning__valid_to__`, $current) = $current
OPTIONAL MATCH (obj:{entity})-[:`__OBJECT__`]->(f)
RETURN id(f) AS fid, f.relation AS rel, f.value AS fval, id(subj) AS sid, subj.value AS sval, subj.class AS scls,
       id(obj) AS oid, obj.value AS oval, obj.class AS ocls, id(c) AS cid,
       src.doc_source_id AS ds, src.source_s3_key AS skey
"""
_CHUNKS = "MATCH (c:{chunk}) WHERE id(c) IN $ids RETURN id(c) AS id, c.value AS text"


def _endpoint() -> tuple[str, int]:
    raw = os.getenv("NEPTUNE_ENDPOINT") or os.getenv("NDB_ENDPOINT") or ""
    if not raw:
        raise RuntimeError("NEPTUNE_ENDPOINT (or NDB_ENDPOINT) is not set: cannot read the lexical graph")
    raw = raw.split("://", 1)[-1].rstrip("/")
    if ":" in raw:
        host, port = raw.rsplit(":", 1)
        return host, int(port)
    return raw, 8182


def lexical_store(namespace: str) -> NeptuneDatabaseLexicalStore:
    """The openCypher store of the namespace's lexical graph, scoped by its graphrag tenant id."""
    host, port = _endpoint()
    return NeptuneDatabaseLexicalStore(
        endpoint=host,
        port=port,
        region=os.getenv("AWS_REGION", os.getenv("NA_REGION", "us-east-1")),
        tenant_id=to_graphrag_tenant_id(namespace),
    )


def _labels(store: NeptuneDatabaseLexicalStore) -> dict[str, str]:
    return {
        k: store._label(base)
        for k, base in (
            ("fact", "Fact"),
            ("entity", "Entity"),
            ("statement", "Statement"),
            ("chunk", "Chunk"),
            ("source", "Source"),
        )
    }


def _predicate_and_complement(r: dict) -> tuple[str, str | None]:
    """The fact's predicate and, for an attribute fact, its complement text.

    The predicate is graphrag's ``relation`` when stored, else the one parsed from the fact text.
    graphrag-lexical-graph 3.18.7 (the pinned version) runs ``SET fact.relation = params.p`` without
    ever passing ``p``, so the property is normally absent. The inducer reads the predicate back from
    ``Fact.value`` for the same reason (``NeptuneDatabaseLexicalStore._get_facts_batch``).

    The complement entity and its ``__OBJECT__`` edge exist only when graphrag ran with
    ``include_local_entities`` (off by default), so an attribute fact normally has no object row and
    its value is the text that follows the predicate.
    """
    rel = str(r["rel"]) if r.get("rel") else ""
    fval, sval = r.get("fval"), r.get("sval")
    if not (fval and sval):
        return rel, None
    if r.get("oid") and r.get("oval"):
        return rel or _extract_spo_predicate(str(fval), str(sval), str(r["oval"])), None
    predicate, complement = _extract_spc_predicate_complement(str(fval), str(sval))
    return rel or predicate, complement


def _collect(r: dict, entities: dict[str, LexEntity], facts: dict[tuple[str, str, str, str], LexFact]) -> None:
    rel, complement = _predicate_and_complement(r)
    if not (rel and r.get("sid") and r.get("sval") and r.get("cid")):
        return
    fid, sid, cid = str(r["fid"]), str(r["sid"]), str(r["cid"])
    entities.setdefault(sid, LexEntity(sid, str(r["sval"]), str(r.get("scls") or "")))
    oid: str | None = None
    if r.get("oid") and r.get("oval"):
        oid = str(r["oid"])
        entities.setdefault(oid, LexEntity(oid, str(r["oval"]), str(r.get("ocls") or "")))
    elif complement:
        # One complement entity per fact. Its subjects can split the fact text differently, so the
        # smallest complement wins and the result does not depend on the order the rows arrive in.
        oid = f"{fid}#c"
        prev_complement = entities.get(oid)
        if prev_complement is None or complement < prev_complement.value:
            entities[oid] = LexEntity(oid, complement, LOCAL_ENTITY_CLASS)
    prov = Provenance(cid, str(r.get("ds") or ""), str(r.get("skey") or ""))
    # A fact id hashes only the fact text, so one node can have several subject and object edges:
    # each (subject, object) pair of a chunk is its own fact row. The smallest provenance wins a tie,
    # so the result does not depend on the order the rows arrive in.
    key = (fid, cid, sid, oid or "")
    prev = facts.get(key)
    if prev is None or prov < prev.prov:
        facts[key] = LexFact(fid, sid, rel, oid, prov)


def read_lexical(
    namespace: str,
    *,
    store: NeptuneDatabaseLexicalStore | None = None,
    on_progress: Callable[[], None] | None = None,
) -> LexicalSnapshot:
    """All entities and facts of the namespace's current sources, each fact once per supporting chunk.

    ``on_progress`` is called after every ids query and every details batch so a long read can
    heartbeat its lease.
    """
    st = store or lexical_store(namespace)
    labels = _labels(st)
    ids_query = _IDS.format(**labels)
    details_query = _DETAILS.format(**labels)
    entities: dict[str, LexEntity] = {}
    facts: dict[tuple[str, str, str, str], LexFact] = {}
    after = ""
    while True:
        ids = [str(r["fid"]) for r in st._oc_query(ids_query, {"after": after, "limit": PAGE_SIZE})]
        if on_progress:
            on_progress()
        for start in range(0, len(ids), BATCH_SIZE):
            batch = ids[start : start + BATCH_SIZE]
            for r in st._oc_query(details_query, {"ids": batch, "current": CURRENT_VALID_TO}):
                _collect(r, entities, facts)
            if on_progress:
                on_progress()
        if len(ids) < PAGE_SIZE:  # a short (or empty) page is the last one
            break
        after = ids[-1]
    return LexicalSnapshot(dict(sorted(entities.items())), tuple(facts[k] for k in sorted(facts)))


def chunk_texts(
    namespace: str, chunk_ids: list[str], *, store: NeptuneDatabaseLexicalStore | None = None
) -> dict[str, str]:
    """The text of each chunk id, read from the lexical graph in batches."""
    if not chunk_ids:
        return {}
    st = store or lexical_store(namespace)
    query = _CHUNKS.format(**_labels(st))
    ids = sorted(set(chunk_ids))
    texts: dict[str, str] = {}
    for start in range(0, len(ids), BATCH_SIZE):
        for r in st._oc_query(query, {"ids": ids[start : start + BATCH_SIZE]}):
            texts[str(r["id"])] = str(r.get("text") or "")
    return texts
