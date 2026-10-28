# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The tools the Ontology designer agent reads the draft with and proposes change sets through.

Each tool reads the draft's current revision when it is called, and
returns its result as JSON wrapped by :func:`wrap`: the draft's labels and descriptions, document text
and instance values are written by other people, and the system prompt tells the model they are data.
A bad input or a refused request comes back as ``{"error": ...}``, so the model can correct itself.
Any other failure is logged and comes back as a fixed error, so no raw AWS error text reaches the
chat or the model. ``last_summary`` is a one-line account of the latest call, for the chat's step events.

The tools never change the draft: ``propose_changes`` stores a pending change set for the steward.
``run_query`` reads, and ``check_question`` stores a competency question's verdict.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError
from rdflib import URIRef

from coa_ontology.abox import queries as abox_queries
from coa_ontology.catalog.ingest import schema_subjects

from . import changes, changesets, checks, graph_view, iris, queries, questions
from . import draft as store
from .iris import check_iri
from .ops import (
    CLASS,
    DATATYPE_PROPERTY,
    KIND_WORDS,
    OBJECT_PROPERTY,
    kind_of,
    label_of,
    mentioned,
    named_parents,
)
from .query_safety import QueryRefused, tenant_labels

_log = logging.getLogger("studio.agent_tools")
TOOL_FAILED = "The tool failed on the server; try again later."
MAX_TOP_CLASSES = 50
MAX_TERM_PROPERTIES = 100
MAX_TERM_MAPPINGS = 20
MAX_EXAMPLES = 5
MAX_EVIDENCE_INSTANCES = 2
MAX_EVIDENCE_CHUNKS = 2
EVIDENCE_CHARS = 400
MAX_ATTRIBUTES = 10
RECENT_HISTORY = 5
MAX_SUMMARY = 300
MAX_TOOL_ROWS = 20
MAX_TOOL_FINDINGS = 50
MAX_EXPLANATION = 1000
TERM_KINDS = (CLASS, OBJECT_PROPERTY, DATATYPE_PROPERTY)
# The tools name a term's kind as the console does (``ops.KIND_WORDS``), and take that word or the kind itself.
_KINDS = {**{k: k for k in TERM_KINDS}, **{word: k for k, word in KIND_WORDS.items()}}
_KIND_ERROR = f"kind must be one of {', '.join(KIND_WORDS[k] for k in TERM_KINDS)}"


# The tools count attributes and relationships under the console's words; the API keeps the kinds.
_COUNT_WORDS = {"objectProperties": "relationships", "datatypeProperties": "attributes"}


def _counted(counts: dict[str, Any] | None) -> dict[str, Any] | None:
    """``counts`` keyed by the console's words (``_COUNT_WORDS``)."""
    return None if counts is None else {_COUNT_WORDS.get(k, k): v for k, v in counts.items()}


def _worded(term: dict[str, Any]) -> dict[str, Any]:
    """``term`` with its ``kind`` in the console's word."""
    return {**term, "kind": KIND_WORDS[term["kind"]]}


def wrap(payload: Any) -> str:
    r"""``payload`` as JSON between ``<data>`` and ``</data>``.

    Every ``<`` in the JSON (only ever inside a string) is written as ``<``, so no text in the
    payload can close the wrapper or open another one.
    """
    text = json.dumps(payload, ensure_ascii=False, default=str).replace("<", "\\u003c")
    return f"<data>\n{text}\n</data>"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _error(e: Exception) -> dict[str, Any]:
    """The error result for an expected failure; a malformed op names its index, as the route does."""
    if isinstance(e, ValidationError):
        first = e.errors(include_url=False, include_context=False, include_input=False)[0]
        loc = first["loc"]
        payload: dict[str, Any] = {"error": f"{'.'.join(str(p) for p in loc)}: {first['msg']}"}
        if loc and isinstance(loc[0], int):
            payload["opIndex"] = loc[0]
        return payload
    if isinstance(e, store.StudioError):
        return {"error": e.message, **{k: v for k, v in e.extra.items() if k == "opIndex"}}
    if isinstance(e, KeyError):
        return {"error": f"{e.args[0]} is not a class or property of the draft"}
    return {"error": str(e)}


class TurnTools:
    """The tools of one agent turn on one proposal's draft."""

    def __init__(
        self,
        namespace: str,
        proposal_id: str,
        *,
        turn_id: str,
        author: str,
        on_change_set: Callable[[dict[str, Any]], None],
        on_query: Callable[[dict[str, Any]], None] = lambda q: None,
        question: dict[str, Any] | None = None,
    ) -> None:
        """Tools for ``turn_id``; ``on_change_set`` gets each change set it stores, ``on_query`` each query it runs."""
        self.namespace = namespace
        self.proposal_id = proposal_id
        self.turn_id = turn_id
        self.author = author
        self.on_change_set = on_change_set
        self.on_query = on_query
        # The question a question-check turn is about, as the turn saw it.
        self.question = question
        # The verdict check_question stored: a question-check turn must end with one.
        self.checked_status: str | None = None
        self.last_summary = ""

    @property
    def checked(self) -> bool:
        """Whether check_question stored a verdict in this turn."""
        return self.checked_status is not None

    def _current(self) -> tuple[store.StudioState, store.Revision]:
        state = store.require_state(store.get_proposal(self.namespace, self.proposal_id), editable=False)
        return state, store.load_revision(self.namespace, self.proposal_id, state.revision)

    def _run(self, call: Callable[[], tuple[str, Any]]) -> str:
        try:
            summary, payload = call()
        except (store.StudioError, ValueError, KeyError) as e:  # ValidationError and IriError are ValueErrors
            payload = _error(e)
            summary = f"error: {payload['error']}"
        except Exception:
            # Its text may be an AWS error's, which neither the chat nor the model should see.
            _log.exception("studio[%s/%s]: an agent tool failed", self.namespace, self.proposal_id)
            payload = {"error": TOOL_FAILED}
            summary = f"error: {TOOL_FAILED}"
        self.last_summary = summary[:MAX_SUMMARY]
        return wrap(payload)

    def get_overview(self) -> str:
        """Counts, the IRI space, the top-level classes, the newest changes and the pending change sets."""

        def run() -> tuple[str, Any]:
            state, rev = self._current()
            g = rev.draft.tbox
            scheme = store.scheme(self.namespace, state)
            own = sorted((c for c in schema_subjects(g)[0] if scheme.in_space(str(c))), key=str)
            top = [
                c
                for c in own
                if not any(scheme.in_space(str(p)) and kind_of(g, p) == CLASS for p in named_parents(g, c))
            ]
            history = changes.history(self.namespace, self.proposal_id, RECENT_HISTORY)["entries"]
            pending = changesets.recent_change_sets(self.namespace, self.proposal_id, changesets.PENDING)
            payload: dict[str, Any] = {
                "revision": state.revision,
                "baseVersion": state.base_version,
                "counts": _counted(state.counts),
                "iriSpace": {
                    "namespace": scheme.namespace_space,
                    "ontology": scheme.ontology_prefix,
                    "style": scheme.style,
                },
                "topClasses": [{"iri": str(c), "label": label_of(g, c)} for c in top[:MAX_TOP_CLASSES]],
                "topClassCount": len(top),
                "recentChanges": [
                    {"revision": e["revision"], "kind": e["kind"], "source": e["source"], "summary": e["summary"]}
                    for e in history
                ],
                "pendingChangeSets": [
                    {"changeSetId": c["changeSetId"], "summary": c["summary"], "rationale": c["rationale"]}
                    for c in pending
                ],
            }
            try:
                payload["lexicalLabels"] = sorted(tenant_labels(queries._tenant(self.namespace)))
            except QueryRefused:
                payload["lexicalLabels"] = []
            classes = int(state.counts.get("classes", 0))
            return f"{classes} classes, {_plural(len(pending), 'pending change set')}", payload

        return self._run(run)

    def find_terms(self, query: str, kind: str | None = None, limit: int = 20) -> str:
        """Terms whose label or IRI contains ``query``."""

        def run() -> tuple[str, Any]:
            if kind is not None and kind not in _KINDS:
                raise ValueError(_KIND_ERROR)
            _, rev = self._current()
            wanted = _KINDS[kind] if kind is not None else None
            terms = graph_view.search_terms(rev.draft, query, wanted, min(int(limit), graph_view.MAX_SEARCH))
            return f"{_plural(len(terms), 'term')} for {query!r}", {"terms": [_worded(t) for t in terms]}

        return self._run(run)

    def get_term(self, iri: str) -> str:
        """One class or property: parents, children, properties, mappings and ABox count."""

        def run() -> tuple[str, Any]:
            check_iri(iri, "iri")
            state, rev = self._current()
            badges = graph_view.abox_badges(self.namespace)
            term = _worded(graph_view.term_view(rev.draft, store.scheme(self.namespace, state), iri, badges))
            props, maps = term.get("properties") or [], term.get("mappings") or []
            term["truncated"] = {
                "properties": len(props) > MAX_TERM_PROPERTIES,
                "mappings": len(maps) > MAX_TERM_MAPPINGS,
            }
            if "properties" in term:
                term["properties"] = [_worded(p) for p in props[:MAX_TERM_PROPERTIES]]
            term["mappings"] = maps[:MAX_TERM_MAPPINGS]
            return f"{term['label']} ({term['kind']})", term

        return self._run(run)

    def get_abox_examples(self, class_iri: str, limit: int = 3) -> str:
        """Instances of a class in the active domain ABox, the first ones with their evidence."""

        def run() -> tuple[str, Any]:
            check_iri(class_iri, "class_iri")
            badges = graph_view.abox_badges(self.namespace)
            if badges is None:
                return "no domain data", {"available": False, "reason": "The namespace has no domain data yet"}
            if class_iri not in badges.active_classes:
                reason = "The active version has no such class; its instances appear after it is deployed"
                return "not in the active version", {"available": False, "reason": reason}
            _, rev = self._current()
            g = rev.draft.tbox

            def labels(iri: str) -> str:
                return label_of(g, URIRef(iri))

            rows, more = abox_queries.list_instances(
                self.namespace, class_iri=class_iri, limit=max(1, min(int(limit), MAX_EXAMPLES))
            )
            instances: list[dict[str, Any]] = []
            for n, row in enumerate(rows):
                entry: dict[str, Any] = {"iri": row["iri"], "label": row["label"]}
                if n < MAX_EVIDENCE_INSTANCES:
                    detail = abox_queries.instance(self.namespace, row["iri"], labels) or {}
                    entry["attributes"] = [
                        {"label": a["label"], "value": a["value"]}
                        for a in detail.get("attributes", [])[:MAX_ATTRIBUTES]
                    ]
                    entry["evidence"] = [
                        {"sourceKey": e.get("sourceKey"), "text": (e.get("text") or "")[:EVIDENCE_CHARS]}
                        for e in detail.get("evidence", [])[:MAX_EVIDENCE_CHUNKS]
                    ]
                instances.append(entry)
            count = badges.counts.get(class_iri, 0)
            payload = {
                "available": True,
                "current": badges.current,
                "reason": badges.view()["reason"],
                "count": count,
                "instances": instances,
                "more": more,
            }
            return f"{len(instances)} of {count} instances", payload

        return self._run(run)

    def suggest_iri(self, kind: str, label: str, domain: str | None = None) -> str:
        """The IRI an add op would mint for ``label`` now (a property needs its ``domain``)."""

        def run() -> tuple[str, Any]:
            state, rev = self._current()
            scheme = store.scheme(self.namespace, state)
            text = " ".join(label.split())

            def taken(candidate: str) -> bool:
                return mentioned(rev.draft, candidate)

            term_kind = _KINDS.get(kind)
            if term_kind == CLASS:
                iri = iris.mint_class_iri(scheme, text, taken)
            elif term_kind in (OBJECT_PROPERTY, DATATYPE_PROPERTY):
                if not domain:
                    raise ValueError("A property's IRI is minted from its domain; give the domain class IRI")
                check_iri(domain, "domain")
                rdf_kind = "ObjectProperty" if term_kind == OBJECT_PROPERTY else "DatatypeProperty"
                iri = iris.mint_property_iri(scheme, text, rdf_kind, domain, taken)
            else:
                raise ValueError(_KIND_ERROR)
            return iri, {"iri": iri}

        return self._run(run)

    def propose_changes(self, ops: list[dict[str, Any]], rationale: str) -> str:
        """Dry-run ``ops`` and store them as a pending change set for the steward to review."""

        def run() -> tuple[str, Any]:
            cs = changesets.propose(
                self.namespace, self.proposal_id, ops, rationale, turn_id=self.turn_id, author=self.author
            )
            self.on_change_set(cs)
            payload = {
                "changeSetId": cs["changeSetId"],
                "status": cs["status"],
                "summary": cs["summary"],
                "warnings": cs["warnings"],
                "diffCounts": _counted(cs["diffCounts"]),
                "note": "Stored for the user to review. It is not applied; the user applies all, some or none of it.",
            }
            return f"proposed a change set of {_plural(len(cs['ops']), 'op')}", payload

        return self._run(run)

    def run_checks(self) -> str:
        """The structural findings of the current revision and its comparison with the data."""

        def run() -> tuple[str, Any]:
            result = checks.checks(self.namespace, self.proposal_id)
            instant = result["instant"]
            findings = instant["findings"]
            instant["findings"] = findings[:MAX_TOOL_FINDINGS]
            instant["truncated"] = instant["truncated"] or len(findings) > MAX_TOOL_FINDINGS
            for group in result["structured"].values():
                if isinstance(group, dict):
                    group["terms"] = group["terms"][:MAX_TOOL_FINDINGS]  # ``count`` still counts them all
            counts = instant["counts"]
            summary = f"{_plural(counts['error'], 'error')}, {_plural(counts['warning'], 'warning')}"
            return summary, result

        return self._run(run)

    def _query(self, language: str, target: str, query: str) -> dict[str, Any]:
        result = queries.run_query(self.namespace, self.proposal_id, language, target, query)
        self.on_query({"language": language, "target": target, "query": query, "rowCount": result["rowCount"]})
        return result

    def run_query(self, language: str, target: str, query: str) -> str:
        """Run a read-only query: SPARQL on the draft and its documents, or Cypher on the lexical graph."""

        def run() -> tuple[str, Any]:
            result = self._query(language, target, query)
            payload = {
                **result,
                "rows": result["rows"][:MAX_TOOL_ROWS],
                "rowsShown": min(len(result["rows"]), MAX_TOOL_ROWS),
            }
            more = "+" if result["truncated"] else ""
            return f"{_plural(result['rowCount'], 'row')}{more}", payload

        return self._run(run)

    def check_question(self, question_id: str, status: str, query: str | None, explanation: str) -> str:
        """Record the verdict on a competency question, after running its query on the draft again."""

        def run() -> tuple[str, Any]:
            # The question's text is data written by people; it may not steer the turn to another question.
            if self.question is not None and question_id != self.question.get("questionId"):
                raise ValueError(
                    f"This turn checks question {self.question.get('questionId')} only; give the verdict on it"
                )
            if status not in questions.STATUSES:
                raise ValueError(f"status must be one of {', '.join(questions.STATUSES)}")
            current = questions.get_question(self.namespace, question_id)
            seen = self.question or current
            text = (query or "").strip() or None
            if status == "not_expressible":
                state, _ = self._current()
                revision, rows, row_count, stored, text = state.revision, [], 0, status, None
            elif text is None:
                raise ValueError(
                    "An answerable or expressible verdict needs the SPARQL query that answers the question"
                )
            else:
                result = self._query("sparql", "draft", text)
                revision, rows, row_count = result["revision"], result["rows"], result["rowCount"]
                if result["form"] == "ASK":  # one row either way: the answer is its value
                    row_count = int(rows[0][0]["value"] == "true")
                # The data decides between the two: rows answer the question, none do not yet.
                stored = "answerable" if row_count else "expressible"
            questions.record_check(
                self.namespace,
                question_id,
                proposal_id=self.proposal_id,
                revision=revision,
                status=stored,
                query=text,
                row_count=row_count,
                rows=rows,
                explanation=explanation.strip()[:MAX_EXPLANATION] or None,
                checked_text=str(seen["text"]),
            )
            self.checked_status = stored
            payload: dict[str, Any] = {"questionId": question_id, "stored": stored, "rowCount": row_count}
            if stored != status:
                rows_seen = "returned rows" if row_count else "returned no rows"
                payload["note"] = f"The query {rows_seen}, so the verdict is stored as {stored}."
            return f"{question_id}: {stored}", payload

        return self._run(run)
