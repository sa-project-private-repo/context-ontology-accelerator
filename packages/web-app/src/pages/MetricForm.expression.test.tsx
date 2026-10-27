// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MetricForm } from "./MetricForm";
import { LocaleProvider } from "../i18n/LocaleProvider";

vi.mock("../api-hooks/use-metrics", () => ({
  useGetMetric: () => ({ data: undefined, isLoading: false, error: null }),
  useCreateMetric: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateMetric: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("../api-hooks/use-list-sources", () => ({
  useListSources: () => ({
    data: { pages: [{ items: [] }] },
    isLoading: false,
    error: null,
  }),
}));
vi.mock("../api-hooks/use-list-source-tables", () => ({
  useListSourceTables: () => ({
    data: { items: [] },
    isFetching: false,
    error: null,
  }),
}));
vi.mock("@components/ApiClientProvider", () => ({
  useApiClient: () => ({ get: vi.fn() }),
}));
vi.mock("../services/ontology-engine", () => ({
  searchEntities: vi.fn().mockResolvedValue([]),
}));

function renderCreateForm() {
  return render(
    <LocaleProvider>
      <MemoryRouter initialEntries={["/namespaces/ns/metrics/create"]}>
        <Routes>
          <Route
            path="/namespaces/:namespaceId/metrics/:metricName"
            element={<MetricForm />}
          />
        </Routes>
      </MemoryRouter>
    </LocaleProvider>,
  );
}

describe("MetricForm SQL expression", () => {
  afterEach(() => {
    window.localStorage.removeItem("scl.locale");
  });

  // The API refuses an aggregate fragment such as SUM(orders.total_amount):
  // a metric expression is a full SELECT statement.
  it.each(["en", "ja"])(
    "suggests a full SELECT statement (%s)",
    async (locale) => {
      window.localStorage.setItem("scl.locale", locale);
      renderCreateForm();
      const expression = await screen.findByPlaceholderText(
        "SELECT SUM(total_amount) FROM orders",
      );
      expect(expression.tagName).toBe("TEXTAREA");
      expect(
        screen.queryByPlaceholderText("SUM(orders.total_amount)"),
      ).toBeNull();
    },
  );
});
