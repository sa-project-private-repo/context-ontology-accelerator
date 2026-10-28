// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The Checks tab of the Ontology designer: the structural findings of
 * the revision on screen, what structured queries cannot answer in it, and
 * its comparison with the data outside the draft, fetched again for every
 * revision. **Run full validation** starts the proposal's validate job, which
 * reads the draft too, and polls it.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Link from "@cloudscape-design/components/link";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import StatusIndicator, {
  type StatusIndicatorProps,
} from "@cloudscape-design/components/status-indicator";
import type { ApiClient } from "@components/ApiClientProvider";
import {
  getProposalValidationJob,
  validateProposal,
  type ProposalValidationJob,
} from "../../../services/ontology-engine";
import {
  getStudioChecks,
  type StudioChecks,
  type StudioDataChecks,
  type StudioFinding,
  type StudioFindingSeverity,
  type StudioStructuredCheck,
  type StudioStructuredGroup,
  type StudioStructuredTerm,
} from "../../../services/studio";
import { useAppI18n, type AppTranslate } from "../../../i18n/LocaleProvider";
import { errorMessage, iriTail } from "./studio-helpers";
import {
  errorSentence,
  findingMessage,
  studioMessageText,
} from "./studio-messages";

/** How often a running validation job is polled. */
export const VALIDATION_POLL_MS = 2000;
/**
 * Failed reads of a running validation job in a row after which it is given
 * up on, so Run full validation can start another one.
 */
export const MAX_FAILED_POLLS = 4;
/** The findings an "Ask the assistant" message names. */
const ASKED_FINDINGS = 10;
/** The terms each structured-query finding links to. */
const SHOWN_TERMS = 20;

const STATUS: Record<StudioFindingSeverity, StatusIndicatorProps.Type> = {
  error: "error",
  warning: "warning",
  info: "info",
};

/**
 * A line of the panel, after its status icon. StatusIndicator lets its text
 * break between any two letters (`word-break: break-all`), so a sentence
 * wrapped inside its words; this text wraps between words, and breaks a word
 * only when it is longer than the line (a long IRI).
 */
function StatusLine({
  type,
  children,
}: {
  type: StatusIndicatorProps.Type;
  children: React.ReactNode;
}) {
  return (
    <StatusIndicator type={type}>
      <span style={{ wordBreak: "normal", overflowWrap: "anywhere" }}>
        {children}
      </span>
    </StatusIndicator>
  );
}

export interface ChecksPanelProps {
  apiClient: ApiClient;
  namespace: string;
  proposalId: string;
  /**
   * The revision on screen; a new one checks again, and a full validation of
   * an earlier one says it is out of date.
   */
  revision: number;
  /** The draft is under review: only then does its chat take a message. */
  editable: boolean;
  /**
   * Why the caller's role may not change the draft. Set, a full validation
   * (a `manageOntology` request) is not offered either, whatever the draft's state.
   */
  deniedReason?: string;
  onOpenTerm: (iri: string) => void;
  /** Prefill the chat with a message and switch to it. */
  onAsk: (text: string) => void;
}

function Finding({
  finding,
  onOpenTerm,
}: {
  finding: StudioFinding;
  onOpenTerm: (iri: string) => void;
}) {
  const { t } = useAppI18n();
  return (
    <SpaceBetween size="xxs">
      <StatusLine type={STATUS[finding.severity]}>
        {studioMessageText(t, findingMessage(finding), finding.message)}
      </StatusLine>
      <SpaceBetween direction="horizontal" size="xs">
        {finding.iris.map((iri) => (
          <Link
            key={iri}
            variant="secondary"
            fontSize="body-s"
            onFollow={(e) => {
              e.preventDefault();
              onOpenTerm(iri);
            }}
          >
            {iriTail(iri)}
          </Link>
        ))}
      </SpaceBetween>
    </SpaceBetween>
  );
}

/**
 * The targets of the stale mappings, one entry per target with its row count
 * when several rows share it, and the rows past the engine's cut of 50 as
 * "+N more", so the list adds up to `stale`.
 */
function staleTargetList(
  stale: number,
  targets: string[],
  t: AppTranslate,
): string {
  const rows = new Map<string, number>();
  for (const target of targets) rows.set(target, (rows.get(target) ?? 0) + 1);
  const items = [...rows]
    .map(([target, count]) =>
      count === 1
        ? iriTail(target)
        : t("{label} ({count})", { label: iriTail(target), count }),
    )
    .join(", ");
  const more = stale - targets.length;
  return more > 0 ? t("{items}, +{count} more", { items, count: more }) : items;
}

function SectionError({ error }: { error: string }) {
  return <StatusLine type="warning">{error}</StatusLine>;
}

const NO_TERMS: StudioStructuredGroup = { count: 0, terms: [] };

/**
 * One structured-query finding: its sentence, then its first terms, as links
 * when they can be opened (a term the draft deleted cannot).
 */
function StructuredFinding({
  type,
  text,
  group,
  detail,
  onOpenTerm,
}: {
  type: StatusIndicatorProps.Type;
  text: string;
  group: StudioStructuredGroup;
  /** What to show after a term's label, such as its table name. */
  detail?: (term: StudioStructuredTerm) => string | undefined;
  onOpenTerm?: (iri: string) => void;
}) {
  const { t } = useAppI18n();
  const shown = group.terms.slice(0, SHOWN_TERMS);
  return (
    <SpaceBetween size="xxs">
      <StatusLine type={type}>{text}</StatusLine>
      <SpaceBetween direction="horizontal" size="xs">
        {shown.map((term) => {
          const extra = detail?.(term);
          const name = extra
            ? t("{label} ({name})", { label: term.label, name: extra })
            : term.label;
          if (!onOpenTerm)
            return (
              <Box key={term.iri} variant="small">
                {name}
              </Box>
            );
          return (
            <Link
              key={term.iri}
              variant="secondary"
              fontSize="body-s"
              onFollow={(e) => {
                e.preventDefault();
                onOpenTerm(term.iri);
              }}
            >
              {name}
            </Link>
          );
        })}
        {group.count > shown.length && (
          <Box variant="small" color="text-body-secondary">
            {t("and {count} more", { count: group.count - shown.length })}
          </Box>
        )}
      </SpaceBetween>
    </SpaceBetween>
  );
}

/** What structured queries (Ontop and NL-to-SQL) cannot answer in the draft. */
function StructuredSection({
  structured,
  onOpenTerm,
}: {
  structured: StudioStructuredCheck;
  onOpenTerm: (iri: string) => void;
}) {
  const { t } = useAppI18n();
  const {
    unmappedClasses,
    unmappedProperties,
    labelDiffers,
    multiTableClasses,
    mapsNoTable,
    error,
  } = structured;
  const removedClasses = structured.removedMappedClasses ?? NO_TERMS;
  const removedProperties = structured.removedMappedProperties ?? NO_TERMS;
  const clear =
    !mapsNoTable &&
    unmappedClasses.count +
      unmappedProperties.count +
      labelDiffers.count +
      multiTableClasses.count +
      removedClasses.count +
      removedProperties.count ===
      0;
  return (
    <SpaceBetween size="s">
      {error && (
        <SectionError
          error={studioMessageText(
            t,
            errorSentence(structured.errorCode),
            error,
          )}
        />
      )}
      {mapsNoTable && (
        <StatusLine type="warning">
          {t(
            "This draft maps no table; structured queries are off after deploy.",
          )}
        </StatusLine>
      )}
      {/* Deleted from the draft: nothing to open, so the terms are not links. */}
      {removedClasses.count > 0 && (
        <StructuredFinding
          type="warning"
          text={t(
            removedClasses.count === 1
              ? "{count} mapped class is deleted; structured queries no longer answer from its tables."
              : "{count} mapped classes are deleted; structured queries no longer answer from their tables.",
            { count: removedClasses.count },
          )}
          group={removedClasses}
          detail={(term) => term.tables?.join(", ")}
        />
      )}
      {removedProperties.count > 0 && (
        <StructuredFinding
          type="warning"
          text={t(
            removedProperties.count === 1
              ? "{count} mapped attribute or relationship is deleted; structured queries no longer use its column or join."
              : "{count} mapped attributes or relationships are deleted; structured queries no longer use their columns or joins.",
            { count: removedProperties.count },
          )}
          group={removedProperties}
          detail={(term) => term.physicalName}
        />
      )}
      {unmappedProperties.count > 0 && (
        <StructuredFinding
          type="warning"
          text={t(
            unmappedProperties.count === 1
              ? "{count} attribute or relationship of a mapped class, or relationship into one, has no column or join behind it. Structured queries do not use it."
              : "{count} attributes or relationships of mapped classes, or relationships into them, have no column or join behind them. Structured queries do not use them.",
            { count: unmappedProperties.count },
          )}
          group={unmappedProperties}
          onOpenTerm={onOpenTerm}
        />
      )}
      {multiTableClasses.count > 0 && (
        <StructuredFinding
          type="warning"
          text={t(
            multiTableClasses.count === 1
              ? "{count} class is mapped from more than one table; structured queries treat it as one table."
              : "{count} classes are mapped from more than one table; structured queries treat each of them as one table.",
            { count: multiTableClasses.count },
          )}
          group={multiTableClasses}
          detail={(term) => term.tables?.join(", ")}
          onOpenTerm={onOpenTerm}
        />
      )}
      {unmappedClasses.count > 0 && (
        <StructuredFinding
          type="info"
          text={t(
            unmappedClasses.count === 1
              ? "{count} class has no table mapping. Structured queries cannot answer it; facts extracted from documents can fill it after deploy."
              : "{count} classes have no table mapping. Structured queries cannot answer them; facts extracted from documents can fill them after deploy.",
            { count: unmappedClasses.count },
          )}
          group={unmappedClasses}
          onOpenTerm={onOpenTerm}
        />
      )}
      {labelDiffers.count > 0 && (
        <StructuredFinding
          type="info"
          text={t(
            labelDiffers.count === 1
              ? "{count} mapped term has a label that differs from its table or column name. Structured queries keep using the table or column name."
              : "{count} mapped terms have a label that differs from their table or column name. Structured queries keep using the table and column names.",
            { count: labelDiffers.count },
          )}
          group={labelDiffers}
          detail={(term) => term.physicalName}
          onOpenTerm={onOpenTerm}
        />
      )}
      {clear && (
        <StatusLine type="success">
          {t(
            "Every class has a table mapping, and so do the attributes and relationships of mapped classes and the relationships into them.",
          )}
        </StatusLine>
      )}
    </SpaceBetween>
  );
}

function DataSections({
  data,
  onOpenTerm,
}: {
  data: StudioDataChecks;
  onOpenTerm: (iri: string) => void;
}) {
  const { t } = useAppI18n();
  const { tables, abox, mappings, metrics } = data;
  // A section's error in the user's language, or as the engine wrote it.
  const failed = (error: string, code?: string | null) =>
    studioMessageText(t, errorSentence(code), error);
  const impacts = abox.impacts ?? [];
  const breaking = metrics.breaking ?? [];
  return (
    <SpaceBetween size="s">
      <SpaceBetween size="xxs">
        <Box variant="h4">{t("Source tables")}</Box>
        {tables.error ? (
          <SectionError error={failed(tables.error, tables.errorCode)} />
        ) : !tables.checked ? (
          <Box color="text-body-secondary">
            {t("The draft maps no tables.")}
          </Box>
        ) : tables.missing?.length ? (
          <StatusLine type="error">
            {t("Not in the source catalog: {tables}", {
              tables: tables.missing.join(", "),
            })}
          </StatusLine>
        ) : (
          <StatusLine type="success">
            {t("Every mapped table is in the source catalog.")}
          </StatusLine>
        )}
      </SpaceBetween>
      <SpaceBetween size="xxs">
        <Box variant="h4">{t("Domain data")}</Box>
        {abox.error ? (
          <SectionError error={failed(abox.error, abox.errorCode)} />
        ) : !abox.available ? (
          <Box color="text-body-secondary">
            {t("The namespace has no domain data yet.")}
          </Box>
        ) : impacts.length === 0 ? (
          <StatusLine type="success">
            {t("No class with instances is removed, renamed or merged.")}
          </StatusLine>
        ) : (
          impacts.map((i) => (
            <StatusLine key={i.classIri} type="warning">
              <Link
                variant="secondary"
                onFollow={(e) => {
                  e.preventDefault();
                  onOpenTerm(i.classIri);
                }}
              >
                {iriTail(i.classIri)}
              </Link>{" "}
              {i.change === "renamed" && i.toIri
                ? t(
                    i.instances === 1
                      ? "has {count} instance and is renamed to {name}."
                      : "has {count} instances and is renamed to {name}.",
                    { count: i.instances, name: iriTail(i.toIri) },
                  )
                : i.change === "merged" && i.toIri
                  ? t(
                      i.instances === 1
                        ? "has {count} instance and is merged into {name}."
                        : "has {count} instances and is merged into {name}.",
                      { count: i.instances, name: iriTail(i.toIri) },
                    )
                  : t(
                      i.instances === 1
                        ? "has {count} instance and is removed."
                        : "has {count} instances and is removed.",
                      { count: i.instances },
                    )}
            </StatusLine>
          ))
        )}
        {abox.available && abox.current === false && (
          <Box variant="small" color="text-body-secondary">
            {t("The domain data is being rebuilt; counts may be out of date.")}
          </Box>
        )}
      </SpaceBetween>
      <SpaceBetween size="xxs">
        <Box variant="h4">{t("Document mappings")}</Box>
        {mappings.error ? (
          <SectionError error={failed(mappings.error, mappings.errorCode)} />
        ) : !mappings.available ? (
          <Box color="text-body-secondary">
            {t("No version is active yet.")}
          </Box>
        ) : mappings.stale ? (
          <StatusLine type="warning">
            {t(
              mappings.stale === 1
                ? "{count} mapping would go stale: {targets}"
                : "{count} mappings would go stale: {targets}",
              {
                count: mappings.stale,
                targets: staleTargetList(
                  mappings.stale,
                  mappings.staleTargets ?? [],
                  t,
                ),
              },
            )}
          </StatusLine>
        ) : (
          <StatusLine type="success">
            {t("No document mapping would go stale.")}
          </StatusLine>
        )}
      </SpaceBetween>
      <SpaceBetween size="xxs">
        <Box variant="h4">{t("Governed metrics")}</Box>
        {metrics.error ? (
          <SectionError error={failed(metrics.error, metrics.errorCode)} />
        ) : breaking.length === 0 ? (
          <StatusLine type="success">{t("No metric would break.")}</StatusLine>
        ) : (
          breaking.map((m) => (
            <StatusLine key={m.metricId} type="error">
              {t("{metric} needs {class}", {
                metric: m.name || m.metricId,
                class: iriTail(m.missingClassIri),
              })}
            </StatusLine>
          ))
        )}
      </SpaceBetween>
    </SpaceBetween>
  );
}

function FullValidation({
  apiClient,
  namespace,
  proposalId,
  revision,
  deniedReason,
}: Pick<
  ChecksPanelProps,
  "apiClient" | "namespace" | "proposalId" | "revision" | "deniedReason"
>) {
  const { t } = useAppI18n();
  const [job, setJob] = useState<ProposalValidationJob | null>(null);
  // The revision on screen when the job started, the one it validates: the
  // draft moves on under an open Checks tab (Undo, an edit from the graph).
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [failedPolls, setFailedPolls] = useState(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const running = job?.status === "pending" || job?.status === "running";
  // A failed read keeps the last answer, so it bounds how long a running job
  // drives the poll.
  const polling = running && failedPolls < MAX_FAILED_POLLS;
  const jobId = job?.job_id;
  useEffect(() => {
    if (!polling || !jobId) return;
    const handle = window.setTimeout(() => {
      getProposalValidationJob(apiClient, namespace, proposalId, jobId).then(
        (next) => {
          if (!mounted.current) return;
          setJob(next);
          setError(null);
          setFailedPolls(0);
        },
        (e: unknown) => {
          if (!mounted.current) return;
          setError(errorMessage(e, t));
          // The new count runs this effect again, which reads again.
          setFailedPolls((n) => n + 1);
        },
      );
    }, VALIDATION_POLL_MS);
    return () => window.clearTimeout(handle);
  }, [apiClient, namespace, proposalId, jobId, polling, job, failedPolls, t]);

  async function start() {
    setStarting(true);
    setError(null);
    // The new run replaces the last one: a job given up on is not read again.
    setJob(null);
    setFailedPolls(0);
    try {
      const started = await validateProposal(apiClient, namespace, proposalId);
      if (mounted.current) {
        setJob(started);
        setStartedAt(revision);
      }
    } catch (e) {
      if (mounted.current) setError(errorMessage(e, t));
    } finally {
      if (mounted.current) setStarting(false);
    }
  }

  const findings = job?.report?.findings ?? [];
  const outdated = job !== null && !running && startedAt !== revision;
  return (
    <SpaceBetween size="xs">
      <Button
        loading={starting || polling}
        disabled={starting || polling || deniedReason !== undefined}
        disabledReason={deniedReason}
        onClick={() => void start()}
      >
        {t("Run full validation")}
      </Button>
      {error && (
        <Alert
          type="error"
          header={
            running && !polling
              ? t("The validation's status could not be read")
              : undefined
          }
        >
          {error}
        </Alert>
      )}
      {outdated && (
        <StatusLine type="warning">
          {t("Validation of revision {revision}, out of date: run it again.", {
            revision: startedAt ?? 0,
          })}
        </StatusLine>
      )}
      {job?.status === "failed" && (
        <Alert type="error">{job.error ?? t("The validation failed.")}</Alert>
      )}
      {job?.status === "completed" && job.report && (
        <SpaceBetween size="xxs">
          <StatusLine type={job.report.passed ? "success" : "error"}>
            {job.report.passed
              ? t("The validation passed.")
              : t("The validation found problems.")}
          </StatusLine>
          {findings.map((f, n) => (
            <StatusLine key={n} type={STATUS[f.severity]}>
              <code>{f.code}</code> {f.message}
            </StatusLine>
          ))}
        </SpaceBetween>
      )}
    </SpaceBetween>
  );
}

/** A message asking the assistant to fix the worst findings. */
export function askAboutFindings(
  findings: StudioFinding[],
  t: AppTranslate,
): string {
  const listed = findings
    .slice(0, ASKED_FINDINGS)
    .map((f) => `- ${studioMessageText(t, findingMessage(f), f.message)}`)
    .join("\n");
  return `${t("The checks found these problems in the draft. Explain them and propose fixes:")}\n${listed}`;
}

export function ChecksPanel({
  apiClient,
  namespace,
  proposalId,
  revision,
  editable,
  deniedReason,
  onOpenTerm,
  onAsk,
}: ChecksPanelProps) {
  const { t } = useAppI18n();
  const [checks, setChecks] = useState<StudioChecks | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef(0);

  const load = useCallback(async () => {
    const mine = ++request.current;
    setLoading(true);
    try {
      const result = await getStudioChecks(apiClient, namespace, proposalId);
      if (mine === request.current) {
        setChecks(result);
        setError(null);
      }
    } catch (e) {
      if (mine === request.current) setError(errorMessage(e, t));
    } finally {
      if (mine === request.current) setLoading(false);
    }
  }, [apiClient, namespace, proposalId, t]);

  useEffect(() => {
    void load();
    // A new revision is checked again.
  }, [load, revision]);

  // Unmounted (another tab): an answer still on its way is dropped.
  useEffect(
    () => () => {
      request.current++;
    },
    [],
  );

  const findings = checks?.instant.findings ?? [];
  const counts = checks?.instant.counts;
  return (
    <SpaceBetween size="m">
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        <Box variant="h3">
          {checks
            ? t("Checks of revision {revision}", { revision: checks.revision })
            : t("Checks")}
        </Box>
        <Button
          iconName="refresh"
          variant="icon"
          ariaLabel={t("Check again")}
          loading={loading}
          onClick={() => void load()}
        />
      </SpaceBetween>
      {error && <Alert type="error">{error}</Alert>}
      {!checks && loading && <Spinner />}
      {checks && counts && (
        <SpaceBetween size="m">
          <SpaceBetween direction="horizontal" size="xs">
            <Badge color={counts.error ? "red" : "grey"}>
              {t(counts.error === 1 ? "{count} error" : "{count} errors", {
                count: counts.error,
              })}
            </Badge>
            <Badge color={counts.warning ? "blue" : "grey"}>
              {t(
                counts.warning === 1 ? "{count} warning" : "{count} warnings",
                { count: counts.warning },
              )}
            </Badge>
            <Badge color="grey">
              {t("{count} info", { count: counts.info })}
            </Badge>
          </SpaceBetween>
          {findings.length === 0 ? (
            <StatusLine type="success">
              {t("No structural problem found.")}
            </StatusLine>
          ) : (
            <SpaceBetween size="s">
              {findings.map((f) => (
                <Finding
                  key={`${f.code} ${f.iris.join(" ")}`}
                  finding={f}
                  onOpenTerm={onOpenTerm}
                />
              ))}
              {checks.instant.truncated && (
                <Box variant="small" color="text-body-secondary">
                  {t("Only the worst {count} findings are listed.", {
                    count: findings.length,
                  })}
                </Box>
              )}
              <Button
                iconName="contact"
                onClick={() =>
                  onAsk(
                    askAboutFindings(
                      findings.filter((f) => f.severity !== "info"),
                      t,
                    ),
                  )
                }
                disabled={
                  !editable || findings.every((f) => f.severity === "info")
                }
                disabledReason={deniedReason}
              >
                {t("Ask the assistant")}
              </Button>
            </SpaceBetween>
          )}
          {checks.structured && (
            <ExpandableSection
              headerText={t("Structured queries")}
              defaultExpanded
            >
              <StructuredSection
                structured={checks.structured}
                onOpenTerm={onOpenTerm}
              />
            </ExpandableSection>
          )}
          <ExpandableSection
            headerText={t("Compared with the data")}
            defaultExpanded
          >
            <DataSections data={checks.data} onOpenTerm={onOpenTerm} />
          </ExpandableSection>
        </SpaceBetween>
      )}
      <ExpandableSection headerText={t("Full validation")}>
        <FullValidation
          apiClient={apiClient}
          namespace={namespace}
          proposalId={proposalId}
          revision={revision}
          deniedReason={deniedReason}
        />
      </ExpandableSection>
    </SpaceBetween>
  );
}
