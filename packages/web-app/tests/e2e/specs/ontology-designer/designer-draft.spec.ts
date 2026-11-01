// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { test, expect } from "../../fixtures/test";
import {
  readE2EEnv,
  readDesignerEnv,
  requireDesignerEnv,
  MISSING_ENV_REASON,
  MISSING_DESIGNER_ENV_REASON,
} from "../../fixtures/env";
import type { DesignerClass } from "../../pages/OntologyDesignerPage";

/**
 * The Ontology designer on a draft that is already open (inducing one takes
 * minutes): open it from the proposal page, inspect a class, change its label
 * and undo the change, the Checks tab and the Query console.
 *
 * The tests run in order in one worker, so the label change never runs beside
 * a test that reads the same class. The change is undone in the test, and the
 * fixture puts the label back if the test fails before its Undo. Nothing is
 * accepted, deployed or deleted.
 *
 * Not covered: the assistant in the Chat tab. A chat turn saves its history
 * and the conversation's memory into the draft, which is shared, and no API
 * removes them.
 */

/** A class to select and rename: editable, and the only one with its label. */
function pickClass(classes: DesignerClass[]): DesignerClass {
  const pick = classes.find(
    (c) =>
      c.editable && classes.filter((o) => o.label === c.label).length === 1,
  );
  if (!pick) {
    throw new Error("the draft has no editable class with a label of its own");
  }
  return pick;
}

test.describe("ontology-designer: draft", () => {
  test.skip(!readE2EEnv(), MISSING_ENV_REASON);
  test.skip(!readDesignerEnv(), MISSING_DESIGNER_ENV_REASON);
  test.describe.configure({ mode: "default" });
  test.setTimeout(90_000);

  test("opens the draft from the proposal page", async ({
    page,
    ontologyDesignerPage: designer,
  }) => {
    const { namespaceId, proposalId } = requireDesignerEnv();
    const title = await designer.gotoProposal(namespaceId, proposalId);
    const classes = await designer.continueInDesigner();

    // The same proposal, the revision on screen, and every class the graph has.
    await expect(designer.title).toHaveText(title);
    await expect(designer.revisionLine).toHaveText(
      /^Revision \d+, built on (v\d+|no version), \d+ class(es)?$/,
    );
    await expect(
      page.getByRole("tab", { name: "Inspector", selected: true }),
    ).toBeVisible();
    await expect(designer.inspector).toHaveText(
      "Select a class in the graph to inspect and edit it.",
    );
    expect(classes.length).toBeGreaterThan(0);
    for (const cls of classes) {
      await expect(designer.graphNode(cls.iri)).toBeAttached();
    }
  });

  test("a class selected in the graph shows in the Inspector with its mappings", async ({
    ontologyDesignerPage: designer,
  }) => {
    const { namespaceId, proposalId } = requireDesignerEnv();
    const cls = pickClass(await designer.goto(namespaceId, proposalId));
    const term = await designer.selectClass(cls);

    await expect(
      designer.graphNode(cls.iri).getByTitle(cls.label, { exact: true }),
    ).toHaveAttribute("data-selected", "true");
    await expect(designer.inspectorTitle(cls.label)).toBeVisible();
    await expect(
      designer.inspector.getByText(cls.iri, { exact: true }),
    ).toBeVisible();
    await expect(designer.labelInput).toHaveValue(cls.label);

    // The Mappings line: each table the class is mapped from, or that it has none.
    await expect(
      designer.inspector.getByText("Mappings", { exact: true }),
    ).toBeVisible();
    const notMapped = designer.inspector.getByText("Not mapped to a table", {
      exact: true,
    });
    if (term.mappings === 0) {
      await expect(notMapped).toBeVisible();
    } else {
      await expect(notMapped).toHaveCount(0);
      // A list item per mapping: a table two mappings read is listed twice.
      for (const table of new Set(term.tables)) {
        await expect(
          designer.inspector
            .getByRole("listitem")
            .filter({ hasText: `(table ${table})` }),
        ).toHaveCount(term.tables.filter((t) => t === table).length);
      }
    }
  });

  test("a label change shows in the History, and Undo puts the label back", async ({
    ontologyDesignerPage: designer,
    uniqueName,
  }) => {
    const { namespaceId, proposalId } = requireDesignerEnv();
    const cls = pickClass(await designer.goto(namespaceId, proposalId));
    const term = await designer.selectClass(cls);
    const label = uniqueName("designer");

    const changed = await designer.rename(term, label);
    await expect(designer.inspectorTitle(label)).toBeVisible();
    await expect(designer.graphNode(cls.iri)).toContainText(label);

    await designer.openTab("History");
    const change = designer.historyEntry(changed);
    await expect(change).toContainText(`Revision ${changed}`);
    await expect(change).toContainText(`Renamed ${term.label} to ${label}`);

    const undone = await designer.undo();
    await expect(designer.historyEntry(undone)).toContainText(
      `Undid revision ${changed}`,
    );
    await expect(change).toContainText("Undone");

    await designer.openTab("Inspector");
    await expect(designer.inspectorTitle(term.label)).toBeVisible();
    await expect(designer.labelInput).toHaveValue(term.label);
    await expect(designer.graphNode(cls.iri)).toContainText(term.label);
  });

  test("the Checks tab lists the classes structured queries cannot answer", async ({
    page,
    ontologyDesignerPage: designer,
  }) => {
    const { namespaceId, proposalId } = requireDesignerEnv();
    await designer.goto(namespaceId, proposalId);
    const revision = await designer.revision();
    const unmapped = await designer.openChecks();
    expect(
      unmapped.labels.length,
      "the draft has a class with no table mapping",
    ).toBeGreaterThan(0);

    await expect(
      page.getByRole("heading", {
        name: `Checks of revision ${revision}`,
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Structured queries", expanded: true }),
    ).toBeVisible();
    await expect(designer.structuredQueries).toContainText(
      unmapped.count === 1
        ? "1 class has no table mapping."
        : `${unmapped.count} classes have no table mapping.`,
    );
    // The finding names its classes, each a link to it.
    await expect(
      designer.structuredQueries.getByRole("button", {
        name: unmapped.labels[0],
        exact: true,
      }),
    ).toBeVisible();
  });

  test("the Query console runs SPARQL on the draft and shows the rows", async ({
    page,
    ontologyDesignerPage: designer,
  }) => {
    const { namespaceId, proposalId } = requireDesignerEnv();
    await designer.goto(namespaceId, proposalId);
    const revision = await designer.revision();
    const panel = await designer.runSampleQuery();

    // "14 rows, 95 ms, revision 6": how many rows, and the revision queried.
    const summary = panel.getByText(/^\d+ rows?, \d+ ms, revision \d+/);
    await expect(summary).toBeVisible({ timeout: 30_000 });
    const [, rows, queried] =
      /^(\d+) rows?, \d+ ms, revision (\d+)/.exec(await summary.innerText()) ??
      [];
    expect(Number(queried)).toBe(revision);
    expect(Number(rows)).toBeGreaterThan(0);

    const table = panel.getByRole("table");
    await expect(
      table.getByRole("columnheader", { name: "class", exact: true }),
    ).toBeVisible();
    await expect(
      table.getByRole("columnheader", { name: "label", exact: true }),
    ).toBeVisible();
    await expect(
      table.getByRole("row").filter({ has: page.getByRole("cell") }),
    ).toHaveCount(Number(rows));
  });
});
