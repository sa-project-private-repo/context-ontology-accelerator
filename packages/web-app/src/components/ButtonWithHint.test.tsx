// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { ButtonWithHint } from "./ButtonWithHint";

describe("ButtonWithHint", () => {
  it("shows its hint on hover", () => {
    const { container } = render(
      <ButtonWithHint hint="Checks the draft">Validate</ButtonWithHint>,
    );
    fireEvent.mouseEnter(container.firstElementChild as Element);
    expect(screen.getByText("Checks the draft")).toBeInTheDocument();
  });

  it("says why it is off instead of showing its hint", () => {
    const { container } = render(
      <ButtonWithHint
        hint="Checks the draft"
        disabled
        disabledReason="Your role does not allow this."
      >
        Validate
      </ButtonWithHint>,
    );
    fireEvent.mouseEnter(container.firstElementChild as Element);
    expect(screen.queryByText("Checks the draft")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Validate" }),
    ).toHaveAccessibleDescription("Your role does not allow this.");
  });

  it("keeps its hint while it is on, whatever reason it was given", () => {
    const { container } = render(
      <ButtonWithHint hint="Checks the draft" disabledReason="Not now.">
        Validate
      </ButtonWithHint>,
    );
    fireEvent.mouseEnter(container.firstElementChild as Element);
    expect(screen.getByText("Checks the draft")).toBeInTheDocument();
  });
});
