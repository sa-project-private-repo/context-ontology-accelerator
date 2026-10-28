// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SplitPanelProvider,
  useSplitPanel,
} from "@components/SplitPanelProvider";
import type { ApiClient } from "@components/ApiClientProvider";
import type { Proposal } from "../../../services/ontology-engine";
import { ProposalDetailRoute } from "./ProposalDetail";

// Leaving the proposal page, rendered as the app renders it: under the split
// panel's provider, with a consumer of it above the routes. React Router 7 runs
// every navigation as a transition, which an update the page keeps making
// would hold off forever: the address would change but the page would stay.

vi.mock("@components/graph/OntologyGraphFlow", () => ({
  OntologyGraphFlow: () => null,
}));

const api = { get: vi.fn(), post: vi.fn() } as unknown as ApiClient;
vi.mock("@components/ApiClientProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@components/ApiClientProvider")>()),
  useApiClient: () => api,
}));
vi.mock("@components/NamespaceProvider", () => ({
  useCurrentNamespace: () => ({ current: undefined }),
}));

const getProposal = vi.fn();
const cancelProposal = vi.fn();
vi.mock("../../../services/ontology-engine", async (importOriginal) => ({
  ...(await importOriginal<
    typeof import("../../../services/ontology-engine")
  >()),
  getProposal: (...a: unknown[]) => getProposal(...a),
  cancelProposal: (...a: unknown[]) => cancelProposal(...a),
}));
const openStudioDraft = vi.fn();
vi.mock("../../../services/studio", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../services/studio")>()),
  openStudioDraft: (...a: unknown[]) => openStudioDraft(...a),
}));

// One block per entity, as the inducer writes it: the entities panel lists the class.
const TURTLE = `@prefix ex: <http://e/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

ex:Customer a owl:Class ;
    rdfs:label "Customer" .`;

function proposal(overrides: Partial<Proposal> = {}): Proposal {
  return {
    proposal_id: "p-1",
    job_id: "j-1",
    namespace: "ns",
    ontology_id: "ont",
    proposal_type: "induced",
    status: "pending",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ontology_turtle: TURTLE,
    r2rml_turtle: "",
    ...overrides,
  };
}

const IN_DESIGNER = {
  studio: {
    base_version: 2,
    revision: 3,
    opened_at: "2026-10-19T00:00:00Z",
    opened_by: "alice",
  },
};

/** Past this many renders the shell drops the page, which ends a render loop. */
const RENDER_LIMIT = 200;
let shellRenders = 0;

/**
 * The app shell as App.tsx has it: it reads the split panel, so every change
 * of the panel renders the routes again, and it shows the panel beside them.
 */
function Shell() {
  const { state } = useSplitPanel();
  shellRenders += 1;
  if (shellRenders > RENDER_LIMIT) return <p>render loop</p>;
  return (
    <>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/ontology"
          element={<p>induction page</p>}
        />
        <Route
          path="/namespaces/:namespaceId/ontology/proposals/:proposalId"
          element={<ProposalDetailRoute />}
        />
        <Route
          path="/namespaces/:namespaceId/ontology/proposals/:proposalId/studio"
          element={<p>studio page</p>}
        />
      </Routes>
      {state.isOpen && (
        <aside aria-label="Split panel">
          <h2>{state.header}</h2>
          {state.content}
        </aside>
      )}
    </>
  );
}

function renderApp() {
  return render(
    <MemoryRouter initialEntries={["/namespaces/ns/ontology/proposals/p-1"]}>
      <SplitPanelProvider>
        <Shell />
      </SplitPanelProvider>
    </MemoryRouter>,
  );
}

/** The button whose label starts with `text` (Cloudscape appends a disabled reason). */
function buttonLabelled(text: string): HTMLElement {
  const button = screen
    .getAllByRole("button")
    .find((b) => b.textContent?.startsWith(text));
  if (!button) throw new Error(`no button labelled ${text}`);
  return button;
}

let consoleError: ReturnType<typeof vi.spyOn>;

/** React's warning for an effect that sets state on every render. */
function updateDepthWarnings(): unknown[][] {
  return consoleError.mock.calls.filter((args: unknown[]) =>
    String(args[0]).includes("Maximum update depth exceeded"),
  );
}

describe.each([
  { selection: "no class selected", selectClass: false },
  { selection: "a class selected", selectClass: true },
])("leaving the proposal page with $selection", ({ selectClass }) => {
  beforeEach(() => {
    vi.resetAllMocks();
    shellRenders = 0;
    consoleError = vi.spyOn(console, "error");
    openStudioDraft.mockResolvedValue({ revision: 0 });
    cancelProposal.mockResolvedValue({ proposal_id: "p-1" });
  });
  afterEach(() => {
    consoleError.mockRestore();
  });

  /** Opens the proposal page, and the class's split panel when the case selects it. */
  async function openPage(overrides: Partial<Proposal> = {}) {
    getProposal.mockResolvedValue(proposal(overrides));
    renderApp();
    expect(await screen.findByText("Proposed classes")).toBeInTheDocument();
    if (selectClass) {
      fireEvent.click(screen.getByRole("row", { name: /Customer/ }));
      const panel = await screen.findByRole("complementary", {
        name: "Split panel",
      });
      expect(within(panel).getByRole("heading")).toHaveTextContent("Customer");
    }
  }

  function expectNoRenderLoop() {
    expect(shellRenders).toBeLessThan(RENDER_LIMIT);
    expect(updateDepthWarnings()).toEqual([]);
  }

  it("renders the shell no more once the page is at rest", async () => {
    await openPage();
    const settled = shellRenders;
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(shellRenders).toBe(settled);
    expectNoRenderLoop();
  });

  it.each([
    { button: "Open in Ontology designer", target: "studio page", draft: {} },
    {
      button: "Continue in Ontology designer",
      target: "studio page",
      draft: IN_DESIGNER,
    },
    // The action of the "Editing in Ontology designer" alert.
    {
      button: "Open Ontology designer",
      target: "studio page",
      draft: IN_DESIGNER,
    },
    { button: "Back", target: "induction page", draft: {} },
    // A cancelled proposal goes back to the Induction page.
    { button: "Cancel", target: "induction page", draft: {} },
  ])("$button shows the $target", async ({ button, target, draft }) => {
    await openPage(draft);
    fireEvent.click(buttonLabelled(button));
    expect(await screen.findByText(target)).toBeInTheDocument();
    expect(screen.queryByText("Proposed classes")).toBeNull();
    expectNoRenderLoop();
  });
});
