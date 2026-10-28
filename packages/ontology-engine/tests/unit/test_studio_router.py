# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import json
import logging
from unittest.mock import patch
from urllib.parse import quote

import pytest
from coa_ontology import dynamo_store
from coa_ontology.pagination import encode_next_token
from coa_ontology.studio import checks, graph_view
from coa_ontology.studio import draft as store
from coa_ontology.studio import router as studio_router
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .studio_fakes import CUSTOMER, EMAIL, NS, ONTO, ORDER, PARTY, TOTAL, VIP, publish_base, seed_proposal

pytestmark = [pytest.mark.unit]
BASE = "/ontology/proposals/p-1/studio"
ALICE = {"X-Coa-Principal": "alice%40example.com"}
WAREHOUSE = f"{ONTO}Warehouse"
V2 = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{WAREHOUSE}> a owl:Class ; rdfs:label "Warehouse" .
"""


@pytest.fixture
def http(versions_aws):
    store.clear_cache()
    app = FastAPI()
    app.include_router(studio_router.router, prefix="/ontology/proposals/{proposal_id}/studio")
    with (
        patch("coa_ontology.proposals._generate_schema_sql", return_value=None),
        patch.object(studio_router.version_deploy, "find_breaking_metrics", return_value=[]) as breaking,
    ):
        publish_base()
        seed_proposal()
        client = TestClient(app)
        client.breaking = breaking  # type: ignore[attr-defined]
        yield client
    store.clear_cache()


def _q(**params: str) -> str:
    return "?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in {"namespace": NS, **params}.items())


def _open(http) -> dict:
    r = http.post(f"{BASE}{_q()}", headers=ALICE)
    assert r.status_code == 200, r.text
    return r.json()


def _change(http, ops: list[dict], expected: int, **kwargs) -> dict:
    r = http.post(f"{BASE}/changes{_q()}", json={"ops": ops, "expectedRevision": expected}, **kwargs)
    assert r.status_code == 200, r.text
    return r.json()


def test_open_then_read_the_draft(http):
    assert http.get(f"{BASE}{_q()}").json()["detail"]["code"] == "StudioDraftNotOpen"
    opened = _open(http)
    assert opened["revision"] == 0 and opened["baseVersion"] == 1 and opened["headVersion"] == 1
    assert opened["rebaseRequired"] is False and opened["editable"] and opened["openedBy"] == "alice@example.com"
    assert opened["counts"]["classes"] == 6 and not opened["canUndo"]
    assert http.get(f"{BASE}{_q()}").json() == opened
    assert http.post(f"{BASE}{_q()}", headers={"X-Coa-Principal": "bob"}).json() == opened  # open is idempotent


def test_the_draft_view_has_exactly_the_documented_fields(http):
    assert set(_open(http)) == {
        "proposalId",
        "status",
        "editable",
        "revision",
        "baseVersion",
        "headVersion",
        "rebaseRequired",
        "ontologyId",
        "counts",
        "canUndo",
        "canRedo",
        "openedAt",
        "openedBy",
        "updatedAt",
        "updatedBy",
    }


def test_graph_term_and_search(http):
    _open(http)
    graph = http.get(f"{BASE}/graph{_q()}").json()
    assert graph["mode"] == "full" and graph["totalClasses"] == 6
    focus = http.get(f"{BASE}/graph{_q(focus=VIP)}").json()
    assert focus["mode"] == "focus" and focus["focus"] == VIP
    assert http.get(f"{BASE}/graph{_q(focus=f'{ONTO}Nope')}").status_code == 404
    term = http.get(f"{BASE}/term{_q(iri=CUSTOMER)}")
    assert term.status_code == 200 and term.json()["label"] == "Customer"
    assert http.get(f"{BASE}/term{_q(iri='not an iri')}").status_code == 422
    terms = http.get(f"{BASE}/terms{_q(q='cust', kind='class')}").json()
    assert [t["iri"] for t in terms["terms"]] == [CUSTOMER, VIP]
    assert terms["revision"] == 0


def test_an_unknown_term_or_focus_is_a_404_with_a_code_and_a_bad_focus_iri_is_a_422(http):
    _open(http)
    gone = http.get(f"{BASE}/term{_q(iri=f'{ONTO}Nope')}")
    assert gone.status_code == 404 and gone.json()["detail"]["code"] == "TermNotFound"
    assert http.get(f"{BASE}/graph{_q(focus=f'{ONTO}Nope')}").json()["detail"]["code"] == "TermNotFound"
    bad = http.get(f"{BASE}/graph{_q(focus='not an iri')}")
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "InvalidIri"


def test_a_term_iri_with_a_fragment_reaches_the_term_view_intact(http):
    _open(http)
    assert PARTY.count("#") == 1
    assert http.get(f"{BASE}/term{_q(iri=PARTY)}").json()["iri"] == PARTY
    assert http.get(f"{BASE}/graph{_q(focus=PARTY)}").json()["focus"] == PARTY


def test_the_graph_and_the_term_read_the_abox_state_once_per_request(http):
    _open(http)
    with patch.object(graph_view, "abox_badges", wraps=graph_view.abox_badges) as badges:
        graph = http.get(f"{BASE}/graph{_q()}").json()
        assert badges.call_count == 1 and graph["abox"] is None
        assert all(n["aboxCount"] is None and n["afterDeploy"] is False for n in graph["nodes"])
        http.get(f"{BASE}/graph{_q(focus=VIP)}")
        assert badges.call_count == 2
        http.get(f"{BASE}/term{_q(iri=CUSTOMER)}")
        assert badges.call_count == 3
        http.get(f"{BASE}/term{_q(iri=f'{ONTO}Nope')}")  # a 404 still read it once
        assert badges.call_count == 4


def test_the_limits_are_bounded(http):
    _open(http)
    for limit, status in (
        (0, 422),
        (-1, 422),
        (graph_view.MAX_SEARCH + 1, 422),
        (1, 200),
        (graph_view.MAX_SEARCH, 200),
    ):
        assert http.get(f"{BASE}/terms{_q(q='c', limit=str(limit))}").status_code == status, limit
    assert len(http.get(f"{BASE}/terms{_q(q='c', limit='1')}").json()["terms"]) == 1
    for limit, status in ((0, 422), (-5, 422), (101, 422), (1, 200), (100, 200)):
        assert http.get(f"{BASE}/history{_q(maxResults=str(limit))}").status_code == status, limit
    assert http.get(f"{BASE}/terms{_q(q='')}").status_code == 422
    assert http.get(f"{BASE}/terms{_q(q='c', kind='thing')}").status_code == 422


def test_changes_conflicts_and_bad_ops(http):
    _open(http)
    ok = http.post(
        f"{BASE}/changes{_q()}", json={"ops": [{"op": "add_class", "label": "Supplier"}], "expectedRevision": 0}
    )
    assert ok.status_code == 200 and ok.json()["revision"] == 1
    stale = http.post(f"{BASE}/changes{_q()}", json={"ops": [{"op": "add_class", "label": "X"}], "expectedRevision": 0})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "DraftConflict"
    assert stale.json()["detail"]["revision"] == 1
    bad = http.post(f"{BASE}/changes{_q()}", json={"ops": [{"op": "restore_triples"}], "expectedRevision": 1})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "InvalidChange"
    refused = http.post(
        f"{BASE}/changes{_q()}",
        json={"ops": [{"op": "add_parent", "iri": PARTY, "parent": VIP}], "expectedRevision": 1},
    )
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "InvalidChange"
    assert refused.json()["detail"]["opIndex"] == 0 and "cycle" in refused.json()["detail"]["message"]
    dry = http.post(
        f"{BASE}/changes{_q()}",
        json={"ops": [{"op": "delete_class", "iri": CUSTOMER}], "expectedRevision": 1, "dryRun": True},
    )
    assert dry.status_code == 200 and dry.json()["dryRun"] and dry.json()["revision"] == 1
    assert http.get(f"{BASE}{_q()}").json()["revision"] == 1  # a dry run stores nothing


def test_an_edit_the_mapping_contradicts_is_a_409_invalid_change_that_names_the_table(http):
    _open(http)
    email = f"{ONTO}customer_email"
    op = {"op": "update_property", "iri": email, "domain": f"{ONTO}Order"}
    refused = http.post(f"{BASE}/changes{_q()}", json={"ops": [op], "expectedRevision": 0})
    detail = refused.json()["detail"]
    assert refused.status_code == 409 and detail["code"] == "InvalidChange" and detail["opIndex"] == 0
    assert detail["message"] == (
        "Op 0: customer_email is mapped from table customer; its domain must stay Customer or one of its parents."
    )
    assert http.get(f"{BASE}{_q()}").json()["revision"] == 0


def test_a_malformed_op_names_its_index_and_does_not_echo_the_input(http):
    _open(http)
    ops = [{"op": "add_class", "label": "Supplier"}, {"op": "add_class", "label": "secret-value", "bogus": 1}]
    bad = http.post(f"{BASE}/changes{_q()}", json={"ops": ops, "expectedRevision": 0})
    detail = bad.json()["detail"]
    assert bad.status_code == 422 and detail["code"] == "InvalidChange" and detail["opIndex"] == 1
    assert "bogus" in detail["message"] and "secret-value" not in bad.text
    unknown = http.post(f"{BASE}/changes{_q()}", json={"ops": [{"op": "drop_table"}], "expectedRevision": 0})
    assert unknown.status_code == 422 and unknown.json()["detail"]["opIndex"] == 0
    assert http.get(f"{BASE}{_q()}").json()["revision"] == 0  # all or none
    # Out-of-contract bodies are refused before any op is read: no ops, too many, too large, a bad revision.
    for body in (
        {"ops": [], "expectedRevision": 0},
        {"ops": [{"op": "add_class", "label": "C"}] * 101, "expectedRevision": 0},
        {"ops": [{"op": "add_class", "label": "x" * studio_router.MAX_CHANGE_SET_BYTES}], "expectedRevision": 0},
        {"ops": [{"op": "add_class", "label": "C"}], "expectedRevision": -1},
        {"ops": [{"op": "add_class", "label": "C"}], "expectedRevision": store.MAX_REVISION + 1},
    ):
        assert http.post(f"{BASE}/changes{_q()}", json=body).status_code == 422


def test_the_author_is_the_decoded_principal_and_unknown_without_one(http):
    _open(http)
    _change(http, [{"op": "add_class", "label": "Supplier"}], 0, headers={"X-Coa-Principal": "carol%40example.com"})
    _change(http, [{"op": "add_class", "label": "Vendor"}], 1)
    assert http.post(f"{BASE}/undo{_q()}", json={"expectedRevision": 2}, headers=ALICE).status_code == 200
    entries = http.get(f"{BASE}/history{_q()}").json()["entries"]
    assert [(e["kind"], e["author"]) for e in entries] == [
        ("undo", "alice@example.com"),
        ("apply", "unknown"),
        ("apply", "carol@example.com"),
        ("open", "alice@example.com"),
    ]
    assert http.get(f"{BASE}{_q()}").json()["updatedBy"] == "alice@example.com"


def test_undo_redo_history_and_rebase(http):
    _open(http)
    http.post(f"{BASE}/changes{_q()}", json={"ops": [{"op": "delete_class", "iri": CUSTOMER}], "expectedRevision": 0})
    assert http.post(f"{BASE}/undo{_q()}", json={"expectedRevision": 1}).json()["revision"] == 2
    assert http.post(f"{BASE}/redo{_q()}", json={"expectedRevision": 2}).json()["revision"] == 3
    history = http.get(f"{BASE}/history{_q()}").json()
    assert [e["kind"] for e in history["entries"]] == ["redo", "undo", "apply", "open"]
    none = http.post(f"{BASE}/rebase{_q()}", json={"expectedRevision": 3})
    assert none.status_code == 409 and none.json()["detail"]["code"] == "NothingToRebase"
    publish_base(proposal="p-other")
    assert http.get(f"{BASE}{_q()}").json()["rebaseRequired"] is True
    rebased = http.post(f"{BASE}/rebase{_q()}", json={"expectedRevision": 3, "skipOps": []})
    assert rebased.status_code == 200 and rebased.json()["baseVersion"] == 2


def test_history_and_checks_answer_their_sentences_as_codes_too(http):
    """The web app could only show the engine's English history lines and findings."""
    _open(http)
    _change(http, [{"op": "add_class", "label": "Premium customer"}], 0)
    entries = http.get(f"{BASE}/history{_q()}").json()["entries"]
    assert entries[0]["summary"] == ["Added class Premium customer"]
    assert entries[0]["summaryMessages"] == [{"code": "ADD_CLASS", "params": {"label": "Premium customer"}}]
    with (
        patch.object(checks.graph_view, "abox_badges", return_value=None),
        patch.object(checks.abox_mapping, "list_rows", side_effect=RuntimeError("ddb down")),
        patch.object(checks.version_deploy, "find_breaking_metrics", return_value=[]),
    ):
        body = http.get(f"{BASE}/checks{_q()}").json()
    isolated = [f for f in body["instant"]["findings"] if f["messageParams"] == {"term": "Premium customer"}]
    assert [(f["code"], f["messageCode"]) for f in isolated] == [("ISOLATED_CLASS", "ISOLATED_CLASS")]
    assert isolated[0]["message"] == "Premium customer has no relationship, parent or attribute"
    assert body["data"]["mappings"] == {"error": checks.MAPPINGS_ERROR, "errorCode": "MAPPINGS_UNREADABLE"}


def test_history_pages_follow_next_token_and_refuse_a_foreign_one(http):
    _open(http)
    for n, label in enumerate(("Supplier", "Vendor", "Carrier")):
        _change(http, [{"op": "add_class", "label": label}], n)
    first = http.get(f"{BASE}/history{_q(maxResults='2')}").json()
    assert [e["revision"] for e in first["entries"]] == [3, 2] and first["revision"] == 3
    second = http.get(f"{BASE}/history{_q(maxResults='2', nextToken=first['nextToken'])}").json()
    assert [e["revision"] for e in second["entries"]] == [1, 0] and "nextToken" not in second
    assert second["revision"] == 3 and second["canUndo"] is True  # the draft's fields are on every page
    for token in ("not-a-token", encode_next_token({"PK": "p-2", "SK": "1"}, scope="studio-history:x:p-2")):
        assert http.get(f"{BASE}/history{_q(nextToken=token)}").status_code == 400
    scope = studio_router.history_scope(NS, "p-1")
    for revision in ("9", "x"):  # past the current revision, not a revision
        token = encode_next_token({"PK": "p-1", "SK": revision}, scope=scope)
        assert http.get(f"{BASE}/history{_q(nextToken=token)}").status_code == 400


def test_nothing_to_undo_or_redo_and_stale_undo_are_409s_with_codes(http):
    _open(http)
    nothing = http.post(f"{BASE}/undo{_q()}", json={"expectedRevision": 0})
    assert nothing.status_code == 409 and nothing.json()["detail"]["code"] == "NothingToUndo"
    nothing = http.post(f"{BASE}/redo{_q()}", json={"expectedRevision": 0})
    assert nothing.status_code == 409 and nothing.json()["detail"]["code"] == "NothingToRedo"
    stale = http.post(f"{BASE}/undo{_q()}", json={"expectedRevision": 7})
    assert stale.status_code == 409 and stale.json()["detail"] == {
        "code": "DraftConflict",
        "message": "Another change was applied first; the draft is now at revision 0.",
        "revision": 0,
    }


def test_a_failed_rebase_reaches_the_client_with_its_failures_and_the_revision(http):
    _open(http)
    _change(http, [{"op": "add_class", "label": "Warehouse", "iri": WAREHOUSE}], 0)
    publish_base(V2, "", "p-other")  # v2 defines Warehouse too
    unknown = http.post(f"{BASE}/rebase{_q()}", json={"expectedRevision": 1, "skipOps": ["9:9"]})
    assert unknown.status_code == 409 and unknown.json()["detail"]["code"] == "InvalidChange"
    failed = http.post(f"{BASE}/rebase{_q()}", json={"expectedRevision": 1})
    detail = failed.json()["detail"]
    assert failed.status_code == 409 and detail["code"] == "RebaseFailed" and detail["revision"] == 1
    assert [(f["opId"], f["op"]["op"]) for f in detail["failures"]] == [("1:0", "add_class")]
    assert "already exists" in detail["failures"][0]["error"]
    assert http.get(f"{BASE}{_q()}").json()["revision"] == 1  # the draft is unchanged
    skipped = http.post(f"{BASE}/rebase{_q()}", json={"expectedRevision": 1, "skipOps": ["1:0"]})
    assert skipped.status_code == 200 and skipped.json()["skipped"] == ["1:0"] and skipped.json()["baseVersion"] == 2


def test_a_body_without_the_expected_revision_is_a_422(http):
    _open(http)
    for path in ("changes", "undo", "redo", "rebase"):
        body = {"ops": [{"op": "add_class", "label": "X"}]} if path == "changes" else {}
        assert http.post(f"{BASE}/{path}{_q()}", json=body).status_code == 422, path


def test_a_missing_proposal_or_a_malformed_proposal_id(http):
    missing = http.get(f"/ontology/proposals/nope/studio{_q()}")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "ProposalNotFound"
    malformed = http.get(f"/ontology/proposals/%20x/studio{_q()}")  # a proposal id may not start with a space
    assert malformed.status_code == 422 and malformed.json()["detail"]["code"] == "InvalidRequest"
    assert http.post(f"/ontology/proposals/a%24b/studio{_q()}").status_code == 422
    assert http.get(f"/ontology/proposals/a%24b/studio/graph{_q()}").status_code == 422


def test_a_value_error_inside_a_handler_is_a_server_fault_not_a_422(http):
    _open(http)
    server = TestClient(http.app, raise_server_exceptions=False)
    # A corrupt change record: json.loads raises JSONDecodeError, a ValueError.
    store._put(store.record_key(NS, "p-1", 0), "{not json", "application/json")
    assert server.get(f"{BASE}/history{_q()}").status_code == 500
    with patch.object(studio_router.changes, "history", side_effect=ValueError("damaged")):
        assert server.get(f"{BASE}/history{_q()}").status_code == 500
    with patch.object(studio_router.store, "require_state", side_effect=ValueError("damaged")):
        assert server.get(f"{BASE}{_q()}").status_code == 500


def _corrupt(revision: int, mutate) -> None:
    """Rewrite a stored change record the way damage or a version skew would: with a field no op has."""
    record = store.get_record(NS, "p-1", revision)
    mutate(record)
    store._put(store.record_key(NS, "p-1", revision), json.dumps(record), "application/json")


def test_a_stored_inverse_that_does_not_parse_makes_undo_a_500_not_an_invalid_change(http):
    server = TestClient(http.app, raise_server_exceptions=False)
    _open(http)
    _change(http, [{"op": "add_class", "label": "Supplier"}], 0)
    _corrupt(1, lambda record: record["inverse"].update(bogus=1))
    undone = server.post(f"{BASE}/undo{_q()}", json={"expectedRevision": 1})
    assert undone.status_code == 500 and "InvalidChange" not in undone.text
    assert http.get(f"{BASE}{_q()}").json()["revision"] == 1  # nothing was written


def test_stored_ops_that_do_not_parse_make_redo_a_500_not_an_invalid_change(http):
    server = TestClient(http.app, raise_server_exceptions=False)
    _open(http)
    _change(http, [{"op": "add_class", "label": "Supplier"}], 0)  # 1
    assert http.post(f"{BASE}/undo{_q()}", json={"expectedRevision": 1}).status_code == 200  # 2
    _corrupt(1, lambda record: record["ops"][0].update(bogus=1))
    redone = server.post(f"{BASE}/redo{_q()}", json={"expectedRevision": 2})
    assert redone.status_code == 500 and "InvalidChange" not in redone.text
    assert http.get(f"{BASE}{_q()}").json()["revision"] == 2


def test_stored_ops_that_do_not_parse_make_rebase_a_500_not_an_invalid_change(http):
    server = TestClient(http.app, raise_server_exceptions=False)
    _open(http)
    _change(http, [{"op": "add_class", "label": "Supplier"}], 0)  # 1
    publish_base(proposal="p-other")  # the draft needs a rebase
    _corrupt(1, lambda record: record["ops"][0].update(bogus=1))
    rebased = server.post(f"{BASE}/rebase{_q()}", json={"expectedRevision": 1})
    assert rebased.status_code == 500 and "InvalidChange" not in rebased.text
    assert http.get(f"{BASE}{_q()}").json()["revision"] == 1


def test_a_proposal_that_left_review_cannot_be_opened_or_written(http):
    seed_proposal("p-2", status="cancelled")
    refused = http.post(f"/ontology/proposals/p-2/studio{_q()}", headers=ALICE)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "ProposalNotEditable"
    _open(http)
    dynamo_store.update_proposal("p-1", namespace=NS, status="cancelled")
    view = http.get(f"{BASE}{_q()}").json()
    assert view["status"] == "cancelled" and view["editable"] is False  # still readable
    assert http.get(f"{BASE}/graph{_q()}").status_code == 200
    write = http.post(f"{BASE}/changes{_q()}", json={"ops": [{"op": "add_class", "label": "X"}], "expectedRevision": 0})
    assert write.status_code == 409 and write.json()["detail"]["code"] == "ProposalNotEditable"


def test_accept_preview_lists_removed_terms_renames_and_breaking_metrics(http):
    _open(http)
    http.breaking.return_value = [{"metricId": "m1", "name": "Customers", "missingClassIri": CUSTOMER}]
    http.post(
        f"{BASE}/changes{_q()}",
        json={
            "ops": [{"op": "change_iri", "iri": VIP, "newIri": f"{ONTO}Vip"}, {"op": "delete_class", "iri": CUSTOMER}],
            "expectedRevision": 0,
        },
    )
    preview = http.get(f"{BASE}/accept-preview{_q()}").json()
    assert preview["revision"] == 1 and preview["baseVersion"] == 1 and not preview["rebaseRequired"]
    assert CUSTOMER in preview["diff"]["classes"]["removed"]
    assert preview["renamed"] == [{"fromIri": VIP, "toIri": f"{ONTO}Vip", "kind": "renamed"}]
    assert preview["breakingMetrics"][0]["metricId"] == "m1" and preview["breakingMetricsError"] is None
    assert preview["headVersion"] == 1 and set(preview) == {
        "revision",
        "baseVersion",
        "headVersion",
        "rebaseRequired",
        "diff",
        "renamed",
        "breakingMetrics",
        "breakingMetricsError",
        "structured",
    }


def test_accept_preview_hides_why_the_breaking_metrics_check_failed_but_logs_it(http, caplog):
    _open(http)
    http.breaking.side_effect = RuntimeError("neptune down at db-1.internal:8182")
    with caplog.at_level(logging.WARNING, logger="studio.router"):
        response = http.get(f"{BASE}/accept-preview{_q()}")
    degraded = response.json()
    assert degraded["breakingMetrics"] == []
    assert (
        degraded["breakingMetricsError"]
        == "Breaking metrics could not be checked; the graph database may be unreachable."
    )
    assert "neptune" not in response.text and "RuntimeError" not in response.text and "db-1" not in response.text
    (record,) = [r for r in caplog.records if r.name == "studio.router"]
    assert record.levelno == logging.WARNING and record.exc_info is not None
    assert "neptune down at db-1.internal:8182" in str(record.exc_info[1])  # the operator still gets the details


def test_accept_preview_gives_its_fixed_errors_as_codes_too(http):
    """The accept dialog showed these two fixed errors in the engine's English whatever the user's language."""
    _open(http)
    http.breaking.side_effect = RuntimeError("neptune down")
    read = checks.storage.read_version_file

    def unreadable_mapping(namespace: str, version: int, name: str) -> str | None:
        if name == "mappings.r2rml":
            raise RuntimeError("s3 down")
        return read(namespace, version, name)

    with patch.object(checks.storage, "read_version_file", side_effect=unreadable_mapping):
        preview = http.get(f"{BASE}/accept-preview{_q()}").json()
    assert preview["breakingMetricsError"] == studio_router.BREAKING_METRICS_ERROR
    assert preview["breakingMetricsErrorCode"] == "METRICS_UNCHECKED"
    assert preview["structured"]["error"] == checks.BASE_MAPPING_ERROR
    assert preview["structured"]["errorCode"] == "BASE_MAPPING_UNREADABLE"
    # Both read the same as the Checks tab's errors of those codes.
    assert checks.ERROR_CODES[studio_router.BREAKING_METRICS_ERROR] == "METRICS_UNCHECKED"


def test_a_change_answers_its_warnings_as_codes_too(http):
    _open(http)
    body = _change(http, [{"op": "update_property", "iri": f"{ONTO}customer_email", "domain": PARTY}], 0)
    assert [m["code"] for m in body["warningMessages"]] == ["DOMAIN_ON_PARENT_TABLE"]
    entry = http.get(f"{BASE}/history{_q()}").json()["entries"][0]
    assert entry["warnings"] == body["warnings"] and entry["warningMessages"] == body["warningMessages"]


def test_accept_preview_composes_renames_and_ignores_undone_ones(http):
    _open(http)
    vip2, vip3 = f"{ONTO}Vip2", f"{ONTO}Vip3"
    _change(http, [{"op": "change_iri", "iri": VIP, "newIri": vip2}], 0)
    _change(http, [{"op": "change_iri", "iri": vip2, "newIri": vip3}], 1)
    renamed = http.get(f"{BASE}/accept-preview{_q()}").json()["renamed"]
    assert renamed == [{"fromIri": VIP, "toIri": vip3, "kind": "renamed"}]  # one entry for the whole chain
    http.post(f"{BASE}/undo{_q()}", json={"expectedRevision": 2})
    renamed = http.get(f"{BASE}/accept-preview{_q()}").json()["renamed"]
    assert renamed == [{"fromIri": VIP, "toIri": vip2, "kind": "renamed"}]  # the undone rename is gone
    http.post(f"{BASE}/undo{_q()}", json={"expectedRevision": 3})
    assert http.get(f"{BASE}/accept-preview{_q()}").json()["renamed"] == []


def test_accept_preview_reports_a_merge_as_merged(http):
    _open(http)
    _change(http, [{"op": "merge_classes", "source": VIP, "target": CUSTOMER}], 0)
    preview = http.get(f"{BASE}/accept-preview{_q()}").json()
    assert preview["renamed"] == [{"fromIri": VIP, "toIri": CUSTOMER, "kind": "merged"}]
    assert VIP in preview["diff"]["classes"]["removed"]


CLIENT = f"{ONTO}Client"


def test_accept_preview_follows_a_merge_target_that_is_renamed_afterwards(http):
    _open(http)
    _change(http, [{"op": "merge_classes", "source": VIP, "target": CUSTOMER}], 0)
    _change(http, [{"op": "change_iri", "iri": CUSTOMER, "newIri": CLIENT}], 1)
    assert http.get(f"{BASE}/accept-preview{_q()}").json()["renamed"] == [
        {"fromIri": VIP, "toIri": CLIENT, "kind": "merged"},  # still a merge, and it follows the target
        {"fromIri": CUSTOMER, "toIri": CLIENT, "kind": "renamed"},  # the target's own rename is listed too
    ]


def test_accept_preview_moves_every_merge_into_a_renamed_target(http):
    _open(http)
    _change(
        http,
        [
            {"op": "merge_classes", "source": VIP, "target": CUSTOMER},
            {"op": "merge_classes", "source": f"{ONTO}Item", "target": CUSTOMER},
        ],
        0,
    )
    _change(http, [{"op": "change_iri", "iri": CUSTOMER, "newIri": CLIENT}], 1)
    assert http.get(f"{BASE}/accept-preview{_q()}").json()["renamed"] == [
        {"fromIri": VIP, "toIri": CLIENT, "kind": "merged"},
        {"fromIri": f"{ONTO}Item", "toIri": CLIENT, "kind": "merged"},
        {"fromIri": CUSTOMER, "toIri": CLIENT, "kind": "renamed"},
    ]


def test_accept_preview_lists_no_rename_to_a_term_the_draft_no_longer_has(http):
    _open(http)
    _change(
        http,
        [{"op": "change_iri", "iri": VIP, "newIri": f"{ONTO}Vip"}, {"op": "delete_class", "iri": f"{ONTO}Vip"}],
        0,
    )
    preview = http.get(f"{BASE}/accept-preview{_q()}").json()
    assert VIP in preview["diff"]["classes"]["removed"] and preview["renamed"] == []


def test_accept_preview_reports_a_renamed_class_merged_later_as_merged_into_the_target(http):
    _open(http)
    _change(http, [{"op": "change_iri", "iri": VIP, "newIri": f"{ONTO}Vip"}], 0)
    _change(http, [{"op": "merge_classes", "source": f"{ONTO}Vip", "target": CUSTOMER}], 1)
    # The intermediate IRI never existed in the base, so only the original class is listed.
    assert http.get(f"{BASE}/accept-preview{_q()}").json()["renamed"] == [
        {"fromIri": VIP, "toIri": CUSTOMER, "kind": "merged"}
    ]


def test_accept_preview_does_not_call_a_deleted_class_renamed_when_its_iri_was_reused_on_the_way(http):
    _open(http)
    item, thing = f"{ONTO}Item", f"{ONTO}Thing"
    _change(http, [{"op": "delete_class", "iri": item}], 0)
    _change(http, [{"op": "change_iri", "iri": VIP, "newIri": item}], 1)  # Item is only a stop on the way
    _change(http, [{"op": "change_iri", "iri": item, "newIri": thing}], 2)
    preview = http.get(f"{BASE}/accept-preview{_q()}").json()
    assert item in preview["diff"]["classes"]["removed"]  # the base's Item is gone
    assert preview["renamed"] == [{"fromIri": VIP, "toIri": thing, "kind": "renamed"}]  # and not renamed to Thing


def test_checks_answer_the_structured_check_of_the_whole_draft(http):
    _open(http)
    _change(http, [{"op": "add_class", "label": "Premium customer", "parents": [CUSTOMER]}], 0)
    with (
        patch.object(checks.graph_view, "abox_badges", return_value=None),
        patch.object(checks.abox_mapping, "list_rows", return_value=[]),
        patch.object(checks.version_deploy, "find_breaking_metrics", return_value=[]),
    ):
        body = http.get(f"{BASE}/checks{_q()}").json()
    structured = body["structured"]
    assert set(structured) == {
        "unmappedClasses",
        "unmappedProperties",
        "labelDiffers",
        "multiTableClasses",
        "removedMappedClasses",
        "removedMappedProperties",
        "mapsNoTable",
        "error",
    }
    # Every class no table answers: the sample's (not Party, a parent of Customer), the proposal's Shipment and
    # the new one.
    assert [t["label"] for t in structured["unmappedClasses"]["terms"]] == [
        "Item",
        "Premium customer",
        "Shipment",
        "VIP customer",
    ]
    assert structured["mapsNoTable"] is False and structured["error"] is None


def test_accept_preview_lists_what_structured_queries_cannot_answer_among_the_terms_it_adds_or_changes(http):
    _open(http)
    _change(
        http,
        [
            {"op": "add_class", "label": "Premium customer", "parents": [CUSTOMER]},
            {"op": "add_datatype_property", "label": "loyalty tier", "domain": CUSTOMER, "range": "xsd:string"},
            {"op": "set_label", "iri": CUSTOMER, "label": "Client"},
        ],
        0,
    )
    structured = http.get(f"{BASE}/accept-preview{_q()}").json()["structured"]
    # VIP customer and Item have no table either, but the draft does not change them.
    assert structured["unmappedClasses"] == {
        "count": 2,
        "terms": [
            {"iri": f"{ONTO}PremiumCustomer", "label": "Premium customer"},
            {"iri": f"{ONTO}Shipment", "label": "Shipment"},
        ],
    }
    assert structured["unmappedProperties"]["terms"] == [
        {"iri": f"{ONTO}customer_loyaltyTier", "label": "loyalty tier", "domainIri": CUSTOMER}
    ]
    assert structured["labelDiffers"]["terms"] == [{"iri": CUSTOMER, "label": "Client", "physicalName": "customer"}]
    assert structured["multiTableClasses"] == {"count": 0, "terms": []}
    assert structured["mapsNoTable"] is False and structured["error"] is None


def test_accept_preview_lists_a_class_that_an_edit_to_another_term_leaves_unanswered(http):
    """Party, answered through Customer in the base version, is not changed when Customer leaves it."""
    _open(http)
    _change(http, [{"op": "remove_parent", "iri": CUSTOMER, "parent": PARTY}], 0)
    preview = http.get(f"{BASE}/accept-preview{_q()}").json()
    assert PARTY not in preview["diff"]["classes"]["changed"]
    # VIP customer and Item were not answered in the base version either.
    assert preview["structured"]["unmappedClasses"] == {
        "count": 2,
        "terms": [{"iri": PARTY, "label": "Party"}, {"iri": f"{ONTO}Shipment", "label": "Shipment"}],
    }


def test_accept_preview_lists_the_mapped_terms_the_draft_deletes(http):
    _open(http)
    _change(http, [{"op": "delete_class", "iri": CUSTOMER}, {"op": "delete_property", "iri": TOTAL}], 0)
    read = studio_router.storage.require_version_file
    with patch.object(studio_router.storage, "require_version_file", side_effect=read) as spy:
        structured = http.get(f"{BASE}/accept-preview{_q()}").json()["structured"]
    # The labels come from the base ontology the preview parsed for its diff, not from a second read.
    assert [c.args[2] for c in spy.call_args_list].count("ontology.ttl") == 1
    # Deleted, not added or changed: listed although the diff lists them as removed.
    assert structured["removedMappedClasses"] == {
        "count": 1,
        "terms": [{"iri": CUSTOMER, "label": "Customer", "tables": ["customer"]}],
    }
    # email stays, detached from the deleted class; its column goes with the customer table.
    assert structured["removedMappedProperties"] == {
        "count": 1,
        "terms": [{"iri": TOTAL, "label": "total", "domainIri": ORDER, "physicalName": "total"}],
    }


def test_accept_preview_does_not_list_a_renamed_or_merged_term_as_deleted(http):
    _open(http)
    _change(
        http,
        [
            {"op": "change_iri", "iri": CUSTOMER, "newIri": f"{ONTO}Client"},
            {"op": "change_iri", "iri": EMAIL, "newIri": f"{ONTO}client_mail"},
            {"op": "merge_classes", "source": ORDER, "target": f"{ONTO}Client"},
        ],
        0,
    )
    preview = http.get(f"{BASE}/accept-preview{_q()}").json()
    assert {CUSTOMER, ORDER} <= set(preview["diff"]["classes"]["removed"])
    structured = preview["structured"]
    assert structured["removedMappedClasses"] == structured["removedMappedProperties"] == {"count": 0, "terms": []}


def test_accept_preview_says_when_the_draft_maps_no_table_left(http):
    _open(http)
    _change(http, [{"op": "delete_class", "iri": CUSTOMER}, {"op": "delete_class", "iri": f"{ONTO}Order"}], 0)
    assert http.get(f"{BASE}/accept-preview{_q()}").json()["structured"]["mapsNoTable"] is True


def test_accept_preview_tells_a_stale_draft_to_rebase(http):
    _open(http)
    publish_base(proposal="p-other")
    preview = http.get(f"{BASE}/accept-preview{_q()}").json()
    assert preview["rebaseRequired"] is True and (preview["baseVersion"], preview["headVersion"]) == (1, 2)


def test_a_bad_namespace_is_refused(http):
    bad = http.get("/ontology/proposals/p-1/studio?namespace=../x")
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "InvalidNamespace"
    assert http.post("/ontology/proposals/p-1/studio?namespace=../x").status_code == 422


def test_the_engine_app_mounts_the_studio_routes():
    from coa_ontology.main import app

    routes = {(getattr(r, "path", ""), m) for r in app.routes for m in getattr(r, "methods", None) or ()}
    prefix = "/ontology/proposals/{proposal_id}/studio"
    for suffix, method in (
        ("", "POST"),
        ("", "GET"),
        ("/graph", "GET"),
        ("/term", "GET"),
        ("/terms", "GET"),
        ("/changes", "POST"),
        ("/undo", "POST"),
        ("/redo", "POST"),
        ("/history", "GET"),
        ("/rebase", "POST"),
        ("/accept-preview", "GET"),
    ):
        assert (f"{prefix}{suffix}", method) in routes, (method, suffix)
