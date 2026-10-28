// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SplitPanelProvider, useSplitPanel } from "./index";

let renders = 0;
let panel: ReturnType<typeof useSplitPanel> | null = null;

/** Reads the panel as the app shell does, and counts its renders. */
function Consumer() {
  panel = useSplitPanel();
  renders += 1;
  return <p>{panel.state.isOpen ? "open" : "closed"}</p>;
}

function renderPanel() {
  renders = 0;
  render(
    <SplitPanelProvider>
      <Consumer />
    </SplitPanelProvider>,
  );
}

describe("SplitPanelProvider", () => {
  it("does not render its consumers again when a closed panel is closed", () => {
    renderPanel();
    const before = renders;
    act(() => panel?.closePanel());
    act(() => panel?.closePanel());
    expect(renders).toBe(before);
    expect(screen.getByText("closed")).toBeInTheDocument();
  });

  it("opens the panel and closes it once", () => {
    renderPanel();
    act(() => panel?.openPanel("Customer", <p>detail</p>));
    expect(screen.getByText("open")).toBeInTheDocument();
    expect(panel?.state.header).toBe("Customer");
    const opened = renders;
    act(() => panel?.closePanel());
    expect(screen.getByText("closed")).toBeInTheDocument();
    expect(renders).toBe(opened + 1);
    // Closing keeps the header and content; closing again changes nothing.
    act(() => panel?.closePanel());
    expect(renders).toBe(opened + 1);
    expect(panel?.state.header).toBe("Customer");
  });
});
