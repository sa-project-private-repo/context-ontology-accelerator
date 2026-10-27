# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The designer query console: SPARQL over the draft and the document ABox, Cypher over the lexical graph.

A SPARQL query reads the draft's current revision, which is
loaded on first use into its own named graph ``{base}/_studio/{ns}/{proposalId}/r{revision}``.
Neptune makes a blank node new in every document posted, so the load writes each blank node of the
revision as the IRI ``{graph}/genid/{label}`` (its labels are canonical), and results show those
IRIs as blank nodes again; a query's ``isBlank``, ``isIRI`` and ``isURI`` are rewritten to answer
for them as for blank nodes (``prepare_sparql``). Revisions never change, so two
loads of one revision write the same triples and loads of two revisions never meet. A marker triple
ends each load, so a graph without it is loaded again. The
``STUDIOGRAPH`` row of the proposal names the graph loaded last (``graph``, ``revision``) and every
graph a load started and nobody dropped yet (``graphs``), so a failed or abandoned load is never
lost track of. A graph leaves the row only once its DROP succeeded.

Recording a newer revision does not drop the older graphs: another query may still be loading or
reading one. It marks them ``superseded`` (graph to the time it was first marked), and each later
recording drops those superseded for longer than ``SUPERSEDED_GRACE_SECONDS``. Accept, cancel,
reject and namespace deletion drop every graph at once and leave the row ``closed``: a load still
running cannot track or record a graph in it, so it drops what it wrote. A query whose graph was
dropped while it ran answers an error rather than what it read.

Each query runs with a 20 s budget and no retry, and returns at most ``MAX_ROWS`` rows. Neptune's
error text is cut to one line, and the query text is never logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from threading import Thread
from typing import Any, NoReturn
from urllib.parse import urlencode

import httpx
from botocore.exceptions import BotoCoreError, ClientError, ReadTimeoutError
from coa_control_plane_server.models.proposal_status import ProposalStatus
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.term import Node

from coa_ontology import dynamo_store
from coa_ontology.abox import store as abox_store
from coa_ontology.stores import neptune_db_graph as ndb

from . import draft as store
from .query_safety import QueryRefused, prepare_cypher, prepare_sparql

_log = logging.getLogger("studio.queries")
QUERY_SECONDS = float(os.getenv("STUDIO_QUERY_SECONDS", "20"))
# Longer than one load plus one query: a query that started on a superseded graph is over by then.
SUPERSEDED_GRACE_SECONDS = float(os.getenv("STUDIO_SUPERSEDED_GRACE_SECONDS", "120"))
# The namespace purge tries a DROP this often, as nothing will name the graph after it.
DROP_ATTEMPTS = 3
DROP_BACKOFF_SECONDS = 0.5
DRAFT_CHANGED = "The draft changed while the query ran; run it again."
# After these the draft never changes again. An accept still running (accepting, embeddings_sync) is not
# one of them: it may fail, and accept_failed is editable.
REVIEW_OVER = frozenset({ProposalStatus.ACCEPTED.value, ProposalStatus.REJECTED.value, ProposalStatus.CANCELLED.value})
MAX_ROWS = 1000
GRAPH_SK = "STUDIOGRAPH"
LOAD_BATCH_LINES = 5000
MARKER = ndb.WB + "studioRevision"
LANGUAGE_TARGETS = {("sparql", "draft"), ("cypher", "lexical")}
_SELECT_ACCEPT = "application/sparql-results+json"
_GRAPH_ACCEPT = "application/n-triples"
_sleep = time.sleep  # tests patch this to skip the wait


class QueryTimeout(store.StudioError):
    """The query did not finish within the time budget."""

    status = 504
    code = "QueryTimeout"


class QueryFailed(store.StudioError):
    """The graph database could not run the query."""

    status = 502
    code = "QueryFailed"


def _one_line(text: str) -> str:
    return (text.strip().splitlines() or [""])[0][:300]


def _timeout_message() -> str:
    return f"The query did not finish within {QUERY_SECONDS:.0f} seconds; narrow it or add a LIMIT"


# ── Neptune SPARQL ──────────────────────────────────────────────────────────


def _neptune_error(response: httpx.Response) -> store.StudioError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    code = str(body.get("code") or "")
    if code == "TimeLimitExceededException":
        return QueryTimeout(_timeout_message())
    if code in ("MalformedQueryException", "BadRequestException", "InvalidParameterException"):
        detail = _one_line(str(body.get("detailedMessage") or body.get("message") or ""))
        return QueryRefused(f"The graph database refused the query: {detail}" if detail else "The query is invalid")
    return QueryFailed(f"The graph database could not run the query ({code or response.status_code})")


def _post_query(query: str, accept: str) -> httpx.Response:
    """POST one SPARQL query to Neptune, signed, with the console's budget and no retry."""
    url = f"{ndb.NDB_ENDPOINT}/sparql"
    body = urlencode({"query": query})
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": accept}
    headers.update(ndb._sign("POST", url, body))
    try:
        with httpx.Client(timeout=QUERY_SECONDS) as client:
            response = client.post(url, content=body, headers=headers)
    except httpx.TimeoutException as e:
        raise QueryTimeout(_timeout_message()) from e
    except httpx.HTTPError as e:
        raise QueryFailed("The graph database could not be reached") from e
    if response.status_code >= 400:
        raise _neptune_error(response)
    return response


def _ask(query: str) -> bool:
    return bool(_post_query(query, _SELECT_ACCEPT).json().get("boolean"))


# ── The draft graph ─────────────────────────────────────────────────────────


class _ReviewEnded(Exception):
    """The row is closed, or gone with its proposal or namespace: the review ended during a load."""


def draft_graph_uri(namespace: str, proposal_id: str, revision: int) -> str:
    """The named graph of one revision of a draft; outside ``{base}/{ns}/``, where serving looks."""
    store._checked(namespace, proposal_id)
    return f"{ndb._resolve_graph_base()}/_studio/{namespace}/{proposal_id}/r{revision}"


def _row_key(namespace: str, proposal_id: str) -> dict[str, str]:
    return {"PK": dynamo_store._proposal_pk(namespace, proposal_id), "SK": GRAPH_SK}


def _row(key: dict[str, str]) -> dict[str, Any] | None:
    row: dict[str, Any] | None = dynamo_store._get_table().get_item(Key=key, ConsistentRead=True).get("Item")
    return row


def _named(row: dict[str, Any]) -> list[str]:
    """Every graph the row names: loaded last, started or superseded."""
    graphs = {str(g) for g in row.get("graphs") or ()} | {str(g) for g in row.get("superseded") or {}}
    if row.get("graph"):
        graphs.add(str(row["graph"]))
    return sorted(graphs)


def _loaded(graph: str, revision: int) -> bool:
    return _ask(f'ASK {{ GRAPH {ndb._iri(graph)} {{ {ndb._iri(graph)} {ndb._iri(MARKER)} "{revision}" }} }}')


def _genid(graph: str) -> str:
    """The prefix of the IRIs that stand for the blank nodes of the revision in ``graph``."""
    return f"{graph}/genid/"


# In an N-Triples line only the subject and the object can be blank nodes. A literal object ends in its
# quote, a language tag or a datatype IRI, so a line ending in a label has a blank-node object.
_BLANK_SUBJECT = re.compile(r"^_:(\S+) ")
_BLANK_OBJECT = re.compile(r' _:([^\s"]+) \.$')


def _load_lines(graph: str, tbox_nt: str) -> list[str]:
    """The revision's lines with each blank node written as an IRI under ``graph``.

    A blank node posted in two batches, or in two loads, would otherwise be two nodes: the canonical
    text puts every line with a blank-node subject after the lines that point to it.
    """
    prefix = _genid(graph)
    lines = []
    for line in tbox_nt.split("\n"):
        if line.strip():
            line = _BLANK_SUBJECT.sub(lambda m: f"<{prefix}{m.group(1)}> ", line)
            lines.append(_BLANK_OBJECT.sub(lambda m: f" <{prefix}{m.group(1)}> .", line))
    return lines


def _load(graph: str, tbox_nt: str, revision: int) -> None:
    """Post the revision's triples, then the marker. Loading a revision twice writes the same triples."""
    lines = _load_lines(graph, tbox_nt)
    for start in range(0, len(lines), LOAD_BATCH_LINES):
        ndb._gsp_post_turtle(graph, "\n".join(lines[start : start + LOAD_BATCH_LINES]) + "\n", retry_oom=False)
    marker = f'{ndb._iri(graph)} {ndb._iri(MARKER)} "{revision}"'
    ndb._sparql_update(f"INSERT DATA {{ GRAPH {ndb._iri(graph)} {{ {marker} }} }}", retry_oom=False)


def _drop(graph: str, attempts: int = 1) -> bool:
    """DROP ``graph``, trying ``attempts`` times in all; False when no attempt succeeded."""
    for attempt in range(attempts):
        if attempt:
            _sleep(DROP_BACKOFF_SECONDS * attempt)
        try:
            ndb._sparql_update(f"DROP SILENT GRAPH {ndb._iri(graph)}", retry_oom=False)
        except Exception:  # noqa: BLE001 - the graph stays tracked in the row; a later drop takes it
            _log.warning("studio: could not drop the draft graph %s", graph, exc_info=True)
        else:
            return True
    return False


def _track(key: dict[str, str], graph: str) -> None:
    """Note in the row that a load of ``graph`` starts, before anything is written to it.

    The row also gets the ``superseded`` map, which marking a graph needs. Raises
    :class:`_ReviewEnded` when the row is closed.
    """
    try:
        dynamo_store._get_table().update_item(
            Key=key,
            UpdateExpression="ADD graphs :g SET superseded = if_not_exists(superseded, :none)",
            ConditionExpression="attribute_not_exists(closed)",
            ExpressionAttributeValues={":g": {graph}, ":none": {}},
        )
    except ClientError as e:
        if not store._conditional_failed(e):
            raise
        raise _ReviewEnded from e


def _forget(key: dict[str, str], graph: str, *, recorded: bool = False, attempts: int = 1) -> bool:
    """Drop a graph no query needs any more, then stop tracking it; False when the DROP failed.

    ``recorded``: the row names it as the graph loaded last, and that goes too.
    """
    if not _drop(graph, attempts):
        return False
    try:
        dynamo_store._get_table().update_item(
            Key=key,
            UpdateExpression="DELETE graphs :gs REMOVE superseded.#g" + (", graph" if recorded else ""),
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames={"#g": graph},
            ExpressionAttributeValues={":gs": {graph}},
        )
    except ClientError as e:
        if not store._conditional_failed(e):  # a deleted row stays deleted
            raise
    return True


def _supersede(key: dict[str, str], graphs: list[str]) -> dict[str, Any]:
    """Mark ``graphs`` superseded, keeping the time each was first marked; return all the row's marks."""
    names = {f"#g{n}": graph for n, graph in enumerate(graphs)}
    marks = ", ".join(f"superseded.{name} = if_not_exists(superseded.{name}, :now)" for name in names)
    try:
        row = (
            dynamo_store._get_table()
            .update_item(
                Key=key,
                UpdateExpression=f"SET {marks}",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues={":now": store._now()},
                ReturnValues="ALL_NEW",
            )
            .get("Attributes")
            or {}
        )
    except ClientError as e:
        if not store._conditional_failed(e):
            raise
        return {}  # the row went with its proposal or namespace
    return dict(row.get("superseded") or {})


def _sweep(key: dict[str, str], superseded: dict[str, Any]) -> None:
    """Drop the graphs superseded for longer than the grace period: no query reads them any more."""
    cutoff = datetime.now(UTC) - timedelta(seconds=SUPERSEDED_GRACE_SECONDS)
    for graph, since in sorted(superseded.items()):
        if datetime.fromisoformat(str(since)) < cutoff:
            _forget(key, graph)


def _record(key: dict[str, str], graph: str, revision: int) -> bool:
    """Name ``graph`` in the row unless a newer revision is named; mark the older graphs superseded, then sweep.

    A graph that lost to a newer revision is marked superseded itself, and False is returned. Raises
    :class:`_ReviewEnded` when the row is closed or gone.
    """
    try:
        old = (
            dynamo_store._get_table()
            .update_item(
                Key=key,
                UpdateExpression="SET graph = :g, revision = :rev, loaded_at = :at",
                ConditionExpression=(
                    "attribute_exists(PK) AND attribute_not_exists(closed)"
                    " AND (attribute_not_exists(revision) OR revision <= :rev)"
                ),
                ExpressionAttributeValues={":g": graph, ":rev": revision, ":at": store._now()},
                ReturnValues="ALL_OLD",
            )
            .get("Attributes")
            or {}
        )
    except ClientError as e:
        if not store._conditional_failed(e):
            raise
        row = _row(key)
        if row is None or "closed" in row:
            raise _ReviewEnded from e
        _supersede(key, [graph])
        return False
    # The graph recorded before, and loads of older revisions, are no use to a new query, but a query
    # that started earlier may still be loading or reading one. A newer revision's graph is left
    # alone: its load may still be running.
    marked = dict(old.get("superseded") or {})
    older = [g for g in _named(old) if g != graph and g not in marked and _revision_of(g) < revision]
    try:
        _sweep(key, _supersede(key, older) if older else marked)
    except Exception:  # noqa: BLE001 - housekeeping, which the next record does again; this graph is recorded
        _log.warning("studio: could not sweep the superseded draft graphs of %s", key["PK"], exc_info=True)
    return True


def _revision_of(graph: str) -> int:
    tail = graph.rsplit("/r", 1)[-1]
    return int(tail) if tail.isdigit() else -1


def _end_load(namespace: str, proposal_id: str, graph: str | None) -> NoReturn:
    """The row is closed or gone, so the review ended during a load: drop every draft graph, ``graph`` too.

    The drop runs even when reading why fails. Raises the error the proposal's state gives when it gives
    one, :class:`~.draft.NotEditable` otherwise.
    """
    try:
        error, _ = _review_state(namespace, proposal_id)
    finally:
        drop_draft_graph(namespace, proposal_id, loading=graph)
    raise error or store.NotEditable("The review of this draft has ended; its draft can no longer change")


def _track_again(namespace: str, proposal_id: str, key: dict[str, str], graph: str) -> None:
    """Track a graph whose load or record failed: a sweep, or the end of the review, may have forgotten it."""
    try:
        _track(key, graph)
    except _ReviewEnded:
        _end_load(namespace, proposal_id, graph)
    _drop_if_closed(namespace, proposal_id, graph)


def ensure_draft_graph(namespace: str, proposal_id: str, state: store.StudioState) -> tuple[str, bool]:
    """The named graph of the draft's current revision, loaded if it is not yet, and whether it is recorded.

    A graph that is not recorded lost a race to a newer revision and is marked superseded. A failed
    load leaves its graph without the marker, so the next query loads it again, and tracked, so the
    end of the review drops it; dropping it here could break another load of the same revision. A
    load that the end of the review overtakes drops what it wrote and raises the review's error.
    """
    graph = draft_graph_uri(namespace, proposal_id, state.revision)
    key = _row_key(namespace, proposal_id)
    row = _row(key)
    if row and "closed" not in row and row.get("graph") == graph and _loaded(graph, state.revision):
        return graph, True
    tbox_nt = store.revision_text(namespace, proposal_id, state.revision)[0]
    try:
        _track(key, graph)
    except _ReviewEnded:
        _end_load(namespace, proposal_id, None)  # nothing written yet
    try:
        try:
            _load(graph, tbox_nt, state.revision)
        except (httpx.HTTPError, RuntimeError) as e:
            raise QueryFailed("The draft could not be loaded into the graph database") from e
        recorded = _record(key, graph, state.revision)
    except _ReviewEnded:
        _end_load(namespace, proposal_id, graph)
    except Exception:
        _track_again(namespace, proposal_id, key, graph)
        raise
    _drop_if_closed(namespace, proposal_id, graph)
    return graph, recorded


def _review_state(namespace: str, proposal_id: str) -> tuple[store.StudioError | None, bool]:
    """Why the draft can no longer change (None while it can), and whether its review is over for good.

    It is over when the proposal is gone, accepted, rejected or cancelled, or when its namespace is
    being deleted: the teardown takes the domain ABox lock before it reads the rows. Both reads are
    strongly consistent.
    """
    try:
        item = store.get_proposal(namespace, proposal_id, consistent=True)
    except store.ProposalNotFound as e:
        return e, True
    teardown = abox_store.is_teardown(abox_store.lock_holder(namespace))
    try:
        store.require_state(item, editable=True)
    except store.StudioError as e:
        return e, teardown or item.get("status") in REVIEW_OVER
    if teardown:
        return store.NotEditable("The namespace is being deleted; its drafts can no longer change"), True
    return None, False


def _drop_if_closed(namespace: str, proposal_id: str, graph: str | None) -> None:
    """Raise if the draft can no longer change, after dropping every draft graph, ``graph`` too, if its review is over.

    A load during the end of the review would leave a graph otherwise. An accept still running drops
    nothing and leaves the row open: if it succeeds it drops the graphs, and if it fails the draft goes
    back to review with them.
    """
    error, over = _review_state(namespace, proposal_id)
    if error is None:
        return
    if over:
        drop_draft_graph(namespace, proposal_id, loading=graph)
    raise error


def _close(key: dict[str, str], loading: str | None) -> dict[str, Any] | None:
    """Close the row, so that no load tracks or records a graph in it any more; return it (None: no row).

    ``loading`` is a graph a load wrote; the row names it again, as a sweep may have forgotten it.
    """
    update = "SET closed = if_not_exists(closed, :now), superseded = if_not_exists(superseded, :none)"
    values: dict[str, Any] = {":now": store._now(), ":none": {}}
    if loading:
        update += " ADD graphs :g"
        values[":g"] = {loading}
    try:
        row: dict[str, Any] | None = (
            dynamo_store._get_table()
            .update_item(
                Key=key,
                UpdateExpression=update,
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
            .get("Attributes")
        )
    except ClientError as e:
        if not store._conditional_failed(e):
            raise
        return None
    return row


def drop_draft_graph(namespace: str, proposal_id: str, *, loading: str | None = None) -> bool:
    """End the review of a proposal's draft: close its row, then drop every graph the row names.

    The closed row stays, so a load still running drops what it wrote instead of tracking it. A graph
    leaves the row once its DROP succeeded; one whose DROP failed stays named, for a later drop or the
    namespace purge. ``loading`` is a graph a load wrote that the row may no longer name. Best effort,
    never raises; True when every graph was dropped.
    """
    key = _row_key(namespace, proposal_id)
    try:
        row = _close(key, loading)
        if row is None:  # never queried, or gone with its proposal or namespace
            return _drop(loading) if loading else True
        dropped = [_forget(key, graph, recorded=graph == row.get("graph")) for graph in _named(row)]
    except Exception:  # noqa: BLE001 - the namespace purge sweeps what is left
        _log.warning("studio[%s/%s]: could not drop the draft graph", namespace, proposal_id, exc_info=True)
        return False
    return all(dropped)


def drop_draft_graph_later(namespace: str, proposal_id: str) -> None:
    """Drop the draft graph on a daemon thread, so a request that ends a review does not wait on Neptune."""
    Thread(
        target=drop_draft_graph, args=(namespace, proposal_id), daemon=True, name=f"studio-drop-{proposal_id}"
    ).start()


def drop_all_draft_graphs(namespace: str) -> int:
    """Drop every draft graph of the namespace (its deletion); return how many rows named one.

    Each row is closed before its graphs are dropped, so a load still running neither tracks nor records
    a graph in it after that; one that starts later sees the teardown's lock (``_drop_if_closed``). A
    failed DROP is tried again, and a DynamoDB error on one row or graph does not stop the others. The
    graphs that still could not be dropped are logged in one ERROR, for an operator: the purge then
    deletes the rows that name them.
    """
    table = dynamo_store._get_table()
    scan: dict[str, Any] = {
        "FilterExpression": "begins_with(PK, :pfx) AND SK = :sk",
        "ExpressionAttributeValues": {":pfx": f"{namespace}#PROPOSAL#", ":sk": GRAPH_SK},
        "ProjectionExpression": "PK, SK, graph, graphs, superseded",
        "ConsistentRead": True,
    }
    named = 0
    left: list[str] = []
    while True:
        page = table.scan(**scan)
        for item in page.get("Items", []):
            key = {"PK": item["PK"], "SK": item["SK"]}
            try:
                row = _close(key, None)
            except Exception:  # noqa: BLE001 - its graphs are dropped all the same, as the scan read them
                _log.warning(
                    "studio[%s]: could not close the draft graph row of %s", namespace, key["PK"], exc_info=True
                )
                row = item
            if not row or not _named(row):
                continue  # gone meanwhile, or naming no graph
            named += 1
            for graph in _named(row):
                try:
                    if not _forget(key, graph, recorded=graph == row.get("graph"), attempts=DROP_ATTEMPTS):
                        left.append(graph)
                except Exception:  # noqa: BLE001 - _forget raises only after its DROP; the purge deletes the row
                    _log.warning("studio[%s]: dropped %s but could not update its row", namespace, graph, exc_info=True)
        if "LastEvaluatedKey" not in page:
            break
        scan["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    if left:
        _log.error("studio[%s]: could not drop the draft graphs %s; drop them by hand", namespace, ", ".join(left))
    return named


# ── Results ─────────────────────────────────────────────────────────────────


def _binding_cell(binding: dict[str, Any] | None, genid: str) -> dict[str, Any] | None:
    if binding is None:
        return None
    kind = binding.get("type")
    value = binding.get("value")
    if kind == "uri" and isinstance(value, str) and value.startswith(genid):
        return {"type": "bnode", "value": value[len(genid) :]}
    cell: dict[str, Any] = {"type": "literal" if kind == "typed-literal" else kind, "value": value}
    if binding.get("datatype"):
        cell["datatype"] = binding["datatype"]
    if binding.get("xml:lang"):
        cell["lang"] = binding["xml:lang"]
    return cell


def _term_cell(term: Node, genid: str) -> dict[str, Any]:
    if isinstance(term, URIRef):
        if term.startswith(genid):
            return {"type": "bnode", "value": str(term)[len(genid) :]}
        return {"type": "uri", "value": str(term)}
    if isinstance(term, BNode):
        return {"type": "bnode", "value": str(term)}
    cell: dict[str, Any] = {"type": "literal", "value": str(term)}
    if isinstance(term, Literal):
        if term.datatype:
            cell["datatype"] = str(term.datatype)
        if term.language:
            cell["lang"] = term.language
    return cell


def _capped(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {
        "columns": columns,
        "rows": rows[:MAX_ROWS],
        "rowCount": min(len(rows), MAX_ROWS),
        "truncated": len(rows) > MAX_ROWS,
    }


def _sparql(text: str, form: str, graph: str) -> dict[str, Any]:
    """Run a prepared query on the draft in ``graph``; the IRIs its load minted for blank nodes read as blank nodes."""
    genid = _genid(graph)
    if form in ("SELECT", "ASK"):
        data = _post_query(text, _SELECT_ACCEPT).json()
        if form == "ASK":
            value = {"type": "literal", "value": str(bool(data.get("boolean"))).lower()}
            return _capped(["result"], [[value]])
        columns = list(data.get("head", {}).get("vars", []))
        bindings = data.get("results", {}).get("bindings", [])
        return _capped(columns, [[_binding_cell(b.get(c), genid) for c in columns] for b in bindings])
    g = Graph()
    try:
        g.parse(data=_post_query(text, _GRAPH_ACCEPT).text, format="nt")
    except Exception as e:  # noqa: BLE001 - an unreadable result is the database's fault, not the user's
        raise QueryFailed("The graph database returned a result that could not be read") from e
    triples = sorted(g, key=lambda t: (str(t[0]), str(t[1]), str(t[2])))
    return _capped(["subject", "predicate", "object"], [[_term_cell(x, genid) for x in t] for t in triples])


# ── Lexical graph (openCypher) ──────────────────────────────────────────────


def _cypher_client() -> Any:
    import boto3
    from botocore.config import Config

    from coa_ontology.abox.lexical import _endpoint

    host, port = _endpoint()
    config = Config(retries={"total_max_attempts": 1, "mode": "standard"}, read_timeout=QUERY_SECONDS)
    return boto3.Session(region_name=os.getenv("AWS_REGION", "us-east-1")).client(
        "neptunedata", endpoint_url=f"https://{host}:{port}", config=config
    )


def _value_cell(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return {"type": "value", "value": json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)}
    if isinstance(value, str):
        return {"type": "literal", "value": value}
    return {"type": "value", "value": json.dumps(value)}


def _tenant(namespace: str) -> str:
    from coa_common.constants import to_graphrag_tenant_id

    try:
        tenant = to_graphrag_tenant_id(namespace)
    except ValueError:
        tenant = ""
    if not tenant:
        raise QueryRefused("This namespace has no lexical graph to query")
    return tenant


def _cypher(namespace: str, query: str) -> dict[str, Any]:
    text = prepare_cypher(query, _tenant(namespace), limit=MAX_ROWS, timeout_ms=int(QUERY_SECONDS * 1000))
    try:
        response = _cypher_client().execute_open_cypher_query(openCypherQuery=text)
    except ReadTimeoutError as e:
        raise QueryTimeout(_timeout_message()) from e
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "TimeLimitExceededException":
            raise QueryTimeout(_timeout_message()) from e
        if code in ("MalformedQueryException", "BadRequestException", "InvalidArgumentException"):
            detail = _one_line(str(e.response.get("Error", {}).get("Message") or ""))
            raise QueryRefused(f"The graph database refused the query: {detail}") from e
        raise QueryFailed(f"The graph database could not run the query ({code or 'error'})") from e
    except BotoCoreError as e:
        raise QueryFailed("The graph database could not be reached") from e
    results = response.get("results") or []
    columns: list[str] = []
    for row in results:
        columns.extend(k for k in row if k not in columns)
    return _capped(columns, [[_value_cell(row.get(c)) for c in columns] for row in results])


# ── Entry point ─────────────────────────────────────────────────────────────


def run_query(namespace: str, proposal_id: str, language: str, target: str, query: str) -> dict[str, Any]:
    """Run one console query: draft SPARQL (draft + document ABox) or lexical Cypher."""
    if (language, target) not in LANGUAGE_TARGETS:
        raise QueryRefused("SPARQL runs on the draft and Cypher on the lexical graph")
    # A draft query needs a draft under review: the graph of a closed one is dropped and must stay so.
    state = store.require_state(
        store.get_proposal(namespace, proposal_id, consistent=True), editable=language == "sparql"
    )
    started = time.monotonic()
    if language == "sparql":
        graph = draft_graph_uri(namespace, proposal_id, state.revision)
        prepared = prepare_sparql(
            query,
            [graph, abox_store.graph_uri(namespace)],
            timeout_ms=int(QUERY_SECONDS * 1000),
            limit=MAX_ROWS,
            genid=_genid(graph),
        )
        ensure_draft_graph(namespace, proposal_id, state)
        result = _sparql(prepared.text, prepared.form, graph)
        # The end of the review drops the graph at once: a query it overtook read part of it, or none.
        if not _loaded(graph, state.revision):
            raise QueryFailed(DRAFT_CHANGED)
        form = prepared.form
    else:
        form = "MATCH"
        result = _cypher(namespace, query)
    return {
        "language": language,
        "target": target,
        "revision": state.revision,
        "form": form,
        **result,
        "elapsedMs": int((time.monotonic() - started) * 1000),
    }
