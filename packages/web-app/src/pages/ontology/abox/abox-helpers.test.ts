// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import type { DomainAbox } from "../../../services/ontology-engine";
import {
  aboxStatusDisplay,
  iriTail,
  isOutOfDate,
  isPolling,
  mappingKeyLabel,
  mappingTargetLabel,
  sourceFileName,
  targetSearchKind,
  versionLine,
} from "./abox-helpers";

const READY: DomainAbox = {
  status: "READY",
  current: true,
  conformsToVersion: 3,
  activeVersion: 3,
  counts: { instances: 0, relations: 0, attributes: 0, evidence: 0 },
  classCounts: [],
  pending: false,
};

describe("aboxStatusDisplay", () => {
  it("maps each status", () => {
    expect(aboxStatusDisplay({ status: "BUILDING", pending: false })).toEqual({
      type: "in-progress",
      label: "Building",
    });
    expect(aboxStatusDisplay({ status: "READY", pending: false })).toEqual({
      type: "success",
      label: "Ready",
    });
    expect(aboxStatusDisplay({ status: "FAILED", pending: false })).toEqual({
      type: "error",
      label: "Build failed",
    });
    expect(aboxStatusDisplay({ status: "EMPTY", pending: false })).toEqual({
      type: "stopped",
      label: "Not built",
    });
  });

  it("shows a queued rebuild over a settled status, but not over a running build", () => {
    expect(aboxStatusDisplay({ status: "READY", pending: true })).toEqual({
      type: "pending",
      label: "Rebuild queued",
    });
    expect(aboxStatusDisplay({ status: "FAILED", pending: true }).label).toBe(
      "Rebuild queued",
    );
    expect(aboxStatusDisplay({ status: "BUILDING", pending: true }).label).toBe(
      "Building",
    );
  });
});

describe("versionLine", () => {
  it("names the version built for and the active one", () => {
    expect(
      versionLine({ ...READY, conformsToVersion: 2, activeVersion: 3 }),
    ).toBe("Built for v2, active v3");
  });

  it("says none when nothing is active any more", () => {
    expect(
      versionLine({ ...READY, conformsToVersion: 2, activeVersion: null }),
    ).toBe("Built for v2, no active version");
  });

  it("names only the active version before the first build", () => {
    expect(
      versionLine({ ...READY, conformsToVersion: null, activeVersion: 1 }),
    ).toBe("Active v1");
  });

  it("says when no version is deployed at all", () => {
    expect(
      versionLine({
        ...READY,
        conformsToVersion: undefined,
        activeVersion: undefined,
      }),
    ).toBe("No ontology version deployed");
  });
});

describe("isPolling", () => {
  it("polls while a build runs or is queued", () => {
    expect(isPolling({ ...READY, status: "BUILDING" })).toBe(true);
    expect(isPolling({ ...READY, pending: true })).toBe(true);
  });

  it("does not poll a settled or missing state", () => {
    expect(isPolling(READY)).toBe(false);
    expect(isPolling({ ...READY, status: "FAILED" })).toBe(false);
    expect(isPolling(undefined)).toBe(false);
  });
});

describe("mappingKeyLabel", () => {
  const labels: Record<string, string> = {
    "https://e/o#Customer": "Customer",
    "https://e/o#Order": "Order",
  };
  const label = (iri: string) => labels[iri] ?? iri;

  it("shows a class key as its lexical class", () => {
    expect(mappingKeyLabel({ kind: "class", key: ["Client"] })).toBe("Client");
  });

  it("shows a relation key as subject -relation-> object", () => {
    expect(
      mappingKeyLabel(
        {
          kind: "relation",
          key: ["PLACED", "https://e/o#Customer", "https://e/o#Order"],
        },
        label,
      ),
    ).toBe("Customer -PLACED-> Order");
  });

  it("shows an attribute key as subject: predicate", () => {
    expect(
      mappingKeyLabel(
        { kind: "attribute", key: ["https://e/o#Customer", "email"] },
        label,
      ),
    ).toBe("Customer: email");
  });

  it("falls back to the raw IRIs and tolerates short keys", () => {
    expect(
      mappingKeyLabel({ kind: "attribute", key: ["https://e/o#X", "p"] }),
    ).toBe("https://e/o#X: p");
    expect(mappingKeyLabel({ kind: "class", key: [] })).toBe("");
    expect(mappingKeyLabel({ kind: "relation", key: ["R"] })).toBe(" -R-> ");
    expect(mappingKeyLabel({ kind: "attribute", key: [] })).toBe(": ");
  });
});

describe("mappingTargetLabel", () => {
  const row = {
    kind: "relation" as const,
    target: "https://e/o#placedBy",
    targetLabel: "placed by",
    direction: "forward" as const,
  };

  it("names the special targets", () => {
    expect(mappingTargetLabel({ ...row, target: "ignore" })).toBe("Ignored");
    expect(
      mappingTargetLabel({ ...row, kind: "attribute", target: "free" }),
    ).toBe("Free attribute");
  });

  it("marks an inverse relation", () => {
    expect(mappingTargetLabel({ ...row, direction: "inverse" })).toBe(
      "placed by (inverse)",
    );
  });

  it("shows the target label otherwise", () => {
    expect(mappingTargetLabel(row)).toBe("placed by");
    expect(
      mappingTargetLabel({ ...row, kind: "class", direction: "inverse" }),
    ).toBe("placed by");
  });
});

describe("targetSearchKind", () => {
  it("searches the TBox term kind each mapping kind targets", () => {
    expect(targetSearchKind("class")).toBe("class");
    expect(targetSearchKind("relation")).toBe("object-property");
    expect(targetSearchKind("attribute")).toBe("datatype-property");
  });
});

describe("sourceFileName", () => {
  it("keeps the last path segment of a source key", () => {
    expect(sourceFileName("docs/2026/report.pdf")).toBe("report.pdf");
    expect(sourceFileName("report.pdf")).toBe("report.pdf");
  });

  it("returns an empty name for an empty key", () => {
    expect(sourceFileName("")).toBe("");
  });
});

describe("isOutOfDate", () => {
  it("flags an ABox built for another version than the active one", () => {
    expect(isOutOfDate({ conformsToVersion: 2, current: false })).toBe(true);
    expect(isOutOfDate({ conformsToVersion: 2, current: true })).toBe(false);
  });

  it("does not flag an ABox that was never built", () => {
    expect(isOutOfDate({ conformsToVersion: null, current: false })).toBe(
      false,
    );
    expect(isOutOfDate({ current: false })).toBe(false);
  });
});

describe("iriTail", () => {
  it("keeps the last fragment or path segment, percent-decoded", () => {
    expect(iriTail("https://e/o#Customer")).toBe("Customer");
    expect(iriTail("https://e/o/Sales%20Order")).toBe("Sales Order");
    expect(iriTail("https://e/o/Order/")).toBe("Order");
  });

  it("keeps a malformed escape as written", () => {
    expect(iriTail("https://e/o#100%")).toBe("100%");
  });

  it("falls back to the IRI when it has no tail", () => {
    expect(iriTail("urn:x")).toBe("urn:x");
    expect(iriTail("")).toBe("");
  });
});
