// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProposalDetailPage } from "./ProposalDetail";
import { LocaleProvider } from "../../../i18n/LocaleProvider";
import type { ApiClient } from "@components/ApiClientProvider";
import type { Proposal } from "../../../services/ontology-engine";

// The proposal page's Summary in the reader's language.

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
vi.mock("../../../services/ontology-engine", async (importOriginal) => ({
  ...(await importOriginal<
    typeof import("../../../services/ontology-engine")
  >()),
  getProposal: (...a: unknown[]) => getProposal(...a),
  listOntologyVersions: vi
    .fn()
    .mockResolvedValue({ versions: [], activeVersion: null }),
}));

const CREATED = "2026-10-23T22:54:50.002066+00:00";

function proposal(overrides: Partial<Proposal> = {}): Proposal {
  return {
    proposal_id: "p-1",
    job_id: "j-1",
    namespace: "ns",
    ontology_id: "ont",
    proposal_type: "induced",
    status: "pending",
    created_at: CREATED,
    updated_at: CREATED,
    ontology_turtle: "",
    r2rml_turtle: "",
    ...overrides,
  };
}

function renderInJapanese() {
  window.localStorage.setItem("scl.locale", "ja");
  return render(
    <LocaleProvider>
      <MemoryRouter initialEntries={["/namespaces/ns/proposals/p-1"]}>
        <Routes>
          <Route
            path="/namespaces/:namespaceId/proposals/:proposalId"
            element={<ProposalDetailPage />}
          />
        </Routes>
      </MemoryRouter>
    </LocaleProvider>,
  );
}

describe("ProposalDetail summary in Japanese", () => {
  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.lang = "";
  });

  it("shows when the proposal was created in the reader's date format", async () => {
    getProposal.mockResolvedValue(proposal());
    renderInJapanese();
    expect(
      await screen.findByText(new Date(CREATED).toLocaleString("ja")),
    ).toBeInTheDocument();
    expect(screen.queryByText(CREATED)).toBeNull();
  });

  it("words a failed proposal's status in Japanese", async () => {
    getProposal.mockResolvedValue(proposal({ status: "failed" }));
    renderInJapanese();
    await screen.findByText(new Date(CREATED).toLocaleString("ja"));
    expect(screen.getByText("失敗")).toBeInTheDocument();
    expect(screen.queryByText("failed")).toBeNull();
  });
});
