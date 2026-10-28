// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach } from "vitest";
import { ApiError, type ApiClient } from "@components/ApiClientProvider";
import {
  checkStudioQuestion,
  deleteStudioQuestion,
  getStudioChecks,
  listStudioQuestions,
  newQuestionId,
  putStudioQuestion,
  runSparqlQuery,
  runStudioQuery,
  applyStudioChangeSet,
  applyStudioChanges,
  discardStudioChangeSet,
  isTurnInProgress,
  listStudioChangeSets,
  listStudioEvents,
  previewStudioChangeSet,
  sendStudioMessage,
  getStudioAcceptPreview,
  getStudioDraft,
  getStudioGraph,
  getStudioTerm,
  isDraftConflict,
  isRebaseRequired,
  listStudioHistory,
  openStudioDraft,
  rebaseFailures,
  rebaseStudioDraft,
  redoStudioChange,
  searchStudioTerms,
  studioErrorDetail,
  undoStudioChange,
} from "./studio";

const BASE = "/namespaces/ns%201/proposals/p-1/studio";

describe("Ontology designer service functions", () => {
  let api: ApiClient;

  beforeEach(() => {
    api = {
      get: vi.fn().mockResolvedValue({}),
      post: vi.fn().mockResolvedValue({}),
      put: vi.fn().mockResolvedValue({}),
      del: vi.fn().mockResolvedValue({}),
    };
  });

  it("opens and reads the draft under the encoded namespace", async () => {
    await openStudioDraft(api, "ns 1", "p-1");
    expect(api.post).toHaveBeenCalledWith(BASE, {});
    await getStudioDraft(api, "ns 1", "p-1");
    expect(api.get).toHaveBeenCalledWith(BASE);
  });

  it("encodes the proposal id too", async () => {
    await getStudioDraft(api, "ns", "p/1 x");
    expect(api.get).toHaveBeenCalledWith(
      "/namespaces/ns/proposals/p%2F1%20x/studio",
    );
  });

  it("encodes IRIs with # in the graph focus and the term query", async () => {
    await getStudioGraph(api, "ns 1", "p-1");
    expect(api.get).toHaveBeenLastCalledWith(`${BASE}/graph`);
    await getStudioGraph(api, "ns 1", "p-1", "http://e.com/o#Order");
    expect(api.get).toHaveBeenLastCalledWith(
      `${BASE}/graph?focus=http%3A%2F%2Fe.com%2Fo%23Order`,
    );
    await getStudioTerm(api, "ns 1", "p-1", "http://e.com/o#Order");
    expect(api.get).toHaveBeenLastCalledWith(
      `${BASE}/term?iri=http%3A%2F%2Fe.com%2Fo%23Order`,
    );
  });

  it("searches terms with optional kind and limit", async () => {
    await searchStudioTerms(api, "ns 1", "p-1", "cust", {
      kind: "class",
      limit: 10,
    });
    expect(api.get).toHaveBeenLastCalledWith(
      `${BASE}/terms?q=cust&kind=class&limit=10`,
    );
    await searchStudioTerms(api, "ns 1", "p-1", "cust");
    expect(api.get).toHaveBeenLastCalledWith(`${BASE}/terms?q=cust`);
  });

  it("sends every write with its expected revision", async () => {
    const ops = [{ op: "add_class" as const, label: "Supplier" }];
    await applyStudioChanges(api, "ns 1", "p-1", ops, 4);
    expect(api.post).toHaveBeenLastCalledWith(`${BASE}/changes`, {
      ops,
      expectedRevision: 4,
      dryRun: false,
    });
    await applyStudioChanges(api, "ns 1", "p-1", ops, 4, true);
    expect(api.post).toHaveBeenLastCalledWith(`${BASE}/changes`, {
      ops,
      expectedRevision: 4,
      dryRun: true,
    });
    await undoStudioChange(api, "ns 1", "p-1", 5);
    expect(api.post).toHaveBeenLastCalledWith(`${BASE}/undo`, {
      expectedRevision: 5,
    });
    await redoStudioChange(api, "ns 1", "p-1", 6);
    expect(api.post).toHaveBeenLastCalledWith(`${BASE}/redo`, {
      expectedRevision: 6,
    });
    await rebaseStudioDraft(api, "ns 1", "p-1", 7, ["3:0"]);
    expect(api.post).toHaveBeenLastCalledWith(`${BASE}/rebase`, {
      expectedRevision: 7,
      skipOps: ["3:0"],
    });
  });

  it("sends revision 0 (a draft just opened) and an empty skip list", async () => {
    await undoStudioChange(api, "ns 1", "p-1", 0);
    expect(api.post).toHaveBeenLastCalledWith(`${BASE}/undo`, {
      expectedRevision: 0,
    });
    await rebaseStudioDraft(api, "ns 1", "p-1", 0);
    expect(api.post).toHaveBeenLastCalledWith(`${BASE}/rebase`, {
      expectedRevision: 0,
      skipOps: [],
    });
  });

  it("sends replace_datatype with the wire key `from`", async () => {
    const ops = [
      {
        op: "replace_datatype" as const,
        from: "http://www.w3.org/2001/XMLSchema#datetime",
        to: "http://www.w3.org/2001/XMLSchema#dateTime",
      },
    ];
    await applyStudioChanges(api, "ns 1", "p-1", ops, 2, true);
    expect(api.post).toHaveBeenLastCalledWith(`${BASE}/changes`, {
      ops: [
        {
          op: "replace_datatype",
          from: "http://www.w3.org/2001/XMLSchema#datetime",
          to: "http://www.w3.org/2001/XMLSchema#dateTime",
        },
      ],
      expectedRevision: 2,
      dryRun: true,
    });
  });

  it("reads history and the accept preview", async () => {
    await listStudioHistory(api, "ns 1", "p-1", {
      maxResults: 20,
      nextToken: "t==",
    });
    expect(api.get).toHaveBeenLastCalledWith(
      `${BASE}/history?maxResults=20&nextToken=t%3D%3D`,
    );
    await listStudioHistory(api, "ns 1", "p-1");
    expect(api.get).toHaveBeenLastCalledWith(`${BASE}/history`);
    await getStudioAcceptPreview(api, "ns 1", "p-1");
    expect(api.get).toHaveBeenLastCalledWith(`${BASE}/accept-preview`);
  });

  it("recognises the engine's structured conflicts", () => {
    const conflict = new ApiError(409, "DraftConflict", "moved", {
      detail: { code: "DraftConflict", message: "moved", revision: 9 },
    });
    expect(isDraftConflict(conflict)).toBe(true);
    expect(isRebaseRequired(conflict)).toBe(false);
    const failed = new ApiError(409, "RebaseFailed", "2 ops", {
      detail: {
        code: "RebaseFailed",
        failures: [
          { opId: "3:0", op: { op: "add_class", label: "X" }, error: "taken" },
        ],
      },
    });
    expect(rebaseFailures(failed)).toEqual([
      { opId: "3:0", op: { op: "add_class", label: "X" }, error: "taken" },
    ]);
    expect(rebaseFailures(conflict)).toEqual([]);
    expect(rebaseFailures(new Error("x"))).toEqual([]);
  });
});

describe("Ontology designer error helpers", () => {
  it("reads the extra fields of a structured detail", () => {
    const conflict = new ApiError(409, "DraftConflict", "moved", {
      detail: { code: "DraftConflict", message: "moved", revision: 9 },
    });
    expect(studioErrorDetail(conflict)).toEqual({
      code: "DraftConflict",
      message: "moved",
      revision: 9,
    });
    const rebase = new ApiError(409, "RebaseRequired", "rebase first", {
      detail: {
        code: "RebaseRequired",
        message: "rebase first",
        baseVersion: 1,
        headVersion: 2,
      },
    });
    expect(isRebaseRequired(rebase)).toBe(true);
    expect(isDraftConflict(rebase)).toBe(false);
    expect(studioErrorDetail(rebase)).toMatchObject({
      baseVersion: 1,
      headVersion: 2,
    });
    const invalid = new ApiError(422, "InvalidChange", "Op 1: bad", {
      detail: { code: "InvalidChange", message: "Op 1: bad", opIndex: 1 },
    });
    expect(studioErrorDetail(invalid)?.opIndex).toBe(1);
  });

  it("answers null or empty, without throwing, for every error that is not a structured detail", () => {
    const withBody = (body: unknown) =>
      new ApiError(409, "UNKNOWN_ERROR", "failed", body);
    const notStudio: unknown[] = [
      // The accept 409s that predate the designer carry a string detail.
      withBody({ detail: "Accept already in progress for this proposal" }),
      // A missing or mistyped field is FastAPI's own list.
      new ApiError(422, "UNKNOWN_ERROR", "API POST failed (422)", {
        detail: [
          {
            type: "missing",
            loc: ["body", "expectedRevision"],
            msg: "Required",
          },
        ],
      }),
      new ApiError(500, "UNKNOWN_ERROR", "no body"),
      withBody(undefined),
      withBody(null),
      withBody("plain text"),
      withBody(7),
      withBody([]),
      withBody({}),
      withBody({ detail: null }),
      withBody({ detail: 5 }),
      new Error("network down"),
      "oops",
      null,
      undefined,
    ];
    for (const e of notStudio) {
      expect(studioErrorDetail(e)).toBeNull();
      expect(isDraftConflict(e)).toBe(false);
      expect(isRebaseRequired(e)).toBe(false);
      expect(rebaseFailures(e)).toEqual([]);
    }
  });

  it("returns no failures when a RebaseFailed carries none it can read", () => {
    for (const body of [
      undefined,
      { detail: "text" },
      { detail: [{ msg: "x" }] },
      { detail: { code: "RebaseFailed" } },
      { detail: { code: "RebaseFailed", failures: "none" } },
    ]) {
      expect(
        rebaseFailures(new ApiError(409, "RebaseFailed", "x", body)),
      ).toEqual([]);
    }
  });
});

describe("Ontology designer chat service functions", () => {
  let api: ApiClient;
  const CS = `${BASE}/changesets/20261020T0940Z-7eea78bc`;

  beforeEach(() => {
    api = {
      get: vi.fn().mockResolvedValue({}),
      post: vi.fn().mockResolvedValue({}),
      put: vi.fn().mockResolvedValue({}),
      del: vi.fn().mockResolvedValue({}),
    };
  });

  it("sends a message with the selected IRIs", async () => {
    await sendStudioMessage(api, "ns 1", "p-1", "Add a parent", [
      "http://e.com/o#Order",
    ]);
    expect(api.post).toHaveBeenLastCalledWith(`${BASE}/messages`, {
      text: "Add a parent",
      selection: ["http://e.com/o#Order"],
    });
    await sendStudioMessage(api, "ns 1", "p-1", "Hi");
    expect(api.post).toHaveBeenLastCalledWith(`${BASE}/messages`, {
      text: "Hi",
      selection: [],
    });
  });

  it("polls events after a seq", async () => {
    await listStudioEvents(api, "ns 1", "p-1");
    expect(api.get).toHaveBeenLastCalledWith(`${BASE}/events?after=0`);
    await listStudioEvents(api, "ns 1", "p-1", 12, 50);
    expect(api.get).toHaveBeenLastCalledWith(
      `${BASE}/events?after=12&limit=50`,
    );
  });

  it("lists change sets, optionally by status", async () => {
    await listStudioChangeSets(api, "ns 1", "p-1");
    expect(api.get).toHaveBeenLastCalledWith(`${BASE}/changesets`);
    await listStudioChangeSets(api, "ns 1", "p-1", { status: "pending" });
    expect(api.get).toHaveBeenLastCalledWith(
      `${BASE}/changesets?status=pending`,
    );
    await listStudioChangeSets(api, "ns 1", "p-1", {
      maxResults: 50,
      nextToken: "a+b",
    });
    expect(api.get).toHaveBeenLastCalledWith(
      `${BASE}/changesets?maxResults=50&nextToken=a%2Bb`,
    );
  });

  it("previews selected ops around the encoded focus", async () => {
    await previewStudioChangeSet(api, "ns 1", "p-1", "20261020T0940Z-7eea78bc");
    expect(api.get).toHaveBeenLastCalledWith(`${CS}/preview`);
    await previewStudioChangeSet(
      api,
      "ns 1",
      "p-1",
      "20261020T0940Z-7eea78bc",
      [0, 2],
      "http://e.com/o#Order",
    );
    expect(api.get).toHaveBeenLastCalledWith(
      `${CS}/preview?opIndexes=0%2C2&focus=http%3A%2F%2Fe.com%2Fo%23Order`,
    );
  });

  it("applies all ops or some, and discards", async () => {
    await applyStudioChangeSet(
      api,
      "ns 1",
      "p-1",
      "20261020T0940Z-7eea78bc",
      9,
    );
    expect(api.post).toHaveBeenLastCalledWith(`${CS}/apply`, {
      expectedRevision: 9,
    });
    await applyStudioChangeSet(
      api,
      "ns 1",
      "p-1",
      "20261020T0940Z-7eea78bc",
      9,
      [1],
    );
    expect(api.post).toHaveBeenLastCalledWith(`${CS}/apply`, {
      expectedRevision: 9,
      opIndexes: [1],
    });
    await discardStudioChangeSet(api, "ns 1", "p-1", "20261020T0940Z-7eea78bc");
    expect(api.post).toHaveBeenLastCalledWith(`${CS}/discard`, {});
  });

  it("reads the checks and runs console queries", async () => {
    await getStudioChecks(api, "ns 1", "p-1");
    expect(api.get).toHaveBeenLastCalledWith(`${BASE}/checks`);
    await runStudioQuery(api, "ns 1", "p-1", "sparql", "draft", "ASK {}");
    expect(api.post).toHaveBeenLastCalledWith(`${BASE}/queries`, {
      language: "sparql",
      target: "draft",
      query: "ASK {}",
    });
    await runSparqlQuery(api, "ns 1", "SELECT * {}");
    expect(api.post).toHaveBeenLastCalledWith("/namespaces/ns%201/sparql", {
      sparql: "SELECT * {}",
    });
  });

  it("keeps competency questions under the namespace and checks them on a draft", async () => {
    const questions = "/namespaces/ns%201/studio/questions";
    await listStudioQuestions(api, "ns 1");
    expect(api.get).toHaveBeenLastCalledWith(questions);
    await listStudioQuestions(api, "ns 1", {
      maxResults: 100,
      nextToken: "t+1",
    });
    expect(api.get).toHaveBeenLastCalledWith(
      `${questions}?maxResults=100&nextToken=t%2B1`,
    );
    await putStudioQuestion(api, "ns 1", "q-1", "Who?", null);
    expect(api.put).toHaveBeenLastCalledWith(`${questions}/q-1`, {
      text: "Who?",
    });
    await putStudioQuestion(api, "ns 1", "q-1", "Who?", "Names");
    expect(api.put).toHaveBeenLastCalledWith(`${questions}/q-1`, {
      text: "Who?",
      expectation: "Names",
    });
    await deleteStudioQuestion(api, "ns 1", "q-1");
    expect(api.del).toHaveBeenLastCalledWith(`${questions}/q-1`);
    await checkStudioQuestion(api, "ns 1", "p-1", "q-1");
    expect(api.post).toHaveBeenLastCalledWith(
      `${BASE}/questions/q-1/check`,
      {},
    );
    expect(newQuestionId()).toMatch(/^[0-9A-Za-z-]{1,64}$/);
  });

  it("recognizes a running turn", () => {
    expect(isTurnInProgress(new ApiError(409, "TurnInProgress", "busy"))).toBe(
      true,
    );
    expect(isTurnInProgress(new ApiError(409, "DraftConflict", "x"))).toBe(
      false,
    );
  });
});
