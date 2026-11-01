// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { test, expect } from "../../fixtures/test";
import {
  readE2EEnv,
  readDesignerNamespaceId,
  requireDesignerNamespaceId,
  MISSING_ENV_REASON,
  MISSING_DESIGNER_NAMESPACE_REASON,
} from "../../fixtures/env";

/**
 * The Ontology page's Versions and Domain data tabs for the designer
 * namespace (E2E_DESIGNER_NAMESPACE_ID), which has a deployed version and
 * built domain data. Read-only: nothing is deployed, rolled back or rebuilt.
 */
test.describe("ontology-designer: versions and domain data", () => {
  test.skip(!readE2EEnv(), MISSING_ENV_REASON);
  test.skip(!readDesignerNamespaceId(), MISSING_DESIGNER_NAMESPACE_REASON);
  test.setTimeout(60_000);

  test("the Versions tab lists the versions and marks the active one", async ({
    ontologyPage,
  }) => {
    await ontologyPage.goto(requireDesignerNamespaceId());
    const { versions, active } = await ontologyPage.openVersions();
    if (active === null) {
      throw new Error("the namespace has no active ontology version");
    }

    // Once loaded, the table lists the versions the engine did, a row each.
    await expect(ontologyPage.versionsHeading).toHaveText(
      `Versions (${versions.length})`,
    );
    await expect(ontologyPage.versionRows).toHaveCount(versions.length);
    for (const version of versions) {
      await expect(ontologyPage.versionRow(version)).toHaveCount(1);
    }
    // One version is served, and its row says so.
    await expect(ontologyPage.versionRow(active)).toContainText("Active");
    await expect(
      ontologyPage.versionsTable.getByText("Active", { exact: true }),
    ).toHaveCount(1);
  });

  test("the Domain data tab shows the build status and instance counts", async ({
    ontologyPage,
  }) => {
    await ontologyPage.goto(requireDesignerNamespaceId());
    const { abox, instances } = await ontologyPage.openDomainData();
    expect(
      { status: abox.status, pending: abox.pending },
      "the namespace's domain data is built, with no rebuild queued",
    ).toEqual({ status: "READY", pending: false });

    const panel = ontologyPage.domainDataPanel;
    await expect(
      panel.getByRole("heading", { name: "Domain ABox", level: 2 }),
    ).toBeVisible();
    await expect(panel.getByText("Ready", { exact: true })).toBeVisible();
    await expect(
      panel.getByText(
        `Built for v${abox.conformsToVersion}, active v${abox.activeVersion}`,
        { exact: true },
      ),
    ).toBeVisible();
    expect(await ontologyPage.shownCounts()).toEqual({
      Instances: String(abox.counts.instances),
      Relations: String(abox.counts.relations),
      Attributes: String(abox.counts.attributes),
      Evidence: String(abox.counts.evidence),
    });
    // Under the counts, the table lists the first page of instances the
    // engine returned, and says when more follow.
    expect(abox.counts.instances).toBeGreaterThan(0);
    expect(instances.count).toBeGreaterThan(0);
    await expect(ontologyPage.instancesHeading).toHaveText(
      `Instances (${instances.count}${instances.more ? "+" : ""})`,
    );
    await expect(ontologyPage.instanceRows).toHaveCount(instances.count);
  });
});
