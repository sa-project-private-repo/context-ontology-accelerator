// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import type { OntologyDeployment } from "../../../services/ontology-engine";
import {
  autoDeployHeldMessage,
  deployActionLabel,
  isRetiredVersion,
  deploymentStatusDisplay,
  deployStepState,
  diffSummaryLabel,
  publishedHead,
  versionStatusDisplay,
} from "./versions-helpers";

describe("publishedHead", () => {
  it("is the newest published version above the reset floor", () => {
    const versions = [
      { version: 4, status: "PUBLISH_FAILED" },
      { version: 3, status: "PUBLISHING" },
      { version: 2, status: "PUBLISHED" },
      { version: 1, status: "PUBLISHED" },
    ];
    expect(publishedHead(versions, null)).toBe(2);
    expect(publishedHead([...versions].reverse(), null)).toBe(2); // in any order
    expect(publishedHead(versions, 2)).toBeNull(); // every published one is retired
    expect(publishedHead([], undefined)).toBeNull();
  });
});

describe("versionStatusDisplay", () => {
  it("maps each status", () => {
    expect(versionStatusDisplay("PUBLISHED")).toEqual({
      type: "success",
      label: "Published",
    });
    expect(versionStatusDisplay("PUBLISHING")).toEqual({
      type: "in-progress",
      label: "Publishing…",
    });
    expect(versionStatusDisplay("PUBLISH_FAILED")).toEqual({
      type: "error",
      label: "Publish failed",
    });
  });

  it("falls back to pending with the raw status", () => {
    expect(versionStatusDisplay("ARCHIVED")).toEqual({
      type: "pending",
      label: "ARCHIVED",
    });
  });
});

describe("deploymentStatusDisplay", () => {
  it("separates a partial failure from a clean one", () => {
    expect(
      deploymentStatusDisplay({ status: "DEPLOY_FAILED", partial: true }).type,
    ).toBe("warning");
    expect(
      deploymentStatusDisplay({ status: "DEPLOY_FAILED", partial: false }).type,
    ).toBe("error");
    expect(
      deploymentStatusDisplay({ status: "DEPLOYING", partial: false }).type,
    ).toBe("in-progress");
    expect(
      deploymentStatusDisplay({ status: "DEPLOYED", partial: false }).type,
    ).toBe("success");
  });

  it("falls back to pending with the raw status", () => {
    expect(
      deploymentStatusDisplay({
        status: "QUEUED" as OntologyDeployment["status"],
        partial: false,
      }),
    ).toEqual({ type: "pending", label: "QUEUED" });
  });

  it("reports a retired version's deployment as retired, never deployed or to finish", () => {
    const retired = {
      type: "stopped",
      label: "Retired: its ontology was deleted",
    };
    expect(
      deploymentStatusDisplay(
        { status: "DEPLOYED", partial: false },
        undefined,
        true,
      ),
    ).toEqual(retired);
    expect(
      deploymentStatusDisplay(
        { status: "DEPLOY_FAILED", partial: true },
        undefined,
        true,
      ),
    ).toEqual(retired);
  });
});

describe("deployStepState", () => {
  it("marks the first unfinished step of a running deployment in progress", () => {
    const running = {
      status: "DEPLOYING" as const,
      progress: { stage: "done", swap: "done" } as const,
    };
    expect(deployStepState(running, "stage")).toBe("done");
    expect(deployStepState(running, "swap")).toBe("done");
    expect(deployStepState(running, "embeddings")).toBe("in-progress");
    expect(deployStepState(running, "registry")).toBe("pending");
    expect(deployStepState(running, "notify")).toBe("pending");
  });

  it("starts a fresh deployment at the first step", () => {
    const fresh = { status: "DEPLOYING" as const, progress: {} };
    expect(deployStepState(fresh, "stage")).toBe("in-progress");
    expect(deployStepState(fresh, "swap")).toBe("pending");
  });

  it("reports recorded steps and leaves the rest pending once finished", () => {
    const failed = {
      status: "DEPLOY_FAILED" as const,
      progress: { stage: "done", swap: "failed" } as const,
    };
    expect(deployStepState(failed, "stage")).toBe("done");
    expect(deployStepState(failed, "swap")).toBe("failed");
    expect(deployStepState(failed, "embeddings")).toBe("pending");
  });
});

describe("deployActionLabel", () => {
  it("names deploy, redeploy and rollback", () => {
    expect(deployActionLabel(2, null)).toBe("Deploy");
    expect(deployActionLabel(2, 2)).toBe("Redeploy");
    expect(deployActionLabel(1, 2)).toBe("Roll back");
    expect(deployActionLabel(3, 2)).toBe("Deploy");
  });

  it("offers a redeploy of the version a partial deployment left behind", () => {
    const partial = { version: 3, partial: true };
    expect(deployActionLabel(3, 2, partial)).toBe("Redeploy");
    expect(deployActionLabel(3, null, partial)).toBe("Redeploy");
    expect(deployActionLabel(1, 2, partial)).toBe("Roll back");
    expect(deployActionLabel(3, 2, { version: 3, partial: false })).toBe(
      "Deploy",
    );
  });
});

describe("isRetiredVersion", () => {
  it("retires the versions at or below the reset floor", () => {
    expect(isRetiredVersion(1, 2)).toBe(true);
    expect(isRetiredVersion(2, 2)).toBe(true);
    expect(isRetiredVersion(3, 2)).toBe(false);
  });

  it("retires nothing without a floor", () => {
    expect(isRetiredVersion(1, null)).toBe(false);
    expect(isRetiredVersion(1, undefined)).toBe(false);
    expect(isRetiredVersion(1, 0)).toBe(false);
  });
});

describe("diffSummaryLabel", () => {
  it("summarises class changes", () => {
    expect(
      diffSummaryLabel({ classes: { added: 3, removed: 1, changed: 0 } }),
    ).toBe("Classes +3 / −1");
    expect(
      diffSummaryLabel({ classes: { added: 0, removed: 0, changed: 2 } }),
    ).toBe("Classes +0 / −0 / ~2");
    expect(diffSummaryLabel(null)).toBe("—");
    expect(diffSummaryLabel(undefined)).toBe("—");
    expect(diffSummaryLabel({})).toBe("—");
  });

  it("adds a property change count when properties changed", () => {
    expect(
      diffSummaryLabel({
        classes: { added: 3, removed: 1, changed: 0 },
        objectProperties: { added: 1, removed: 0, changed: 1 },
        datatypeProperties: { added: 0, removed: 2, changed: 0 },
      }),
    ).toBe("Classes +3 / −1, properties ~4");
    expect(
      diffSummaryLabel({
        classes: { added: 3, removed: 1, changed: 0 },
        objectProperties: { added: 0, removed: 0, changed: 0 },
        datatypeProperties: { added: 0, removed: 0, changed: 0 },
      }),
    ).toBe("Classes +3 / −1");
  });
});

describe("autoDeployHeldMessage", () => {
  it("does not offer a retry for a version of a deleted ontology", () => {
    expect(
      autoDeployHeldMessage({
        reason: "DEPLOY_FAILED_TO_START",
        error:
          "Version v1 belongs to a deleted ontology; accept a proposal to publish it again.",
      }),
    ).toBe(
      "The deployment could not start: Version v1 belongs to a deleted ontology; accept a proposal to publish it again.",
    );
  });

  it("never doubles the period after a reason that ends with one", () => {
    const messages = [
      autoDeployHeldMessage({
        reason: "DEPLOY_FAILED_TO_START",
        error: "boom.",
      }),
      autoDeployHeldMessage({ reason: "DEPLOY_FAILED_TO_START" }),
      autoDeployHeldMessage({
        reason: "BREAKING_METRICS",
        metrics: [
          { metricId: "r", name: "Revenue (net).", missingClassIri: "x" },
        ],
      }),
      autoDeployHeldMessage({ reason: "DEPLOY_IN_PROGRESS" }),
    ];
    for (const m of messages) expect(m).not.toContain("..");
    expect(messages[0]).toBe(
      "The deployment could not start: boom. Retry from Ontology › Induction › Versions.",
    );
  });

  it("explains each hold reason", () => {
    expect(
      autoDeployHeldMessage({
        reason: "BREAKING_METRICS",
        metrics: [
          { metricId: "revenue", name: "revenue", missingClassIri: "x" },
        ],
      }),
    ).toContain("revenue");
    expect(autoDeployHeldMessage({ reason: "DEPLOY_IN_PROGRESS" })).toContain(
      "Another deployment",
    );
    expect(
      autoDeployHeldMessage({
        reason: "DEPLOY_FAILED_TO_START",
        error: "boom",
      }),
    ).toContain("boom");
  });

  it("names a metric once even when it loses several classes", () => {
    expect(
      autoDeployHeldMessage({
        reason: "BREAKING_METRICS",
        metrics: [
          { metricId: "revenue", name: "revenue", missingClassIri: "x" },
          { metricId: "revenue", name: "revenue", missingClassIri: "y" },
          { metricId: "orders", name: "orders", missingClassIri: "x" },
        ],
      }),
    ).toContain("governed metrics: revenue, orders.");
  });
});
