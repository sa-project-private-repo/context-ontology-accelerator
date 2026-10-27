// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * One change set the assistant proposed: its rationale and ops, and the
 * user's choice to preview it on the graph, apply all or some of its ops, or
 * discard it. A pending change set starts with every op checked; unchecking
 * ops previews and applies only the rest.
 */
import React, { useEffect, useRef, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Checkbox from "@cloudscape-design/components/checkbox";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useAppI18n, type AppTranslate } from "../../../i18n/LocaleProvider";
import type {
  StudioChangeSet,
  StudioChangeSetPreview,
} from "../../../services/studio";
import { studioMessageLines } from "./studio-messages";

export interface ChangeSetCardProps {
  changeSet: StudioChangeSet;
  /** The draft accepts edits. */
  editable: boolean;
  /**
   * Why the caller's role may not change the draft: set, the buttons say so
   * and the change set cannot be discarded either.
   */
  deniedReason?: string;
  /** A write to the draft is running. */
  busy: boolean;
  /** This change set's preview is on the graph. */
  previewing: boolean;
  /** Preview the ops (all for null); resolves the preview, or null when it could not be read. */
  onPreview: (
    opIndexes: number[] | null,
  ) => Promise<StudioChangeSetPreview | null>;
  onHidePreview: () => void;
  /** Apply the ops (all for null); resolves an error message, or null. */
  onApply: (opIndexes: number[] | null) => Promise<string | null>;
  /** Resolves an error message, or null. */
  onDiscard: () => Promise<string | null>;
}

function statusBadge(cs: StudioChangeSet, t: AppTranslate) {
  switch (cs.status) {
    case "applied":
      return (
        <Badge color="green">
          {cs.appliedRevision === null
            ? t("Applied")
            : t("Applied in revision {revision}", {
                revision: cs.appliedRevision,
              })}
        </Badge>
      );
    case "discarded":
      return <Badge color="grey">{t("Discarded")}</Badge>;
    default:
      return <Badge color="blue">{t("Pending")}</Badge>;
  }
}

export function ChangeSetCard({
  changeSet,
  editable,
  deniedReason,
  busy,
  previewing,
  onPreview,
  onHidePreview,
  onApply,
  onDiscard,
}: ChangeSetCardProps) {
  const { t } = useAppI18n();
  // Its op lines and warnings in the user's language, or as the engine stored them.
  const lines = studioMessageLines(
    t,
    changeSet.summaryMessages,
    changeSet.summary,
  );
  const warnings = studioMessageLines(
    t,
    changeSet.warningMessages,
    changeSet.warnings,
  );
  const count = changeSet.ops.length;
  const [checked, setChecked] = useState<number[]>(() =>
    Array.from({ length: count }, (_, i) => i),
  );
  const [error, setError] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const pending = changeSet.status === "pending";
  const stale = pending ? changeSet.stale : null;
  const choosing = pending && editable;
  const all = checked.length === count;
  const selection = all ? null : checked;
  // The newest preview request wins.
  const previewRequest = useRef(0);

  async function preview(opIndexes: number[] | null) {
    const request = ++previewRequest.current;
    const result = await onPreview(opIndexes);
    if (request !== previewRequest.current) return;
    const failed = result && !result.applies ? result.error : null;
    setError(
      failed
        ? t("“{change}” does not apply: {message}", {
            change:
              lines[failed.opIndex] ??
              t("Op {index}", { index: failed.opIndex }),
            message: failed.message,
          })
        : null,
    );
  }

  // While the preview is shown, a change of the checked ops previews them again.
  const previewingRef = useRef(previewing);
  previewingRef.current = previewing;
  const firstRun = useRef(true);
  useEffect(() => {
    if (firstRun.current) {
      firstRun.current = false;
      return;
    }
    if (previewingRef.current && checked.length > 0)
      void preview(all ? null : checked);
    // `preview` and `all` follow `checked`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checked]);

  async function run(action: () => Promise<string | null>) {
    setWorking(true);
    try {
      setError(await action());
    } finally {
      setWorking(false);
    }
  }

  return (
    <Container
      header={
        <Header
          variant="h3"
          actions={statusBadge(changeSet, t)}
          description={changeSet.rationale}
        >
          {t("Change set")}
        </Header>
      }
    >
      <SpaceBetween size="s">
        {stale && (
          <Alert type="warning" header={t("The draft changed")}>
            {stale.message}
          </Alert>
        )}
        {/* An apply refused as stale says what the warning above says. */}
        {error && error !== stale?.message && (
          <Alert type="error">{error}</Alert>
        )}
        {choosing ? (
          <SpaceBetween size="xxs">
            {lines.map((line, i) => (
              <Checkbox
                key={i}
                checked={checked.includes(i)}
                onChange={({ detail }) =>
                  setChecked((c) =>
                    detail.checked
                      ? [...c, i].sort((a, b) => a - b)
                      : c.filter((x) => x !== i),
                  )
                }
              >
                {line}
              </Checkbox>
            ))}
          </SpaceBetween>
        ) : (
          <ul style={{ margin: 0, paddingInlineStart: 18 }}>
            {lines.map((line, i) => (
              <li
                key={i}
                style={{
                  opacity:
                    changeSet.status === "applied" &&
                    changeSet.appliedOpIndexes &&
                    !changeSet.appliedOpIndexes.includes(i)
                      ? 0.5
                      : 1,
                }}
              >
                {line}
              </li>
            ))}
          </ul>
        )}
        {changeSet.warnings.length > 0 && (
          <Box variant="small" color="text-status-warning">
            {warnings.join(" ")}
          </Box>
        )}
        {pending && (
          <SpaceBetween direction="horizontal" size="xs">
            {previewing ? (
              <Button onClick={onHidePreview}>{t("Hide preview")}</Button>
            ) : (
              <Button
                disabled={checked.length === 0}
                onClick={() => void preview(selection)}
              >
                {t("Show on graph")}
              </Button>
            )}
            <Button
              variant="primary"
              disabled={!editable || busy || checked.length === 0}
              disabledReason={deniedReason}
              loading={working}
              onClick={() => void run(() => onApply(selection))}
            >
              {all
                ? t("Apply all")
                : t("Apply selected ({count})", { count: checked.length })}
            </Button>
            <Button
              disabled={busy || working || deniedReason !== undefined}
              disabledReason={deniedReason}
              onClick={() => void run(onDiscard)}
            >
              {t("Discard")}
            </Button>
          </SpaceBetween>
        )}
      </SpaceBetween>
    </Container>
  );
}
