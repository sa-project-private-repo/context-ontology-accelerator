// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  act,
} from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ProposalDetailPage, ProposalDetailRoute } from "./ProposalDetail";
import type { ApiClient } from "@components/ApiClientProvider";
import type { Proposal } from "../../../services/ontology-engine";

// The Deploy-immediately checkbox, the post-accept deployment note and the
// auto-deploy-held banner on the proposal accept screen. Kept apart from
// ProposalDetail.test.tsx, which tests the module's pure helpers.

// The graph canvas pulls in d3-force (via layout.ts), which is irrelevant here.
vi.mock("@components/graph/OntologyGraphFlow", () => ({
  OntologyGraphFlow: () => null,
}));

const api = { get: vi.fn(), post: vi.fn() } as unknown as ApiClient;
vi.mock("@components/ApiClientProvider", () => ({
  useApiClient: () => api,
}));
vi.mock("@components/NamespaceProvider", () => ({
  useCurrentNamespace: () => ({ current: undefined }),
}));

const acceptProposal = vi.fn();
const getProposal = vi.fn();
const listOntologyVersions = vi.fn();
const repairProposalDatatypes = vi.fn();
const fetchJsonFromUrl = vi.fn();
vi.mock("../../../services/ontology-engine", async (importOriginal) => ({
  ...(await importOriginal<
    typeof import("../../../services/ontology-engine")
  >()),
  acceptProposal: (...a: unknown[]) => acceptProposal(...a),
  getProposal: (...a: unknown[]) => getProposal(...a),
  listOntologyVersions: (...a: unknown[]) => listOntologyVersions(...a),
  repairProposalDatatypes: (...a: unknown[]) => repairProposalDatatypes(...a),
  fetchJsonFromUrl: (...a: unknown[]) => fetchJsonFromUrl(...a),
}));

const TURTLE =
  "@prefix ex: <http://e/> . @prefix owl: <http://www.w3.org/2002/07/owl#> . ex:A a owl:Class .";

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

const PENDING = proposal();
const ACCEPTED = proposal({ status: "accepted", published_version: 3 });
const STARTED = { ...ACCEPTED, auto_deploy_id: "d-1" };
const HELD_METRICS = {
  reason: "BREAKING_METRICS" as const,
  metrics: [
    { metricId: "revenue", name: "revenue", missingClassIri: "http://e/Order" },
  ],
};
const HELD = { ...ACCEPTED, auto_deploy_held: HELD_METRICS };
const HELD_IN_PROGRESS = {
  ...ACCEPTED,
  auto_deploy_held: { reason: "DEPLOY_IN_PROGRESS" as const },
};

/** Successive getProposal results; the last repeats. An Error rejects. */
function queueReads(...reads: Array<Proposal | Error>) {
  const next = () => {
    const r = reads.length > 1 ? reads.shift()! : reads[0];
    return r instanceof Error ? Promise.reject(r) : Promise.resolve(r);
  };
  getProposal.mockImplementation(next);
}

function renderPage(
  extra?: React.ReactNode,
  page: React.ReactElement = <ProposalDetailPage />,
) {
  return render(
    <MemoryRouter initialEntries={["/namespaces/ns/proposals/p-1"]}>
      {extra}
      <Routes>
        <Route
          path="/namespaces/:namespaceId/proposals/:proposalId"
          element={page}
        />
      </Routes>
    </MemoryRouter>,
  );
}

function tick(ms = 2000) {
  return act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** Click Accept, then run the accept poll that sees the "accepted" status. */
async function clickAccept() {
  fireEvent.click(
    await screen.findByRole("button", { name: "Accept proposal" }),
  );
  await waitFor(() => expect(acceptProposal).toHaveBeenCalled());
  await tick();
}

const NOT_DEPLOYED = /was published but not deployed/;

/** Match text as rendered, line breaks included (the accept result keeps the deployment note on its own line). */
const KEEP_LINE_BREAKS = { normalizer: (text: string) => text.trim() };

function screenTextExact(text: string): HTMLElement {
  return screen.getByText(text, KEEP_LINE_BREAKS);
}

describe("ProposalDetail: accept with Deploy immediately", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.resetAllMocks();
    acceptProposal.mockResolvedValue({
      proposal_id: "p-1",
      status: "accepting",
    });
    listOntologyVersions.mockResolvedValue({
      versions: [],
      activeVersion: 1,
      latestDeployment: null,
    });
    repairProposalDatatypes.mockResolvedValue({
      repaired_count: 0,
      changes: [],
    });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("is checked by default, sends deploy: true, and skips the wait when the outcome is already there", async () => {
    queueReads(PENDING, STARTED);
    renderPage();

    expect(
      await screen.findByRole("checkbox", { name: "Deploy immediately" }),
    ).toBeChecked();
    await clickAccept();

    expect(acceptProposal).toHaveBeenCalledWith(api, "ns", "p-1", {
      deploy: true,
    });
    expect(
      screenTextExact(
        "Published as version v3.\nDeployment started. Follow it in Ontology › Induction › Versions.",
      ),
    ).toBeInTheDocument();
    // Initial load + the accept poll; no follow-up reads.
    await tick(10_000);
    expect(getProposal).toHaveBeenCalledTimes(2);
    expect(listOntologyVersions).not.toHaveBeenCalled();
    expect(screen.queryByText(NOT_DEPLOYED)).not.toBeInTheDocument();
  });

  it("sends deploy: false when unchecked, points to the Versions tab, and does not wait for an outcome", async () => {
    queueReads(PENDING, ACCEPTED);
    renderPage();

    fireEvent.click(
      await screen.findByRole("checkbox", { name: "Deploy immediately" }),
    );
    await clickAccept();

    expect(acceptProposal).toHaveBeenCalledWith(api, "ns", "p-1", {
      deploy: false,
    });
    expect(
      screenTextExact(
        "Published as version v3.\nDeploy it from Ontology › Induction › Versions.",
      ),
    ).toBeInTheDocument();
    await tick(10_000);
    expect(getProposal).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(NOT_DEPLOYED)).not.toBeInTheDocument();
  });

  it("keeps following up past two polls until the deployment starts", async () => {
    queueReads(PENDING, ACCEPTED, ACCEPTED, ACCEPTED, ACCEPTED, STARTED);
    renderPage();
    await clickAccept();

    const waiting =
      "Published as version v3.\nWaiting for the deployment to start…";
    expect(screenTextExact(waiting)).toBeInTheDocument();
    // The "Publishing a new ontology version" progress text is not shown
    // while the outcome is still being waited for.
    expect(
      screen.queryByText(/Publishing a new ontology version/),
    ).not.toBeInTheDocument();
    for (let i = 0; i < 3; i++) {
      await tick();
      expect(screenTextExact(waiting)).toBeInTheDocument();
    }
    await tick();

    expect(
      screenTextExact(
        "Published as version v3.\nDeployment started. Follow it in Ontology › Induction › Versions.",
      ),
    ).toBeInTheDocument();
    expect(getProposal).toHaveBeenCalledTimes(6);
    await tick(10_000);
    expect(getProposal).toHaveBeenCalledTimes(6);
  });

  it("shows the held banner when the hold is recorded only after several polls", async () => {
    queueReads(PENDING, ACCEPTED, ACCEPTED, ACCEPTED, HELD);
    renderPage();
    await clickAccept();
    await tick(6000);

    expect(
      await screen.findByText("Version v3 was published but not deployed"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Deploying would break governed metrics: revenue. Review the impact and deploy from Ontology › Induction › Versions.",
      ),
    ).toBeInTheDocument();
    // A held deploy gets no deployment note; the banner explains it.
    expect(screen.getByText("Published as version v3.")).toBeInTheDocument();
    expect(listOntologyVersions).toHaveBeenCalledTimes(1);
    expect(listOntologyVersions).toHaveBeenCalledWith(api, "ns");
  });

  it("keeps polling through a failed read", async () => {
    queueReads(PENDING, ACCEPTED, new Error("503"), STARTED);
    renderPage();
    await clickAccept();
    await tick(4000);

    expect(
      screenTextExact(
        "Published as version v3.\nDeployment started. Follow it in Ontology › Induction › Versions.",
      ),
    ).toBeInTheDocument();
  });

  it("stops after about 30 seconds and says the deployment was requested", async () => {
    queueReads(PENDING, ACCEPTED);
    renderPage();
    await clickAccept();
    const requested =
      "Published as version v3.\nDeployment requested. Check Ontology › Induction › Versions for its status.";

    await tick(26_000);
    expect(
      screen.queryByText(requested, KEEP_LINE_BREAKS),
    ).not.toBeInTheDocument();
    await tick(6_000);
    expect(screenTextExact(requested)).toBeInTheDocument();

    const reads = getProposal.mock.calls.length;
    // One read per poll interval for the 30 seconds, plus load and accept polls.
    expect(reads).toBeGreaterThanOrEqual(12);
    expect(reads).toBeLessThanOrEqual(18);
    await tick(10_000);
    expect(getProposal).toHaveBeenCalledTimes(reads);
  });

  it("still reports a failed accept with its error and does not wait for an outcome", async () => {
    queueReads(
      PENDING,
      proposal({
        status: "accept_failed",
        accept_error: "Step 's3_persist' failed: boom",
      }),
    );
    renderPage();
    await clickAccept();

    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(
      screen.getByText("Step 's3_persist' failed: boom"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Accept proposal" }),
    ).not.toHaveAttribute("aria-disabled", "true");
    expect(
      screen.getByRole("checkbox", { name: "Deploy immediately" }),
    ).toBeEnabled();
    await tick(10_000);
    expect(getProposal).toHaveBeenCalledTimes(2);
  });
});

describe("ProposalDetailRoute", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.resetAllMocks();
    acceptProposal.mockResolvedValue({
      proposal_id: "p-1",
      status: "accepting",
    });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts fresh when the user navigates to another proposal", async () => {
    let p1Reads = 0;
    getProposal.mockImplementation((_api, _ns, id: string) => {
      if (id === "p-2")
        return Promise.resolve({ ...PENDING, proposal_id: "p-2" });
      p1Reads += 1;
      return Promise.resolve(p1Reads === 1 ? PENDING : STARTED);
    });
    renderPage(
      <Link to="/namespaces/ns/proposals/p-2">next proposal</Link>,
      <ProposalDetailRoute />,
    );
    await clickAccept();
    expect(screen.getByText(/Published as version v3/)).toBeInTheDocument();

    fireEvent.click(screen.getByText("next proposal"));

    await screen.findByRole("button", { name: "Accept proposal" });
    expect(screen.queryByText(/Published as version/)).not.toBeInTheDocument();
  });
});

describe("ProposalDetail: Deploy immediately checkbox", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.resetAllMocks();
    acceptProposal.mockResolvedValue({
      proposal_id: "p-1",
      status: "accepting",
    });
    listOntologyVersions.mockResolvedValue({
      versions: [],
      activeVersion: 1,
      latestDeployment: null,
    });
    repairProposalDatatypes.mockResolvedValue({
      repaired_count: 0,
      changes: [],
    });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("is not offered once the proposal is accepted", async () => {
    queueReads(STARTED);
    renderPage();

    await screen.findByRole("button", { name: "Already accepted" });
    expect(
      screen.queryByRole("checkbox", { name: "Deploy immediately" }),
    ).not.toBeInTheDocument();
  });

  it("says in the Accept hint where to deploy the version later", async () => {
    queueReads(PENDING);
    renderPage();

    fireEvent.mouseEnter(
      await screen.findByRole("button", { name: "Accept proposal" }),
    );
    expect(
      await screen.findByText(
        "Publishes this draft as the namespace's next ontology version. With “Deploy immediately” it also becomes the served ontology; otherwise deploy it later from Ontology › Induction › Versions.",
      ),
    ).toBeInTheDocument();
  });

  it("is disabled for other read-only statuses", async () => {
    queueReads(proposal({ status: "rejected" }));
    renderPage();

    expect(
      await screen.findByRole("checkbox", { name: "Deploy immediately" }),
    ).toBeDisabled();
  });

  it("is disabled while the accept check runs, so the sent value is the visible one", async () => {
    let finishCheck!: (v: unknown) => void;
    repairProposalDatatypes.mockReturnValue(
      new Promise((resolve) => {
        finishCheck = resolve;
      }),
    );
    queueReads(PENDING, STARTED);
    renderPage();

    const checkbox = await screen.findByRole("checkbox", {
      name: "Deploy immediately",
    });
    fireEvent.click(checkbox); // unchecked
    fireEvent.click(screen.getByRole("button", { name: "Accept proposal" }));
    await waitFor(() => expect(checkbox).toBeDisabled());
    expect(acceptProposal).not.toHaveBeenCalled();

    await act(async () => {
      finishCheck({ repaired_count: 0, changes: [] });
    });
    await waitFor(() => expect(acceptProposal).toHaveBeenCalled());
    expect(acceptProposal).toHaveBeenCalledWith(api, "ns", "p-1", {
      deploy: false,
    });
    expect(checkbox).not.toBeChecked();
  });

  it("is disabled while repairing and Repair & accept sends the visible value", async () => {
    let finishRepair!: (v: unknown) => void;
    repairProposalDatatypes.mockImplementation(
      (
        _api: unknown,
        _ns: unknown,
        _id: unknown,
        opts?: { dryRun?: boolean },
      ) =>
        opts?.dryRun
          ? Promise.resolve({
              repaired_count: 1,
              changes: [{ found: "xsd:strng", canonical: "xsd:string" }],
            })
          : new Promise((resolve) => {
              finishRepair = resolve;
            }),
    );
    queueReads(PENDING, PENDING, STARTED);
    renderPage();

    const checkbox = await screen.findByRole("checkbox", {
      name: "Deploy immediately",
    });
    fireEvent.click(checkbox); // unchecked
    fireEvent.click(screen.getByRole("button", { name: "Accept proposal" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Repair & accept" }),
    );
    await waitFor(() => expect(checkbox).toBeDisabled());

    await act(async () => {
      finishRepair({ repaired_count: 1, changes: [] });
    });
    await waitFor(() => expect(acceptProposal).toHaveBeenCalled());
    expect(acceptProposal).toHaveBeenCalledWith(api, "ns", "p-1", {
      deploy: false,
    });
    expect(checkbox).not.toBeChecked();
  });
});

describe("ProposalDetail: Repair & accept ordering", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.resetAllMocks();
    acceptProposal.mockResolvedValue({
      proposal_id: "p-1",
      status: "accepting",
    });
    listOntologyVersions.mockResolvedValue({
      versions: [],
      activeVersion: 1,
      latestDeployment: null,
    });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("waits for the reload to finish before accepting", async () => {
    repairProposalDatatypes.mockImplementation(
      (
        _api: unknown,
        _ns: unknown,
        _id: unknown,
        opts?: { dryRun?: boolean },
      ) =>
        Promise.resolve(
          opts?.dryRun
            ? {
                repaired_count: 1,
                changes: [{ found: "xsd:strng", canonical: "xsd:string" }],
              }
            : { repaired_count: 1, changes: [] },
        ),
    );
    let finishReload!: (p: Proposal) => void;
    let reads = 0;
    getProposal.mockImplementation(() => {
      reads += 1;
      if (reads === 2)
        // The reload that follows the repair.
        return new Promise<Proposal>((resolve) => {
          finishReload = resolve;
        });
      return Promise.resolve(reads === 1 ? PENDING : STARTED);
    });
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "Accept proposal" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Repair & accept" }),
    );
    await waitFor(() => expect(getProposal).toHaveBeenCalledTimes(2));
    await tick(5000);
    expect(acceptProposal).not.toHaveBeenCalled();

    await act(async () => {
      finishReload(PENDING);
    });
    await waitFor(() => expect(acceptProposal).toHaveBeenCalled());
  });
});

describe("ProposalDetail: auto-deploy follow-up and held banner on load", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.resetAllMocks();
    listOntologyVersions.mockResolvedValue({
      versions: [],
      activeVersion: 1,
      latestDeployment: null,
    });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("follows up on an accepted proposal with no outcome yet and shows the banner when the hold arrives", async () => {
    queueReads(ACCEPTED, ACCEPTED, HELD_IN_PROGRESS);
    renderPage();

    await screen.findByRole("button", { name: "Already accepted" });
    expect(screen.queryByText(NOT_DEPLOYED)).not.toBeInTheDocument();
    await tick();
    expect(screen.queryByText(NOT_DEPLOYED)).not.toBeInTheDocument();
    await tick();

    expect(
      await screen.findByText("Version v3 was published but not deployed"),
    ).toBeInTheDocument();
    expect(acceptProposal).not.toHaveBeenCalled();
    expect(listOntologyVersions).toHaveBeenCalledTimes(1);
    await tick(10_000);
    expect(getProposal).toHaveBeenCalledTimes(3);
  });

  it("gives up quietly after about 30 seconds", async () => {
    queueReads(ACCEPTED);
    renderPage();

    await screen.findByRole("button", { name: "Already accepted" });
    await tick(32_000);
    const reads = getProposal.mock.calls.length;
    expect(reads).toBeLessThanOrEqual(18);
    await tick(10_000);
    expect(getProposal).toHaveBeenCalledTimes(reads);
    expect(screen.queryByText(NOT_DEPLOYED)).not.toBeInTheDocument();
  });

  it("does not follow up when the outcome is already recorded", async () => {
    queueReads(STARTED);
    renderPage();

    await screen.findByRole("button", { name: "Already accepted" });
    await tick(10_000);
    expect(getProposal).toHaveBeenCalledTimes(1);
  });

  it("does not follow up for a proposal without a published version", async () => {
    queueReads(proposal({ status: "accepted" }));
    renderPage();

    await screen.findByRole("button", { name: "Already accepted" });
    await tick(10_000);
    expect(getProposal).toHaveBeenCalledTimes(1);
  });

  it("shows the held banner on load and looks up the active version once", async () => {
    queueReads(
      proposal({
        status: "accepted",
        published_version: 3,
        auto_deploy_held: {
          reason: "DEPLOY_FAILED_TO_START",
          error: "lock timeout",
        },
      }),
    );
    renderPage();

    expect(
      await screen.findByText("Version v3 was published but not deployed"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "The deployment could not start: lock timeout. Retry from Ontology › Induction › Versions.",
      ),
    ).toBeInTheDocument();
    await tick(10_000);
    expect(listOntologyVersions).toHaveBeenCalledTimes(1);
  });

  it("keeps the held banner when the active version cannot be looked up", async () => {
    listOntologyVersions.mockRejectedValue(new Error("boom"));
    queueReads(HELD_IN_PROGRESS);
    renderPage();

    expect(
      await screen.findByText("Version v3 was published but not deployed"),
    ).toBeInTheDocument();
  });

  it.each([
    ["the active version", 3],
    ["a newer version", 5],
  ])("hides the held banner when %s is already deployed", async (_, active) => {
    listOntologyVersions.mockResolvedValue({
      versions: [],
      activeVersion: active,
      latestDeployment: null,
    });
    queueReads(HELD_IN_PROGRESS);
    renderPage();

    await waitFor(() => expect(listOntologyVersions).toHaveBeenCalled());
    await tick(0);
    expect(screen.queryByText(NOT_DEPLOYED)).not.toBeInTheDocument();
  });

  it.each([
    ["an older version", 2],
    ["no version", null],
  ])("shows the held banner when %s is active", async (_, active) => {
    listOntologyVersions.mockResolvedValue({
      versions: [],
      activeVersion: active,
      latestDeployment: null,
    });
    queueReads(HELD_IN_PROGRESS);
    renderPage();

    expect(
      await screen.findByText("Version v3 was published but not deployed"),
    ).toBeInTheDocument();
  });

  it("picks up auto_deploy_held when Refresh brings it in", async () => {
    queueReads(STARTED, HELD_IN_PROGRESS);
    renderPage();

    await screen.findByRole("button", { name: "Already accepted" });
    expect(listOntologyVersions).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect(
      await screen.findByText("Version v3 was published but not deployed"),
    ).toBeInTheDocument();
    expect(listOntologyVersions).toHaveBeenCalledTimes(1);
  });

  it("looks the active version up again on Refresh", async () => {
    queueReads(HELD_IN_PROGRESS);
    renderPage();
    expect(
      await screen.findByText("Version v3 was published but not deployed"),
    ).toBeInTheDocument();
    expect(listOntologyVersions).toHaveBeenCalledTimes(1);

    // The version gets deployed from the Versions tab in the meantime.
    listOntologyVersions.mockResolvedValue({
      versions: [],
      activeVersion: 3,
      latestDeployment: null,
    });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() =>
      expect(screen.queryByText(NOT_DEPLOYED)).not.toBeInTheDocument(),
    );
    expect(listOntologyVersions).toHaveBeenCalledTimes(2);
  });

  it("looks the active version up again when the published version changes", async () => {
    getProposal.mockImplementation((_api, _ns, id: string) =>
      Promise.resolve(
        id === "p-1"
          ? HELD_IN_PROGRESS
          : { ...HELD_IN_PROGRESS, proposal_id: "p-2", published_version: 4 },
      ),
    );
    renderPage(<Link to="/namespaces/ns/proposals/p-2">next proposal</Link>);

    expect(
      await screen.findByText("Version v3 was published but not deployed"),
    ).toBeInTheDocument();
    expect(listOntologyVersions).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText("next proposal"));

    expect(
      await screen.findByText("Version v4 was published but not deployed"),
    ).toBeInTheDocument();
    await waitFor(() => expect(listOntologyVersions).toHaveBeenCalledTimes(2));
  });

  it("stops following up when the page is left", async () => {
    queueReads(ACCEPTED);
    const { unmount } = renderPage();

    await screen.findByRole("button", { name: "Already accepted" });
    await tick(4000);
    const reads = getProposal.mock.calls.length;
    expect(reads).toBeGreaterThan(1);

    unmount();
    await tick(40_000);
    expect(getProposal).toHaveBeenCalledTimes(reads);
  });

  it("keeps an offloaded constraint config when the follow-up brings the outcome in", async () => {
    fetchJsonFromUrl.mockResolvedValue({
      classes: [
        {
          class_uri: "http://e/A",
          class_name: "A",
          constraints: [
            {
              property_path: "id",
              property_name: "id",
              constraint_type: "required",
              enabled: true,
              params: {},
              source: "db_constraint",
              description: "id is required",
            },
          ],
        },
      ],
    });
    const offloaded = {
      constraints_url: "https://s3.example/constraints.json",
    };
    queueReads(
      { ...ACCEPTED, ...offloaded },
      { ...HELD_IN_PROGRESS, ...offloaded },
    );
    renderPage();

    expect(await screen.findByText("(1 active)")).toBeInTheDocument();
    await tick();

    expect(
      await screen.findByText("Version v3 was published but not deployed"),
    ).toBeInTheDocument();
    expect(screen.getByText("(1 active)")).toBeInTheDocument();
    expect(fetchJsonFromUrl).toHaveBeenCalledTimes(1);
  });

  it("keeps the held banner hidden while the active version is being looked up", async () => {
    listOntologyVersions.mockReturnValue(new Promise(() => {}));
    queueReads(HELD_IN_PROGRESS);
    renderPage();

    await waitFor(() => expect(listOntologyVersions).toHaveBeenCalled());
    await tick(0);
    expect(screen.queryByText(NOT_DEPLOYED)).not.toBeInTheDocument();
  });

  it("ignores a late accepting-poll read once the proposal has moved on", async () => {
    const ACCEPTING = proposal({ status: "accepting" });
    let finishLate!: (p: Proposal) => void;
    let reads = 0;
    getProposal.mockImplementation(() => {
      reads += 1;
      if (reads === 2)
        // The 3 s accepting poll, answered only after the proposal is accepted.
        return new Promise<Proposal>((resolve) => {
          finishLate = resolve;
        });
      return Promise.resolve(reads === 1 ? ACCEPTING : STARTED);
    });
    renderPage();

    await waitFor(() => expect(getProposal).toHaveBeenCalledTimes(1));
    await tick(3000);
    expect(getProposal).toHaveBeenCalledTimes(2);
    // In an act, so the poll's cleanup has run before the late read settles, as it has in a browser.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    });
    await screen.findByRole("button", { name: "Already accepted" });

    await act(async () => {
      finishLate(ACCEPTING);
    });
    expect(
      screen.getByRole("button", { name: "Already accepted" }),
    ).toBeInTheDocument();
  });
});
