// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Pure display helpers for the ontology Versions tab, exported separately so
 * they are unit-tested without rendering (same approach as proposalStatusDisplay).
 * Helpers that build copy take the caller's `t` and default to English.
 */
import type { StatusIndicatorProps } from "@cloudscape-design/components/status-indicator";
import { formatAppMessage, type AppMessage } from "../../../i18n/appMessages";
import type { AppTranslate } from "../../../i18n/LocaleProvider";
import type {
  AutoDeployHeld,
  DiffCounts,
  OntologyDeployment,
  OntologyVersion,
  SchemaKind,
} from "../../../services/ontology-engine";
import {
  DEPLOY_STEPS,
  type DeployStep,
} from "../../../services/ontology-versions-types";

export { DEPLOY_STEPS, type DeployStep };

const englishTranslate: AppTranslate = (message, values) =>
  formatAppMessage("en", message, values);

/** Catalogue keys; render them through `t`. */
export const DEPLOY_STEP_LABELS: Record<DeployStep, AppMessage> = {
  stage: "Stage graph",
  swap: "Swap graph",
  embeddings: "Replace embeddings",
  registry: "Update registry",
  latest: "Publish VKG files",
  // The step sends the reload event; the VKG serves the version once its new task starts.
  notify: "Request VKG reload",
};

export function versionStatusDisplay(
  status: string,
  t: AppTranslate = englishTranslate,
): {
  type: StatusIndicatorProps["type"];
  label: string;
} {
  if (status === "PUBLISHED") return { type: "success", label: t("Published") };
  if (status === "PUBLISHING")
    return { type: "in-progress", label: t("Publishing…") };
  if (status === "PUBLISH_FAILED")
    return { type: "error", label: t("Publish failed") };
  return { type: "pending", label: status };
}

/**
 * `retired`: the version belongs to a deleted ontology (isRetiredVersion). Nothing
 * serves it any more and it cannot be deployed again, so its last deployment must
 * read neither as deployed nor as one to finish.
 */
export function deploymentStatusDisplay(
  deployment: Pick<OntologyDeployment, "status" | "partial">,
  t: AppTranslate = englishTranslate,
  retired = false,
): { type: StatusIndicatorProps["type"]; label: string } {
  if (retired)
    return { type: "stopped", label: t("Retired: its ontology was deleted") };
  if (deployment.status === "DEPLOYED")
    return { type: "success", label: t("Deployed") };
  if (deployment.status === "DEPLOYING")
    return { type: "in-progress", label: t("Deploying…") };
  if (deployment.status === "DEPLOY_FAILED") {
    return deployment.partial
      ? {
          type: "warning",
          label: t("Partially deployed: redeploy to finish"),
        }
      : { type: "error", label: t("Deploy failed") };
  }
  return { type: "pending", label: deployment.status };
}

export type DeployStepState = "done" | "failed" | "in-progress" | "pending";

/**
 * One step's state. The engine records only finished steps ("done" / "failed"),
 * so while a deployment is DEPLOYING the first step not yet done is the one
 * running.
 */
export function deployStepState(
  deployment: Pick<OntologyDeployment, "status" | "progress">,
  step: DeployStep,
): DeployStepState {
  const recorded = deployment.progress?.[step];
  if (recorded) return recorded;
  if (
    deployment.status === "DEPLOYING" &&
    DEPLOY_STEPS.find((s) => deployment.progress?.[s] !== "done") === step
  )
    return "in-progress";
  return "pending";
}

/** What deploying a version does to the namespace; also a catalogue key. */
export type DeployAction = "Deploy" | "Redeploy" | "Roll back";

export function deployActionLabel(
  version: number,
  activeVersion: number | null | undefined,
  servingDeployment?: Pick<OntologyDeployment, "version" | "partial"> | null,
): DeployAction {
  // A partial failure left this version half-served: deploying it again finishes the job.
  if (servingDeployment?.partial && servingDeployment.version === version)
    return "Redeploy";
  if (activeVersion == null) return "Deploy";
  if (version === activeVersion) return "Redeploy";
  return version < activeVersion ? "Roll back" : "Deploy";
}

/**
 * The version the next publish builds on: the newest published one above the
 * reset floor (the engine's `registry.head_version`), or null.
 */
export function publishedHead(
  versions: readonly Pick<OntologyVersion, "version" | "status">[],
  resetAfterVersion: number | null | undefined,
): number | null {
  const heads = versions
    .filter(
      (v) =>
        v.status === "PUBLISHED" &&
        !isRetiredVersion(v.version, resetAfterVersion),
    )
    .map((v) => v.version);
  return heads.length > 0 ? Math.max(...heads) : null;
}

/** A version at or below the reset floor belongs to a deleted ontology and cannot be deployed. */
export function isRetiredVersion(
  version: number,
  resetAfterVersion: number | null | undefined,
): boolean {
  return resetAfterVersion != null && version <= resetAfterVersion;
}

export function diffSummaryLabel(
  summary: Partial<Record<SchemaKind, DiffCounts>> | null | undefined,
  t: AppTranslate = englishTranslate,
): string {
  const classes = summary?.classes;
  if (!classes) return "—";
  const parts = [`+${classes.added}`, `−${classes.removed}`];
  if (classes.changed) parts.push(`~${classes.changed}`);
  const kinds: SchemaKind[] = ["objectProperties", "datatypeProperties"];
  const properties = kinds.reduce((n, kind) => {
    const c = summary?.[kind];
    return c ? n + c.added + c.removed + c.changed : n;
  }, 0);
  const changes = parts.join(" / ");
  return properties
    ? t("Classes {changes}, properties ~{properties}", { changes, properties })
    : t("Classes {changes}", { changes });
}

// The engine's refusal of a version at or below the reset floor (versions/deploy.py
// start_deployment); such a version stays undeployable, so no retry is offered.
const RETIRED_VERSION_ERROR = "belongs to a deleted ontology";

/** `text` without the trailing periods it may carry, so a template can add exactly one. */
function withoutFinalPeriod(text: string): string {
  return text.trim().replace(/\.+$/, "");
}

/**
 * Why an accept's deployment was held, and what to do in the Versions tab.
 * The copy names that tab's place in the navigation: neither page that shows
 * the message, the designer page or the proposal page, has the tab itself.
 */
export function autoDeployHeldMessage(
  held: AutoDeployHeld,
  t: AppTranslate = englishTranslate,
): string {
  if (held.reason === "BREAKING_METRICS") {
    // One metric can lose several classes, so it appears once per missing class.
    const names = [...new Set((held.metrics ?? []).map((m) => m.name))].join(
      ", ",
    );
    return t(
      "Deploying would break governed metrics: {names}. Review the impact and deploy from Ontology › Induction › Versions.",
      { names: withoutFinalPeriod(names) },
    );
  }
  if (held.reason === "DEPLOY_IN_PROGRESS") {
    return t(
      "Another deployment was running. Deploy this version from Ontology › Induction › Versions when it finishes.",
    );
  }
  const reason = withoutFinalPeriod(held.error ?? t("unknown error"));
  return held.error?.includes(RETIRED_VERSION_ERROR)
    ? t("The deployment could not start: {reason}.", { reason })
    : t(
        "The deployment could not start: {reason}. Retry from Ontology › Induction › Versions.",
        { reason },
      );
}
