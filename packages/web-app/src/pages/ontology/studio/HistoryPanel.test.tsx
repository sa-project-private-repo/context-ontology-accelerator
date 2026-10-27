// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import { LocaleProvider } from "../../../i18n/LocaleProvider";
import { HistoryPanel } from "./HistoryPanel";

const HISTORY = {
  revision: 3,
  canUndo: true,
  canRedo: true,
  entries: [
    {
      revision: 3,
      kind: "undo" as const,
      source: "user" as const,
      rationale: null,
      changeSetId: null,
      author: "bob",
      at: null,
      summary: ["Undid revision 2"],
      opCount: 1,
      target: 2,
      warnings: [],
      active: false,
      undone: false,
    },
    {
      revision: 2,
      kind: "apply" as const,
      source: "agent" as const,
      rationale: "Legacy duplicates Customer",
      changeSetId: "cs-1",
      author: "alice",
      at: null,
      summary: ["Deleted class Legacy (detach its properties)"],
      opCount: 1,
      target: null,
      warnings: ["Another class is also labelled 'Order'"],
      active: false,
      undone: true,
    },
  ],
};

describe("HistoryPanel", () => {
  it("lists revisions newest first and marks undone ones", () => {
    render(
      <HistoryPanel
        history={HISTORY}
        loading={false}
        error={null}
        onRefresh={vi.fn()}
        onLoadOlder={vi.fn()}
      />,
    );
    const newest = screen.getByTestId("history-3");
    expect(within(newest).getByText("Undo")).toBeTruthy();
    expect(within(newest).getByText("Undid revision 2")).toBeTruthy();
    const undone = screen.getByTestId("history-2");
    expect(within(undone).getByText("Undone")).toBeTruthy();
    expect(within(undone).getByText(/also labelled 'Order'/)).toBeTruthy();
    // The last page offers no older revisions.
    expect(
      screen.queryByRole("button", { name: "Load older revisions" }),
    ).toBeNull();
  });

  it("offers older revisions while there is a next page", () => {
    const onLoadOlder = vi.fn();
    render(
      <HistoryPanel
        history={{ ...HISTORY, nextToken: "t-2" }}
        loading={false}
        error={null}
        onRefresh={vi.fn()}
        onLoadOlder={onLoadOlder}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Load older revisions" }),
    );
    expect(onLoadOlder).toHaveBeenCalled();
  });

  it("marks an applied assistant change set and shows its rationale", () => {
    render(
      <HistoryPanel
        history={HISTORY}
        loading={false}
        error={null}
        onRefresh={vi.fn()}
      />,
    );
    const agent = screen.getByTestId("history-2");
    expect(within(agent).getByText("Assistant")).toBeTruthy();
    expect(within(agent).getByText("Legacy duplicates Customer")).toBeTruthy();
    expect(
      within(screen.getByTestId("history-3")).queryByText("Assistant"),
    ).toBeNull();
  });

  it("refreshes on demand and shows a load error", () => {
    const onRefresh = vi.fn();
    render(
      <HistoryPanel
        history={null}
        loading={false}
        error="Could not read the history"
        onRefresh={onRefresh}
        onLoadOlder={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Refresh history" }));
    expect(onRefresh).toHaveBeenCalled();
    expect(screen.getByText("Could not read the history")).toBeTruthy();
  });

  describe("in Japanese", () => {
    afterEach(() => {
      window.localStorage.clear();
      document.documentElement.lang = "";
    });

    it("writes the time of a revision for the Japanese locale", async () => {
      window.localStorage.setItem("scl.locale", "ja");
      const at = "2026-10-24T07:51:24Z";
      const entry = { ...HISTORY.entries[0], at };
      render(
        <LocaleProvider>
          <HistoryPanel
            history={{ ...HISTORY, entries: [entry] }}
            loading={false}
            error={null}
            onRefresh={vi.fn()}
            onLoadOlder={vi.fn()}
          />
        </LocaleProvider>,
      );
      const time = new Date(at).toLocaleString("ja");
      expect(await screen.findByText(new RegExp(time))).toBeTruthy();
    });

    it("writes the coded lines in Japanese and the others as the engine stored them", async () => {
      window.localStorage.setItem("scl.locale", "ja");
      const coded = {
        ...HISTORY.entries[1],
        revision: 5,
        summary: ["Added class Supplier", "Made Supplier a subclass of Party"],
        summaryMessages: [
          { code: "ADD_CLASS", params: { label: "Supplier" } },
          { code: "A_LATER_CODE", params: { term: "Supplier" } },
        ],
      };
      render(
        <LocaleProvider>
          <HistoryPanel
            history={{ ...HISTORY, entries: [coded, HISTORY.entries[0]] }}
            loading={false}
            error={null}
            onRefresh={vi.fn()}
            onLoadOlder={vi.fn()}
          />
        </LocaleProvider>,
      );
      const entry = await screen.findByTestId("history-5");
      expect(
        await within(entry).findByText("クラス Supplier を追加しました"),
      ).toBeTruthy();
      // A code this app does not know yet keeps the engine's English line.
      expect(
        within(entry).getByText("Made Supplier a subclass of Party"),
      ).toBeTruthy();
      // An entry stored before the codes has its English lines only.
      expect(
        within(screen.getByTestId("history-3")).getByText("Undid revision 2"),
      ).toBeTruthy();
    });

    it("writes coded warnings in Japanese, and those of an older revision as the engine stored them", async () => {
      window.localStorage.setItem("scl.locale", "ja");
      const warned = {
        ...HISTORY.entries[1],
        revision: 6,
        undone: false,
        warnings: [
          "customer_email is mapped from table customer; with the domain Party, SPARQL through the VKG no longer offers it on Customer, but NL-to-SQL still does.",
        ],
        warningMessages: [
          {
            code: "DOMAIN_ON_PARENT_TABLE",
            params: {
              property: "customer_email",
              tables: "customer",
              domain: "Party",
              mappedClass: "Customer",
            },
          },
        ],
      };
      render(
        <LocaleProvider>
          <HistoryPanel
            history={{ ...HISTORY, entries: [warned, HISTORY.entries[1]] }}
            loading={false}
            error={null}
            onRefresh={vi.fn()}
            onLoadOlder={vi.fn()}
          />
        </LocaleProvider>,
      );
      expect(
        await within(await screen.findByTestId("history-6")).findByText(
          "customer_email はテーブル customer からマッピングされています。ドメインを Party にすると、VKG 経由の SPARQL では Customer で使えなくなりますが、NL-to-SQL では引き続き使えます。",
        ),
      ).toBeTruthy();
      expect(
        within(screen.getByTestId("history-2")).getByText(
          "Another class is also labelled 'Order'",
        ),
      ).toBeTruthy();
    });
  });
});
