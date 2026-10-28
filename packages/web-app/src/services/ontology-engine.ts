// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Ontology Engine API client.
 *
 * All requests go through API Gateway (signed by the OIDC bearer token via
 * ApiClient). Pass `apiClient` (from `useApiClient()`) into each function.
 *
 * Backend FastAPI routes are exposed under namespace-scoped paths by the API
 * Gateway → Lambda proxy in `infra/bin/app.ts` and
 * `packages/ontology-engine/.../api_proxy_handler.py`.
 */
import type { OntologyRecord } from "@coa/control-plane-client";
import type { ApiClient } from "../components/ApiClientProvider";
import { collectAllPages } from "../utils/pagination";

export { LIST_ALL_MAX_PAGES } from "../utils/pagination";
import type { DeployStep } from "./ontology-versions-types";
import type {
  AboxMappingDirection,
  AboxMappingKind,
  AboxMappingStatus,
  DomainAboxStatus,
} from "./abox-types";

// ─── Types ─────────────────────────────────────────────────────────────

export type InductionJobStage =
  | "pending"
  | "fetching_metadata"
  | "generating_embeddings"
  | "matching"
  | "building_ontology"
  | "validating"
  | "storing"
  | "completed"
  | "failed"
  | "ingested";

export interface InductionJob {
  job_id: string;
  status: InductionJobStage;
  created_at: string;
  error?: string;
  report?: Record<string, unknown>;
  /** Set when duplicate detection short-circuits the run: the ``proposal_id``
   * of the already-accepted proposal with an identical structural fingerprint.
   * No new proposal is created in that case, so the client must redirect HERE
   * (not to the job id, which has no proposal row → 404). */
  duplicate_of?: string;
}

export interface Proposal {
  proposal_id: string;
  job_id: string;
  namespace: string;
  ontology_id: string;
  proposal_type: string;
  source_type?: string;
  status: string;
  created_at: string;
  updated_at: string;
  metadata?: Record<string, unknown>;
  ontology_turtle?: string;
  r2rml_turtle?: string;
  /** Presigned S3 GET URLs for the (potentially multi-MB) Turtle artifacts.
   * The detail endpoint serves these instead of inlining the Turtle, which
   * would exceed the 6 MB API Gateway / Lambda response limit. ``null`` when
   * the artifact does not exist. Fetch directly from S3 (not via apiClient). */
  ontology_url?: string | null;
  r2rml_url?: string | null;
  /** Presigned S3 GET URL for the grounding-match list (``matches.json``).
   * Served out-of-band for the same reason as the Turtle: the list is one entry
   * per grounded table, each with up to ~30 candidate classes, so a wide schema
   * can exceed the 6 MB response limit. ``null`` when there is no offloaded
   * matches object — either an all-novel induction (no matches) or a legacy
   * proposal that still carries the list inline in ``metadata.matches``. Fetch
   * directly from S3 (not via apiClient). */
  matches_url?: string | null;
  /** Presigned S3 GET URL for the SHACL constraint config
   * (``metadata.constraint_config``). Served out-of-band for the same reason as
   * the Turtle and matches: the config is one entry per class with one entry per
   * constrained property, so a wide schema can exceed the 6 MB response limit.
   * ``null`` when there is no offloaded constraints object — either a proposal
   * with no constraints, or a legacy proposal that still carries the config
   * inline in ``metadata.constraint_config``. Fetch directly from S3 (not via
   * apiClient). The S3 body is the config object itself, not an envelope. */
  constraints_url?: string | null;
  /** Reason from the most recent failed accept attempt, naming the pipeline
   * step that failed (ingest / embeddings_searchable / reconcile_counts /
   * s3_persist). Set together with status === "accept_failed"; cleared on the
   * next successful accept. Legacy failures may instead carry it on
   * status === "pending". */
  accept_error?: string;
  /** The accept's last step. ``code`` is "RebaseRequired" when a newer version beat a designer
   * draft's publish; read it only while status === "accept_failed" (a later rebase leaves it). */
  accept_progress?: { step: string; state: string; code?: string };
  /** Version this proposal's accept published; set once status === "accepted". */
  published_version?: number;
  /** Why the automatic deployment after accept did not start (absent when it started or was not requested).
   * Also briefly absent right after status flips to "accepted": the accept
   * worker records the deploy outcome (this or auto_deploy_id) only afterwards. */
  auto_deploy_held?: AutoDeployHeld;
  /** Id of the deployment that accept started automatically (absent when none was started). */
  auto_deploy_id?: string;
  /** Ontology designer draft state; absent until the proposal is opened in the designer.
   * Its Turtle editors are then read-only and accept needs ``expectedRevision``. */
  studio?: ProposalStudio | null;
}

/** The ``studio`` field of a proposal open in the Ontology designer. */
export interface ProposalStudio {
  base_version: number | null;
  revision: number;
  opened_at: string;
  opened_by: string;
}

/** Response from POST /accept (202 Accepted).
 *
 * The accept route is async — it returns immediately after flipping the
 * proposal to ``accepting``. The terminal status (``accepted`` on success, or
 * ``accept_failed`` with ``accept_error`` naming the failed step) is observable
 * on the proposal itself via GET /proposals/{id}, same pattern as data-source
 * scan/enrichment uses for source.status. An ``accept_failed`` proposal stays
 * re-acceptable — the accept pipeline is idempotent on re-run.
 */
export interface AcceptResult {
  proposal_id: string;
  status: string;
}

/**
 * Re-exported from the generated client rather than hand-declared.
 *
 * There used to be a second, snake_case `OntologyRecord` here. It drifted from
 * the Smithy contract (which declares camelCase inside a `{ontologies: [...]}`
 * envelope), and because the two shared a name the mismatch was invisible: the
 * generated client deserialised `out.ontologies` to `undefined`, callers turned
 * that into `[]`, and features silently did nothing. `ListOntologies` now
 * serialises the declared shape (see the `by_alias` route in
 * `catalog/routers/ontologies.py` and `test_list_ontologies_wire_shape.py`), so
 * one type covers both the REST helper below and the react-query hook.
 */
export type { OntologyRecord };

export interface GraphSearchHit {
  uri: string;
  label: string;
  kind: string;
  /** All named graphs the entity is defined in. A class IRI asserted in more
   *  than one ontology (e.g. a shared IRI referenced by both schema.org and
   *  FIBO) lists every source graph, not just one. */
  graph_uris: string[];
}

/** A page of graph-search hits plus the total match count across all pages.
 *  Backs the Explorer's server-side pagination (Cloudscape "1 of N"). */
export interface GraphSearchResult {
  hits: GraphSearchHit[];
  total_count: number;
}

export interface GraphVertex {
  uri: string;
  kind: string;
  labels: string[];
  comments: string[];
  alt_labels: string[];
  graph_uris: string[];
  edges: Array<{
    predicate: string;
    predicate_label: string;
    direction: string;
    neighbor: { uri: string; label: string | null; kind: string };
  }>;
  /** True for a synthesized taxonomy vertex built from the ontology-overview
   *  (carries only rdfs:subClassOf edges, enough to draw the hierarchy). Its
   *  full relationships/attributes are fetched on demand when the node is
   *  selected. Absent/false for a vertex loaded via getClass. */
  partial?: boolean;
}

export interface ValidationJob {
  job_id: string;
  status: string;
  created_at: string;
  error?: string;
  report?: {
    passed: boolean;
    findings: Array<{ tier: number; code: string; message: string }>;
    metrics?: Record<string, unknown>;
  };
}

export interface ProposalValidationFinding {
  validator: string;
  tier: string;
  severity: "error" | "warning" | "info";
  code: string;
  message: string;
  subject?: string;
  details?: Record<string, unknown>;
}

export interface ProposalValidationJob {
  job_id: string;
  proposal_id: string;
  status: "pending" | "running" | "completed" | "failed";
  created_at: string;
  error?: string | null;
  report?: {
    job_id: string;
    proposal_id: string;
    passed: boolean;
    findings: ProposalValidationFinding[];
    metrics?: Record<string, number | null> | null;
    tiers_run: string[];
    created_at: string;
  } | null;
}

// ─── Helpers ───────────────────────────────────────────────────────────

function qs(params: Record<string, string | number | undefined>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined);
  return entries.length
    ? "?" +
        new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString()
    : "";
}

// ─── Induction Jobs ────────────────────────────────────────────────────

export interface TriggerInductionInput {
  datasource_ids: string[];
  ontology_uri_prefix: string;
  namespace: string;
  label?: string;
  confidence_threshold?: number;
  grounding_ontology_ids?: string[];
  strategy?: string;
  grounding_mode?: string;
  graph_arn?: string;
}

export async function triggerInduction(
  apiClient: ApiClient,
  namespace: string,
  input: TriggerInductionInput,
): Promise<InductionJob> {
  return apiClient.post<InductionJob>(
    `/namespaces/${encodeURIComponent(namespace)}/induce`,
    input,
  );
}

export async function getInductionJob(
  apiClient: ApiClient,
  namespace: string,
  jobId: string,
): Promise<InductionJob> {
  return apiClient.get<InductionJob>(
    `/namespaces/${encodeURIComponent(namespace)}/induce/jobs/${encodeURIComponent(jobId)}`,
  );
}

// ─── Proposals ─────────────────────────────────────────────────────────

/** One page of the Smithy ``ListProposals`` output. */
export interface ListProposalsPage {
  proposals: Proposal[];
  /** Present only when more proposals may follow; pass back as ``nextToken``. */
  nextToken?: string;
}

type ListProposalsOpts = {
  ontologyId?: string;
  status?: string;
  type?: string;
  source_type?: string;
  maxResults?: number;
  nextToken?: string;
};

/**
 * Fetch one page of a namespace's proposals (the first page unless
 * ``opts.nextToken`` is given). Query keys use the contract's camelCase.
 *
 * Also accepts the pre-pagination bare-array response, so the UI keeps working
 * against a backend rolled back to before the ``ListProposals`` envelope.
 */
export async function listProposalsPage(
  apiClient: ApiClient,
  namespace: string,
  opts?: ListProposalsOpts,
): Promise<ListProposalsPage> {
  const page = await apiClient.get<ListProposalsPage | Proposal[]>(
    `/namespaces/${encodeURIComponent(namespace)}/proposals${qs(opts ?? {})}`,
  );
  if (Array.isArray(page)) return { proposals: page };
  if (!Array.isArray(page?.proposals)) {
    console.warn("ListProposals response has no proposals list", {
      namespace,
    });
    return { proposals: [], nextToken: page?.nextToken };
  }
  return { proposals: page.proposals, nextToken: page.nextToken };
}

/**
 * List the first page of a namespace's proposals. Use
 * {@link listAllProposals} when the caller needs every proposal (e.g. to
 * decide whether ANY proposal blocks a new induction).
 */
export async function listProposals(
  apiClient: ApiClient,
  namespace: string,
  opts?: ListProposalsOpts,
): Promise<Proposal[]> {
  return (await listProposalsPage(apiClient, namespace, opts)).proposals;
}

/**
 * List every proposal in a namespace by following ``nextToken`` to the end
 * (see {@link collectAllPages} for the short/empty-page and loop-guard rules).
 */
export async function listAllProposals(
  apiClient: ApiClient,
  namespace: string,
  opts?: Omit<ListProposalsOpts, "nextToken">,
): Promise<Proposal[]> {
  return collectAllPages("ListProposals", async (nextToken) => {
    const page = await listProposalsPage(apiClient, namespace, {
      ...opts,
      nextToken,
    });
    return { items: page.proposals, nextToken: page.nextToken };
  });
}

export async function getProposal(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
): Promise<Proposal> {
  return apiClient.get<Proposal>(
    `/namespaces/${encodeURIComponent(namespace)}/proposals/${encodeURIComponent(proposalId)}`,
  );
}

/**
 * Fetch a Turtle artifact directly from a presigned S3 URL.
 *
 * The proposal ontology/r2rml Turtle is served out-of-band (presigned S3 GET)
 * rather than inlined in the proposal response — a large induced ontology
 * (6+ MB) would exceed the 6 MB API Gateway / Lambda response limit. This goes
 * straight to S3 (the presigned URL carries its own auth), so it must NOT go
 * through apiClient.
 */
export async function fetchTurtleFromUrl(url: string): Promise<string> {
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(
      `Failed to download Turtle from S3 (${resp.status} ${resp.statusText})`,
    );
  }
  return resp.text();
}

/**
 * Fetch a JSON artifact directly from a presigned S3 URL — the JSON sibling of
 * {@link fetchTurtleFromUrl}, used for the grounding-match list served via
 * ``Proposal.matches_url``. Goes straight to S3 (the presigned URL carries its
 * own auth), so it must NOT go through apiClient. Returns the parsed body as
 * ``unknown``; the caller narrows it (e.g. ``parseConceptMatches``).
 */
export async function fetchJsonFromUrl(url: string): Promise<unknown> {
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(
      `Failed to download JSON from S3 (${resp.status} ${resp.statusText})`,
    );
  }
  try {
    return await resp.json();
  } catch (e) {
    // Malformed JSON from S3 throws a bare SyntaxError with no artifact context;
    // wrap it so the failing URL is identifiable when debugging.
    const detail = e instanceof Error ? e.message : String(e);
    throw new Error(`Failed to parse JSON from S3 (${url}): ${detail}`, {
      cause: e,
    });
  }
}

/**
 * Upload Turtle directly to S3 via a create-only presigned PUT URL.
 *
 * The URL carries its own authorization and signs ``If-None-Match: *``, so
 * this request must bypass apiClient and include that exact header. The
 * precondition prevents a replay from replacing bytes that UpdateProposal
 * already validated. Content-Type remains unsigned.
 */
export async function putToPresignedUrl(
  url: string,
  body: BodyInit,
  contentType = "text/turtle",
): Promise<void> {
  const resp = await fetch(url, {
    method: "PUT",
    headers: {
      "Content-Type": contentType,
      "If-None-Match": "*",
    },
    body,
  });
  if (resp.ok) return;

  if (resp.status === 409 || resp.status === 412) {
    throw new Error(
      `The proposal revision already has an upload or the write outcome conflicted (${resp.status} ${resp.statusText}). Save again to create a fresh revision and re-upload.`,
    );
  }
  if (resp.status === 403) {
    throw new Error(
      `Failed to upload to S3 (403 ${resp.statusText}). The upload URL may have expired or a required signed header may be missing. Save again to request a fresh revision.`,
    );
  }
  throw new Error(`Failed to upload to S3 (${resp.status} ${resp.statusText})`);
}

/**
 * Request presigned S3 PUT URLs for a proposal's ontology / R2RML artifacts.
 * The client uploads edited Turtle straight to S3 with {@link putToPresignedUrl},
 * then calls {@link updateProposal} with ``ontology_uploaded`` / ``r2rml_uploaded``.
 */
export async function getProposalUploadUrls(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
): Promise<{
  revisionId: string;
  ontologyPutUrl: string;
  r2rmlPutUrl: string;
}> {
  return apiClient.post<{
    revisionId: string;
    ontologyPutUrl: string;
    r2rmlPutUrl: string;
  }>(
    `/namespaces/${encodeURIComponent(namespace)}/proposals/${encodeURIComponent(proposalId)}/upload-url`,
    {},
  );
}

export async function acceptProposal(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  opts?: { ontology_id?: string; deploy?: boolean; expectedRevision?: number },
): Promise<AcceptResult> {
  const body: Record<string, unknown> = {};
  if (opts?.ontology_id) body.ontology_id = opts.ontology_id;
  if (opts?.deploy !== undefined) body.deploy = opts.deploy;
  // Required by the engine for a proposal open in the Ontology designer: the reviewed revision.
  // Errors then: 422 ExpectedRevisionRequired, 409 DraftConflict, 409 RebaseRequired. A newer
  // version landing after those checks fails the accept later: status "accept_failed" with
  // accept_progress.code "RebaseRequired". That code is only meaningful while the status is
  // "accept_failed": the row can keep the old one after a successful rebase.
  if (opts?.expectedRevision !== undefined)
    body.expectedRevision = opts.expectedRevision;
  return apiClient.post<AcceptResult>(
    `/namespaces/${encodeURIComponent(namespace)}/proposals/${encodeURIComponent(proposalId)}/accept`,
    body,
  );
}

export async function cancelProposal(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
): Promise<{ proposal_id: string; status: string }> {
  return apiClient.post<{ proposal_id: string; status: string }>(
    `/namespaces/${encodeURIComponent(namespace)}/proposals/${encodeURIComponent(proposalId)}/cancel`,
    {},
  );
}

export async function updateProposal(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  fields: {
    ontology_turtle?: string;
    r2rml_turtle?: string;
    ontology_uploaded?: boolean;
    r2rml_uploaded?: boolean;
    grounding_overrides?: Record<string, string>;
    novel_tables?: string[];
    upload_revision_id?: string;
  },
): Promise<{ proposal_id: string; status: string }> {
  return apiClient.post<{ proposal_id: string; status: string }>(
    `/namespaces/${encodeURIComponent(namespace)}/proposals/update`,
    { proposal_id: proposalId, ...fields },
  );
}

export async function rejectProposals(
  apiClient: ApiClient,
  namespace: string,
  proposalIds: string[],
  reason: string,
): Promise<Array<{ proposal_id: string; status?: string; error?: string }>> {
  return apiClient.post<
    Array<{ proposal_id: string; status?: string; error?: string }>
  >(`/namespaces/${encodeURIComponent(namespace)}/proposals/reject`, {
    proposal_ids: proposalIds,
    reason,
  });
}

export async function validateProposal(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  body?: {
    tiers?: string[];
    shacl_shapes_turtle?: string;
    competency_questions?: string[];
  },
): Promise<ProposalValidationJob> {
  return apiClient.post<ProposalValidationJob>(
    `/namespaces/${encodeURIComponent(namespace)}/proposals/${encodeURIComponent(proposalId)}/validate`,
    body ?? {},
  );
}

export async function getProposalValidationJob(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  jobId: string,
): Promise<ProposalValidationJob> {
  return apiClient.get<ProposalValidationJob>(
    `/namespaces/${encodeURIComponent(namespace)}/proposals/${encodeURIComponent(proposalId)}/validate/jobs/${encodeURIComponent(jobId)}`,
  );
}

/**
 * A single malformed datatype token flagged by the MALFORMED_DATATYPE_TOKENS
 * validation finding (carried in `finding.details.offenders`). `found` is the
 * offending datatype IRI, `canonical` its proposed replacement.
 */
export interface DatatypeOffender {
  subject: string;
  predicate: string;
  found: string;
  canonical: string;
}

export interface DatatypeRepairResult {
  proposal_id: string;
  repaired_count: number;
  changes: Array<{ found: string; canonical: string }>;
  dry_run?: boolean;
  /** The draft's revision after a repair of a proposal open in the Ontology designer (not on a preview). */
  revision?: number;
}

/**
 * Apply — or, with `dryRun`, preview — the consent-gated repair of malformed/
 * mis-cased/aliased XSD datatype tokens surfaced by
 * {@link ProposalValidationFinding} MALFORMED_DATATYPE_TOKENS. Server-
 * authoritative: the canonical XSD map lives on the server. Idempotent — a clean
 * proposal returns `repaired_count: 0`. `dryRun` returns the change count without
 * persisting (used by the accept-time guard to detect unrepaired tokens).
 */
export async function repairProposalDatatypes(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  opts?: { dryRun?: boolean; expectedRevision?: number },
): Promise<DatatypeRepairResult> {
  const body: Record<string, unknown> = {};
  if (opts?.dryRun) body.dry_run = true;
  // Required to repair (not to preview) a proposal open in the Ontology designer, whose draft is
  // changed instead of the stored Turtle. Errors: 422 ExpectedRevisionRequired, 409 DraftConflict,
  // ProposalNotEditable and InvalidChange.
  if (opts?.expectedRevision !== undefined)
    body.expectedRevision = opts.expectedRevision;
  return apiClient.post<DatatypeRepairResult>(
    `/namespaces/${encodeURIComponent(namespace)}/proposals/${encodeURIComponent(proposalId)}/repair-datatypes`,
    body,
  );
}

// ─── Constraint Config ────────────────────────────────────────────────

/**
 * Async constraint-inference job. ``POST /infer-constraints`` now returns
 * 202 + this handle immediately (the Bedrock call can exceed API Gateway's
 * 29s timeout); poll ``getInferConstraintsJob`` until ``completed``/``failed``.
 * Mirrors {@link ProposalValidationJob}.
 */
export interface InferConstraintsJob {
  job_id: string;
  proposal_id: string;
  status: "pending" | "running" | "completed" | "failed";
  created_at: string;
  error?: string | null;
  result?: { inferred_constraints: unknown } | null;
}

export async function inferConstraints(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
): Promise<InferConstraintsJob> {
  // Returns 202 + a job handle; poll getInferConstraintsJob for the result.
  return apiClient.post<InferConstraintsJob>(
    `/namespaces/${encodeURIComponent(namespace)}/proposals/${encodeURIComponent(proposalId)}/infer-constraints`,
    {},
  );
}

export async function getInferConstraintsJob(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  jobId: string,
): Promise<InferConstraintsJob> {
  return apiClient.get<InferConstraintsJob>(
    `/namespaces/${encodeURIComponent(namespace)}/proposals/${encodeURIComponent(proposalId)}/infer-constraints/jobs/${encodeURIComponent(jobId)}`,
  );
}

export async function compileConstraints(
  apiClient: ApiClient,
  namespace: string,
  proposalId: string,
  constraintConfig: unknown,
  customTurtle?: string,
): Promise<{ proposal_id: string; shapes_turtle: string }> {
  return apiClient.post<{ proposal_id: string; shapes_turtle: string }>(
    `/namespaces/${encodeURIComponent(namespace)}/proposals/${encodeURIComponent(proposalId)}/compile-constraints`,
    {
      constraint_config: constraintConfig,
      ...(customTurtle && { custom_turtle: customTurtle }),
    },
  );
}

// ─── Ontology Catalog ──────────────────────────────────────────────────

/**
 * A listed ontology whose id is known to be present.
 *
 * Smithy marks `ontologyId` `@required`, but the TypeScript generator types
 * every member as `T | undefined`, so that guarantee doesn't survive codegen.
 * Narrowing once at the boundary is what keeps callers from coercing with
 * `?? ""` at each use site — an id-less row cannot be linked to, keyed in a
 * table, or deleted, so it is unusable rather than merely awkward.
 */
export type ListedOntology = OntologyRecord & { ontologyId: string };

/** Runtime narrowing for {@link ListedOntology}; see the type's note. */
export function hasOntologyId(o: OntologyRecord): o is ListedOntology {
  return typeof o.ontologyId === "string" && o.ontologyId.length > 0;
}

/**
 * List a namespace's ontologies. Follows ``nextToken`` across every page unless
 * ``opts.limit`` is given, in which case only that one bounded page is fetched.
 */
export async function listOntologies(
  apiClient: ApiClient,
  namespace: string,
  opts?: {
    ontology_type?: string;
    domain_tag?: string;
    limit?: number;
  },
): Promise<ListedOntology[]> {
  // Unwraps the `{ontologies: [...]}` envelope the Smithy contract declares.
  // `?? []` guards a legacy deployment still emitting a bare array: callers get
  // an empty list rather than a crash mid-render.
  const fetchPage = async (nextToken: string | undefined) => {
    const out = await apiClient.get<{
      ontologies?: OntologyRecord[];
      nextToken?: string;
    }>(
      `/namespaces/${encodeURIComponent(namespace)}/ontologies${qs({ ...opts, nextToken })}`,
    );
    return { items: out.ontologies ?? [], nextToken: out.nextToken };
  };
  const rows =
    opts?.limit !== undefined
      ? (await fetchPage(undefined)).items
      : await collectAllPages("ListOntologies", fetchPage);
  return rows.filter(hasOntologyId);
}

/**
 * Delete an ontology and all its artifacts (graph triples, vector embeddings,
 * registry entry, and the accepted proposals that produced it) from a
 * namespace. Async: the backend returns 202 immediately, flips the registry
 * row to ``status: "deleting"`` (so it stays listed as "Delete in progress"),
 * and removes the row once the graph + embedding teardown completes. Callers
 * should poll listOntologies until the id is gone.
 *
 * The ontology id is a full IRI (contains `#`), so it goes in the `ontology_id`
 * query param — API Gateway cannot route an encoded `#` in a path segment. This
 * mirrors the graph vertex-lookup endpoints (`/graph/class?uri=`).
 */
export async function deleteOntology(
  apiClient: ApiClient,
  namespace: string,
  ontologyId: string,
): Promise<void> {
  return apiClient.del<void>(
    `/namespaces/${encodeURIComponent(namespace)}/ontologies?ontology_id=${encodeURIComponent(ontologyId)}`,
  );
}

// ─── Graph (entity search) ─────────────────────────────────────────────

/**
 * Fetch one page of graph-search hits plus the total match count. The backend
 * returns hits in a deterministic order, so ``limit`` / ``offset`` yield stable
 * non-overlapping windows — this is the primitive the Explorer's server-side
 * pagination builds on. Prefer this over {@link searchEntities} when paging.
 */
export async function searchEntitiesPaged(
  apiClient: ApiClient,
  namespace: string,
  query: string,
  opts?: {
    kind?: string;
    ontology_id?: string;
    exclude_ontology_id?: string;
    limit?: number;
    offset?: number;
  },
): Promise<GraphSearchResult> {
  return apiClient.get<GraphSearchResult>(
    `/namespaces/${encodeURIComponent(namespace)}/graph/search${qs({ q: query, ...opts })}`,
  );
}

/**
 * Convenience wrapper returning just the hits array (first/only page). Kept for
 * callers that don't paginate (e.g. the metric-form entity picker and the graph
 * view's neighbour search); the backend response is the ``{hits, total_count}``
 * envelope, from which this unwraps ``hits``.
 */
export async function searchEntities(
  apiClient: ApiClient,
  namespace: string,
  query: string,
  opts?: {
    kind?: string;
    ontology_id?: string;
    exclude_ontology_id?: string;
    limit?: number;
    offset?: number;
  },
): Promise<GraphSearchHit[]> {
  const result = await searchEntitiesPaged(apiClient, namespace, query, opts);
  return result.hits;
}

export async function getClass(
  apiClient: ApiClient,
  namespace: string,
  classUri: string,
): Promise<GraphVertex> {
  return apiClient.get<GraphVertex>(
    `/namespaces/${encodeURIComponent(namespace)}/graph/class${qs({ uri: classUri })}`,
  );
}

/** One class as returned by the ontology-overview endpoint. */
export interface OntologyOverviewClass {
  uri: string;
  label: string | null;
  comment: string | null;
  groundedTo?: string | null;
  matchType?: string | null;
  /** IRIs of this class's direct super-classes (rdfs:subClassOf). Lets the
   *  Graph view reconstruct the taxonomy from the overview alone, without a
   *  per-class vertex fan-out. */
  subClassOf?: string[] | null;
}

/** One property as returned by the ontology-overview endpoint. Shape mirrors
 *  the Smithy ``OntologyPropertySummary`` structure — object and datatype
 *  properties share the same envelope (the range's IRI distinguishes them). */
export interface OntologyPropertySummary {
  uri: string;
  label?: string | null;
  /** IRI of the property's domain class. */
  domain?: string | null;
  /** IRI of the property's range (a class IRI for object properties, a
   *  datatype IRI for datatype properties). */
  range?: string | null;
}

export interface OntologyOverviewResult {
  ontology_id: string;
  namespace?: string;
  graph_uri?: string | null;
  classes: OntologyOverviewClass[];
  /** Named-IRI object properties (domain-class → range-class relationships).
   *  Populated on the full overview; empty on ``sample=true``. */
  objectProperties?: OntologyPropertySummary[] | null;
  /** Named-IRI datatype properties (class → literal-range attributes). Not
   *  used by the graph seed today, kept on the type so callers see it. */
  datatypeProperties?: OntologyPropertySummary[] | null;
}

/**
 * Enumerate one ontology's classes (+ descriptions/grounding) in a single
 * SPARQL round-trip. Much cheaper than N per-class ``getClass`` calls when
 * you only need class metadata (e.g. the Ontology Explorer list view).
 */
export async function getOntologyOverview(
  apiClient: ApiClient,
  namespace: string,
  ontologyId: string,
  opts?: { sample?: boolean },
): Promise<OntologyOverviewResult> {
  return apiClient.get<OntologyOverviewResult>(
    `/namespaces/${encodeURIComponent(namespace)}/graph/ontology-overview${qs({
      ontology_id: ontologyId,
      // Bounded taxonomy seed for the Graph view's initial render (a few roots
      // + capped descendants, no properties). Omitted → full overview.
      ...(opts?.sample ? { sample: "true" } : {}),
    })}`,
  );
}

export async function getObjectProperty(
  apiClient: ApiClient,
  namespace: string,
  propUri: string,
): Promise<GraphVertex> {
  return apiClient.get<GraphVertex>(
    `/namespaces/${encodeURIComponent(namespace)}/graph/object-property${qs({ uri: propUri })}`,
  );
}

export async function getDatatypeProperty(
  apiClient: ApiClient,
  namespace: string,
  propUri: string,
): Promise<GraphVertex> {
  return apiClient.get<GraphVertex>(
    `/namespaces/${encodeURIComponent(namespace)}/graph/datatype-property${qs({ uri: propUri })}`,
  );
}

/**
 * Export an ontology serialized as Turtle. The backend serves the
 * ontology's named graph (the source of truth) as ``text/turtle`` from
 * ``/ontologies/{id}/download`` — every triple currently in the graph.
 * Returned as a raw string for inline display or file download.
 */
export async function downloadOntology(
  apiClient: ApiClient,
  namespace: string,
  ontologyId: string,
): Promise<string> {
  // The endpoint no longer inlines the Turtle (a 6+ MB ontology would exceed the
  // API Gateway / Lambda response limit and 502). It returns a presigned S3 URL
  // we fetch directly, mirroring the proposal artifact path (#143).
  const { downloadUrl } = await apiClient.get<{ downloadUrl: string }>(
    `/namespaces/${encodeURIComponent(namespace)}/ontologies/${encodeURIComponent(
      ontologyId,
    )}/download`,
  );
  if (!downloadUrl) {
    throw new Error("Download response did not include a download URL.");
  }
  return fetchTurtleFromUrl(downloadUrl);
}

// ─── Validation (ontology-level, not proposal-level) ───────────────────

export interface TriggerValidationInput {
  ontology_id: string;
  tiers?: number[];
}

export async function triggerValidation(
  apiClient: ApiClient,
  namespace: string,
  input: TriggerValidationInput,
): Promise<ValidationJob> {
  return apiClient.post<ValidationJob>(
    `/namespaces/${encodeURIComponent(namespace)}/validate`,
    input,
  );
}

export async function getValidationJob(
  apiClient: ApiClient,
  namespace: string,
  jobId: string,
): Promise<ValidationJob> {
  return apiClient.get<ValidationJob>(
    `/namespaces/${encodeURIComponent(namespace)}/validate/jobs/${encodeURIComponent(jobId)}`,
  );
}

// ─── Inducted datasources ──────────────────────────────────────────────

export interface InducedDatasource {
  datasourceId: string;
  name?: string;
  proposal_id: string;
  ontology_id: string;
  label: string;
  source_type?: string;
  tables_processed: number;
  columns_processed: number;
  novel_classes_created: number;
}

/**
 * List every datasource behind a namespace's accepted proposals, following
 * ``nextToken`` across pages. Accepts both the paginated
 * ``{datasources: [...], nextToken}`` envelope and the bare array older
 * deployments returned.
 */
export async function listInducedDatasources(
  apiClient: ApiClient,
  namespace: string,
): Promise<InducedDatasource[]> {
  return collectAllPages("ListInducedDatasources", async (nextToken) => {
    const out = await apiClient.get<
      | InducedDatasource[]
      | { datasources?: InducedDatasource[]; nextToken?: string }
    >(
      `/namespaces/${encodeURIComponent(namespace)}/induce/datasources/induced${qs({ nextToken })}`,
    );
    if (Array.isArray(out)) return { items: out };
    return { items: out?.datasources ?? [], nextToken: out?.nextToken };
  });
}

// ─── Foundational ontologies ───────────────────────────────────────────

export interface FoundationalOntology {
  key: string;
  title: string;
  description: string;
  uri: string;
  source_url: string;
  format: string;
  domain_tags: string[];
  license?: string;
}

export async function listFoundationalOntologies(
  apiClient: ApiClient,
  namespace: string,
): Promise<{ items: FoundationalOntology[] }> {
  return apiClient.get(
    `/namespaces/${encodeURIComponent(namespace)}/ontology/foundational`,
  );
}

/**
 * Async ingest-job handle — shared by ingest-from-s3 and foundational-load
 * (both return 202 + this shape) and by the ingest-status poll endpoint.
 * camelCase on the wire (Smithy contract).
 */
export interface IngestJob {
  jobId: string;
  ontologyId: string;
  status: "pending" | "running" | "embeddings_sync" | "completed" | "failed";
  error?: string | null;
  result?: {
    ontologyId?: string;
    classCount?: number;
    embeddingCount?: number;
  } | null;
}

interface IngestJobResponse {
  result: IngestJob;
}

/**
 * Trigger an async load of a curated foundational ontology. Returns 202 +
 * an {@link IngestJob} keyed by the ontology URI (``result.ontologyId``);
 * poll {@link getOntologyIngestStatus} with that URI + ``result.jobId``
 * until the status is terminal. Async because fetching + embedding a large
 * ontology (FIBO, Schema.org) can exceed API Gateway's 29s timeout.
 */
export async function loadFoundationalOntology(
  apiClient: ApiClient,
  namespace: string,
  key: string,
): Promise<IngestJob> {
  const { result } = await apiClient.post<IngestJobResponse>(
    `/namespaces/${encodeURIComponent(namespace)}/ontology/foundational/${encodeURIComponent(key)}/load`,
    {},
  );
  return result;
}

/**
 * Poll an async ontology-ingest job (foundational-load or ingest-from-s3).
 * Keyed by ontology URI + job id — the same endpoint both writers share.
 */
export async function getOntologyIngestStatus(
  apiClient: ApiClient,
  namespace: string,
  ontologyId: string,
  jobId: string,
): Promise<IngestJob> {
  const { result } = await apiClient.get<IngestJobResponse>(
    `/namespaces/${encodeURIComponent(namespace)}/ontologies/${encodeURIComponent(ontologyId)}/ingest-status/${encodeURIComponent(jobId)}`,
  );
  return result;
}

/**
 * Upload a user-supplied OWL/RDF Turtle file as a custom ontology.
 * Calls the existing ``POST /ontologies/{id}/upload`` endpoint after
 * creating the ontology record. Pass the raw Turtle text as ``content``.
 */
export async function uploadOntologyFile(
  apiClient: ApiClient,
  namespace: string,
  ontologyId: string,
  title: string,
  file: File,
  format: "turtle" | "rdf+xml" | "json-ld" = "turtle",
): Promise<unknown> {
  const contentTypeMap: Record<string, string> = {
    turtle: "text/turtle",
    "rdf+xml": "application/rdf+xml",
    "json-ld": "application/ld+json",
  };
  const ext =
    format === "rdf+xml" ? ".rdf" : format === "json-ld" ? ".jsonld" : ".ttl";

  // Step 1: Get pre-signed S3 PUT URL
  const { uploadUrl, s3Key } = await apiClient.post<{
    uploadUrl: string;
    s3Key: string;
  }>(`/namespaces/${encodeURIComponent(namespace)}/ontologies/upload-url`, {
    filename: `${ontologyId.replace(/[^A-Za-z0-9._-]/g, "_")}${ext}`,
    content_type: contentTypeMap[format] ?? "text/turtle",
  });

  // Step 2: PUT file directly to S3 (raw File object, same as ConnectSource)
  let resp: Response;
  try {
    resp = await fetch(uploadUrl, {
      method: "PUT",
      body: file,
      headers: { "Content-Type": contentTypeMap[format] ?? "text/turtle" },
    });
  } catch (e) {
    throw new Error(
      `Network error uploading to S3: ${e instanceof Error ? e.message : "connection failed"}`,
      { cause: e },
    );
  }
  if (!resp.ok) {
    throw new Error(
      `Failed to upload file to S3: ${resp.status} ${resp.statusText}`,
    );
  }

  // Step 3: Trigger ingestion
  return apiClient.post(
    `/namespaces/${encodeURIComponent(namespace)}/ontologies/${encodeURIComponent(ontologyId)}/ingest-from-s3`,
    { s3_key: s3Key, title, format, ontology_type: "user_uploaded" },
  );
}

// ─── Ontology versions ─────────────────────────────────────────────────

export type OntologyVersionStatus =
  | "PUBLISHING"
  | "PUBLISHED"
  | "PUBLISH_FAILED";
export type OntologyDeploymentStatus =
  | "DEPLOYING"
  | "DEPLOYED"
  | "DEPLOY_FAILED";
export type SchemaKind = "classes" | "objectProperties" | "datatypeProperties";

export interface DiffCounts {
  added: number;
  removed: number;
  changed: number;
}

export interface OntologyVersion {
  version: number;
  status: OntologyVersionStatus;
  parentVersion?: number | null;
  ontologyId: string;
  source: "PROPOSAL" | "BOOTSTRAP";
  sourceProposalId?: string | null;
  createdBy?: string | null;
  createdAt?: string | null;
  diffSummary?: Partial<Record<SchemaKind, DiffCounts>> | null;
  error?: string | null;
}

export interface OntologyDeployment {
  deploymentId: string;
  version: number;
  status: OntologyDeploymentStatus;
  partial: boolean;
  /** Finished steps only; a step still running has no entry yet. */
  progress: Partial<Record<DeployStep, "done" | "failed">>;
  requestedBy?: string | null;
  error?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface BreakingMetric {
  metricId: string;
  name: string;
  missingClassIri: string;
}

export interface AutoDeployHeld {
  reason: "BREAKING_METRICS" | "DEPLOY_IN_PROGRESS" | "DEPLOY_FAILED_TO_START";
  metrics?: BreakingMetric[];
  error?: string;
}

export interface OntologyVersionList {
  versions: OntologyVersion[];
  /** Set on a single page when more versions follow; absent once all pages are collected. */
  nextToken?: string;
  activeVersion?: number | null;
  latestDeployment?: OntologyDeployment | null;
  /**
   * The latest deployment that may have changed what is served: attempts that failed
   * before their swap left serving untouched and are skipped. `partial` here means
   * serving is still half-swapped, whatever the latest attempt did.
   */
  servingDeployment?: OntologyDeployment | null;
  /** Versions at or below this belong to a deleted ontology and cannot be deployed. */
  resetAfterVersion?: number | null;
}

export interface OntologyVersionDetail {
  version: OntologyVersion;
  /** Snapshot manifest (snapshot.build_manifest); `{}` unless the version is PUBLISHED. */
  manifest: {
    formatVersion?: number;
    tripleCount?: number;
    files?: Record<string, { sha256: string; bytes: number }>;
  } & Record<string, unknown>;
  diff: Partial<
    Record<
      SchemaKind,
      { added: string[]; removed: string[]; changed: string[] }
    >
  >;
  downloadUrls: Record<string, string>;
  breakingMetrics: BreakingMetric[];
}

function ontologyPath(namespace: string): string {
  return `/namespaces/${encodeURIComponent(namespace)}/ontology`;
}

/** One page of ListOntologyVersions; the namespace-wide fields are on every page. */
export async function listOntologyVersionsPage(
  apiClient: ApiClient,
  namespace: string,
  opts?: { nextToken?: string; maxResults?: number },
): Promise<OntologyVersionList> {
  const page = await apiClient.get<Partial<OntologyVersionList> | undefined>(
    `${ontologyPath(namespace)}/versions${qs(opts ?? {})}`,
  );
  return { ...page, versions: page?.versions ?? [] };
}

/**
 * Every version of the namespace, newest first, by following ``nextToken`` to
 * the end. The namespace-wide fields come from the last page read, the most
 * recent answer.
 */
export async function listOntologyVersions(
  apiClient: ApiClient,
  namespace: string,
): Promise<OntologyVersionList> {
  let last: OntologyVersionList = { versions: [] };
  const versions = await collectAllPages(
    "ListOntologyVersions",
    async (nextToken) => {
      last = await listOntologyVersionsPage(apiClient, namespace, {
        nextToken,
      });
      return { items: last.versions, nextToken: last.nextToken };
    },
  );
  return {
    activeVersion: last.activeVersion,
    latestDeployment: last.latestDeployment,
    servingDeployment: last.servingDeployment,
    resetAfterVersion: last.resetAfterVersion,
    versions,
  };
}

export async function getOntologyVersion(
  apiClient: ApiClient,
  namespace: string,
  version: number,
): Promise<OntologyVersionDetail> {
  return apiClient.get<OntologyVersionDetail>(
    `${ontologyPath(namespace)}/versions/${version}`,
  );
}

export async function deployOntologyVersion(
  apiClient: ApiClient,
  namespace: string,
  version: number,
  acknowledgedBreakingMetrics: string[] = [],
): Promise<{ deploymentId: string }> {
  return apiClient.post<{ deploymentId: string }>(
    `${ontologyPath(namespace)}/versions/${version}/deploy`,
    { acknowledgedBreakingMetrics },
  );
}

export async function getOntologyDeployment(
  apiClient: ApiClient,
  namespace: string,
  deploymentId: string,
): Promise<OntologyDeployment> {
  return apiClient.get<OntologyDeployment>(
    `${ontologyPath(namespace)}/deployments/${encodeURIComponent(deploymentId)}`,
  );
}

// ─── Domain ABox ───────────────────────────────────────────────────────

export interface DomainAboxCounts {
  instances: number;
  relations: number;
  attributes: number;
  evidence: number;
}

export interface DomainAbox {
  status: DomainAboxStatus;
  /** Whether the ABox was built for the active ontology version. */
  current: boolean;
  conformsToVersion?: number | null;
  activeVersion?: number | null;
  buildId?: string | null;
  builtAt?: string | null;
  counts: DomainAboxCounts;
  classCounts: { classIri: string; label: string; count: number }[];
  mappingRevision?: string | null;
  /** A rebuild is queued. */
  pending: boolean;
  lastError?: string | null;
}

export interface DomainInstanceSummary {
  iri: string;
  classIri: string;
  classLabel: string;
  label: string;
  altLabels: string[];
  evidenceCount: number;
}

export interface DomainInstanceList {
  instances: DomainInstanceSummary[];
  nextToken?: string | null;
}

/** The other end of an instance's link. */
export interface DomainLinked {
  iri: string;
  label: string;
  classIri: string;
}

export interface DomainInstance {
  iri: string;
  classes: { iri: string; label: string }[];
  label: string;
  altLabels: string[];
  /** `free` marks a value kept under its lexical predicate, not a TBox property. `evidence` holds chunk ids. */
  attributes: {
    property: string;
    label: string;
    value: string;
    free: boolean;
    evidence: string[];
  }[];
  outgoing: {
    property: string;
    label: string;
    target: DomainLinked;
    evidence: string[];
  }[];
  incoming: { property: string; label: string; source: DomainLinked }[];
  evidence: {
    chunkId: string;
    docSourceId: string;
    sourceKey: string;
    text: string;
  }[];
  /** Which lists the engine cut at its cap. Optional: older engines do not send it. */
  truncated?: { incoming: boolean; evidence: boolean };
}

export interface AboxMapping {
  id: string;
  kind: AboxMappingKind;
  /** class: [lexical class]; relation: [lexical relation, subject class IRI, object class IRI]; attribute: [subject class IRI, lexical predicate]. */
  key: string[];
  /** A TBox term IRI, or `ignore` (class and relation rows) / `free` (attribute rows). */
  target: string;
  targetLabel: string;
  direction: AboxMappingDirection;
  origin: "auto" | "manual";
  status: AboxMappingStatus;
  rationale: string;
  decidedForVersion?: number | null;
  updatedBy: string;
  updatedAt: string;
}

function aboxPath(namespace: string): string {
  return `/namespaces/${encodeURIComponent(namespace)}/abox`;
}

export async function getDomainAbox(
  apiClient: ApiClient,
  namespace: string,
): Promise<DomainAbox> {
  return apiClient.get<DomainAbox>(aboxPath(namespace));
}

export async function listDomainInstances(
  apiClient: ApiClient,
  namespace: string,
  opts: {
    classIri?: string;
    q?: string;
    maxResults?: number;
    nextToken?: string;
  } = {},
): Promise<DomainInstanceList> {
  return apiClient.get<DomainInstanceList>(
    `${aboxPath(namespace)}/instances${qs(opts)}`,
  );
}

/** The IRI goes in the query string: a `#` in a path segment is refused by API Gateway. */
export async function getDomainInstance(
  apiClient: ApiClient,
  namespace: string,
  iri: string,
): Promise<DomainInstance> {
  return apiClient.get<DomainInstance>(
    `${aboxPath(namespace)}/instance${qs({ iri })}`,
  );
}

/** Page size for {@link listAboxMappings}: the contract's maximum, so a table needs few calls. */
const ABOX_MAPPINGS_PAGE_SIZE = 200;

/** List every mapping row of the filter by following ``nextToken`` to the end. */
export async function listAboxMappings(
  apiClient: ApiClient,
  namespace: string,
  opts: { kind?: AboxMappingKind; status?: AboxMappingStatus } = {},
): Promise<{ mappings: AboxMapping[] }> {
  const mappings = await collectAllPages(
    "ListAboxMappings",
    async (nextToken) => {
      const page = await apiClient.get<{
        mappings: AboxMapping[];
        nextToken?: string;
      }>(
        `${aboxPath(namespace)}/mappings${qs({
          ...opts,
          maxResults: ABOX_MAPPINGS_PAGE_SIZE,
          nextToken,
        })}`,
      );
      return { items: page.mappings, nextToken: page.nextToken };
    },
  );
  return { mappings };
}

/** Pin a mapping row to a human decision; the engine queues a rebuild. */
export async function updateAboxMapping(
  apiClient: ApiClient,
  namespace: string,
  mappingId: string,
  body: { target: string; direction?: AboxMappingDirection },
): Promise<AboxMapping> {
  return apiClient.put<AboxMapping>(
    `${aboxPath(namespace)}/mappings/${encodeURIComponent(mappingId)}`,
    body,
  );
}

/** Hand a manual mapping row back to the matcher; the engine queues a rebuild. */
export async function resetAboxMapping(
  apiClient: ApiClient,
  namespace: string,
  mappingId: string,
): Promise<AboxMapping> {
  return apiClient.del<AboxMapping>(
    `${aboxPath(namespace)}/mappings/${encodeURIComponent(mappingId)}`,
  );
}

export async function rebuildDomainAbox(
  apiClient: ApiClient,
  namespace: string,
): Promise<{ requested: boolean }> {
  return apiClient.post<{ requested: boolean }>(
    `${aboxPath(namespace)}/rebuild`,
    {},
  );
}
