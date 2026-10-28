// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect } from "vitest";
import { ApiError } from "@components/ApiClientProvider";
import {
  badgeFor,
  diffCounts,
  errorMessage,
  iriTail,
  MAX_REQUEST_BYTES,
  requestBytes,
  revisionLine,
  sqlName,
  toFlowEdges,
  toFlowNodes,
} from "./studio-helpers";
import type { StudioGraph, StudioGraphNode } from "../../../services/studio";

const NODE: StudioGraphNode = {
  iri: "http://e/o#Customer",
  label: "Customer",
  grounded: false,
  editable: true,
  attributes: [{ iri: "http://e/o#email", label: "email", range: null }],
  attributeCount: 1,
  aboxCount: 12,
  afterDeploy: false,
};
const CURRENT = {
  status: "READY",
  current: true,
  conformsToVersion: 3,
  activeVersion: 3,
  reason: null,
};

describe("studio helpers", () => {
  it("shows the tail of an IRI", () => {
    expect(iriTail("http://e/o#Customer")).toBe("Customer");
    expect(iriTail("http://e/o/class/%EA%B3%A0%EA%B0%9D")).toBe("고객");
    expect(iriTail("http://e/o/")).toBe("o");
  });

  it("shows an IRI tail that is not valid percent-encoding as it is", () => {
    // decodeURIComponent throws URIError on a stray "%"; a render must not.
    expect(iriTail("http://e/o#100%")).toBe("100%");
    expect(iriTail("http://e/o#%E0%A4%A")).toBe("%E0%A4%A");
  });

  it("badges count, after-deploy and out-of-date classes", () => {
    expect(badgeFor(NODE, null)).toBeUndefined();
    expect(badgeFor(NODE, CURRENT)).toEqual({
      text: "12",
      title: "12 instances in the active version's domain data",
      dimmed: false,
    });
    expect(badgeFor({ ...NODE, aboxCount: 1 }, CURRENT)?.title).toBe(
      "1 instance in the active version's domain data",
    );
    expect(
      badgeFor(NODE, {
        ...CURRENT,
        current: false,
        reason: "Built for v2; v3 is active",
      }),
    ).toEqual({
      text: "12",
      title:
        "12 instances in the active version's domain data (Built for v2; v3 is active)",
      dimmed: true,
    });
    expect(
      badgeFor({ ...NODE, aboxCount: null, afterDeploy: true }, CURRENT)?.text,
    ).toBe("new");
    expect(badgeFor({ ...NODE, aboxCount: null }, CURRENT)).toBeUndefined();
  });

  it("maps the graph API onto the flow component", () => {
    const graph: StudioGraph = {
      revision: 1,
      mode: "full",
      focus: null,
      totalClasses: 2,
      truncated: false,
      nodes: [NODE, { ...NODE, iri: "http://e/o#Order", grounded: true }],
      edges: [
        {
          id: "op|p|c|o",
          kind: "objectProperty",
          source: "http://e/o#Order",
          target: "http://e/o#Customer",
          iri: "http://e/o#placedBy",
          label: "placed by",
        },
        {
          id: "sub|a|b",
          kind: "subClassOf",
          source: "http://e/o#Order",
          target: "http://e/o#Customer",
        },
      ],
      abox: CURRENT,
    };
    const nodes = toFlowNodes(graph);
    expect(nodes[0]).toMatchObject({
      id: "http://e/o#Customer",
      kind: "class",
      attributes: [{ label: "email", range: undefined }],
      badge: { text: "12" },
    });
    expect(nodes[1].kind).toBe("grounded");
    expect(toFlowEdges(graph).map((e) => [e.kind, e.label])).toEqual([
      ["objectProperty", "placed by"],
      ["subClassOf", undefined],
    ]);
  });

  it("carries a preview's marks as highlights", () => {
    const edge = {
      id: "sub|a|b",
      kind: "subClassOf" as const,
      source: "http://e/o#Order",
      target: "http://e/o#Customer",
    };
    const graph: StudioGraph = {
      revision: 1,
      mode: "full",
      focus: null,
      totalClasses: 2,
      truncated: false,
      nodes: [NODE, { ...NODE, iri: "http://e/o#Order" }],
      edges: [edge, { ...edge, id: "sub|b|a" }],
      abox: CURRENT,
    };
    const preview = {
      ...graph,
      nodes: [
        { ...graph.nodes[0], preview: "added" as const },
        { ...graph.nodes[1], preview: null },
      ],
      edges: [
        { ...graph.edges[0], preview: "changed" as const },
        { ...graph.edges[1], preview: null },
      ],
    };
    expect(toFlowNodes(preview).map((n) => n.highlight)).toEqual([
      "added",
      undefined,
    ]);
    expect(toFlowEdges(preview).map((e) => e.highlight)).toEqual([
      "changed",
      undefined,
    ]);
    expect(toFlowNodes(graph).every((n) => n.highlight === undefined)).toBe(
      true,
    );
  });

  it("describes the revision and counts a diff", () => {
    expect(revisionLine({ revision: 7, baseVersion: 3 } as never)).toBe(
      "Revision 7, built on v3",
    );
    expect(revisionLine({ revision: 0, baseVersion: null } as never)).toBe(
      "Revision 0, built on no version",
    );
    expect(
      diffCounts({
        classes: { added: ["a"], removed: ["b", "c"], changed: [] },
        objectProperties: { added: [], removed: ["d"], changed: ["e"] },
        datatypeProperties: { added: [], removed: [], changed: [] },
      }),
    ).toEqual({ added: 1, removed: 3, changed: 1 });
  });

  it("says what went wrong in words the engine chose, or in general ones", () => {
    const studio = new ApiError(409, "InvalidChange", "x is not a class", {
      detail: { code: "InvalidChange", message: "x is not a class" },
    });
    const plain = new ApiError(409, "UNKNOWN_ERROR", "Cannot accept", {
      detail: "Cannot accept a rejected proposal",
    });
    const list = new ApiError(422, "UNKNOWN_ERROR", "API POST /x failed", {
      detail: [{ loc: ["body", "ops"], msg: "field required" }],
    });
    const gateway = new ApiError(404, "UNKNOWN_ERROR", "Not Found", {
      message: "Missing Authentication Token",
    });
    const noBody = new ApiError(502, "UNKNOWN_ERROR", "API GET /x failed");
    expect(errorMessage(studio)).toBe("x is not a class");
    expect(errorMessage(plain)).toBe("Cannot accept a rejected proposal");
    expect(errorMessage(list)).toBe(
      "The request failed (HTTP 422). Reload the page and try again.",
    );
    expect(errorMessage(gateway)).toBe("Missing Authentication Token");
    expect(errorMessage(noBody)).toBe(
      "The request failed (HTTP 502). Reload the page and try again.",
    );
    expect(errorMessage(new TypeError("Failed to fetch"))).toBe(
      "Failed to fetch",
    );
  });

  it("explains the bare 403 of the API's WAF instead of showing 'Forbidden'", () => {
    // API Gateway's answer for a request the WAF blocked, as the client parses it.
    const blocked = new ApiError(403, "UNKNOWN_ERROR", "Forbidden", {
      message: "Forbidden",
    });
    expect(errorMessage(blocked)).toMatch(
      /^The request was blocked before it reached the server, most likely because it is larger than 8 KB/,
    );
    // A refusal the engine words itself is shown as it is.
    const engine = new ApiError(
      403,
      "UNKNOWN_ERROR",
      "This endpoint is internal",
      {
        detail: "This endpoint is internal",
      },
    );
    expect(errorMessage(engine)).toBe("This endpoint is internal");
  });

  it("words an authorizer's refusal as the console's permission message", () => {
    // The authorizer denies a role that may read the draft but not change it.
    for (const body of [
      {
        message:
          "User is not authorized to access this resource with an explicit deny in an identity-based policy",
      },
      { Message: "User is not authorized to access this resource" },
      undefined,
    ]) {
      const denied = new ApiError(403, "FORBIDDEN", "denied", body);
      expect(errorMessage(denied)).toBe(
        "You are not authorized to perform this action.",
      );
    }
  });

  it("measures a request body in the UTF-8 bytes the API client sends", () => {
    expect(requestBytes({ text: "abc" })).toBe('{"text":"abc"}'.length);
    // Hangul takes three bytes a character.
    expect(requestBytes({ text: "가".repeat(2800) })).toBe(
      '{"text":""}'.length + 3 * 2800,
    );
    expect(requestBytes({ text: "가".repeat(2800) })).toBeGreaterThan(
      MAX_REQUEST_BYTES,
    );
    expect(requestBytes({ text: "a".repeat(4000) })).toBeLessThan(
      MAX_REQUEST_BYTES,
    );
  });
});

describe("sqlName", () => {
  it("is the last segment of a mapping's table or column, without its SQL quotes", () => {
    expect(sqlName('"email"')).toBe("email");
    expect(sqlName("email")).toBe("email");
    expect(sqlName('"sales"."orders"')).toBe("orders");
    expect(sqlName('"q1.results"')).toBe("q1.results");
    expect(sqlName('"say""hi"')).toBe('say"hi');
  });
});
