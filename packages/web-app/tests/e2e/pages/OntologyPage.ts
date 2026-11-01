// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { expect, type Locator, type Page } from "@playwright/test";
import { objectField } from "@utils/object-field";
import { isGet, listOf } from "../fixtures/responses";

/** The namespace's versions, as every page of GET /namespaces/:id/ontology/versions lists them. */
export interface ListedVersions {
  versions: number[];
  active: number | null;
}

type DomainCount = "instances" | "relations" | "attributes" | "evidence";

/** The domain data's state, as GET /namespaces/:id/abox returns it. */
export interface DomainAboxState {
  status: string | null;
  pending: boolean;
  conformsToVersion: number | null;
  activeVersion: number | null;
  counts: Record<DomainCount, number | null>;
}

/** The first page of instances, as GET /namespaces/:id/abox/instances returns it. */
export interface InstancesPage {
  count: number;
  /** More pages follow: the table offers Load more. */
  more: boolean;
}

/**
 * Page Object for a namespace's Ontology page (/namespaces/:id/ontology,
 * titled Induction), for its Versions and Domain data tabs. Opening a tab
 * resolves what the tab fetched, so a spec checks the page against the
 * engine's own answer.
 */
export class OntologyPage {
  constructor(private readonly page: Page) {}

  async goto(namespaceId: string): Promise<void> {
    await this.page.goto(`/namespaces/${namespaceId}/ontology`);
    await expect(
      this.page.getByRole("heading", { name: "Induction", level: 1 }),
    ).toBeVisible({ timeout: 30_000 });
  }

  private async openTab(name: string): Promise<void> {
    await this.page.getByRole("tab", { name, exact: true }).click();
    await expect(
      this.page.getByRole("tabpanel", { name, exact: true }),
    ).toBeVisible();
  }

  /** Open the Versions tab; resolves the versions the engine listed and the active one. */
  async openVersions(): Promise<ListedVersions> {
    const versions = new Set<number>();
    // The tab reads every page in turn; the last one has no nextToken.
    const last = this.page.waitForResponse(async (response) => {
      if (!isGet(/\/ontology\/versions$/)(response)) return false;
      const page: unknown = await response.json();
      for (const v of listOf(objectField(page, "versions"))) {
        const version = numberOf(objectField(v, "version"));
        if (version !== null) versions.add(version);
      }
      return typeof objectField(page, "nextToken") !== "string";
    });
    await this.openTab("Versions");
    const active = numberOf(
      objectField(await (await last).json(), "activeVersion"),
    );
    return { versions: [...versions], active };
  }

  get versionsTable(): Locator {
    return this.page.getByRole("table", { name: "Versions" });
  }

  /** The table's title and count: "Versions (5)", and "Versions (0)" while it loads. */
  get versionsHeading(): Locator {
    return this.page.getByRole("heading", { name: /^Versions \(\d+\)$/ });
  }

  /**
   * The table's rows of versions: not its header row, nor the one row of its
   * loading or empty state.
   */
  get versionRows(): Locator {
    return this.versionsTable
      .getByRole("row")
      .filter({ has: this.page.getByRole("cell", { name: /^v\d+( |$)/ }) });
  }

  /** The row of a version: its first cell reads "v5", then any badges. */
  versionRow(version: number): Locator {
    return this.versionRows.filter({
      has: this.page.getByRole("cell", {
        name: new RegExp(`^v${version}( |$)`),
      }),
    });
  }

  /**
   * Open the Domain data tab; resolves the state the engine reported and the
   * first page of instances it listed (read once the state is known).
   */
  async openDomainData(): Promise<{
    abox: DomainAboxState;
    instances: InstancesPage;
  }> {
    const state = this.page.waitForResponse(isGet(/\/abox$/));
    const listed = this.page.waitForResponse(isGet(/\/abox\/instances$/));
    await this.openTab("Domain data");
    const body: unknown = await (await state).json();
    const status = objectField(body, "status");
    const counts = objectField(body, "counts");
    const list: unknown = await (await listed).json();
    return {
      abox: {
        status: typeof status === "string" ? status : null,
        pending: objectField(body, "pending") === true,
        conformsToVersion: numberOf(objectField(body, "conformsToVersion")),
        activeVersion: numberOf(objectField(body, "activeVersion")),
        counts: {
          instances: numberOf(objectField(counts, "instances")),
          relations: numberOf(objectField(counts, "relations")),
          attributes: numberOf(objectField(counts, "attributes")),
          evidence: numberOf(objectField(counts, "evidence")),
        },
      },
      instances: {
        count: listOf(objectField(list, "instances")).length,
        more: typeof objectField(list, "nextToken") === "string",
      },
    };
  }

  get domainDataPanel(): Locator {
    return this.page.getByRole("tabpanel", { name: "Domain data" });
  }

  /** The Domain ABox counts as shown, by their labels. */
  async shownCounts(): Promise<Record<string, string>> {
    const labels = await this.domainDataPanel.getByRole("term").allInnerTexts();
    const values = await this.domainDataPanel
      .getByRole("definition")
      .allInnerTexts();
    return Object.fromEntries(
      labels.map((label, i) => [label.trim(), (values[i] ?? "").trim()]),
    );
  }

  /** The Instances table's title and count, "Instances (50+)"; no count while it loads. */
  get instancesHeading(): Locator {
    return this.domainDataPanel.getByRole("heading", {
      name: /^Instances( \(|$)/,
    });
  }

  /**
   * The Instances table's rows of instances: each opens its instance from the
   * Label column, which the header row and the one row of the loading or
   * empty state do not.
   */
  get instanceRows(): Locator {
    return this.page
      .getByRole("table", { name: "Instances", exact: true })
      .getByRole("row")
      .filter({ has: this.page.getByRole("button", { name: /^Open / }) });
  }
}

function numberOf(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}
