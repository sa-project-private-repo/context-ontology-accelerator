// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Accepting a designer draft publishes exactly the revision on screen as the
 * next version. The dialog shows what that removes and renames, the governed
 * metrics it would break, the terms it adds or changes that structured
 * queries cannot answer, and the mapped terms it takes out of structured
 * queries, before the user confirms.
 */
import React, { useEffect, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Checkbox from "@cloudscape-design/components/checkbox";
import Link from "@cloudscape-design/components/link";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import type { ApiClient } from "@components/ApiClientProvider";
import type { AppMessage } from "../../../i18n/appMessages";
import { useAppI18n } from "../../../i18n/LocaleProvider";
import {
  getStudioAcceptPreview,
  type StudioAcceptPreview,
} from "../../../services/studio";
import { diffCounts, errorMessage, iriTail } from "./studio-helpers";
import { errorSentence, studioMessageText } from "./studio-messages";

const SHOWN = 20;
const KIND_TITLES: Record<
  "classes" | "objectProperties" | "datatypeProperties",
  AppMessage
> = {
  classes: "Classes removed ({count})",
  objectProperties: "Relationships removed ({count})",
  datatypeProperties: "Attributes removed ({count})",
};

export interface AcceptDialogProps {
  visible: boolean;
  apiClient: ApiClient;
  namespace: string;
  proposalId: string;
  revision: number;
  accepting: boolean;
  onDismiss: () => void;
  onAccept: (deploy: boolean) => void;
  /** Each preview as it arrives: it can know of a newer version or revision before the page does. */
  onPreview: (preview: StudioAcceptPreview) => void;
  /** The "Deploy immediately" choice the dialog starts with (read when it mounts). */
  defaultDeploy?: boolean;
  /** Set by a page other than the designer page: the rebase notice then links here. */
  onOpenStudio?: () => void;
}

export function AcceptDialog({
  visible,
  apiClient,
  namespace,
  proposalId,
  revision,
  accepting,
  onDismiss,
  onAccept,
  onPreview,
  defaultDeploy = true,
  onOpenStudio,
}: AcceptDialogProps) {
  const { t } = useAppI18n();
  const [preview, setPreview] = useState<StudioAcceptPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deploy, setDeploy] = useState(defaultDeploy);

  useEffect(() => {
    if (!visible) return;
    let live = true;
    setPreview(null);
    setError(null);
    const load = async () => {
      try {
        const p = await getStudioAcceptPreview(
          apiClient,
          namespace,
          proposalId,
        );
        if (!live) return;
        setPreview(p);
        onPreview(p);
      } catch (e) {
        if (live) setError(errorMessage(e, t));
      }
    };
    void load();
    return () => {
      live = false;
    };
  }, [visible, apiClient, namespace, proposalId, revision, onPreview, t]);

  const counts = preview ? diffCounts(preview.diff) : null;
  const structured = preview?.structured;
  // The added or changed terms that no table mapping backs: the first terms of
  // each group, attributes and relationships of mapped classes first, as the
  // Checks tab lists them, so many new classes cannot hide them.
  const unanswered = structured
    ? [
        ...structured.unmappedProperties.terms.slice(0, SHOWN),
        ...structured.unmappedClasses.terms.slice(0, SHOWN),
      ]
    : [];
  const unansweredCount = structured
    ? structured.unmappedClasses.count + structured.unmappedProperties.count
    : 0;
  // The mapped terms of the base version that the draft deletes, with the
  // tables or the column structured queries stop reading.
  const removedGroups = structured
    ? [structured.removedMappedClasses, structured.removedMappedProperties]
    : [];
  const removedTerms = removedGroups.flatMap((g) => g?.terms ?? []);
  const removedCount = removedGroups.reduce((n, g) => n + (g?.count ?? 0), 0);
  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={t("Accept revision {revision}", { revision })}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              {t("Cancel")}
            </Button>
            <Button
              variant="primary"
              loading={accepting}
              disabled={!preview || preview.rebaseRequired}
              onClick={() => onAccept(deploy)}
            >
              {t("Accept")}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      {error && <Alert type="error">{error}</Alert>}
      {!preview && !error && <Spinner />}
      {preview && counts && (
        <SpaceBetween size="m">
          <Box>
            {preview.baseVersion === null
              ? t(
                  "The draft becomes the next version as it is: {added} added, {changed} changed and {removed} removed term(s) compared with no version.",
                  { ...counts },
                )
              : t(
                  "The draft becomes the next version as it is: {added} added, {changed} changed and {removed} removed term(s) compared with v{version}.",
                  { ...counts, version: preview.baseVersion },
                )}
          </Box>
          {preview.rebaseRequired && (
            <Alert type="error" header={t("Rebase first")}>
              {t(
                "A newer version was published after this draft was built. Rebase the draft, review it, and accept again.",
              )}
              {onOpenStudio && (
                <>
                  {" "}
                  <Link
                    href="#"
                    onFollow={(e) => {
                      e.preventDefault();
                      onOpenStudio();
                    }}
                  >
                    {t("Open the Ontology designer to rebase it.")}
                  </Link>
                </>
              )}
            </Alert>
          )}
          {(Object.keys(KIND_TITLES) as (keyof typeof KIND_TITLES)[]).map(
            (kind) => {
              const removed = preview.diff[kind].removed;
              if (removed.length === 0) return null;
              return (
                <div key={kind}>
                  <Box variant="h4">
                    {t(KIND_TITLES[kind], { count: removed.length })}
                  </Box>
                  <ul style={{ margin: 0, paddingInlineStart: 18 }}>
                    {removed.slice(0, SHOWN).map((iri) => (
                      <li key={iri} title={iri}>
                        {iriTail(iri)}
                      </li>
                    ))}
                  </ul>
                  {removed.length > SHOWN && (
                    <Box variant="small">
                      {t("and {count} more", {
                        count: removed.length - SHOWN,
                      })}
                    </Box>
                  )}
                </div>
              );
            },
          )}
          {preview.renamed.length > 0 && (
            <div>
              <Box variant="h4">
                {t("Renamed or merged ({count})", {
                  count: preview.renamed.length,
                })}
              </Box>
              <ul style={{ margin: 0, paddingInlineStart: 18 }}>
                {preview.renamed.map((r) => (
                  <li key={r.fromIri}>
                    {t(
                      r.kind === "merged"
                        ? "{from} merged into {to}"
                        : "{from} renamed to {to}",
                      { from: iriTail(r.fromIri), to: iriTail(r.toIri) },
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {structured?.mapsNoTable && (
            <Alert type="warning">
              {t(
                "This draft maps no table; structured queries are off after deploy.",
              )}
            </Alert>
          )}
          {/* A fixed message from the engine: the no-table check did not run. */}
          {structured?.error && (
            <Alert type="info">
              {studioMessageText(
                t,
                errorSentence(structured.errorCode),
                structured.error,
              )}
            </Alert>
          )}
          {unansweredCount > 0 && (
            <div>
              <Box variant="h4">
                {t("Not answered by structured queries ({count})", {
                  count: unansweredCount,
                })}
              </Box>
              <ul style={{ margin: 0, paddingInlineStart: 18 }}>
                {unanswered.map((term) => (
                  <li key={term.iri} title={term.iri}>
                    {term.label}
                  </li>
                ))}
              </ul>
              {unansweredCount > unanswered.length && (
                <Box variant="small">
                  {t("and {count} more", {
                    count: unansweredCount - unanswered.length,
                  })}
                </Box>
              )}
            </div>
          )}
          {removedCount > 0 && (
            <div>
              <Box variant="h4">
                {t("Removed from structured queries ({count})", {
                  count: removedCount,
                })}
              </Box>
              <ul style={{ margin: 0, paddingInlineStart: 18 }}>
                {removedTerms.slice(0, SHOWN).map((term) => {
                  const read = term.tables?.join(", ") ?? term.physicalName;
                  return (
                    <li key={term.iri} title={term.iri}>
                      {read
                        ? t("{label} ({name})", {
                            label: term.label,
                            name: read,
                          })
                        : term.label}
                    </li>
                  );
                })}
              </ul>
              {removedCount > Math.min(removedTerms.length, SHOWN) && (
                <Box variant="small">
                  {t("and {count} more", {
                    count: removedCount - Math.min(removedTerms.length, SHOWN),
                  })}
                </Box>
              )}
            </div>
          )}
          {preview.breakingMetrics.length > 0 && (
            <Alert
              type="warning"
              header={t("Governed metrics this version breaks")}
            >
              <ul style={{ margin: 0, paddingInlineStart: 18 }}>
                {preview.breakingMetrics.map((m) => (
                  <li key={m.metricId}>
                    {t("{name} (uses {class})", {
                      name: m.name,
                      class: iriTail(m.missingClassIri),
                    })}
                  </li>
                ))}
              </ul>
              {t(
                "An automatic deployment is held until you deploy this version from Ontology › Induction › Versions and acknowledge them.",
              )}
            </Alert>
          )}
          {/* A fixed message from the engine, in the user's language by its code. */}
          {preview.breakingMetricsError && (
            <Alert type="info">
              {studioMessageText(
                t,
                errorSentence(preview.breakingMetricsErrorCode),
                preview.breakingMetricsError,
              )}
            </Alert>
          )}
          <Checkbox
            checked={deploy}
            onChange={({ detail }) => setDeploy(detail.checked)}
          >
            {t("Deploy immediately")}
          </Checkbox>
        </SpaceBetween>
      )}
    </Modal>
  );
}
