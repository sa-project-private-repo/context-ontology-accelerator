// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
  type RenderOptions,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProposalDetailPage } from "./ProposalDetail";
import { LocaleProvider } from "../../../i18n/LocaleProvider";
import { ApiError, type ApiClient } from "@components/ApiClientProvider";
import type { Proposal } from "../../../services/ontology-engine";
import type { StudioAcceptPreview } from "../../../services/studio";
import { conflictMessage } from "../studio/studio-helpers";

// The proposal page once the Ontology designer owns the draft.

// The graph is a stub with one button that selects class B, as a click on its node does.
vi.mock("@components/graph/OntologyGraphFlow", () => ({
  OntologyGraphFlow: (p: {
    onNodeSelect?: (d: { id: string } | null) => void;
  }) => (
    <button onClick={() => p.onNodeSelect?.({ id: "http://e/B" })}>
      select B
    </button>
  ),
}));

// The caller's role: true for one that can change the ontology (a data steward).
const access = vi.hoisted(() => ({ canManage: true }));
vi.mock("@api-hooks/use-can-manage-ontology", () => ({
  useCanManageOntology: () => access.canManage,
}));

const api = { get: vi.fn(), post: vi.fn() } as unknown as ApiClient;
vi.mock("@components/ApiClientProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@components/ApiClientProvider")>()),
  useApiClient: () => api,
}));
vi.mock("@components/NamespaceProvider", () => ({
  useCurrentNamespace: () => ({ current: undefined }),
}));

const acceptProposal = vi.fn();
const getProposal = vi.fn();
const listOntologyVersions = vi.fn();
const repairProposalDatatypes = vi.fn();
const validateProposal = vi.fn();
const getProposalValidationJob = vi.fn();
vi.mock("../../../services/ontology-engine", async (importOriginal) => ({
  ...(await importOriginal<
    typeof import("../../../services/ontology-engine")
  >()),
  acceptProposal: (...a: unknown[]) => acceptProposal(...a),
  getProposal: (...a: unknown[]) => getProposal(...a),
  listOntologyVersions: (...a: unknown[]) => listOntologyVersions(...a),
  repairProposalDatatypes: (...a: unknown[]) => repairProposalDatatypes(...a),
  validateProposal: (...a: unknown[]) => validateProposal(...a),
  getProposalValidationJob: (...a: unknown[]) => getProposalValidationJob(...a),
}));
const openStudioDraft = vi.fn();
const getStudioAcceptPreview = vi.fn();
vi.mock("../../../services/studio", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../services/studio")>()),
  openStudioDraft: (...a: unknown[]) => openStudioDraft(...a),
  getStudioAcceptPreview: (...a: unknown[]) => getStudioAcceptPreview(...a),
}));

const TURTLE =
  "@prefix ex: <http://e/> . @prefix owl: <http://www.w3.org/2002/07/owl#> . ex:A a owl:Class .";
// One block per entity, as the inducer writes it: the entities panel lists these.
const CLASS_TURTLE = `@prefix ex: <http://e/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

ex:A a owl:Class .`;
// Class B carries a suggestion, which the class card offers to confirm.
const SUGGESTED =
  "@prefix ex: <http://e/> . @prefix owl: <http://www.w3.org/2002/07/owl#> . @prefix coa: <http://coa.amazon.com/vocab/coa#> . ex:A a owl:Class . ex:B a owl:Class ; coa:suggestedSubClassOf ex:A .";
const R2RML =
  "@prefix rr: <http://www.w3.org/ns/r2rml#> . <#M> a rr:TriplesMap .";
const XSD = "http://www.w3.org/2001/XMLSchema#";
const BAD_DATATYPE = [{ found: `${XSD}datetime`, canonical: `${XSD}dateTime` }];

function proposal(overrides: Partial<Proposal> = {}): Proposal {
  return {
    proposal_id: "p-1",
    job_id: "j-1",
    namespace: "ns",
    ontology_id: "ont",
    proposal_type: "induced",
    status: "updated",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ontology_turtle: TURTLE,
    r2rml_turtle: "",
    ...overrides,
  };
}

function studioAt(revision: number): NonNullable<Proposal["studio"]> {
  return {
    base_version: 2,
    revision,
    opened_at: "2026-10-19T00:00:00Z",
    opened_by: "alice",
  };
}

const IN_STUDIO = proposal({ studio: studioAt(3) });

const CUSTOMER = "http://e/o#Customer";
const NO_CHANGES = { added: [], removed: [], changed: [] };

/** The accept preview of a draft that deletes Customer, which a governed metric uses. */
function acceptPreview(
  overrides: Partial<StudioAcceptPreview> = {},
): StudioAcceptPreview {
  return {
    revision: 3,
    baseVersion: 2,
    headVersion: 2,
    rebaseRequired: false,
    diff: {
      classes: { ...NO_CHANGES, removed: [CUSTOMER] },
      objectProperties: NO_CHANGES,
      datatypeProperties: NO_CHANGES,
    },
    renamed: [
      {
        fromIri: "http://e/o#Order",
        toIri: "http://e/o#Sale",
        kind: "renamed",
      },
    ],
    breakingMetrics: [
      { metricId: "m1", name: "Customers", missingClassIri: CUSTOMER },
    ],
    breakingMetricsError: null,
    ...overrides,
  };
}

function preview(changes: typeof BAD_DATATYPE | [] = []) {
  return {
    proposal_id: "p-1",
    repaired_count: changes.length,
    changes,
    dry_run: true,
  };
}

/** A finished validation that found one malformed datatype, with its Repair button. */
const VALIDATED = {
  job_id: "v-1",
  proposal_id: "p-1",
  status: "completed" as const,
  created_at: "2026-10-19T00:00:00Z",
  report: {
    job_id: "v-1",
    proposal_id: "p-1",
    passed: false,
    tiers_run: ["tier1_blocking"],
    created_at: "2026-10-19T00:00:00Z",
    findings: [
      {
        validator: "datatypes",
        tier: "tier1_blocking",
        severity: "error" as const,
        code: "MALFORMED_DATATYPE_TOKENS",
        message: "A datatype is spelled in a non-standard way",
        details: {
          offenders: [
            {
              subject: "http://e/amount",
              predicate: "http://www.w3.org/2000/01/rdf-schema#range",
              ...BAD_DATATYPE[0],
            },
          ],
        },
      },
    ],
  },
};

function apiError(status: number, code: string, message: string, extra = {}) {
  return new ApiError(status, code, message, {
    detail: { code, message, ...extra },
  });
}

function renderPage(wrapper?: RenderOptions["wrapper"]) {
  return render(
    <MemoryRouter initialEntries={["/namespaces/ns/proposals/p-1"]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/proposals/:proposalId"
          element={<ProposalDetailPage />}
        />
        <Route
          path="/namespaces/:namespaceId/ontology/proposals/:proposalId/studio"
          element={<div>studio page</div>}
        />
      </Routes>
    </MemoryRouter>,
    { wrapper },
  );
}

function tick(ms = 2000) {
  return act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/**
 * The button whose label starts with `text`. Cloudscape appends a disabled reason to the
 * button's own text, and "Save changes" is also bolded in the unsaved-edits notice.
 */
function buttonLabelled(text: string): HTMLButtonElement {
  const button = screen
    .getAllByRole("button")
    .find((b) => b.textContent?.startsWith(text));
  if (!button) throw new Error(`no button labelled ${text}`);
  return button as HTMLButtonElement;
}

function isDisabled(button: HTMLElement): boolean {
  return (
    (button as HTMLButtonElement).disabled ||
    button.getAttribute("aria-disabled") === "true"
  );
}

/** Validate (the header's button, not the constraint panel's), then repair what it found. */
async function applyRepairFromValidation() {
  await screen.findByText("Proposed classes");
  fireEvent.click(screen.getAllByRole("button", { name: "Validate" })[0]);
  await tick();
  fireEvent.click(
    await screen.findByRole("button", { name: "Repair 1 datatype issue" }),
  );
  fireEvent.click(await screen.findByRole("button", { name: "Apply repair" }));
}

async function clickAccept() {
  fireEvent.click(
    await screen.findByRole("button", { name: "Accept proposal" }),
  );
}

/** The accept dialog of `revision`, once its preview is on screen. */
async function dialogFor(revision: number): Promise<HTMLElement> {
  const dialog = await screen.findByRole("dialog", {
    name: `Accept revision ${revision}`,
  });
  await within(dialog).findByText(/becomes the next version/);
  return dialog;
}

async function confirmInDialog(revision: number) {
  const dialog = await dialogFor(revision);
  fireEvent.click(within(dialog).getByRole("button", { name: "Accept" }));
}

describe("ProposalDetail with the Ontology designer", () => {
  beforeEach(() => {
    // The accept poll sleeps between reads: fake timers keep it from outliving a test.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.resetAllMocks();
    access.canManage = true;
    listOntologyVersions.mockResolvedValue({
      versions: [],
      activeVersion: 1,
      latestDeployment: null,
    });
    acceptProposal.mockResolvedValue({
      proposal_id: "p-1",
      status: "accepting",
    });
    repairProposalDatatypes.mockResolvedValue(preview());
    getStudioAcceptPreview.mockResolvedValue(acceptPreview());
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  describe("the lockout", () => {
    it("says the draft is edited in the designer and continues there without opening a draft", async () => {
      getProposal.mockResolvedValue(IN_STUDIO);
      renderPage();
      expect(
        await screen.findByText("Editing in Ontology designer"),
      ).toBeInTheDocument();
      expect(screen.getByText(/designer draft \(revision 3\)/)).toBeVisible();
      fireEvent.click(
        screen.getByRole("button", { name: "Continue in Ontology designer" }),
      );
      expect(await screen.findByText("studio page")).toBeInTheDocument();
      expect(openStudioDraft).not.toHaveBeenCalled();
    });

    it("says an accepted designer proposal published its draft, not the induction shown", async () => {
      getProposal.mockResolvedValue(
        proposal({
          status: "accepted",
          published_version: 4,
          studio: studioAt(3),
        }),
      );
      renderPage();
      expect(
        await screen.findByText("Edited in Ontology designer"),
      ).toBeInTheDocument();
      expect(
        screen.getByText(
          /published the designer draft \(revision 3\) as version v4; see Ontology › Induction › Versions\./,
        ),
      ).toBeVisible();
      expect(screen.queryByText("Editing in Ontology designer")).toBeNull();
    });

    it("locks every edit control, also against edits made before the page learned of the draft", async () => {
      // Unsaved edits made while the proposal was not open in the designer. The 3 s poll
      // of an accept in flight then brings the row the designer owns (the edits stay in
      // the form).
      getProposal
        .mockResolvedValueOnce(
          proposal({
            status: "accepting",
            ontology_turtle: CLASS_TURTLE,
            r2rml_turtle: R2RML,
          }),
        )
        .mockResolvedValue(
          proposal({
            status: "accept_failed",
            ontology_turtle: CLASS_TURTLE,
            r2rml_turtle: R2RML,
            studio: studioAt(3),
          }),
        );
      renderPage();
      expect(await screen.findByText("Proposed classes")).toBeInTheDocument();

      // R2RML and Turtle editors, both left open with a change in them.
      const [r2rmlEdit, turtleEdit] = screen.getAllByRole("button", {
        name: "Edit",
      });
      fireEvent.click(r2rmlEdit);
      fireEvent.change(screen.getByLabelText("R2RML Turtle"), {
        target: { value: `${R2RML} # edited` },
      });
      fireEvent.click(turtleEdit);
      fireEvent.change(screen.getByLabelText("Ontology Turtle"), {
        target: { value: `${CLASS_TURTLE}\n# edited` },
      });
      // The entity editor, open on class A.
      fireEvent.click(screen.getByRole("row", { name: /ex:A/ }));
      fireEvent.click(screen.getByRole("button", { name: "Edit selected" }));

      // The form is dirty and every control is live: the locks below are not vacuous.
      expect(screen.getByText(/Unsaved edits\./)).toBeInTheDocument();
      expect(isDisabled(buttonLabelled("Save changes"))).toBe(false);
      expect(
        (screen.getByLabelText("R2RML Turtle") as HTMLTextAreaElement).disabled,
      ).toBe(false);
      expect(
        (screen.getByLabelText("Ontology Turtle") as HTMLTextAreaElement)
          .disabled,
      ).toBe(false);
      const applies = screen.getAllByRole("button", { name: "Apply" });
      expect(applies).toHaveLength(2);
      expect(applies.map(isDisabled)).toEqual([false, false]);
      expect(
        (screen.getByLabelText(/^Edit Turtle for/) as HTMLTextAreaElement)
          .disabled,
      ).toBe(false);

      await tick(3000);
      expect(
        await screen.findByText("Editing in Ontology designer"),
      ).toBeInTheDocument();

      expect(isDisabled(buttonLabelled("Save changes"))).toBe(true);
      // Save is locked, so the notice must not send the user to it.
      expect(screen.queryByText(/Unsaved edits\./)).toBeNull();
      expect(
        screen.getByText(/These edits cannot be saved: this proposal is open/),
      ).toBeInTheDocument();
      // The edits are lost to nothing by continuing in the designer: no unsaved-edits guard there.
      expect(isDisabled(buttonLabelled("Continue in Ontology designer"))).toBe(
        false,
      );
      expect(
        (screen.getByLabelText("R2RML Turtle") as HTMLTextAreaElement).disabled,
      ).toBe(true);
      expect(
        (screen.getByLabelText("Ontology Turtle") as HTMLTextAreaElement)
          .disabled,
      ).toBe(true);
      expect(
        screen.getAllByRole("button", { name: "Apply" }).map(isDisabled),
      ).toEqual([true, true]);
      expect(
        (screen.getByLabelText(/^Edit Turtle for/) as HTMLTextAreaElement)
          .disabled,
      ).toBe(true);
      // The toggles (R2RML, Turtle, the entity editor) are gone.
      expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
      expect(screen.queryByRole("button", { name: "View" })).toBeNull();
      expect(
        screen.queryByRole("button", { name: "Edit selected" }),
      ).toBeNull();
    });

    it("offers the class suggestions to confirm only while the draft is not in the designer", async () => {
      getProposal.mockResolvedValue(proposal({ ontology_turtle: SUGGESTED }));
      const { unmount } = renderPage();
      fireEvent.click(await screen.findByRole("button", { name: "select B" }));
      expect(await screen.findByText("Suggested inheritance")).toBeVisible();
      unmount();

      getProposal.mockResolvedValue(
        proposal({ ontology_turtle: SUGGESTED, studio: studioAt(3) }),
      );
      renderPage();
      fireEvent.click(await screen.findByRole("button", { name: "select B" }));
      // The card is there (it names the class) and offers nothing to change.
      expect(await screen.findByText("http://e/B")).toBeVisible();
      expect(screen.queryByText("Suggested inheritance")).toBeNull();
    });
  });

  describe("Open in Ontology designer", () => {
    it("opens a reviewable proposal in the designer", async () => {
      getProposal.mockResolvedValue(proposal());
      openStudioDraft.mockResolvedValue({ revision: 0 });
      renderPage();
      fireEvent.click(
        await screen.findByRole("button", {
          name: "Open in Ontology designer",
        }),
      );
      await waitFor(() =>
        expect(openStudioDraft).toHaveBeenCalledWith(api, "ns", "p-1"),
      );
      expect(await screen.findByText("studio page")).toBeInTheDocument();
      expect(screen.queryByText("Editing in Ontology designer")).toBeNull();
    });

    it.each(["pending", "updated", "accept_failed"])(
      "is enabled for a %s proposal",
      async (status) => {
        getProposal.mockResolvedValue(proposal({ status }));
        renderPage();
        await screen.findByText("Proposed classes");
        expect(isDisabled(buttonLabelled("Open in Ontology designer"))).toBe(
          false,
        );
      },
    );

    it.each(["accepting", "embeddings_sync", "accepted", "rejected"])(
      "is disabled, with the reason, for a %s proposal that was not opened",
      async (status) => {
        getProposal.mockResolvedValue(proposal({ status }));
        renderPage();
        await screen.findByText("Proposed classes");
        expect(isDisabled(buttonLabelled("Open in Ontology designer"))).toBe(
          true,
        );
        expect(
          screen.getByText(
            "Only a pending or updated proposal, or one whose accept failed, opens in the Ontology designer.",
          ),
        ).toBeInTheDocument();
      },
    );

    it("is disabled, with the reason, while the form has unsaved edits", async () => {
      getProposal.mockResolvedValue(
        proposal({ ontology_turtle: CLASS_TURTLE }),
      );
      renderPage();
      await screen.findByText("Proposed classes");
      expect(isDisabled(buttonLabelled("Open in Ontology designer"))).toBe(
        false,
      );
      // An applied Turtle edit makes the form dirty.
      fireEvent.click(screen.getByRole("button", { name: "Edit" }));
      fireEvent.change(screen.getByLabelText("Ontology Turtle"), {
        target: { value: `${CLASS_TURTLE}\n# edited` },
      });
      fireEvent.click(screen.getByRole("button", { name: "Apply" }));
      expect(isDisabled(buttonLabelled("Save changes"))).toBe(false);
      expect(isDisabled(buttonLabelled("Open in Ontology designer"))).toBe(
        true,
      );
      expect(
        screen.getByText("Save or discard your edits first"),
      ).toBeInTheDocument();
      fireEvent.click(buttonLabelled("Open in Ontology designer"));
      expect(openStudioDraft).not.toHaveBeenCalled();
    });

    it("continues in the designer for an accepting proposal whose draft is open", async () => {
      getProposal.mockResolvedValue(
        proposal({ status: "accepting", studio: studioAt(3) }),
      );
      renderPage();
      await screen.findByText("Editing in Ontology designer");
      expect(isDisabled(buttonLabelled("Continue in Ontology designer"))).toBe(
        false,
      );
    });

    it.each(["accepted", "rejected", "cancelled"])(
      "drops the banner but still continues in the designer for a %s proposal whose draft is open",
      async (status) => {
        getProposal.mockResolvedValue(
          proposal({ status, studio: studioAt(3) }),
        );
        renderPage();
        await screen.findByText("Proposed classes");
        // The banner would say that accepting publishes the draft.
        expect(screen.queryByText("Editing in Ontology designer")).toBeNull();
        expect(screen.queryByText(/Accepting publishes/)).toBeNull();
        expect(
          isDisabled(buttonLabelled("Continue in Ontology designer")),
        ).toBe(false);
      },
    );
  });

  describe("accept", () => {
    it("checks the datatypes of the draft, shows what it publishes, then accepts the revision it shows", async () => {
      getProposal.mockResolvedValue(IN_STUDIO);
      renderPage();
      await clickAccept();
      const dialog = await dialogFor(3);
      // The review: what the draft removes and renames, and the metrics it breaks.
      expect(within(dialog).getByText("Classes removed (1)")).toBeVisible();
      expect(within(dialog).getByText("Order renamed to Sale")).toBeVisible();
      expect(
        within(dialog).getByText("Governed metrics this version breaks"),
      ).toBeVisible();
      expect(
        within(dialog).getByText(/Customers \(uses Customer\)/),
      ).toBeVisible();
      expect(getStudioAcceptPreview).toHaveBeenCalledWith(api, "ns", "p-1");
      // Nothing is published before the user confirms.
      expect(acceptProposal).not.toHaveBeenCalled();
      fireEvent.click(within(dialog).getByRole("button", { name: "Accept" }));
      await waitFor(() =>
        expect(acceptProposal).toHaveBeenCalledWith(api, "ns", "p-1", {
          deploy: true,
          expectedRevision: 3,
        }),
      );
      // The engine answers the datatype preview from the draft, so the check is the usual one.
      expect(repairProposalDatatypes).toHaveBeenCalledTimes(1);
      expect(repairProposalDatatypes).toHaveBeenCalledWith(api, "ns", "p-1", {
        dryRun: true,
      });
    });

    it("accepts nothing when the dialog is dismissed", async () => {
      getProposal.mockResolvedValue(IN_STUDIO);
      renderPage();
      await clickAccept();
      const dialog = await dialogFor(3);
      fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
      await waitFor(() =>
        expect(
          screen.queryByRole("dialog", { name: /Accept revision/ }),
        ).toBeNull(),
      );
      expect(acceptProposal).not.toHaveBeenCalled();
    });

    it("starts the dialog with the page's Deploy immediately choice", async () => {
      getProposal.mockResolvedValue(IN_STUDIO);
      renderPage();
      fireEvent.click(
        await screen.findByRole("checkbox", { name: "Deploy immediately" }),
      );
      await clickAccept();
      const dialog = await dialogFor(3);
      expect(
        within(dialog).getByRole("checkbox", { name: "Deploy immediately" }),
      ).not.toBeChecked();
      fireEvent.click(within(dialog).getByRole("button", { name: "Accept" }));
      await waitFor(() =>
        expect(acceptProposal).toHaveBeenCalledWith(api, "ns", "p-1", {
          deploy: false,
          expectedRevision: 3,
        }),
      );
    });

    it("accepts with the choice made in the dialog", async () => {
      getProposal.mockResolvedValue(IN_STUDIO);
      renderPage();
      await clickAccept();
      const dialog = await dialogFor(3);
      fireEvent.click(
        within(dialog).getByRole("checkbox", { name: "Deploy immediately" }),
      );
      fireEvent.click(within(dialog).getByRole("button", { name: "Accept" }));
      await waitFor(() =>
        expect(acceptProposal).toHaveBeenCalledWith(api, "ns", "p-1", {
          deploy: false,
          expectedRevision: 3,
        }),
      );
    });

    it("reviews the shown revision even if the datatype warning is accepted anyway", async () => {
      getProposal.mockResolvedValue(IN_STUDIO);
      repairProposalDatatypes.mockResolvedValue(preview(BAD_DATATYPE));
      renderPage();
      await clickAccept();
      fireEvent.click(
        await screen.findByRole("button", { name: "Accept anyway" }),
      );
      expect(acceptProposal).not.toHaveBeenCalled();
      await confirmInDialog(3);
      await waitFor(() =>
        expect(acceptProposal).toHaveBeenCalledWith(api, "ns", "p-1", {
          deploy: true,
          expectedRevision: 3,
        }),
      );
      expect(repairProposalDatatypes).toHaveBeenCalledTimes(1);
    });

    it("repairs the draft at the revision shown, reviews the revision the repair wrote, then accepts it", async () => {
      // The reload after the repair may still show revision 3: the dialog and the accept
      // use the revision the repair returned.
      getProposal.mockResolvedValue(IN_STUDIO);
      getStudioAcceptPreview.mockResolvedValue(acceptPreview({ revision: 4 }));
      repairProposalDatatypes
        .mockResolvedValueOnce(preview(BAD_DATATYPE))
        .mockResolvedValueOnce({
          ...preview(BAD_DATATYPE),
          dry_run: false,
          revision: 4,
        });
      renderPage();
      await clickAccept();
      fireEvent.click(
        await screen.findByRole("button", { name: "Repair & accept" }),
      );
      // The dialog is for the revision the repair wrote, and nothing is accepted yet.
      await dialogFor(4);
      expect(getStudioAcceptPreview).toHaveBeenCalledTimes(1);
      expect(repairProposalDatatypes).toHaveBeenNthCalledWith(
        2,
        api,
        "ns",
        "p-1",
        { expectedRevision: 3 },
      );
      expect(acceptProposal).not.toHaveBeenCalled();
      await confirmInDialog(4);
      await waitFor(() =>
        expect(acceptProposal).toHaveBeenCalledWith(api, "ns", "p-1", {
          deploy: true,
          expectedRevision: 4,
        }),
      );
    });

    it("blocks the accept and links to the designer when the preview finds a newer version", async () => {
      getProposal.mockResolvedValue(IN_STUDIO);
      getStudioAcceptPreview.mockResolvedValue(
        acceptPreview({ rebaseRequired: true, headVersion: 4 }),
      );
      renderPage();
      await clickAccept();
      const dialog = await dialogFor(3);
      expect(within(dialog).getByText("Rebase first")).toBeVisible();
      expect(
        isDisabled(within(dialog).getByRole("button", { name: "Accept" })),
      ).toBe(true);
      expect(acceptProposal).not.toHaveBeenCalled();
      fireEvent.click(
        within(dialog).getByRole("link", {
          name: "Open the Ontology designer to rebase it.",
        }),
      );
      expect(await screen.findByText("studio page")).toBeInTheDocument();
    });

    it("reviews the newer revision when the preview is of one", async () => {
      getProposal
        .mockResolvedValueOnce(IN_STUDIO)
        .mockResolvedValue(proposal({ studio: studioAt(5) }));
      getStudioAcceptPreview.mockResolvedValue(acceptPreview({ revision: 5 }));
      renderPage();
      await clickAccept();
      // The dialog opens for revision 3, learns of 5 from its preview and follows it.
      await confirmInDialog(5);
      await waitFor(() =>
        expect(acceptProposal).toHaveBeenCalledWith(api, "ns", "p-1", {
          deploy: true,
          expectedRevision: 5,
        }),
      );
      expect(getProposal).toHaveBeenCalledTimes(2);
    });

    it("reloads the draft when another change landed first", async () => {
      getProposal
        .mockResolvedValueOnce(IN_STUDIO)
        .mockResolvedValue(proposal({ studio: studioAt(5) }));
      getStudioAcceptPreview
        .mockResolvedValueOnce(acceptPreview())
        .mockResolvedValue(acceptPreview({ revision: 5 }));
      acceptProposal.mockRejectedValueOnce(
        apiError(409, "DraftConflict", "moved", { revision: 5 }),
      );
      renderPage();
      await clickAccept();
      await confirmInDialog(3);
      expect(await screen.findByText(conflictMessage())).toBeInTheDocument();
      // The page shows the reloaded revision, and the next accept reviews and sends it.
      expect(
        await screen.findByText(/designer draft \(revision 5\)/),
      ).toBeVisible();
      await clickAccept();
      await confirmInDialog(5);
      await waitFor(() => expect(acceptProposal).toHaveBeenCalledTimes(2));
      expect(acceptProposal).toHaveBeenLastCalledWith(api, "ns", "p-1", {
        deploy: true,
        expectedRevision: 5,
      });
    });

    it("sends the user to the designer to rebase when a newer version was published", async () => {
      getProposal.mockResolvedValue(IN_STUDIO);
      acceptProposal.mockRejectedValueOnce(
        apiError(
          409,
          "RebaseRequired",
          "The draft was built on v1, but the newest version is v2; rebase it first.",
          { baseVersion: 1, headVersion: 2 },
        ),
      );
      renderPage();
      await clickAccept();
      await confirmInDialog(3);
      expect(await screen.findByText("Rebase required")).toBeInTheDocument();
      expect(getProposal).toHaveBeenCalledTimes(1);
      fireEvent.click(screen.getByRole("link", { name: "Ontology designer" }));
      expect(await screen.findByText("studio page")).toBeInTheDocument();
    });

    it("reloads a page that did not know the proposal is open in the designer", async () => {
      getProposal
        .mockResolvedValueOnce(proposal())
        .mockResolvedValue(IN_STUDIO);
      acceptProposal.mockRejectedValueOnce(
        apiError(
          422,
          "ExpectedRevisionRequired",
          "This proposal is open in the Ontology designer; accept it with the expectedRevision you reviewed.",
        ),
      );
      renderPage();
      await clickAccept();
      expect(
        await screen.findByText(
          "This proposal is open in the Ontology designer. The page was reloaded; review the draft and accept again.",
        ),
      ).toBeInTheDocument();
      expect(
        await screen.findByText("Editing in Ontology designer"),
      ).toBeInTheDocument();
    });

    describe("on a page opened before another tab accepted the proposal", () => {
      const ACCEPTED_ELSEWHERE = proposal({
        status: "accepted",
        published_version: 4,
        studio: studioAt(5),
      });

      /** The reloaded page shows the proposal accepted, and no failure. */
      async function expectShownAccepted() {
        expect(
          await screen.findByText("Edited in Ontology designer"),
        ).toBeInTheDocument();
        await tick();
        expect(buttonLabelled("Already accepted")).toBeTruthy();
        expect(screen.queryByText("Failed")).toBeNull();
        expect(screen.queryByText(/accept again|try again/)).toBeNull();
        expect(acceptProposal).toHaveBeenCalledTimes(1);
      }

      it("shows it accepted when it did not know the draft was open", async () => {
        getProposal
          .mockResolvedValueOnce(proposal())
          .mockResolvedValue(ACCEPTED_ELSEWHERE);
        acceptProposal.mockRejectedValueOnce(
          apiError(
            422,
            "ExpectedRevisionRequired",
            "This proposal is open in the Ontology designer; accept it with the expectedRevision you reviewed.",
          ),
        );
        renderPage();
        await clickAccept();
        await expectShownAccepted();
      });

      it("shows it accepted when it reviewed an earlier revision", async () => {
        getProposal
          .mockResolvedValueOnce(IN_STUDIO)
          .mockResolvedValue(ACCEPTED_ELSEWHERE);
        acceptProposal.mockRejectedValueOnce(
          apiError(409, "DraftConflict", "moved", { revision: 5 }),
        );
        renderPage();
        await clickAccept();
        await confirmInDialog(3);
        await expectShownAccepted();
      });
    });

    it("reloads the draft when the repair finds another change landed first", async () => {
      getProposal
        .mockResolvedValueOnce(IN_STUDIO)
        .mockResolvedValue(proposal({ studio: studioAt(5) }));
      repairProposalDatatypes
        .mockResolvedValueOnce(preview(BAD_DATATYPE))
        .mockRejectedValueOnce(
          apiError(409, "DraftConflict", "moved", { revision: 5 }),
        );
      renderPage();
      await clickAccept();
      fireEvent.click(
        await screen.findByRole("button", { name: "Repair & accept" }),
      );
      expect(await screen.findByText(conflictMessage())).toBeInTheDocument();
      expect(
        await screen.findByText(/designer draft \(revision 5\)/),
      ).toBeVisible();
      expect(acceptProposal).not.toHaveBeenCalled();
    });

    it("repairs the draft from the validation panel at the revision shown", async () => {
      getProposal.mockResolvedValue(IN_STUDIO);
      validateProposal.mockResolvedValue(VALIDATED);
      getProposalValidationJob.mockResolvedValue(VALIDATED);
      repairProposalDatatypes.mockResolvedValue({
        ...preview(BAD_DATATYPE),
        dry_run: false,
        revision: 4,
      });
      renderPage();
      await applyRepairFromValidation();
      await waitFor(() =>
        expect(repairProposalDatatypes).toHaveBeenCalledWith(api, "ns", "p-1", {
          expectedRevision: 3,
        }),
      );
    });

    it("reloads the draft when the validation panel's repair finds another change landed first", async () => {
      getProposal
        .mockResolvedValueOnce(IN_STUDIO)
        .mockResolvedValue(proposal({ studio: studioAt(5) }));
      validateProposal.mockResolvedValue(VALIDATED);
      getProposalValidationJob.mockResolvedValue(VALIDATED);
      repairProposalDatatypes.mockRejectedValue(
        apiError(409, "DraftConflict", "moved", { revision: 5 }),
      );
      renderPage();
      await applyRepairFromValidation();
      expect(
        await screen.findByText(/designer draft \(revision 5\)/),
      ).toBeVisible();
      expect(
        screen.getByText("Datatype repair failed: moved"),
      ).toBeInTheDocument();
    });

    it("points to the designer when the publish found a newer version", async () => {
      getProposal.mockResolvedValueOnce(IN_STUDIO).mockResolvedValue(
        proposal({
          status: "accept_failed",
          studio: studioAt(3),
          accept_error:
            "The draft was built on v2; rebase the draft in the Ontology designer and accept again.",
          accept_progress: {
            step: "publish",
            state: "failed",
            code: "RebaseRequired",
          },
        }),
      );
      renderPage();
      await clickAccept();
      await confirmInDialog(3);
      await tick();
      expect(await screen.findByText("Rebase required")).toBeInTheDocument();
      expect(
        screen.getByRole("link", { name: "Ontology designer" }),
      ).toBeInTheDocument();
    });

    it("writes the Japanese Rebase required alert as one sentence around the link", async () => {
      window.localStorage.setItem("scl.locale", "ja");
      try {
        getProposal.mockResolvedValue(
          proposal({
            status: "accept_failed",
            studio: studioAt(3),
            accept_progress: {
              step: "publish",
              state: "failed",
              code: "RebaseRequired",
            },
          }),
        );
        renderPage(LocaleProvider);
        const link = await screen.findByRole("link", {
          name: "オントロジーデザイナー",
        });
        // The link is katakana, so no space separates it from its particle.
        expect(link.parentElement?.textContent).toBe(
          "このドラフトの作成後に新しいオントロジーバージョンが公開されました。オントロジーデザイナーでドラフトをリベースし、結果を確認してから再度承認してください。",
        );
      } finally {
        window.localStorage.clear();
        document.documentElement.lang = "";
      }
    });

    it("reads the failure code only while the accept is failed", async () => {
      // After a rebase the row keeps the old code until the next accept.
      getProposal.mockResolvedValue(
        proposal({
          status: "updated",
          studio: studioAt(4),
          accept_progress: {
            step: "publish",
            state: "failed",
            code: "RebaseRequired",
          },
        }),
      );
      renderPage();
      await screen.findByText("Editing in Ontology designer");
      expect(screen.queryByText("Rebase required")).toBeNull();
    });

    it("keeps the accept of a proposal that was never opened in the designer as it was", async () => {
      getProposal.mockResolvedValueOnce(proposal({ status: "pending" }));
      getProposal.mockResolvedValue(
        proposal({ status: "accepted", published_version: 3 }),
      );
      repairProposalDatatypes
        .mockResolvedValueOnce(preview(BAD_DATATYPE))
        .mockResolvedValueOnce({ ...preview(BAD_DATATYPE), dry_run: false });
      renderPage();
      await clickAccept();
      fireEvent.click(
        await screen.findByRole("button", { name: "Repair & accept" }),
      );
      await waitFor(() => expect(acceptProposal).toHaveBeenCalled());
      expect(repairProposalDatatypes).toHaveBeenNthCalledWith(
        2,
        api,
        "ns",
        "p-1",
      );
      expect(acceptProposal).toHaveBeenCalledWith(api, "ns", "p-1", {
        deploy: true,
      });
      expect(screen.queryByText("Editing in Ontology designer")).toBeNull();
      expect(
        screen.queryByRole("dialog", { name: /Accept revision/ }),
      ).toBeNull();
      expect(getStudioAcceptPreview).not.toHaveBeenCalled();
    });

    it("accepts a proposal that was never opened in the designer without the designer dialog", async () => {
      getProposal.mockResolvedValueOnce(proposal({ status: "pending" }));
      getProposal.mockResolvedValue(
        proposal({ status: "accepted", published_version: 3 }),
      );
      renderPage();
      await clickAccept();
      await waitFor(() =>
        expect(acceptProposal).toHaveBeenCalledWith(api, "ns", "p-1", {
          deploy: true,
        }),
      );
      expect(
        screen.queryByRole("dialog", { name: /Accept revision/ }),
      ).toBeNull();
      expect(getStudioAcceptPreview).not.toHaveBeenCalled();
    });
  });

  describe("by role", () => {
    const READ_ONLY =
      "Your role on this namespace can read this proposal, but not change, validate, cancel or accept it.";
    const DENIED = "Your role on this namespace does not allow this.";
    // What a structured induction stores: database constraints, enabled, so the
    // constraint panel's Validate is on unless the role turns it off.
    const CONSTRAINTS = {
      constraint_config: {
        classes: [
          {
            class_uri: "http://e/A",
            class_name: "A",
            constraints: [
              {
                property_path: "http://e/a_id",
                property_name: "a_id",
                constraint_type: "required",
                enabled: true,
                params: {},
                source: "db_constraint",
                description: "a_id is NOT NULL",
              },
            ],
          },
        ],
      },
    };

    it("offers a role that only reads no change, and says why", async () => {
      access.canManage = false;
      getProposal.mockResolvedValue(
        proposal({ status: "pending", metadata: CONSTRAINTS }),
      );
      renderPage();
      expect(await screen.findByText(READ_ONLY)).toBeInTheDocument();
      // The Turtle source is shown, not offered for editing.
      expect(screen.getByText("Ontology Source (Turtle)")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
      expect(
        screen.getByRole("checkbox", { name: "Deploy immediately" }),
      ).toBeDisabled();
      // The header's Validate and the constraint panel's (a compile-constraints request).
      const validates = screen.getAllByRole("button", { name: "Validate" });
      expect(validates).toHaveLength(2);
      for (const button of [
        ...validates,
        buttonLabelled("Accept proposal"),
        buttonLabelled("Save changes"),
        ...["Cancel", "Open in Ontology designer", "Infer constraints"].map(
          (name) => screen.getByRole("button", { name }),
        ),
      ]) {
        expect(isDisabled(button)).toBe(true);
        expect(button).toHaveAccessibleDescription(DENIED);
      }
      fireEvent.click(validates[1]);
      fireEvent.click(
        screen.getByRole("button", { name: "Open in Ontology designer" }),
      );
      expect(openStudioDraft).not.toHaveBeenCalled();
      expect(api.post).not.toHaveBeenCalled();
      expect(screen.queryByText("Could not load proposal")).toBeNull();
    });

    it("still lets a role that only reads continue to an open designer draft", async () => {
      access.canManage = false;
      getProposal.mockResolvedValue(IN_STUDIO);
      renderPage();
      const next = await screen.findByRole("button", {
        name: "Continue in Ontology designer",
      });
      expect(isDisabled(next)).toBe(false);
      expect(isDisabled(buttonLabelled("Accept proposal"))).toBe(true);
      fireEvent.click(next);
      expect(await screen.findByText("studio page")).toBeInTheDocument();
    });

    it("offers a role that can change the ontology every action", async () => {
      getProposal.mockResolvedValue(
        proposal({ status: "pending", metadata: CONSTRAINTS }),
      );
      renderPage();
      await screen.findByText("Proposed classes");
      expect(screen.queryByText(READ_ONLY)).toBeNull();
      expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
      for (const name of [
        "Cancel",
        "Open in Ontology designer",
        "Accept proposal",
        "Infer constraints",
      ]) {
        expect(isDisabled(screen.getByRole("button", { name }))).toBe(false);
      }
      for (const validate of screen.getAllByRole("button", {
        name: "Validate",
      })) {
        expect(isDisabled(validate)).toBe(false);
      }
    });
  });
});
