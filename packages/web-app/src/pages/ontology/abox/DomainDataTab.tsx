// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Domain data tab: the namespace's domain ABox (the typed instances, relations
 * and evidence extracted from its documents), its build state, and the
 * mappings from the strings found in the documents to ontology terms.
 */
import React, { useEffect, useRef, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Autosuggest, {
  type AutosuggestProps,
} from "@cloudscape-design/components/autosuggest";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Modal from "@cloudscape-design/components/modal";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator, {
  type StatusIndicatorProps,
} from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import TextFilter from "@cloudscape-design/components/text-filter";
import { useCanManageOntology } from "@api-hooks/use-can-manage-ontology";
import { formatTimestamp } from "@utils/helpers";
import { ApiError, type ApiClient } from "@components/ApiClientProvider";
import type { AppMessage } from "../../../i18n/appMessages";
import { useAppI18n, type AppTranslate } from "../../../i18n/LocaleProvider";
import {
  getDomainAbox,
  getDomainInstance,
  listAboxMappings,
  listDomainInstances,
  rebuildDomainAbox,
  resetAboxMapping,
  searchEntities,
  updateAboxMapping,
  type AboxMapping,
  type DomainAbox,
  type DomainInstance,
  type DomainInstanceSummary,
} from "../../../services/ontology-engine";
import {
  ABOX_MAPPING_KINDS,
  ABOX_MAPPING_STATUSES,
  type AboxMappingDirection,
  type AboxMappingKind,
  type AboxMappingStatus,
} from "../../../services/abox-types";
import {
  aboxStatusDisplay,
  iriTail,
  isOutOfDate,
  isPolling,
  mappingKeyLabel,
  mappingTargetLabel,
  sourceFileName,
  targetSearchKind,
  versionLine,
} from "./abox-helpers";

const POLL_INTERVAL_MS = 3000;
/**
 * Failed state fetches in a row after which the last answer (a running or
 * queued build) stops driving the poll; the next success resumes it.
 */
const MAX_FAILED_POLLS = 4;
/**
 * Polls to keep making, beyond those a running or queued build drives, after
 * this view queues a rebuild (Rebuild, a mapping save or reset). The request is
 * marked pending before the call returns, but once the worker takes it the
 * state reads READY with nothing pending until the build marks itself
 * BUILDING. These polls bridge that gap, also for a build chained after the
 * one running when the request was made.
 */
const TRAILING_POLLS = 2;
const PAGE_SIZE = 50;
/** Pause in typing after which the instance label filter runs. */
const FILTER_DELAY_MS = 400;
const SEARCH_LIMIT = 15;
const KIND_LABELS: Record<AboxMappingKind, AppMessage> = {
  class: "Classes",
  relation: "Relations",
  attribute: "Attributes",
};
const NO_MAPPINGS: Record<AboxMappingKind, AppMessage> = {
  class: "No class mappings yet.",
  relation: "No relation mappings yet.",
  attribute: "No attribute mappings yet.",
};
/** What the change dialog searches for, per row kind. */
const TARGET_SEARCH: Record<
  AboxMappingKind,
  { description: AppMessage; placeholder: AppMessage }
> = {
  class: {
    description: "Search the ontology's classes.",
    placeholder: "Type to search classes…",
  },
  relation: {
    description: "Search the ontology's object properties.",
    placeholder: "Type to search object properties…",
  },
  attribute: {
    description: "Search the ontology's datatype properties.",
    placeholder: "Type to search datatype properties…",
  },
};
const ORIGIN_LABELS: Record<AboxMapping["origin"], AppMessage> = {
  auto: "auto",
  manual: "manual",
};
const MAPPING_STATUS: Record<
  AboxMappingStatus,
  { type: StatusIndicatorProps["type"]; label: AppMessage }
> = {
  active: { type: "success", label: "Active" },
  stale: { type: "warning", label: "Stale" },
  ignored: { type: "stopped", label: "Ignored" },
};

interface ChangeDialog {
  /** Distinguishes dialogs; late answers for an older one are dropped. */
  request: number;
  row: AboxMapping;
  keyLabel: string;
  text: string;
  /** The term IRI picked from the suggestions; Save needs one. */
  target: string | null;
  /**
   * Picked in the dialog, never taken from the row: whether a direction fits
   * depends on the property's domain and range. A relation's Save needs one.
   */
  direction: AboxMappingDirection | null;
  options: AutosuggestProps.Option[];
  searching: boolean;
  searchError: string | null;
  busy: boolean;
  error: string | null;
}

/** The engine's `detail` for a refused call, else the error's own message. */
function errorMessage(e: unknown): string {
  const detail =
    e instanceof ApiError
      ? (e.body as { detail?: unknown } | null | undefined)?.detail
      : undefined;
  return typeof detail === "string" ? detail : (e as Error).message;
}

/** A refused edit after which the listed rows are out of date: the row changed meanwhile (409) or is gone (404). */
function rowsOutOfDate(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 409 || e.status === 404);
}

interface DomainDataTabProps {
  apiClient: ApiClient;
  namespace?: string;
}

/**
 * Keyed by namespace, so switching namespaces starts from scratch: no state,
 * instances, mappings or dialog of the previous namespace survive, and its
 * in-flight answers are dropped.
 */
export function DomainDataTab(props: DomainDataTabProps) {
  return <NamespaceDomainData key={props.namespace ?? ""} {...props} />;
}

function NamespaceDomainData({ apiClient, namespace }: DomainDataTabProps) {
  const { t, locale } = useAppI18n();
  // A role that only reads (a data analyst) is not offered a rebuild or a
  // mapping change the API would refuse it.
  const canChange = useCanManageOntology(namespace);
  // ── Build state ──
  const [abox, setAbox] = useState<DomainAbox | null>(null);
  // Only loads the user waits on (the first one, Refresh) set this; background
  // polls leave the Refresh button alone.
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reload, setReload] = useState({ key: 0, background: false });
  // Bumped each time a state fetch settles; the next poll is scheduled from it.
  const [settled, setSettled] = useState(0);
  const [failedPolls, setFailedPolls] = useState(0);
  const [trailingPolls, setTrailingPolls] = useState(0);
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildError, setRebuildError] = useState<string | null>(null);
  const [listsReload, setListsReload] = useState(0);

  useEffect(() => {
    if (!namespace) return;
    let cancelled = false;
    if (!reload.background) setLoading(true);
    // A load the user waits on (Refresh) re-reads the lists too. Bumped with the
    // state it read, so a build that landed meanwhile does not re-read them twice.
    const refreshLists = () => {
      if (!reload.background) setListsReload((n) => n + 1);
    };
    getDomainAbox(apiClient, namespace)
      .then((state) => {
        if (cancelled) return;
        setAbox(state);
        setLoadError(null);
        setFailedPolls(0);
        refreshLists();
      })
      .catch((e: Error) => {
        if (cancelled) return;
        setLoadError(e.message);
        setFailedPolls((n) => n + 1);
        refreshLists();
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
        setSettled((n) => n + 1);
      });
    return () => {
      cancelled = true;
    };
  }, [apiClient, namespace, reload]);

  // Poll while a build runs or is queued, and for a few polls after this view
  // queued one. Each poll is scheduled only after the previous fetch settled, so
  // a backend slower than the interval still gets its answer applied. Failed
  // fetches keep the last answer, so they bound how long it drives the poll.
  const buildPolling =
    failedPolls < MAX_FAILED_POLLS && isPolling(abox ?? undefined);
  const polling = buildPolling || trailingPolls > 0;
  useEffect(() => {
    if (!namespace || !polling) return;
    const id = window.setTimeout(() => {
      // The trailing budget is spent only on polls no build drives, so it is
      // still there when the request turns into a build chained after this one.
      if (!buildPolling) setTrailingPolls((n) => Math.max(0, n - 1));
      setReload((r) => ({ key: r.key + 1, background: true }));
    }, POLL_INTERVAL_MS);
    return () => window.clearTimeout(id);
  }, [namespace, polling, buildPolling, settled]);

  async function rebuild() {
    if (!namespace) return;
    setRebuilding(true);
    setRebuildError(null);
    try {
      await rebuildDomainAbox(apiClient, namespace);
      setTrailingPolls(TRAILING_POLLS);
    } catch (e) {
      setRebuildError(errorMessage(e));
    } finally {
      setRebuilding(false);
      // Either way: a queued rebuild shows up (and starts polling), a refusal
      // may come from a state this view has not seen yet.
      setReload((r) => ({ key: r.key + 1, background: true }));
    }
  }

  // The instances and mappings are read once the state is known, and re-read
  // when a new build lands (it replaces the instances and can add mapping rows)
  // and on Refresh. A build writes its id when it starts; only the READY write
  // changes builtAt, so that is what marks a landed build.
  const loaded = abox !== null;
  const builtAt = abox?.builtAt ?? null;

  // ── Instances ──
  const [classIri, setClassIri] = useState<string | null>(null);
  const [filterText, setFilterText] = useState("");
  const [query, setQuery] = useState("");
  const [instances, setInstances] = useState<DomainInstanceSummary[]>([]);
  const [nextToken, setNextToken] = useState<string | null>(null);
  const [instancesLoading, setInstancesLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [instancesError, setInstancesError] = useState<string | null>(null);
  // Bumped by every list request; an answer applies only if no later one started.
  const instancesRequest = useRef(0);

  useEffect(() => {
    if (filterText === query) return;
    const id = window.setTimeout(() => setQuery(filterText), FILTER_DELAY_MS);
    return () => window.clearTimeout(id);
  }, [filterText, query]);

  const filters = {
    classIri: classIri ?? undefined,
    q: query.trim() || undefined,
    maxResults: PAGE_SIZE,
  };

  useEffect(() => {
    if (!namespace || !loaded) return;
    const request = ++instancesRequest.current;
    setInstancesLoading(true);
    setLoadingMore(false);
    listDomainInstances(apiClient, namespace, {
      classIri: classIri ?? undefined,
      q: query.trim() || undefined,
      maxResults: PAGE_SIZE,
    })
      .then((list) => {
        if (request !== instancesRequest.current) return;
        setInstances(list.instances);
        setNextToken(list.nextToken ?? null);
        setInstancesError(null);
      })
      .catch((e) => {
        if (request !== instancesRequest.current) return;
        setInstances([]);
        setNextToken(null);
        setInstancesError(errorMessage(e));
      })
      .finally(() => {
        if (request === instancesRequest.current) setInstancesLoading(false);
      });
  }, [apiClient, namespace, loaded, builtAt, listsReload, classIri, query]);

  async function loadMore() {
    if (!namespace || !nextToken) return;
    const request = ++instancesRequest.current;
    setLoadingMore(true);
    try {
      const list = await listDomainInstances(apiClient, namespace, {
        ...filters,
        nextToken,
      });
      if (request !== instancesRequest.current) return;
      setInstances((rows) => [...rows, ...list.instances]);
      setNextToken(list.nextToken ?? null);
      setInstancesError(null);
    } catch (e) {
      if (request !== instancesRequest.current) return;
      setInstancesError(errorMessage(e));
    } finally {
      if (request === instancesRequest.current) setLoadingMore(false);
    }
  }

  // ── Instance detail ──
  const [selected, setSelected] = useState<DomainInstance | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailNotice, setDetailNotice] = useState<string | null>(null);
  const detailRequest = useRef(0);
  // The instance the user last opened, shown or still loading; null once closed.
  const wanted = useRef<{ iri: string; label: string } | null>(null);
  const detailPanel = useRef<HTMLDivElement>(null);
  const noticeRegion = useRef<HTMLDivElement>(null);
  const focusNotice = useRef(false);

  async function openInstance(iri: string, label: string) {
    if (!namespace) return;
    const request = ++detailRequest.current;
    wanted.current = { iri, label };
    setDetailNotice(null);
    try {
      const detail = await getDomainInstance(apiClient, namespace, iri);
      // A later click, Close or a landed build superseded this request.
      if (request !== detailRequest.current) return;
      setSelected(detail);
      setDetailError(null);
    } catch (e) {
      if (request !== detailRequest.current) return;
      wanted.current = null;
      setSelected(null);
      setDetailError(errorMessage(e));
    }
  }

  function closeDetail() {
    detailRequest.current++;
    wanted.current = null;
    setSelected(null);
  }

  // A landed build replaces the instances: re-read the wanted one, even while
  // its first read is in flight (that answer may predate the swap; this read
  // supersedes it). Only a 404 closes the panel; on another error the shown
  // detail stays, with the error.
  const detailBuild = useRef(builtAt);
  useEffect(() => {
    if (detailBuild.current === builtAt) return;
    detailBuild.current = builtAt;
    const want = wanted.current;
    if (!namespace || !want) return;
    const request = ++detailRequest.current;
    getDomainInstance(apiClient, namespace, want.iri)
      .then((detail) => {
        if (request !== detailRequest.current) return;
        setSelected(detail);
        setDetailError(null);
      })
      .catch((e) => {
        if (request !== detailRequest.current) return;
        if (e instanceof ApiError && e.status === 404) {
          // Focus would be lost with the panel; hand it to the notice.
          focusNotice.current = !!detailPanel.current?.contains(
            document.activeElement,
          );
          wanted.current = null;
          setSelected(null);
          setDetailNotice(
            t("{label} is not in the rebuilt domain ABox.", {
              label: want.label,
            }),
          );
        } else {
          setDetailError(errorMessage(e));
        }
      });
  }, [apiClient, namespace, builtAt, t]);

  useEffect(() => {
    if (!detailNotice || !focusNotice.current) return;
    focusNotice.current = false;
    noticeRegion.current?.focus();
  }, [detailNotice]);

  // ── Mappings ──
  const [mappingKind, setMappingKind] = useState<AboxMappingKind>("class");
  const [mappingStatus, setMappingStatus] = useState<AboxMappingStatus | null>(
    null,
  );
  const [mappings, setMappings] = useState<{
    kind: AboxMappingKind;
    status: AboxMappingStatus | null;
    rows: AboxMapping[];
  } | null>(null);
  const [mappingsLoading, setMappingsLoading] = useState(false);
  const [mappingsError, setMappingsError] = useState<string | null>(null);
  const [mappingsReload, setMappingsReload] = useState(0);
  const [resetting, setResetting] = useState<string | null>(null);
  const [resetError, setResetError] = useState<string | null>(null);
  // Rows reset to auto, each with the build that had landed last when the
  // reset answered: the matcher chooses their target at a later build, so
  // their stored target is not the one they will get. The rows read for a
  // later build hold the target it chose and settle them.
  const [resetPending, setResetPending] = useState<
    ReadonlyMap<string, string | null>
  >(() => new Map());
  // The build that landed last. A reset reads it when it answers, since a
  // build can land while the reset is on its way.
  const lastBuild = useRef(builtAt);
  useEffect(() => {
    lastBuild.current = builtAt;
  }, [builtAt]);

  useEffect(() => {
    if (!namespace || !loaded) return;
    let cancelled = false;
    setMappingsLoading(true);
    listAboxMappings(apiClient, namespace, {
      kind: mappingKind,
      status: mappingStatus ?? undefined,
    })
      .then((result) => {
        if (cancelled) return;
        setMappings({
          kind: mappingKind,
          status: mappingStatus,
          rows: result.mappings,
        });
        setMappingsError(null);
        // These rows hold what this build chose for the rows reset before it
        // landed. A row reset after that waits for the build after it.
        setResetPending((pending) => {
          const waiting = [...pending].filter(([, build]) => build === builtAt);
          return waiting.length === pending.size ? pending : new Map(waiting);
        });
      })
      .catch((e) => {
        if (cancelled) return;
        setMappingsError(errorMessage(e));
      })
      .finally(() => {
        if (!cancelled) setMappingsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [
    apiClient,
    namespace,
    loaded,
    builtAt,
    listsReload,
    mappingKind,
    mappingStatus,
    mappingsReload,
  ]);

  // Rows of another filter (the answer for the new one is still loading) are not shown.
  const rows =
    mappings?.kind === mappingKind && mappings.status === mappingStatus
      ? mappings.rows
      : [];
  // Relation and attribute keys hold class IRIs: name them from the state's
  // class counts and the targets of the loaded rows, else from the IRI itself.
  const termLabels = new Map<string, string>();
  for (const c of abox?.classCounts ?? []) termLabels.set(c.classIri, c.label);
  for (const m of rows)
    if (m.target !== "ignore" && m.target !== "free")
      termLabels.set(m.target, m.targetLabel);
  const termLabel = (iri: string) => termLabels.get(iri) ?? iriTail(iri);

  /** A saved or reset row queues a rebuild: re-read the rows, and poll the state. */
  function mappingChanged() {
    setMappingsReload((n) => n + 1);
    setTrailingPolls(TRAILING_POLLS);
    setReload((r) => ({ key: r.key + 1, background: true }));
  }

  async function resetMapping(row: AboxMapping) {
    if (!namespace) return;
    setResetting(row.id);
    setResetError(null);
    try {
      const reset = await resetAboxMapping(apiClient, namespace, row.id);
      const build = lastBuild.current;
      setResetPending((ids) => new Map(ids).set(row.id, build));
      setMappings(
        (m) =>
          m && {
            ...m,
            rows: m.rows.map((r) => (r.id === reset.id ? reset : r)),
          },
      );
      mappingChanged();
    } catch (e) {
      setResetError(errorMessage(e));
      if (rowsOutOfDate(e)) setMappingsReload((n) => n + 1);
    } finally {
      setResetting(null);
    }
  }

  // ── Change-target dialog ──
  const [dialog, setDialog] = useState<ChangeDialog | null>(null);
  const dialogRequest = useRef(0);
  const searchRequest = useRef(0);

  /** Apply `next` only to the dialog the result was started from. */
  function updateDialog(
    request: number,
    next: (d: ChangeDialog) => ChangeDialog | null,
  ) {
    setDialog((d) => (d?.request === request ? next(d) : d));
  }

  function openChange(row: AboxMapping) {
    setDialog({
      request: ++dialogRequest.current,
      row,
      keyLabel: mappingKeyLabel(row, termLabel),
      text: "",
      target: null,
      direction: null,
      options: [],
      searching: false,
      searchError: null,
      busy: false,
      error: null,
    });
  }

  async function searchTargets(
    request: number,
    kind: AboxMappingKind,
    text: string,
  ) {
    if (!namespace) return;
    const search = ++searchRequest.current;
    if (!text.trim()) {
      updateDialog(request, (d) => ({ ...d, options: [], searching: false }));
      return;
    }
    updateDialog(request, (d) => ({
      ...d,
      searching: true,
      searchError: null,
    }));
    try {
      const hits = await searchEntities(apiClient, namespace, text, {
        kind: targetSearchKind(kind),
        limit: SEARCH_LIMIT,
      });
      if (search !== searchRequest.current) return;
      updateDialog(request, (d) => ({
        ...d,
        searching: false,
        options: hits.map((h) => ({
          value: h.uri,
          label: h.label,
          description: h.uri,
        })),
      }));
    } catch (e) {
      if (search !== searchRequest.current) return;
      updateDialog(request, (d) => ({
        ...d,
        searching: false,
        options: [],
        searchError: errorMessage(e),
      }));
    }
  }

  async function saveMapping(target: string) {
    if (!namespace || !dialog) return;
    const { request, row, direction } = dialog;
    // Only relation rows have a direction (their Save waits for one); the engine refuses one elsewhere.
    const body =
      row.kind === "relation" && target !== "ignore" && direction
        ? { target, direction }
        : { target };
    updateDialog(request, (d) => ({ ...d, busy: true, error: null }));
    try {
      await updateAboxMapping(apiClient, namespace, row.id, body);
      setResetPending((ids) => {
        const rest = new Map(ids);
        rest.delete(row.id);
        return rest;
      });
      updateDialog(request, () => null);
      mappingChanged();
    } catch (e) {
      updateDialog(request, (d) => ({
        ...d,
        busy: false,
        error: errorMessage(e),
      }));
      if (rowsOutOfDate(e)) setMappingsReload((n) => n + 1);
    }
  }

  const display = abox ? aboxStatusDisplay(abox, t) : null;
  const allClasses: SelectProps.Option = { value: "", label: t("All classes") };
  const allStatuses: SelectProps.Option = {
    value: "",
    label: t("All statuses"),
  };
  const classOptions: SelectProps.Option[] = [
    allClasses,
    ...(abox?.classCounts ?? []).map((c) => ({
      value: c.classIri,
      label: `${c.label} (${c.count})`,
    })),
  ];
  const selectedClass: SelectProps.Option = classOptions.find(
    (o) => o.value === (classIri ?? ""),
  ) ??
    // The class has no instances any more since the last build.
    { value: classIri ?? "", label: classIri ? iriTail(classIri) : "" };
  const filtered = !!classIri || !!filters.q;
  const statusOptions: SelectProps.Option[] = [
    allStatuses,
    ...ABOX_MAPPING_STATUSES.map((st) => ({
      value: st,
      label: t(MAPPING_STATUS[st].label),
    })),
  ];
  const noVersion = !!abox && abox.activeVersion == null;
  const needsDirection = dialog?.row.kind === "relation" && !dialog.direction;
  // The lists are read once the state is known. A table whose rows are on
  // their way says so, and its header counts nothing yet.
  const instancesPending = instancesLoading || (!loaded && loading);
  const mappingsPending =
    (mappingsLoading && rows.length === 0) || (!loaded && loading);

  return (
    <SpaceBetween size="l">
      {loadError && (
        <Alert type="error" header={t("Could not load the domain ABox")}>
          {loadError}
        </Alert>
      )}
      {rebuildError && (
        <Alert
          type="error"
          header={t("Could not queue a rebuild")}
          dismissible
          onDismiss={() => setRebuildError(null)}
        >
          {rebuildError}
        </Alert>
      )}

      <Container
        header={
          <Header
            description={t(
              "Instances, relations and evidence extracted from the namespace's documents for the deployed ontology version.",
            )}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  iconName="refresh"
                  loading={loading}
                  onClick={() =>
                    setReload((r) => ({ key: r.key + 1, background: false }))
                  }
                >
                  {t("Refresh")}
                </Button>
                <Button
                  loading={rebuilding}
                  disabled={noVersion || !canChange}
                  disabledReason={
                    noVersion
                      ? t("No ontology version is active to build from.")
                      : canChange
                        ? undefined
                        : t("Your role on this namespace does not allow this.")
                  }
                  onClick={rebuild}
                >
                  {t("Rebuild")}
                </Button>
              </SpaceBetween>
            }
          >
            {t("Domain ABox")}
          </Header>
        }
      >
        {abox && display ? (
          <SpaceBetween size="m">
            <SpaceBetween direction="horizontal" size="s" alignItems="center">
              <StatusIndicator type={display.type}>
                {display.label}
              </StatusIndicator>
              <Box color="text-body-secondary">{versionLine(abox, t)}</Box>
              {abox.builtAt && (
                <Box color="text-body-secondary">
                  {t("Last built {time}", {
                    time: formatTimestamp(abox.builtAt, locale),
                  })}
                </Box>
              )}
              {isOutOfDate(abox) && (
                <Badge color="severity-medium">{t("Out of date")}</Badge>
              )}
            </SpaceBetween>
            <KeyValuePairs
              columns={4}
              items={[
                { label: t("Instances"), value: abox.counts.instances },
                { label: t("Relations"), value: abox.counts.relations },
                { label: t("Attributes"), value: abox.counts.attributes },
                { label: t("Evidence"), value: abox.counts.evidence },
              ]}
            />
            {abox.lastError && (
              <Alert type="error" header={t("Last build failed")}>
                {abox.lastError}
              </Alert>
            )}
          </SpaceBetween>
        ) : (
          loading && (
            <StatusIndicator type="loading">
              {t("Loading domain ABox")}
            </StatusIndicator>
          )
        )}
      </Container>

      {instancesError && (
        <Alert type="error" header={t("Could not load instances")}>
          {instancesError}
        </Alert>
      )}
      <Table<DomainInstanceSummary>
        loading={instancesPending}
        loadingText={t("Loading instances")}
        items={instances}
        trackBy="iri"
        empty={
          <Box textAlign="center">
            {filtered
              ? t("No instances match the filter.")
              : t("No instances yet.")}
          </Box>
        }
        filter={
          <SpaceBetween direction="horizontal" size="xs">
            <Select
              ariaLabel={t("Filter by class")}
              selectedOption={selectedClass}
              options={classOptions}
              onChange={({ detail }) =>
                setClassIri(detail.selectedOption.value || null)
              }
            />
            {/* TextFilter has no key handler of its own; Enter runs the query now. */}
            <div
              onKeyDown={(e) => {
                if (e.key === "Enter") setQuery(filterText);
              }}
            >
              <TextFilter
                filteringText={filterText}
                filteringPlaceholder={t("Find instances by label")}
                filteringAriaLabel={t("Filter instances")}
                onChange={({ detail }) => setFilterText(detail.filteringText)}
              />
            </div>
          </SpaceBetween>
        }
        header={
          <Header
            counter={
              instancesPending
                ? undefined
                : `(${instances.length}${nextToken ? "+" : ""})`
            }
          >
            {t("Instances")}
          </Header>
        }
        columnDefinitions={[
          {
            id: "label",
            header: t("Label"),
            cell: (i) => (
              <Button
                variant="inline-link"
                ariaLabel={t("Open {label} ({classLabel})", {
                  label: i.label,
                  classLabel: i.classLabel,
                })}
                onClick={() => openInstance(i.iri, i.label)}
              >
                {i.label}
              </Button>
            ),
          },
          { id: "class", header: t("Class"), cell: (i) => i.classLabel },
          {
            id: "altLabels",
            header: t("Other labels"),
            cell: (i) => i.altLabels.join(", ") || "—",
          },
          {
            id: "evidence",
            header: t("Evidence"),
            cell: (i) => i.evidenceCount,
          },
        ]}
        footer={
          nextToken && (
            <Box textAlign="center">
              <Button
                loading={loadingMore}
                disabled={instancesLoading}
                onClick={loadMore}
              >
                {t("Load more")}
              </Button>
            </Box>
          )
        }
      />

      {detailError && (
        <Alert
          type="error"
          header={t("Could not load the instance")}
          dismissible
          onDismiss={() => setDetailError(null)}
        >
          {detailError}
        </Alert>
      )}
      {/* Always mounted, so screen readers announce a notice that appears in it. */}
      <div ref={noticeRegion} tabIndex={-1} aria-live="polite">
        {detailNotice && (
          <Alert
            type="info"
            dismissible
            onDismiss={() => setDetailNotice(null)}
          >
            {detailNotice}
          </Alert>
        )}
      </div>
      {selected && (
        <div ref={detailPanel}>
          <InstanceDetail
            instance={selected}
            onOpen={openInstance}
            onClose={closeDetail}
          />
        </div>
      )}

      {mappingsError && (
        <Alert type="error" header={t("Could not load mappings")}>
          {mappingsError}
        </Alert>
      )}
      {resetError && (
        <Alert
          type="error"
          header={t("Could not reset the mapping")}
          dismissible
          onDismiss={() => setResetError(null)}
        >
          {resetError}
        </Alert>
      )}
      <Table<AboxMapping>
        loading={mappingsPending}
        loadingText={t("Loading mappings")}
        items={rows}
        trackBy="id"
        empty={
          <Box textAlign="center">
            {mappingStatus
              ? t("No mappings match the filter.")
              : t(NO_MAPPINGS[mappingKind])}
          </Box>
        }
        filter={
          <SpaceBetween direction="horizontal" size="xs" alignItems="center">
            <SegmentedControl
              label={t("Mapping kind")}
              selectedId={mappingKind}
              onChange={({ detail }) =>
                setMappingKind(detail.selectedId as AboxMappingKind)
              }
              options={ABOX_MAPPING_KINDS.map((k) => ({
                id: k,
                text: t(KIND_LABELS[k]),
              }))}
            />
            <Select
              ariaLabel={t("Filter by status")}
              selectedOption={
                statusOptions.find((o) => o.value === (mappingStatus ?? "")) ??
                allStatuses
              }
              options={statusOptions}
              onChange={({ detail }) =>
                setMappingStatus(
                  (detail.selectedOption.value as AboxMappingStatus) || null,
                )
              }
            />
          </SpaceBetween>
        }
        header={
          <Header
            counter={mappingsPending ? undefined : `(${rows.length})`}
            description={
              !canChange
                ? t(
                    "How the strings found in the documents map to ontology terms. Your role on this namespace cannot change them.",
                  )
                : noVersion
                  ? t(
                      "How the strings found in the documents map to ontology terms. A target can be changed again once an ontology version is deployed.",
                    )
                  : t(
                      "How the strings found in the documents map to ontology terms. A change queues a rebuild.",
                    )
            }
          >
            {t("Mappings")}
          </Header>
        }
        columnDefinitions={[
          {
            id: "key",
            header: t("Found in documents"),
            cell: (m) => mappingKeyLabel(m, termLabel),
          },
          {
            id: "target",
            header: t("Maps to"),
            cell: (m) =>
              m.origin === "auto" && resetPending.has(m.id)
                ? t("Chosen by the next build")
                : mappingTargetLabel(m, t),
          },
          {
            id: "origin",
            header: t("Origin"),
            cell: (m) => (
              <Badge color={m.origin === "manual" ? "blue" : "grey"}>
                {t(ORIGIN_LABELS[m.origin])}
              </Badge>
            ),
          },
          {
            id: "status",
            header: t("Status"),
            cell: (m) => (
              <StatusIndicator type={MAPPING_STATUS[m.status].type}>
                {t(MAPPING_STATUS[m.status].label)}
              </StatusIndicator>
            ),
          },
          {
            id: "rationale",
            header: t("Rationale"),
            cell: (m) => m.rationale || "—",
          },
          {
            id: "actions",
            header: t("Actions"),
            cell: (m) => {
              const keyLabel = mappingKeyLabel(m, termLabel);
              return (
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    variant="inline-link"
                    ariaLabel={t("Change target of {key}", { key: keyLabel })}
                    // The engine refuses a new target (409) without a version
                    // to check it against; a reset needs none. An inline link
                    // takes no disabledReason: the table header says why.
                    disabled={noVersion || !canChange}
                    onClick={() => openChange(m)}
                  >
                    {t("Change target")}
                  </Button>
                  {m.origin === "manual" && (
                    <Button
                      variant="inline-link"
                      ariaLabel={t("Reset {key} to auto", { key: keyLabel })}
                      loading={resetting === m.id}
                      disabled={resetting !== null || !canChange}
                      onClick={() => resetMapping(m)}
                    >
                      {t("Reset to auto")}
                    </Button>
                  )}
                </SpaceBetween>
              );
            },
          },
        ]}
      />

      {dialog && (
        <Modal
          visible
          closeAriaLabel={t("Close dialog")}
          onDismiss={() => {
            // The edit cannot be withdrawn; keep its outcome visible.
            if (!dialog.busy) setDialog(null);
          }}
          header={t("Change target of {key}", { key: dialog.keyLabel })}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                {/* The engine accepts `ignore` for class and relation rows only. */}
                {dialog.row.kind === "attribute" ? (
                  <Button
                    disabled={dialog.busy}
                    onClick={() => saveMapping("free")}
                  >
                    {t("Use free attribute")}
                  </Button>
                ) : (
                  <Button
                    disabled={dialog.busy}
                    onClick={() => saveMapping("ignore")}
                  >
                    {t("Ignore")}
                  </Button>
                )}
                <Button
                  variant="link"
                  disabled={dialog.busy}
                  onClick={() => setDialog(null)}
                >
                  {t("Cancel")}
                </Button>
                <Button
                  variant="primary"
                  loading={dialog.busy}
                  disabled={!dialog.target || needsDirection || dialog.busy}
                  disabledReason={
                    !dialog.target
                      ? t("Pick a target first.")
                      : needsDirection
                        ? t("Pick a direction first.")
                        : undefined
                  }
                  onClick={() => dialog.target && saveMapping(dialog.target)}
                >
                  {t("Save")}
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <Box>
              {t("Currently: {target}", {
                target: mappingTargetLabel(dialog.row, t),
              })}
            </Box>
            <FormField
              label={t("Target")}
              description={t(TARGET_SEARCH[dialog.row.kind].description)}
              errorText={dialog.searchError}
            >
              <Autosuggest
                ariaLabel={t("Target")}
                value={dialog.text}
                disabled={dialog.busy}
                // A typed string is not a term: Save waits for a picked suggestion.
                onChange={({ detail }) =>
                  updateDialog(dialog.request, (d) => ({
                    ...d,
                    text: detail.value,
                    target: null,
                  }))
                }
                onLoadItems={({ detail }) =>
                  searchTargets(
                    dialog.request,
                    dialog.row.kind,
                    detail.filteringText,
                  )
                }
                onSelect={({ detail }) => {
                  // Enter on typed text selects that text, not a suggestion.
                  const option = detail.selectedOption;
                  if (!option) return;
                  updateDialog(dialog.request, (d) => ({
                    ...d,
                    target: detail.value,
                    text: option.label ?? detail.value,
                  }));
                }}
                options={dialog.options}
                statusType={dialog.searching ? "loading" : "finished"}
                placeholder={t(TARGET_SEARCH[dialog.row.kind].placeholder)}
                empty={t("No matches")}
                loadingText={t("Searching…")}
                filteringType="manual"
                enteredTextLabel={(v) => t('Use: "{value}"', { value: v })}
                hideEnteredTextOption
              />
            </FormField>
            {dialog.row.kind === "relation" && (
              <FormField
                label={t("Direction")}
                description={t(
                  "The property's domain and range decide which way fits.",
                )}
              >
                <Select
                  disabled={dialog.busy}
                  placeholder={t("Choose a direction")}
                  selectedOption={
                    directionOptions(dialog.row, termLabel, t).find(
                      (o) => o.value === dialog.direction,
                    ) ?? null
                  }
                  options={directionOptions(dialog.row, termLabel, t)}
                  onChange={({ detail }) =>
                    updateDialog(dialog.request, (d) => ({
                      ...d,
                      direction: detail.selectedOption
                        .value as AboxMappingDirection,
                    }))
                  }
                />
              </FormField>
            )}
            {dialog.error && (
              <Alert type="error" header={t("Could not change the mapping")}>
                {dialog.error}
              </Alert>
            )}
          </SpaceBetween>
        </Modal>
      )}
    </SpaceBetween>
  );
}

/** Forward runs the property from the key's subject class to its object class; inverse the other way. */
function directionOptions(
  row: AboxMapping,
  label: (iri: string) => string,
  t: AppTranslate,
): SelectProps.Option[] {
  const subject = label(row.key[1] ?? "");
  const object = label(row.key[2] ?? "");
  return [
    {
      value: "forward",
      label: t("Forward"),
      description: t("From {from} to {to}", { from: subject, to: object }),
    },
    {
      value: "inverse",
      label: t("Inverse"),
      description: t("From {from} to {to}", { from: object, to: subject }),
    },
  ];
}

function InstanceDetail({
  instance,
  onOpen,
  onClose,
}: {
  instance: DomainInstance;
  onOpen: (iri: string, label: string) => void;
  onClose: () => void;
}) {
  const { t } = useAppI18n();
  const hasLinks = instance.outgoing.length + instance.incoming.length > 0;
  // Focus (and show) the heading whenever another instance opens.
  const heading = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    heading.current?.focus();
    heading.current?.scrollIntoView({ block: "nearest" });
  }, [instance.iri]);
  return (
    <Container
      header={
        <Header
          description={instance.classes.map((c) => c.label).join(", ")}
          actions={<Button onClick={onClose}>{t("Close")}</Button>}
        >
          <span ref={heading} tabIndex={-1}>
            {instance.label}
          </span>
        </Header>
      }
    >
      <SpaceBetween size="l">
        {instance.altLabels.length > 0 && (
          <Box>
            {t("Also known as {labels}", {
              labels: instance.altLabels.join(", "),
            })}
          </Box>
        )}
        <Table
          variant="embedded"
          header={
            <Header variant="h3" counter={`(${instance.attributes.length})`}>
              {t("Attributes")}
            </Header>
          }
          items={instance.attributes}
          trackBy={(a) => `${a.property}|${a.value}`}
          empty={<Box textAlign="center">{t("No attributes")}</Box>}
          columnDefinitions={[
            {
              id: "property",
              header: t("Property"),
              cell: (a) => (
                <SpaceBetween direction="horizontal" size="xxs">
                  <span>{a.label}</span>
                  {a.free && <Badge>{t("free")}</Badge>}
                </SpaceBetween>
              ),
            },
            { id: "value", header: t("Value"), cell: (a) => a.value },
            {
              id: "evidence",
              header: t("Evidence"),
              cell: (a) => a.evidence.length,
            },
          ]}
        />
        <SpaceBetween size="xs">
          <Header variant="h3">{t("Links")}</Header>
          {!hasLinks && <Box color="text-body-secondary">{t("No links")}</Box>}
          {instance.outgoing.map((l) => (
            <Box key={`out|${l.property}|${l.target.iri}`}>
              {/* Reads as a sentence: this instance, the relation, the target. */}
              <span>{l.label}</span>{" "}
              {/* Named with the relation: two links can lead to the same instance. */}
              <Button
                variant="inline-link"
                ariaLabel={`${l.label} ${l.target.label}`}
                onClick={() => onOpen(l.target.iri, l.target.label)}
              >
                {l.target.label}
              </Button>
            </Box>
          ))}
          {instance.incoming.map((l) => (
            <Box key={`in|${l.property}|${l.source.iri}`}>
              {/* Reads as a sentence: the source, the relation, this instance. */}
              <Button
                variant="inline-link"
                ariaLabel={`${l.source.label} ${l.label}`}
                onClick={() => onOpen(l.source.iri, l.source.label)}
              >
                {l.source.label}
              </Button>{" "}
              <span>{l.label}</span>
            </Box>
          ))}
          {instance.truncated?.incoming && (
            <Box variant="small" color="text-body-secondary">
              {t("Showing the first {count} incoming links.", {
                count: instance.incoming.length,
              })}
            </Box>
          )}
        </SpaceBetween>
        <SpaceBetween size="xs">
          <Header variant="h3" counter={`(${instance.evidence.length})`}>
            {t("Evidence")}
          </Header>
          {instance.evidence.length === 0 && (
            <Box color="text-body-secondary">{t("No evidence")}</Box>
          )}
          {instance.evidence.map((ev, i) => (
            <ExpandableSection
              key={ev.chunkId}
              headerText={t("{file}, chunk {index}", {
                file: sourceFileName(ev.sourceKey) || ev.chunkId,
                index: i + 1,
              })}
            >
              <Box>{ev.text}</Box>
            </ExpandableSection>
          ))}
          {instance.truncated?.evidence && (
            <Box variant="small" color="text-body-secondary">
              {t("Showing the first {count} evidence chunks.", {
                count: instance.evidence.length,
              })}
            </Box>
          )}
        </SpaceBetween>
      </SpaceBetween>
    </Container>
  );
}
