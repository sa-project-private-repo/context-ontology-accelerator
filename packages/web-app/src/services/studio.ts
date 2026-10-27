// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
/**
 * Ontology designer client: the draft of a proposal, its graph and terms, the
 * typed change sets that edit it, the chat with the assistant and the change
 * sets it proposes, and the checks, the query console and the competency
 * questions. Every write to the draft sends the revision it
 * was made against (`expectedRevision`) and answers with the new `revision`.
 *
 * Accepting a draft is not here: it is `acceptProposal` in `ontology-engine.ts`
 * with the reviewed revision as `expectedRevision`.
 */
import { ApiError, type ApiClient } from "../components/ApiClientProvider";
import type { BreakingMetric, SchemaKind } from "./ontology-engine";

export type StudioTermKind = "class" | "objectProperty" | "datatypeProperty";

export interface StudioDraft {
  proposalId: string;
  status: string;
  editable: boolean;
  revision: number;
  baseVersion: number | null;
  headVersion: number | null;
  rebaseRequired: boolean;
  ontologyId: string;
  counts: {
    classes: number;
    objectProperties: number;
    datatypeProperties: number;
    triplesMaps: number;
  };
  canUndo: boolean;
  canRedo: boolean;
  openedAt: string;
  openedBy: string;
  updatedAt: string;
  updatedBy: string;
}

export interface StudioAttribute {
  iri: string;
  label: string;
  range: string | null;
}

export interface StudioGraphNode {
  iri: string;
  label: string;
  grounded: boolean;
  editable: boolean;
  attributes: StudioAttribute[];
  attributeCount: number;
  /** Instances in the active version's domain ABox; null when the active version lacks the class. */
  aboxCount: number | null;
  /** The class is only in the draft: its instances appear after it is deployed. */
  afterDeploy: boolean;
}

export interface StudioGraphEdge {
  id: string;
  kind: "subClassOf" | "objectProperty";
  source: string;
  target: string;
  iri?: string;
  label?: string;
}

/**
 * The active version's domain ABox as the badges see it. Dim every badge when
 * `current` is false (the counts were built for an older version); `reason`
 * says why.
 */
export interface StudioAboxState {
  status: string;
  current: boolean;
  conformsToVersion: number | null;
  activeVersion: number;
  reason: string | null;
}

export interface StudioGraph {
  revision: number;
  mode: "full" | "focus";
  focus: string | null;
  totalClasses: number;
  truncated: boolean;
  nodes: StudioGraphNode[];
  edges: StudioGraphEdge[];
  /** Null when the namespace has no ABox (the nodes' badges are then empty too). */
  abox: StudioAboxState | null;
}

/**
 * A term named by another term: a parent, child, domain or range.
 *
 * `declared` is true when the draft declares the IRI as a class or property, so
 * `getStudioTerm` can open it. It is false for an external term the draft only
 * names (a parent from a reference ontology such as FIBO, a datatype
 * property's XSD range); `getStudioTerm` answers 404 `TermNotFound` for those,
 * so show them as plain text, not as a link.
 */
export interface StudioTermRef {
  iri: string;
  label: string;
  declared: boolean;
}

export interface StudioTermProperty {
  iri: string;
  label: string;
  kind: "objectProperty" | "datatypeProperty";
  direction: "outgoing" | "incoming";
  /**
   * The other end: the range of an outgoing property, the domain of an incoming
   * one. Like a `StudioTermRef` it may be external (a datatype property's is
   * the XSD datatype).
   */
  other: string | null;
  otherLabel: string | null;
  /** Like `StudioTermRef.declared`, for `other`; false when `other` is null. */
  otherDeclared: boolean;
}

export interface StudioTerm {
  iri: string;
  kind: StudioTermKind;
  label: string;
  labels: { value: string; lang: string | null }[];
  altLabels: string[];
  comment: string;
  editable: boolean;
  parents?: StudioTermRef[];
  children?: StudioTermRef[];
  matches?: { target: string; kind: "exact" | "close" }[];
  properties?: StudioTermProperty[];
  domain?: StudioTermRef[];
  range?: StudioTermRef[];
  mappings: {
    triplesMap: string | null;
    table?: string | null;
    column?: string | null;
    parentTriplesMap?: string | null;
  }[];
  abox?: { aboxCount: number | null; afterDeploy: boolean } | null;
}

export interface StudioTermHit {
  iri: string;
  label: string;
  kind: StudioTermKind;
}

/** The 14 typed user ops, as the engine accepts them. */
export type StudioOp =
  | {
      op: "add_class";
      label: string;
      comment?: string;
      parents?: string[];
      iri?: string;
    }
  | { op: "set_label"; iri: string; label: string; lang?: string }
  | { op: "set_comment"; iri: string; comment: string }
  | { op: "change_iri"; iri: string; newIri: string }
  | { op: "delete_class"; iri: string; cascade: "detach" | "delete" }
  | { op: "merge_classes"; source: string; target: string }
  | { op: "add_parent"; iri: string; parent: string }
  | { op: "remove_parent"; iri: string; parent: string }
  | {
      op: "add_object_property";
      label: string;
      domain: string;
      range: string;
      comment?: string;
      iri?: string;
    }
  | {
      op: "add_datatype_property";
      label: string;
      domain: string;
      range: string;
      comment?: string;
      iri?: string;
    }
  | {
      op: "update_property";
      iri: string;
      domain?: string;
      range?: string;
      label?: string;
      comment?: string;
    }
  | { op: "delete_property"; iri: string }
  | {
      op: "set_match";
      iri: string;
      target?: string;
      kind: "exact" | "close" | "none";
    }
  /**
   * Rewrites every use of the datatype `from` (any IRI, or a malformed token
   * such as `xsd:datetime`) in ranges, `rr:datatype` and typed literals; `to`
   * must be a datatype the designer offers. The engine also builds these for
   * `repairProposalDatatypes` on a proposal open in the designer.
   */
  | { op: "replace_datatype"; from: string; to: string };

export type StudioDiff = Record<
  SchemaKind,
  { added: string[]; removed: string[]; changed: string[] }
>;

export interface StudioChangeResult {
  revision: number;
  dryRun: boolean;
  /** In English. */
  warnings: string[];
  /**
   * The lines of `warnings` as codes, in the same order (at most the first
   * 100); older engines do not send it.
   */
  warningMessages?: StudioMessage[] | null;
  created: string[];
  diff: StudioDiff;
  canUndo: boolean;
  canRedo: boolean;
}

/**
 * A sentence of the designer's checks or history as a stable code and the
 * values it names, which the web app writes in the user's language.
 */
export interface StudioMessage {
  code: string;
  params: Record<string, string>;
}

export interface StudioHistoryEntry {
  revision: number;
  kind: "open" | "apply" | "undo" | "redo" | "rebase";
  /** "agent" for an assistant change set the user applied; "user" otherwise. */
  source: "user" | "agent";
  /** The assistant's reason, for an applied change set. */
  rationale: string | null;
  changeSetId: string | null;
  /** Who wrote it; for an assistant change set, who applied it. */
  author: string | null;
  at: string | null;
  /** One line per op, in English. */
  summary: string[];
  /**
   * The lines of `summary` as codes, in the same order (at most the first
   * 100); absent for a revision stored before the codes.
   */
  summaryMessages?: StudioMessage[] | null;
  opCount: number;
  target: number | null;
  /** In English. */
  warnings: string[];
  /**
   * The lines of `warnings` as codes, in the same order (at most the first
   * 100); absent for a revision stored before the codes.
   */
  warningMessages?: StudioMessage[] | null;
  active: boolean;
  undone: boolean;
}

/** One page of the history; `revision`, `canUndo` and `canRedo` describe the draft now. */
export interface StudioHistory {
  revision: number;
  entries: StudioHistoryEntry[];
  canUndo: boolean;
  canRedo: boolean;
  /** Pass back to read the next (older) page; absent on the last page. */
  nextToken?: string;
}

export interface StudioRebaseResult {
  revision: number;
  baseVersion: number | null;
  replayed: number;
  skipped: string[];
  canUndo: boolean;
  canRedo: boolean;
}

export interface StudioRebaseFailure {
  opId: string;
  op: StudioOp;
  error: string;
}

export interface StudioAcceptPreview {
  revision: number;
  baseVersion: number | null;
  headVersion: number | null;
  rebaseRequired: boolean;
  diff: StudioDiff;
  renamed: { fromIri: string; toIri: string; kind: "renamed" | "merged" }[];
  breakingMetrics: BreakingMetric[];
  /**
   * A fixed message ("Breaking metrics could not be checked; the graph database
   * may be unreachable.") when the metrics could not be listed, with
   * `breakingMetrics` empty; null when they were. Show it as it is.
   */
  breakingMetricsError: string | null;
  /** The code of `breakingMetricsError`; older engines do not send it. */
  breakingMetricsErrorCode?: string | null;
  /**
   * The structured-query check, limited to the terms the diff adds or
   * changes. Optional: older engines do not send it.
   */
  structured?: StudioStructuredCheck;
}

function studioPath(namespace: string, proposalId: string): string {
  return `/namespaces/${encodeURIComponent(namespace)}/proposals/${encodeURIComponent(proposalId)}/studio`;
}

function query(params: Record<string, string | number | undefined>): string {
  const entries = Object.entries(params).filter(
    (e): e is [string, string | number] => e[1] !== undefined,
  );
  return entries.length
    ? "?" +
        new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString()
    : "";
}

export function openStudioDraft(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
): Promise<StudioDraft> {
  return apiClient.post<StudioDraft>(studioPath(namespace, proposalId), {});
}

export function getStudioDraft(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
): Promise<StudioDraft> {
  return apiClient.get<StudioDraft>(studioPath(namespace, proposalId));
}

export function getStudioGraph(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  focus?: string,
): Promise<StudioGraph> {
  return apiClient.get<StudioGraph>(
    `${studioPath(namespace, proposalId)}/graph${query({ focus })}`,
  );
}

export function getStudioTerm(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  iri: string,
): Promise<StudioTerm> {
  return apiClient.get<StudioTerm>(
    `${studioPath(namespace, proposalId)}/term${query({ iri })}`,
  );
}

export function searchStudioTerms(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  q: string,
  opts: { kind?: StudioTermKind; limit?: number } = {},
): Promise<{ revision: number; terms: StudioTermHit[] }> {
  return apiClient.get<{ revision: number; terms: StudioTermHit[] }>(
    `${studioPath(namespace, proposalId)}/terms${query({ q, kind: opts.kind, limit: opts.limit })}`,
  );
}

export function applyStudioChanges(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  ops: StudioOp[],
  expectedRevision: number,
  dryRun = false,
): Promise<StudioChangeResult> {
  return apiClient.post<StudioChangeResult>(
    `${studioPath(namespace, proposalId)}/changes`,
    { ops, expectedRevision, dryRun },
  );
}

export function undoStudioChange(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  expectedRevision: number,
): Promise<StudioChangeResult> {
  return apiClient.post<StudioChangeResult>(
    `${studioPath(namespace, proposalId)}/undo`,
    { expectedRevision },
  );
}

export function redoStudioChange(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  expectedRevision: number,
): Promise<StudioChangeResult> {
  return apiClient.post<StudioChangeResult>(
    `${studioPath(namespace, proposalId)}/redo`,
    { expectedRevision },
  );
}

export function listStudioHistory(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  opts: { maxResults?: number; nextToken?: string } = {},
): Promise<StudioHistory> {
  return apiClient.get<StudioHistory>(
    `${studioPath(namespace, proposalId)}/history${query({ maxResults: opts.maxResults, nextToken: opts.nextToken })}`,
  );
}

export function rebaseStudioDraft(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  expectedRevision: number,
  skipOps: string[] = [],
): Promise<StudioRebaseResult> {
  return apiClient.post<StudioRebaseResult>(
    `${studioPath(namespace, proposalId)}/rebase`,
    { expectedRevision, skipOps },
  );
}

export function getStudioAcceptPreview(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
): Promise<StudioAcceptPreview> {
  return apiClient.get<StudioAcceptPreview>(
    `${studioPath(namespace, proposalId)}/accept-preview`,
  );
}

/**
 * The structured `detail` of a designer error, `{code, message, ...}`: `revision`
 * (DraftConflict, RebaseFailed), `baseVersion` / `headVersion` (RebaseRequired),
 * `opIndex` (InvalidChange), `status` (ProposalNotEditable), `failures`
 * (RebaseFailed). Extra fields are unchecked.
 */
export type StudioErrorDetail = { code?: string; message?: string } & Record<
  string,
  unknown
>;

/**
 * The structured `detail` of an engine error, or null when there is none: the
 * accept 409s that predate the designer carry a plain string `detail`, a 422 from
 * FastAPI's own validation carries a list, and `e` may not be an `ApiError`.
 */
export function studioErrorDetail(e: unknown): StudioErrorDetail | null {
  if (!(e instanceof ApiError)) return null;
  const body = e.body;
  if (typeof body !== "object" || body === null) return null;
  const detail = (body as { detail?: unknown }).detail;
  return typeof detail === "object" && detail !== null && !Array.isArray(detail)
    ? (detail as StudioErrorDetail)
    : null;
}

export type StudioTurnStatus = "running" | "completed" | "limited" | "failed";

export interface StudioSelectedTerm {
  iri: string;
  label: string;
  kind: StudioTermKind;
}

interface StudioEventBase {
  /** Increases across every turn of the proposal. */
  seq: number;
  turnId: string;
  at: string | null;
}

/** One step of the chat, by `type`. `text` of an answer is Markdown. */
export type StudioChatEvent =
  | (StudioEventBase & {
      type: "user_message";
      text: string;
      author: string;
      selection: StudioSelectedTerm[];
    })
  | (StudioEventBase & { type: "thinking"; text: string })
  | (StudioEventBase & { type: "tool_call"; tool: string; input: string })
  | (StudioEventBase & {
      type: "tool_result";
      tool: string;
      status: "success" | "error";
      summary: string;
    })
  | (StudioEventBase & {
      type: "change_set";
      changeSetId: string;
      /** In English. */
      opSummaries: string[];
      /** The lines of `opSummaries` as codes; absent for an older event. */
      opSummaryMessages?: StudioMessage[] | null;
    })
  | (StudioEventBase & { type: "answer"; text: string })
  | (StudioEventBase & {
      type: "query";
      language: StudioQueryLanguage;
      target: StudioQueryTarget;
      query: string;
      rowCount: number;
    })
  | (StudioEventBase & {
      type: "turn_ended";
      status: StudioTurnStatus;
      message: string | null;
    });

export interface StudioTurn {
  turnId: string;
  status: StudioTurnStatus;
  author: string | null;
  startedAt: string | null;
  endedAt: string | null;
}

export interface StudioEvents {
  events: StudioChatEvent[];
  lastSeq: number;
  /** More events follow: ask again after the last `seq` returned. */
  more: boolean;
  /** The newest turn; null before the first message. */
  turn: StudioTurn | null;
}

export type StudioChangeSetStatus = "pending" | "applied" | "discarded";

export interface StudioChangeSet {
  changeSetId: string;
  turnId: string | null;
  status: StudioChangeSetStatus;
  createdAt: string | null;
  /** The revision it was checked on when the assistant proposed it. */
  createdRevision: number;
  createdBy: string | null;
  rationale: string;
  ops: StudioOp[];
  /** One line per op, in English. */
  summary: string[];
  /**
   * The lines of `summary` as codes, in the same order; absent for a change
   * set proposed before the codes.
   */
  summaryMessages?: StudioMessage[] | null;
  /** In English. */
  warnings: string[];
  /** The lines of `warnings` as codes; absent for an older change set. */
  warningMessages?: StudioMessage[] | null;
  diffCounts: Record<
    SchemaKind,
    { added: number; removed: number; changed: number }
  > | null;
  appliedRevision: number | null;
  appliedOpIndexes: number[] | null;
  appliedBy: string | null;
  appliedAt: string | null;
  discardedBy: string | null;
  discardedAt: string | null;
  /** Set when it no longer applied to a newer revision of the draft. */
  stale: { revision: number; message: string; opIndex: number } | null;
}

export type StudioPreviewMark = "added" | "changed" | "removed";

/** The graph after a change set, each node and edge marked by what it does. */
export interface StudioPreviewGraph extends Omit<
  StudioGraph,
  "nodes" | "edges"
> {
  nodes: (StudioGraphNode & { preview: StudioPreviewMark | null })[];
  edges: (StudioGraphEdge & { preview: StudioPreviewMark | null })[];
}

export interface StudioChangeSetPreview {
  revision: number;
  changeSetId: string;
  opIndexes: number[];
  /** False when the selected ops do not apply; `error` says which op and why. */
  applies: boolean;
  error: { message: string; opIndex: number } | null;
  warnings: string[];
  created: string[];
  diff: StudioDiff | null;
  graph: StudioPreviewGraph | null;
}

export interface StudioChangeSetApplyResult extends StudioChangeResult {
  changeSet: StudioChangeSet;
}

/** Send a message to the assistant (202); poll `listStudioEvents` for its turn. */
export function sendStudioMessage(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  text: string,
  selection: string[] = [],
): Promise<{ turnId: string; seq: number }> {
  return apiClient.post<{ turnId: string; seq: number }>(
    `${studioPath(namespace, proposalId)}/messages`,
    { text, selection },
  );
}

export function listStudioEvents(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  after = 0,
  limit?: number,
): Promise<StudioEvents> {
  return apiClient.get<StudioEvents>(
    `${studioPath(namespace, proposalId)}/events${query({ after, limit })}`,
  );
}

export interface StudioChangeSets {
  revision: number;
  /** This page, oldest first. */
  changeSets: StudioChangeSet[];
  /** Pass back to read the next page; absent on the last page. */
  nextToken?: string;
}

/** One page of the change sets the assistant proposed, oldest first. */
export function listStudioChangeSets(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  opts: {
    status?: StudioChangeSetStatus;
    maxResults?: number;
    nextToken?: string;
  } = {},
): Promise<StudioChangeSets> {
  return apiClient.get<StudioChangeSets>(
    `${studioPath(namespace, proposalId)}/changesets${query({
      status: opts.status,
      maxResults: opts.maxResults,
      nextToken: opts.nextToken,
    })}`,
  );
}

function changeSetPath(
  namespace: string,
  proposalId: string,
  changeSetId: string,
): string {
  return `${studioPath(namespace, proposalId)}/changesets/${encodeURIComponent(changeSetId)}`;
}

/** Dry-run the selected ops (all for none) on the current revision. */
export function previewStudioChangeSet(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  changeSetId: string,
  opIndexes?: number[] | null,
  focus?: string | null,
): Promise<StudioChangeSetPreview> {
  return apiClient.get<StudioChangeSetPreview>(
    `${changeSetPath(namespace, proposalId, changeSetId)}/preview${query({
      opIndexes: opIndexes ? opIndexes.join(",") : undefined,
      focus: focus ?? undefined,
    })}`,
  );
}

/** Apply the selected ops (all for none) as one revision. */
export function applyStudioChangeSet(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  changeSetId: string,
  expectedRevision: number,
  opIndexes?: number[] | null,
): Promise<StudioChangeSetApplyResult> {
  return apiClient.post<StudioChangeSetApplyResult>(
    `${changeSetPath(namespace, proposalId, changeSetId)}/apply`,
    opIndexes ? { expectedRevision, opIndexes } : { expectedRevision },
  );
}

export function discardStudioChangeSet(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  changeSetId: string,
): Promise<StudioChangeSet> {
  return apiClient.post<StudioChangeSet>(
    `${changeSetPath(namespace, proposalId, changeSetId)}/discard`,
    {},
  );
}

/** Another turn is still running (409 TurnInProgress). */
export function isTurnInProgress(e: unknown): boolean {
  return e instanceof ApiError && e.code === "TurnInProgress";
}

/** Another change landed first: reload the draft. */
export function isDraftConflict(e: unknown): boolean {
  return e instanceof ApiError && e.code === "DraftConflict";
}

/** A newer version was published after the draft was opened. */
export function isRebaseRequired(e: unknown): boolean {
  return e instanceof ApiError && e.code === "RebaseRequired";
}

/** The ops a rebase could not replay, when `e` is a `RebaseFailed` 409. */
export function rebaseFailures(e: unknown): StudioRebaseFailure[] {
  if (!(e instanceof ApiError) || e.code !== "RebaseFailed") return [];
  const failures = studioErrorDetail(e)?.failures;
  return Array.isArray(failures) ? (failures as StudioRebaseFailure[]) : [];
}

// ── Checks, the query console and competency questions ───────────────────

export type StudioFindingSeverity = "error" | "warning" | "info";

export interface StudioFinding {
  /** DANGLING_DOMAIN, DANGLING_RANGE, SUBCLASS_CYCLE, MISSING_LABEL, DUPLICATE_LABEL, ISOLATED_CLASS or DATATYPE_RANGE_NOT_DATATYPE. */
  code: string;
  severity: StudioFindingSeverity;
  /** In English. */
  message: string;
  /** The sentence of `message` as a code; older engines do not send it. */
  messageCode?: string;
  /** The values `messageCode` names. */
  messageParams?: Record<string, string>;
  iris: string[];
}

/**
 * Each data section is best effort: `error` is set, and the rest absent, when
 * it could not be checked; `errorCode` is the code of `error` (older engines
 * do not send it).
 */
export interface StudioDataChecks {
  tables: {
    checked?: boolean;
    missing?: string[];
    error: string | null;
    errorCode?: string | null;
  };
  abox: {
    available?: boolean;
    current?: boolean;
    impacts?: {
      classIri: string;
      instances: number;
      /** "renamed" and "merged" name the class of the draft that takes the instances in `toIri`; a rename whose target left the draft is "removed". */
      change: "removed" | "renamed" | "merged";
      toIri: string | null;
    }[];
    error: string | null;
    errorCode?: string | null;
  };
  mappings: {
    available?: boolean;
    stale?: number;
    staleTargets?: string[];
    error: string | null;
    errorCode?: string | null;
  };
  metrics: {
    breaking?: BreakingMetric[];
    error: string | null;
    errorCode?: string | null;
  };
}

/** A term of the structured-query check; the group it is in decides which optional field is set. */
export interface StudioStructuredTerm {
  iri: string;
  label: string;
  /**
   * `unmappedProperties`: the mapped class the property is listed for, its
   * first mapped domain class, or, for a relationship none of whose domains
   * is mapped, its first mapped range class. `removedMappedProperties`: the
   * class whose table mapped it in the version the draft was built on.
   */
  domainIri?: string;
  /**
   * `labelDiffers`: the table or column name structured queries keep using.
   * `removedMappedProperties`: the column an attribute was read from.
   */
  physicalName?: string;
  /**
   * `multiTableClasses`: the tables the class is mapped from.
   * `removedMappedClasses`: the tables it was mapped from.
   */
  tables?: string[];
}

export interface StudioStructuredGroup {
  /** Every term, counted. */
  count: number;
  /** At most 100, sorted by label. */
  terms: StudioStructuredTerm[];
}

/**
 * What structured queries (Ontop and NL-to-SQL) cannot answer in the draft,
 * and the mapped terms they know by their table or column name.
 */
export interface StudioStructuredCheck {
  unmappedClasses: StudioStructuredGroup;
  unmappedProperties: StudioStructuredGroup;
  labelDiffers: StudioStructuredGroup;
  multiTableClasses: StudioStructuredGroup;
  /**
   * Mapped classes of the version the draft was built on that it deletes
   * (renamed or merged ones keep their maps). Optional: older engines do not
   * send it.
   */
  removedMappedClasses?: StudioStructuredGroup;
  /**
   * Mapped attributes and relationships of that version that the draft
   * deletes. Optional: older engines do not send it.
   */
  removedMappedProperties?: StudioStructuredGroup;
  /** The draft maps no table although the version it was built on did. */
  mapsNoTable: boolean;
  /**
   * A fixed message, shown as it is, when the version the draft was built on
   * could not be read: `mapsNoTable` is false and nothing is listed as deleted.
   */
  error?: string | null;
  /** The code of `error`; older engines do not send it. */
  errorCode?: string | null;
}

export interface StudioChecks {
  revision: number;
  instant: {
    findings: StudioFinding[];
    counts: Record<StudioFindingSeverity, number>;
    /** More than 200 findings: only the worst 200 are listed. */
    truncated: boolean;
  };
  /** Optional: older engines do not send it. */
  structured?: StudioStructuredCheck;
  data: StudioDataChecks;
}

export function getStudioChecks(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
): Promise<StudioChecks> {
  return apiClient.get<StudioChecks>(
    `${studioPath(namespace, proposalId)}/checks`,
  );
}

export type StudioQueryLanguage = "sparql" | "cypher";
export type StudioQueryTarget = "draft" | "lexical";

export interface StudioQueryCell {
  type: "uri" | "literal" | "bnode" | "value";
  value: string;
  datatype?: string;
  lang?: string;
}

export interface StudioQueryResult {
  language: StudioQueryLanguage;
  target: StudioQueryTarget;
  revision: number;
  /** SELECT, ASK, CONSTRUCT or DESCRIBE; MATCH for Cypher. */
  form: string;
  columns: string[];
  /** An unbound variable is null. */
  rows: (StudioQueryCell | null)[][];
  rowCount: number;
  truncated: boolean;
  elapsedMs: number;
}

/** Run a read-only query: SPARQL on the draft and its documents, or Cypher on the lexical graph. */
export function runStudioQuery(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  language: StudioQueryLanguage,
  target: StudioQueryTarget,
  queryText: string,
): Promise<StudioQueryResult> {
  return apiClient.post<StudioQueryResult>(
    `${studioPath(namespace, proposalId)}/queries`,
    { language, target, query: queryText },
  );
}

export interface VkgQueryResult {
  columns: string[];
  /** Plain values in column order; an unbound variable is null. */
  rows: unknown[][];
  rowCount: number;
  truncated: boolean;
  /** The SQL that ran, after the firewall. */
  executedSql: string | null;
}

/** Run a SPARQL SELECT or ASK over the namespace's VKG with the caller's own grants (data layer). */
export function runSparqlQuery(
  apiClient: ApiClient,
  namespace: string,
  sparql: string,
): Promise<VkgQueryResult> {
  return apiClient.post<VkgQueryResult>(
    `/namespaces/${encodeURIComponent(namespace)}/sparql`,
    { sparql },
  );
}

export type StudioQuestionStatus =
  | "answerable"
  | "expressible"
  | "not_expressible";

export interface StudioQuestion {
  questionId: string;
  text: string;
  expectation: string | null;
  /** The assistant's latest check; cleared when the text changes. */
  lastCheck: {
    proposalId: string;
    revision: number;
    status: StudioQuestionStatus;
    query: string | null;
    rowCount: number;
    sample: (StudioQueryCell | null)[][];
    explanation: string | null;
    checkedAt: string;
  } | null;
  createdAt: string | null;
  updatedAt: string | null;
  updatedBy: string | null;
}

function questionsPath(namespace: string): string {
  return `/namespaces/${encodeURIComponent(namespace)}/studio/questions`;
}

export interface StudioQuestions {
  /** This page, oldest first. */
  questions: StudioQuestion[];
  /** Pass back to read the next page; absent on the last page. */
  nextToken?: string;
}

/** One page of the namespace's competency questions, shared by every draft, oldest first. */
export function listStudioQuestions(
  apiClient: ApiClient,
  namespace: string,
  opts: { maxResults?: number; nextToken?: string } = {},
): Promise<StudioQuestions> {
  return apiClient.get<StudioQuestions>(
    `${questionsPath(namespace)}${query({
      maxResults: opts.maxResults,
      nextToken: opts.nextToken,
    })}`,
  );
}

/** Create or replace a question; the client mints `questionId`. */
export function putStudioQuestion(
  apiClient: ApiClient,
  namespace: string,
  questionId: string,
  text: string,
  expectation: string | null,
): Promise<StudioQuestion> {
  return apiClient.put<StudioQuestion>(
    `${questionsPath(namespace)}/${encodeURIComponent(questionId)}`,
    expectation ? { text, expectation } : { text },
  );
}

export function deleteStudioQuestion(
  apiClient: ApiClient,
  namespace: string,
  questionId: string,
): Promise<void> {
  return apiClient.del<void>(
    `${questionsPath(namespace)}/${encodeURIComponent(questionId)}`,
  );
}

/** Ask the assistant to check a question against the draft (202): a turn, like a message. */
export function checkStudioQuestion(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  questionId: string,
): Promise<{ turnId: string; seq: number }> {
  return apiClient.post<{ turnId: string; seq: number }>(
    `${studioPath(namespace, proposalId)}/questions/${encodeURIComponent(questionId)}/check`,
    {},
  );
}

/** A new question id: letters, digits and hyphens, as the server requires. */
export function newQuestionId(): string {
  return crypto.randomUUID();
}
