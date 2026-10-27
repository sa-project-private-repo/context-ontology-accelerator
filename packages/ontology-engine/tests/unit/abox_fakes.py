# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Fakes shared by the domain ABox tests (copied from ``test_versions_deploy.py``, not imported from it)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
from rdflib import BNode, Dataset, URIRef


def _binding(term) -> dict[str, str]:
    """One term as Neptune's SPARQL JSON shows it: ``type`` is ``uri``, ``bnode`` or ``literal``."""
    if isinstance(term, URIRef):
        return {"type": "uri", "value": str(term)}
    if isinstance(term, BNode):
        return {"type": "bnode", "value": str(term)}
    out = {"type": "literal", "value": str(term)}
    if term.language:
        out["xml:lang"] = term.language
    elif term.datatype:
        out["datatype"] = str(term.datatype)
    return out


class InlineThread:
    """A ``threading.Thread`` stand-in that runs its target synchronously on ``start``."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class Neptune:
    """In-memory Neptune: rdflib runs the SPARQL deploy sends, and every request is recorded.

    ``before_update`` runs before an update is applied and may raise (Neptune refused
    it, or it never arrived); ``after_update`` runs after and may raise (the update
    landed but its answer was lost). ``before_query`` may raise to fail a query.
    ``before_update`` may also call ``run_later``: the update times out while Neptune
    keeps running it, and it ends (see ``settle``) before Neptune runs the next update.
    """

    def __init__(self) -> None:
        self.ds = Dataset()
        self.updates: list[str] = []
        self.queries: list[str] = []
        self.gsp_targets: list[str] = []
        # Every update and GSP post, with whether Neptune's client may retry it on its own (OOM).
        self.sends: list[tuple[str, bool]] = []
        self.before_update: Callable[[str], None] = lambda text: None
        self.after_update: Callable[[str], None] = lambda text: None
        self.before_query: Callable[[str], None] = lambda text: None
        self.in_flight: list[str] = []

    def update(self, text: str, *, retry_oom: bool = True) -> None:
        self.updates.append(text)
        self.sends.append((text, retry_oom))
        self.settle()  # an update still running ends before the next one runs
        self.before_update(text)
        self.ds.update(text)
        self.after_update(text)

    def run_later(self, text: str) -> None:
        """From ``before_update``: the reply times out while Neptune is still running ``text``."""
        self.in_flight.append(text)
        raise httpx.ReadTimeout("no reply yet; Neptune is still running the update")

    def settle(self, *, commit: bool = True) -> None:
        """End the updates still running: they commit, or they roll back (Neptune's query timeout)."""
        running, self.in_flight = self.in_flight, []
        for text in running if commit else ():
            self.ds.update(text)

    def query(self, text: str) -> dict:
        self.queries.append(text)
        self.before_query(text)
        result = self.ds.query(text)
        if result.type == "ASK":
            return {"head": {}, "boolean": bool(result.askAnswer)}
        rows = [{str(v): _binding(row[v]) for v in result.vars if row[v] is not None} for row in result]
        return {"results": {"bindings": rows}}

    def gsp_post(self, graph_uri: str, turtle: str, *, retry_oom: bool = True) -> dict:
        self.gsp_targets.append(graph_uri)
        self.sends.append((f"POST {graph_uri}", retry_oom))
        self.load(graph_uri, turtle)
        return {"graph_uri": graph_uri, "status": "ok"}

    def load(self, graph_uri: str, turtle: str) -> None:
        self.ds.graph(URIRef(graph_uri)).parse(data=turtle, format="turtle")

    def triples(self, graph_uri: str) -> set:
        return set(self.ds.graph(URIRef(graph_uri)))

    def graphs(self) -> list[str]:
        """The IRIs of the named graphs that hold at least one triple."""
        return sorted(str(g.identifier) for g in self.ds.graphs() if len(g))

    def moves(self) -> list[str]:
        return [u for u in self.updates if u.startswith("MOVE")]

    def dropped_after_first_move(self) -> bool:
        first = next(i for i, u in enumerate(self.updates) if u.startswith("MOVE"))
        return any(u.startswith("DROP") for u in self.updates[first:])
