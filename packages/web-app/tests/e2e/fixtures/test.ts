// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { test as base, expect } from "@playwright/test";
import { uniqueName } from "./resources";
import { forceDeleteNamespaces } from "./control-plane-api";
import { restoreLabels } from "./designer-api";
import { NamespacesPage } from "../pages/NamespacesPage";
import { SourcesPage } from "../pages/SourcesPage";
import { MetricsPage } from "../pages/MetricsPage";
import { GrantsPage } from "../pages/GrantsPage";
import { PlaygroundPage } from "../pages/PlaygroundPage";
import { SystemHealthPage } from "../pages/SystemHealthPage";
import { OntologyDesignerPage } from "../pages/OntologyDesignerPage";
import { OntologyPage } from "../pages/OntologyPage";

/**
 * Extended Playwright `test` for the Context Ontology Accelerator E2E suite: provides the `uniqueName`
 * helper and a lazily-instantiated Page Object per feature area. Import from
 * here instead of "@playwright/test" so specs pick up the shared fixtures.
 */

export interface E2EFixtures {
  uniqueName: (area: string) => string;
  namespacesPage: NamespacesPage;
  sourcesPage: SourcesPage;
  metricsPage: MetricsPage;
  grantsPage: GrantsPage;
  playgroundPage: PlaygroundPage;
  systemHealthPage: SystemHealthPage;
  ontologyDesignerPage: OntologyDesignerPage;
  ontologyPage: OntologyPage;
}

export const test = base.extend<E2EFixtures>({
  uniqueName: async ({}, use, testInfo) => {
    await use((area: string) => uniqueName(area, testInfo.workerIndex));
  },
  namespacesPage: async ({ page }, use) => {
    const pom = new NamespacesPage(page);
    await use(pom);
    // Guaranteed teardown via the control-plane API (run in the live page for a
    // fresh token): force-delete every namespace this test created, even on
    // failure. Reliable where UI-driven cleanup is not.
    await forceDeleteNamespaces(
      page,
      pom.createdNames(),
      pom.createdNamespaceIds(),
    );
  },
  sourcesPage: async ({ page }, use) => {
    await use(new SourcesPage(page));
  },
  metricsPage: async ({ page }, use) => {
    await use(new MetricsPage(page));
  },
  grantsPage: async ({ page }, use) => {
    await use(new GrantsPage(page));
  },
  playgroundPage: async ({ page }, use) => {
    await use(new PlaygroundPage(page));
  },
  systemHealthPage: async ({ page }, use) => {
    await use(new SystemHealthPage(page));
  },
  ontologyDesignerPage: async ({ page }, use) => {
    const pom = new OntologyDesignerPage(page);
    await use(pom);
    // A shared draft: put back, through the API, any label the test changed
    // and did not restore (it failed before its Undo).
    await restoreLabels(page, pom.renamedTerms());
  },
  ontologyPage: async ({ page }, use) => {
    await use(new OntologyPage(page));
  },
});

export { expect };
