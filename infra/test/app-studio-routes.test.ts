// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as fs from "fs";
import * as path from "path";
import { EXPENSIVE_API_OPERATIONS } from "../lib/constants";

/**
 * Every Ontology designer route in the Smithy model has an API Gateway handler.
 *
 * A spec path without an `ssmPathHandlers` entry is wired to the 501 stub, so
 * a route added to `graph-studio.smithy` but not to `infra/bin/app.ts` would
 * deploy and answer 501. A source assertion, as in app-connector-tag.test.ts:
 * `bin/app.ts` reads SSM at module scope and cannot be imported in a test.
 */
const APP = fs.readFileSync(
  path.join(__dirname, "..", "bin", "app.ts"),
  "utf8",
);
const SMITHY = fs.readFileSync(
  path.join(
    __dirname,
    "..",
    "..",
    "models",
    "src",
    "main",
    "smithy",
    "graph-studio.smithy",
  ),
  "utf8",
);

describe("Ontology designer routes", () => {
  // GetStudioDraft and OpenStudioDraft share one path, as do PutStudioQuestion and DeleteStudioQuestion.
  const uris = [
    ...new Set([...SMITHY.matchAll(/uri: "([^"]+)"/g)].map((m) => m[1])),
  ];

  it("are all found in the model", () => {
    expect(uris).toHaveLength(21);
  });

  it.each(uris)("%s goes to the ontology-engine API function", (uri) => {
    expect(APP).toContain(
      `"${uri}": \`/\${prefix}/ontology-engine/api-fn-arn\``,
    );
  });
});

describe("Ontology designer throttling", () => {
  it.each([
    "/namespaces/{namespaceId}/proposals/{proposalId}/studio/messages",
    "/namespaces/{namespaceId}/proposals/{proposalId}/studio/queries",
    "/namespaces/{namespaceId}/proposals/{proposalId}/studio/questions/{questionId}/check",
  ])("throttles %s, which runs an agent turn or a graph query", (path) => {
    expect(EXPENSIVE_API_OPERATIONS).toContainEqual({ method: "POST", path });
  });
});

describe("VKG SPARQL of the query console", () => {
  it("goes to the data-layer function and is throttled", () => {
    expect(APP).toContain(
      '"/namespaces/{namespaceId}/sparql": `/${prefix}/data-layer/api-fn-arn`',
    );
    expect(EXPENSIVE_API_OPERATIONS).toContainEqual({
      method: "POST",
      path: "/namespaces/{namespaceId}/sparql",
    });
  });
});
