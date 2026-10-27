// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BreadcrumbProvider, useBreadcrumbs } from "./index";

let renders = 0;
let crumbs: ReturnType<typeof useBreadcrumbs> | null = null;

/** Reads the trail as the app shell does, and counts its renders. */
function Consumer() {
  crumbs = useBreadcrumbs();
  renders += 1;
  return <p>{crumbs.items.map((i) => i.text).join(" / ") || "no trail"}</p>;
}

function renderTrail() {
  renders = 0;
  render(
    <BreadcrumbProvider>
      <Consumer />
    </BreadcrumbProvider>,
  );
}

describe("BreadcrumbProvider", () => {
  it("does not render its consumers again when an empty trail is cleared", () => {
    renderTrail();
    const before = renders;
    act(() => crumbs?.clearBreadcrumbs());
    expect(renders).toBe(before);
    expect(screen.getByText("no trail")).toBeInTheDocument();
  });

  it("clears a published trail", () => {
    renderTrail();
    act(() => crumbs?.setBreadcrumbs([{ text: "COA" }, { text: "Sources" }]));
    expect(screen.getByText("COA / Sources")).toBeInTheDocument();
    act(() => crumbs?.clearBreadcrumbs());
    expect(screen.getByText("no trail")).toBeInTheDocument();
  });
});
