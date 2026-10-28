// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The Ontology designer joins the app's breadcrumb trail below its proposal:
 * COA › {Namespace} › Ontology › Induction › {Proposal} › Ontology designer,
 * as ProposalDetail publishes the trail down to the proposal.
 */
import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "@components/ApiClientProvider";
import type { BreadcrumbItem } from "@components/BreadcrumbProvider";
import { LocaleProvider } from "../../../i18n/LocaleProvider";
import { StudioPage } from "./StudioPage";

// This is a breadcrumb-trail test, so it opts out of the global
// vitest-setup mocks that no-op useBreadcrumbTrail and stub useNamespace.
vi.unmock("@api-hooks/use-breadcrumb-trail");
vi.unmock("@api-hooks/use-namespace");

vi.mock("@api-hooks/use-get-namespace", () => ({
  useGetNamespace: () => ({
    data: { namespace: { displayName: "My Namespace" } },
    isSuccess: true,
    isLoading: false,
  }),
}));

let captured: BreadcrumbItem[] = [];
vi.mock("@components/BreadcrumbProvider", () => ({
  useBreadcrumbs: () => ({
    setBreadcrumbs: (items: BreadcrumbItem[]) => {
      captured = items;
    },
    clearBreadcrumbs: () => {
      captured = [];
    },
  }),
}));

// The trail does not wait for the draft: every read stays pending.
const pending = () => new Promise<never>(() => {});
const api = { get: pending, post: pending } as unknown as ApiClient;

function renderDesigner() {
  return render(
    <MemoryRouter
      initialEntries={["/namespaces/ns/ontology/proposals/p-1/studio"]}
    >
      <Routes>
        <Route
          path="/namespaces/:namespaceId/ontology/proposals/:proposalId/studio"
          element={
            <StudioPage apiClient={api} namespace="ns" proposalId="p-1" />
          }
        />
        <Route
          path="/namespaces/:namespaceId/ontology/proposals/:proposalId"
          element={<div>proposal page</div>}
        />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  captured = [];
});

describe("StudioPage breadcrumb trail", () => {
  it("names the namespace and the proposal above the designer", async () => {
    renderDesigner();

    await waitFor(() => expect(captured.length).toBeGreaterThan(1));
    expect(captured.map((c) => c.text)).toEqual([
      "COA",
      "My Namespace",
      "Ontology",
      "Induction",
      "p-1",
      "Ontology designer",
    ]);
    // The designer is the current page and Ontology a grouping label: no link.
    expect(captured.at(-1)?.onClick).toBeUndefined();
    expect(captured[2].onClick).toBeUndefined();
    expect(captured[1].onClick).toBeTypeOf("function");
    expect(captured[3].onClick).toBeTypeOf("function");

    act(() => captured[4].onClick?.());
    expect(await screen.findByText("proposal page")).toBeInTheDocument();
  });

  it("translates the fixed crumbs in Japanese and leaves the names verbatim", async () => {
    window.localStorage.setItem("scl.locale", "ja");
    try {
      render(
        <LocaleProvider>
          <MemoryRouter>
            <StudioPage apiClient={api} namespace="ns" proposalId="p-1" />
          </MemoryRouter>
        </LocaleProvider>,
      );

      await waitFor(() =>
        expect(captured.map((c) => c.text)).toEqual([
          "COA",
          "My Namespace",
          "オントロジー",
          "誘導",
          "p-1",
          "オントロジーデザイナー",
        ]),
      );
    } finally {
      window.localStorage.clear();
      document.documentElement.lang = "";
    }
  });
});
