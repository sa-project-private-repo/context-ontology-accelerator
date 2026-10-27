// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The Ontology designer: edit a proposal's draft directly, or with the
 * assistant. The graph and the Inspector send typed change sets; the Chat
 * tab's assistant proposes change sets that the user previews on the graph
 * and applies. The Checks, Query and Questions tabs check the draft, query it
 * and test it against the namespace's competency questions. Every write names
 * the revision on screen and a 409 DraftConflict reloads the draft. Accept
 * publishes the reviewed revision as the next version.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import { useCanManageOntology } from "@api-hooks/use-can-manage-ontology";
import { useNamespace } from "@api-hooks/use-namespace";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Checkbox from "@cloudscape-design/components/checkbox";
import Container from "@cloudscape-design/components/container";
import Flashbar, {
  type FlashbarProps,
} from "@cloudscape-design/components/flashbar";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import RadioGroup from "@cloudscape-design/components/radio-group";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import Tabs from "@cloudscape-design/components/tabs";
import {
  ApiError,
  useApiClient,
  type ApiClient,
} from "@components/ApiClientProvider";
import {
  acceptProposal,
  getDomainAbox,
  getProposal,
  listOntologyVersionsPage,
  type Proposal,
} from "../../../services/ontology-engine";
import {
  applyStudioChangeSet,
  applyStudioChanges,
  getStudioDraft,
  getStudioGraph,
  getStudioTerm,
  isDraftConflict,
  isRebaseRequired,
  listStudioHistory,
  openStudioDraft,
  previewStudioChangeSet,
  rebaseFailures,
  rebaseStudioDraft,
  redoStudioChange,
  searchStudioTerms,
  studioErrorDetail,
  undoStudioChange,
  type StudioAcceptPreview,
  type StudioChangeResult,
  type StudioChangeSet,
  type StudioChangeSetPreview,
  type StudioDraft,
  type StudioGraph as StudioGraphData,
  type StudioHistory,
  type StudioOp,
  type StudioPreviewGraph,
  type StudioRebaseFailure,
  type StudioTerm,
  type StudioTermHit,
  type StudioTermKind,
} from "../../../services/studio";
import { useAppI18n, type AppTranslate } from "../../../i18n/LocaleProvider";
import { proposalStatusText } from "../../OntologyPage";
import { isPolling } from "../abox/abox-helpers";
import { isAcceptTerminalFailure } from "../induce/helpers";
import { autoDeployHeldMessage } from "../versions/versions-helpers";
import { AcceptDialog } from "./AcceptDialog";
import { ChatPanel } from "./ChatPanel";
import { ChecksPanel } from "./ChecksPanel";
import { HistoryPanel } from "./HistoryPanel";
import { Inspector } from "./Inspector";
import { QueryConsole, type ConsoleQuery } from "./QueryConsole";
import { QuestionsPanel } from "./QuestionsPanel";
import { StudioGraph, type GraphAction } from "./StudioGraph";
import {
  conflictMessage,
  errorMessage,
  iriTail,
  MAX_REQUEST_BYTES,
  requestBytes,
  revisionLine,
} from "./studio-helpers";
import { studioMessageLines } from "./studio-messages";
import { TermPicker } from "./TermPicker";

const ACCEPT_POLL_MS = 2000;
const ACCEPT_TIMEOUT_MS = 5 * 60 * 1000;
/** The accept worker records the auto-deploy outcome a little after "accepted". */
const DEPLOY_OUTCOME_WAIT_MS = 30 * 1000;
/** While the instance counts are dimmed, how often the domain data is read again, and for how long. */
const ABOX_POLL_MS = 15 * 1000;
const ABOX_POLL_TIMEOUT_MS = 10 * 60 * 1000;
const SEARCH_LIMIT = 15;
/** The proposal statuses a draft opens from. */
const OPENABLE_STATUSES = ["pending", "updated", "accept_failed"];
/** The ops that create a term; each creates exactly one, in order. */
const CREATING_OPS: ReadonlySet<StudioOp["op"]> = new Set([
  "add_class",
  "add_object_property",
  "add_datatype_property",
]);

type Dialog =
  | { kind: "addSubclass"; iri: string; label: string; text: string }
  | {
      kind: "addRelationship";
      iri: string;
      label: string;
      text: string;
      target: StudioTermHit | null;
    }
  | { kind: "rename"; iri: string; label: string; text: string }
  | {
      kind: "delete";
      iris: string[];
      label: string;
      cascade: "detach" | "delete";
    }
  | {
      kind: "merge";
      iris: [string, string];
      labels: [string, string];
      target: string;
    };

/** The versions a rebase prompt names. */
type Versions = Pick<StudioDraft, "baseVersion" | "headVersion">;

/** A write was applied, or the page reloaded because the draft moved on or locked, or it was refused. */
type WriteOutcome = "applied" | "reloaded" | "failed";

/** The term the Inspector shows, or why it could not be read. */
interface ShownTerm {
  iri: string;
  term: StudioTerm | null;
  error: string | null;
}

function versionText(version: number | null, t: AppTranslate): string {
  return version === null ? t("no version") : t("v{version}", { version });
}

function versionOf(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

/** The flash for an accepted proposal: its version, and what became of the deployment it asked for. */
function acceptedFlash(
  p: Proposal,
  deploy: boolean,
  waiting: boolean,
  t: AppTranslate,
): [FlashbarProps.Type, string] {
  const version = p.published_version;
  if (version == null) return ["success", t("Accepted.")];
  const published = t("Published as version v{version}.", { version });
  if (!deploy)
    return [
      "success",
      `${published} ${t("Deploy it from Ontology › Induction › Versions.")}`,
    ];
  if (p.auto_deploy_held)
    return [
      "warning",
      `${t("Published as version v{version}, but not deployed.", { version })} ${autoDeployHeldMessage(p.auto_deploy_held, t)}`,
    ];
  if (p.auto_deploy_id)
    return [
      "success",
      `${published} ${t("Deployment started. Follow it in Ontology › Induction › Versions.")}`,
    ];
  return [
    "success",
    `${published} ${
      waiting
        ? t("Waiting for the deployment to start…")
        : t(
            "Deployment requested. Check Ontology › Induction › Versions for its status.",
          )
    }`,
  ];
}

/**
 * A selection of several classes holds only the ones on screen, so "Delete
 * selected" and "Merge…" never reach a class the user cannot see. One IRI is
 * left alone: it can be a property or a class the Inspector opened.
 */
function onScreen(ids: string[], graph: StudioGraphData): string[] {
  if (ids.length < 2) return ids;
  const present = new Set(graph.nodes.map((n) => n.iri));
  const kept = ids.filter((i) => present.has(i));
  return kept.length === ids.length ? ids : kept;
}

/**
 * The selection once `ops` were applied: a class they created becomes it, a
 * renamed or merged term is followed to its new IRI, a deleted one leaves.
 */
function selectionAfter(
  selected: string[],
  ops: StudioOp[],
  created: string[],
): string[] {
  const newClass = ops
    .filter((o) => CREATING_OPS.has(o.op))
    .findIndex((o) => o.op === "add_class");
  if (newClass >= 0 && created[newClass]) return [created[newClass]];
  let next = selected;
  for (const op of ops) {
    if (op.op === "change_iri") {
      next = next.map((i) => (i === op.iri ? op.newIri : i));
    } else if (op.op === "merge_classes") {
      next = [...new Set(next.map((i) => (i === op.source ? op.target : i)))];
    } else if (op.op === "delete_class" || op.op === "delete_property") {
      next = next.filter((i) => i !== op.iri);
    }
  }
  const same =
    next.length === selected.length && next.every((i, n) => i === selected[n]);
  return same ? selected : next;
}

function dialogHeader(dialog: Dialog | null, t: AppTranslate): string {
  switch (dialog?.kind) {
    case "addSubclass":
      return t("Add a subclass of {label}", { label: dialog.label });
    case "addRelationship":
      return t("Add a relationship from {label}", { label: dialog.label });
    case "rename":
      return t("Rename {label}", { label: dialog.label });
    case "delete":
      return t("Delete {label}", { label: dialog.label });
    case "merge":
      return t("Merge classes");
    default:
      return "";
  }
}

function dialogReady(dialog: Dialog): boolean {
  switch (dialog.kind) {
    case "addSubclass":
      return !!dialog.text.trim();
    case "addRelationship":
      return !!dialog.text.trim() && dialog.target !== null;
    case "rename":
      return !!dialog.text.trim() && dialog.text.trim() !== dialog.label;
    default:
      return true;
  }
}

/**
 * The change set a confirmed dialog sends (field names as in the engine's
 * ops.py). `lang` is the language of the label a renamed class shows.
 */
function dialogOps(dialog: Dialog, lang?: string | null): StudioOp[] {
  switch (dialog.kind) {
    case "addSubclass":
      return [
        { op: "add_class", label: dialog.text.trim(), parents: [dialog.iri] },
      ];
    case "addRelationship":
      return dialog.target
        ? [
            {
              op: "add_object_property",
              label: dialog.text.trim(),
              domain: dialog.iri,
              range: dialog.target.iri,
            },
          ]
        : [];
    case "rename":
      return [
        {
          op: "set_label",
          iri: dialog.iri,
          label: dialog.text.trim(),
          ...(lang ? { lang } : {}),
        },
      ];
    case "delete":
      return dialog.iris.map((iri) => ({
        op: "delete_class",
        iri,
        cascade: dialog.cascade,
      }));
    case "merge":
      return [
        {
          op: "merge_classes",
          source:
            dialog.iris.find((i) => i !== dialog.target) ?? dialog.iris[0],
          target: dialog.target,
        },
      ];
  }
}

export function StudioRoute() {
  const { namespaceId, proposalId } = useParams<{
    namespaceId: string;
    proposalId: string;
  }>();
  const apiClient = useApiClient();
  if (!namespaceId || !proposalId) return null;
  return (
    <StudioPage
      key={`${namespaceId}/${proposalId}`}
      apiClient={apiClient}
      namespace={namespaceId}
      proposalId={proposalId}
    />
  );
}

export interface StudioPageProps {
  apiClient: ApiClient;
  namespace: string;
  proposalId: string;
}

export function StudioPage({
  apiClient,
  namespace,
  proposalId,
}: StudioPageProps) {
  const navigate = useNavigate();
  const { t } = useAppI18n();
  const { name: nsName } = useNamespace(namespace);
  useBreadcrumbTrail([
    { text: nsName, href: `/namespaces/${namespace}`, namespaceId: namespace },
    { text: t("Ontology") },
    {
      text: t("Induction"),
      href: `/namespaces/${namespace}/ontology`,
      namespaceId: namespace,
    },
    {
      text: proposalId,
      href: `/namespaces/${namespace}/ontology/proposals/${proposalId}`,
      namespaceId: namespace,
    },
    { text: t("Ontology designer") },
  ]);
  // A role that only reads (a data analyst) is not offered what the API would refuse it.
  const canChange = useCanManageOntology(namespace);
  const denied = canChange
    ? undefined
    : t("Your role on this namespace does not allow this.");
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [draft, setDraft] = useState<StudioDraft | null>(null);
  const [graph, setGraph] = useState<StudioGraphData | null>(null);
  const [notOpen, setNotOpen] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [shown, setShown] = useState<ShownTerm | null>(null);
  const [history, setHistory] = useState<StudioHistory | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState<FlashbarProps.MessageDefinition[]>([]);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [tab, setTab] = useState("inspector");
  // What another tab hands to the chat or the query console when it opens it.
  const [chatPrefill, setChatPrefill] = useState<string | null>(null);
  const [queryPrefill, setQueryPrefill] = useState<ConsoleQuery | null>(null);
  const takeChatPrefill = useCallback(() => setChatPrefill(null), []);
  const takeQueryPrefill = useCallback(() => setQueryPrefill(null), []);
  const [failures, setFailures] = useState<StudioRebaseFailure[] | null>(null);
  const [skips, setSkips] = useState<string[]>([]);
  const [rebaseOffer, setRebaseOffer] = useState<Versions | null>(null);
  const [acceptOpen, setAcceptOpen] = useState(false);
  const [accepting, setAccepting] = useState(false);
  // The served version: after a rollback it is older than the one the next version builds on.
  const [activeVersion, setActiveVersion] = useState<number | null>(null);
  // The change set previewed on the graph, with the graph after it.
  const [preview, setPreview] = useState<{
    changeSetId: string;
    graph: StudioPreviewGraph;
  } | null>(null);
  // The class the user focused on; null asks for the graph without a focus.
  const focus = useRef<string | null>(null);
  // Each bumped per request: only the latest answer is shown.
  const loadRequest = useRef(0);
  const historyRequest = useRef(0);
  const activeRequest = useRef(0);
  const termRequest = useRef(0);
  // The term the Inspector wants and the revision on screen, as of the last
  // render: a reload that finds another revision reads that term again.
  const inspected = useRef<{ iri: string | null; revision?: number }>({
    iri: null,
  });
  // `busy` only takes effect on the next render; this is set at once, so a
  // second write cannot start before the first one has disabled the forms.
  const writing = useRef(false);
  const flashId = useRef(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const notify = useCallback(
    (type: FlashbarProps.Type, content: string) => {
      const id = `studio-${++flashId.current}`;
      setFlash([
        {
          id,
          type,
          content,
          dismissible: true,
          dismissLabel: t("Dismiss"),
          onDismiss: () => setFlash([]),
        },
      ]);
    },
    [t],
  );

  const loadHistory = useCallback(async () => {
    const request = ++historyRequest.current;
    setHistoryLoading(true);
    try {
      const h = await listStudioHistory(apiClient, namespace, proposalId);
      if (request !== historyRequest.current) return;
      setHistory(h);
      setHistoryError(null);
    } catch (e) {
      if (request === historyRequest.current)
        setHistoryError(errorMessage(e, t));
    } finally {
      if (request === historyRequest.current) setHistoryLoading(false);
    }
  }, [apiClient, namespace, proposalId, t]);

  // Appends the next (older) page; a reload of the first page supersedes it.
  const loadOlderHistory = useCallback(async () => {
    const nextToken = history?.nextToken;
    if (!nextToken) return;
    const request = ++historyRequest.current;
    setHistoryLoading(true);
    try {
      const page = await listStudioHistory(apiClient, namespace, proposalId, {
        nextToken,
      });
      if (request !== historyRequest.current) return;
      setHistory((h) =>
        h ? { ...page, entries: [...h.entries, ...page.entries] } : page,
      );
      setHistoryError(null);
    } catch (e) {
      if (request === historyRequest.current)
        setHistoryError(errorMessage(e, t));
    } finally {
      if (request === historyRequest.current) setHistoryLoading(false);
    }
  }, [apiClient, namespace, proposalId, history?.nextToken, t]);

  /**
   * Reads the active version again: a deploy or a rollback made elsewhere changes
   * it, as a publish there changes the head the draft reports. Best effort:
   * without it the page only leaves out the notice.
   */
  const loadActiveVersion = useCallback(async () => {
    const request = ++activeRequest.current;
    try {
      const page = await listOntologyVersionsPage(apiClient, namespace, {
        maxResults: 1,
      });
      if (mounted.current && request === activeRequest.current)
        setActiveVersion(page.activeVersion ?? null);
    } catch {
      // Keeps the last one read.
    }
  }, [apiClient, namespace]);

  /**
   * Reads the Inspector's term `iri`. Resolves what shows the answer: the
   * term, why it could not be read, or nothing to inspect when the draft no
   * longer has it. Only the newest read shows anything. `quiet`: a read that
   * fails for another reason rejects instead.
   */
  const readTerm = useCallback(
    async (iri: string, quiet = false): Promise<() => void> => {
      const request = ++termRequest.current;
      let show: () => void;
      try {
        const term = await getStudioTerm(apiClient, namespace, proposalId, iri);
        show = () => setShown({ iri, term, error: null });
      } catch (e) {
        if (e instanceof ApiError && e.code === "TermNotFound") {
          // No longer in the draft (deleted elsewhere, undone): nothing to inspect.
          show = () => {
            setSelected((s) => (s.length === 1 && s[0] === iri ? [] : s));
            setShown(null);
          };
        } else if (quiet) {
          throw e;
        } else {
          const error = errorMessage(e, t);
          show = () => setShown({ iri, term: null, error });
        }
      }
      return () => {
        if (request === termRequest.current) show();
      };
    },
    [apiClient, namespace, proposalId, t],
  );

  /**
   * Reads the proposal, the draft, its graph and history again. `nextFocus`
   * moves the focus (null: none); left out, the focus stays. `background`
   * (the domain data check) reads the Inspector's term on the same revision
   * too, as its instance count changes with the domain data, leaves out the
   * history, which does not, and keeps the page as it is when a read fails.
   * Resolves the draft now on screen, or null when this load was not shown.
   */
  const reload = useCallback(
    async (
      nextFocus?: string | null,
      { background = false }: { background?: boolean } = {},
    ): Promise<StudioDraft | null> => {
      if (nextFocus !== undefined) focus.current = nextFocus;
      const request = ++loadRequest.current;
      const latest = () => mounted.current && request === loadRequest.current;
      const fetchGraph = (at: string | null) =>
        at === null
          ? getStudioGraph(apiClient, namespace, proposalId)
          : getStudioGraph(apiClient, namespace, proposalId, at);
      try {
        const [p, d] = await Promise.all([
          getProposal(apiClient, namespace, proposalId),
          getStudioDraft(apiClient, namespace, proposalId).catch(
            (e: unknown) => {
              if (e instanceof ApiError && e.code === "StudioDraftNotOpen")
                return null;
              throw e;
            },
          ),
        ]);
        if (!latest()) return null;
        if (d === null) {
          setProposal(p);
          setNotOpen(true);
          setLoadError(null);
          return null;
        }
        if (!background) void loadHistory();
        void loadActiveVersion();
        const at = focus.current;
        // A new revision can change the Inspector's term too (its parents, its
        // properties, the label an undo restored): read it with the graph and
        // show the two together, so the header never runs ahead of the Inspector.
        const { iri: inspect, revision: shownAt } = inspected.current;
        const termRead =
          inspect !== null && (background || d.revision !== shownAt)
            ? readTerm(inspect, background)
            : null;
        // Only a background read rejects; when the graph read fails first, the load ends there.
        termRead?.catch(() => undefined);
        let g: StudioGraphData;
        try {
          g = await fetchGraph(at);
        } catch (e) {
          // The focused class is gone (deleted, renamed, undone): show the graph without it.
          if (
            at === null ||
            !(e instanceof ApiError && e.code === "TermNotFound")
          )
            throw e;
          focus.current = null;
          g = await fetchGraph(null);
        }
        const showTerm = await termRead;
        if (!latest()) return null;
        setProposal(p);
        setDraft(d);
        setGraph(g);
        setNotOpen(false);
        setLoadError(null);
        showTerm?.();
        return d;
      } catch (e) {
        if (latest() && !background) setLoadError(errorMessage(e, t));
        return null;
      }
    },
    [
      apiClient,
      namespace,
      proposalId,
      loadHistory,
      loadActiveVersion,
      readTerm,
      t,
    ],
  );

  useEffect(() => {
    void reload();
  }, [reload]);

  // A deploy rebuilds the domain data after it ends, and nothing else reads
  // the graph again then: an accept on this page reads it once, when the
  // accept lands. While the instance counts are dimmed, read the state of the
  // domain data every 15 seconds, and once it was built for the active
  // version, read the draft again with the Inspector's term, in the
  // background: a read that fails leaves the page as it is, and the next
  // check reads again. This stops after 10 minutes, unless a build runs or
  // waits.
  const aboxStale = graph?.abox?.current === false;
  useEffect(() => {
    if (!aboxStale) return;
    const deadline = Date.now() + ABOX_POLL_TIMEOUT_MS;
    let stopped = false;
    let timer = 0;
    const poll = async () => {
      let building = false;
      try {
        const state = await getDomainAbox(apiClient, namespace);
        if (stopped) return;
        if (state.current) await reload(undefined, { background: true });
        building = isPolling(state);
      } catch {
        // A transient read error: try again until the time limit.
      }
      if (!stopped && (building || Date.now() < deadline))
        timer = window.setTimeout(() => void poll(), ABOX_POLL_MS);
    };
    timer = window.setTimeout(() => void poll(), ABOX_POLL_MS);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [aboxStale, apiClient, namespace, reload]);

  // The Inspector's term: read when another term is wanted. Each reload that
  // finds a new revision reads it again and shows it with the draft, so until
  // then the old one stays on screen.
  const wanted = selected.length === 1 ? selected[0] : null;
  const revision = draft?.revision;
  inspected.current = { iri: wanted, revision };
  useEffect(() => {
    if (wanted === null) return;
    const load = async () => {
      const show = await readTerm(wanted);
      show();
    };
    void load();
  }, [wanted, readTerm]);

  // A new graph (a reload, another focus) can leave selected classes off screen.
  useEffect(() => {
    if (graph) setSelected((s) => onScreen(s, graph));
  }, [graph]);

  // Bumped by each preview request and each hide, so only the newest answer
  // is drawn: an older one arriving late, or one for a hidden preview, is not.
  const previewRequest = useRef(0);
  const shownRevision = useRef(revision);
  shownRevision.current = revision;
  const hidePreview = useCallback(() => {
    previewRequest.current++;
    setPreview(null);
  }, []);

  // A preview shows one revision: any change of the draft ends it.
  useEffect(() => {
    hidePreview();
  }, [revision, hidePreview]);

  /** Previews ops of a change set on the graph (all for null), around the graph's focus. */
  const previewChangeSet = useCallback(
    async (
      changeSet: StudioChangeSet,
      opIndexes: number[] | null,
    ): Promise<StudioChangeSetPreview | null> => {
      const request = ++previewRequest.current;
      try {
        const result = await previewStudioChangeSet(
          apiClient,
          namespace,
          proposalId,
          changeSet.changeSetId,
          opIndexes,
          graph?.mode === "focus" ? graph.focus : null,
        );
        if (request !== previewRequest.current) return result;
        if (result.revision !== shownRevision.current) {
          // Built on another revision than the one on screen: show the draft as it is now.
          setPreview(null);
          void reload();
          return result;
        }
        setPreview(
          result.applies && result.graph
            ? { changeSetId: changeSet.changeSetId, graph: result.graph }
            : null,
        );
        return result;
      } catch (e) {
        if (request === previewRequest.current)
          notify("error", errorMessage(e, t));
        return null;
      }
    },
    [apiClient, namespace, proposalId, graph, reload, notify, t],
  );

  // A Shift-click adds to whatever was selected, even a term the Inspector opened.
  const changeSelection = useCallback(
    (ids: string[]) => setSelected(graph ? onScreen(ids, graph) : ids),
    [graph],
  );

  // The accept preview can know more than the draft on screen: a version
  // published since, or a change made elsewhere.
  const onPreview = useCallback(
    (preview: StudioAcceptPreview) => {
      if (preview.rebaseRequired) {
        setRebaseOffer({
          baseVersion: preview.baseVersion,
          headVersion: preview.headVersion,
        });
      }
      if (preview.revision !== revision) void reload();
    },
    [revision, reload],
  );

  const search = useCallback(
    async (q: string, kind?: StudioTermKind): Promise<StudioTermHit[]> =>
      (
        await searchStudioTerms(apiClient, namespace, proposalId, q, {
          kind,
          limit: SEARCH_LIMIT,
        })
      ).terms,
    [apiClient, namespace, proposalId],
  );

  /** Marks a write as running, or answers false when one already is. */
  function begin(): boolean {
    if (writing.current) return false;
    writing.current = true;
    setBusy(true);
    return true;
  }

  function end() {
    writing.current = false;
    setBusy(false);
  }

  /** The draft moved on under this page (409 DraftConflict): say so and show it as it is now. */
  async function reloadAfterConflict() {
    notify("info", conflictMessage(t));
    await reload();
  }

  /**
   * Runs one write against the revision on screen and reloads after it. A
   * refusal goes to `onError` (the flash bar unless a dialog shows it).
   */
  async function write(
    run: (revision: number) => Promise<StudioChangeResult>,
    {
      onApplied,
      onError = (m: string) => notify("error", m),
    }: {
      onApplied?: (result: StudioChangeResult) => void;
      onError?: (message: string) => void;
    } = {},
  ): Promise<WriteOutcome> {
    if (!draft || !begin()) return "failed";
    try {
      const result = await run(draft.revision);
      onApplied?.(result);
      if (result.warnings.length > 0) {
        const warnings = studioMessageLines(
          t,
          result.warningMessages,
          result.warnings,
        );
        notify("warning", warnings.join(" "));
      } else {
        setFlash([]);
      }
      await reload();
      return "applied";
    } catch (e) {
      if (isDraftConflict(e)) {
        await reloadAfterConflict();
        return "reloaded";
      }
      if (e instanceof ApiError && e.code === "ProposalNotEditable") {
        // Accepted or cancelled meanwhile: the reload shows it read-only.
        notify("error", errorMessage(e, t));
        await reload();
        return "reloaded";
      }
      onError(errorMessage(e, t));
      return "failed";
    } finally {
      end();
    }
  }

  function apply(
    ops: StudioOp[],
    onError: (message: string) => void = (m) => notify("error", m),
  ): Promise<WriteOutcome> {
    // Measured as applyStudioChanges sends it: past MAX_REQUEST_BYTES the WAF
    // would answer a bare 403.
    if (
      draft &&
      requestBytes({ ops, expectedRevision: draft.revision, dryRun: false }) >
        MAX_REQUEST_BYTES
    ) {
      onError(
        t(
          "This change is too large to send: a change is at most 8 KB, and Japanese, Korean and other non-Latin characters take up to 3 bytes each. Shorten the text.",
        ),
      );
      return Promise.resolve("failed");
    }
    return write(
      (rev) => applyStudioChanges(apiClient, namespace, proposalId, ops, rev),
      {
        onApplied: (result) =>
          setSelected((s) => selectionAfter(s, ops, result.created)),
        onError,
      },
    );
  }

  /** Applies ops of a change set (all for null); resolves why it was refused, or null. */
  async function applyChangeSet(
    changeSet: StudioChangeSet,
    opIndexes: number[] | null,
  ): Promise<string | null> {
    let refusal: string | null = null;
    const outcome = await write(
      (rev) =>
        applyStudioChangeSet(
          apiClient,
          namespace,
          proposalId,
          changeSet.changeSetId,
          rev,
          opIndexes,
        ),
      {
        onError: (m) => {
          refusal = m;
        },
      },
    );
    if (outcome === "applied") hidePreview();
    return refusal;
  }

  async function open() {
    if (!begin()) return;
    try {
      await openStudioDraft(apiClient, namespace, proposalId);
      await reload();
    } catch (e) {
      notify("error", errorMessage(e, t));
    } finally {
      end();
    }
  }

  async function rebase(skipOps: string[] = []) {
    if (!draft || !begin()) return;
    try {
      const result = await rebaseStudioDraft(
        apiClient,
        namespace,
        proposalId,
        draft.revision,
        skipOps,
      );
      setFailures(null);
      setRebaseOffer(null);
      const values = {
        version: versionText(result.baseVersion, t),
        replayed: result.replayed,
        skipped: result.skipped.length,
      };
      notify(
        "success",
        result.skipped.length
          ? t(
              "Rebased onto {version}: {replayed} change(s) replayed, {skipped} skipped.",
              values,
            )
          : t("Rebased onto {version}: {replayed} change(s) replayed.", values),
      );
      await reload();
    } catch (e) {
      const failed = rebaseFailures(e);
      if (failed.length > 0) {
        // The ops this try skipped stay listed and skipped, beside the ones that failed now:
        // the engine does not replay the skipped ones, so it cannot report them again.
        const kept = (failures ?? []).filter((f) => skipOps.includes(f.opId));
        const shown = [
          ...kept,
          ...failed.filter((f) => !skipOps.includes(f.opId)),
        ];
        setFailures(shown);
        setSkips(shown.map((f) => f.opId));
        return;
      }
      setFailures(null);
      if (isDraftConflict(e)) {
        await reloadAfterConflict();
      } else if (e instanceof ApiError && e.code === "NothingToRebase") {
        setRebaseOffer(null);
        notify("info", t("The draft is already built on the newest version."));
        await reload();
      } else {
        notify("error", errorMessage(e, t));
      }
    } finally {
      end();
    }
  }

  /** Waits a little for the accept worker to record what became of the deployment the accept asked for. */
  async function followDeployOutcome(accepted: Proposal) {
    let p = accepted;
    const deadline = Date.now() + DEPLOY_OUTCOME_WAIT_MS;
    while (!p.auto_deploy_id && !p.auto_deploy_held && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, ACCEPT_POLL_MS));
      if (!mounted.current) return;
      try {
        p = await getProposal(apiClient, namespace, proposalId);
      } catch {
        // A transient read error: keep polling until the deadline.
      }
    }
    if (mounted.current) notify(...acceptedFlash(p, true, false, t));
  }

  /** Polls the proposal after a 202 until its accept lands, as ProposalDetail does. */
  async function followAccept(deploy: boolean) {
    const start = Date.now();
    while (Date.now() - start < ACCEPT_TIMEOUT_MS) {
      await new Promise((r) => setTimeout(r, ACCEPT_POLL_MS));
      if (!mounted.current) return;
      let fresh: Proposal;
      try {
        fresh = await getProposal(apiClient, namespace, proposalId);
      } catch {
        continue; // A transient read error: keep polling until the deadline.
      }
      if (!mounted.current) return;
      setProposal(fresh);
      if (fresh.status === "accepted") {
        const awaitDeploy =
          deploy &&
          fresh.published_version != null &&
          !fresh.auto_deploy_id &&
          !fresh.auto_deploy_held;
        notify(...acceptedFlash(fresh, deploy, awaitDeploy, t));
        await reload();
        if (awaitDeploy) await followDeployOutcome(fresh);
        return;
      }
      if (isAcceptTerminalFailure(fresh)) {
        notify(
          "error",
          fresh.accept_error || t("Accept failed. Accept again to retry."),
        );
        const d = await reload();
        // The code outlives a later rebase: trust it only while the accept is failed.
        if (
          d &&
          fresh.status === "accept_failed" &&
          fresh.accept_progress?.code === "RebaseRequired"
        ) {
          setRebaseOffer({
            baseVersion: d.baseVersion,
            headVersion: d.headVersion,
          });
        }
        return;
      }
    }
    notify(
      "warning",
      t("Accept is still running after 5 minutes; refresh the page to check."),
    );
  }

  async function accept(deploy: boolean) {
    if (!draft || accepting) return;
    setAccepting(true);
    try {
      await acceptProposal(apiClient, namespace, proposalId, {
        deploy,
        expectedRevision: draft.revision,
      });
    } catch (e) {
      setAccepting(false);
      setAcceptOpen(false);
      if (isDraftConflict(e)) {
        await reloadAfterConflict();
      } else if (isRebaseRequired(e)) {
        const detail = studioErrorDetail(e);
        setRebaseOffer({
          baseVersion: versionOf(detail?.baseVersion),
          headVersion: versionOf(detail?.headVersion),
        });
        await reload();
      } else {
        notify("error", errorMessage(e, t));
      }
      return;
    }
    setAcceptOpen(false);
    try {
      await followAccept(deploy);
    } finally {
      if (mounted.current) setAccepting(false);
    }
  }

  function openDialog(next: Dialog) {
    setDialogError(null);
    setDialog(next);
  }

  /** The Add relationship dialog's target class: a pick sets it, an edit of the box drops it. */
  function setTarget(target: StudioTermHit | null) {
    // Functional: a pick's own edit and the pick arrive in one batch, and
    // the pick has to be what stands.
    setDialog((d) => (d?.kind === "addRelationship" ? { ...d, target } : d));
  }

  function onAction(action: GraphAction) {
    const { iri, label } = action;
    switch (action.kind) {
      case "addSubclass":
        openDialog({ kind: "addSubclass", iri, label, text: "" });
        break;
      case "addRelationship":
        openDialog({
          kind: "addRelationship",
          iri,
          label,
          text: "",
          target: null,
        });
        break;
      case "rename":
        openDialog({ kind: "rename", iri, label, text: label });
        break;
      case "delete":
        openDialog({ kind: "delete", iris: [iri], label, cascade: "detach" });
        break;
    }
  }

  /**
   * The language of the label `iri` shows, which set_label replaces (as the
   * Inspector does): sent untagged, it would add a second label beside it.
   */
  async function shownLabelLang(iri: string): Promise<string | null> {
    const term = await getStudioTerm(apiClient, namespace, proposalId, iri);
    return term.labels.find((l) => l.value === term.label)?.lang ?? null;
  }

  async function confirmDialog() {
    if (!dialog || !dialogReady(dialog)) return;
    setDialogError(null);
    let lang: string | null = null;
    if (dialog.kind === "rename") {
      if (!begin()) return;
      try {
        lang = await shownLabelLang(dialog.iri);
      } catch (e) {
        // Without the label's language the rename could duplicate it: send nothing.
        setDialogError(errorMessage(e, t));
        return;
      } finally {
        end();
      }
    }
    // A refusal stays in the dialog, where the user is looking.
    const outcome = await apply(dialogOps(dialog, lang), setDialogError);
    if (outcome !== "failed") setDialog(null);
  }

  if (loadError) {
    return (
      <Alert
        type="error"
        header={t("Could not load the designer draft")}
        action={<Button onClick={() => void reload()}>{t("Retry")}</Button>}
      >
        {loadError}
      </Alert>
    );
  }

  const title =
    (proposal?.metadata?.label as string | undefined) ??
    t("Proposal {id}", { id: proposalId.slice(0, 8) });
  const flashbar = flash.length > 0 && <Flashbar items={flash} />;

  if (notOpen) {
    const canOpen = !!proposal && OPENABLE_STATUSES.includes(proposal.status);
    return (
      <SpaceBetween size="m">
        {flashbar}
        <Container header={<Header variant="h1">{title}</Header>}>
          <SpaceBetween size="m">
            <Box>
              {t("This proposal is not open in the Ontology designer.")}
            </Box>
            {proposal && !canOpen && (
              <Box color="text-body-secondary">
                {t(
                  "It is {status}. Only a pending or updated proposal, or one whose accept failed, opens in the Ontology designer.",
                  { status: proposalStatusText(proposal.status, t) },
                )}
              </Box>
            )}
            <Button
              variant="primary"
              loading={busy}
              disabled={!canOpen || !canChange}
              disabledReason={canOpen ? denied : undefined}
              onClick={() => void open()}
            >
              {t("Open in Ontology designer")}
            </Button>
          </SpaceBetween>
        </Container>
      </SpaceBetween>
    );
  }

  if (!draft || !graph) return <Spinner size="large" />;

  const editable = draft.editable && !busy && !accepting && canChange;
  const rebaseNeeded =
    draft.editable && (draft.rebaseRequired || rebaseOffer !== null);
  const versions = rebaseOffer ?? draft;
  const multi = selected.length > 1;
  const selectedNodes = graph.nodes.filter((n) => selected.includes(n.iri));
  const selectionEditable =
    editable &&
    selectedNodes.length > 0 &&
    selectedNodes.every((n) => n.editable);
  const termShown = shown !== null && shown.iri === wanted ? shown : null;
  const classes = draft.counts.classes;
  // What a message is about: the selected terms, by the labels on screen.
  const selectionLabels = selected.map((iri) => ({
    iri,
    label:
      graph.nodes.find((n) => n.iri === iri)?.label ??
      (shown?.iri === iri && shown.term ? shown.term.label : iriTail(iri)),
  }));

  /** Show a term (from a finding or a result cell) in the Inspector. */
  function openTerm(iri: string) {
    setSelected([iri]);
    setTab("inspector");
  }

  /** Prefill the chat with a message for the assistant and show it. */
  function ask(text: string) {
    setChatPrefill(text);
    setTab("chat");
  }

  function openQuery(q: ConsoleQuery) {
    setQueryPrefill(q);
    setTab("query");
  }

  const inspector = multi ? (
    <Container
      header={
        <Header variant="h2">
          {t("{count} classes selected", { count: selected.length })}
        </Header>
      }
    >
      <SpaceBetween size="s">
        <Button
          disabled={!selectionEditable}
          onClick={() =>
            openDialog({
              kind: "delete",
              iris: selectedNodes.map((n) => n.iri),
              label: t("{count} classes", { count: selectedNodes.length }),
              cascade: "detach",
            })
          }
        >
          {t("Delete selected")}
        </Button>
        {selectedNodes.length === 2 && (
          <Button
            disabled={!selectionEditable}
            onClick={() =>
              openDialog({
                kind: "merge",
                iris: [selectedNodes[0].iri, selectedNodes[1].iri],
                labels: [selectedNodes[0].label, selectedNodes[1].label],
                target: selectedNodes[1].iri,
              })
            }
          >
            {t("Merge…")}
          </Button>
        )}
      </SpaceBetween>
    </Container>
  ) : termShown?.error ? (
    <Alert type="error" header={t("Could not load the term")}>
      {termShown.error}
    </Alert>
  ) : (
    <Inspector
      term={termShown?.term ?? null}
      // Only while another term is on its way: a refetch of the shown one
      // keeps the forms, and what was typed in them, on screen.
      loading={wanted !== null && termShown === null}
      editable={
        draft.editable && !accepting && canChange && !!termShown?.term?.editable
      }
      deniedReason={denied}
      busy={busy}
      apiClient={apiClient}
      namespace={namespace}
      abox={graph.abox}
      onApply={async (ops) => (await apply(ops)) === "applied"}
      search={search}
      onOpenTerm={(iri) => setSelected([iri])}
    />
  );

  return (
    <SpaceBetween size="m">
      <Header
        variant="h1"
        description={t(
          classes === 1
            ? "{revision}, {count} class"
            : "{revision}, {count} classes",
          { revision: revisionLine(draft, t), count: classes },
        )}
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button
              onClick={() =>
                navigate(
                  `/namespaces/${namespace}/ontology/proposals/${proposalId}`,
                )
              }
            >
              {t("Back to proposal")}
            </Button>
            <Button
              iconName="undo"
              disabled={!editable || !draft.canUndo}
              disabledReason={denied}
              onClick={() =>
                void write((rev) =>
                  undoStudioChange(apiClient, namespace, proposalId, rev),
                )
              }
            >
              {t("Undo")}
            </Button>
            <Button
              iconName="redo"
              disabled={!editable || !draft.canRedo}
              disabledReason={denied}
              onClick={() =>
                void write((rev) =>
                  redoStudioChange(apiClient, namespace, proposalId, rev),
                )
              }
            >
              {t("Redo")}
            </Button>
            <Button onClick={() => setTab("history")}>{t("History")}</Button>
            <Button
              variant="primary"
              disabled={!editable || rebaseNeeded}
              disabledReason={denied}
              loading={accepting}
              onClick={() => setAcceptOpen(true)}
            >
              {t("Accept")}
            </Button>
          </SpaceBetween>
        }
      >
        {title}
      </Header>
      {flashbar}
      {!draft.editable && (
        <Alert type="info">
          {t("This proposal is {status}; its draft is read-only.", {
            status: proposalStatusText(draft.status, t),
          })}
        </Alert>
      )}
      {draft.editable && !canChange && (
        <Alert type="info">
          {t(
            "Your role on this namespace can read and query this draft, but not change it.",
          )}
        </Alert>
      )}
      {draft.editable &&
        draft.headVersion !== null &&
        activeVersion !== null &&
        activeVersion < draft.headVersion && (
          <Alert type="warning">
            {t(
              "The next version builds on v{head}, not the active v{active}; its changes carry over unless you revert them.",
              { head: draft.headVersion, active: activeVersion },
            )}
          </Alert>
        )}
      {rebaseNeeded && (
        <Alert
          type="warning"
          header={t("A newer version was published")}
          action={
            <Button
              loading={busy}
              disabled={accepting || !canChange}
              disabledReason={denied}
              onClick={() => void rebase()}
            >
              {t("Rebase")}
            </Button>
          }
        >
          {t(
            "This draft was built on {base}, and the newest version is {head}. Rebase to replay your changes on it before accepting.",
            {
              base: versionText(versions.baseVersion, t),
              head: versionText(versions.headVersion, t),
            },
          )}
        </Alert>
      )}
      <div style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
        <div style={{ flex: 2, minWidth: 0 }}>
          <Container>
            <StudioGraph
              graph={graph}
              selected={selected}
              editable={editable}
              onSelectionChange={changeSelection}
              onFocus={(iri) => {
                hidePreview();
                void reload(iri);
              }}
              onAction={onAction}
              search={search}
              preview={preview?.graph ?? null}
              onHidePreview={hidePreview}
            />
          </Container>
        </div>
        <div style={{ flex: 1, minWidth: 360 }}>
          <Tabs
            activeTabId={tab}
            onChange={({ detail }) => setTab(detail.activeTabId)}
            // Every tab that holds typed text, results or a running job stays
            // mounted once opened ("lazy"), so a visit to another tab (also one
            // the page makes itself, such as Ask the assistant) loses none of it.
            tabs={[
              {
                id: "inspector",
                label: t("Inspector"),
                contentRenderStrategy: "lazy",
                content: inspector,
              },
              {
                id: "chat",
                label: t("Chat"),
                contentRenderStrategy: "lazy",
                content: (
                  <ChatPanel
                    apiClient={apiClient}
                    namespace={namespace}
                    proposalId={proposalId}
                    editable={draft.editable && !accepting && canChange}
                    deniedReason={denied}
                    busy={busy}
                    revision={draft.revision}
                    selection={selectionLabels}
                    previewId={preview?.changeSetId ?? null}
                    onPreview={previewChangeSet}
                    onHidePreview={hidePreview}
                    onApply={applyChangeSet}
                    prefill={chatPrefill}
                    onPrefillTaken={takeChatPrefill}
                    onOpenQuery={openQuery}
                    active={tab === "chat"}
                  />
                ),
              },
              {
                id: "checks",
                label: t("Checks"),
                contentRenderStrategy: "lazy",
                content: (
                  <ChecksPanel
                    apiClient={apiClient}
                    namespace={namespace}
                    proposalId={proposalId}
                    revision={draft.revision}
                    editable={draft.editable && !accepting && canChange}
                    deniedReason={denied}
                    onOpenTerm={openTerm}
                    onAsk={ask}
                  />
                ),
              },
              {
                id: "query",
                label: t("Query"),
                contentRenderStrategy: "lazy",
                content: (
                  <QueryConsole
                    apiClient={apiClient}
                    namespace={namespace}
                    proposalId={proposalId}
                    editable={draft.editable && !accepting && canChange}
                    deniedReason={denied}
                    // The engine refuses a draft query from the moment the
                    // accept starts, while the draft read before it still
                    // says editable.
                    draftClosedReason={
                      !draft.editable
                        ? t(
                            "This proposal is {status}, so its draft can no longer be queried.",
                            { status: proposalStatusText(draft.status, t) },
                          )
                        : accepting
                          ? t(
                              "This proposal is being accepted, so its draft cannot be queried.",
                            )
                          : undefined
                    }
                    prefill={queryPrefill}
                    onPrefillTaken={takeQueryPrefill}
                    onOpenTerm={openTerm}
                    onAsk={ask}
                  />
                ),
              },
              {
                id: "questions",
                label: t("Questions"),
                contentRenderStrategy: "lazy",
                content: (
                  <QuestionsPanel
                    apiClient={apiClient}
                    namespace={namespace}
                    proposalId={proposalId}
                    editable={draft.editable && !accepting && canChange}
                    deniedReason={denied}
                    onChecking={() => setTab("chat")}
                    onOpenQuery={(query) =>
                      openQuery({ language: "sparql", target: "draft", query })
                    }
                    active={tab === "questions"}
                  />
                ),
              },
              {
                id: "history",
                label: t("History"),
                content: (
                  <HistoryPanel
                    history={history}
                    loading={historyLoading}
                    error={historyError}
                    onRefresh={() => void loadHistory()}
                    onLoadOlder={() => void loadOlderHistory()}
                  />
                ),
              },
            ]}
          />
        </div>
      </div>

      <Modal
        visible={dialog !== null}
        onDismiss={() => setDialog(null)}
        header={dialogHeader(dialog, t)}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDialog(null)}>
                {t("Cancel")}
              </Button>
              <Button
                variant="primary"
                loading={busy}
                disabled={!dialog || !dialogReady(dialog) || !draft.editable}
                disabledReason={
                  dialog?.kind === "addRelationship" && dialog.target === null
                    ? t("Pick a target class first.")
                    : undefined
                }
                onClick={() => void confirmDialog()}
              >
                {dialog?.kind === "delete"
                  ? t("Delete")
                  : dialog?.kind === "merge"
                    ? t("Merge")
                    : t("Save")}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          {dialogError && <Alert type="error">{dialogError}</Alert>}
          {dialog && "text" in dialog && (
            <FormField
              label={
                dialog.kind === "addRelationship"
                  ? t("Relationship label")
                  : t("Label")
              }
            >
              <Input
                value={dialog.text}
                onChange={({ detail }) =>
                  setDialog({ ...dialog, text: detail.value })
                }
              />
            </FormField>
          )}
          {dialog?.kind === "addRelationship" && (
            <FormField label={t("Target class")}>
              <TermPicker
                ariaLabel={t("Target class")}
                kind="class"
                search={search}
                // Typed text is not a class: Save waits for a picked suggestion.
                onTextChange={() => setTarget(null)}
                onPick={setTarget}
              />
            </FormField>
          )}
          {dialog?.kind === "delete" && (
            <FormField label={t("Properties that use the class")}>
              <RadioGroup
                value={dialog.cascade}
                onChange={({ detail }) =>
                  setDialog({
                    ...dialog,
                    cascade: detail.value === "delete" ? "delete" : "detach",
                  })
                }
                items={[
                  {
                    value: "detach",
                    label: t("Keep them"),
                    description: t(
                      "Remove the class from their domain or range. Their table mappings are removed with the class.",
                    ),
                  },
                  {
                    value: "delete",
                    label: t("Delete them"),
                    description: t("Delete them and their mappings too."),
                  },
                ]}
              />
            </FormField>
          )}
          {dialog?.kind === "merge" && (
            <FormField
              label={t("Keep")}
              description={t(
                "The other class's references move to it and its labels become alternative labels.",
              )}
            >
              <Select
                selectedOption={{
                  value: dialog.target,
                  label: dialog.labels[dialog.iris.indexOf(dialog.target)],
                }}
                options={dialog.iris.map((iri, i) => ({
                  value: iri,
                  label: dialog.labels[i],
                }))}
                onChange={({ detail }) =>
                  setDialog({
                    ...dialog,
                    target: detail.selectedOption.value ?? dialog.target,
                  })
                }
              />
            </FormField>
          )}
        </SpaceBetween>
      </Modal>

      <Modal
        visible={failures !== null}
        onDismiss={() => setFailures(null)}
        header={t("Some changes do not apply to the newest version")}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setFailures(null)}>
                {t("Cancel")}
              </Button>
              <Button
                variant="primary"
                loading={busy}
                disabled={skips.length === 0}
                onClick={() => void rebase(skips)}
              >
                {t("Rebase without the checked ops")}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          {(failures ?? []).map((f) => (
            <Checkbox
              key={f.opId}
              checked={skips.includes(f.opId)}
              onChange={({ detail }) =>
                setSkips((s) =>
                  detail.checked
                    ? [...s, f.opId]
                    : s.filter((x) => x !== f.opId),
                )
              }
            >
              {f.op.op} ({f.opId}): {f.error}
            </Checkbox>
          ))}
        </SpaceBetween>
      </Modal>

      <AcceptDialog
        visible={acceptOpen}
        apiClient={apiClient}
        namespace={namespace}
        proposalId={proposalId}
        revision={draft.revision}
        accepting={accepting}
        onDismiss={() => setAcceptOpen(false)}
        onAccept={(deploy) => void accept(deploy)}
        onPreview={onPreview}
      />
    </SpaceBetween>
  );
}
