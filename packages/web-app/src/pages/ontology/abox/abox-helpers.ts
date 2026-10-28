// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Pure display helpers for the Domain data tab, exported separately so they
 * are unit-tested without rendering (same approach as versions-helpers).
 */
import type { StatusIndicatorProps } from "@cloudscape-design/components/status-indicator";
import { formatAppMessage } from "../../../i18n/appMessages";
import type { AppTranslate } from "../../../i18n/LocaleProvider";
import type {
  AboxMapping,
  DomainAbox,
} from "../../../services/ontology-engine";

/** English rendering when no translator is passed (tests and non-React callers). */
const englishTranslate: AppTranslate = (message, values) =>
  formatAppMessage("en", message, values);

/** A queued rebuild shows over a settled status, never over a running build. */
export function aboxStatusDisplay(
  abox: Pick<DomainAbox, "status" | "pending">,
  t: AppTranslate = englishTranslate,
): {
  type: StatusIndicatorProps["type"];
  label: string;
} {
  if (abox.status === "BUILDING")
    return { type: "in-progress", label: t("Building") };
  if (abox.pending) return { type: "pending", label: t("Rebuild queued") };
  if (abox.status === "READY") return { type: "success", label: t("Ready") };
  if (abox.status === "FAILED")
    return { type: "error", label: t("Build failed") };
  return { type: "stopped", label: t("Not built") };
}

/** "Built for v2, active v3", or what is known before the first build. */
export function versionLine(
  abox: DomainAbox,
  t: AppTranslate = englishTranslate,
): string {
  if (abox.conformsToVersion == null)
    return abox.activeVersion == null
      ? t("No ontology version deployed")
      : t("Active v{version}", { version: abox.activeVersion });
  return abox.activeVersion == null
    ? t("Built for v{built}, no active version", {
        built: abox.conformsToVersion,
      })
    : t("Built for v{built}, active v{active}", {
        built: abox.conformsToVersion,
        active: abox.activeVersion,
      });
}

export function isPolling(abox: DomainAbox | undefined): boolean {
  return !!abox && (abox.status === "BUILDING" || abox.pending);
}

/** The lexical side of a mapping row; `label` names the class IRIs in relation and attribute keys. */
export function mappingKeyLabel(
  m: Pick<AboxMapping, "kind" | "key">,
  label: (iri: string) => string = (i) => i,
): string {
  if (m.kind === "class") return m.key[0] ?? "";
  if (m.kind === "relation")
    return `${label(m.key[1] ?? "")} -${m.key[0] ?? ""}-> ${label(m.key[2] ?? "")}`;
  return `${label(m.key[0] ?? "")}: ${m.key[1] ?? ""}`;
}

export function mappingTargetLabel(
  m: Pick<AboxMapping, "target" | "targetLabel" | "direction" | "kind">,
  t: AppTranslate = englishTranslate,
): string {
  if (m.target === "ignore") return t("Ignored");
  if (m.target === "free") return t("Free attribute");
  return m.kind === "relation" && m.direction === "inverse"
    ? t("{label} (inverse)", { label: m.targetLabel })
    : m.targetLabel;
}

/** searchEntities kind for picking a mapping target. */
export function targetSearchKind(
  kind: AboxMapping["kind"],
): "class" | "object-property" | "datatype-property" {
  return kind === "class"
    ? "class"
    : kind === "relation"
      ? "object-property"
      : "datatype-property";
}

/** The file name at the end of an evidence chunk's source key. */
export function sourceFileName(sourceKey: string): string {
  return sourceKey.split("/").pop() ?? "";
}

/** Built for a version that is no longer the active one (never true before the first build). */
export function isOutOfDate(
  abox: Pick<DomainAbox, "conformsToVersion" | "current">,
): boolean {
  return abox.conformsToVersion != null && !abox.current;
}

/**
 * Display name of a term with no known label: its last path or fragment
 * segment, percent-decoded, as the engine labels one with
 * `unquote(iri_tail(iri))` (`iri_tail` itself does not decode).
 */
export function iriTail(iri: string): string {
  const tail =
    iri
      .replace(/[/#]+$/, "")
      .split(/[#/]/)
      .pop() ?? "";
  try {
    return decodeURIComponent(tail) || iri;
  } catch {
    return tail || iri;
  }
}
