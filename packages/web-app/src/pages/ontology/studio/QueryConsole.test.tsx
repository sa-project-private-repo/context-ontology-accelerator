// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import {
  act,
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ApiError, type ApiClient } from "@components/ApiClientProvider";
import type {
  StudioQueryResult,
  VkgQueryResult,
} from "../../../services/studio";
import { QueryConsole, sampleQuery } from "./QueryConsole";

const studio = vi.hoisted(() => ({
  runStudioQuery: vi.fn(),
  runSparqlQuery: vi.fn(),
}));
vi.mock("../../../services/studio", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../services/studio")>()),
  runStudioQuery: (...a: unknown[]) => studio.runStudioQuery(...a),
  runSparqlQuery: (...a: unknown[]) => studio.runSparqlQuery(...a),
}));

const api = {} as ApiClient;
const NS = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d";
const RESULT: StudioQueryResult = {
  language: "sparql",
  target: "draft",
  revision: 3,
  form: "SELECT",
  columns: ["class", "label"],
  rows: [
    [
      { type: "uri", value: "http://e/o#Order" },
      { type: "literal", value: "Order", lang: "en" },
    ],
    [{ type: "uri", value: "http://e/o#Item" }, null],
  ],
  rowCount: 2,
  truncated: true,
  elapsedMs: 41,
};

function renderConsole(
  overrides: Partial<React.ComponentProps<typeof QueryConsole>> = {},
) {
  const props = {
    apiClient: api,
    namespace: NS,
    proposalId: "p-1",
    editable: true,
    prefill: null,
    onPrefillTaken: vi.fn(),
    onOpenTerm: vi.fn(),
    onAsk: vi.fn(),
    ...overrides,
  };
  return { props, ...render(<QueryConsole {...props} />) };
}

function editor(): HTMLTextAreaElement {
  return screen.getByLabelText("Query text") as HTMLTextAreaElement;
}

function runButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Run" }) as HTMLButtonElement;
}

/** Disabled, or busy: Cloudscape marks a loading button aria-disabled. */
function isDisabled(button: HTMLButtonElement): boolean {
  return button.disabled || button.getAttribute("aria-disabled") === "true";
}

/** Pick what the query reads (the Cloudscape Select opens on mousedown and picks on mouseup). */
function pickTarget(label: string) {
  fireEvent.mouseDown(
    screen.getByRole("button", { name: /What the query reads/ }),
  );
  const match = screen
    .getAllByRole("option")
    .find((o) => within(o).queryAllByText(label).length > 0);
  if (!match) throw new Error(`No option ${label}`);
  fireEvent.mouseUp(match);
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("QueryConsole", () => {
  beforeEach(() => {
    studio.runStudioQuery.mockReset().mockResolvedValue(RESULT);
    studio.runSparqlQuery.mockReset();
  });

  it("runs a draft query and shows its rows, IRIs as links to the term", async () => {
    const { props } = renderConsole();
    expect(editor().value).toBe(sampleQuery("draft", NS));
    fireEvent.change(editor(), {
      target: { value: "SELECT ?class ?label WHERE { }" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findAllByText("Order")).toHaveLength(2); // the IRI's tail and the literal
    expect(studio.runStudioQuery).toHaveBeenCalledWith(
      api,
      NS,
      "p-1",
      "sparql",
      "draft",
      "SELECT ?class ?label WHERE { }",
    );
    expect(screen.getByText(/2 rows, 41 ms, revision 3/)).toBeTruthy();
    expect(screen.getByText(/only the first rows are shown/)).toBeTruthy();
    expect(screen.getByText("—")).toBeTruthy(); // unbound
    fireEvent.click(screen.getByText("Item"));
    expect(props.onOpenTerm).toHaveBeenCalledWith("http://e/o#Item");
  });

  it("shows a refusal and offers it to the assistant", async () => {
    studio.runStudioQuery.mockRejectedValue(
      new ApiError(422, "QueryRefused", "422", {
        detail: { code: "QueryRefused", message: "GRAPH is not allowed" },
      }),
    );
    const { props } = renderConsole();
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("GRAPH is not allowed")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Ask the assistant" }));
    const text = (props.onAsk as ReturnType<typeof vi.fn>).mock
      .calls[0][0] as string;
    expect(text).toContain("```sparql");
    expect(text).toContain("It fails with: GRAPH is not allowed");
  });

  it("does not leave the previous query's rows under a refusal", async () => {
    renderConsole();
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText(/2 rows/)).toBeTruthy();
    studio.runStudioQuery.mockRejectedValue(
      new ApiError(422, "QueryRefused", "422", {
        detail: { code: "QueryRefused", message: "GRAPH is not allowed" },
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("GRAPH is not allowed")).toBeTruthy();
    expect(screen.queryByText(/2 rows/)).toBeNull();
  });

  it("runs a query on a read-only draft, but does not offer to ask the assistant", async () => {
    // Its chat takes no message.
    renderConsole({ editable: false });
    expect(
      (
        screen.getByRole("button", {
          name: "Ask the assistant",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText(/2 rows/)).toBeTruthy();
  });

  describe("once the proposal is no longer under review", () => {
    const CLOSED =
      "This proposal is accepted, so its draft can no longer be queried.";

    it("does not offer the draft, whose query the engine refuses, and starts on the VKG", async () => {
      studio.runSparqlQuery.mockResolvedValue({
        columns: ["s"],
        rows: [["Acme"]],
        rowCount: 1,
        truncated: false,
        executedSql: null,
      });
      renderConsole({ draftClosedReason: CLOSED });
      expect(editor().value).toBe(sampleQuery("vkg", NS));
      const select = screen.getByRole("button", {
        name: /What the query reads/,
      });
      expect(select.textContent).toContain("Database via VKG (SPARQL)");
      fireEvent.mouseDown(select);
      const [draft, lexical] = [
        "Draft + documents (SPARQL)",
        "Lexical graph (Cypher)",
      ].map((label) =>
        screen
          .getAllByRole("option")
          .find((o) => within(o).queryAllByText(label).length > 0),
      );
      expect(draft).toHaveAttribute("aria-disabled", "true");
      expect(lexical).toBeTruthy();
      expect(lexical).not.toHaveAttribute("aria-disabled", "true");
      // Picking it changes nothing.
      if (draft) fireEvent.mouseUp(draft);
      expect(select.textContent).toContain("Database via VKG (SPARQL)");
      fireEvent.click(runButton());
      expect(await screen.findByText("Acme")).toBeTruthy();
      expect(studio.runSparqlQuery).toHaveBeenCalledWith(
        api,
        NS,
        sampleQuery("vkg", NS),
      );
      expect(studio.runStudioQuery).not.toHaveBeenCalled();
    });

    it("shows a draft query handed over from the chat or a question, says why it cannot run, and runs it elsewhere", async () => {
      const query = "SELECT ?c WHERE { ?c a <http://e/o#Order> }";
      studio.runSparqlQuery.mockResolvedValue({
        columns: ["c"],
        rows: [["http://e/o#Order/1"]],
        rowCount: 1,
        truncated: false,
        executedSql: null,
      });
      renderConsole({
        draftClosedReason: CLOSED,
        prefill: { language: "sparql", target: "draft", query },
      });
      expect(editor().value).toBe(query);
      expect(isDisabled(runButton())).toBe(true);
      expect(runButton()).toHaveAccessibleDescription(CLOSED);
      fireEvent.click(runButton());
      fireEvent.keyDown(editor(), { key: "Enter", ctrlKey: true });
      expect(studio.runStudioQuery).not.toHaveBeenCalled();
      pickTarget("Database via VKG (SPARQL)");
      expect(isDisabled(runButton())).toBe(false);
      fireEvent.click(runButton());
      await waitFor(() =>
        expect(studio.runSparqlQuery).toHaveBeenCalledWith(api, NS, query),
      );
      expect(studio.runStudioQuery).not.toHaveBeenCalled();
    });
  });

  it("opens a query handed over from the chat on its target, once", async () => {
    const onPrefillTaken = vi.fn();
    renderConsole({
      prefill: {
        language: "cypher",
        target: "lexical",
        query: "MATCH (e) RETURN e",
      },
      onPrefillTaken,
    });
    expect(editor().value).toBe("MATCH (e) RETURN e");
    await waitFor(() => expect(onPrefillTaken).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() =>
      expect(studio.runStudioQuery).toHaveBeenCalledWith(
        api,
        NS,
        "p-1",
        "cypher",
        "lexical",
        "MATCH (e) RETURN e",
      ),
    );
  });

  it("drops the last query's rows when another query is opened in it", async () => {
    const { props, rerender } = renderConsole();
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText(/2 rows, 41 ms, revision 3/)).toBeTruthy();
    // Open in Query console, from the chat or a question.
    rerender(
      <QueryConsole
        {...props}
        prefill={{
          language: "sparql",
          target: "draft",
          query: "SELECT ?s WHERE { ?s ?p ?o }",
        }}
      />,
    );
    await waitFor(() =>
      expect(editor().value).toBe("SELECT ?s WHERE { ?s ?p ?o }"),
    );
    expect(screen.queryByText(/2 rows/)).toBeNull();
    expect(screen.queryByText("Item")).toBeNull();
  });

  it("drops the last run's rows, and a run still on its way, when the target changes", async () => {
    renderConsole();
    fireEvent.click(runButton());
    expect(await screen.findByText(/2 rows, 41 ms, revision 3/)).toBeTruthy();
    pickTarget("Lexical graph (Cypher)");
    // The draft's rows would read as the lexical graph's.
    expect(screen.queryByText(/2 rows/)).toBeNull();
    expect(screen.queryByText("Item")).toBeNull();
    const slow = deferred<StudioQueryResult>();
    studio.runStudioQuery.mockReturnValue(slow.promise);
    fireEvent.click(runButton());
    pickTarget("Draft + documents (SPARQL)");
    expect(isDisabled(runButton())).toBe(false);
    // The lexical graph's answer comes only now, under the draft.
    await act(async () => slow.resolve(RESULT));
    expect(screen.queryByText(/2 rows/)).toBeNull();
  });

  it("drops a run still on its way when another query is opened, and runs the opened one", async () => {
    const slow = deferred<VkgQueryResult>();
    studio.runSparqlQuery.mockReturnValue(slow.promise);
    const { props, rerender } = renderConsole({
      prefill: {
        language: "sparql",
        target: "vkg",
        query: "SELECT ?name { ?c ?p ?name }",
      },
    });
    fireEvent.click(runButton());
    // Open in Query console, from the chat or a question, while Run spins.
    const opened = "SELECT ?s WHERE { ?s ?p ?o }";
    rerender(
      <QueryConsole
        {...props}
        prefill={{ language: "sparql", target: "draft", query: opened }}
      />,
    );
    await waitFor(() => expect(editor().value).toBe(opened));
    expect(isDisabled(runButton())).toBe(false);
    const next = deferred<StudioQueryResult>();
    studio.runStudioQuery.mockReturnValue(next.promise);
    fireEvent.click(runButton());
    expect(isDisabled(runButton())).toBe(true);
    // The VKG query answers only now.
    await act(async () =>
      slow.resolve({
        columns: ["name"],
        rows: [["Acme"]],
        rowCount: 1,
        truncated: false,
        executedSql: "SELECT name FROM customer",
      }),
    );
    expect(screen.queryByText("Acme")).toBeNull();
    expect(screen.queryByText("SQL that ran")).toBeNull();
    expect(isDisabled(runButton())).toBe(true);
    await act(async () => next.resolve(RESULT));
    expect(screen.getByText(/2 rows, 41 ms, revision 3/)).toBeTruthy();
    expect(isDisabled(runButton())).toBe(false);
  });

  it("drops the error of a run still on its way when another query is opened, and does not ask about it", async () => {
    const slow = deferred<StudioQueryResult>();
    studio.runStudioQuery.mockReturnValue(slow.promise);
    const { props, rerender } = renderConsole();
    fireEvent.click(runButton());
    const opened = "SELECT ?s WHERE { ?s ?p ?o }";
    rerender(
      <QueryConsole
        {...props}
        prefill={{ language: "sparql", target: "draft", query: opened }}
      />,
    );
    await waitFor(() => expect(editor().value).toBe(opened));
    await act(async () =>
      slow.reject(
        new ApiError(422, "QueryRefused", "422", {
          detail: { code: "QueryRefused", message: "GRAPH is not allowed" },
        }),
      ),
    );
    expect(screen.queryByText("GRAPH is not allowed")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Ask the assistant" }));
    const text = (props.onAsk as ReturnType<typeof vi.fn>).mock
      .calls[0][0] as string;
    expect(text).toContain(opened);
    expect(text).not.toContain("It fails with");
  });

  it("runs a VKG query through the data layer and shows the SQL that ran", async () => {
    studio.runSparqlQuery.mockResolvedValue({
      columns: ["name"],
      rows: [["Acme"], [7]],
      rowCount: 2,
      truncated: false,
      executedSql: "SELECT name FROM customer",
    });
    renderConsole({
      prefill: {
        language: "sparql",
        target: "vkg",
        query: "SELECT ?name { ?c ?p ?name }",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("Acme")).toBeTruthy();
    expect(screen.getByText("7")).toBeTruthy();
    expect(studio.runSparqlQuery).toHaveBeenCalledWith(
      api,
      NS,
      "SELECT ?name { ?c ?p ?name }",
    );
    expect(screen.getByText("SQL that ran")).toBeTruthy();
  });

  it("refuses a query too large for the API, though under the character limit", () => {
    const { props, rerender } = renderConsole();
    const run = () =>
      screen.getByRole("button", { name: "Run" }) as HTMLButtonElement;
    // 2,700 Hangul characters: far fewer than 20,000, but over 8 KB as UTF-8.
    fireEvent.change(editor(), {
      target: { value: `SELECT ?s WHERE { ?s ?p "${"가".repeat(2700)}" }` },
    });
    expect(screen.getByText(/A query is at most 8 KB/)).toBeTruthy();
    expect(screen.queryByText(/at most 20000 characters/)).toBeNull();
    expect(run().disabled).toBe(true);
    fireEvent.keyDown(editor(), { key: "Enter", ctrlKey: true });
    expect(studio.runStudioQuery).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Insert sample" }));
    expect(run().disabled).toBe(false);

    // A VALUES list of 400 IRIs: 8,833 ASCII characters, over 8 KB on the VKG too.
    const iris = Array.from(
      { length: 400 },
      (_, n) => `<http://e/o#cust${String(n).padStart(4, "0")}>`,
    ).join(" ");
    rerender(
      <QueryConsole
        {...props}
        prefill={{
          language: "sparql",
          target: "vkg",
          query: `SELECT ?s WHERE { VALUES ?s { ${iris} } }`,
        }}
      />,
    );
    expect(screen.getByText(/A query is at most 8 KB/)).toBeTruthy();
    expect(run().disabled).toBe(true);
    fireEvent.keyDown(editor(), { key: "Enter", ctrlKey: true });
    expect(studio.runSparqlQuery).not.toHaveBeenCalled();
  });

  it("explains a request the API's WAF blocked instead of saying Forbidden", async () => {
    studio.runSparqlQuery.mockRejectedValue(
      new ApiError(403, "UNKNOWN_ERROR", "Forbidden", { message: "Forbidden" }),
    );
    renderConsole({
      prefill: { language: "sparql", target: "vkg", query: "ASK { ?s ?p ?o }" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(
      await screen.findByText(
        /^The request was blocked before it reached the server, most likely because it is larger than 8 KB/,
      ),
    ).toBeTruthy();
    expect(screen.queryByText("Forbidden")).toBeNull();
  });

  it("shows why the VKG refused a query, as the data layer wrote it", async () => {
    const message =
      "<http://e/o#customers_loyaltyTier> has no table mapping, so the VKG cannot answer it.";
    studio.runSparqlQuery.mockRejectedValue(
      new ApiError(400, "UNKNOWN_ERROR", message, {
        message,
        error: "ValidationError",
      }),
    );
    renderConsole({
      prefill: {
        language: "sparql",
        target: "vkg",
        query: "SELECT ?t WHERE { ?c <http://e/o#customers_loyaltyTier> ?t }",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText(message)).toBeTruthy();
  });

  it("puts the namespace's tenant label in the Cypher sample", () => {
    expect(sampleQuery("lexical", NS)).toContain(
      "`__Entity__a1b2c3d4e5f64a7b8c9d0e1f2__`",
    );
  });
});
