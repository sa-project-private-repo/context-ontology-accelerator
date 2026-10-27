// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ToolsPanelProvider, useToolsPanel } from "./index";

let renders = 0;
let tools: ReturnType<typeof useToolsPanel> | null = null;

/** Reads the drawers as the app shell does, and counts its renders. */
function Consumer() {
  tools = useToolsPanel();
  renders += 1;
  return <p>{tools.state.activeDrawerId ?? "none"}</p>;
}

function renderTools() {
  renders = 0;
  render(
    <ToolsPanelProvider>
      <Consumer />
    </ToolsPanelProvider>,
  );
}

describe("ToolsPanelProvider", () => {
  it("does not render its consumers again when no drawer is open and one is closed", () => {
    renderTools();
    const before = renders;
    act(() => tools?.closeDrawer());
    expect(renders).toBe(before);
    expect(screen.getByText("none")).toBeInTheDocument();
  });

  it("closes the open drawer", () => {
    renderTools();
    act(() => tools?.openDrawer("help"));
    expect(screen.getByText("help")).toBeInTheDocument();
    act(() => tools?.closeDrawer());
    expect(screen.getByText("none")).toBeInTheDocument();
  });
});
