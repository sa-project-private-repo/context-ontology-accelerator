// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, fireEvent, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { TermPicker, TERM_SEARCH_DEBOUNCE_MS } from "./TermPicker";
import type { StudioTermHit } from "../../../services/studio";

const CUSTOMER: StudioTermHit = {
  iri: "http://e/o#Customer",
  label: "Customer",
  kind: "class",
};
const ORDER: StudioTermHit = {
  iri: "http://e/o#Order",
  label: "Order",
  kind: "class",
};

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Types into the box the way a user does: focus, then change. */
function type(input: HTMLElement, value: string) {
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value } });
}

/** Lets the debounce elapse and the search promise settle. */
async function elapse(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("TermPicker", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("searches once, after a pause in typing", async () => {
    const search = vi.fn().mockResolvedValue([]);
    render(
      <TermPicker
        ariaLabel="Find"
        kind="class"
        search={search}
        onPick={vi.fn()}
      />,
    );
    const input = screen.getByRole("combobox", { name: "Find" });
    type(input, "b");
    await elapse(TERM_SEARCH_DEBOUNCE_MS - 50);
    type(input, "bu");
    await elapse(TERM_SEARCH_DEBOUNCE_MS - 50);
    type(input, "buy");
    await elapse(TERM_SEARCH_DEBOUNCE_MS - 1);
    expect(search).not.toHaveBeenCalled();
    await elapse(1);
    expect(search).toHaveBeenCalledTimes(1);
    expect(search).toHaveBeenCalledWith("buy", "class");
  });

  it("does not search for blank text and drops a search that is still pending", async () => {
    const pending = deferred<StudioTermHit[]>();
    const search = vi.fn().mockReturnValue(pending.promise);
    render(<TermPicker ariaLabel="Find" search={search} onPick={vi.fn()} />);
    const input = screen.getByRole("combobox", { name: "Find" });
    type(input, "   ");
    await elapse(TERM_SEARCH_DEBOUNCE_MS);
    expect(search).not.toHaveBeenCalled();

    type(input, "buyer");
    await elapse(TERM_SEARCH_DEBOUNCE_MS);
    expect(search).toHaveBeenCalledTimes(1);
    // The text is cleared while the answer is on its way: it must not show up.
    fireEvent.change(input, { target: { value: "" } });
    pending.resolve([CUSTOMER]);
    await elapse(0);
    expect(screen.queryByRole("option")).toBeNull();
  });

  it("drops an answer that arrives after a newer search was made", async () => {
    const first = deferred<StudioTermHit[]>();
    const second = deferred<StudioTermHit[]>();
    const search = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    render(<TermPicker ariaLabel="Find" search={search} onPick={vi.fn()} />);
    const input = screen.getByRole("combobox", { name: "Find" });

    type(input, "buy");
    await elapse(TERM_SEARCH_DEBOUNCE_MS);
    type(input, "purch");
    await elapse(TERM_SEARCH_DEBOUNCE_MS);
    expect(search.mock.calls.map((c) => c[0])).toEqual(["buy", "purch"]);

    second.resolve([ORDER]);
    await elapse(0);
    expect(screen.getByRole("option", { name: /Order/ })).toBeTruthy();
    // The slow first answer is for text the user has moved on from.
    first.resolve([CUSTOMER]);
    await elapse(0);
    expect(screen.queryByRole("option", { name: /Customer/ })).toBeNull();
    expect(screen.getByRole("option", { name: /Order/ })).toBeTruthy();
  });

  it("says the search failed instead of claiming there are no matches", async () => {
    const search = vi.fn().mockRejectedValue(new Error("boom"));
    render(<TermPicker ariaLabel="Find" search={search} onPick={vi.fn()} />);
    type(screen.getByRole("combobox", { name: "Find" }), "buyer");
    await elapse(TERM_SEARCH_DEBOUNCE_MS);
    expect(screen.getByText(/Search failed/)).toBeTruthy();
    expect(screen.queryByText("No matches")).toBeNull();
  });

  it("reports the picked term and shows its label", async () => {
    const search = vi.fn().mockResolvedValue([CUSTOMER]);
    const onPick = vi.fn();
    render(<TermPicker ariaLabel="Find" search={search} onPick={onPick} />);
    const input = screen.getByRole("combobox", {
      name: "Find",
    }) as HTMLInputElement;
    type(input, "buyer");
    await elapse(TERM_SEARCH_DEBOUNCE_MS);
    fireEvent.mouseUp(screen.getByRole("option", { name: /Customer/ }));
    expect(onPick).toHaveBeenCalledWith(CUSTOMER);
    expect(input.value).toBe("Customer");
  });

  it("does not search for the IRI that picking a hit puts through onChange", async () => {
    // Cloudscape fires onChange({value: <the option's IRI>}) and then onSelect.
    // Only the typed query may reach the engine; a search for the IRI would
    // replace the hits (or fail) behind the label the box now shows.
    const search = vi
      .fn()
      .mockResolvedValueOnce([CUSTOMER])
      .mockRejectedValue(new Error("search for the IRI"));
    const onPick = vi.fn();
    render(
      <TermPicker
        ariaLabel="Find"
        kind="class"
        search={search}
        onPick={onPick}
      />,
    );
    const input = screen.getByRole("combobox", {
      name: "Find",
    }) as HTMLInputElement;
    type(input, "buyer");
    await elapse(TERM_SEARCH_DEBOUNCE_MS);
    fireEvent.mouseUp(screen.getByRole("option", { name: /Customer/ }));
    expect(onPick).toHaveBeenCalledWith(CUSTOMER);

    await elapse(TERM_SEARCH_DEBOUNCE_MS * 2);
    expect(search.mock.calls).toEqual([["buyer", "class"]]);
    expect(input.value).toBe("Customer");

    // Opened again, the box still lists the hit it was given and is not busy
    // or failed.
    fireEvent.keyDown(input, { key: "ArrowDown", keyCode: 40 });
    expect(screen.getByRole("option", { name: /Customer/ })).toBeTruthy();
    expect(screen.queryByText(/Search failed/)).toBeNull();
    expect(screen.queryByText("Searching…")).toBeNull();
  });

  it("still searches the typed text when Enter is pressed before the pause ends", async () => {
    // With nothing highlighted, Enter selects a "use what was typed" item whose
    // value is the text itself. That is not a pick, so it must not cancel the
    // search for that text.
    const search = vi.fn().mockResolvedValue([CUSTOMER]);
    const onPick = vi.fn();
    render(
      <TermPicker
        ariaLabel="Find"
        kind="class"
        search={search}
        onPick={onPick}
      />,
    );
    const input = screen.getByRole("combobox", { name: "Find" });
    type(input, "buyer");
    await elapse(TERM_SEARCH_DEBOUNCE_MS - 100);
    fireEvent.keyDown(input, { key: "Enter", keyCode: 13 });
    await elapse(TERM_SEARCH_DEBOUNCE_MS * 2);
    expect(search.mock.calls).toEqual([["buyer", "class"]]);
    expect(onPick).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: "ArrowDown", keyCode: 40 });
    expect(screen.getByRole("option", { name: /Customer/ })).toBeTruthy();
    expect(screen.queryByText(/Search failed/)).toBeNull();
    expect(screen.queryByText("Searching…")).toBeNull();
  });

  it("reports every edit of the text, and a pick after the edit it puts through", async () => {
    // A parent that keeps the pick drops it on an edit: the pick must come last.
    const search = vi.fn().mockResolvedValue([CUSTOMER]);
    const onPick = vi.fn();
    const onTextChange = vi.fn();
    render(
      <TermPicker
        ariaLabel="Find"
        search={search}
        onPick={onPick}
        onTextChange={onTextChange}
      />,
    );
    const input = screen.getByRole("combobox", { name: "Find" });
    type(input, "buyer");
    expect(onTextChange).toHaveBeenLastCalledWith("buyer");
    await elapse(TERM_SEARCH_DEBOUNCE_MS);
    fireEvent.mouseUp(screen.getByRole("option", { name: /Customer/ }));
    expect(onPick).toHaveBeenCalledWith(CUSTOMER);
    expect(onTextChange.mock.invocationCallOrder.at(-1)).toBeLessThan(
      onPick.mock.invocationCallOrder[0],
    );
    fireEvent.change(input, { target: { value: "Custo" } });
    expect(onTextChange).toHaveBeenLastCalledWith("Custo");
    expect(onPick).toHaveBeenCalledTimes(1);
  });

  it("stops a pending search when it goes away", async () => {
    const search = vi.fn().mockResolvedValue([]);
    const { unmount } = render(
      <TermPicker ariaLabel="Find" search={search} onPick={vi.fn()} />,
    );
    type(screen.getByRole("combobox", { name: "Find" }), "buyer");
    unmount();
    await elapse(TERM_SEARCH_DEBOUNCE_MS * 2);
    expect(search).not.toHaveBeenCalled();
  });
});
