// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Ontology Versions tab: published versions, which one is served, and
 * deploy / redeploy / roll back with a governed-metric impact check.
 */
import React, { useEffect, useRef, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Checkbox from "@cloudscape-design/components/checkbox";
import Container from "@cloudscape-design/components/container";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Header from "@cloudscape-design/components/header";
import Link from "@cloudscape-design/components/link";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator, {
  type StatusIndicatorProps,
} from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import { useCanManageOntology } from "@api-hooks/use-can-manage-ontology";
import { formatTimestamp } from "@utils/helpers";
import { ApiError, type ApiClient } from "@components/ApiClientProvider";
import type { AppMessage } from "../../../i18n/appMessages";
import { useAppI18n, type AppTranslate } from "../../../i18n/LocaleProvider";
import {
  deployOntologyVersion,
  getOntologyVersion,
  listOntologyVersions,
  type BreakingMetric,
  type OntologyVersion,
  type OntologyVersionDetail,
  type OntologyVersionList,
  type SchemaKind,
} from "../../../services/ontology-engine";
import {
  DEPLOY_STEP_LABELS,
  DEPLOY_STEPS,
  deployActionLabel,
  isRetiredVersion,
  deploymentStatusDisplay,
  deployStepState,
  diffSummaryLabel,
  publishedHead,
  versionStatusDisplay,
  type DeployAction,
  type DeployStepState,
} from "./versions-helpers";

const POLL_INTERVAL_MS = 3000;
/**
 * Polls to keep making after the last PUBLISHING version is published: an
 * accept's auto-deploy writes its DEPLOYING row a few seconds later (it first
 * parses the snapshot and runs the impact check), and that must not end polling.
 */
const TRAILING_POLLS = 4;
/**
 * Failed list fetches in a row after which the last answer (a running deployment,
 * a publishing version) stops driving the poll; the next success resumes it.
 */
const MAX_FAILED_POLLS = 4;
const SCHEMA_KINDS: { kind: SchemaKind; label: AppMessage }[] = [
  { kind: "classes", label: "Classes" },
  { kind: "objectProperties", label: "Object properties" },
  { kind: "datatypeProperties", label: "Datatype properties" },
];
const STEP_INDICATOR: Record<
  DeployStepState,
  { type: StatusIndicatorProps["type"]; ariaLabel: AppMessage }
> = {
  done: { type: "success", ariaLabel: "Done" },
  failed: { type: "error", ariaLabel: "Failed" },
  "in-progress": { type: "in-progress", ariaLabel: "In progress" },
  pending: { type: "pending", ariaLabel: "Not started" },
};

interface DeployDialog {
  /** Distinguishes dialogs for the same version; late answers for an older one are dropped. */
  request: number;
  version: number;
  /** deployActionLabel at the time the dialog opened, e.g. "Roll back". */
  action: DeployAction;
  detail: OntologyVersionDetail | null;
  acknowledged: boolean;
  busy: boolean;
  error: { header: string; message: string } | null;
}

/** "Deploy v2" / "Redeploy v2" / "Roll back to v1". */
function actionPhrase(
  t: AppTranslate,
  action: DeployAction,
  version: number,
): string {
  if (action === "Roll back") return t("Roll back to v{version}", { version });
  if (action === "Redeploy") return t("Redeploy v{version}", { version });
  return t("Deploy v{version}", { version });
}

/** The same phrase spelled out for screen readers: "Roll back to version 1". */
function actionAriaLabel(
  t: AppTranslate,
  action: DeployAction,
  version: number,
): string {
  if (action === "Roll back")
    return t("Roll back to version {version}", { version });
  if (action === "Redeploy")
    return t("Redeploy version {version}", { version });
  return t("Deploy version {version}", { version });
}

function objectField(value: unknown, key: string): unknown {
  return value && typeof value === "object" && key in value
    ? Reflect.get(value, key)
    : undefined;
}

function isBreakingMetric(value: unknown): value is BreakingMetric {
  return (
    typeof objectField(value, "metricId") === "string" &&
    typeof objectField(value, "name") === "string" &&
    typeof objectField(value, "missingClassIri") === "string"
  );
}

/** Metrics a deploy was refused for (409) because they broke after the impact check ran. */
function breakingMetricsFromConflict(e: unknown): BreakingMetric[] | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null;
  const metrics = objectField(objectField(e.body, "detail"), "breakingMetrics");
  return Array.isArray(metrics) &&
    metrics.length > 0 &&
    metrics.every(isBreakingMetric)
    ? metrics
    : null;
}

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function isPublishing(list: OntologyVersionList): boolean {
  return list.versions.some((v) => v.status === "PUBLISHING");
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

interface VersionsTabProps {
  apiClient: ApiClient;
  namespace?: string;
}

/**
 * Keyed by namespace, so switching namespaces starts from scratch: no rows,
 * details or deploy dialog of the previous namespace survive (nothing of it can
 * be deployed against the new one), and its in-flight answers are dropped.
 */
export function VersionsTab(props: VersionsTabProps) {
  return <NamespaceVersions key={props.namespace ?? ""} {...props} />;
}

function NamespaceVersions({ apiClient, namespace }: VersionsTabProps) {
  const { t, locale } = useAppI18n();
  // A role that only reads (a data analyst) is not offered a deploy the API would refuse it.
  const canDeploy = useCanManageOntology(namespace);
  const [data, setData] = useState<OntologyVersionList | null>(null);
  // Only loads the user waits on (the first one, Refresh) set this; background
  // polls leave the Refresh button alone.
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reload, setReload] = useState({ key: 0, background: false });
  // Bumped each time a list fetch settles; the next poll is scheduled from it.
  const [settled, setSettled] = useState(0);
  const [trailingPolls, setTrailingPolls] = useState(0);
  const [failedPolls, setFailedPolls] = useState(0);
  const [selected, setSelected] = useState<OntologyVersionDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const detailRequest = useRef(0);
  const [dialog, setDialog] = useState<DeployDialog | null>(null);
  const dialogRequest = useRef(0);

  useEffect(() => {
    if (!namespace) return;
    let cancelled = false;
    if (!reload.background) setLoading(true);
    const load = async () => {
      try {
        const list = await listOntologyVersions(apiClient, namespace);
        if (cancelled) return;
        setData(list);
        setLoadError(null);
        setFailedPolls(0);
        setTrailingPolls((n) =>
          isPublishing(list) ? TRAILING_POLLS : Math.max(0, n - 1),
        );
      } catch (e) {
        if (cancelled) return;
        setLoadError(errorMessage(e));
        setFailedPolls((n) => n + 1);
        setTrailingPolls((n) => Math.max(0, n - 1));
      }
      if (cancelled) return;
      setLoading(false);
      setSettled((n) => n + 1);
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [apiClient, namespace, reload]);

  const running = data?.latestDeployment?.status === "DEPLOYING";
  // Poll while a deployment runs (live step progress), while a version is still
  // publishing and for a few polls after (an accept's auto-deploy starts once it
  // is published). Each poll is scheduled only after the previous fetch settled,
  // so a backend slower than the interval still gets its answer applied. Failed
  // fetches keep the last answer, so they bound how long it drives the poll.
  const polling =
    (failedPolls < MAX_FAILED_POLLS &&
      (running || (!!data && isPublishing(data)))) ||
    trailingPolls > 0;
  useEffect(() => {
    if (!namespace || !polling) return;
    const id = window.setTimeout(
      () => setReload((r) => ({ key: r.key + 1, background: true })),
      POLL_INTERVAL_MS,
    );
    return () => window.clearTimeout(id);
  }, [namespace, polling, settled]);

  async function showDetail(version: number) {
    if (!namespace) return;
    const request = ++detailRequest.current;
    try {
      const detail = await getOntologyVersion(apiClient, namespace, version);
      // A later click (or Close) superseded this request.
      if (request !== detailRequest.current) return;
      setSelected(detail);
      setDetailError(null);
    } catch (e) {
      if (request !== detailRequest.current) return;
      setSelected(null);
      setDetailError(errorMessage(e));
    }
  }

  function closeDetail() {
    detailRequest.current++;
    setSelected(null);
  }

  async function openDeploy(version: number, action: DeployAction) {
    if (!namespace) return;
    const request = ++dialogRequest.current;
    setDialog({
      request,
      version,
      action,
      detail: null,
      acknowledged: false,
      busy: false,
      error: null,
    });
    try {
      const detail = await getOntologyVersion(apiClient, namespace, version);
      // Ignore a late answer for a dialog the user already closed or replaced.
      setDialog((d) => (d?.request === request ? { ...d, detail } : d));
    } catch (e) {
      const error = {
        header: t("Could not check deployment impact"),
        message: errorMessage(e),
      };
      setDialog((d) => (d?.request === request ? { ...d, error } : d));
    }
  }

  async function confirmDeploy() {
    if (!namespace || !dialog?.detail) return;
    const { request, version } = dialog;
    const acknowledged = dialog.acknowledged
      ? [...new Set(dialog.detail.breakingMetrics.map((m) => m.metricId))]
      : [];
    // Results apply only to the dialog they were started from.
    const update = (next: (d: DeployDialog) => DeployDialog | null) =>
      setDialog((d) => (d?.request === request ? next(d) : d));
    update((d) => ({ ...d, busy: true, error: null }));
    try {
      await deployOntologyVersion(apiClient, namespace, version, acknowledged);
      update(() => null);
    } catch (e) {
      // Metrics can break between the impact check and the deploy (e.g. a
      // metric was linked meanwhile): show them and ask for a fresh acknowledgement.
      const raced = breakingMetricsFromConflict(e);
      update((d) =>
        raced && d.detail
          ? {
              ...d,
              detail: { ...d.detail, breakingMetrics: raced },
              acknowledged: false,
              busy: false,
            }
          : {
              ...d,
              busy: false,
              error: { header: t("Deploy failed"), message: errorMessage(e) },
            },
      );
    } finally {
      // Refresh either way: a refusal such as "a deployment is in progress"
      // (e.g. an accept's auto-deploy) should surface that run and start polling.
      setReload((r) => ({ key: r.key + 1, background: true }));
    }
  }

  const active = data?.activeVersion ?? null;
  const latest = data?.latestDeployment ?? null;
  // Not `latest`: an attempt that failed before its swap leaves an earlier partial one in place.
  const serving = data?.servingDeployment ?? null;
  const resetAfter = data?.resetAfterVersion ?? null;
  const latestRetired =
    !!latest && isRetiredVersion(latest.version, resetAfter);
  // After a rollback, or before a new version is deployed, the active version is older than the
  // one the next publish builds on. A running deployment is about to change that, so it waits.
  const head = data ? publishedHead(data.versions, resetAfter) : null;
  const buildsOnNewer =
    !running && active !== null && head !== null && active < head;
  const breaking = dialog?.detail?.breakingMetrics ?? [];
  const breakingMetricCount = new Set(breaking.map((m) => m.metricId)).size;
  const canConfirm =
    !!dialog?.detail &&
    !dialog.busy &&
    !running &&
    (breaking.length === 0 || dialog.acknowledged);
  // A running deployment can appear while the dialog is open, e.g. the reload
  // after a 409 surfaces an accept's auto-deploy.
  const confirmDisabledReason = !dialog?.detail
    ? dialog?.error
      ? t("The deployment impact could not be checked.")
      : t("Checking deployment impact…")
    : running
      ? t("A deployment is running")
      : breaking.length > 0 && !dialog.acknowledged
        ? t("Acknowledge the metrics this deploy breaks first.")
        : undefined;

  return (
    <SpaceBetween size="l">
      {loadError && (
        <Alert type="error" header={t("Could not load versions")}>
          {loadError}
        </Alert>
      )}

      {serving?.partial && !isRetiredVersion(serving.version, resetAfter) && (
        <Alert type="warning" header={t("Redeploy required")}>
          {t(
            "The deployment of v{version} failed after the served graph may already have been replaced, so the graph, embeddings and VKG files can be out of step. Deploy v{version} again to finish.",
            { version: serving.version },
          )}
        </Alert>
      )}

      {buildsOnNewer && (
        <Alert type="warning">
          {t(
            "The next version builds on v{head}, not the active v{active}; its changes carry over unless you revert them.",
            { head, active },
          )}
        </Alert>
      )}

      {latest && (
        <Container
          header={
            <Header variant="h3">
              {t("Latest deployment: v{version}", { version: latest.version })}
            </Header>
          }
        >
          <SpaceBetween size="s">
            <StatusIndicator
              type={deploymentStatusDisplay(latest, t, latestRetired).type}
            >
              {deploymentStatusDisplay(latest, t, latestRetired).label}
            </StatusIndicator>
            {latest.requestedBy && (
              <Box color="text-body-secondary">
                {t("Requested by {user}", { user: latest.requestedBy })}
              </Box>
            )}
            <SpaceBetween direction="horizontal" size="m">
              {DEPLOY_STEPS.map((step) => {
                const state = STEP_INDICATOR[deployStepState(latest, step)];
                return (
                  <StatusIndicator
                    key={step}
                    type={state.type}
                    iconAriaLabel={t(state.ariaLabel)}
                  >
                    {t(DEPLOY_STEP_LABELS[step])}
                  </StatusIndicator>
                );
              })}
            </SpaceBetween>
            {latest.error && <Box variant="code">{latest.error}</Box>}
          </SpaceBetween>
        </Container>
      )}

      <Table<OntologyVersion>
        loading={loading && !data}
        loadingText={t("Loading versions")}
        items={data?.versions ?? []}
        trackBy="version"
        empty={
          <Box textAlign="center">
            {t("No versions yet. Accepting a proposal publishes version 1.")}
          </Box>
        }
        header={
          <Header
            counter={`(${data?.versions.length ?? 0})`}
            description={
              canDeploy
                ? undefined
                : t(
                    "Your role on this namespace cannot deploy or roll back versions.",
                  )
            }
            actions={
              <Button
                iconName="refresh"
                loading={loading}
                onClick={() =>
                  setReload((r) => ({ key: r.key + 1, background: false }))
                }
              >
                {t("Refresh")}
              </Button>
            }
          >
            {t("Versions")}
          </Header>
        }
        columnDefinitions={[
          {
            id: "version",
            header: t("Version"),
            cell: (v) => (
              <SpaceBetween direction="horizontal" size="xs">
                <span>v{v.version}</span>
                {v.version === active && (
                  <Badge color="green">{t("Active")}</Badge>
                )}
                {isRetiredVersion(v.version, resetAfter) && (
                  <Badge color="grey">{t("Retired")}</Badge>
                )}
              </SpaceBetween>
            ),
          },
          {
            id: "status",
            header: t("Status"),
            cell: (v) => (
              <StatusIndicator type={versionStatusDisplay(v.status, t).type}>
                {versionStatusDisplay(v.status, t).label}
              </StatusIndicator>
            ),
          },
          {
            id: "source",
            header: t("Origin"),
            cell: (v) =>
              v.source === "BOOTSTRAP" ? t("Existing ontology") : t("Proposal"),
          },
          {
            id: "createdBy",
            header: t("Published by"),
            cell: (v) => v.createdBy ?? "—",
          },
          {
            id: "createdAt",
            header: t("Published"),
            cell: (v) => formatTimestamp(v.createdAt ?? undefined, locale),
          },
          {
            id: "changes",
            header: t("Changes"),
            cell: (v) => diffSummaryLabel(v.diffSummary, t),
          },
          {
            id: "actions",
            header: t("Actions"),
            cell: (v) => {
              const action = deployActionLabel(v.version, active, serving);
              return (
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    variant="inline-link"
                    ariaLabel={t("Details of version {version}", {
                      version: v.version,
                    })}
                    onClick={() => showDetail(v.version)}
                  >
                    {t("Details")}
                  </Button>
                  {/* A retired version belongs to a deleted ontology; the engine refuses to deploy it. */}
                  {!isRetiredVersion(v.version, resetAfter) && (
                    <Button
                      variant="inline-link"
                      ariaLabel={actionAriaLabel(t, action, v.version)}
                      // An inline link takes no disabledReason: for a role
                      // that cannot deploy, the table header says why.
                      disabled={
                        v.status !== "PUBLISHED" || running || !canDeploy
                      }
                      onClick={() => openDeploy(v.version, action)}
                    >
                      {t(action)}
                    </Button>
                  )}
                </SpaceBetween>
              );
            },
          },
        ]}
      />

      {detailError && (
        <Alert
          type="error"
          header={t("Could not load version details")}
          dismissible
          onDismiss={() => setDetailError(null)}
        >
          {detailError}
        </Alert>
      )}

      {selected && (
        <Container
          header={
            <Header
              variant="h3"
              actions={<Button onClick={closeDetail}>{t("Close")}</Button>}
            >
              {t("v{version} details", { version: selected.version.version })}
            </Header>
          }
        >
          <SpaceBetween size="m">
            {SCHEMA_KINDS.map(({ kind, label }) => {
              const entry = selected.diff?.[kind];
              if (!entry) return null;
              return (
                <ExpandableSection
                  key={kind}
                  headerText={t("{kind}: +{added} / −{removed} / ~{changed}", {
                    kind: t(label),
                    added: entry.added.length,
                    removed: entry.removed.length,
                    changed: entry.changed.length,
                  })}
                >
                  <SpaceBetween size="xxs">
                    {entry.added.map((iri) => (
                      <Box key={`a-${iri}`}>+ {iri}</Box>
                    ))}
                    {entry.removed.map((iri) => (
                      <Box key={`r-${iri}`}>− {iri}</Box>
                    ))}
                    {entry.changed.map((iri) => (
                      <Box key={`c-${iri}`}>~ {iri}</Box>
                    ))}
                  </SpaceBetween>
                </ExpandableSection>
              );
            })}
            <SpaceBetween direction="horizontal" size="s">
              {Object.entries(selected.downloadUrls).map(([name, url]) => {
                const bytes = selected.manifest.files?.[name]?.bytes;
                return (
                  <SpaceBetween key={name} direction="horizontal" size="xxs">
                    <Link href={url} external>
                      {name}
                    </Link>
                    {bytes !== undefined && (
                      <Box color="text-body-secondary">
                        ({formatBytes(bytes)})
                      </Box>
                    )}
                  </SpaceBetween>
                );
              })}
            </SpaceBetween>
          </SpaceBetween>
        </Container>
      )}

      {dialog && (
        <Modal
          visible
          closeAriaLabel={t("Close dialog")}
          onDismiss={() => {
            // The deploy request cannot be withdrawn; keep its outcome visible.
            if (!dialog.busy) setDialog(null);
          }}
          header={t("{action}?", {
            action: actionPhrase(t, dialog.action, dialog.version),
          })}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="link"
                  onClick={() => setDialog(null)}
                  disabled={dialog.busy}
                >
                  {t("Cancel")}
                </Button>
                <Button
                  variant="primary"
                  loading={dialog.busy}
                  disabled={!canConfirm}
                  disabledReason={confirmDisabledReason}
                  onClick={confirmDeploy}
                >
                  {actionPhrase(t, dialog.action, dialog.version)}
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            {!dialog.detail && !dialog.error && (
              <StatusIndicator type="loading">
                {t("Checking deployment impact…")}
              </StatusIndicator>
            )}
            {dialog.detail && breaking.length === 0 && (
              <Box>
                {t(
                  "This replaces the served ontology graph and its embeddings with v{version}, and asks the VKG to reload its mappings, which takes effect within a few minutes.",
                  { version: dialog.version },
                )}
              </Box>
            )}
            {breaking.length > 0 && (
              <SpaceBetween size="m">
                <Alert
                  type="warning"
                  header={t(
                    breakingMetricCount === 1
                      ? "{count} governed metric will lose its linked class"
                      : "{count} governed metrics will lose their linked class",
                    { count: breakingMetricCount },
                  )}
                >
                  {t(
                    "These metrics stay defined but point at classes v{version} no longer contains.",
                    { version: dialog.version },
                  )}
                </Alert>
                <Table
                  variant="embedded"
                  items={breaking}
                  trackBy={(m) => `${m.metricId}|${m.missingClassIri}`}
                  columnDefinitions={[
                    { id: "name", header: t("Metric"), cell: (m) => m.name },
                    {
                      id: "class",
                      header: t("Removed class"),
                      cell: (m) => m.missingClassIri,
                    },
                  ]}
                />
                <Checkbox
                  checked={dialog.acknowledged}
                  disabled={dialog.busy}
                  onChange={({ detail }) =>
                    setDialog({ ...dialog, acknowledged: detail.checked })
                  }
                >
                  {t("I understand these metrics will break")}
                </Checkbox>
              </SpaceBetween>
            )}
            {dialog.error && (
              <Alert type="error" header={dialog.error.header}>
                {dialog.error.message}
              </Alert>
            )}
          </SpaceBetween>
        </Modal>
      )}
    </SpaceBetween>
  );
}
