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
import type { StudioQuestion } from "../../../services/studio";
import { QuestionsPanel } from "./QuestionsPanel";

const studio = vi.hoisted(() => ({
  listStudioQuestions: vi.fn(),
  putStudioQuestion: vi.fn(),
  deleteStudioQuestion: vi.fn(),
  checkStudioQuestion: vi.fn(),
}));
vi.mock("../../../services/studio", async (importOriginal) => {
  const real =
    await importOriginal<typeof import("../../../services/studio")>();
  return {
    ...real,
    ...Object.fromEntries(
      Object.entries(studio).map(([name, fn]) => [
        name,
        (...a: unknown[]) => fn(...a),
      ]),
    ),
  };
});

const api = {} as ApiClient;
const CHECKED: StudioQuestion = {
  questionId: "q-1",
  text: "Which medications did each patient receive?",
  expectation: "Patient and medication names",
  lastCheck: {
    proposalId: "p-1",
    revision: 4,
    status: "expressible",
    query: "SELECT ?p ?m WHERE { ?p ?r ?m }",
    rowCount: 0,
    sample: [],
    explanation: "No document mentions one yet.",
    checkedAt: "2026-10-21T09:10:00+00:00",
  },
  createdAt: "2026-10-21T09:00:00+00:00",
  updatedAt: "2026-10-21T09:00:00+00:00",
  updatedBy: "alice",
};
const NEW: StudioQuestion = {
  ...CHECKED,
  questionId: "q-2",
  text: "Who ships late?",
  expectation: null,
  lastCheck: null,
};

function renderPanel(
  overrides: Partial<React.ComponentProps<typeof QuestionsPanel>> = {},
) {
  const props = {
    apiClient: api,
    namespace: "ns",
    proposalId: "p-1",
    editable: true,
    onChecking: vi.fn(),
    onOpenQuery: vi.fn(),
    ...overrides,
  };
  return { props, ...render(<QuestionsPanel {...props} />) };
}

/** A button of a question's card, by its name, which says the question it acts on. */
function cardButton(
  action: "Check" | "Edit" | "Delete" | "Open query of",
  q: StudioQuestion = CHECKED,
): HTMLButtonElement {
  return screen.getByRole("button", {
    name: `${action} question "${q.text}"`,
  }) as HTMLButtonElement;
}

/** Disabled, or busy: Cloudscape marks a loading button aria-disabled. */
function isDisabled(button: HTMLButtonElement): boolean {
  return button.disabled || button.getAttribute("aria-disabled") === "true";
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("QuestionsPanel", () => {
  beforeEach(() => {
    for (const fn of Object.values(studio)) fn.mockReset();
    studio.listStudioQuestions.mockResolvedValue({ questions: [CHECKED] });
  });

  it("lists the questions with their verdict and opens a checked query", async () => {
    const { props } = renderPanel();
    expect(await screen.findByText(CHECKED.text)).toBeTruthy();
    expect(screen.getByText("Expressible, no data")).toBeTruthy();
    expect(screen.getByText(/No document mentions one yet/)).toBeTruthy();
    fireEvent.click(cardButton("Open query of"));
    expect(props.onOpenQuery).toHaveBeenCalledWith(CHECKED.lastCheck!.query);
  });

  it("reads every page of the questions", async () => {
    studio.listStudioQuestions
      .mockResolvedValueOnce({ questions: [CHECKED], nextToken: "page-2" })
      .mockResolvedValueOnce({ questions: [NEW] });
    renderPanel();
    expect(await screen.findByText(NEW.text)).toBeTruthy();
    expect(screen.getByText(CHECKED.text)).toBeTruthy();
    expect(studio.listStudioQuestions).toHaveBeenLastCalledWith(
      expect.anything(),
      expect.any(String),
      { maxResults: 100, nextToken: "page-2" },
    );
  });

  it("adds a question with a new id", async () => {
    studio.putStudioQuestion.mockResolvedValue(NEW);
    renderPanel();
    await screen.findByText(CHECKED.text);
    const box = screen.getAllByRole("textbox")[0];
    fireEvent.change(box, { target: { value: "Who ships late?" } });
    fireEvent.click(screen.getByRole("button", { name: "Add question" }));
    expect(
      await screen.findByText("Who ships late?", {
        selector: "*:not(textarea)",
      }),
    ).toBeTruthy();
    const [, ns, id, text, expectation] =
      studio.putStudioQuestion.mock.calls[0];
    expect([ns, text, expectation]).toEqual(["ns", "Who ships late?", null]);
    expect(id).toMatch(/^[0-9A-Za-z-]{1,64}$/);
  });

  it("checks a question and shows the chat, or says a turn is running", async () => {
    studio.checkStudioQuestion.mockResolvedValueOnce({ turnId: "t-1", seq: 3 });
    const { props } = renderPanel();
    await screen.findByText(CHECKED.text);
    fireEvent.click(cardButton("Check"));
    await waitFor(() => expect(props.onChecking).toHaveBeenCalled());
    expect(studio.checkStudioQuestion).toHaveBeenCalledWith(
      api,
      "ns",
      "p-1",
      "q-1",
    );

    studio.checkStudioQuestion.mockRejectedValueOnce(
      new ApiError(409, "TurnInProgress", "busy"),
    );
    fireEvent.click(cardButton("Check"));
    const busy = await screen.findByText(/still working on another message/);
    // Not a failure: the check waits for the turn that runs.
    expect(busy.closest('[class*="type-info"]')).not.toBeNull();
  });

  it("shows a save or a check that failed as an error", async () => {
    // A read-only role: the authorizer denies the PUT and the check's POST.
    const denied = new ApiError(403, "UNKNOWN_ERROR", "API PUT failed (403)", {
      Message:
        "User is not authorized to access this resource with an explicit deny",
    });
    studio.putStudioQuestion.mockRejectedValue(denied);
    studio.checkStudioQuestion.mockRejectedValue(denied);
    renderPanel();
    await screen.findByText(CHECKED.text);
    fireEvent.change(screen.getAllByRole("textbox")[0], {
      target: { value: "Who ships late?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add question" }));
    let failure = await screen.findByText(
      "You are not authorized to perform this action.",
    );
    expect(failure.closest('[class*="type-error"]')).not.toBeNull();
    expect(failure.closest('[class*="type-info"]')).toBeNull();

    fireEvent.click(cardButton("Check"));
    await waitFor(() =>
      expect(studio.checkStudioQuestion).toHaveBeenCalledTimes(1),
    );
    failure = await screen.findByText(
      "You are not authorized to perform this action.",
    );
    expect(failure.closest('[class*="type-error"]')).not.toBeNull();
  });

  it("lists the questions again when it is shown again, keeping a question being added", async () => {
    const { props, rerender } = renderPanel();
    await screen.findByText(CHECKED.text);
    fireEvent.change(screen.getAllByRole("textbox")[0], {
      target: { value: "Who ships late?" },
    });
    rerender(<QuestionsPanel {...props} active={false} />);
    expect(studio.listStudioQuestions).toHaveBeenCalledTimes(1);

    // A check run meanwhile stored a new verdict on the question.
    studio.listStudioQuestions.mockResolvedValue({
      questions: [
        {
          ...CHECKED,
          lastCheck: { ...CHECKED.lastCheck!, status: "answerable" },
        },
      ],
    });
    rerender(<QuestionsPanel {...props} active />);
    expect(await screen.findByText("Answerable")).toBeTruthy();
    expect(
      (screen.getAllByRole("textbox")[0] as HTMLTextAreaElement).value,
    ).toBe("Who ships late?");
  });

  it("deletes a question once the deletion is confirmed", async () => {
    const deleted = deferred<undefined>();
    studio.deleteStudioQuestion.mockReturnValue(deleted.promise);
    renderPanel();
    await screen.findByText(CHECKED.text);
    fireEvent.click(cardButton("Delete"));
    // Every draft shares the question and the deletion cannot be undone.
    const dialog = screen.getByRole("dialog", { name: "Delete question?" });
    expect(within(dialog).getByText(CHECKED.text)).toBeTruthy();
    expect(
      within(dialog).getByText(
        "It is removed, with its last check, for every draft of this namespace and cannot be undone.",
      ),
    ).toBeTruthy();
    expect(studio.deleteStudioQuestion).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(studio.deleteStudioQuestion).toHaveBeenCalledWith(api, "ns", "q-1");
    expect(
      screen.queryByRole("dialog", { name: "Delete question?" }),
    ).toBeNull();
    // While the deletion runs the card takes no other action.
    expect(isDisabled(cardButton("Delete"))).toBe(true);
    expect(cardButton("Edit").disabled).toBe(true);
    expect(cardButton("Check").disabled).toBe(true);
    await act(async () => deleted.resolve(undefined));
    expect(screen.queryByText(CHECKED.text)).toBeNull();
  });

  it("keeps the question when the deletion is cancelled", async () => {
    studio.listStudioQuestions.mockResolvedValue({ questions: [NEW] });
    renderPanel();
    await screen.findByText(NEW.text);
    fireEvent.click(cardButton("Delete", NEW));
    const dialog = screen.getByRole("dialog", { name: "Delete question?" });
    // A question never checked has no check to lose.
    expect(
      within(dialog).getByText(
        "It is removed for every draft of this namespace and cannot be undone.",
      ),
    ).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(
      screen.queryByRole("dialog", { name: "Delete question?" }),
    ).toBeNull();
    expect(screen.getByText(NEW.text)).toBeTruthy();
    expect(studio.deleteStudioQuestion).not.toHaveBeenCalled();
  });

  it("keeps a question whose deletion failed, and says why as an error", async () => {
    studio.deleteStudioQuestion.mockRejectedValue(
      new ApiError(500, "UNKNOWN_ERROR", "API DELETE failed (500)"),
    );
    renderPanel();
    await screen.findByText(CHECKED.text);
    fireEvent.click(cardButton("Delete"));
    fireEvent.click(
      within(
        screen.getByRole("dialog", { name: "Delete question?" }),
      ).getByRole("button", { name: "Delete" }),
    );
    const failure = await screen.findByText(/The request failed \(HTTP 500\)/);
    expect(failure.closest('[class*="type-error"]')).not.toBeNull();
    expect(screen.getByText(CHECKED.text)).toBeTruthy();
    expect(isDisabled(cardButton("Delete"))).toBe(false);
  });

  it("names the question in the accessible name of each of its buttons", async () => {
    studio.listStudioQuestions.mockResolvedValue({ questions: [CHECKED, NEW] });
    renderPanel();
    await screen.findByText(NEW.text);
    for (const q of [CHECKED, NEW])
      for (const action of ["Check", "Edit", "Delete"])
        expect(
          screen.getByRole("button", {
            name: `${action} question "${q.text}"`,
          }),
        ).toBeTruthy();
    // Only a checked question has a query to open.
    expect(
      screen.getAllByRole("button", { name: /^Open query of question / }),
    ).toHaveLength(1);
    expect(
      screen.getByRole("button", {
        name: `Open query of question "${CHECKED.text}"`,
      }),
    ).toBeTruthy();
  });

  it("is read-only for a draft no longer under review", async () => {
    renderPanel({ editable: false });
    await screen.findByText(CHECKED.text);
    expect(cardButton("Check").disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "Add question" })).toBeNull();
  });
});
