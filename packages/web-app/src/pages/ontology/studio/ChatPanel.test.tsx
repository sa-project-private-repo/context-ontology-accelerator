// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  act,
  within,
} from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ApiError, type ApiClient } from "@components/ApiClientProvider";
import { LocaleProvider } from "../../../i18n/LocaleProvider";
import type {
  StudioChangeSet,
  StudioChatEvent,
  StudioEvents,
  StudioTurn,
} from "../../../services/studio";
import { ChatPanel, POLL_MS, type ChatPanelProps } from "./ChatPanel";

const studio = vi.hoisted(() => ({
  sendStudioMessage: vi.fn(),
  listStudioEvents: vi.fn(),
  listStudioChangeSets: vi.fn(),
  discardStudioChangeSet: vi.fn(),
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
const T1 = "t-1";

function turn(status: StudioTurn["status"], turnId = T1): StudioTurn {
  return { turnId, status, author: "alice", startedAt: null, endedAt: null };
}

function events(
  list: StudioChatEvent[],
  status: StudioTurn["status"] | null,
  more = false,
): StudioEvents {
  return {
    events: list,
    lastSeq: list.length ? list[list.length - 1].seq : 0,
    more,
    turn: status ? turn(status) : null,
  };
}

const ASK: StudioChatEvent = {
  seq: 1,
  turnId: T1,
  at: null,
  type: "user_message",
  text: "Add a Supplier class",
  author: "alice",
  selection: [{ iri: "http://e/o#Party", label: "Party", kind: "class" }],
};
const LOOK: StudioChatEvent[] = [
  { seq: 2, turnId: T1, at: null, type: "thinking", text: "Let me look." },
  {
    seq: 3,
    turnId: T1,
    at: null,
    type: "tool_call",
    tool: "find_terms",
    input: 'query="party"',
  },
  {
    seq: 4,
    turnId: T1,
    at: null,
    type: "tool_result",
    tool: "find_terms",
    status: "success",
    summary: "1 term for 'party'",
  },
];
const PROPOSED: StudioChatEvent = {
  seq: 5,
  turnId: T1,
  at: null,
  type: "change_set",
  changeSetId: "cs-1",
  opSummaries: ["Added class Supplier"],
};
const ANSWER: StudioChatEvent = {
  seq: 6,
  turnId: T1,
  at: null,
  type: "answer",
  text: "I proposed a **Supplier** class.",
};
const ENDED: StudioChatEvent = {
  seq: 7,
  turnId: T1,
  at: null,
  type: "turn_ended",
  status: "completed",
  message: null,
};

const CS: StudioChangeSet = {
  changeSetId: "cs-1",
  turnId: T1,
  status: "pending",
  createdAt: null,
  createdRevision: 2,
  createdBy: "alice",
  rationale: "Contracts name suppliers",
  ops: [{ op: "add_class", label: "Supplier" }],
  summary: ["Added class Supplier"],
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

function props(overrides: Partial<ChatPanelProps> = {}): ChatPanelProps {
  return {
    apiClient: api,
    namespace: "ns",
    proposalId: "p-1",
    editable: true,
    busy: false,
    revision: 2,
    selection: [],
    previewId: null,
    onPreview: vi.fn().mockResolvedValue(null),
    onHidePreview: vi.fn(),
    onApply: vi.fn().mockResolvedValue(null),
    ...overrides,
  };
}

/** Advance fake timers inside act, so the state updates of the polls they fire are flushed. */
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("ChatPanel", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    for (const fn of Object.values(studio)) fn.mockReset();
    studio.listStudioChangeSets.mockResolvedValue({
      revision: 2,
      changeSets: [],
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows the past conversation with its steps, card and answer", async () => {
    studio.listStudioEvents.mockResolvedValue(
      events([ASK, ...LOOK, PROPOSED, ANSWER, ENDED], "completed"),
    );
    studio.listStudioChangeSets.mockResolvedValue({
      revision: 2,
      changeSets: [CS],
    });
    render(<ChatPanel {...props()} />);
    expect(await screen.findByText("Add a Supplier class")).toBeTruthy();
    expect(screen.getByText("Party")).toBeTruthy(); // the selection sent with it
    expect(screen.getByText("Supplier")).toBeTruthy(); // the answer's Markdown
    expect(screen.getByText("Steps (3)")).toBeTruthy();
    expect(await screen.findByText("Contracts name suppliers")).toBeTruthy();
    expect(studio.listStudioEvents).toHaveBeenCalledWith(api, "ns", "p-1", 0);
  });

  it("reads every page of the past events", async () => {
    studio.listStudioEvents
      .mockResolvedValueOnce(events([ASK], "completed", true))
      .mockResolvedValueOnce(events([ANSWER, ENDED], "completed"));
    render(<ChatPanel {...props()} />);
    expect(await screen.findByText("Supplier")).toBeTruthy();
    expect(studio.listStudioEvents).toHaveBeenLastCalledWith(
      api,
      "ns",
      "p-1",
      1,
    );
  });

  it("says the conversation is loading and takes no message until it has", async () => {
    // "Ask the assistant" mounts the panel with a message in the box.
    const first = deferred<StudioEvents>();
    studio.listStudioEvents.mockReturnValueOnce(first.promise);
    render(
      <ChatPanel
        {...props({ prefill: "Explain the findings", onPrefillTaken: vi.fn() })}
      />,
    );
    expect(screen.getByText("Loading the conversation")).toBeTruthy();
    expect(screen.queryByText(/Ask the assistant to review/)).toBeNull();
    const box = screen.getByLabelText(
      "Message to the assistant",
    ) as HTMLTextAreaElement;
    await waitFor(() => expect(box.value).toBe("Explain the findings"));
    const send = screen.getByRole("button", {
      name: "Send",
    }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    fireEvent.keyDown(box, { key: "Enter", ctrlKey: true });
    expect(studio.sendStudioMessage).not.toHaveBeenCalled();

    await act(async () =>
      first.resolve(events([ASK, ANSWER, ENDED], "completed")),
    );
    expect(screen.queryByText("Loading the conversation")).toBeNull();
    expect(screen.getAllByText("Add a Supplier class")).toHaveLength(1);
    expect(send.disabled).toBe(false);
  });

  it("starts a read of the events only once the one in flight has landed", async () => {
    // StrictMode runs the mount effect twice, as a remount would.
    const reads: { after: number; resolve: (v: StudioEvents) => void }[] = [];
    studio.listStudioEvents.mockImplementation(
      (_api: unknown, _ns: unknown, _p: unknown, after: number) =>
        new Promise<StudioEvents>((resolve) => reads.push({ after, resolve })),
    );
    render(
      <React.StrictMode>
        <ChatPanel {...props()} />
      </React.StrictMode>,
    );
    await act(async () => undefined);
    expect(reads.map((r) => r.after)).toEqual([0]);
    await act(async () =>
      reads[0].resolve(events([ASK, ANSWER, ENDED], "completed")),
    );
    // The second read asks only for what the first one did not bring.
    await waitFor(() => expect(reads).toHaveLength(2));
    expect(reads[1].after).toBe(ENDED.seq);
    await act(async () => reads[1].resolve(events([], "completed")));
    expect(screen.getAllByText("Add a Supplier class")).toHaveLength(1);
    expect(screen.getAllByText("Supplier")).toHaveLength(1);
  });

  it("reads what happened while it was hidden when it is shown again", async () => {
    studio.listStudioEvents.mockResolvedValueOnce(
      events([ASK, ANSWER, ENDED], "completed"),
    );
    const p = props();
    const { rerender } = render(<ChatPanel {...p} />);
    await screen.findByText("Add a Supplier class");
    rerender(<ChatPanel {...p} active={false} />);
    expect(studio.listStudioEvents).toHaveBeenCalledTimes(1);

    // Meanwhile a question check started a turn from the Questions tab.
    studio.listStudioEvents.mockResolvedValueOnce({
      events: [
        {
          seq: 8,
          turnId: "t-2",
          at: null,
          type: "user_message",
          text: "Check the question",
          author: "alice",
          selection: [],
        },
      ],
      lastSeq: 8,
      more: false,
      turn: turn("running", "t-2"),
    });
    rerender(<ChatPanel {...p} active />);
    expect(
      await screen.findByLabelText("The assistant is working"),
    ).toBeTruthy();
    expect(studio.listStudioEvents).toHaveBeenLastCalledWith(
      api,
      "ns",
      "p-1",
      ENDED.seq,
    );
    expect(screen.getAllByText("Add a Supplier class")).toHaveLength(1);
  });

  it("scrolls to what arrived while it was hidden when it is shown again", async () => {
    studio.listStudioEvents.mockResolvedValueOnce(events([ASK], "running"));
    const p = props();
    const { container, rerender } = render(<ChatPanel {...p} />);
    await screen.findByLabelText("The assistant is working");
    // No layout here: the scroller's height is what a browser would report.
    // A hidden tab panel (display: none) has none.
    const scroller = container.querySelector<HTMLDivElement>(
      'div[style*="overflow-y: auto"]',
    )!;
    let height = 0;
    let top = 0;
    Object.defineProperty(scroller, "scrollHeight", { get: () => height });
    Object.defineProperty(scroller, "scrollTop", {
      get: () => top,
      set: (v: number) => {
        top = v;
      },
    });
    rerender(<ChatPanel {...p} active={false} />);
    // The answer lands while another tab is shown.
    studio.listStudioEvents.mockResolvedValueOnce(
      events([ANSWER, ENDED], "completed"),
    );
    await advance(POLL_MS);
    expect(screen.getByText("Supplier")).toBeTruthy();

    // Shown again; its read on showing brings nothing new.
    studio.listStudioEvents.mockResolvedValue(events([], "completed"));
    height = 900;
    rerender(<ChatPanel {...p} active />);
    await waitFor(() => expect(top).toBe(900));
  });

  it("polls every second while a turn runs and stops when it ends", async () => {
    studio.listStudioEvents.mockResolvedValueOnce(events([ASK], "running"));
    render(<ChatPanel {...props()} />);
    expect(
      await screen.findByLabelText("The assistant is working"),
    ).toBeTruthy();
    studio.listStudioEvents.mockResolvedValueOnce(events(LOOK, "running"));
    await advance(POLL_MS);
    expect(screen.getByText("Steps (3)")).toBeTruthy();
    expect(studio.listStudioEvents).toHaveBeenLastCalledWith(
      api,
      "ns",
      "p-1",
      1,
    );
    studio.listStudioChangeSets.mockResolvedValue({
      revision: 2,
      changeSets: [CS],
    });
    studio.listStudioEvents.mockResolvedValueOnce(
      events([PROPOSED, ANSWER, ENDED], "completed"),
    );
    await advance(POLL_MS);
    expect(await screen.findByText("Contracts name suppliers")).toBeTruthy();
    expect(screen.queryByLabelText("The assistant is working")).toBeNull();
    const calls = studio.listStudioEvents.mock.calls.length;
    await advance(POLL_MS * 3);
    expect(studio.listStudioEvents.mock.calls.length).toBe(calls);
  });

  it("sends the text with the selection, clears it and starts polling", async () => {
    studio.listStudioEvents.mockResolvedValueOnce(events([], null));
    render(
      <ChatPanel
        {...props({ selection: [{ iri: "http://e/o#Party", label: "Party" }] })}
      />,
    );
    expect(await screen.findByText(/Ask the assistant/)).toBeTruthy();
    expect(screen.getByText("Sent with: Party")).toBeTruthy();
    studio.sendStudioMessage.mockResolvedValue({ turnId: T1, seq: 1 });
    studio.listStudioEvents.mockResolvedValueOnce(events([ASK], "running"));
    const box = screen.getByLabelText("Message to the assistant");
    fireEvent.change(box, { target: { value: "Add a Supplier class" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() =>
      expect(studio.sendStudioMessage).toHaveBeenCalledWith(
        api,
        "ns",
        "p-1",
        "Add a Supplier class",
        ["http://e/o#Party"],
      ),
    );
    expect(
      await screen.findByLabelText("The assistant is working"),
    ).toBeTruthy();
    expect((box as HTMLTextAreaElement).value).toBe("");
    expect(
      (screen.getByRole("button", { name: "Send" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("says when another turn is still running", async () => {
    studio.listStudioEvents.mockResolvedValue(events([], null));
    render(<ChatPanel {...props()} />);
    await screen.findByText(/Ask the assistant/);
    studio.sendStudioMessage.mockRejectedValue(
      new ApiError(409, "TurnInProgress", "busy"),
    );
    fireEvent.change(screen.getByLabelText("Message to the assistant"), {
      target: { value: "Hello" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const busy = await screen.findByText(
      "The assistant is still working on the previous message.",
    );
    // Not a failure: the message waits for the turn that runs.
    expect(busy.closest('[class*="type-info"]')).not.toBeNull();
  });

  it("shows a message that could not be sent as an error", async () => {
    studio.listStudioEvents.mockResolvedValue(events([], null));
    render(<ChatPanel {...props()} />);
    await screen.findByText(/Ask the assistant/);
    // A read-only role: the authorizer denies the POST.
    studio.sendStudioMessage.mockRejectedValue(
      new ApiError(403, "UNKNOWN_ERROR", "API POST failed (403)", {
        Message:
          "User is not authorized to access this resource with an explicit deny",
      }),
    );
    fireEvent.change(screen.getByLabelText("Message to the assistant"), {
      target: { value: "Hello" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const failure = await screen.findByText(
      "You are not authorized to perform this action.",
    );
    expect(failure.closest('[class*="type-error"]')).not.toBeNull();
    expect(failure.closest('[class*="type-info"]')).toBeNull();
  });

  it("shows why a turn ended early", async () => {
    studio.listStudioEvents.mockResolvedValue(
      events(
        [
          ASK,
          {
            seq: 2,
            turnId: T1,
            at: null,
            type: "turn_ended",
            status: "failed",
            message:
              "The assistant stopped unexpectedly. Send your message again.",
          },
        ],
        "failed",
      ),
    );
    render(<ChatPanel {...props()} />);
    expect(
      await screen.findByText(
        "The assistant stopped unexpectedly. Send your message again.",
      ),
    ).toBeTruthy();
  });

  it("reads every page of the change sets", async () => {
    studio.listStudioEvents.mockResolvedValue(
      events([ASK, PROPOSED, ENDED], "completed"),
    );
    studio.listStudioChangeSets
      .mockResolvedValueOnce({ revision: 2, changeSets: [], nextToken: "n-1" })
      .mockResolvedValueOnce({ revision: 2, changeSets: [CS] });
    render(<ChatPanel {...props()} />);
    expect(await screen.findByText("Contracts name suppliers")).toBeTruthy();
    expect(studio.listStudioChangeSets).toHaveBeenLastCalledWith(
      api,
      "ns",
      "p-1",
      { maxResults: 50, nextToken: "n-1" },
    );
  });

  it("applies and discards a change set through its card", async () => {
    studio.listStudioEvents.mockResolvedValue(
      events([ASK, PROPOSED, ANSWER, ENDED], "completed"),
    );
    studio.listStudioChangeSets.mockResolvedValue({
      revision: 2,
      changeSets: [CS],
    });
    const p = props();
    const { rerender } = render(<ChatPanel {...p} />);
    await screen.findByText("Contracts name suppliers");
    fireEvent.click(screen.getByRole("button", { name: "Apply all" }));
    await waitFor(() => expect(p.onApply).toHaveBeenCalledWith(CS, null));
    studio.listStudioChangeSets.mockResolvedValue({
      revision: 3,
      changeSets: [{ ...CS, status: "applied", appliedRevision: 3 }],
    });
    rerender(<ChatPanel {...p} revision={3} />);
    expect(await screen.findByText("Applied in revision 3")).toBeTruthy();
  });

  it("discards a change set and hides its preview", async () => {
    studio.listStudioEvents.mockResolvedValue(
      events([ASK, PROPOSED, ENDED], "completed"),
    );
    studio.listStudioChangeSets.mockResolvedValue({
      revision: 2,
      changeSets: [CS],
    });
    studio.discardStudioChangeSet.mockResolvedValue({
      ...CS,
      status: "discarded",
    });
    const p = props({ previewId: "cs-1" });
    render(<ChatPanel {...p} />);
    const card = (await screen.findByText("Contracts name suppliers")).closest(
      "div",
    ) as HTMLElement;
    expect(card).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(await screen.findByText("Discarded")).toBeTruthy();
    expect(p.onHidePreview).toHaveBeenCalled();
    expect(
      within(document.body).queryByRole("button", { name: "Discard" }),
    ).toBeNull();
  });

  it("sends at most twenty selected terms and says so", async () => {
    studio.listStudioEvents.mockResolvedValue(events([], null));
    studio.sendStudioMessage.mockResolvedValue({ turnId: T1, seq: 1 });
    const selection = Array.from({ length: 22 }, (_, n) => ({
      iri: `http://e/o#C${n}`,
      label: `C${n}`,
    }));
    render(<ChatPanel {...props({ selection })} />);
    expect(
      await screen.findByText(/\(the first 20 of 22 selected\)/),
    ).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Message to the assistant"), {
      target: { value: "Review these" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() =>
      expect(studio.sendStudioMessage.mock.calls[0][4]).toHaveLength(20),
    );
  });

  it("lists a change set whose chat event never arrived", async () => {
    studio.listStudioEvents.mockResolvedValue(events([ASK, ENDED], "failed"));
    studio.listStudioChangeSets.mockResolvedValue({
      revision: 2,
      changeSets: [CS],
    });
    render(<ChatPanel {...props()} />);
    expect(await screen.findByText("Other change sets")).toBeTruthy();
    expect(screen.getByText("Contracts name suppliers")).toBeTruthy();
  });

  it("takes no message too large for the API, though under the character limit", async () => {
    studio.listStudioEvents.mockResolvedValue(events([], null));
    render(<ChatPanel {...props()} />);
    await screen.findByText(/Ask the assistant to review/);
    // 2,800 Hangul characters: fewer than 4,000, but over 8 KB as UTF-8.
    const box = screen.getByLabelText("Message to the assistant");
    fireEvent.change(box, { target: { value: "가".repeat(2800) } });
    expect(
      screen.getByText(
        /A message, with the terms sent with it, is at most 8 KB/,
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/at most 4000 characters/)).toBeNull();
    expect(
      (screen.getByRole("button", { name: "Send" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    fireEvent.keyDown(box, { key: "Enter", ctrlKey: true });
    expect(studio.sendStudioMessage).not.toHaveBeenCalled();
  });

  it("takes no message on a read-only draft", async () => {
    studio.listStudioEvents.mockResolvedValue(events([], null));
    render(<ChatPanel {...props({ editable: false })} />);
    await screen.findByText(/Ask the assistant/);
    expect(
      (screen.getByLabelText("Message to the assistant") as HTMLTextAreaElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Send" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });
  it("puts a message handed over from another tab in the box, once", async () => {
    studio.listStudioEvents.mockResolvedValue(events([], null));
    const onPrefillTaken = vi.fn();
    render(
      <ChatPanel
        {...props({ prefill: "Explain the findings", onPrefillTaken })}
      />,
    );
    const box = (await screen.findByLabelText(
      "Message to the assistant",
    )) as HTMLTextAreaElement;
    await waitFor(() => expect(box.value).toBe("Explain the findings"));
    expect(onPrefillTaken).toHaveBeenCalledTimes(1);
  });

  it("offers a query the assistant ran in the query console", async () => {
    const ran: StudioChatEvent = {
      seq: 2,
      turnId: T1,
      at: null,
      type: "query",
      language: "sparql",
      target: "draft",
      query: "SELECT ?c WHERE { ?c a ?t }",
      rowCount: 3,
    };
    studio.listStudioEvents.mockResolvedValue(
      events([ASK, ran, ANSWER, ENDED], "completed"),
    );
    const onOpenQuery = vi.fn();
    render(<ChatPanel {...props({ onOpenQuery })} />);
    expect(await screen.findByText(/Ran a SPARQL query: 3 rows/)).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: "Open in Query console" }),
    );
    expect(onOpenQuery).toHaveBeenCalledWith({
      language: "sparql",
      target: "draft",
      query: "SELECT ?c WHERE { ?c a ?t }",
    });
  });

  it("writes the line of a change set it has not read in the user's language", async () => {
    window.localStorage.setItem("scl.locale", "ja");
    const coded: StudioChatEvent = {
      ...PROPOSED,
      opSummaryMessages: [{ code: "ADD_CLASS", params: { label: "Supplier" } }],
    };
    // An event written before the codes keeps the engine's English line.
    const older: StudioChatEvent = {
      ...PROPOSED,
      seq: 6,
      changeSetId: "cs-2",
      opSummaries: ["Added class Vendor"],
    };
    // Read again once the locale is set: only the events after the last one shown.
    studio.listStudioEvents.mockImplementation(
      async (_api: ApiClient, _ns: string, _p: string, after: number) =>
        events(
          [ASK, coded, older, ENDED].filter((e) => e.seq > after),
          "completed",
        ),
    );
    try {
      render(
        <LocaleProvider>
          <ChatPanel {...props()} />
        </LocaleProvider>,
      );
      expect(
        await screen.findByText("変更セット: クラス Supplier を追加しました"),
      ).toBeTruthy();
      expect(screen.getByText("変更セット: Added class Vendor")).toBeTruthy();
    } finally {
      window.localStorage.clear();
      document.documentElement.lang = "";
    }
  });
});
