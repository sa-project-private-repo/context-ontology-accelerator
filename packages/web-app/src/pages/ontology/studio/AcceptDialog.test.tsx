// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, it, expect, vi, beforeEach } from "vitest";
import type { ApiClient } from "@components/ApiClientProvider";
import { LocaleProvider } from "../../../i18n/LocaleProvider";
import type {
  StudioAcceptPreview,
  StudioStructuredCheck,
} from "../../../services/studio";
import { AcceptDialog } from "./AcceptDialog";

const studio = vi.hoisted(() => ({ getStudioAcceptPreview: vi.fn() }));
vi.mock("../../../services/studio", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../services/studio")>()),
  getStudioAcceptPreview: (...a: unknown[]) =>
    studio.getStudioAcceptPreview(...a),
}));

const api = {} as ApiClient;
const O = "http://e/o#";
const NONE = { count: 0, terms: [] };
const STRUCTURED: StudioStructuredCheck = {
  unmappedClasses: {
    count: 2,
    terms: [
      { iri: `${O}LoyaltyProgramme`, label: "Loyalty programme" },
      { iri: `${O}PremiumCustomer`, label: "Premium customer" },
    ],
  },
  unmappedProperties: {
    count: 1,
    terms: [
      {
        iri: `${O}customers_loyaltyTier`,
        label: "loyalty tier",
        domainIri: `${O}Customers`,
      },
    ],
  },
  labelDiffers: NONE,
  multiTableClasses: NONE,
  mapsNoTable: false,
  error: null,
};

function preview(overrides: Partial<StudioAcceptPreview> = {}) {
  return {
    revision: 2,
    baseVersion: 1,
    headVersion: 1,
    rebaseRequired: false,
    diff: {
      classes: { added: [], removed: [], changed: [] },
      objectProperties: { added: [], removed: [], changed: [] },
      datatypeProperties: { added: [], removed: [], changed: [] },
    },
    renamed: [],
    breakingMetrics: [],
    breakingMetricsError: null,
    structured: STRUCTURED,
    ...overrides,
  } satisfies StudioAcceptPreview;
}

function renderDialog() {
  return render(
    <AcceptDialog
      visible
      apiClient={api}
      namespace="ns"
      proposalId="p-1"
      revision={2}
      accepting={false}
      onDismiss={vi.fn()}
      onAccept={vi.fn()}
      onPreview={vi.fn()}
    />,
  );
}

describe("AcceptDialog", () => {
  beforeEach(() => {
    studio.getStudioAcceptPreview.mockReset().mockResolvedValue(preview());
  });

  it("lists the added or changed terms that structured queries cannot answer", async () => {
    renderDialog();
    expect(
      await screen.findByText("Not answered by structured queries (3)"),
    ).toBeTruthy();
    expect(screen.getByText("Loyalty programme")).toBeTruthy();
    expect(screen.getByText("Premium customer")).toBeTruthy();
    expect(screen.getByText("loyalty tier").getAttribute("title")).toBe(
      `${O}customers_loyaltyTier`,
    );
    expect(screen.queryByText(/structured queries are off/)).toBeNull();
  });

  it("counts the terms past the first 20", async () => {
    const terms = Array.from({ length: 100 }, (_, i) => ({
      iri: `${O}C${i}`,
      label: `C${i}`,
    }));
    studio.getStudioAcceptPreview.mockResolvedValue(
      preview({
        structured: {
          ...STRUCTURED,
          unmappedClasses: { count: 140, terms },
        },
      }),
    );
    renderDialog();
    expect(
      await screen.findByText("Not answered by structured queries (141)"),
    ).toBeTruthy();
    expect(screen.getByText("C19")).toBeTruthy();
    expect(screen.queryByText("C20")).toBeNull();
    // Each group shows its first 20: the attribute, then C0 to C19.
    expect(screen.getByText("and 120 more")).toBeTruthy();
  });

  it("keeps the attributes and relationships of mapped classes in view next to many new classes", async () => {
    // A first documents induction can add dozens of classes; a mapped class's new attribute stays listed.
    const classes = Array.from({ length: 21 }, (_, i) => ({
      iri: `${O}Concept${i}`,
      label: `Document concept ${i}`,
    }));
    studio.getStudioAcceptPreview.mockResolvedValue(
      preview({
        structured: {
          ...STRUCTURED,
          unmappedClasses: { count: 21, terms: classes },
          unmappedProperties: {
            count: 2,
            terms: [
              ...STRUCTURED.unmappedProperties.terms,
              {
                iri: `${O}customers_preferredOrder`,
                label: "preferred order",
                domainIri: `${O}Customers`,
              },
            ],
          },
        },
      }),
    );
    renderDialog();
    expect(
      await screen.findByText("Not answered by structured queries (23)"),
    ).toBeTruthy();
    expect(screen.getByText("loyalty tier")).toBeTruthy();
    expect(screen.getByText("preferred order")).toBeTruthy();
    expect(screen.getByText("Document concept 19")).toBeTruthy();
    expect(screen.getByText("and 1 more")).toBeTruthy();
  });

  it("lists the mapped terms the draft takes out of structured queries, with their tables and columns", async () => {
    studio.getStudioAcceptPreview.mockResolvedValue(
      preview({
        structured: {
          ...STRUCTURED,
          removedMappedClasses: {
            count: 1,
            terms: [{ iri: `${O}Orders`, label: "orders", tables: ["orders"] }],
          },
          removedMappedProperties: {
            count: 22,
            terms: Array.from({ length: 22 }, (_, i) => ({
              iri: `${O}customers_c${i}`,
              label: `c${i}`,
              domainIri: `${O}Customers`,
              physicalName: `c${i}`,
            })),
          },
        },
      }),
    );
    renderDialog();
    expect(
      await screen.findByText("Removed from structured queries (23)"),
    ).toBeTruthy();
    expect(screen.getByText("orders (orders)").getAttribute("title")).toBe(
      `${O}Orders`,
    );
    expect(screen.getByText("c18 (c18)")).toBeTruthy();
    expect(screen.queryByText("c19 (c19)")).toBeNull();
    expect(screen.getByText("and 3 more")).toBeTruthy();
  });

  it("shows no removed list when the draft deletes nothing mapped, or for an engine that does not say", async () => {
    renderDialog();
    expect(
      await screen.findByText("Not answered by structured queries (3)"),
    ).toBeTruthy();
    expect(screen.queryByText(/Removed from structured queries/)).toBeNull();
  });

  it("warns that a draft mapping no table turns structured queries off", async () => {
    studio.getStudioAcceptPreview.mockResolvedValue(
      preview({
        structured: {
          ...STRUCTURED,
          unmappedClasses: NONE,
          unmappedProperties: NONE,
          mapsNoTable: true,
        },
      }),
    );
    renderDialog();
    expect(
      await screen.findByText(
        "This draft maps no table; structured queries are off after deploy.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/Not answered by structured queries/)).toBeNull();
  });

  it("says when the no-table check could not run, as the engine wrote it", async () => {
    const error =
      "The table mappings of the version the draft was built on could not be read.";
    studio.getStudioAcceptPreview.mockResolvedValue(
      preview({ structured: { ...STRUCTURED, error } }),
    );
    renderDialog();
    expect(await screen.findByText(error)).toBeTruthy();
    expect(
      screen.getByText("Not answered by structured queries (3)"),
    ).toBeTruthy();
  });

  it("shows no structured-query list for an engine that does not send it", async () => {
    const { structured: _s, ...older } = preview();
    studio.getStudioAcceptPreview.mockResolvedValue(older);
    renderDialog();
    expect(await screen.findByText(/becomes the next version/)).toBeTruthy();
    expect(screen.queryByText(/Not answered by structured queries/)).toBeNull();
  });

  describe("in Japanese", () => {
    const METRICS =
      "Breaking metrics could not be checked; the graph database may be unreachable.";
    const BASE_MAPPING =
      "The table mappings of the version the draft was built on could not be read.";

    afterEach(() => {
      window.localStorage.clear();
      document.documentElement.lang = "";
    });

    function renderInJapanese() {
      window.localStorage.setItem("scl.locale", "ja");
      return render(
        <LocaleProvider>
          <AcceptDialog
            visible
            apiClient={api}
            namespace="ns"
            proposalId="p-1"
            revision={2}
            accepting={false}
            onDismiss={vi.fn()}
            onAccept={vi.fn()}
            onPreview={vi.fn()}
          />
        </LocaleProvider>,
      );
    }

    it("writes the engine's fixed errors in Japanese by their codes", async () => {
      studio.getStudioAcceptPreview.mockResolvedValue(
        preview({
          breakingMetricsError: METRICS,
          breakingMetricsErrorCode: "METRICS_UNCHECKED",
          structured: {
            ...STRUCTURED,
            error: BASE_MAPPING,
            errorCode: "BASE_MAPPING_UNREADABLE",
          },
        }),
      );
      renderInJapanese();
      expect(
        await screen.findByText(
          "壊れるメトリクスを確認できませんでした。グラフデータベースに接続できない可能性があります。",
        ),
      ).toBeTruthy();
      expect(
        screen.getByText(
          "ドラフトがベースにしているバージョンのテーブルマッピングを読み取れませんでした。",
        ),
      ).toBeTruthy();
    });

    it("shows them as the engine wrote them from an engine that sends no codes", async () => {
      studio.getStudioAcceptPreview.mockResolvedValue(
        preview({
          breakingMetricsError: METRICS,
          structured: { ...STRUCTURED, error: BASE_MAPPING },
        }),
      );
      renderInJapanese();
      expect(await screen.findByText(METRICS)).toBeTruthy();
      expect(screen.getByText(BASE_MAPPING)).toBeTruthy();
    });
  });
});
