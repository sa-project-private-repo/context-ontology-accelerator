// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The Query tab of the Ontology designer: read-only queries on the draft and
 * the instances from documents (SPARQL), on the documents' lexical graph
 * (Cypher), or on the database through the namespace's VKG (SPARQL, with the
 * caller's own grants). At most 1,000 rows come back within 20 seconds.
 */
import React, { useEffect, useRef, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import FormField from "@cloudscape-design/components/form-field";
import Link from "@cloudscape-design/components/link";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import type { ApiClient } from "@components/ApiClientProvider";
import {
  runSparqlQuery,
  runStudioQuery,
  type StudioQueryCell,
  type StudioQueryLanguage,
  type StudioQueryTarget,
} from "../../../services/studio";
import type { AppMessage } from "../../../i18n/appMessages";
import { useAppI18n } from "../../../i18n/LocaleProvider";
import {
  errorMessage,
  iriTail,
  MAX_REQUEST_BYTES,
  requestBytes,
} from "./studio-helpers";

/** What a console query reads. */
export type ConsoleTarget = "draft" | "lexical" | "vkg";

/** A query to open in the console (from the chat or a question). */
export interface ConsoleQuery {
  language: StudioQueryLanguage;
  target: StudioQueryTarget | "vkg";
  query: string;
}

/** The longest query the engine takes. */
export const MAX_QUERY = 20000;

const TARGETS: { value: ConsoleTarget; label: AppMessage }[] = [
  { value: "draft", label: "Draft + documents (SPARQL)" },
  { value: "lexical", label: "Lexical graph (Cypher)" },
  { value: "vkg", label: "Database via VKG (SPARQL)" },
];

/** The lexical graph's tenant labels carry the namespace id without hyphens, cut to 25 characters. */
function tenantOf(namespace: string): string {
  return namespace.replace(/-/g, "").slice(0, 25);
}

export function sampleQuery(target: ConsoleTarget, namespace: string): string {
  switch (target) {
    case "draft":
      return [
        "PREFIX owl: <http://www.w3.org/2002/07/owl#>",
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>",
        "SELECT ?class ?label WHERE {",
        "  ?class a owl:Class ;",
        "         rdfs:label ?label .",
        "}",
        "ORDER BY ?label",
        "LIMIT 50",
      ].join("\n");
    case "lexical":
      return [
        `MATCH (e:\`__Entity__${tenantOf(namespace)}__\`)`,
        "RETURN e.value AS entity, e.class AS class",
        "LIMIT 50",
      ].join("\n");
    case "vkg":
      return ["SELECT ?s ?p ?o WHERE {", "  ?s ?p ?o .", "}", "LIMIT 20"].join(
        "\n",
      );
  }
}

/** One row of the results table. */
interface ResultRow {
  index: number;
  cells: (StudioQueryCell | null)[];
}

/** A result the table shows: cells of either kind, by column. */
interface Shown {
  target: ConsoleTarget;
  columns: string[];
  rows: (StudioQueryCell | null)[][];
  rowCount: number;
  truncated: boolean;
  elapsedMs: number | null;
  revision: number | null;
  executedSql: string | null;
}

function plainCell(value: unknown): StudioQueryCell | null {
  if (value === null || value === undefined) return null;
  return {
    type: "value",
    value: typeof value === "string" ? value : JSON.stringify(value),
  };
}

function Cell({
  cell,
  onOpenTerm,
}: {
  cell: StudioQueryCell | null;
  onOpenTerm: (iri: string) => void;
}) {
  if (cell === null) return <Box color="text-body-secondary">—</Box>;
  if (cell.type === "uri")
    return (
      <Link
        onFollow={(e) => {
          e.preventDefault();
          onOpenTerm(cell.value);
        }}
      >
        <span title={cell.value}>{iriTail(cell.value)}</span>
      </Link>
    );
  const hint = cell.lang
    ? `@${cell.lang}`
    : cell.datatype
      ? iriTail(cell.datatype)
      : "";
  return (
    <span title={hint || undefined} style={{ whiteSpace: "pre-wrap" }}>
      {cell.type === "bnode" ? `_:${cell.value}` : cell.value}
    </span>
  );
}

export interface QueryConsoleProps {
  apiClient: ApiClient;
  namespace: string;
  proposalId: string;
  /** The draft is under review: only then does its chat take a message. */
  editable: boolean;
  /** Why the caller's role may not message the assistant: set, Ask the assistant says so. */
  deniedReason?: string;
  /**
   * Why the draft cannot be queried, set while its accept runs and once its
   * proposal is no longer under review: the engine then refuses a draft query
   * (409 ProposalNotEditable), and drops the draft's graph when the review
   * ends. The Draft + documents target is not offered, and the console starts
   * on the VKG.
   */
  draftClosedReason?: string;
  /** A query to open; taken once, then `onPrefillTaken` is called. */
  prefill: ConsoleQuery | null;
  onPrefillTaken: () => void;
  onOpenTerm: (iri: string) => void;
  /** Prefill the chat with a message and switch to it. */
  onAsk: (text: string) => void;
}

function targetOf(q: ConsoleQuery): ConsoleTarget {
  return q.target === "vkg"
    ? "vkg"
    : q.language === "cypher"
      ? "lexical"
      : "draft";
}

export function QueryConsole({
  apiClient,
  namespace,
  proposalId,
  editable,
  deniedReason,
  draftClosedReason,
  prefill,
  onPrefillTaken,
  onOpenTerm,
  onAsk,
}: QueryConsoleProps) {
  const { t } = useAppI18n();
  const targets = TARGETS.map((o) => ({
    value: o.value,
    label: t(o.label),
    ...(o.value === "draft" && draftClosedReason !== undefined
      ? { disabled: true, disabledReason: draftClosedReason }
      : {}),
  }));
  const start: ConsoleTarget =
    draftClosedReason === undefined ? "draft" : "vkg";
  const [target, setTarget] = useState<ConsoleTarget>(
    prefill ? targetOf(prefill) : start,
  );
  const [text, setText] = useState(
    prefill ? prefill.query : sampleQuery(start, namespace),
  );
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [shown, setShown] = useState<Shown | null>(null);
  // Bumped by each run and each query opened: a run's answer is used only
  // while neither has come since.
  const runs = useRef(0);
  // Unmounted: an answer still on its way is dropped.
  useEffect(
    () => () => {
      runs.current++;
    },
    [],
  );

  useEffect(() => {
    if (!prefill) return;
    // A run still on its way would land its rows, or its error, under the
    // opened query; Run takes the opened one at once.
    runs.current++;
    setRunning(false);
    setTarget(targetOf(prefill));
    setText(prefill.query);
    setError(null);
    // The rows still shown would read as the opened query's.
    setShown(null);
    onPrefillTaken();
  }, [prefill, onPrefillTaken]);

  async function run() {
    const queryText = text.trim();
    if (
      !queryText ||
      running ||
      queryText.length > MAX_QUERY ||
      tooLarge ||
      draftClosed
    )
      return;
    const mine = ++runs.current;
    setRunning(true);
    setError(null);
    try {
      let next: Shown;
      if (target === "vkg") {
        const started = performance.now();
        const r = await runSparqlQuery(apiClient, namespace, queryText);
        next = {
          target,
          columns: r.columns,
          rows: r.rows.map((row) => row.map(plainCell)),
          rowCount: r.rowCount,
          truncated: r.truncated,
          elapsedMs: Math.round(performance.now() - started),
          revision: null,
          executedSql: r.executedSql,
        };
      } else {
        const r = await runStudioQuery(
          apiClient,
          namespace,
          proposalId,
          target === "lexical" ? "cypher" : "sparql",
          target,
          queryText,
        );
        next = {
          target,
          columns: r.columns,
          rows: r.rows,
          rowCount: r.rowCount,
          truncated: r.truncated,
          elapsedMs: r.elapsedMs,
          revision: r.revision,
          executedSql: null,
        };
      }
      if (mine === runs.current) setShown(next);
    } catch (e) {
      if (mine === runs.current) {
        // The rows still shown would read as this query's.
        setShown(null);
        setError(errorMessage(e, t));
      }
    } finally {
      if (mine === runs.current) setRunning(false);
    }
  }

  function ask() {
    const language = target === "lexical" ? "cypher" : "sparql";
    const where = targets.find((o) => o.value === target)?.label ?? target;
    const problem = error ? `\n${t("It fails with: {error}", { error })}` : "";
    onAsk(
      `${t("Help me with this query on {where}:", { where })}\n\`\`\`${language}\n${text.trim()}\n\`\`\`${problem}`,
    );
  }

  const tooLong = text.length > MAX_QUERY;
  // A draft query handed over from the chat or a question, or the target the
  // console was on when the accept started or the review ended: shown, but
  // not run.
  const draftClosed = target === "draft" && draftClosedReason !== undefined;
  // Measured as runStudioQuery and runSparqlQuery send it: past
  // MAX_REQUEST_BYTES the WAF would answer a bare 403.
  const tooLarge =
    !tooLong &&
    requestBytes(
      target === "vkg"
        ? { sparql: text.trim() }
        : {
            language: target === "lexical" ? "cypher" : "sparql",
            target,
            query: text.trim(),
          },
    ) > MAX_REQUEST_BYTES;
  const items: ResultRow[] = (shown?.rows ?? []).map((cells, index) => ({
    index,
    cells,
  }));
  return (
    <SpaceBetween size="s">
      <FormField label={t("Query")}>
        <Select
          selectedOption={targets.find((o) => o.value === target) ?? null}
          options={targets}
          onChange={({ detail }) => {
            const next = detail.selectedOption.value as ConsoleTarget;
            if (next === target) return;
            // The rows shown, and a run still on its way, read the other
            // target: under this one they would read as its answer.
            runs.current++;
            setRunning(false);
            setError(null);
            setShown(null);
            setTarget(next);
          }}
          ariaLabel={t("What the query reads")}
        />
      </FormField>
      <textarea
        aria-label={t("Query text")}
        value={text}
        rows={10}
        spellCheck={false}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void run();
        }}
        style={{
          width: "100%",
          boxSizing: "border-box",
          fontFamily: "Monaco, Menlo, Consolas, monospace",
          fontSize: 12,
          padding: 8,
          borderRadius: 8,
          border:
            tooLong || tooLarge ? "2px solid #d91515" : "2px solid #8c8c94",
          resize: "vertical",
        }}
      />
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        <Button
          variant="primary"
          loading={running}
          disabled={!text.trim() || tooLong || tooLarge || draftClosed}
          disabledReason={draftClosed ? draftClosedReason : undefined}
          onClick={() => void run()}
        >
          {t("Run")}
        </Button>
        <Button onClick={() => setText(sampleQuery(target, namespace))}>
          {t("Insert sample")}
        </Button>
        <Button
          iconName="contact"
          disabled={!editable || !text.trim()}
          disabledReason={deniedReason}
          onClick={ask}
        >
          {t("Ask the assistant")}
        </Button>
        {tooLong && (
          <Box variant="small" color="text-status-error">
            {t("A query is at most {max} characters.", { max: MAX_QUERY })}
          </Box>
        )}
        {tooLarge && (
          <Box variant="small" color="text-status-error">
            {t(
              "A query is at most 8 KB; Japanese, Korean and other non-Latin characters take up to 3 bytes each.",
            )}
          </Box>
        )}
      </SpaceBetween>
      {target === "vkg" && (
        <Box variant="small" color="text-body-secondary">
          {t(
            "Runs on the database with your own grants; the VKG maps the active version, not the draft.",
          )}
        </Box>
      )}
      {error && <Alert type="error">{error}</Alert>}
      {shown && (
        <SpaceBetween size="xs">
          <Box variant="small" color="text-body-secondary">
            {[
              t(shown.rowCount === 1 ? "{count} row" : "{count} rows", {
                count: shown.rowCount,
              }),
              shown.elapsedMs !== null && t("{ms} ms", { ms: shown.elapsedMs }),
              shown.revision !== null &&
                t("revision {revision}", { revision: shown.revision }),
              shown.truncated &&
                t("only the first rows are shown; add a LIMIT"),
            ]
              .filter(Boolean)
              .join(", ")}
          </Box>
          <Table<ResultRow>
            variant="embedded"
            wrapLines
            items={items}
            trackBy="index"
            empty={<Box color="text-body-secondary">{t("No rows.")}</Box>}
            columnDefinitions={shown.columns.map((column, n) => ({
              id: `${n}`,
              header: column,
              cell: (item) => (
                <Cell cell={item.cells[n] ?? null} onOpenTerm={onOpenTerm} />
              ),
            }))}
          />
          {shown.executedSql && (
            <ExpandableSection headerText={t("SQL that ran")}>
              <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>
                {shown.executedSql}
              </pre>
            </ExpandableSection>
          )}
        </SpaceBetween>
      )}
    </SpaceBetween>
  );
}
