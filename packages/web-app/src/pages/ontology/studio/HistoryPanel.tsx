// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/** The draft's change history, newest first (``GET .../studio/history``). */
import React from "react";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import { formatTimestamp } from "@utils/helpers";
import type { AppMessage } from "../../../i18n/appMessages";
import { useAppI18n } from "../../../i18n/LocaleProvider";
import type {
  StudioHistory,
  StudioHistoryEntry,
} from "../../../services/studio";
import { studioMessageLines, studioMessageText } from "./studio-messages";

const KIND_LABELS: Record<StudioHistoryEntry["kind"], AppMessage> = {
  open: "Opened",
  apply: "Change",
  undo: "Undo",
  redo: "Redo",
  rebase: "Rebase",
};

export interface HistoryPanelProps {
  history: StudioHistory | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  /** Reads the next (older) page; offered while `history.nextToken` is set. */
  onLoadOlder: () => void;
}

export function HistoryPanel({
  history,
  loading,
  error,
  onRefresh,
  onLoadOlder,
}: HistoryPanelProps) {
  const { t, locale } = useAppI18n();
  return (
    <Container
      header={
        <Header
          variant="h2"
          actions={
            <Button
              iconName="refresh"
              ariaLabel={t("Refresh history")}
              loading={loading}
              onClick={onRefresh}
            />
          }
        >
          {t("Change history")}
        </Header>
      }
    >
      {error && <Alert type="error">{error}</Alert>}
      {loading && !history && <Spinner />}
      {history && (
        <SpaceBetween size="s">
          {history.entries.map((e) => (
            <div
              key={e.revision}
              data-testid={`history-${e.revision}`}
              style={{ opacity: e.undone ? 0.55 : 1 }}
            >
              <SpaceBetween
                direction="horizontal"
                size="xs"
                alignItems="center"
              >
                <Box fontWeight="bold">
                  {t("Revision {revision}", { revision: e.revision })}
                </Box>
                <Badge color={e.kind === "apply" ? "blue" : "grey"}>
                  {t(KIND_LABELS[e.kind])}
                </Badge>
                {e.source === "agent" && (
                  <Badge color="green">{t("Assistant")}</Badge>
                )}
                {e.undone && <Badge color="grey">{t("Undone")}</Badge>}
              </SpaceBetween>
              <Box variant="small">
                {e.at
                  ? t("{author}, {time}", {
                      author: e.author ?? t("unknown"),
                      time: formatTimestamp(e.at, locale),
                    })
                  : (e.author ?? t("unknown"))}
              </Box>
              {e.rationale && (
                <Box variant="small" color="text-body-secondary">
                  {e.rationale}
                </Box>
              )}
              <ul style={{ margin: "2px 0 0", paddingInlineStart: 18 }}>
                {e.summary.map((line, i) => (
                  <li key={i}>
                    {studioMessageText(t, e.summaryMessages?.[i], line)}
                  </li>
                ))}
              </ul>
              {e.warnings.length > 0 && (
                <Box variant="small" color="text-status-warning">
                  {studioMessageLines(t, e.warningMessages, e.warnings).join(
                    " ",
                  )}
                </Box>
              )}
            </div>
          ))}
          {history.nextToken && (
            <Button onClick={onLoadOlder} loading={loading}>
              {t("Load older revisions")}
            </Button>
          )}
        </SpaceBetween>
      )}
    </Container>
  );
}
