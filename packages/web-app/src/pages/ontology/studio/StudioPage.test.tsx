// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  act,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ApiError, type ApiClient } from "@components/ApiClientProvider";
import type {
  StudioAcceptPreview,
  StudioDraft,
  StudioGraph as StudioGraphData,
  StudioTerm,
} from "../../../services/studio";
import { LocaleProvider } from "../../../i18n/LocaleProvider";
import { VALIDATION_POLL_MS } from "./ChecksPanel";
import { sampleQuery } from "./QueryConsole";
import { StudioPage } from "./StudioPage";
import { conflictMessage } from "./studio-helpers";

// The graph is a stub with one button per thing the real one reports.
const ACTION_WORDS = {
  delete: "delete",
  addSubclass: "subclass",
  addRelationship: "relate",
  rename: "rename",
} as const;
vi.mock("./StudioGraph", () => ({
  StudioGraph: (p: {
    graph: StudioGraphData;
    selected: string[];
    onSelectionChange: (ids: string[]) => void;
    onFocus: (iri: string | null) => void;
    onAction: (a: { kind: string; iri: string; label: string }) => void;
    preview: StudioGraphData | null;
    onHidePreview: () => void;
  }) => (
    <div>
      {p.graph.abox?.current === false && <span>counts dimmed</span>}
      {p.preview && (
        <div>
          previewing {p.preview.nodes.map((n) => n.label).join(", ")}
          <button onClick={() => p.onHidePreview()}>hide the preview</button>
        </div>
      )}
      {p.graph.nodes.map((n) => (
        <span key={n.iri}>
          <button onClick={() => p.onSelectionChange([n.iri])}>
            select {n.label}
          </button>
          {/* A Shift-click: adds to the selection, as OntologyGraphFlow does. */}
          <button onClick={() => p.onSelectionChange([...p.selected, n.iri])}>
            add {n.label}
          </button>
          <button onClick={() => p.onFocus(n.iri)}>focus {n.label}</button>
          {Object.entries(ACTION_WORDS).map(([kind, word]) => (
            <button
              key={kind}
              onClick={() => p.onAction({ kind, iri: n.iri, label: n.label })}
            >
              {word} {n.label}
            </button>
          ))}
        </span>
      ))}
      <button
        onClick={() => p.onSelectionChange(p.graph.nodes.map((n) => n.iri))}
      >
        select all
      </button>
      <button onClick={() => p.onFocus(null)}>show all</button>
    </div>
  ),
}));

// The caller's role: true for one that can change the ontology (a data steward).
const access = vi.hoisted(() => ({ canManage: true }));
vi.mock("@api-hooks/use-can-manage-ontology", () => ({
  useCanManageOntology: () => access.canManage,
}));

const studio = vi.hoisted(() => ({
  openStudioDraft: vi.fn(),
  getStudioDraft: vi.fn(),
  getStudioGraph: vi.fn(),
  getStudioTerm: vi.fn(),
  searchStudioTerms: vi.fn(),
  applyStudioChanges: vi.fn(),
  undoStudioChange: vi.fn(),
  redoStudioChange: vi.fn(),
  listStudioHistory: vi.fn(),
  rebaseStudioDraft: vi.fn(),
  getStudioAcceptPreview: vi.fn(),
  listStudioEvents: vi.fn(),
  listStudioChangeSets: vi.fn(),
  previewStudioChangeSet: vi.fn(),
  applyStudioChangeSet: vi.fn(),
  sendStudioMessage: vi.fn(),
  getStudioChecks: vi.fn(),
  runStudioQuery: vi.fn(),
  listStudioQuestions: vi.fn(),
  checkStudioQuestion: vi.fn(),
}));
vi.mock("../../../services/studio", async (importOriginal) => {
  const real =
    await importOriginal<typeof import("../../../services/studio")>();
  return {
    ...real,
    ...Object.fromEntries(
      Object.entries(studio).map(([name, fn]) => [
        name,
        (...a: unknown[]) => fn(...a),
      ]),
    ),
  };
});
const getProposal = vi.fn();
const acceptProposal = vi.fn();
const validateProposal = vi.fn();
const getProposalValidationJob = vi.fn();
const listOntologyVersionsPage = vi.fn();
const getDomainAbox = vi.fn();
const listDomainInstances = vi.fn();
vi.mock("../../../services/ontology-engine", () => ({
  getProposal: (...a: unknown[]) => getProposal(...a),
  acceptProposal: (...a: unknown[]) => acceptProposal(...a),
  listOntologyVersionsPage: (...a: unknown[]) => listOntologyVersionsPage(...a),
  getDomainAbox: (...a: unknown[]) => getDomainAbox(...a),
  listDomainInstances: (...a: unknown[]) => listDomainInstances(...a),
  validateProposal: (...a: unknown[]) => validateProposal(...a),
  getProposalValidationJob: (...a: unknown[]) => getProposalValidationJob(...a),
}));

const api = {} as ApiClient;
const CUSTOMER = "http://e/o#Customer";
const ORDER = "http://e/o#Order";

function draft(overrides: Partial<StudioDraft> = {}): StudioDraft {
  return {
    proposalId: "p-1",
    status: "updated",
    editable: true,
    revision: 2,
    baseVersion: 3,
    headVersion: 3,
    rebaseRequired: false,
    ontologyId: "http://e/o#",
    counts: {
      classes: 2,
      objectProperties: 0,
      datatypeProperties: 0,
      triplesMaps: 0,
    },
    canUndo: true,
    canRedo: false,
    openedAt: "2026-10-19T00:00:00Z",
    openedBy: "alice",
    updatedAt: "2026-10-19T00:00:00Z",
    updatedBy: "alice",
    ...overrides,
  };
}

function node(iri: string): StudioGraphData["nodes"][number] {
  return {
    iri,
    label: iri.split("#")[1],
    grounded: false,
    editable: true,
    attributes: [],
    attributeCount: 0,
    aboxCount: null,
    afterDeploy: false,
  };
}

const GRAPH: StudioGraphData = {
  revision: 2,
  mode: "full",
  focus: null,
  totalClasses: 2,
  truncated: false,
  nodes: [CUSTOMER, ORDER].map((iri) => node(iri)),
  edges: [],
  abox: null,
};
const RESULT = {
  revision: 3,
  dryRun: false,
  warnings: [],
  created: [],
  diff: {},
  canUndo: true,
  canRedo: false,
};
const CUSTOMER_TERM: StudioTerm = {
  iri: CUSTOMER,
  kind: "class",
  label: "Customer",
  labels: [{ value: "Customer", lang: null }],
  altLabels: [],
  comment: "",
  editable: true,
  parents: [],
  children: [],
  matches: [],
  properties: [],
  mappings: [],
  abox: null,
};
const PREVIEW: StudioAcceptPreview = {
  revision: 2,
  baseVersion: 3,
  headVersion: 3,
  rebaseRequired: false,
  diff: {
    classes: { added: [], removed: [CUSTOMER], changed: [] },
    objectProperties: { added: [], removed: [], changed: [] },
    datatypeProperties: { added: [], removed: [], changed: [] },
  },
  renamed: [],
  breakingMetrics: [
    { metricId: "m1", name: "Customers", missingClassIri: CUSTOMER },
  ],
  breakingMetricsError: null,
};

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

/** The ops of the latest change set, compared strictly: no stray or undefined keys. */
function lastOps(): unknown {
  return studio.applyStudioChanges.mock.lastCall?.[3];
}

/** The top bar's Accept (the accept dialog, rendered later, has its own). */
function topBarAccept(): HTMLButtonElement {
  return screen.getAllByRole("button", {
    name: "Accept",
  })[0] as HTMLButtonElement;
}

function renderPage() {
  return render(
    <MemoryRouter>
      <StudioPage apiClient={api} namespace="ns" proposalId="p-1" />
    </MemoryRouter>,
  );
}

/** The page with the console set to Japanese; the caller clears the setting. */
function renderPageInJapanese() {
  window.localStorage.setItem("scl.locale", "ja");
  return render(
    <LocaleProvider>
      <MemoryRouter>
        <StudioPage apiClient={api} namespace="ns" proposalId="p-1" />
      </MemoryRouter>
    </LocaleProvider>,
  );
}

/** Opens the accept dialog, waits for its preview and confirms. */
async function acceptInDialog() {
  await screen.findByText("Shipping");
  fireEvent.click(topBarAccept());
  const dialog = screen.getByRole("dialog", { name: "Accept revision 2" });
  await within(dialog).findByText(/becomes the next version/);
  fireEvent.click(within(dialog).getByRole("button", { name: "Accept" }));
  return dialog;
}

describe("StudioPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    access.canManage = true;
    getProposal.mockResolvedValue({
      proposal_id: "p-1",
      status: "updated",
      metadata: { label: "Shipping" },
    });
    studio.getStudioDraft.mockResolvedValue(draft());
    studio.getStudioGraph.mockResolvedValue(GRAPH);
    studio.listStudioHistory.mockResolvedValue({
      revision: 2,
      entries: [],
      canUndo: true,
      canRedo: false,
    });
    studio.getStudioTerm.mockResolvedValue(null);
    studio.applyStudioChanges.mockResolvedValue(RESULT);
    studio.getStudioAcceptPreview.mockResolvedValue(PREVIEW);
    listOntologyVersionsPage.mockResolvedValue({
      versions: [],
      activeVersion: 3,
    });
    listDomainInstances.mockResolvedValue({ instances: [] });
  });

  afterEach(() => vi.useRealTimers());

  it("says the next version builds on the newest version, not the active one, after a rollback", async () => {
    listOntologyVersionsPage.mockResolvedValue({
      versions: [],
      activeVersion: 2,
    });
    renderPage();
    expect(
      await screen.findByText(
        "The next version builds on v3, not the active v2; its changes carry over unless you revert them.",
      ),
    ).toBeTruthy();
    expect(listOntologyVersionsPage).toHaveBeenCalledWith(api, "ns", {
      maxResults: 1,
    });
  });

  it("reads the active version again with the draft, so the notice follows a deploy made elsewhere", async () => {
    studio.getStudioTerm.mockResolvedValue(CUSTOMER_TERM);
    renderPage(); // v3 is both the head and the active version
    await screen.findByText("Shipping");
    await waitFor(() =>
      expect(listOntologyVersionsPage).toHaveBeenCalledTimes(1),
    );
    // Another tab accepts a proposal with Deploy immediately: v4 is published, its deploy still runs.
    studio.getStudioDraft.mockResolvedValue(
      draft({ revision: 3, headVersion: 4, rebaseRequired: true }),
    );
    fireEvent.click(await screen.findByText("rename Customer"));
    const dialog = screen.getByRole("dialog", { name: "Rename Customer" });
    const input = within(dialog).getByLabelText("Label") as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("Customer"));
    fireEvent.change(input, { target: { value: "Client" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText(
        "The next version builds on v4, not the active v3; its changes carry over unless you revert them.",
      ),
    ).toBeTruthy();
    // The deploy finishes: the next reload of the draft reads v4 as active, and the notice goes.
    listOntologyVersionsPage.mockResolvedValue({
      versions: [],
      activeVersion: 4,
    });
    studio.undoStudioChange.mockResolvedValue(RESULT);
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() =>
      expect(screen.queryByText(/The next version builds on/)).toBeNull(),
    );
  });

  it("says nothing about the next version on a read-only draft", async () => {
    listOntologyVersionsPage.mockResolvedValue({
      versions: [],
      activeVersion: 2,
    });
    studio.getStudioDraft.mockResolvedValue(
      draft({ editable: false, status: "accepted" }),
    );
    renderPage();
    await screen.findByText(
      "This proposal is accepted; its draft is read-only.",
    );
    await waitFor(() => expect(listOntologyVersionsPage).toHaveBeenCalled());
    await act(async () => {}); // the active version has arrived
    expect(screen.queryByText(/The next version builds on/)).toBeNull();
  });

  it("says nothing about the active version when it is the newest one or cannot be read", async () => {
    const { unmount } = renderPage();
    await screen.findByText("Shipping");
    await waitFor(() => expect(listOntologyVersionsPage).toHaveBeenCalled());
    expect(screen.queryByText(/The next version builds on/)).toBeNull();
    unmount();
    listOntologyVersionsPage.mockRejectedValue(
      new Error("Service Unavailable"),
    );
    renderPage();
    await screen.findByText("Shipping");
    await waitFor(() =>
      expect(listOntologyVersionsPage).toHaveBeenCalledTimes(2),
    );
    expect(screen.queryByText(/The next version builds on/)).toBeNull();
  });

  it("shows the draft's name, revision and base", async () => {
    renderPage();
    expect(await screen.findByText("Shipping")).toBeTruthy();
    expect(screen.getByText("Revision 2, built on v3, 2 classes")).toBeTruthy();
  });

  describe("context-menu actions", () => {
    it("says that keeping the properties of a deleted class drops their table mappings", async () => {
      renderPage();
      fireEvent.click(await screen.findByText("delete Customer"));
      const dialog = screen.getByRole("dialog", { name: "Delete Customer" });
      expect(
        within(dialog).getByText(
          "Remove the class from their domain or range. Their table mappings are removed with the class.",
        ),
      ).toBeTruthy();
    });

    it("deletes a class from the context menu against the revision on screen", async () => {
      renderPage();
      fireEvent.click(await screen.findByText("delete Customer"));
      // Every Cloudscape Modal stays in the DOM: find the open one by its header.
      const dialog = screen.getByRole("dialog", { name: "Delete Customer" });
      fireEvent.click(within(dialog).getByText("Delete them"));
      fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
      await waitFor(() =>
        expect(studio.applyStudioChanges).toHaveBeenCalledWith(
          api,
          "ns",
          "p-1",
          [{ op: "delete_class", iri: CUSTOMER, cascade: "delete" }],
          2,
        ),
      );
      expect(lastOps()).toStrictEqual([
        { op: "delete_class", iri: CUSTOMER, cascade: "delete" },
      ]);
      await waitFor(() =>
        expect(studio.getStudioDraft).toHaveBeenCalledTimes(2),
      );
    });

    it("keeps the properties of a deleted class unless told otherwise", async () => {
      renderPage();
      fireEvent.click(await screen.findByText("delete Order"));
      const dialog = screen.getByRole("dialog", { name: "Delete Order" });
      fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
      await waitFor(() => expect(studio.applyStudioChanges).toHaveBeenCalled());
      expect(lastOps()).toStrictEqual([
        { op: "delete_class", iri: ORDER, cascade: "detach" },
      ]);
    });

    it("adds a subclass named in the dialog and selects it", async () => {
      const RETURN = "http://e/o#Return";
      studio.applyStudioChanges.mockResolvedValue({
        ...RESULT,
        created: [RETURN],
      });
      renderPage();
      fireEvent.click(await screen.findByText("subclass Order"));
      const dialog = screen.getByRole("dialog", {
        name: "Add a subclass of Order",
      });
      fireEvent.change(within(dialog).getByLabelText("Label"), {
        target: { value: " Return " },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
      await waitFor(() =>
        expect(studio.applyStudioChanges).toHaveBeenCalledWith(
          api,
          "ns",
          "p-1",
          [{ op: "add_class", label: "Return", parents: [ORDER] }],
          2,
        ),
      );
      expect(lastOps()).toStrictEqual([
        { op: "add_class", label: "Return", parents: [ORDER] },
      ]);
      await waitFor(() =>
        expect(studio.getStudioTerm).toHaveBeenCalledWith(
          api,
          "ns",
          "p-1",
          RETURN,
        ),
      );
    });

    describe("rename", () => {
      /** Renames Customer to Client through the context menu. */
      async function renameToClient() {
        renderPage();
        fireEvent.click(await screen.findByText("rename Customer"));
        const dialog = screen.getByRole("dialog", { name: "Rename Customer" });
        const input = within(dialog).getByLabelText(
          "Label",
        ) as HTMLInputElement;
        expect(input.value).toBe("Customer");
        fireEvent.change(input, { target: { value: "Client" } });
        fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
        return dialog;
      }

      it("replaces the shown label in its language", async () => {
        studio.getStudioTerm.mockResolvedValue({
          ...CUSTOMER_TERM,
          labels: [
            { value: "Kunde", lang: "de" },
            { value: "Customer", lang: "en" },
          ],
        });
        await renameToClient();
        await waitFor(() =>
          expect(studio.applyStudioChanges).toHaveBeenCalled(),
        );
        expect(studio.getStudioTerm).toHaveBeenCalledWith(
          api,
          "ns",
          "p-1",
          CUSTOMER,
        );
        expect(lastOps()).toStrictEqual([
          { op: "set_label", iri: CUSTOMER, label: "Client", lang: "en" },
        ]);
        expect(studio.applyStudioChanges.mock.lastCall?.[4]).toBe(2);
      });

      it("sends no lang for an untagged label", async () => {
        studio.getStudioTerm.mockResolvedValue(CUSTOMER_TERM);
        await renameToClient();
        await waitFor(() =>
          expect(studio.applyStudioChanges).toHaveBeenCalled(),
        );
        expect(lastOps()).toStrictEqual([
          { op: "set_label", iri: CUSTOMER, label: "Client" },
        ]);
      });

      it("sends nothing when the label's language cannot be read", async () => {
        studio.getStudioTerm.mockRejectedValue(
          new ApiError(404, "TermNotFound", "not in the draft", {
            detail: { code: "TermNotFound", message: "not in the draft" },
          }),
        );
        const dialog = await renameToClient();
        expect(
          await within(dialog).findByText("not in the draft"),
        ).toBeTruthy();
        expect(studio.applyStudioChanges).not.toHaveBeenCalled();
        expect(
          screen.getByRole("dialog", { name: "Rename Customer" }),
        ).toBeTruthy();
      });
    });

    it("adds a relationship to a class picked in the dialog", async () => {
      studio.searchStudioTerms.mockResolvedValue({
        revision: 2,
        terms: [{ iri: ORDER, label: "Order", kind: "class" }],
      });
      renderPage();
      fireEvent.click(await screen.findByText("relate Customer"));
      const dialog = screen.getByRole("dialog", {
        name: "Add a relationship from Customer",
      });
      const save = within(dialog).getByRole("button", {
        name: "Save",
      }) as HTMLButtonElement;
      fireEvent.change(within(dialog).getByLabelText("Relationship label"), {
        target: { value: "places" },
      });
      expect(save).toHaveAttribute("aria-disabled", "true");
      const target = within(dialog).getByRole("combobox", {
        name: "Target class",
      });
      fireEvent.focus(target);
      fireEvent.change(target, { target: { value: "zzz" } });
      fireEvent.mouseUp(await screen.findByRole("option", { name: /Order/ }));
      expect(studio.searchStudioTerms).toHaveBeenCalledWith(
        api,
        "ns",
        "p-1",
        "zzz",
        { kind: "class", limit: 15 },
      );
      fireEvent.click(save);
      await waitFor(() => expect(studio.applyStudioChanges).toHaveBeenCalled());
      expect(lastOps()).toStrictEqual([
        {
          op: "add_object_property",
          label: "places",
          domain: CUSTOMER,
          range: ORDER,
        },
      ]);
    });

    it("drops the picked target class when its box is typed over or cleared", async () => {
      studio.searchStudioTerms.mockResolvedValue({
        revision: 2,
        terms: [{ iri: ORDER, label: "Order", kind: "class" }],
      });
      renderPage();
      fireEvent.click(await screen.findByText("relate Customer"));
      const dialog = screen.getByRole("dialog", {
        name: "Add a relationship from Customer",
      });
      fireEvent.change(within(dialog).getByLabelText("Relationship label"), {
        target: { value: "places" },
      });
      const target = within(dialog).getByRole("combobox", {
        name: "Target class",
      }) as HTMLInputElement;
      fireEvent.focus(target);
      fireEvent.change(target, { target: { value: "zzz" } });
      fireEvent.mouseUp(await screen.findByRole("option", { name: /Order/ }));
      const save = within(dialog).getByRole("button", { name: "Save" });
      expect(save).not.toHaveAttribute("aria-disabled");
      expect(save).not.toBeDisabled();

      // Typed over without a pick: the box no longer names Order.
      fireEvent.change(target, { target: { value: "Invoice" } });
      expect(save).toHaveAttribute("aria-disabled", "true");
      fireEvent.click(save);
      fireEvent.change(target, { target: { value: "" } });
      expect(save).toHaveAttribute("aria-disabled", "true");
      fireEvent.click(save);
      expect(studio.applyStudioChanges).not.toHaveBeenCalled();
    });

    it("explains in the dialog why a change from a stale menu does not apply", async () => {
      const why = `${CUSTOMER} is not a class of the draft`;
      studio.getStudioTerm.mockResolvedValue(CUSTOMER_TERM);
      studio.applyStudioChanges.mockRejectedValue(
        new ApiError(409, "InvalidChange", why, {
          detail: { code: "InvalidChange", message: why, opIndex: 0 },
        }),
      );
      renderPage();
      fireEvent.click(await screen.findByText("rename Customer"));
      const dialog = screen.getByRole("dialog", { name: "Rename Customer" });
      fireEvent.change(within(dialog).getByLabelText("Label"), {
        target: { value: "Client" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
      expect(await within(dialog).findByText(why)).toBeTruthy();
      // Still open, so the label can be fixed or the dialog cancelled.
      expect(
        screen.getByRole("dialog", { name: "Rename Customer" }),
      ).toBeTruthy();
    });

    it("reloads and explains a conflict with another change", async () => {
      studio.applyStudioChanges.mockRejectedValue(
        new ApiError(409, "DraftConflict", "moved", {
          detail: { code: "DraftConflict", revision: 5 },
        }),
      );
      renderPage();
      fireEvent.click(await screen.findByText("delete Order"));
      fireEvent.click(
        within(screen.getByRole("dialog", { name: "Delete Order" })).getByRole(
          "button",
          { name: "Delete" },
        ),
      );
      expect(await screen.findByText(conflictMessage())).toBeTruthy();
      expect(studio.getStudioDraft).toHaveBeenCalledTimes(2);
      expect(screen.queryByRole("dialog", { name: "Delete Order" })).toBeNull();
    });
  });

  it("undoes the newest change", async () => {
    studio.undoStudioChange.mockResolvedValue(RESULT);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Undo" }));
    await waitFor(() =>
      expect(studio.undoStudioChange).toHaveBeenCalledWith(api, "ns", "p-1", 2),
    );
    expect(
      (screen.getByRole("button", { name: "Redo" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("writes a change's warnings from their codes, and as the engine wrote them without", async () => {
    studio.applyStudioChanges.mockResolvedValue({
      ...RESULT,
      warnings: ["As the engine wrote it", "A warning without its code"],
      warningMessages: [
        {
          code: "RANGE_UNCONFIRMED",
          params: { property: "placed_by", range: "Party" },
        },
      ],
    });
    renderPage();
    fireEvent.click(await screen.findByText("delete Order"));
    const dialog = screen.getByRole("dialog", { name: "Delete Order" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(
      await screen.findByText(
        "placed_by is mapped through an IRI template, so the mapping cannot confirm its range Party A warning without its code",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/As the engine wrote it/)).toBeNull();
  });

  describe("several selected classes", () => {
    it("merges two selected classes", async () => {
      renderPage();
      fireEvent.click(await screen.findByText("select all"));
      fireEvent.click(await screen.findByRole("button", { name: "Merge…" }));
      fireEvent.click(
        within(screen.getByRole("dialog", { name: "Merge classes" })).getByRole(
          "button",
          { name: "Merge" },
        ),
      );
      await waitFor(() =>
        expect(studio.applyStudioChanges).toHaveBeenCalledWith(
          api,
          "ns",
          "p-1",
          [{ op: "merge_classes", source: CUSTOMER, target: ORDER }],
          2,
        ),
      );
      expect(lastOps()).toStrictEqual([
        { op: "merge_classes", source: CUSTOMER, target: ORDER },
      ]);
    });

    it("deletes every selected class", async () => {
      renderPage();
      fireEvent.click(await screen.findByText("select all"));
      fireEvent.click(
        await screen.findByRole("button", { name: "Delete selected" }),
      );
      fireEvent.click(
        within(
          screen.getByRole("dialog", { name: "Delete 2 classes" }),
        ).getByRole("button", { name: "Delete" }),
      );
      await waitFor(() => expect(studio.applyStudioChanges).toHaveBeenCalled());
      expect(lastOps()).toStrictEqual([
        { op: "delete_class", iri: CUSTOMER, cascade: "detach" },
        { op: "delete_class", iri: ORDER, cascade: "detach" },
      ]);
    });

    it("deletes only the selected classes still on screen", async () => {
      const RETURN = "http://e/o#Return";
      const all: StudioGraphData = {
        ...GRAPH,
        totalClasses: 3,
        nodes: [CUSTOMER, ORDER, RETURN].map((iri) => node(iri)),
      };
      const focused: StudioGraphData = {
        ...all,
        mode: "focus",
        focus: ORDER,
        nodes: [CUSTOMER, ORDER].map((iri) => node(iri)),
      };
      studio.getStudioGraph.mockImplementation(
        (_a: unknown, _n: unknown, _p: unknown, focus?: string) =>
          Promise.resolve(focus === ORDER ? focused : all),
      );
      renderPage();
      fireEvent.click(await screen.findByText("select all"));
      expect(await screen.findByText("3 classes selected")).toBeTruthy();
      // Return leaves the screen with the new focus, and so the selection.
      fireEvent.click(screen.getByText("focus Order"));
      expect(await screen.findByText("2 classes selected")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));
      fireEvent.click(
        within(
          screen.getByRole("dialog", { name: "Delete 2 classes" }),
        ).getByRole("button", { name: "Delete" }),
      );
      await waitFor(() => expect(studio.applyStudioChanges).toHaveBeenCalled());
      expect(lastOps()).toStrictEqual([
        { op: "delete_class", iri: CUSTOMER, cascade: "detach" },
        { op: "delete_class", iri: ORDER, cascade: "detach" },
      ]);
    });

    it("keeps the inspected class off screen, but adds to a selection only classes on screen", async () => {
      const orderOnly: StudioGraphData = {
        ...GRAPH,
        mode: "focus",
        focus: ORDER,
        nodes: [node(ORDER)],
      };
      studio.getStudioGraph.mockImplementation(
        (_a: unknown, _n: unknown, _p: unknown, focus?: string) =>
          Promise.resolve(focus === ORDER ? orderOnly : GRAPH),
      );
      studio.getStudioTerm.mockResolvedValue(CUSTOMER_TERM);
      renderPage();
      fireEvent.click(await screen.findByText("select Customer"));
      await screen.findByRole("textbox", { name: "Label" });
      fireEvent.click(screen.getByText("focus Order"));
      await waitFor(() =>
        expect(screen.queryByText("select Customer")).toBeNull(),
      );
      // One selected term is the Inspector's, whatever the graph shows.
      expect(
        (screen.getByRole("textbox", { name: "Label" }) as HTMLInputElement)
          .value,
      ).toBe("Customer");
      // A Shift-click on Order: Customer is off screen, so Order alone is selected.
      fireEvent.click(screen.getByText("add Order"));
      await waitFor(() =>
        expect(studio.getStudioTerm).toHaveBeenLastCalledWith(
          api,
          "ns",
          "p-1",
          ORDER,
        ),
      );
      expect(screen.queryByText(/classes selected/)).toBeNull();
    });
  });

  it("keeps the focus across reloads and leaves it on null", async () => {
    studio.undoStudioChange.mockResolvedValue(RESULT);
    renderPage();
    fireEvent.click(await screen.findByText("focus Customer"));
    await waitFor(() => expect(studio.getStudioGraph).toHaveBeenCalledTimes(2));
    expect(studio.getStudioGraph.mock.calls[0]).toStrictEqual([
      api,
      "ns",
      "p-1",
    ]);
    expect(studio.getStudioGraph.mock.lastCall).toStrictEqual([
      api,
      "ns",
      "p-1",
      CUSTOMER,
    ]);

    // A reload after a change keeps the focus ...
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(studio.getStudioGraph).toHaveBeenCalledTimes(3));
    expect(studio.getStudioGraph.mock.lastCall).toStrictEqual([
      api,
      "ns",
      "p-1",
      CUSTOMER,
    ]);

    // ... and null asks for the graph without one.
    fireEvent.click(screen.getByText("show all"));
    await waitFor(() => expect(studio.getStudioGraph).toHaveBeenCalledTimes(4));
    expect(studio.getStudioGraph.mock.lastCall).toStrictEqual([
      api,
      "ns",
      "p-1",
    ]);
  });

  it("drops the focus on a class that is gone", async () => {
    studio.getStudioGraph.mockImplementation(
      (_a: unknown, _n: unknown, _p: unknown, focus?: string) =>
        focus === CUSTOMER
          ? Promise.reject(
              new ApiError(404, "TermNotFound", "gone", {
                detail: { code: "TermNotFound", message: "gone" },
              }),
            )
          : Promise.resolve(GRAPH),
    );
    renderPage();
    fireEvent.click(await screen.findByText("focus Customer"));
    await waitFor(() => expect(studio.getStudioGraph).toHaveBeenCalledTimes(3));
    expect(studio.getStudioGraph.mock.lastCall).toStrictEqual([
      api,
      "ns",
      "p-1",
    ]);
    expect(screen.queryByText("Could not load the designer draft")).toBeNull();
    expect(screen.getByText("show all")).toBeTruthy();
  });

  it("shows the draft read-only when a write finds the proposal left review", async () => {
    studio.getStudioDraft
      .mockResolvedValueOnce(draft())
      .mockResolvedValue(draft({ editable: false, status: "cancelled" }));
    studio.applyStudioChanges.mockRejectedValue(
      new ApiError(409, "ProposalNotEditable", "The proposal is cancelled", {
        detail: {
          code: "ProposalNotEditable",
          message: "The proposal is cancelled",
          status: "cancelled",
        },
      }),
    );
    renderPage();
    fireEvent.click(await screen.findByText("delete Order"));
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "Delete Order" })).getByRole(
        "button",
        { name: "Delete" },
      ),
    );
    expect(await screen.findByText("The proposal is cancelled")).toBeTruthy();
    expect(
      await screen.findByText(
        "This proposal is cancelled; its draft is read-only.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "Delete Order" })).toBeNull();
  });

  describe("the Inspector", () => {
    it("keeps the forms and what was typed while the edited term reloads", async () => {
      const ABOX = {
        status: "ready",
        current: false,
        conformsToVersion: 2,
        activeVersion: 3,
        reason: "built for v2",
      };
      studio.getStudioGraph.mockResolvedValue({ ...GRAPH, abox: ABOX });
      studio.getStudioDraft
        .mockResolvedValueOnce(draft())
        .mockResolvedValue(draft({ revision: 3 }));
      const written = deferred<typeof RESULT>();
      studio.applyStudioChanges.mockReturnValue(written.promise);
      const refetched = deferred<StudioTerm>();
      studio.getStudioTerm
        .mockResolvedValueOnce({
          ...CUSTOMER_TERM,
          abox: { aboxCount: 0, afterDeploy: false },
        })
        .mockReturnValueOnce(refetched.promise);
      renderPage();
      fireEvent.click(await screen.findByText("select Customer"));
      const input = (await screen.findByRole("textbox", {
        name: "Label",
      })) as HTMLInputElement;
      // The graph's ABox state reaches the Inspector.
      expect(screen.getByText(/Out of date: built for v2/)).toBeTruthy();
      fireEvent.change(input, { target: { value: "Client" } });
      const save = screen.getByRole("button", {
        name: "Save label",
      }) as HTMLButtonElement;
      fireEvent.click(save);
      // Busy at once: a second click sends nothing.
      expect(save.disabled).toBe(true);
      fireEvent.click(save);
      expect(studio.applyStudioChanges).toHaveBeenCalledTimes(1);
      expect(lastOps()).toStrictEqual([
        { op: "set_label", iri: CUSTOMER, label: "Client" },
      ]);

      await act(async () => written.resolve(RESULT));
      await waitFor(() =>
        expect(studio.getStudioTerm).toHaveBeenCalledTimes(2),
      );
      // The term of revision 3 is still on its way: the same form, same text.
      expect(input.isConnected).toBe(true);
      expect(input.value).toBe("Client");
      await act(async () =>
        refetched.resolve({
          ...CUSTOMER_TERM,
          label: "Client",
          labels: [{ value: "Client", lang: null }],
        }),
      );
      expect(screen.getByRole("textbox", { name: "Label" })).toBe(input);
    });

    it("refuses a description too large for the API, and keeps it in the form", async () => {
      studio.getStudioTerm.mockResolvedValue(CUSTOMER_TERM);
      renderPage();
      fireEvent.click(await screen.findByText("select Customer"));
      const description = (await screen.findByRole("textbox", {
        name: "Description",
      })) as HTMLTextAreaElement;
      // 3,000 Hangul characters: under the engine's 4,000, over 8 KB as UTF-8.
      const text = "설명".repeat(1500);
      fireEvent.change(description, { target: { value: text } });
      fireEvent.click(screen.getByRole("button", { name: "Save description" }));
      expect(
        await screen.findByText(/^This change is too large to send/),
      ).toBeTruthy();
      expect(studio.applyStudioChanges).not.toHaveBeenCalled();
      expect(description.value).toBe(text);
    });

    it("lets go of a selected class the draft no longer has", async () => {
      studio.undoStudioChange.mockResolvedValue(RESULT);
      studio.getStudioDraft
        .mockResolvedValueOnce(draft())
        .mockResolvedValue(draft({ revision: 3 }));
      studio.getStudioTerm
        .mockResolvedValueOnce(CUSTOMER_TERM)
        .mockRejectedValue(
          new ApiError(404, "TermNotFound", "not in the draft", {
            detail: { code: "TermNotFound", message: "not in the draft" },
          }),
        );
      renderPage();
      fireEvent.click(await screen.findByText("select Customer"));
      await screen.findByRole("textbox", { name: "Label" });
      fireEvent.click(screen.getByRole("button", { name: "Undo" }));
      expect(
        await screen.findByText(
          "Select a class in the graph to inspect and edit it.",
        ),
      ).toBeTruthy();
      expect(screen.queryByText("not in the draft")).toBeNull();
    });

    it("selects the new IRI after the IRI changed", async () => {
      const CLIENT = "http://e/o#Client";
      studio.getStudioTerm.mockResolvedValue(CUSTOMER_TERM);
      renderPage();
      fireEvent.click(await screen.findByText("select Customer"));
      fireEvent.change(await screen.findByLabelText("New IRI"), {
        target: { value: CLIENT },
      });
      const action = screen
        .getAllByRole("button", { name: "Change IRI" })
        .find((b) => !b.hasAttribute("aria-expanded"));
      fireEvent.click(action as HTMLElement);
      await waitFor(() =>
        expect(studio.getStudioTerm).toHaveBeenLastCalledWith(
          api,
          "ns",
          "p-1",
          CLIENT,
        ),
      );
      expect(lastOps()).toStrictEqual([
        { op: "change_iri", iri: CUSTOMER, newIri: CLIENT },
      ]);
    });

    it("shows a saved change in the Inspector's lists with the revision it made", async () => {
      const PARTY = "http://e/o#Party";
      studio.getStudioDraft
        .mockResolvedValueOnce(draft())
        .mockResolvedValue(draft({ revision: 3 }));
      const reread = deferred<StudioTerm>();
      studio.getStudioTerm
        .mockResolvedValueOnce({
          ...CUSTOMER_TERM,
          parents: [{ iri: PARTY, label: "Party", declared: true }],
        })
        .mockReturnValueOnce(reread.promise);
      renderPage();
      fireEvent.click(await screen.findByText("select Customer"));
      fireEvent.click(
        await screen.findByRole("button", { name: "Remove parent Party" }),
      );
      await waitFor(() =>
        expect(studio.getStudioTerm).toHaveBeenCalledTimes(2),
      );
      // The term of revision 3 is on its way: the page still shows revision 2, whole.
      expect(
        screen.getByText("Revision 2, built on v3, 2 classes"),
      ).toBeTruthy();
      expect(
        screen.getByRole("button", { name: "Remove parent Party" }),
      ).toBeTruthy();
      await act(async () => reread.resolve(CUSTOMER_TERM));
      expect(
        await screen.findByText("Revision 3, built on v3, 2 classes"),
      ).toBeTruthy();
      expect(
        screen.queryByRole("button", { name: "Remove parent Party" }),
      ).toBeNull();
      expect(studio.getStudioTerm).toHaveBeenCalledTimes(2);
    });

    it("shows the label an undo restores with its revision, and keeps what is typed after it", async () => {
      studio.undoStudioChange.mockResolvedValue(RESULT);
      studio.getStudioDraft
        .mockResolvedValueOnce(draft())
        .mockResolvedValue(draft({ revision: 3 }));
      const reread = deferred<StudioTerm>();
      studio.getStudioTerm
        .mockResolvedValueOnce({
          ...CUSTOMER_TERM,
          label: "Client",
          labels: [{ value: "Client", lang: null }],
        })
        .mockReturnValueOnce(reread.promise);
      renderPage();
      fireEvent.click(await screen.findByText("select Customer"));
      const input = (await screen.findByRole("textbox", {
        name: "Label",
      })) as HTMLInputElement;
      expect(input.value).toBe("Client");
      fireEvent.click(screen.getByRole("button", { name: "Undo" }));
      await waitFor(() =>
        expect(studio.getStudioTerm).toHaveBeenCalledTimes(2),
      );
      // Until the undone term is read the page shows revision 2, and nothing can be typed.
      expect(
        screen.getByText("Revision 2, built on v3, 2 classes"),
      ).toBeTruthy();
      expect(input.disabled).toBe(true);
      await act(async () => reread.resolve(CUSTOMER_TERM));
      expect(
        await screen.findByText("Revision 3, built on v3, 2 classes"),
      ).toBeTruthy();
      await waitFor(() => expect(input.disabled).toBe(false));
      expect(input.value).toBe("Customer");
      fireEvent.change(input, { target: { value: "Customer account" } });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
      expect(input.value).toBe("Customer account");
      expect(studio.getStudioTerm).toHaveBeenCalledTimes(2);
    });

    it("reads the term again only for a new revision, not for another focus", async () => {
      studio.getStudioTerm.mockResolvedValue(CUSTOMER_TERM);
      renderPage();
      fireEvent.click(await screen.findByText("select Customer"));
      await screen.findByRole("textbox", { name: "Label" });
      fireEvent.click(screen.getByText("focus Order"));
      await waitFor(() =>
        expect(studio.getStudioGraph).toHaveBeenCalledTimes(2),
      );
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
      expect(studio.getStudioTerm).toHaveBeenCalledTimes(1);
    });
  });

  describe("dimmed instance counts", () => {
    const STALE = {
      status: "READY",
      current: false,
      conformsToVersion: 2,
      activeVersion: 3,
      reason: "The domain data was built for v2; v3 is active",
    };
    const CURRENT = {
      ...STALE,
      current: true,
      conformsToVersion: 3,
      reason: null,
    };
    /** Another term, so a read the user's selection started is told from the check's. */
    const ORDER_TERM: StudioTerm = {
      ...CUSTOMER_TERM,
      iri: ORDER,
      label: "Order",
      labels: [{ value: "Order", lang: null }],
    };

    /** The namespace's domain data (GET .../abox), built for `version` while v3 is active. */
    function domainData(version: number, extra: Record<string, unknown> = {}) {
      return {
        status: "READY",
        current: version === 3,
        conformsToVersion: version,
        activeVersion: 3,
        counts: { instances: 16, relations: 0, attributes: 0, evidence: 0 },
        classCounts: [],
        pending: false,
        ...extra,
      };
    }

    async function wait(ms: number) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms);
      });
    }

    /** Customer with an instance for each of `labels`, the examples the Inspector lists under it. */
    function customers(...labels: string[]) {
      studio.getStudioTerm.mockResolvedValue({
        ...CUSTOMER_TERM,
        abox: { aboxCount: labels.length, afterDeploy: false },
      });
      listDomainInstances.mockResolvedValue({
        instances: labels.map((label) => ({
          iri: `http://e/data/${label}`,
          classIri: CUSTOMER,
          classLabel: "Customer",
          label,
          altLabels: [],
          evidenceCount: 1,
        })),
      });
    }

    it("reads the graph, the Inspector's term and its examples again once the domain data is built for the active version", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      // Accepted with Deploy immediately: v3 is active, its domain data is not built yet.
      studio.getStudioDraft.mockResolvedValue(
        draft({ editable: false, status: "accepted" }),
      );
      studio.getStudioGraph.mockResolvedValue({ ...GRAPH, abox: STALE });
      customers("Alice", "Bob", "Carol");
      getDomainAbox.mockResolvedValue(domainData(2));
      renderPage();
      fireEvent.click(await screen.findByText("select Customer"));
      expect(
        await screen.findByText("3 instances in the active version"),
      ).toBeTruthy();
      expect(await screen.findByText("Carol")).toBeTruthy();
      expect(
        screen.getByText(
          "Out of date: The domain data was built for v2; v3 is active",
        ),
      ).toBeTruthy();
      expect(screen.getByText("counts dimmed")).toBeTruthy();

      // Not built yet: the page reads nothing else.
      await wait(15_100);
      expect(getDomainAbox).toHaveBeenCalledTimes(1);
      expect(getDomainAbox).toHaveBeenCalledWith(api, "ns");
      expect(studio.getStudioGraph).toHaveBeenCalledTimes(1);
      expect(studio.getStudioTerm).toHaveBeenCalledTimes(1);
      expect(listDomainInstances).toHaveBeenCalledTimes(1);

      // The build for v3 lands: the graph, the term and its examples show it, undimmed.
      getDomainAbox.mockResolvedValue(domainData(3));
      studio.getStudioGraph.mockResolvedValue({ ...GRAPH, abox: CURRENT });
      customers("Alice", "Bob", "Carol", "Dave");
      await wait(15_100);
      expect(
        await screen.findByText("4 instances in the active version"),
      ).toBeTruthy();
      expect(await screen.findByText("Dave")).toBeTruthy();
      expect(listDomainInstances).toHaveBeenCalledTimes(2);
      expect(screen.queryByText(/Out of date/)).toBeNull();
      expect(screen.queryByText("counts dimmed")).toBeNull();
      expect(studio.getStudioGraph).toHaveBeenCalledTimes(2);

      // Nothing is dimmed any more: the page stops reading the domain data.
      await wait(60_000);
      expect(getDomainAbox).toHaveBeenCalledTimes(2);
    });

    it("keeps the page, the Inspector and what was typed when a read fails, and reads again at the next check", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      // The build for v3 has landed by the first check.
      studio.getStudioGraph.mockResolvedValue({ ...GRAPH, abox: STALE });
      customers();
      getDomainAbox.mockResolvedValue(domainData(3));
      renderPage();
      fireEvent.click(await screen.findByText("select Customer"));
      const input = (await screen.findByRole("textbox", {
        name: "Label",
      })) as HTMLInputElement;
      fireEvent.change(input, { target: { value: "Client" } });

      // The graph cannot be read: the page stays as it was.
      studio.getStudioGraph.mockRejectedValueOnce(
        new TypeError("Failed to fetch"),
      );
      await wait(15_100);
      expect(studio.getStudioGraph).toHaveBeenCalledTimes(2);
      expect(
        screen.queryByText("Could not load the designer draft"),
      ).toBeNull();
      expect(screen.getByText("counts dimmed")).toBeTruthy();

      // The term cannot be read: neither it nor the graph changes.
      studio.getStudioGraph.mockResolvedValue({ ...GRAPH, abox: CURRENT });
      studio.getStudioTerm.mockRejectedValueOnce(
        new TypeError("Failed to fetch"),
      );
      await wait(15_000);
      expect(studio.getStudioGraph).toHaveBeenCalledTimes(3);
      expect(screen.queryByText("Could not load the term")).toBeNull();
      expect(screen.getByText("counts dimmed")).toBeTruthy();
      expect(screen.getByText(/Out of date/)).toBeTruthy();
      expect(input.isConnected).toBe(true);
      expect(input.value).toBe("Client");

      // The next check reads both.
      customers("Dave");
      await wait(15_000);
      expect(
        await screen.findByText("1 instance in the active version"),
      ).toBeTruthy();
      expect(screen.queryByText("counts dimmed")).toBeNull();
      expect(screen.queryByText(/Out of date/)).toBeNull();
      expect(input.isConnected).toBe(true);
      expect(input.value).toBe("Client");
    });

    it("shows the term a selection asked for when the check's graph read fails", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      studio.getStudioGraph.mockResolvedValue({ ...GRAPH, abox: STALE });
      getDomainAbox.mockResolvedValue(domainData(3));
      studio.getStudioTerm.mockResolvedValue(CUSTOMER_TERM);
      renderPage();
      fireEvent.click(await screen.findByText("select Customer"));
      expect(await screen.findByDisplayValue("Customer")).toBeTruthy();

      // The check's reload starts, and its draft read is held: the selection below lands inside it.
      const heldDraft = deferred<StudioDraft>();
      studio.getStudioDraft.mockReturnValueOnce(heldDraft.promise);
      await wait(15_100);

      // The user selects another class; the check reads the same term after it.
      const selectionRead = deferred<StudioTerm>();
      studio.getStudioTerm.mockReturnValueOnce(selectionRead.promise);
      fireEvent.click(screen.getByText("select Order"));
      studio.getStudioTerm.mockResolvedValue(ORDER_TERM);
      studio.getStudioGraph.mockRejectedValueOnce(
        new TypeError("Failed to fetch"),
      );
      await act(async () => heldDraft.resolve(draft()));

      // The check's reload ended at its graph read: the selection's read still shows.
      await act(async () => selectionRead.resolve(ORDER_TERM));
      expect(await screen.findByDisplayValue("Order")).toBeTruthy();
      expect(screen.queryByText("Could not load the term")).toBeNull();
    });

    it("shows the term a selection asked for when a reload supersedes the check's", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      studio.getStudioGraph.mockResolvedValue({ ...GRAPH, abox: STALE });
      getDomainAbox.mockResolvedValue(domainData(3));
      studio.getStudioTerm.mockResolvedValue(CUSTOMER_TERM);
      renderPage();
      fireEvent.click(await screen.findByText("select Customer"));
      expect(await screen.findByDisplayValue("Customer")).toBeTruthy();

      const heldDraft = deferred<StudioDraft>();
      studio.getStudioDraft.mockReturnValueOnce(heldDraft.promise);
      await wait(15_100);

      // The user selects another class, then both reads of it are held.
      const selectionRead = deferred<StudioTerm>();
      studio.getStudioTerm.mockReturnValueOnce(selectionRead.promise);
      fireEvent.click(screen.getByText("select Order"));
      const checkRead = deferred<StudioTerm>();
      studio.getStudioTerm.mockReturnValueOnce(checkRead.promise);
      await act(async () => heldDraft.resolve(draft()));

      // Another focus reloads on the same revision: it reads no term, and it
      // supersedes the check's reload, which then shows nothing.
      fireEvent.click(screen.getByText("focus Order"));
      await waitFor(() =>
        expect(studio.getStudioGraph).toHaveBeenCalledTimes(3),
      );
      await act(async () => {
        checkRead.resolve(ORDER_TERM);
        selectionRead.resolve(ORDER_TERM);
      });
      expect(await screen.findByDisplayValue("Order")).toBeTruthy();
    });

    it("reads no history: the older revisions loaded stay", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const entry = (revision: number, line: string) => ({
        revision,
        kind: "apply",
        author: "alice",
        at: null,
        summary: [line],
        opCount: 1,
        target: null,
        warnings: [],
        active: true,
        undone: false,
      });
      const page = { revision: 2, canUndo: true, canRedo: false };
      studio.listStudioHistory
        .mockResolvedValueOnce({
          ...page,
          entries: [entry(2, "Added class Return")],
          nextToken: "t-1",
        })
        .mockResolvedValueOnce({
          ...page,
          entries: [entry(1, "Added class Refund")],
        });
      studio.getStudioDraft.mockResolvedValue(
        draft({ editable: false, status: "accepted" }),
      );
      studio.getStudioGraph.mockResolvedValue({ ...GRAPH, abox: STALE });
      getDomainAbox.mockResolvedValue(domainData(2));
      renderPage();
      fireEvent.click(await screen.findByRole("button", { name: "History" }));
      fireEvent.click(
        await screen.findByRole("button", { name: "Load older revisions" }),
      );
      expect(await screen.findByText("Added class Refund")).toBeTruthy();

      // The build lands: the counts are read again, the history is not.
      getDomainAbox.mockResolvedValue(domainData(3));
      studio.getStudioGraph.mockResolvedValue({ ...GRAPH, abox: CURRENT });
      await wait(15_100);
      await waitFor(() =>
        expect(screen.queryByText("counts dimmed")).toBeNull(),
      );
      expect(screen.getByText("Added class Refund")).toBeTruthy();
      expect(screen.getByText("Added class Return")).toBeTruthy();
      expect(studio.listStudioHistory).toHaveBeenCalledTimes(2);
    });

    it("reads no domain data while the counts are current, or once the page is closed", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      studio.getStudioGraph.mockResolvedValue({ ...GRAPH, abox: CURRENT });
      const current = renderPage();
      await screen.findByText("Shipping");
      await wait(60_000);
      expect(getDomainAbox).not.toHaveBeenCalled();
      current.unmount();

      studio.getStudioGraph.mockResolvedValue({ ...GRAPH, abox: STALE });
      getDomainAbox.mockResolvedValue(domainData(2));
      const dimmed = renderPage();
      await screen.findByText("counts dimmed");
      dimmed.unmount();
      await wait(60_000);
      expect(getDomainAbox).not.toHaveBeenCalled();
    });

    it("stops after 10 minutes, but not while a build runs or waits", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      studio.getStudioGraph.mockResolvedValue({ ...GRAPH, abox: STALE });
      getDomainAbox.mockResolvedValue(domainData(2, { status: "BUILDING" }));
      renderPage();
      await screen.findByText("counts dimmed");
      await wait(11 * 60_000);
      const polls = getDomainAbox.mock.calls.length;
      expect(polls).toBeGreaterThanOrEqual(43);

      // The build failed and nothing waits: past 10 minutes, this check is the last.
      getDomainAbox.mockResolvedValue(domainData(2, { status: "FAILED" }));
      await wait(60_000);
      expect(getDomainAbox).toHaveBeenCalledTimes(polls + 1);
      expect(studio.getStudioGraph).toHaveBeenCalledTimes(1);
    });

    /** The counts dimmed, a failed build for `built` behind them, and the first 10 minutes over. */
    async function gaveUpWaiting(built = 2): Promise<number> {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      studio.getStudioGraph.mockResolvedValue({
        ...GRAPH,
        abox: {
          ...STALE,
          conformsToVersion: built,
          reason: `The domain data was built for v${built}; v3 is active`,
        },
      });
      getDomainAbox.mockResolvedValue(domainData(built, { status: "FAILED" }));
      renderPage();
      await screen.findByText("counts dimmed");
      await wait(11 * 60_000);
      const polls = getDomainAbox.mock.calls.length;
      await wait(60_000);
      expect(getDomainAbox).toHaveBeenCalledTimes(polls);
      return polls;
    }

    /** The domain data, and the graph, built for v4 while v4 is active. */
    function builtForV4() {
      getDomainAbox.mockResolvedValue(
        domainData(4, { current: true, activeVersion: 4 }),
      );
      studio.getStudioGraph.mockResolvedValue({
        ...GRAPH,
        abox: { ...CURRENT, conformsToVersion: 4, activeVersion: 4 },
      });
    }

    it("waits again when the graph reports another build to wait for", async () => {
      const polls = await gaveUpWaiting();

      // A deploy elsewhere makes v4 active: a reload reads that, and the wait
      // starts over for the rebuild it asks for.
      studio.getStudioGraph.mockResolvedValue({
        ...GRAPH,
        abox: {
          ...STALE,
          activeVersion: 4,
          reason: "The domain data was built for v2; v4 is active",
        },
      });
      fireEvent.click(screen.getByText("focus Order"));
      await waitFor(() =>
        expect(studio.getStudioGraph).toHaveBeenCalledTimes(2),
      );

      builtForV4();
      await wait(15_100);
      expect(getDomainAbox).toHaveBeenCalledTimes(polls + 1);
      await waitFor(() =>
        expect(screen.queryByText("counts dimmed")).toBeNull(),
      );
    });

    it("waits again when the graph reports a build that started for the same version", async () => {
      const polls = await gaveUpWaiting();

      // A build of v3 started after the one that failed: the versions are
      // those of the failed build, so only the status says there is a new one.
      studio.getStudioGraph.mockResolvedValue({
        ...GRAPH,
        abox: { ...STALE, status: "BUILDING" },
      });
      fireEvent.click(screen.getByText("focus Order"));
      await waitFor(() =>
        expect(studio.getStudioGraph).toHaveBeenCalledTimes(2),
      );

      getDomainAbox.mockResolvedValue(domainData(3));
      studio.getStudioGraph.mockResolvedValue({ ...GRAPH, abox: CURRENT });
      await wait(15_100);
      expect(getDomainAbox).toHaveBeenCalledTimes(polls + 1);
      await waitFor(() =>
        expect(screen.queryByText("counts dimmed")).toBeNull(),
      );
    });

    it("waits again when a rebuild landed for a version that is stale too", async () => {
      const polls = await gaveUpWaiting(1);

      // The build that followed the failed one landed for v2, which v3 being
      // active leaves stale: only the version the data conforms to says the
      // page has a further rebuild to wait for.
      studio.getStudioGraph.mockResolvedValue({ ...GRAPH, abox: STALE });
      fireEvent.click(screen.getByText("focus Order"));
      await waitFor(() =>
        expect(studio.getStudioGraph).toHaveBeenCalledTimes(2),
      );

      getDomainAbox.mockResolvedValue(domainData(3));
      studio.getStudioGraph.mockResolvedValue({ ...GRAPH, abox: CURRENT });
      await wait(15_100);
      expect(getDomainAbox).toHaveBeenCalledTimes(polls + 1);
      await waitFor(() =>
        expect(screen.queryByText("counts dimmed")).toBeNull(),
      );
    });

    it("waits again for the rebuild an accept from this page asks for", async () => {
      const polls = await gaveUpWaiting();

      // The accept publishes v4 and deploys it, which rebuilds the domain data.
      // The graph still reports the build for v2, so only the accept says so.
      acceptProposal.mockResolvedValue({
        proposal_id: "p-1",
        status: "accepting",
      });
      fireEvent.click(topBarAccept());
      const dialog = screen.getByRole("dialog", { name: "Accept revision 2" });
      await within(dialog).findByText("Classes removed (1)");
      getProposal.mockResolvedValue({
        proposal_id: "p-1",
        status: "accepted",
        published_version: 4,
        auto_deploy_id: "d-1",
        metadata: { label: "Shipping" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Accept" }));
      await wait(2100); // The accept lands on the first poll of the proposal.
      expect(await screen.findByText(/Published as version v4\./)).toBeTruthy();

      builtForV4();
      await wait(15_100);
      expect(getDomainAbox.mock.calls.length).toBeGreaterThan(polls);
      await waitFor(() =>
        expect(screen.queryByText("counts dimmed")).toBeNull(),
      );
    });
  });

  it("shows the history on its tab and refreshes only the history", async () => {
    studio.listStudioHistory.mockResolvedValue({
      revision: 2,
      canUndo: true,
      canRedo: false,
      entries: [
        {
          revision: 2,
          kind: "apply",
          author: "alice",
          at: null,
          summary: ["Added class Return"],
          opCount: 1,
          target: null,
          warnings: [],
          active: true,
          undone: false,
        },
      ],
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "History" }));
    expect(await screen.findByText("Added class Return")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Refresh history" }));
    await waitFor(() =>
      expect(studio.listStudioHistory).toHaveBeenCalledTimes(2),
    );
    expect(studio.getStudioDraft).toHaveBeenCalledTimes(1);
  });

  it("appends the older history page after the newest one", async () => {
    const entry = (revision: number, line: string) => ({
      revision,
      kind: "apply",
      author: "alice",
      at: null,
      summary: [line],
      opCount: 1,
      target: null,
      warnings: [],
      active: true,
      undone: false,
    });
    const page = { revision: 2, canUndo: true, canRedo: false };
    studio.listStudioHistory
      .mockResolvedValueOnce({
        ...page,
        entries: [entry(2, "Added class Return")],
        nextToken: "t-1",
      })
      .mockResolvedValueOnce({
        ...page,
        entries: [entry(1, "Added class Refund")],
      });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "History" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Load older revisions" }),
    );
    expect(await screen.findByText("Added class Refund")).toBeTruthy();
    expect(screen.getByText("Added class Return")).toBeTruthy();
    expect(studio.listStudioHistory).toHaveBeenLastCalledWith(
      expect.anything(),
      expect.anything(),
      expect.anything(),
      { nextToken: "t-1" },
    );
    expect(
      screen.queryByRole("button", { name: "Load older revisions" }),
    ).toBeNull();
  });

  describe("rebase", () => {
    it("rebases, and offers to skip the ops that no longer apply", async () => {
      studio.getStudioDraft.mockResolvedValue(
        draft({ rebaseRequired: true, headVersion: 4 }),
      );
      studio.rebaseStudioDraft
        .mockRejectedValueOnce(
          new ApiError(409, "RebaseFailed", "1 op", {
            detail: {
              code: "RebaseFailed",
              revision: 2,
              failures: [
                {
                  opId: "3:0",
                  op: { op: "add_class", label: "Warehouse" },
                  error: "already exists",
                },
              ],
            },
          }),
        )
        .mockResolvedValueOnce({
          revision: 4,
          baseVersion: 4,
          replayed: 0,
          skipped: ["3:0"],
          canUndo: false,
          canRedo: false,
        });
      renderPage();
      expect(
        await screen.findByText("A newer version was published"),
      ).toBeTruthy();
      expect(topBarAccept().disabled).toBe(true);
      fireEvent.click(screen.getByRole("button", { name: "Rebase" }));
      await waitFor(() =>
        expect(studio.rebaseStudioDraft).toHaveBeenCalledWith(
          api,
          "ns",
          "p-1",
          2,
          [],
        ),
      );
      expect(
        await screen.findByText(/add_class \(3:0\): already exists/),
      ).toBeTruthy();
      fireEvent.click(
        screen.getByRole("button", { name: "Rebase without the checked ops" }),
      );
      await waitFor(() =>
        expect(studio.rebaseStudioDraft).toHaveBeenLastCalledWith(
          api,
          "ns",
          "p-1",
          2,
          ["3:0"],
        ),
      );
      expect(await screen.findByText(/Rebased onto v4/)).toBeTruthy();
    });

    it("keeps the ops it skipped listed and ticked when the retry fails on another op", async () => {
      const failure = (opId: string, label: string) => ({
        opId,
        op: { op: "add_class", label },
        error: "already exists",
      });
      const failed = (...failures: ReturnType<typeof failure>[]) =>
        new ApiError(409, "RebaseFailed", "ops", {
          detail: { code: "RebaseFailed", revision: 2, failures },
        });
      studio.getStudioDraft.mockResolvedValue(
        draft({ rebaseRequired: true, headVersion: 4 }),
      );
      studio.rebaseStudioDraft
        .mockRejectedValueOnce(failed(failure("3:0", "Warehouse")))
        // The skipped op is not replayed, so only the new failure is reported.
        .mockRejectedValueOnce(failed(failure("4:0", "Depot")))
        .mockResolvedValueOnce({
          revision: 4,
          baseVersion: 4,
          replayed: 0,
          skipped: ["3:0", "4:0"],
          canUndo: false,
          canRedo: false,
        });
      renderPage();
      fireEvent.click(await screen.findByRole("button", { name: "Rebase" }));
      expect(
        await screen.findByText(/add_class \(3:0\): already exists/),
      ).toBeTruthy();
      fireEvent.click(
        screen.getByRole("button", { name: "Rebase without the checked ops" }),
      );
      await waitFor(() =>
        expect(studio.rebaseStudioDraft).toHaveBeenLastCalledWith(
          api,
          "ns",
          "p-1",
          2,
          ["3:0"],
        ),
      );
      // Both ops are listed, and both stay ticked.
      expect(
        await screen.findByText(/add_class \(4:0\): already exists/),
      ).toBeTruthy();
      expect(
        screen.getByText(/add_class \(3:0\): already exists/),
      ).toBeTruthy();
      const ticked = screen
        .getAllByRole("checkbox")
        .map((c) => (c as HTMLInputElement).checked);
      expect(ticked).toEqual([true, true]);
      fireEvent.click(
        screen.getByRole("button", { name: "Rebase without the checked ops" }),
      );
      await waitFor(() =>
        expect(studio.rebaseStudioDraft).toHaveBeenLastCalledWith(
          api,
          "ns",
          "p-1",
          2,
          ["3:0", "4:0"],
        ),
      );
      expect(await screen.findByText(/Rebased onto v4/)).toBeTruthy();
    });

    it("says when there is nothing to rebase and stops asking", async () => {
      // The accept asked for a rebase, but someone rebased the draft meanwhile.
      acceptProposal.mockRejectedValue(
        new ApiError(409, "RebaseRequired", "rebase it first", {
          detail: { code: "RebaseRequired", baseVersion: 3, headVersion: 4 },
        }),
      );
      studio.rebaseStudioDraft.mockRejectedValue(
        new ApiError(409, "NothingToRebase", "already on v3", {
          detail: { code: "NothingToRebase", message: "already on v3" },
        }),
      );
      renderPage();
      await acceptInDialog();
      fireEvent.click(await screen.findByRole("button", { name: "Rebase" }));
      expect(
        await screen.findByText(
          "The draft is already built on the newest version.",
        ),
      ).toBeTruthy();
      await waitFor(() => expect(topBarAccept().disabled).toBe(false));
      expect(screen.queryByText("A newer version was published")).toBeNull();
    });
  });

  describe("accept", () => {
    it("accepts the reviewed revision and reports the published version", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      acceptProposal.mockResolvedValue({
        proposal_id: "p-1",
        status: "accepting",
      });
      renderPage();
      await screen.findByText("Shipping");
      fireEvent.click(topBarAccept());
      const dialog = screen.getByRole("dialog", { name: "Accept revision 2" });
      expect(
        await within(dialog).findByText("Classes removed (1)"),
      ).toBeTruthy();
      expect(
        within(dialog).getByText(/Customers \(uses Customer\)/),
      ).toBeTruthy();
      expect(
        within(dialog).getByText(
          /deploy this version from Ontology › Induction › Versions and acknowledge them\./,
        ),
      ).toBeTruthy();
      getProposal.mockResolvedValue({
        proposal_id: "p-1",
        status: "accepted",
        published_version: 4,
        metadata: { label: "Shipping" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Accept" }));
      await waitFor(() =>
        expect(acceptProposal).toHaveBeenCalledWith(api, "ns", "p-1", {
          deploy: true,
          expectedRevision: 2,
        }),
      );
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2100);
      });
      expect(await screen.findByText(/Published as version v4\./)).toBeTruthy();
    });

    async function acceptReturning(...fresh: Record<string, unknown>[]) {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      acceptProposal.mockResolvedValue({
        proposal_id: "p-1",
        status: "accepting",
      });
      renderPage();
      await screen.findByText("Shipping");
      fireEvent.click(topBarAccept());
      const dialog = screen.getByRole("dialog", { name: "Accept revision 2" });
      await within(dialog).findByText("Classes removed (1)");
      const accepted = {
        proposal_id: "p-1",
        status: "accepted",
        published_version: 4,
        metadata: { label: "Shipping" },
      };
      for (const f of fresh)
        getProposal.mockResolvedValueOnce({ ...accepted, ...f });
      getProposal.mockResolvedValue({
        ...accepted,
        ...fresh[fresh.length - 1],
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Accept" }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2100);
      });
    }

    it("says when the deployment the accept asked for was held", async () => {
      await acceptReturning({
        auto_deploy_held: {
          reason: "BREAKING_METRICS",
          metrics: [
            { metricId: "m-1", name: "visit_count", missingClassIri: "x" },
          ],
        },
      });
      expect(
        await screen.findByText(
          "Published as version v4, but not deployed. Deploying would break governed metrics: visit_count. Review the impact and deploy from Ontology › Induction › Versions.",
        ),
      ).toBeTruthy();
    });

    it("reports the deployment once the accept worker records it", async () => {
      await acceptReturning({}, { auto_deploy_id: "d-1" });
      expect(
        await screen.findByText(/Waiting for the deployment to start/),
      ).toBeTruthy();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2100);
      });
      expect(
        await screen.findByText(
          "Published as version v4. Deployment started. Follow it in Ontology › Induction › Versions.",
        ),
      ).toBeTruthy();
    });

    it("shows the fixed message when breaking metrics could not be checked", async () => {
      const unchecked =
        "Breaking metrics could not be checked; the graph database may be unreachable.";
      studio.getStudioAcceptPreview.mockResolvedValue({
        ...PREVIEW,
        breakingMetrics: [],
        breakingMetricsError: unchecked,
      });
      renderPage();
      await screen.findByText("Shipping");
      fireEvent.click(topBarAccept());
      const dialog = screen.getByRole("dialog", { name: "Accept revision 2" });
      expect(await within(dialog).findByText(unchecked)).toBeTruthy();
    });

    it("offers the rebase the accept preview found once the dialog is dismissed", async () => {
      studio.getStudioAcceptPreview.mockResolvedValue({
        ...PREVIEW,
        rebaseRequired: true,
        headVersion: 4,
      });
      renderPage();
      await screen.findByText("Shipping");
      expect(screen.queryByText("A newer version was published")).toBeNull();
      fireEvent.click(topBarAccept());
      const dialog = screen.getByRole("dialog", { name: "Accept revision 2" });
      expect(await within(dialog).findByText("Rebase first")).toBeTruthy();
      fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
      expect(screen.getByText("A newer version was published")).toBeTruthy();
      expect(screen.getByText(/version is v4/)).toBeTruthy();
      expect(screen.getByRole("button", { name: "Rebase" })).toBeTruthy();
      expect(topBarAccept().disabled).toBe(true);
    });

    it("reloads when the accept preview is of a newer revision", async () => {
      studio.getStudioDraft
        .mockResolvedValueOnce(draft())
        .mockResolvedValue(draft({ revision: 3 }));
      studio.getStudioAcceptPreview.mockResolvedValue({
        ...PREVIEW,
        revision: 3,
      });
      renderPage();
      await screen.findByText("Shipping");
      fireEvent.click(topBarAccept());
      expect(
        await screen.findByText("Revision 3, built on v3, 2 classes"),
      ).toBeTruthy();
      expect(
        screen.getByRole("dialog", { name: "Accept revision 3" }),
      ).toBeTruthy();
      // The preview of revision 3 then matches: no further reload.
      await waitFor(() =>
        expect(studio.getStudioAcceptPreview).toHaveBeenCalledTimes(2),
      );
      expect(studio.getStudioDraft).toHaveBeenCalledTimes(2);
    });

    it("reloads when another change landed before the accept", async () => {
      acceptProposal.mockRejectedValue(
        new ApiError(409, "DraftConflict", "moved", {
          detail: { code: "DraftConflict", message: "moved", revision: 3 },
        }),
      );
      renderPage();
      await acceptInDialog();
      expect(await screen.findByText(conflictMessage())).toBeTruthy();
      expect(studio.getStudioDraft).toHaveBeenCalledTimes(2);
    });

    it("asks for a rebase when a newer version was published first", async () => {
      acceptProposal.mockRejectedValue(
        new ApiError(409, "RebaseRequired", "rebase it first", {
          detail: {
            code: "RebaseRequired",
            message: "rebase it first",
            baseVersion: 3,
            headVersion: 4,
          },
        }),
      );
      studio.rebaseStudioDraft.mockResolvedValue({
        revision: 3,
        baseVersion: 4,
        replayed: 1,
        skipped: [],
        canUndo: false,
        canRedo: false,
      });
      renderPage();
      await acceptInDialog();
      expect(
        await screen.findByText("A newer version was published"),
      ).toBeTruthy();
      expect(screen.getByText(/built on v3, and the newest/)).toBeTruthy();
      expect(screen.getByText(/version is v4/)).toBeTruthy();
      expect(topBarAccept().disabled).toBe(true);
      fireEvent.click(screen.getByRole("button", { name: "Rebase" }));
      await waitFor(() =>
        expect(studio.rebaseStudioDraft).toHaveBeenCalledWith(
          api,
          "ns",
          "p-1",
          2,
          [],
        ),
      );
      await waitFor(() =>
        expect(screen.queryByText("A newer version was published")).toBeNull(),
      );
    });

    it("offers a rebase when the publish found a newer version", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      acceptProposal.mockResolvedValue({
        proposal_id: "p-1",
        status: "accepting",
      });
      renderPage();
      await acceptInDialog();
      getProposal.mockResolvedValue({
        proposal_id: "p-1",
        status: "accept_failed",
        accept_error:
          "v4 was published first; rebase the draft in the Ontology designer and accept again.",
        accept_progress: {
          step: "publish",
          state: "failed",
          code: "RebaseRequired",
        },
        metadata: { label: "Shipping" },
      });
      studio.getStudioDraft.mockResolvedValue(
        draft({
          status: "accept_failed",
          rebaseRequired: true,
          headVersion: 4,
        }),
      );
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2100);
      });
      expect(await screen.findByText(/v4 was published first/)).toBeTruthy();
      expect(screen.getByText("A newer version was published")).toBeTruthy();
      expect(screen.getByRole("button", { name: "Rebase" })).toBeTruthy();
    });

    it("trusts the failure code only while the accept is failed", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      acceptProposal.mockResolvedValue({
        proposal_id: "p-1",
        status: "accepting",
      });
      renderPage();
      await acceptInDialog();
      // The older failure shape (pending + accept_error) with a code an
      // earlier failed accept left behind.
      getProposal.mockResolvedValue({
        proposal_id: "p-1",
        status: "pending",
        accept_error: "ingest failed",
        accept_progress: {
          step: "publish",
          state: "failed",
          code: "RebaseRequired",
        },
        metadata: { label: "Shipping" },
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2100);
      });
      expect(await screen.findByText("ingest failed")).toBeTruthy();
      expect(screen.queryByText("A newer version was published")).toBeNull();
      expect(topBarAccept().disabled).toBe(false);
    });

    it.each([
      [
        "a designer error",
        new ApiError(
          422,
          "ExpectedRevisionRequired",
          "Accept it with the expectedRevision you reviewed.",
          {
            detail: {
              code: "ExpectedRevisionRequired",
              message: "Accept it with the expectedRevision you reviewed.",
            },
          },
        ),
        "Accept it with the expectedRevision you reviewed.",
      ],
      [
        "an accept error with a plain detail",
        new ApiError(
          409,
          "UNKNOWN_ERROR",
          "Accept already in progress for this proposal",
          { detail: "Accept already in progress for this proposal" },
        ),
        "Accept already in progress for this proposal",
      ],
      [
        "a request the engine could not parse",
        new ApiError(
          422,
          "UNKNOWN_ERROR",
          'API POST /accept failed (422): {"detail":[{"loc":["body"]}]}',
          { detail: [{ loc: ["body"], msg: "bad", type: "value_error" }] },
        ),
        "The request failed (HTTP 422). Reload the page and try again.",
      ],
    ])("shows why the accept was refused: %s", async (_, error, shown) => {
      acceptProposal.mockRejectedValue(error);
      renderPage();
      await acceptInDialog();
      expect(await screen.findByText(shown)).toBeTruthy();
      expect(screen.queryByText(/API POST/)).toBeNull();
    });
  });

  describe("a proposal not open in the designer", () => {
    it("offers to open a proposal that is not open in the designer yet", async () => {
      studio.getStudioDraft
        .mockRejectedValueOnce(
          new ApiError(404, "StudioDraftNotOpen", "not open", {
            detail: { code: "StudioDraftNotOpen" },
          }),
        )
        .mockResolvedValue(draft({ revision: 0 }));
      studio.openStudioDraft.mockResolvedValue(draft({ revision: 0 }));
      renderPage();
      fireEvent.click(
        await screen.findByRole("button", {
          name: "Open in Ontology designer",
        }),
      );
      await waitFor(() =>
        expect(studio.openStudioDraft).toHaveBeenCalledWith(api, "ns", "p-1"),
      );
      expect(await screen.findByText(/Revision 0/)).toBeTruthy();
    });

    it("cannot open a proposal that left review", async () => {
      getProposal.mockResolvedValue({
        proposal_id: "p-1",
        status: "accepted",
        metadata: { label: "Shipping" },
      });
      studio.getStudioDraft.mockRejectedValue(
        new ApiError(404, "StudioDraftNotOpen", "not open", {
          detail: { code: "StudioDraftNotOpen" },
        }),
      );
      renderPage();
      const open = (await screen.findByRole("button", {
        name: "Open in Ontology designer",
      })) as HTMLButtonElement;
      expect(open.disabled).toBe(true);
    });

    it("names the status of a proposal that left review in Japanese", async () => {
      getProposal.mockResolvedValue({
        proposal_id: "p-1",
        status: "cancelled",
        metadata: { label: "Shipping" },
      });
      studio.getStudioDraft.mockRejectedValue(
        new ApiError(404, "StudioDraftNotOpen", "not open", {
          detail: { code: "StudioDraftNotOpen" },
        }),
      );
      try {
        renderPageInJapanese();
        expect(
          await screen.findByText(
            "この提案の状態は「キャンセル済み」です。オントロジーデザイナーで開けるのは、レビュー待ちまたは更新済みの提案か、承認に失敗した提案だけです。",
          ),
        ).toBeTruthy();
      } finally {
        window.localStorage.clear();
        document.documentElement.lang = "";
      }
    });
  });

  it("is read-only once the proposal left review", async () => {
    studio.getStudioDraft.mockResolvedValue(
      draft({ editable: false, status: "accepted" }),
    );
    renderPage();
    expect(
      await screen.findByText(
        "This proposal is accepted; its draft is read-only.",
      ),
    ).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Undo" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(topBarAccept().disabled).toBe(true);
  });

  it("names the status of a read-only draft in Japanese", async () => {
    studio.getStudioDraft.mockResolvedValue(
      draft({ editable: false, status: "accepted" }),
    );
    try {
      renderPageInJapanese();
      expect(
        await screen.findByText(
          "この提案の状態は「承認済み」のため、ドラフトは読み取り専用です。",
        ),
      ).toBeTruthy();
    } finally {
      window.localStorage.clear();
      document.documentElement.lang = "";
    }
  });

  describe("Chat tab", () => {
    const CHANGE_SET = {
      changeSetId: "cs-1",
      turnId: "t-1",
      status: "pending" as const,
      createdAt: null,
      createdRevision: 2,
      createdBy: "alice",
      rationale: "Suppliers appear in the contracts",
      ops: [{ op: "add_class" as const, label: "Supplier" }],
      summary: ["Added class Supplier"],
      warnings: [],
      diffCounts: null,
      appliedRevision: null,
      appliedOpIndexes: null,
      appliedBy: null,
      appliedAt: null,
      discardedBy: null,
      discardedAt: null,
      stale: null,
    };

    beforeEach(() => {
      studio.listStudioEvents.mockResolvedValue({
        events: [
          {
            seq: 1,
            turnId: "t-1",
            at: null,
            type: "user_message",
            text: "Add suppliers",
            author: "alice",
            selection: [],
          },
          {
            seq: 2,
            turnId: "t-1",
            at: null,
            type: "change_set",
            changeSetId: "cs-1",
            opSummaries: ["Added class Supplier"],
          },
        ],
        lastSeq: 2,
        more: false,
        turn: {
          turnId: "t-1",
          status: "completed",
          author: "alice",
          startedAt: null,
          endedAt: null,
        },
      });
      studio.listStudioChangeSets.mockResolvedValue({
        revision: 2,
        changeSets: [CHANGE_SET],
      });
    });

    async function openChat() {
      await screen.findByText("Shipping");
      fireEvent.click(screen.getByRole("tab", { name: "Chat" }));
      return screen.findByText("Suppliers appear in the contracts");
    }

    it("applies a change set through the page's write path", async () => {
      studio.applyStudioChangeSet.mockResolvedValue({
        ...RESULT,
        changeSet: { ...CHANGE_SET, status: "applied", appliedRevision: 3 },
      });
      renderPage();
      await openChat();
      const draftReads = studio.getStudioDraft.mock.calls.length;
      fireEvent.click(screen.getByRole("button", { name: "Apply all" }));
      await waitFor(() =>
        expect(studio.applyStudioChangeSet).toHaveBeenCalledWith(
          api,
          "ns",
          "p-1",
          "cs-1",
          2,
          null,
        ),
      );
      await waitFor(() =>
        expect(studio.getStudioDraft.mock.calls.length).toBeGreaterThan(
          draftReads,
        ),
      );
    });

    it("reloads after a conflicting apply and says so", async () => {
      studio.applyStudioChangeSet.mockRejectedValue(
        new ApiError(409, "DraftConflict", "moved", {
          detail: { code: "DraftConflict", message: "moved", revision: 3 },
        }),
      );
      renderPage();
      await openChat();
      fireEvent.click(screen.getByRole("button", { name: "Apply all" }));
      expect(await screen.findByText(conflictMessage())).toBeTruthy();
    });

    it("shows why a change set no longer applies on its card", async () => {
      studio.applyStudioChangeSet.mockRejectedValue(
        new ApiError(409, "ChangeSetStale", "stale", {
          detail: {
            code: "ChangeSetStale",
            message:
              "The draft changed after the assistant proposed this change set",
          },
        }),
      );
      renderPage();
      await openChat();
      fireEvent.click(screen.getByRole("button", { name: "Apply all" }));
      expect(
        await screen.findByText(
          "The draft changed after the assistant proposed this change set",
        ),
      ).toBeTruthy();
      expect(studio.listStudioChangeSets.mock.calls.length).toBeGreaterThan(1);
    });

    it("draws a preview on the graph until it is hidden", async () => {
      studio.previewStudioChangeSet.mockResolvedValue({
        revision: 2,
        changeSetId: "cs-1",
        opIndexes: [0],
        applies: true,
        error: null,
        warnings: [],
        created: [],
        diff: null,
        graph: {
          ...GRAPH,
          nodes: [
            ...GRAPH.nodes,
            { ...node("http://e/o#Supplier"), preview: "added" },
          ].map((n) => ({ preview: null, ...n })),
          edges: [],
        },
      });
      renderPage();
      await openChat();
      fireEvent.click(screen.getByRole("button", { name: "Show on graph" }));
      expect(
        await screen.findByText(/previewing Customer, Order, Supplier/),
      ).toBeTruthy();
      expect(studio.previewStudioChangeSet).toHaveBeenCalledWith(
        api,
        "ns",
        "p-1",
        "cs-1",
        null,
        null,
      );
      fireEvent.click(screen.getByRole("button", { name: "hide the preview" }));
      expect(screen.queryByText(/previewing/)).toBeNull();
    });

    it("draws only the newest preview when answers arrive out of order", async () => {
      const answer = (label: string) => ({
        revision: 2,
        changeSetId: "cs-1",
        opIndexes: [0],
        applies: true,
        error: null,
        warnings: [],
        created: [],
        diff: null,
        graph: {
          ...GRAPH,
          nodes: [
            { ...node("http://e/o#" + label), preview: "added" as const },
          ],
          edges: [],
        },
      });
      const first = deferred<ReturnType<typeof answer>>();
      const second = deferred<ReturnType<typeof answer>>();
      studio.previewStudioChangeSet
        .mockReturnValueOnce(first.promise)
        .mockReturnValueOnce(second.promise);
      renderPage();
      await openChat();
      fireEvent.click(screen.getByRole("button", { name: "Show on graph" }));
      fireEvent.click(screen.getByRole("button", { name: "Show on graph" }));
      await act(async () => second.resolve(answer("Newest")));
      await act(async () => first.resolve(answer("Older")));
      expect(screen.getByText(/previewing Newest/)).toBeTruthy();
      expect(screen.queryByText(/previewing Older/)).toBeNull();
    });

    it("drops a preview that arrives after it was hidden", async () => {
      const late = deferred<unknown>();
      studio.previewStudioChangeSet.mockReturnValueOnce(late.promise);
      renderPage();
      await openChat();
      fireEvent.click(screen.getByRole("button", { name: "Show on graph" }));
      fireEvent.click(screen.getByRole("tab", { name: "Inspector" }));
      fireEvent.click(screen.getByRole("button", { name: "focus Customer" }));
      await act(async () =>
        late.resolve({
          revision: 2,
          changeSetId: "cs-1",
          opIndexes: [0],
          applies: true,
          error: null,
          warnings: [],
          created: [],
          diff: null,
          graph: {
            ...GRAPH,
            nodes: GRAPH.nodes.map((n) => ({ ...n, preview: null })),
            edges: [],
          },
        }),
      );
      expect(screen.queryByText(/previewing/)).toBeNull();
    });

    it("sends the selected classes with a message", async () => {
      studio.sendStudioMessage.mockResolvedValue({ turnId: "t-2", seq: 3 });
      renderPage();
      await screen.findByText("Shipping");
      fireEvent.click(screen.getByRole("button", { name: "select Customer" }));
      await openChat();
      expect(screen.getByText("Sent with: Customer")).toBeTruthy();
      fireEvent.change(screen.getByLabelText("Message to the assistant"), {
        target: { value: "Is this right?" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
      await waitFor(() =>
        expect(studio.sendStudioMessage).toHaveBeenCalledWith(
          api,
          "ns",
          "p-1",
          "Is this right?",
          [CUSTOMER],
        ),
      );
    });
  });
  describe("checks, queries and questions", () => {
    const FINDING =
      "The range of placed by is Ghost, which is not a class of the draft";
    beforeEach(() => {
      studio.getStudioChecks.mockResolvedValue({
        revision: 2,
        instant: {
          findings: [
            {
              code: "DANGLING_RANGE",
              severity: "error",
              message: FINDING,
              iris: [ORDER],
            },
          ],
          counts: { error: 1, warning: 0, info: 0 },
          truncated: false,
        },
        data: {
          tables: { checked: false, missing: [], error: null },
          abox: { available: false, impacts: [], error: null },
          mappings: {
            available: false,
            stale: 0,
            staleTargets: [],
            error: null,
          },
          metrics: { breaking: [], error: null },
        },
      });
      studio.listStudioEvents.mockResolvedValue({
        events: [],
        lastSeq: 0,
        more: false,
        turn: null,
      });
      studio.listStudioChangeSets.mockResolvedValue({
        revision: 2,
        changeSets: [],
      });
      studio.listStudioQuestions.mockResolvedValue({ questions: [] });
    });

    it("offers the six tabs in order", async () => {
      renderPage();
      await screen.findByText("Shipping");
      expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual([
        "Inspector",
        "Chat",
        "Checks",
        "Query",
        "Questions",
        "History",
      ]);
    });

    it("asks the assistant about the findings in the chat", async () => {
      renderPage();
      await screen.findByText("Shipping");
      fireEvent.click(screen.getByRole("tab", { name: "Checks" }));
      await screen.findByText(FINDING);
      fireEvent.click(
        screen.getByRole("button", { name: "Ask the assistant" }),
      );
      const box = (await screen.findByLabelText(
        "Message to the assistant",
      )) as HTMLTextAreaElement;
      await waitFor(() => expect(box.value).toContain(FINDING));
    });

    it("does not offer to ask the assistant on a read-only draft", async () => {
      studio.getStudioDraft.mockResolvedValue(
        draft({ editable: false, status: "cancelled" }),
      );
      renderPage();
      await screen.findByText(
        "This proposal is cancelled; its draft is read-only.",
      );
      fireEvent.click(screen.getByRole("tab", { name: "Checks" }));
      await screen.findByText(FINDING);
      const asks = () =>
        screen.getAllByRole("button", {
          name: "Ask the assistant",
        }) as HTMLButtonElement[];
      expect(asks().map((b) => b.disabled)).toEqual([true]);
      fireEvent.click(screen.getByRole("tab", { name: "Query" }));
      // The Checks tab stays mounted, hidden: both panels' buttons are off.
      expect(asks().map((b) => b.disabled)).toEqual([true, true]);
    });

    it("starts the query console on the VKG once the proposal is no longer under review, and says why", async () => {
      // The engine refuses a draft query then: 409 ProposalNotEditable.
      studio.getStudioDraft.mockResolvedValue(
        draft({ editable: false, status: "accepted" }),
      );
      renderPage();
      await screen.findByText(
        "This proposal is accepted; its draft is read-only.",
      );
      fireEvent.click(screen.getByRole("tab", { name: "Query" }));
      expect(
        (screen.getByLabelText("Query text") as HTMLTextAreaElement).value,
      ).toBe(sampleQuery("vkg", "ns"));
      fireEvent.mouseDown(
        screen.getByRole("button", { name: /What the query reads/ }),
      );
      const draftTarget = screen
        .getAllByRole("option")
        .find(
          (o) =>
            within(o).queryAllByText("Draft + documents (SPARQL)").length > 0,
        );
      expect(draftTarget).toHaveAttribute("aria-disabled", "true");
      expect(
        screen.getByText(
          "This proposal is accepted, so its draft can no longer be queried.",
        ),
      ).toBeTruthy();
    });

    describe("while the accept runs", () => {
      // The engine refuses a draft query from the moment the accept starts
      // (409 ProposalNotEditable: the proposal is accepting), before the page
      // reads the draft again.
      const ACCEPTING =
        "This proposal is being accepted, so its draft cannot be queried.";
      const ACCEPTED =
        "This proposal is accepted, so its draft can no longer be queried.";

      function runButton(): HTMLButtonElement {
        return screen.getByRole("button", { name: "Run" }) as HTMLButtonElement;
      }

      function isOff(button: HTMLButtonElement): boolean {
        return (
          button.disabled || button.getAttribute("aria-disabled") === "true"
        );
      }

      /** Opens the target list and returns the draft's option. */
      function draftOption(): HTMLElement {
        fireEvent.mouseDown(
          screen.getByRole("button", { name: /What the query reads/ }),
        );
        const option = screen
          .getAllByRole("option")
          .find(
            (o) =>
              within(o).queryAllByText("Draft + documents (SPARQL)").length > 0,
          );
        if (!option) throw new Error("No draft option");
        return option;
      }

      function closeTargets() {
        fireEvent.mouseDown(
          screen.getByRole("button", { name: /What the query reads/ }),
        );
      }

      async function startAccept() {
        getProposal.mockResolvedValue({
          proposal_id: "p-1",
          status: "accepting",
          metadata: { label: "Shipping" },
        });
        await acceptInDialog();
        await waitFor(() => expect(acceptProposal).toHaveBeenCalled());
      }

      beforeEach(() => {
        vi.useFakeTimers({ shouldAdvanceTime: true });
        acceptProposal.mockResolvedValue({
          proposal_id: "p-1",
          status: "accepting",
        });
      });

      it("keeps a draft query in the console but does not run it, and runs it again once the accept fails", async () => {
        renderPage();
        await screen.findByText("Shipping");
        fireEvent.click(screen.getByRole("tab", { name: "Query" }));
        expect(isOff(runButton())).toBe(false);
        await startAccept();
        expect(
          (screen.getByLabelText("Query text") as HTMLTextAreaElement).value,
        ).toBe(sampleQuery("draft", "ns"));
        expect(isOff(runButton())).toBe(true);
        expect(runButton()).toHaveAccessibleDescription(ACCEPTING);
        const option = draftOption();
        expect(option).toHaveAttribute("aria-disabled", "true");
        expect(option).toHaveAccessibleDescription(ACCEPTING);
        closeTargets();
        fireEvent.keyDown(screen.getByLabelText("Query text"), {
          key: "Enter",
          ctrlKey: true,
        });
        expect(studio.runStudioQuery).not.toHaveBeenCalled();
        // The accept fails: accept_failed is under review, and the draft can be queried again.
        getProposal.mockResolvedValue({
          proposal_id: "p-1",
          status: "accept_failed",
          accept_error: "ingest failed",
          metadata: { label: "Shipping" },
        });
        studio.getStudioDraft.mockResolvedValue(
          draft({ status: "accept_failed" }),
        );
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2100);
        });
        expect(await screen.findByText("ingest failed")).toBeTruthy();
        await waitFor(() => expect(isOff(runButton())).toBe(false));
        expect(draftOption()).not.toHaveAttribute("aria-disabled", "true");
      });

      it("starts a console first opened during the accept on the VKG, and says the proposal is accepted once it is", async () => {
        renderPage();
        await screen.findByText("Shipping");
        await startAccept();
        fireEvent.click(screen.getByRole("tab", { name: "Query" }));
        expect(
          (screen.getByLabelText("Query text") as HTMLTextAreaElement).value,
        ).toBe(sampleQuery("vkg", "ns"));
        expect(isOff(runButton())).toBe(false);
        const option = draftOption();
        expect(option).toHaveAttribute("aria-disabled", "true");
        expect(option).toHaveAccessibleDescription(ACCEPTING);
        closeTargets();
        // The accept lands.
        getProposal.mockResolvedValue({
          proposal_id: "p-1",
          status: "accepted",
          published_version: 4,
          metadata: { label: "Shipping" },
        });
        studio.getStudioDraft.mockResolvedValue(
          draft({ editable: false, status: "accepted" }),
        );
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2100);
        });
        await screen.findByText(
          "This proposal is accepted; its draft is read-only.",
        );
        expect(draftOption()).toHaveAccessibleDescription(ACCEPTED);
        // Still within the accept: the page waits for the deployment it asked for.
        expect(
          screen.getByText(/Waiting for the deployment to start/),
        ).toBeTruthy();
      });
    });

    it("keeps the query console's text after a visit to another tab", async () => {
      renderPage();
      await screen.findByText("Shipping");
      fireEvent.click(screen.getByRole("tab", { name: "Query" }));
      const editor = screen.getByLabelText("Query text") as HTMLTextAreaElement;
      fireEvent.change(editor, { target: { value: "ASK { ?s ?p ?o }" } });
      fireEvent.click(screen.getByRole("tab", { name: "Inspector" }));
      fireEvent.click(screen.getByRole("tab", { name: "Query" }));
      expect(
        (screen.getByLabelText("Query text") as HTMLTextAreaElement).value,
      ).toBe("ASK { ?s ?p ?o }");
    });

    it("keeps an unsaved description in the Inspector after a visit to another tab", async () => {
      studio.getStudioTerm.mockResolvedValue(CUSTOMER_TERM);
      renderPage();
      fireEvent.click(await screen.findByText("select Customer"));
      fireEvent.change(
        await screen.findByRole("textbox", { name: "Description" }),
        { target: { value: "Typed but not saved" } },
      );
      fireEvent.click(screen.getByRole("tab", { name: "Chat" }));
      fireEvent.click(screen.getByRole("tab", { name: "Inspector" }));
      expect(
        (
          screen.getByRole("textbox", {
            name: "Description",
          }) as HTMLTextAreaElement
        ).value,
      ).toBe("Typed but not saved");
    });

    it("keeps a typed chat message after a visit to another tab", async () => {
      renderPage();
      await screen.findByText("Shipping");
      fireEvent.click(screen.getByRole("tab", { name: "Chat" }));
      fireEvent.change(
        await screen.findByLabelText("Message to the assistant"),
        { target: { value: "Is Order right?" } },
      );
      fireEvent.click(screen.getByRole("tab", { name: "Checks" }));
      fireEvent.click(screen.getByRole("tab", { name: "Chat" }));
      expect(
        (
          screen.getByLabelText(
            "Message to the assistant",
          ) as HTMLTextAreaElement
        ).value,
      ).toBe("Is Order right?");
    });

    it("follows a running full validation through a visit to another tab", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      validateProposal.mockResolvedValue({
        job_id: "j-1",
        proposal_id: "p-1",
        status: "running",
        created_at: "",
      });
      getProposalValidationJob.mockResolvedValue({
        job_id: "j-1",
        proposal_id: "p-1",
        status: "completed",
        created_at: "",
        report: {
          job_id: "j-1",
          proposal_id: "p-1",
          passed: true,
          findings: [],
          tiers_run: ["1"],
          created_at: "",
        },
      });
      renderPage();
      await screen.findByText("Shipping");
      fireEvent.click(screen.getByRole("tab", { name: "Checks" }));
      await screen.findByText(FINDING);
      fireEvent.click(screen.getByText("Full validation"));
      fireEvent.click(
        screen.getByRole("button", { name: "Run full validation" }),
      );
      await waitFor(() => expect(validateProposal).toHaveBeenCalledTimes(1));
      fireEvent.click(screen.getByRole("tab", { name: "Inspector" }));
      fireEvent.click(screen.getByRole("tab", { name: "Checks" }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(VALIDATION_POLL_MS + 100);
      });
      expect(await screen.findByText("The validation passed.")).toBeTruthy();
      expect(getProposalValidationJob).toHaveBeenCalledWith(
        api,
        "ns",
        "p-1",
        "j-1",
      );
    });

    it("keeps a question being added after Check opened the chat", async () => {
      const QUESTION = {
        questionId: "q-1",
        text: "Which orders shipped late?",
        expectation: null,
        lastCheck: null,
        createdAt: "",
        updatedAt: "",
        updatedBy: "alice",
      };
      studio.listStudioQuestions.mockResolvedValue({ questions: [QUESTION] });
      studio.checkStudioQuestion.mockResolvedValue({ turnId: "t-1", seq: 1 });
      renderPage();
      await screen.findByText("Shipping");
      fireEvent.click(screen.getByRole("tab", { name: "Questions" }));
      await screen.findByText("Which orders shipped late?");
      fireEvent.change(screen.getByLabelText("Question"), {
        target: { value: "Who placed each order?" },
      });
      fireEvent.click(
        screen.getByRole("button", {
          name: 'Check question "Which orders shipped late?"',
        }),
      );
      expect(
        await screen.findByRole("tab", { name: "Chat", selected: true }),
      ).toBeTruthy();
      fireEvent.click(screen.getByRole("tab", { name: "Questions" }));
      expect(
        (screen.getByLabelText("Question") as HTMLTextAreaElement).value,
      ).toBe("Who placed each order?");
    });

    it("shows the check's turn in a chat opened before, and its verdict back on Questions", async () => {
      const QUESTION = {
        questionId: "q-1",
        text: "Which orders shipped late?",
        expectation: null,
        lastCheck: null,
        createdAt: "",
        updatedAt: "",
        updatedBy: "alice",
      };
      studio.listStudioQuestions.mockResolvedValue({ questions: [QUESTION] });
      studio.checkStudioQuestion.mockResolvedValue({ turnId: "t-1", seq: 1 });
      renderPage();
      await screen.findByText("Shipping");
      fireEvent.click(screen.getByRole("tab", { name: "Chat" }));
      await screen.findByText(/Ask the assistant to review/);
      fireEvent.click(screen.getByRole("tab", { name: "Questions" }));
      await screen.findByText("Which orders shipped late?");

      // The check starts a turn on the server, then opens the chat.
      studio.listStudioEvents.mockResolvedValue({
        events: [
          {
            seq: 1,
            turnId: "t-1",
            at: null,
            type: "user_message",
            text: "Check: Which orders shipped late?",
            author: "alice",
            selection: [],
          },
        ],
        lastSeq: 1,
        more: false,
        turn: {
          turnId: "t-1",
          status: "running",
          author: "alice",
          startedAt: null,
          endedAt: null,
        },
      });
      fireEvent.click(
        screen.getByRole("button", {
          name: 'Check question "Which orders shipped late?"',
        }),
      );
      expect(
        await screen.findByLabelText("The assistant is working"),
      ).toBeTruthy();
      expect(studio.listStudioEvents).toHaveBeenLastCalledWith(
        api,
        "ns",
        "p-1",
        0,
      );

      // The turn stored its verdict on the question.
      studio.listStudioQuestions.mockResolvedValue({
        questions: [
          {
            ...QUESTION,
            lastCheck: {
              proposalId: "p-1",
              revision: 2,
              status: "answerable",
              query: "SELECT ?o WHERE { ?o a ?c }",
              rowCount: 3,
              sample: [],
              explanation: null,
              checkedAt: "",
            },
          },
        ],
      });
      fireEvent.click(screen.getByRole("tab", { name: "Questions" }));
      expect(await screen.findByText("Answerable")).toBeTruthy();
    });

    it("opens a finding's term in the Inspector", async () => {
      renderPage();
      await screen.findByText("Shipping");
      fireEvent.click(screen.getByRole("tab", { name: "Checks" }));
      await screen.findByText(FINDING);
      fireEvent.click(screen.getByText("Order", { selector: "a, a *" }));
      await waitFor(() =>
        expect(studio.getStudioTerm).toHaveBeenCalledWith(
          api,
          "ns",
          "p-1",
          ORDER,
        ),
      );
      expect(
        screen.getByRole("tab", { name: "Inspector", selected: true }),
      ).toBeTruthy();
    });
  });

  describe("a role that only reads", () => {
    const READ_ONLY =
      "Your role on this namespace can read and query this draft, but not change it.";
    const DENIED = "Your role on this namespace does not allow this.";
    const QUESTION = {
      questionId: "q-1",
      text: "Which orders shipped late?",
      expectation: null,
      lastCheck: null,
      createdAt: "",
      updatedAt: "",
      updatedBy: "alice",
    };
    const CHANGE_SET = {
      changeSetId: "cs-1",
      turnId: "t-1",
      status: "pending" as const,
      createdAt: null,
      createdRevision: 2,
      createdBy: "alice",
      rationale: "Suppliers appear in the contracts",
      ops: [{ op: "add_class" as const, label: "Supplier" }],
      summary: ["Added class Supplier"],
      warnings: [],
      diffCounts: null,
      appliedRevision: null,
      appliedOpIndexes: null,
      appliedBy: null,
      appliedAt: null,
      discardedBy: null,
      discardedAt: null,
      stale: null,
    };

    /** Off: disabled, or aria-disabled where the button says why. */
    function isOff(button: HTMLElement): boolean {
      return (
        (button as HTMLButtonElement).disabled ||
        button.getAttribute("aria-disabled") === "true"
      );
    }

    function expectDenied(button: HTMLElement) {
      expect(isOff(button)).toBe(true);
      expect(button).toHaveAccessibleDescription(DENIED);
    }

    beforeEach(() => {
      access.canManage = false;
      studio.getStudioChecks.mockResolvedValue({
        revision: 2,
        instant: {
          findings: [],
          counts: { error: 0, warning: 0, info: 0 },
          truncated: false,
        },
        data: {
          tables: { checked: false, missing: [], error: null },
          abox: { available: false, impacts: [], error: null },
          mappings: {
            available: false,
            stale: 0,
            staleTargets: [],
            error: null,
          },
          metrics: { breaking: [], error: null },
        },
      });
      studio.listStudioEvents.mockResolvedValue({
        events: [
          {
            seq: 1,
            turnId: "t-1",
            at: null,
            type: "change_set",
            changeSetId: "cs-1",
            opSummaries: ["Added class Supplier"],
          },
        ],
        lastSeq: 1,
        more: false,
        turn: null,
      });
      studio.listStudioChangeSets.mockResolvedValue({
        revision: 2,
        changeSets: [CHANGE_SET],
      });
      studio.listStudioQuestions.mockResolvedValue({ questions: [QUESTION] });
    });

    it("says what the role can do and turns off the top bar's changes", async () => {
      studio.getStudioDraft.mockResolvedValue(
        draft({ canRedo: true, rebaseRequired: true }),
      );
      renderPage();
      expect(await screen.findByText(READ_ONLY)).toBeTruthy();
      expectDenied(screen.getByRole("button", { name: "Undo" }));
      expectDenied(screen.getByRole("button", { name: "Redo" }));
      expectDenied(screen.getByRole("button", { name: "Rebase" }));
      expectDenied(topBarAccept());
    });

    it("turns off the Inspector's edits, and says why", async () => {
      studio.getStudioTerm.mockResolvedValue(CUSTOMER_TERM);
      renderPage();
      fireEvent.click(await screen.findByText("select Customer"));
      expect(
        await screen.findByRole("textbox", { name: "Label" }),
      ).toBeDisabled();
      // In the Inspector, not only in the reasons of the buttons that are off.
      expect(
        screen.getAllByText(DENIED).filter((e) => !e.closest("button")),
      ).toHaveLength(1);
      expect(
        screen.queryByText(
          "This term is read-only here: the draft cannot change, or the term belongs to another ontology.",
        ),
      ).toBeNull();
    });

    it("turns off asking the assistant from Checks and Query, and says why", async () => {
      studio.getStudioChecks.mockResolvedValue({
        revision: 2,
        instant: {
          findings: [
            {
              code: "DANGLING_RANGE",
              severity: "error",
              message: "The range of placed by is not a class of the draft",
              iris: [ORDER],
            },
          ],
          counts: { error: 1, warning: 0, info: 0 },
          truncated: false,
        },
        data: {
          tables: { checked: false, missing: [], error: null },
          abox: { available: false, impacts: [], error: null },
          mappings: {
            available: false,
            stale: 0,
            staleTargets: [],
            error: null,
          },
          metrics: { breaking: [], error: null },
        },
      });
      renderPage();
      await screen.findByText("Shipping");
      fireEvent.click(screen.getByRole("tab", { name: "Checks" }));
      await screen.findByText(
        "The range of placed by is not a class of the draft",
      );
      expectDenied(screen.getByRole("button", { name: "Ask the assistant" }));
      fireEvent.click(screen.getByRole("tab", { name: "Query" }));
      // The Checks tab stays mounted, hidden: the second is the console's.
      const asks = screen.getAllByRole("button", { name: "Ask the assistant" });
      expect(asks).toHaveLength(2);
      expectDenied(asks[1]);
      fireEvent.click(asks[1]);
      expect(
        screen.getByRole("tab", { name: "Query", selected: true }),
      ).toBeTruthy();
    });

    it("turns off sending a message, and applying or discarding a change set", async () => {
      renderPage();
      await screen.findByText("Shipping");
      fireEvent.click(screen.getByRole("tab", { name: "Chat" }));
      await screen.findByText("Suppliers appear in the contracts");
      const box = screen.getByLabelText("Message to the assistant");
      expect(box).toBeDisabled();
      expect(box).toHaveAttribute("placeholder", DENIED);
      expectDenied(screen.getByRole("button", { name: "Send" }));
      expectDenied(screen.getByRole("button", { name: "Apply all" }));
      expectDenied(screen.getByRole("button", { name: "Discard" }));
      // Previewing only reads, so it stays offered.
      expect(isOff(screen.getByRole("button", { name: "Show on graph" }))).toBe(
        false,
      );
    });

    it("turns off a full validation, and saving, deleting or checking a question", async () => {
      renderPage();
      await screen.findByText("Shipping");
      fireEvent.click(screen.getByRole("tab", { name: "Checks" }));
      fireEvent.click(await screen.findByText("Full validation"));
      expectDenied(screen.getByRole("button", { name: "Run full validation" }));
      fireEvent.click(screen.getByRole("tab", { name: "Questions" }));
      await screen.findByText(QUESTION.text);
      for (const action of ["Check", "Edit", "Delete"]) {
        expectDenied(
          screen.getByRole("button", {
            name: `${action} question "${QUESTION.text}"`,
          }),
        );
      }
      expect(screen.queryByRole("button", { name: "Add question" })).toBeNull();
    });

    it("still queries the draft: the role reads it", async () => {
      renderPage();
      await screen.findByText(READ_ONLY);
      fireEvent.click(screen.getByRole("tab", { name: "Query" }));
      expect(
        (screen.getByLabelText("Query text") as HTMLTextAreaElement).value,
      ).toBe(sampleQuery("draft", "ns"));
      expect(isOff(screen.getByRole("button", { name: "Run" }))).toBe(false);
    });

    it("cannot open a draft for a proposal still in review", async () => {
      studio.getStudioDraft.mockRejectedValue(
        new ApiError(404, "StudioDraftNotOpen", "not open", {
          detail: { code: "StudioDraftNotOpen" },
        }),
      );
      renderPage();
      expectDenied(
        await screen.findByRole("button", {
          name: "Open in Ontology designer",
        }),
      );
      expect(studio.openStudioDraft).not.toHaveBeenCalled();
    });

    it("offers every change to a role that can change the ontology", async () => {
      access.canManage = true;
      studio.getStudioTerm.mockResolvedValue(CUSTOMER_TERM);
      renderPage();
      await screen.findByText("Shipping");
      expect(screen.queryByText(READ_ONLY)).toBeNull();
      expect(isOff(screen.getByRole("button", { name: "Undo" }))).toBe(false);
      expect(isOff(topBarAccept())).toBe(false);
      fireEvent.click(screen.getByText("select Customer"));
      expect(
        await screen.findByRole("textbox", { name: "Label" }),
      ).toBeEnabled();
      fireEvent.click(screen.getByRole("tab", { name: "Query" }));
      expect(
        isOff(screen.getByRole("button", { name: "Ask the assistant" })),
      ).toBe(false);
      fireEvent.click(screen.getByRole("tab", { name: "Chat" }));
      await screen.findByText("Suppliers appear in the contracts");
      expect(isOff(screen.getByRole("button", { name: "Apply all" }))).toBe(
        false,
      );
      expect(isOff(screen.getByRole("button", { name: "Discard" }))).toBe(
        false,
      );
      fireEvent.click(screen.getByRole("tab", { name: "Questions" }));
      await screen.findByText(QUESTION.text);
      expect(screen.getByRole("button", { name: "Add question" })).toBeTruthy();
    });
  });
});
