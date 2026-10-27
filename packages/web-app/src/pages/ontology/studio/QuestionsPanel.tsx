// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The Questions tab of the Ontology designer: the namespace's competency
 * questions, which every draft shares. **Check** asks the assistant to decide,
 * with a query on this draft, whether the draft answers a question; the verdict
 * comes back on the question and the work shows in the Chat tab.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import Textarea from "@cloudscape-design/components/textarea";
import type { ApiClient } from "@components/ApiClientProvider";
import {
  checkStudioQuestion,
  deleteStudioQuestion,
  isTurnInProgress,
  listStudioQuestions,
  newQuestionId,
  putStudioQuestion,
  type StudioQuestion,
  type StudioQuestionStatus,
} from "../../../services/studio";
import type { AppMessage } from "../../../i18n/appMessages";
import { useAppI18n } from "../../../i18n/LocaleProvider";
import { errorMessage } from "./studio-helpers";

/** The longest question and expectation the engine takes. */
export const MAX_QUESTION = 1000;
export const MAX_EXPECTATION = 500;

const VERDICTS: Record<
  StudioQuestionStatus,
  { label: AppMessage; color: "green" | "blue" | "red" }
> = {
  answerable: { label: "Answerable", color: "green" },
  expressible: { label: "Expressible, no data", color: "blue" },
  not_expressible: { label: "Not expressible", color: "red" },
};

export interface QuestionsPanelProps {
  apiClient: ApiClient;
  namespace: string;
  proposalId: string;
  /** Questions can be saved and checked (the draft is under review). */
  editable: boolean;
  /**
   * Why the caller's role may not save, delete or check a question: set, the
   * buttons say so.
   */
  deniedReason?: string;
  /** A check started: show the chat, where the assistant works on it. */
  onChecking: () => void;
  /** Open a question's query in the query console. */
  onOpenQuery: (query: string) => void;
  /**
   * The panel is on screen. It stays mounted while another tab is, and lists
   * the questions again when it is shown again: a check stores its verdict
   * on the question meanwhile.
   */
  active?: boolean;
}

interface Editing {
  questionId: string | null;
  text: string;
  expectation: string;
}

function QuestionForm({
  editing,
  saving,
  onChange,
  onSave,
  onCancel,
}: {
  editing: Editing;
  saving: boolean;
  onChange: (next: Editing) => void;
  onSave: () => void;
  onCancel?: () => void;
}) {
  const { t } = useAppI18n();
  const tooLong =
    editing.text.length > MAX_QUESTION ||
    editing.expectation.length > MAX_EXPECTATION;
  return (
    <SpaceBetween size="xs">
      <FormField label={t("Question")}>
        <Textarea
          value={editing.text}
          rows={2}
          placeholder={t("Which medications did each patient receive?")}
          invalid={editing.text.length > MAX_QUESTION}
          onChange={({ detail }) =>
            onChange({ ...editing, text: detail.value })
          }
        />
      </FormField>
      <FormField label={t("Expected answer (optional)")}>
        <Input
          value={editing.expectation}
          placeholder={t("Patient names with medication names")}
          invalid={editing.expectation.length > MAX_EXPECTATION}
          onChange={({ detail }) =>
            onChange({ ...editing, expectation: detail.value })
          }
        />
      </FormField>
      <SpaceBetween direction="horizontal" size="xs">
        <Button
          variant="primary"
          loading={saving}
          disabled={!editing.text.trim() || tooLong}
          onClick={onSave}
        >
          {editing.questionId ? t("Save") : t("Add question")}
        </Button>
        {onCancel && (
          <Button variant="link" onClick={onCancel}>
            {t("Cancel")}
          </Button>
        )}
      </SpaceBetween>
    </SpaceBetween>
  );
}

const EMPTY: Editing = { questionId: null, text: "", expectation: "" };

export function QuestionsPanel({
  apiClient,
  namespace,
  proposalId,
  editable,
  deniedReason,
  onChecking,
  onOpenQuery,
  active = true,
}: QuestionsPanelProps) {
  const { t } = useAppI18n();
  const [questions, setQuestions] = useState<StudioQuestion[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A request that failed is an error; a check refused for a turn still
  // running is not.
  const [notice, setNotice] = useState<{
    type: "info" | "error";
    text: string;
  } | null>(null);
  const [adding, setAdding] = useState<Editing>(EMPTY);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [saving, setSaving] = useState(false);
  const [checking, setChecking] = useState<string | null>(null);
  // Every draft shares a question and its deletion cannot be undone, so a
  // Delete waits for a confirmation; then the id of the one being deleted.
  const [confirming, setConfirming] = useState<StudioQuestion | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    try {
      const listed: StudioQuestion[] = [];
      let nextToken: string | undefined;
      do {
        const page = await listStudioQuestions(apiClient, namespace, {
          maxResults: 100,
          nextToken,
        });
        listed.push(...page.questions);
        nextToken = page.nextToken;
      } while (nextToken);
      if (mounted.current) {
        setQuestions(listed);
        setError(null);
      }
    } catch (e) {
      if (mounted.current) setError(errorMessage(e, t));
    }
  }, [apiClient, namespace, t]);

  useEffect(() => {
    if (active) void load();
  }, [load, active]);

  async function save(form: Editing, done: () => void) {
    setSaving(true);
    setNotice(null);
    try {
      const saved = await putStudioQuestion(
        apiClient,
        namespace,
        form.questionId ?? newQuestionId(),
        form.text.trim(),
        form.expectation.trim() || null,
      );
      if (!mounted.current) return;
      setQuestions((all) => {
        const list = all ?? [];
        return list.some((q) => q.questionId === saved.questionId)
          ? list.map((q) => (q.questionId === saved.questionId ? saved : q))
          : [...list, saved];
      });
      done();
    } catch (e) {
      if (mounted.current)
        setNotice({ type: "error", text: errorMessage(e, t) });
    } finally {
      if (mounted.current) setSaving(false);
    }
  }

  async function remove(q: StudioQuestion) {
    setConfirming(null);
    setDeleting(q.questionId);
    setNotice(null);
    try {
      await deleteStudioQuestion(apiClient, namespace, q.questionId);
      if (mounted.current)
        setQuestions((all) =>
          (all ?? []).filter((x) => x.questionId !== q.questionId),
        );
    } catch (e) {
      if (mounted.current)
        setNotice({ type: "error", text: errorMessage(e, t) });
    } finally {
      if (mounted.current) setDeleting(null);
    }
  }

  async function check(q: StudioQuestion) {
    setChecking(q.questionId);
    setNotice(null);
    try {
      await checkStudioQuestion(apiClient, namespace, proposalId, q.questionId);
      onChecking();
    } catch (e) {
      if (mounted.current)
        setNotice(
          isTurnInProgress(e)
            ? {
                type: "info",
                text: t(
                  "The assistant is still working on another message; check again when it is done.",
                ),
              }
            : { type: "error", text: errorMessage(e, t) },
        );
    } finally {
      if (mounted.current) setChecking(null);
    }
  }

  return (
    <SpaceBetween size="m">
      <Box color="text-body-secondary">
        {t(
          "Competency questions are what the ontology must answer. Every draft of the namespace shares them; Check asks the assistant to test one against this draft.",
        )}
      </Box>
      {error && <Alert type="error">{error}</Alert>}
      {notice && (
        <Alert type={notice.type} dismissible onDismiss={() => setNotice(null)}>
          {notice.text}
        </Alert>
      )}
      {questions === null && !error && <Spinner />}
      {questions?.length === 0 && (
        <Box color="text-body-secondary">{t("No question yet.")}</Box>
      )}
      {questions?.map((q) =>
        editing?.questionId === q.questionId ? (
          <Container key={q.questionId}>
            <QuestionForm
              editing={editing}
              saving={saving}
              onChange={setEditing}
              onSave={() => void save(editing, () => setEditing(null))}
              onCancel={() => setEditing(null)}
            />
          </Container>
        ) : (
          <Container key={q.questionId}>
            <SpaceBetween size="xs">
              <SpaceBetween
                direction="horizontal"
                size="xs"
                alignItems="center"
              >
                {q.lastCheck ? (
                  <Badge color={VERDICTS[q.lastCheck.status].color}>
                    {t(VERDICTS[q.lastCheck.status].label)}
                  </Badge>
                ) : (
                  <Badge color="grey">{t("Not checked")}</Badge>
                )}
                <Box fontWeight="bold">{q.text}</Box>
              </SpaceBetween>
              {q.expectation && (
                <Box variant="small" color="text-body-secondary">
                  {t("Expected: {expectation}", {
                    expectation: q.expectation,
                  })}
                </Box>
              )}
              {q.lastCheck && (
                <Box variant="small" color="text-body-secondary">
                  {q.lastCheck.explanation ? `${q.lastCheck.explanation} ` : ""}
                  {q.lastCheck.proposalId === proposalId
                    ? t(
                        q.lastCheck.rowCount === 1
                          ? "(revision {revision}, {count} row)"
                          : "(revision {revision}, {count} rows)",
                        {
                          revision: q.lastCheck.revision,
                          count: q.lastCheck.rowCount,
                        },
                      )
                    : t(
                        q.lastCheck.rowCount === 1
                          ? "(another draft, {count} row)"
                          : "(another draft, {count} rows)",
                        { count: q.lastCheck.rowCount },
                      )}
                </Box>
              )}
              {/* Every card has the same buttons: their names say which
                  question each one acts on. */}
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  ariaLabel={t('Check question "{question}"', {
                    question: q.text,
                  })}
                  disabled={
                    !editable || checking !== null || deleting === q.questionId
                  }
                  disabledReason={deniedReason}
                  loading={checking === q.questionId}
                  onClick={() => void check(q)}
                >
                  {t("Check")}
                </Button>
                {q.lastCheck?.query && (
                  <Button
                    ariaLabel={t('Open query of question "{question}"', {
                      question: q.text,
                    })}
                    onClick={() => onOpenQuery(q.lastCheck!.query!)}
                  >
                    {t("Open query")}
                  </Button>
                )}
                <Button
                  ariaLabel={t('Edit question "{question}"', {
                    question: q.text,
                  })}
                  disabled={!editable || deleting === q.questionId}
                  disabledReason={deniedReason}
                  onClick={() =>
                    setEditing({
                      questionId: q.questionId,
                      text: q.text,
                      expectation: q.expectation ?? "",
                    })
                  }
                >
                  {t("Edit")}
                </Button>
                <Button
                  ariaLabel={t('Delete question "{question}"', {
                    question: q.text,
                  })}
                  disabled={!editable || deleting !== null}
                  disabledReason={deniedReason}
                  loading={deleting === q.questionId}
                  onClick={() => setConfirming(q)}
                >
                  {t("Delete")}
                </Button>
              </SpaceBetween>
            </SpaceBetween>
          </Container>
        ),
      )}
      {editable && (
        <Container>
          <QuestionForm
            editing={adding}
            saving={saving && editing === null}
            onChange={setAdding}
            onSave={() => void save(adding, () => setAdding(EMPTY))}
          />
        </Container>
      )}
      {confirming && (
        <Modal
          visible
          onDismiss={() => setConfirming(null)}
          header={t("Delete question?")}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => setConfirming(null)}>
                  {t("Cancel")}
                </Button>
                <Button
                  variant="primary"
                  onClick={() => void remove(confirming)}
                >
                  {t("Delete")}
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="s">
            <Box fontWeight="bold">{confirming.text}</Box>
            <Box>
              {confirming.lastCheck
                ? t(
                    "It is removed, with its last check, for every draft of this namespace and cannot be undone.",
                  )
                : t(
                    "It is removed for every draft of this namespace and cannot be undone.",
                  )}
            </Box>
          </SpaceBetween>
        </Modal>
      )}
    </SpaceBetween>
  );
}
