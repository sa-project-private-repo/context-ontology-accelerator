// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Pure helpers for the Ontology designer. Helpers that build copy take the
 * caller's `t` and default to English (as the Versions tab helpers do).
 */
import { ApiError } from "@components/ApiClientProvider";
import type {
  GraphEdgeData,
  GraphNodeBadge,
  GraphNodeData,
} from "@components/graph/OntologyGraphFlow";
import {
  studioErrorDetail,
  type StudioAboxState,
  type StudioDiff,
  type StudioDraft,
  type StudioGraph,
  type StudioGraphNode,
  type StudioPreviewGraph,
} from "../../../services/studio";
import { formatAppMessage } from "../../../i18n/appMessages";
import type { AppTranslate } from "../../../i18n/LocaleProvider";
import { splitSqlIdentPath } from "../induce/grounding";

const englishTranslate: AppTranslate = (message, values) =>
  formatAppMessage("en", message, values);

/** Shown when a write lost to another change (409 DraftConflict). */
export function conflictMessage(t: AppTranslate = englishTranslate): string {
  return t(
    "Another change was applied first. The draft was reloaded; check it and try again.",
  );
}

/**
 * The largest request body the designer sends, in UTF-8 bytes. The API's WAF
 * (AWSManagedRulesCommonRuleSet, SizeRestrictions_BODY) blocks a body over
 * 8 KB with a bare 403 before the engine sees it. The engine's limits count
 * characters, and Japanese or Korean text takes three bytes a character, so a
 * message or description well inside them can still be blocked.
 */
export const MAX_REQUEST_BYTES = 8000;

/** The size of `body` as the API client sends it: JSON, in UTF-8 bytes. */
export function requestBytes(body: unknown): number {
  return new TextEncoder().encode(JSON.stringify(body)).length;
}

/**
 * What to tell the user about a failed request: the engine's own message (a
 * designer `detail.message`, or the plain-string `detail` of the accept errors
 * that predate the designer), else a general one. A FastAPI list-form 422 or a body
 * that is not JSON leaves `ApiError.message` as raw request text, never shown.
 */
export function errorMessage(
  e: unknown,
  t: AppTranslate = englishTranslate,
): string {
  if (!(e instanceof ApiError)) {
    return e instanceof Error ? e.message : String(e);
  }
  const message = studioErrorDetail(e)?.message;
  if (typeof message === "string" && message) return message;
  const body =
    typeof e.body === "object" && e.body !== null
      ? (e.body as { detail?: unknown; message?: unknown })
      : {};
  if (typeof body.detail === "string" && body.detail) return body.detail;
  // API Gateway's answer for a request its WAF blocked: no word on why.
  if (e.status === 403 && body.message === "Forbidden")
    return t(
      "The request was blocked before it reached the server, most likely because it is larger than 8 KB (Japanese, Korean and other non-Latin characters take up to 3 bytes each). Shorten the text and try again.",
    );
  // The authorizer's refusal (a role that may read the draft but not change
  // it): its text names IAM policies, so say it as the API client does.
  if (e.status === 403)
    return t("You are not authorized to perform this action.");
  if (typeof body.message === "string" && body.message) return body.message;
  return t(
    "The request failed (HTTP {status}). Reload the page and try again.",
    { status: e.status },
  );
}

/** The XSD datatypes offered for a new or changed attribute range. */
export const DATATYPES = [
  "string",
  "integer",
  "decimal",
  "boolean",
  "date",
  "dateTime",
  "double",
  "long",
].map((local) => ({
  value: `http://www.w3.org/2001/XMLSchema#${local}`,
  label: `xsd:${local}`,
}));

/** The last path or fragment segment of an IRI, for display. */
export function iriTail(iri: string): string {
  const parts = iri.replace(/[#/]+$/, "").split(/[#/]/);
  const tail = parts[parts.length - 1] || iri;
  try {
    return decodeURIComponent(tail);
  } catch {
    // A stray "%" is not valid percent-encoding; show the tail as it is.
    return tail;
  }
}

/**
 * A mapping's table or column as the engine names it in its messages: the last
 * segment of the ``rr:tableName`` or ``rr:column`` value, without SQL quotes.
 */
export function sqlName(name: string): string {
  const segments = splitSqlIdentPath(name);
  return segments[segments.length - 1];
}

/**
 * The ABox pill of a class: its instance count, or "new" for a class only the
 * draft has. A count is dimmed when the ABox was built for another version;
 * "new" is not a count and says nothing about the ABox, so it never is.
 */
export function badgeFor(
  node: StudioGraphNode,
  abox: StudioAboxState | null,
  t: AppTranslate = englishTranslate,
): GraphNodeBadge | undefined {
  if (!abox) return undefined;
  if (node.afterDeploy) {
    return {
      text: t("new"),
      title: t("Filled after deploy: the active version has no such class yet"),
    };
  }
  if (node.aboxCount === null) return undefined;
  const base = t(
    node.aboxCount === 1
      ? "{count} instance in the active version's domain data"
      : "{count} instances in the active version's domain data",
    { count: node.aboxCount },
  );
  const stale = abox.current === false;
  return {
    text: String(node.aboxCount),
    title: stale
      ? t("{base} ({reason})", {
          base,
          reason: abox.reason ?? t("out of date"),
        })
      : base,
    dimmed: stale,
  };
}

/** The nodes of the draft graph, or of a change set preview with its marks. */
export function toFlowNodes(
  graph: StudioGraph | StudioPreviewGraph,
  t: AppTranslate = englishTranslate,
): GraphNodeData[] {
  return graph.nodes.map((n) => ({
    id: n.iri,
    uri: n.iri,
    label: n.label,
    kind: n.grounded ? "grounded" : "class",
    attributes: n.attributes.map((a) => ({
      label: a.label,
      range: a.range ?? undefined,
    })),
    badge: badgeFor(n, graph.abox, t),
    highlight: ("preview" in n && n.preview) || undefined,
  }));
}

export function toFlowEdges(
  graph: StudioGraph | StudioPreviewGraph,
): GraphEdgeData[] {
  return graph.edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    kind: e.kind,
    label: e.kind === "objectProperty" ? e.label : undefined,
    highlight: ("preview" in e && e.preview) || undefined,
  }));
}

/** "Revision 7, built on v3" for the top bar. */
export function revisionLine(
  draft: StudioDraft,
  t: AppTranslate = englishTranslate,
): string {
  return draft.baseVersion === null
    ? t("Revision {revision}, built on no version", {
        revision: draft.revision,
      })
    : t("Revision {revision}, built on v{version}", {
        revision: draft.revision,
        version: draft.baseVersion,
      });
}

/** How many terms a diff removes and adds, over every kind. */
export function diffCounts(diff: StudioDiff): {
  added: number;
  removed: number;
  changed: number;
} {
  const kinds = Object.values(diff);
  return {
    added: kinds.reduce((n, k) => n + k.added.length, 0),
    removed: kinds.reduce((n, k) => n + k.removed.length, 0),
    changed: kinds.reduce((n, k) => n + k.changed.length, 0),
  };
}
