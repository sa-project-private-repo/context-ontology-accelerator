// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  expect,
  type Locator,
  type Page,
  type Response,
} from "@playwright/test";
import { objectField } from "@utils/object-field";
import { splitSqlIdentPath } from "@pages/ontology/induce/grounding";
import type { RenamedTerm } from "../fixtures/designer-api";
import { isGet, listOf } from "../fixtures/responses";

/** A class of the draft graph, as GET .../studio/graph lists it. */
export interface DesignerClass {
  iri: string;
  label: string;
  editable: boolean;
}

/** What the specs read of a term, as GET .../studio/term returns it. */
export interface DesignerTerm {
  iri: string;
  label: string;
  /** The language of the label shown; a rename replaces the label in it. */
  lang: string | null;
  mappings: number;
  /** The tables its mappings read, named as the Inspector names them. */
  tables: string[];
}

/** The classes GET .../studio/checks finds no table mapping for. */
export interface UnmappedClasses {
  count: number;
  labels: string[];
}

export type DesignerTab = "Inspector" | "Checks" | "Query" | "History";

/**
 * Page Object for the Ontology designer
 * (/namespaces/:id/ontology/proposals/:proposalId/studio): the class graph,
 * the Inspector, and the Checks, Query and History tabs beside it. It reads
 * the graph, a term and the checks from the responses the page fetched, so a
 * spec knows the draft's classes without naming them.
 *
 * A rename is tracked before it is saved; the fixture's teardown puts back a
 * label the spec did not restore (see restoreLabels).
 */
export class OntologyDesignerPage {
  private readonly renamed: RenamedTerm[] = [];
  private namespaceId = "";
  private proposalId = "";

  constructor(private readonly page: Page) {}

  /** Open the designer on a proposal's draft; resolves the classes it draws. */
  async goto(
    namespaceId: string,
    proposalId: string,
  ): Promise<DesignerClass[]> {
    this.namespaceId = namespaceId;
    this.proposalId = proposalId;
    const graph = this.page.waitForResponse(isGet(/\/studio\/graph$/));
    await this.page.goto(designerPath(namespaceId, proposalId));
    return this.loaded(graph);
  }

  /** Open the proposal page of a draft open in the designer; resolves its title. */
  async gotoProposal(namespaceId: string, proposalId: string): Promise<string> {
    this.namespaceId = namespaceId;
    this.proposalId = proposalId;
    await this.page.goto(
      `/namespaces/${namespaceId}/ontology/proposals/${proposalId}`,
    );
    // Shown only while the designer owns the draft: Continue then opens it
    // as it is, where Open would start a draft.
    await expect(
      this.page.getByText("Editing in Ontology designer", { exact: true }),
    ).toBeVisible({ timeout: 30_000 });
    return this.title.innerText();
  }

  /** From the proposal page, continue in the designer; resolves the classes it draws. */
  async continueInDesigner(): Promise<DesignerClass[]> {
    const graph = this.page.waitForResponse(isGet(/\/studio\/graph$/));
    await this.page
      .getByRole("button", { name: "Continue in Ontology designer" })
      .click();
    await expect
      .poll(() => new URL(this.page.url()).pathname)
      .toBe(designerPath(this.namespaceId, this.proposalId));
    return this.loaded(graph);
  }

  private async loaded(graph: Promise<Response>): Promise<DesignerClass[]> {
    await expect(this.revisionLine).toBeVisible({ timeout: 30_000 });
    return classesOf(await (await graph).json());
  }

  /** The page title: the proposal's label. */
  get title(): Locator {
    return this.page.getByRole("main").getByRole("heading", { level: 1 });
  }

  /** The line under the title, e.g. "Revision 7, built on v5, 14 classes". */
  get revisionLine(): Locator {
    return this.page.getByText(/^Revision \d+, built on /);
  }

  /** The revision on screen. */
  async revision(): Promise<number> {
    const match = /^Revision (\d+),/.exec(await this.revisionLine.innerText());
    if (!match) throw new Error("the designer shows no revision");
    return Number(match[1]);
  }

  async expectRevision(revision: number): Promise<void> {
    await expect(this.revisionLine).toHaveText(
      new RegExp(`^Revision ${revision}, built on `),
      { timeout: 30_000 },
    );
  }

  /** A class of the graph, by IRI (React Flow's own test id). */
  graphNode(iri: string): Locator {
    return this.page.getByTestId(`rf__node-${iri}`);
  }

  /**
   * Select a class with a click on the graph; resolves the term the Inspector
   * read for it. The layout keeps labels readable, which can leave classes
   * outside the pane, so the graph is fitted to the pane first.
   */
  async selectClass(cls: DesignerClass): Promise<DesignerTerm> {
    const term = this.page.waitForResponse(
      (r) =>
        isGet(/\/studio\/term$/)(r) &&
        new URL(r.url()).searchParams.get("iri") === cls.iri,
    );
    await this.page.getByRole("button", { name: "Fit View" }).click();
    await this.graphNode(cls.iri).click();
    return termOf(await (await term).json());
  }

  get inspector(): Locator {
    return this.page.getByRole("tabpanel", { name: "Inspector" });
  }

  /** The Inspector's title for a class: its label, then the Class badge. */
  inspectorTitle(label: string): Locator {
    return this.inspector.getByRole("heading", {
      level: 2,
      name: `${label} Class`,
      exact: true,
    });
  }

  get labelInput(): Locator {
    return this.inspector.getByRole("textbox", { name: "Label", exact: true });
  }

  /** Rename the term shown in the Inspector; resolves the revision the rename made. */
  async rename(term: DesignerTerm, label: string): Promise<number> {
    const revision = (await this.revision()) + 1;
    // Before the save: the teardown restores the label even when the spec
    // fails after the save landed.
    this.renamed.push({
      namespaceId: this.namespaceId,
      proposalId: this.proposalId,
      iri: term.iri,
      label: term.label,
      lang: term.lang,
      revision,
    });
    await this.labelInput.fill(label);
    await this.inspector.getByRole("button", { name: "Save label" }).click();
    await this.expectRevision(revision);
    return revision;
  }

  /** Undo the newest change; resolves the revision the undo made. */
  async undo(): Promise<number> {
    const revision = (await this.revision()) + 1;
    await this.page.getByRole("button", { name: "Undo", exact: true }).click();
    await this.expectRevision(revision);
    return revision;
  }

  /** The labels this page object changed, for the fixture's teardown. */
  renamedTerms(): RenamedTerm[] {
    return [...this.renamed];
  }

  /** Show a tab beside the graph; resolves its panel. */
  async openTab(name: DesignerTab): Promise<Locator> {
    await this.page.getByRole("tab", { name, exact: true }).click();
    const panel = this.page.getByRole("tabpanel", { name, exact: true });
    await expect(panel).toBeVisible();
    return panel;
  }

  /** One revision in the History tab. */
  historyEntry(revision: number): Locator {
    return this.page.getByTestId(`history-${revision}`);
  }

  /** Open the Checks tab; resolves the classes the checks found no table mapping for. */
  async openChecks(): Promise<UnmappedClasses> {
    const checks = this.page.waitForResponse(isGet(/\/studio\/checks$/));
    await this.openTab("Checks");
    return unmappedOf(await (await checks).json());
  }

  /** The Checks tab's Structured queries section. */
  get structuredQueries(): Locator {
    return this.page.getByRole("group", { name: "Structured queries" });
  }

  /** Run the Query tab's sample query (the draft's classes) on the draft; resolves the tab's panel. */
  async runSampleQuery(): Promise<Locator> {
    const panel = await this.openTab("Query");
    await expect(
      panel.getByRole("button", { name: /What the query reads/ }),
    ).toContainText("Draft + documents (SPARQL)");
    await expect(
      panel.getByRole("textbox", { name: "Query text" }),
    ).toHaveValue(/SELECT \?class \?label/);
    await panel.getByRole("button", { name: "Run", exact: true }).click();
    return panel;
  }
}

function designerPath(namespaceId: string, proposalId: string): string {
  return `/namespaces/${namespaceId}/ontology/proposals/${proposalId}/studio`;
}

function classesOf(graph: unknown): DesignerClass[] {
  return listOf(objectField(graph, "nodes")).flatMap((node) => {
    const iri = objectField(node, "iri");
    const label = objectField(node, "label");
    return typeof iri === "string" && typeof label === "string"
      ? [{ iri, label, editable: objectField(node, "editable") === true }]
      : [];
  });
}

/**
 * A mapping's table as the Inspector names it (the designer's sqlName): the
 * last segment, unquoted, so a quoted "q1.results" stays whole.
 */
function tableName(table: string): string {
  return splitSqlIdentPath(table).pop() ?? table;
}

function termOf(term: unknown): DesignerTerm {
  const iri = objectField(term, "iri");
  const label = objectField(term, "label");
  if (typeof iri !== "string" || typeof label !== "string") {
    throw new Error("the term the Inspector read has no IRI or label");
  }
  const shown = listOf(objectField(term, "labels")).find(
    (l) => objectField(l, "value") === label,
  );
  const lang = objectField(shown, "lang");
  const mappings = listOf(objectField(term, "mappings"));
  return {
    iri,
    label,
    lang: typeof lang === "string" ? lang : null,
    mappings: mappings.length,
    tables: mappings.flatMap((m) => {
      const table = objectField(m, "table");
      return typeof table === "string" ? [tableName(table)] : [];
    }),
  };
}

function unmappedOf(checks: unknown): UnmappedClasses {
  const group = objectField(
    objectField(checks, "structured"),
    "unmappedClasses",
  );
  const count = objectField(group, "count");
  return {
    count: typeof count === "number" ? count : 0,
    labels: listOf(objectField(group, "terms")).flatMap((t) => {
      const label = objectField(t, "label");
      return typeof label === "string" ? [label] : [];
    }),
  };
}
