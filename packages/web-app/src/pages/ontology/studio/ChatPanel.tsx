// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The Chat tab of the Ontology designer: the conversation with the assistant
 * about the draft. A message starts a turn on the server. While it runs, the panel polls
 * its events every second and shows the assistant's steps, the change sets it
 * proposes (as cards the user previews, applies or discards) and its answer.
 *
 * The page owns writes to the draft: applying a change set goes through its
 * `onApply`, so revisions, conflicts and reloads work as for direct edits. A
 * query the assistant ran opens in the Query tab through `onOpenQuery`.
 */
import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import CloudscapeChatBubble from "@cloudscape-design/chat-components/chat-bubble";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Textarea from "@cloudscape-design/components/textarea";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ChatAvatar } from "@components/Chat";
import type { ApiClient } from "@components/ApiClientProvider";
import {
  discardStudioChangeSet,
  isTurnInProgress,
  listStudioChangeSets,
  listStudioEvents,
  sendStudioMessage,
  type StudioChangeSet,
  type StudioChangeSetPreview,
  type StudioChatEvent,
  type StudioTurn,
} from "../../../services/studio";
import { useAppI18n } from "../../../i18n/LocaleProvider";
import type { ConsoleQuery } from "./QueryConsole";
import { ChangeSetCard } from "./ChangeSetCard";
import {
  errorMessage,
  MAX_REQUEST_BYTES,
  requestBytes,
} from "./studio-helpers";
import { studioMessageLines } from "./studio-messages";

/** How often a running turn is polled. */
export const POLL_MS = 1000;
/** The longest message the engine takes. */
export const MAX_MESSAGE = 4000;
/** The most selected terms the engine takes with a message. */
export const MAX_SELECTION = 20;
/** Change sets read per request (the engine's maximum page). */
const CHANGE_SETS_PAGE = 50;

export interface ChatPanelProps {
  apiClient: ApiClient;
  namespace: string;
  proposalId: string;
  /** The draft accepts edits (and messages). */
  editable: boolean;
  /**
   * Why the caller's role may not change the draft: set, the message box, Send
   * and the change sets' buttons say so, and a change set cannot be discarded
   * either.
   */
  deniedReason?: string;
  /** A write to the draft is running. */
  busy: boolean;
  /** The draft's revision; a new one reloads the change sets. */
  revision: number;
  /** The terms selected in the graph, sent with a message. */
  selection: { iri: string; label: string }[];
  /** The change set whose preview is on the graph. */
  previewId: string | null;
  onPreview: (
    changeSet: StudioChangeSet,
    opIndexes: number[] | null,
  ) => Promise<StudioChangeSetPreview | null>;
  onHidePreview: () => void;
  /** Apply ops of a change set (all for null); resolves an error message, or null. */
  onApply: (
    changeSet: StudioChangeSet,
    opIndexes: number[] | null,
  ) => Promise<string | null>;
  /** A message to put in the box (from another tab); taken once, then `onPrefillTaken` is called. */
  prefill?: string | null;
  onPrefillTaken?: () => void;
  /** Open a query the assistant ran in the query console. */
  onOpenQuery?: (query: ConsoleQuery) => void;
  /**
   * The panel is on screen. It stays mounted while another tab is, and reads
   * what happened meanwhile when it is shown again: a question check starts a
   * turn from the Questions tab, and so can another window.
   */
  active?: boolean;
}

interface TurnView {
  turnId: string;
  events: StudioChatEvent[];
}

/** Events grouped by turn, in the order the turns started. */
function byTurn(events: StudioChatEvent[]): TurnView[] {
  const turns: TurnView[] = [];
  const index = new Map<string, TurnView>();
  for (const e of events) {
    let t = index.get(e.turnId);
    if (!t) {
      t = { turnId: e.turnId, events: [] };
      index.set(e.turnId, t);
      turns.push(t);
    }
    t.events.push(e);
  }
  return turns;
}

function initials(author: string): string {
  return (author.trim()[0] ?? "U").toUpperCase();
}

function Step({ event }: { event: StudioChatEvent }) {
  switch (event.type) {
    case "thinking":
      return (
        <Box variant="small" color="text-body-secondary">
          <i>{event.text}</i>
        </Box>
      );
    case "tool_call":
      return (
        <Box variant="small">
          <code>{event.tool}</code>
          {event.input ? ` ${event.input}` : ""}
        </Box>
      );
    case "tool_result":
      return (
        <StatusIndicator
          type={event.status === "success" ? "success" : "warning"}
        >
          <Box variant="small" display="inline">
            {event.summary}
          </Box>
        </StatusIndicator>
      );
    default:
      return null;
  }
}

const STEP_TYPES = new Set(["thinking", "tool_call", "tool_result"]);

export function ChatPanel({
  apiClient,
  namespace,
  proposalId,
  editable,
  deniedReason,
  busy,
  revision,
  selection,
  previewId,
  onPreview,
  onHidePreview,
  onApply,
  prefill = null,
  onPrefillTaken,
  onOpenQuery,
  active = true,
}: ChatPanelProps) {
  const { t } = useAppI18n();
  const [events, setEvents] = useState<StudioChatEvent[]>([]);
  const [turn, setTurn] = useState<StudioTurn | null>(null);
  const [changeSets, setChangeSets] = useState<Record<string, StudioChangeSet>>(
    {},
  );
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  // A send that failed is an error; one refused for a turn still running is not.
  const [notice, setNotice] = useState<{
    type: "info" | "error";
    text: string;
  } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Until the conversation has loaded it would look empty: the panel says it
  // is loading and Send waits, so no message goes out before the history it
  // follows is on screen.
  const [loaded, setLoaded] = useState(false);
  const lastSeq = useRef(0);
  // The read of the events in flight. Each read starts after it: two at once
  // would ask from the same seq, show its events twice, and the slower one's
  // older turn would replace the newer one.
  const reading = useRef<Promise<unknown>>(Promise.resolve());
  const mounted = useRef(true);
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (prefill === null) return;
    setText(prefill);
    onPrefillTaken?.();
  }, [prefill, onPrefillTaken]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  /** Reads every page of the change sets. */
  const loadChangeSets = useCallback(async () => {
    const listed: StudioChangeSet[] = [];
    let nextToken: string | undefined;
    do {
      const page = await listStudioChangeSets(
        apiClient,
        namespace,
        proposalId,
        {
          maxResults: CHANGE_SETS_PAGE,
          nextToken,
        },
      );
      listed.push(...page.changeSets);
      nextToken = page.nextToken;
    } while (nextToken);
    if (!mounted.current) return;
    setChangeSets(Object.fromEntries(listed.map((c) => [c.changeSetId, c])));
  }, [apiClient, namespace, proposalId]);

  /**
   * Reads every event after the last one shown, once the read in flight has
   * landed; resolves the new ones and the newest turn.
   */
  const fetchEvents = useCallback(() => {
    const read = reading.current.then(async () => {
      const fresh: StudioChatEvent[] = [];
      let after = lastSeq.current;
      let latest: StudioTurn | null;
      for (;;) {
        const page = await listStudioEvents(
          apiClient,
          namespace,
          proposalId,
          after,
        );
        fresh.push(...page.events);
        if (page.events.length > 0)
          after = page.events[page.events.length - 1].seq;
        latest = page.turn;
        if (!page.more || page.events.length === 0) break;
      }
      // Only once every page arrived: a read that failed half-way showed
      // none of its events, so the next one asks for them again.
      lastSeq.current = after;
      if (mounted.current) {
        if (fresh.length > 0) setEvents((prev) => [...prev, ...fresh]);
        setTurn(latest);
      }
      return { fresh, latest };
    });
    reading.current = read.catch(() => undefined);
    return read;
  }, [apiClient, namespace, proposalId]);

  useEffect(() => {
    if (!active) return;
    void (async () => {
      try {
        await fetchEvents();
        await loadChangeSets();
        if (mounted.current) setLoadError(null);
      } catch (e) {
        if (mounted.current) setLoadError(errorMessage(e, t));
      } finally {
        if (mounted.current) setLoaded(true);
      }
    })();
  }, [fetchEvents, loadChangeSets, t, active]);

  // A change set applied (or undone) moves the draft: its status follows.
  const firstRevision = useRef(true);
  useEffect(() => {
    if (firstRevision.current) {
      firstRevision.current = false;
      return;
    }
    void loadChangeSets().catch(() => undefined);
  }, [revision, loadChangeSets]);

  const running = turn?.status === "running";
  useEffect(() => {
    if (!running) return;
    let stopped = false;
    let handle = 0;
    const tick = async () => {
      try {
        const { fresh, latest } = await fetchEvents();
        if (
          !stopped &&
          (fresh.some((e) => e.type === "change_set") ||
            latest?.status !== "running")
        )
          await loadChangeSets();
      } catch {
        // A failed poll is tried again at the next tick.
      }
      if (!stopped) handle = window.setTimeout(() => void tick(), POLL_MS);
    };
    handle = window.setTimeout(() => void tick(), POLL_MS);
    return () => {
      stopped = true;
      window.clearTimeout(handle);
    };
  }, [running, fetchEvents, loadChangeSets]);

  // A hidden panel has no height to scroll to: events that arrived meanwhile
  // are scrolled to once it is shown again.
  useEffect(() => {
    if (!active) return;
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events, running, active]);

  async function send() {
    const message = text.trim();
    if (
      !message ||
      !loaded ||
      sending ||
      running ||
      !editable ||
      tooLong ||
      tooLarge
    )
      return;
    setSending(true);
    setNotice(null);
    try {
      await sendStudioMessage(
        apiClient,
        namespace,
        proposalId,
        message,
        sent.map((s) => s.iri),
      );
      setText("");
      await fetchEvents();
    } catch (e) {
      setNotice(
        isTurnInProgress(e)
          ? {
              type: "info",
              text: t(
                "The assistant is still working on the previous message.",
              ),
            }
          : { type: "error", text: errorMessage(e, t) },
      );
      if (isTurnInProgress(e)) await fetchEvents().catch(() => undefined);
    } finally {
      if (mounted.current) setSending(false);
    }
  }

  async function discard(cs: StudioChangeSet): Promise<string | null> {
    try {
      const updated = await discardStudioChangeSet(
        apiClient,
        namespace,
        proposalId,
        cs.changeSetId,
      );
      if (previewId === cs.changeSetId) onHidePreview();
      setChangeSets((all) => ({ ...all, [updated.changeSetId]: updated }));
      return null;
    } catch (e) {
      await loadChangeSets().catch(() => undefined);
      return errorMessage(e, t);
    }
  }

  const turns = useMemo(() => byTurn(events), [events]);
  const tooLong = text.length > MAX_MESSAGE;
  const sent = selection.slice(0, MAX_SELECTION);
  // Measured as sendStudioMessage sends it: past MAX_REQUEST_BYTES the WAF
  // would answer a bare 403.
  const tooLarge =
    !tooLong &&
    requestBytes({ text: text.trim(), selection: sent.map((s) => s.iri) }) >
      MAX_REQUEST_BYTES;
  // A change set whose chat event never landed (its turn was cut off) is still
  // listed, so it can be applied or discarded.
  const unannounced = useMemo(() => {
    const announced = new Set(
      events.flatMap((e) => (e.type === "change_set" ? [e.changeSetId] : [])),
    );
    return Object.values(changeSets).filter(
      (cs) => !announced.has(cs.changeSetId),
    );
  }, [events, changeSets]);

  function card(cs: StudioChangeSet, key: React.Key) {
    return (
      <ChangeSetCard
        key={key}
        changeSet={cs}
        editable={editable}
        deniedReason={deniedReason}
        busy={busy}
        previewing={previewId === cs.changeSetId}
        onPreview={(ops) => onPreview(cs, ops)}
        onHidePreview={onHidePreview}
        onApply={async (ops) => {
          const refusal = await onApply(cs, ops);
          // A refusal can have marked the change set stale.
          if (refusal) await loadChangeSets().catch(() => undefined);
          return refusal;
        }}
        onDiscard={() => discard(cs)}
      />
    );
  }

  return (
    <SpaceBetween size="s">
      {loadError && <Alert type="error">{loadError}</Alert>}
      <div
        ref={scroller}
        style={{ maxHeight: 560, overflowY: "auto", paddingRight: 4 }}
      >
        <SpaceBetween size="m">
          {!loaded && (
            <StatusIndicator type="loading">
              {t("Loading the conversation")}
            </StatusIndicator>
          )}
          {loaded && turns.length === 0 && !loadError && (
            <Box color="text-body-secondary">
              {t(
                "Ask the assistant to review or change the draft, for example “Medication needs a parent class Treatment”. It proposes change sets; nothing changes until you apply one.",
              )}
            </Box>
          )}
          {turns.map((tv) => {
            const steps = tv.events.filter((e) => STEP_TYPES.has(e.type));
            const live = running && turn?.turnId === tv.turnId;
            return (
              <SpaceBetween key={tv.turnId} size="xs">
                {tv.events.map((e) =>
                  e.type === "user_message" ? (
                    <CloudscapeChatBubble
                      key={e.seq}
                      type="outgoing"
                      ariaLabel={t("Message from {author}", {
                        author: e.author,
                      })}
                      avatar={
                        <ChatAvatar role="user" initials={initials(e.author)} />
                      }
                    >
                      <div style={{ whiteSpace: "pre-wrap" }}>{e.text}</div>
                      {e.selection.length > 0 && (
                        <SpaceBetween direction="horizontal" size="xxs">
                          {e.selection.map((s) => (
                            <Badge key={s.iri}>{s.label}</Badge>
                          ))}
                        </SpaceBetween>
                      )}
                    </CloudscapeChatBubble>
                  ) : null,
                )}
                {steps.length > 0 && (
                  <ExpandableSection
                    variant="footer"
                    headerText={t("Steps ({count})", {
                      count: steps.length,
                    })}
                  >
                    <SpaceBetween size="xxs">
                      {steps.map((e) => (
                        <Step key={e.seq} event={e} />
                      ))}
                    </SpaceBetween>
                  </ExpandableSection>
                )}
                {tv.events.map((e) =>
                  e.type === "query" ? (
                    <SpaceBetween
                      key={e.seq}
                      direction="horizontal"
                      size="xs"
                      alignItems="center"
                    >
                      <Box variant="small">
                        {t(
                          e.rowCount === 1
                            ? "Ran a {language} query: {count} row"
                            : "Ran a {language} query: {count} rows",
                          {
                            language:
                              e.language === "cypher" ? "Cypher" : "SPARQL",
                            count: e.rowCount,
                          },
                        )}
                      </Box>
                      {onOpenQuery && (
                        <Button
                          variant="inline-link"
                          onClick={() =>
                            onOpenQuery({
                              language: e.language,
                              target: e.target,
                              query: e.query,
                            })
                          }
                        >
                          {t("Open in Query console")}
                        </Button>
                      )}
                    </SpaceBetween>
                  ) : null,
                )}
                {tv.events.map((e) => {
                  if (e.type !== "change_set") return null;
                  const cs = changeSets[e.changeSetId];
                  return cs ? (
                    card(cs, e.seq)
                  ) : (
                    <Box key={e.seq} variant="small">
                      {t("Change set: {changes}", {
                        changes: studioMessageLines(
                          t,
                          e.opSummaryMessages,
                          e.opSummaries,
                        ).join("; "),
                      })}
                    </Box>
                  );
                })}
                {tv.events.map((e) =>
                  e.type === "answer" ? (
                    <CloudscapeChatBubble
                      key={e.seq}
                      type="incoming"
                      ariaLabel={t("Answer from the assistant")}
                      avatar={<ChatAvatar role="assistant" />}
                    >
                      <Markdown remarkPlugins={[remarkGfm]}>{e.text}</Markdown>
                    </CloudscapeChatBubble>
                  ) : e.type === "turn_ended" && e.status !== "completed" ? (
                    <StatusIndicator
                      key={e.seq}
                      type={e.status === "failed" ? "error" : "warning"}
                    >
                      {e.message ?? t("The turn ended early.")}
                    </StatusIndicator>
                  ) : null,
                )}
                {live && (
                  <CloudscapeChatBubble
                    type="incoming"
                    ariaLabel={t("The assistant is working")}
                    avatar={<ChatAvatar role="assistant" isLoading />}
                    showLoadingBar
                  >
                    <Box color="text-body-secondary">{t("Working…")}</Box>
                  </CloudscapeChatBubble>
                )}
              </SpaceBetween>
            );
          })}
          {unannounced.length > 0 && (
            <SpaceBetween size="xs">
              <Box variant="h4">{t("Other change sets")}</Box>
              {unannounced.map((cs) => card(cs, cs.changeSetId))}
            </SpaceBetween>
          )}
        </SpaceBetween>
      </div>
      {notice && (
        <Alert type={notice.type} dismissible onDismiss={() => setNotice(null)}>
          {notice.text}
        </Alert>
      )}
      <Textarea
        value={text}
        rows={3}
        ariaLabel={t("Message to the assistant")}
        placeholder={
          editable
            ? t(
                "Ask the assistant to review or change the draft… (Ctrl+Enter sends)",
              )
            : (deniedReason ?? t("The draft is read-only."))
        }
        disabled={!editable}
        invalid={tooLong || tooLarge}
        onChange={({ detail }) => setText(detail.value)}
        onKeyDown={({ detail }) => {
          if (detail.key === "Enter" && (detail.ctrlKey || detail.metaKey))
            void send();
        }}
      />
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        <Button
          variant="primary"
          iconName="send"
          loading={sending}
          disabled={
            !loaded ||
            !editable ||
            running ||
            !text.trim() ||
            tooLong ||
            tooLarge
          }
          disabledReason={deniedReason}
          onClick={() => void send()}
        >
          {t("Send")}
        </Button>
        {selection.length > 0 && (
          <Box variant="small" color="text-body-secondary">
            {selection.length > MAX_SELECTION
              ? t("Sent with: {labels} (the first {max} of {count} selected)", {
                  labels: sent.map((s) => s.label).join(", "),
                  max: MAX_SELECTION,
                  count: selection.length,
                })
              : t("Sent with: {labels}", {
                  labels: sent.map((s) => s.label).join(", "),
                })}
          </Box>
        )}
        {tooLong && (
          <Box variant="small" color="text-status-error">
            {t("A message is at most {max} characters.", { max: MAX_MESSAGE })}
          </Box>
        )}
        {tooLarge && (
          <Box variant="small" color="text-status-error">
            {t(
              "A message, with the terms sent with it, is at most 8 KB; Japanese, Korean and other non-Latin characters take up to 3 bytes each.",
            )}
          </Box>
        )}
      </SpaceBetween>
    </SpaceBetween>
  );
}
