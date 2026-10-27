// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import { LocaleProvider } from "../../../i18n/LocaleProvider";
import type {
  StudioChangeSet,
  StudioChangeSetPreview,
} from "../../../services/studio";
import { ChangeSetCard, type ChangeSetCardProps } from "./ChangeSetCard";

const CS: StudioChangeSet = {
  changeSetId: "cs-1",
  turnId: "t-1",
  status: "pending",
  createdAt: null,
  createdRevision: 4,
  createdBy: "alice",
  rationale: "Contracts name suppliers of items",
  ops: [
    { op: "add_class", label: "Supplier", iri: "http://e/o#Supplier" },
    {
      op: "add_object_property",
      label: "supplies",
      domain: "http://e/o#Supplier",
      range: "http://e/o#Item",
    },
  ],
  summary: [
    "Added class Supplier",
    "Added relationship supplies from Supplier to Item",
  ],
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

function preview(
  overrides: Partial<StudioChangeSetPreview> = {},
): StudioChangeSetPreview {
  return {
    revision: 4,
    changeSetId: "cs-1",
    opIndexes: [0, 1],
    applies: true,
    error: null,
    warnings: [],
    created: [],
    diff: null,
    graph: null,
    ...overrides,
  };
}

function renderCard(overrides: Partial<ChangeSetCardProps> = {}) {
  const props: ChangeSetCardProps = {
    changeSet: CS,
    editable: true,
    busy: false,
    previewing: false,
    onPreview: vi.fn().mockResolvedValue(preview()),
    onHidePreview: vi.fn(),
    onApply: vi.fn().mockResolvedValue(null),
    onDiscard: vi.fn().mockResolvedValue(null),
    ...overrides,
  };
  return { props, ...render(<ChangeSetCard {...props} />) };
}

describe("ChangeSetCard", () => {
  it("shows the rationale and every op checked, and applies them all", async () => {
    const { props } = renderCard();
    expect(screen.getByText("Contracts name suppliers of items")).toBeTruthy();
    expect(screen.getByText("Pending")).toBeTruthy();
    const boxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    expect(boxes.map((b) => b.checked)).toEqual([true, true]);
    fireEvent.click(screen.getByRole("button", { name: "Apply all" }));
    await waitFor(() => expect(props.onApply).toHaveBeenCalledWith(null));
  });

  it("applies only the checked ops", async () => {
    const { props } = renderCard();
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByRole("button", { name: "Apply selected (1)" }));
    await waitFor(() => expect(props.onApply).toHaveBeenCalledWith([0]));
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    expect(
      (
        screen.getByRole("button", {
          name: "Apply selected (0)",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  });

  it("previews on the graph and says which op does not apply", async () => {
    const failing = preview({
      applies: false,
      opIndexes: [1],
      error: {
        message: "domain Supplier is not a class of the draft",
        opIndex: 1,
      },
    });
    const onPreview = vi.fn().mockResolvedValue(failing);
    renderCard({ onPreview });
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    fireEvent.click(screen.getByRole("button", { name: "Show on graph" }));
    await waitFor(() => expect(onPreview).toHaveBeenCalledWith([1]));
    expect(
      await screen.findByText(
        "“Added relationship supplies from Supplier to Item” does not apply: domain Supplier is not a class of the draft",
      ),
    ).toBeTruthy();
  });

  it("previews again when the checked ops change while the preview is shown", async () => {
    const { props } = renderCard({ previewing: true });
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    await waitFor(() => expect(props.onPreview).toHaveBeenCalledWith([0]));
    fireEvent.click(screen.getByRole("button", { name: "Hide preview" }));
    expect(props.onHidePreview).toHaveBeenCalled();
  });

  it("shows why an apply or a discard failed", async () => {
    renderCard({
      onApply: vi.fn().mockResolvedValue("Op 1: no longer applies"),
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply all" }));
    expect(await screen.findByText("Op 1: no longer applies")).toBeTruthy();
  });

  it("warns that a stale change set no longer applies", () => {
    renderCard({
      changeSet: {
        ...CS,
        stale: {
          revision: 6,
          message:
            "The draft changed after the assistant proposed this change set",
          opIndex: 0,
        },
      },
    });
    expect(screen.getByText("The draft changed")).toBeTruthy();
    expect(
      screen.getByText(
        "The draft changed after the assistant proposed this change set",
      ),
    ).toBeTruthy();
  });

  it("says once why an apply refused as stale failed", async () => {
    const reason =
      "The draft changed after the assistant proposed this change set, and it no longer applies: Op 1: x is not a class of the draft";
    const { props, rerender } = renderCard({
      onApply: vi.fn().mockResolvedValue(reason),
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply all" }));
    await waitFor(() => expect(props.onApply).toHaveBeenCalled());
    // The panel reloads the change set, now marked stale with the same reason.
    rerender(
      <ChangeSetCard
        {...props}
        changeSet={{
          ...CS,
          stale: { revision: 6, message: reason, opIndex: 1 },
        }}
      />,
    );
    await waitFor(() => expect(screen.getAllByText(reason)).toHaveLength(1));
    expect(screen.getByText("The draft changed")).toBeTruthy();
  });

  it("discards", async () => {
    const { props } = renderCard();
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(props.onDiscard).toHaveBeenCalled());
  });

  it("shows an applied change set without choices", () => {
    renderCard({
      changeSet: {
        ...CS,
        status: "applied",
        appliedRevision: 5,
        appliedOpIndexes: [0],
      },
    });
    expect(screen.getByText("Applied in revision 5")).toBeTruthy();
    expect(screen.queryAllByRole("checkbox")).toEqual([]);
    expect(screen.queryByRole("button", { name: /Apply/ })).toBeNull();
  });

  it("offers no apply on a read-only draft", () => {
    renderCard({ editable: false });
    expect(screen.queryAllByRole("checkbox")).toEqual([]);
    expect(
      (screen.getByRole("button", { name: "Apply all" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  describe("in Japanese", () => {
    afterEach(() => {
      window.localStorage.clear();
      document.documentElement.lang = "";
    });

    function renderInJapanese(overrides: Partial<ChangeSetCardProps>) {
      window.localStorage.setItem("scl.locale", "ja");
      const props: ChangeSetCardProps = {
        changeSet: CS,
        editable: true,
        busy: false,
        previewing: false,
        onPreview: vi.fn().mockResolvedValue(preview()),
        onHidePreview: vi.fn(),
        onApply: vi.fn().mockResolvedValue(null),
        onDiscard: vi.fn().mockResolvedValue(null),
        ...overrides,
      };
      return render(
        <LocaleProvider>
          <ChangeSetCard {...props} />
        </LocaleProvider>,
      );
    }

    it("writes the coded op lines and warnings in Japanese, and the others as the engine stored them", async () => {
      const vendor = "http://e/o#Vendor";
      renderInJapanese({
        changeSet: {
          ...CS,
          summaryMessages: [
            { code: "ADD_CLASS", params: { label: "Supplier" } },
            // A code this app does not know yet keeps the engine's English line.
            { code: "A_LATER_CODE", params: {} },
          ],
          warnings: [`Another class is also labelled 'Supplier': ${vendor}`],
          warningMessages: [
            {
              code: "ANOTHER_CLASS_LABELLED",
              params: { label: "Supplier", iri: vendor },
            },
          ],
        },
        onPreview: vi.fn().mockResolvedValue(
          preview({
            applies: false,
            error: { message: "Op 0: taken", opIndex: 0 },
          }),
        ),
      });
      expect(
        await screen.findByText("クラス Supplier を追加しました"),
      ).toBeTruthy();
      expect(
        screen.getByText("Added relationship supplies from Supplier to Item"),
      ).toBeTruthy();
      expect(
        screen.getByText(
          `別のクラスにも同じラベル「Supplier」が付いています: ${vendor}`,
        ),
      ).toBeTruthy();
      // The op that does not apply is named by its line in Japanese too.
      fireEvent.click(screen.getByRole("button", { name: "グラフに表示" }));
      expect(
        await screen.findByText(
          "「クラス Supplier を追加しました」を適用できません: Op 0: taken",
        ),
      ).toBeTruthy();
    });

    it("keeps the English lines of a change set proposed before the codes", async () => {
      renderInJapanese({});
      expect(await screen.findByText("Added class Supplier")).toBeTruthy();
      expect(screen.getByRole("button", { name: "グラフに表示" })).toBeTruthy();
    });
  });
});
