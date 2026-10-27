// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Inspector: the selected class or property of the draft, edited with forms.
 * Saving a form sends the matching typed op through `onApply`.
 */
import React, { useEffect, useRef, useState } from "react";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Textarea from "@cloudscape-design/components/textarea";
import type { ApiClient } from "@components/ApiClientProvider";
import {
  getDomainInstance,
  listDomainInstances,
  type DomainInstance,
  type DomainInstanceSummary,
} from "../../../services/ontology-engine";
import type {
  StudioAboxState,
  StudioOp,
  StudioTerm,
  StudioTermHit,
  StudioTermKind,
  StudioTermRef,
} from "../../../services/studio";
import { useAppI18n, type AppTranslate } from "../../../i18n/LocaleProvider";
import { sourceFileName } from "../abox/abox-helpers";
import { DATATYPES, iriTail, sqlName } from "./studio-helpers";
import { TermPicker } from "./TermPicker";

const XSD = "http://www.w3.org/2001/XMLSchema#";
function matchKinds(t: AppTranslate): SelectProps.Option[] {
  return [
    { value: "exact", label: t("Exact match") },
    { value: "close", label: t("Close match") },
    { value: "none", label: t("No grounding") },
  ];
}

function kindLabel(kind: StudioTermKind, t: AppTranslate): string {
  switch (kind) {
    case "class":
      return t("Class");
    case "objectProperty":
      return t("Relationship");
    case "datatypeProperty":
      return t("Attribute");
  }
}
const EXAMPLE_LIMIT = 5;
const EVIDENCE_SHOWN = 3;
const EVIDENCE_CHARS = 300;

export interface InspectorProps {
  term: StudioTerm | null;
  loading: boolean;
  /** The draft is editable and the term is in the namespace's IRI space. */
  editable: boolean;
  /** Why the caller's role may not change the draft: set, it says so instead of why the term is read-only. */
  deniedReason?: string;
  busy: boolean;
  apiClient: ApiClient;
  namespace: string;
  /** The graph's `abox`: when its `current` is false the domain data shown here is out of date. */
  abox?: StudioAboxState | null;
  /** Resolves true when the change was applied, false when it was not (the caller says why). */
  onApply: (ops: StudioOp[]) => Promise<boolean>;
  search: (q: string, kind?: StudioTermKind) => Promise<StudioTermHit[]>;
  onOpenTerm: (iri: string) => void;
}

/** The form shows the saved value, compared as the forms compare before a save. */
function showsSaved<T>(value: T, saved: T): boolean {
  return typeof value === "string" && typeof saved === "string"
    ? value.trim() === saved.trim()
    : Object.is(value, saved);
}

/**
 * A form value that follows `source`, the saved value, while the form shows
 * it, and keeps what the user typed otherwise: a newer saved value (a save,
 * an undo, a change made elsewhere) never replaces text still being edited.
 */
function useEdited<T>(source: T): [T, (value: T) => void] {
  const [state, setState] = useState({ source, value: source });
  if (!Object.is(state.source, source)) {
    const value = showsSaved(state.value, state.source) ? source : state.value;
    setState({ source, value });
    return [value, (next) => setState({ source, value: next })];
  }
  return [state.value, (value) => setState({ source, value })];
}

/** A blank node's id has no scheme; every IRI, even a malformed "xsd:datetime", has one. */
function isAnonymous(iri: string): boolean {
  return !/^[A-Za-z][A-Za-z0-9+.-]*:/.test(iri);
}

function datatypeOption(iri: string): SelectProps.Option {
  return {
    value: iri,
    label: iri.startsWith(XSD) ? `xsd:${iri.slice(XSD.length)}` : iriTail(iri),
  };
}

/** A term the draft declares opens on click; one it only names (a reference ontology's, an XSD type) is text. */
function TermRefView({
  term,
  onOpenTerm,
}: {
  term: StudioTermRef;
  onOpenTerm: (iri: string) => void;
}) {
  return term.declared ? (
    <Button variant="inline-link" onClick={() => onOpenTerm(term.iri)}>
      {term.label}
    </Button>
  ) : (
    <span title={term.iri}>{term.label}</span>
  );
}

/**
 * A class picker that empties itself once its pick was applied, or at once
 * with `emptyOnPick` (a box that adds, such as Add parent: a refused pick
 * left in it would only be refused again).
 */
function ClassPicker({
  ariaLabel,
  placeholder,
  disabled,
  search,
  onPick,
  emptyOnPick = false,
}: {
  ariaLabel: string;
  placeholder: string;
  disabled: boolean;
  search: InspectorProps["search"];
  onPick: (hit: StudioTermHit) => Promise<boolean>;
  emptyOnPick?: boolean;
}) {
  const [resets, setResets] = useState(0);
  return (
    <TermPicker
      key={resets}
      ariaLabel={ariaLabel}
      placeholder={placeholder}
      kind="class"
      disabled={disabled}
      search={search}
      onPick={async (hit) => {
        if (emptyOnPick) setResets((n) => n + 1);
        if ((await onPick(hit)) && !emptyOnPick) setResets((n) => n + 1);
      }}
    />
  );
}

function DomainExamples({
  apiClient,
  namespace,
  classIri,
}: {
  apiClient: ApiClient;
  namespace: string;
  classIri: string;
}) {
  const { t } = useAppI18n();
  const [rows, setRows] = useState<DomainInstanceSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<DomainInstance | null>(null);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);
  // Bumped by every click on an evidence link: only the latest answer is used.
  const evidenceRequest = useRef(0);
  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const r = await listDomainInstances(apiClient, namespace, {
          classIri,
          maxResults: EXAMPLE_LIMIT,
        });
        if (live) setRows(r.instances);
      } catch (e) {
        if (live) setError(e instanceof Error ? e.message : String(e));
      }
    };
    void load();
    return () => {
      live = false;
    };
  }, [apiClient, namespace, classIri]);

  async function toggleEvidence(iri: string) {
    const request = ++evidenceRequest.current;
    setEvidenceError(null);
    if (open?.iri === iri) {
      setOpen(null);
      return;
    }
    try {
      const detail = await getDomainInstance(apiClient, namespace, iri);
      if (request === evidenceRequest.current) setOpen(detail);
    } catch (e) {
      if (request === evidenceRequest.current) {
        setEvidenceError((e as Error).message);
      }
    }
  }

  if (error) return <Box color="text-status-error">{error}</Box>;
  if (!rows) return <Spinner />;
  if (rows.length === 0) {
    return <Box color="text-body-secondary">{t("None")}</Box>;
  }
  return (
    <SpaceBetween size="xs">
      <ul style={{ margin: 0, paddingInlineStart: 18 }}>
        {rows.map((r) => (
          <li key={r.iri}>
            {r.label}{" "}
            <Button
              variant="inline-link"
              ariaLabel={t("Evidence for {label}", { label: r.label })}
              ariaExpanded={open?.iri === r.iri}
              onClick={() => void toggleEvidence(r.iri)}
            >
              {t("{count} evidence", { count: r.evidenceCount })}
            </Button>
            {open?.iri === r.iri &&
              (open.evidence.length === 0 ? (
                <Box color="text-body-secondary">{t("No evidence")}</Box>
              ) : (
                <ul style={{ margin: 0, paddingInlineStart: 18 }}>
                  {open.evidence.slice(0, EVIDENCE_SHOWN).map((ev) => (
                    <li key={ev.chunkId}>
                      <Box variant="small" display="inline">
                        {sourceFileName(ev.sourceKey) || ev.chunkId}:
                      </Box>{" "}
                      {ev.text.length > EVIDENCE_CHARS
                        ? `${ev.text.slice(0, EVIDENCE_CHARS)}…`
                        : ev.text}
                    </li>
                  ))}
                </ul>
              ))}
          </li>
        ))}
      </ul>
      {evidenceError && <Box color="text-status-error">{evidenceError}</Box>}
    </SpaceBetween>
  );
}

export function Inspector(props: InspectorProps) {
  const { term, loading } = props;
  const { t } = useAppI18n();
  if (loading) return <Spinner size="large" />;
  if (!term) {
    return (
      <Box color="text-body-secondary" padding="m">
        {t("Select a class in the graph to inspect and edit it.")}
      </Box>
    );
  }
  // One set of forms per term: what was typed for one term never reaches another.
  return <TermForms key={term.iri} {...props} term={term} />;
}

function TermForms({
  term,
  editable,
  deniedReason,
  busy,
  apiClient,
  namespace,
  abox,
  onApply,
  search,
  onOpenTerm,
}: InspectorProps & { term: StudioTerm }) {
  const { t } = useAppI18n();
  // The label the form edits is the one the term shows, in its own language.
  const labelLang = term.labels.find((l) => l.value === term.label)?.lang;
  const grounding = term.matches?.[0];
  const currentRange = term.range?.[0]?.iri;
  // A range outside the offered datatypes (xsd:float, rdfs:Literal) is still the selected one.
  const rangeOptions =
    currentRange && !DATATYPES.some((d) => d.value === currentRange)
      ? [datatypeOption(currentRange), ...DATATYPES]
      : DATATYPES;

  const [label, setLabel] = useEdited(term.label);
  const [comment, setComment] = useEdited(term.comment);
  const [newIri, setNewIri] = useEdited(term.iri);
  const [matchKind, setMatchKind] = useEdited<string>(
    grounding?.kind ?? "none",
  );
  const [matchTarget, setMatchTarget] = useEdited(grounding?.target ?? "");
  const [rangeIri, setRangeIri] = useEdited<string | null>(
    currentRange ?? null,
  );
  const [attrLabel, setAttrLabel] = useState("");
  const [attrType, setAttrType] = useState(DATATYPES[0].value);
  const disabled = !editable || busy;
  // A mapped attribute's datatype is its column's: the engine refuses another one.
  const mappedColumn =
    term.kind === "datatypeProperty"
      ? term.mappings.find((m) => m.column)?.column
      : undefined;
  const groundingKinds = matchKinds(t);
  const isClass = term.kind === "class";
  const groundingUnchanged =
    matchKind === (grounding?.kind ?? "none") &&
    (matchKind === "none" || matchTarget.trim() === grounding?.target);

  return (
    <Container
      header={
        <Header
          variant="h2"
          description={term.iri}
          actions={
            !isClass && (
              <Button
                disabled={disabled}
                onClick={() =>
                  void onApply([{ op: "delete_property", iri: term.iri }])
                }
              >
                {t("Delete property")}
              </Button>
            )
          }
        >
          {term.label} <Badge>{kindLabel(term.kind, t)}</Badge>
        </Header>
      }
    >
      <SpaceBetween size="l">
        {!editable && (
          <Box color="text-body-secondary">
            {deniedReason ??
              t(
                "This term is read-only here: the draft cannot change, or the term belongs to another ontology.",
              )}
          </Box>
        )}
        <FormField
          label={t("Label")}
          description={
            labelLang ? t("Language: {lang}", { lang: labelLang }) : undefined
          }
        >
          <SpaceBetween direction="horizontal" size="xs">
            <Input
              ariaLabel={t("Label")}
              value={label}
              disabled={disabled}
              onChange={({ detail }) => setLabel(detail.value)}
            />
            <Button
              disabled={
                disabled || !label.trim() || label.trim() === term.label
              }
              onClick={() =>
                void onApply([
                  {
                    op: "set_label",
                    iri: term.iri,
                    label: label.trim(),
                    ...(labelLang ? { lang: labelLang } : {}),
                  },
                ])
              }
            >
              {t("Save label")}
            </Button>
          </SpaceBetween>
        </FormField>
        <FormField label={t("Description")}>
          <SpaceBetween size="xs">
            <Textarea
              ariaLabel={t("Description")}
              value={comment}
              rows={3}
              disabled={disabled}
              onChange={({ detail }) => setComment(detail.value)}
            />
            <Button
              disabled={disabled || comment.trim() === term.comment.trim()}
              onClick={() =>
                void onApply([
                  {
                    op: "set_comment",
                    iri: term.iri,
                    comment: comment.trim(),
                  },
                ])
              }
            >
              {t("Save description")}
            </Button>
          </SpaceBetween>
        </FormField>
        <ExpandableSection headerText={t("Change IRI")}>
          <SpaceBetween direction="horizontal" size="xs">
            <Input
              ariaLabel={t("New IRI")}
              value={newIri}
              disabled={disabled}
              onChange={({ detail }) => setNewIri(detail.value)}
            />
            <Button
              disabled={
                disabled || !newIri.trim() || newIri.trim() === term.iri
              }
              onClick={() =>
                void onApply([
                  { op: "change_iri", iri: term.iri, newIri: newIri.trim() },
                ])
              }
            >
              {t("Change IRI")}
            </Button>
          </SpaceBetween>
        </ExpandableSection>

        {isClass && (
          <FormField label={t("Parents")}>
            <SpaceBetween size="xs">
              {(term.parents ?? []).length === 0 && (
                <Box color="text-body-secondary">{t("None")}</Box>
              )}
              {(term.parents ?? []).map((p) => (
                <SpaceBetween key={p.iri} direction="horizontal" size="xs">
                  <TermRefView term={p} onOpenTerm={onOpenTerm} />
                  <Button
                    variant="icon"
                    iconName="close"
                    ariaLabel={t("Remove parent {label}", { label: p.label })}
                    disabled={disabled}
                    onClick={() =>
                      void onApply([
                        { op: "remove_parent", iri: term.iri, parent: p.iri },
                      ])
                    }
                  />
                </SpaceBetween>
              ))}
              <ClassPicker
                ariaLabel={t("Add parent")}
                placeholder={t("Add a parent class…")}
                disabled={disabled}
                emptyOnPick
                search={search}
                onPick={(hit) =>
                  onApply([
                    { op: "add_parent", iri: term.iri, parent: hit.iri },
                  ])
                }
              />
            </SpaceBetween>
          </FormField>
        )}

        {isClass && (term.children ?? []).length > 0 && (
          <ExpandableSection
            headerText={t("Subclasses ({count})", {
              count: (term.children ?? []).length,
            })}
          >
            <SpaceBetween size="xs">
              {(term.children ?? []).map((c) => (
                <TermRefView key={c.iri} term={c} onOpenTerm={onOpenTerm} />
              ))}
            </SpaceBetween>
          </ExpandableSection>
        )}

        {isClass && (
          <FormField label={t("Relationships and attributes")}>
            <SpaceBetween size="xs">
              {(term.properties ?? []).map((p) => (
                <SpaceBetween key={p.iri} direction="horizontal" size="xs">
                  <TermRefView
                    term={{ iri: p.iri, label: p.label, declared: true }}
                    onOpenTerm={onOpenTerm}
                  />
                  <Box variant="small" display="inline">
                    {p.direction === "outgoing" ? t("to") : t("from")}{" "}
                    {p.other !== null && isAnonymous(p.other) ? (
                      p.kind === "datatypeProperty" &&
                      p.direction === "outgoing" ? (
                        t("(anonymous datatype)")
                      ) : (
                        t("(anonymous class)")
                      )
                    ) : p.other !== null ? (
                      <TermRefView
                        term={{
                          iri: p.other,
                          label: p.otherLabel ?? iriTail(p.other),
                          declared: p.otherDeclared,
                        }}
                        onOpenTerm={onOpenTerm}
                      />
                    ) : (
                      t("open")
                    )}
                  </Box>
                  <Button
                    variant="icon"
                    iconName="remove"
                    ariaLabel={t("Delete property {label}", { label: p.label })}
                    disabled={disabled}
                    onClick={() =>
                      void onApply([{ op: "delete_property", iri: p.iri }])
                    }
                  />
                </SpaceBetween>
              ))}
              <SpaceBetween direction="horizontal" size="xs">
                <Input
                  ariaLabel={t("New attribute label")}
                  placeholder={t("New attribute")}
                  value={attrLabel}
                  disabled={disabled}
                  onChange={({ detail }) => setAttrLabel(detail.value)}
                />
                <Select
                  ariaLabel={t("New attribute datatype")}
                  selectedOption={
                    DATATYPES.find((d) => d.value === attrType) ?? null
                  }
                  disabled={disabled}
                  options={DATATYPES}
                  onChange={({ detail }) =>
                    setAttrType(detail.selectedOption.value ?? attrType)
                  }
                />
                <Button
                  disabled={disabled || !attrLabel.trim()}
                  onClick={async () => {
                    const ok = await onApply([
                      {
                        op: "add_datatype_property",
                        label: attrLabel.trim(),
                        domain: term.iri,
                        range: attrType,
                      },
                    ]);
                    if (ok) setAttrLabel("");
                  }}
                >
                  {t("Add attribute")}
                </Button>
              </SpaceBetween>
            </SpaceBetween>
          </FormField>
        )}

        {isClass && (
          <FormField
            label={t("Grounding")}
            description={t(
              "The reference-ontology class this class stands for.",
            )}
          >
            <SpaceBetween direction="horizontal" size="xs">
              <Select
                ariaLabel={t("Grounding kind")}
                selectedOption={
                  groundingKinds.find((k) => k.value === matchKind) ?? null
                }
                disabled={disabled}
                options={groundingKinds}
                onChange={({ detail }) =>
                  setMatchKind(detail.selectedOption.value ?? "none")
                }
              />
              <Input
                ariaLabel={t("Grounding target")}
                placeholder="https://…"
                value={matchTarget}
                disabled={disabled || matchKind === "none"}
                onChange={({ detail }) => setMatchTarget(detail.value)}
              />
              <Button
                disabled={
                  disabled ||
                  groundingUnchanged ||
                  (matchKind !== "none" && !matchTarget.trim())
                }
                onClick={() =>
                  void onApply([
                    matchKind === "exact" || matchKind === "close"
                      ? {
                          op: "set_match",
                          iri: term.iri,
                          kind: matchKind,
                          target: matchTarget.trim(),
                        }
                      : { op: "set_match", iri: term.iri, kind: "none" },
                  ])
                }
              >
                {t("Save grounding")}
              </Button>
            </SpaceBetween>
          </FormField>
        )}

        {!isClass && (
          <FormField label={t("Domain")}>
            <SpaceBetween size="xs">
              {(term.domain ?? []).map((d) => (
                <TermRefView key={d.iri} term={d} onOpenTerm={onOpenTerm} />
              ))}
              <ClassPicker
                ariaLabel={t("Change domain")}
                placeholder={t("Change the domain class…")}
                disabled={disabled}
                search={search}
                onPick={(hit) =>
                  onApply([
                    { op: "update_property", iri: term.iri, domain: hit.iri },
                  ])
                }
              />
            </SpaceBetween>
          </FormField>
        )}

        {term.kind === "objectProperty" && (
          <FormField label={t("Range")}>
            <SpaceBetween size="xs">
              {(term.range ?? []).map((r) => (
                <TermRefView key={r.iri} term={r} onOpenTerm={onOpenTerm} />
              ))}
              <ClassPicker
                ariaLabel={t("Change range")}
                placeholder={t("Change the range class…")}
                disabled={disabled}
                search={search}
                onPick={(hit) =>
                  onApply([
                    { op: "update_property", iri: term.iri, range: hit.iri },
                  ])
                }
              />
            </SpaceBetween>
          </FormField>
        )}

        {term.kind === "datatypeProperty" && (
          <FormField
            label={t("Datatype")}
            description={
              mappedColumn
                ? t("The datatype follows column {column}.", {
                    column: sqlName(mappedColumn),
                  })
                : undefined
            }
          >
            <SpaceBetween direction="horizontal" size="xs">
              <Select
                ariaLabel={t("Datatype")}
                placeholder={t("Choose a datatype")}
                selectedOption={
                  rangeOptions.find((d) => d.value === rangeIri) ?? null
                }
                disabled={disabled || !!mappedColumn}
                options={rangeOptions}
                onChange={({ detail }) =>
                  setRangeIri(detail.selectedOption.value ?? null)
                }
              />
              <Button
                disabled={
                  disabled ||
                  !!mappedColumn ||
                  !rangeIri ||
                  rangeIri === currentRange
                }
                onClick={() =>
                  rangeIri &&
                  void onApply([
                    { op: "update_property", iri: term.iri, range: rangeIri },
                  ])
                }
              >
                {t("Change datatype")}
              </Button>
            </SpaceBetween>
          </FormField>
        )}

        <FormField label={t("Mappings")}>
          {term.mappings.length === 0 ? (
            <SpaceBetween size="xxs">
              <Box color="text-body-secondary">
                {t("Not mapped to a table")}
              </Box>
              <Box variant="small" color="text-body-secondary">
                {/* As the Checks count it: a class above a mapped class is answered through it. */}
                {isClass && (term.children ?? []).length > 0
                  ? t(
                      "Structured queries can use this term only through a subclass mapped to a table.",
                    )
                  : t("Structured queries cannot use this term.")}
              </Box>
            </SpaceBetween>
          ) : (
            <ul style={{ margin: 0, paddingInlineStart: 18 }}>
              {term.mappings.map((m, i) => (
                <li key={`${m.triplesMap}-${i}`}>
                  {[
                    m.triplesMap ? iriTail(m.triplesMap) : t("Mapping"),
                    m.table &&
                      t("(table {table})", { table: sqlName(m.table) }),
                    m.column &&
                      t("(column {column})", { column: sqlName(m.column) }),
                    m.parentTriplesMap &&
                      t("(references {map})", {
                        map: iriTail(m.parentTriplesMap),
                      }),
                  ]
                    .filter(Boolean)
                    .join(" ")}
                </li>
              ))}
            </ul>
          )}
        </FormField>

        {isClass && term.abox && (
          <FormField label={t("Domain data")}>
            {term.abox.afterDeploy ? (
              <Box color="text-body-secondary">
                {t(
                  "Filled after deploy: the active version has no such class yet.",
                )}
              </Box>
            ) : (
              <SpaceBetween size="xs">
                <Box>
                  {t(
                    (term.abox.aboxCount ?? 0) === 1
                      ? "{count} instance in the active version"
                      : "{count} instances in the active version",
                    { count: term.abox.aboxCount ?? 0 },
                  )}
                </Box>
                {abox?.current === false && (
                  <StatusIndicator type="warning">
                    {abox.reason
                      ? t("Out of date: {reason}", { reason: abox.reason })
                      : t("Out of date")}
                  </StatusIndicator>
                )}
                {(term.abox.aboxCount ?? 0) > 0 && (
                  <DomainExamples
                    // Another build of the domain data: read the examples again.
                    key={`${abox?.conformsToVersion ?? ""}:${term.abox.aboxCount}`}
                    apiClient={apiClient}
                    namespace={namespace}
                    classIri={term.iri}
                  />
                )}
              </SpaceBetween>
            )}
          </FormField>
        )}
      </SpaceBetween>
    </Container>
  );
}
