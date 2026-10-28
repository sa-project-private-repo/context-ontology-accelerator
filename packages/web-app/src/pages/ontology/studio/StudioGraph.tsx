// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The editable class graph of a designer draft: OntologyGraphFlow with
 * multi-select, a right-click menu, ABox badges, and the focus view the
 * engine serves above 150 classes.
 *
 * The page owns `selected` (keep it in state: it reaches the graph as
 * `selectedIds`, and a fresh array on every render would rebuild the graph's
 * selection each time). The nodes and edges are rebuilt only when `graph`
 * (or `preview`) changes, because a change to the nodes re-runs the graph
 * layout. While a change set `preview` is given, it is drawn instead of the
 * draft, with its marks, and the right-click menu is off: its actions would
 * edit the draft, not the preview.
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { OntologyGraphFlow } from "@components/graph/OntologyGraphFlow";
import type {
  StudioGraph as StudioGraphData,
  StudioPreviewGraph,
  StudioTermHit,
  StudioTermKind,
} from "../../../services/studio";
import type { AppMessage } from "../../../i18n/appMessages";
import { useAppI18n } from "../../../i18n/LocaleProvider";
import { iriTail, toFlowEdges, toFlowNodes } from "./studio-helpers";
import { TermPicker } from "./TermPicker";

export type GraphActionKind =
  | "addSubclass"
  | "addRelationship"
  | "rename"
  | "delete";

export interface GraphAction {
  kind: GraphActionKind;
  iri: string;
  label: string;
}

const MENU: { kind: GraphActionKind; text: AppMessage }[] = [
  { kind: "addSubclass", text: "Add subclass" },
  { kind: "addRelationship", text: "Add relationship" },
  { kind: "rename", text: "Rename" },
  { kind: "delete", text: "Delete" },
];

/** The engine's full-graph limit; above it only the focus view exists. */
export const FULL_GRAPH_LIMIT = 150;

/** Menu size in px, to keep a menu opened near an edge inside the window. */
const MENU_WIDTH = 190;
const MENU_HEIGHT = 144;

interface MenuState {
  iri: string;
  label: string;
  editable: boolean;
  x: number;
  y: number;
}

/**
 * Moves keyboard focus to a node of the graph: React Flow's wrapper
 * (`.react-flow__node[data-id]`) is the focusable element, not our node div.
 * Compared in code rather than in a selector, because an IRI is not a safe
 * attribute value.
 */
function focusGraphNode(container: HTMLElement | null, iri: string) {
  const nodes = container?.querySelectorAll<HTMLElement>(".react-flow__node");
  for (const el of Array.from(nodes ?? [])) {
    if (el.getAttribute("data-id") === iri) {
      el.focus();
      return;
    }
  }
}

export interface StudioGraphProps {
  graph: StudioGraphData;
  selected: string[];
  /** The draft accepts edits (it is open for review). Classes outside the namespace never do. */
  editable: boolean;
  onSelectionChange: (ids: string[]) => void;
  /** Focus on a class; `null` leaves the focus view for the full graph. */
  onFocus: (iri: string | null) => void;
  onAction: (action: GraphAction) => void;
  search: (q: string, kind?: StudioTermKind) => Promise<StudioTermHit[]>;
  /** A change set preview to draw instead of the draft. */
  preview?: StudioPreviewGraph | null;
  onHidePreview?: () => void;
}

export function StudioGraph({
  graph,
  selected,
  editable,
  onSelectionChange,
  onFocus,
  onAction,
  search,
  preview = null,
  onHidePreview,
}: StudioGraphProps) {
  const { t } = useAppI18n();
  const shownGraph = preview ?? graph;
  const nodes = useMemo(() => toFlowNodes(shownGraph, t), [shownGraph, t]);
  const edges = useMemo(() => toFlowEdges(shownGraph), [shownGraph]);
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [activeItem, setActiveItem] = useState(0);
  const graphRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const closeMenu = useCallback(
    (restoreFocus: boolean) => {
      if (menu && restoreFocus) focusGraphNode(graphRef.current, menu.iri);
      setMenu(null);
    },
    [menu],
  );

  // A menu is a focus scope: it takes focus when it opens ...
  useEffect(() => {
    if (!menu) return;
    menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
  }, [menu]);

  // ... and Escape or a press outside it closes it. The press is caught on the
  // way down: React Flow's pan and drag handlers (d3-zoom, d3-drag) stop a
  // mousedown on the pane or a node from bubbling up to the document.
  useEffect(() => {
    if (!menu) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeMenu(true);
    };
    const onPress = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) closeMenu(false);
    };
    window.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPress, true);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPress, true);
    };
  }, [menu, closeMenu]);

  function onMenuKeyDown(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (e.key === "Tab") {
      closeMenu(true);
      return;
    }
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [],
    );
    const at = items.indexOf(document.activeElement as HTMLElement);
    const last = items.length - 1;
    let target: number;
    switch (e.key) {
      case "ArrowDown":
        target = at >= last ? 0 : at + 1;
        break;
      case "ArrowUp":
        target = at <= 0 ? last : at - 1;
        break;
      case "Home":
        target = 0;
        break;
      case "End":
        target = last;
        break;
      default:
        return;
    }
    e.preventDefault();
    items[target]?.focus();
  }

  const focusLabel =
    graph.nodes.find((n) => n.iri === graph.focus)?.label ??
    (graph.focus ? iriTail(graph.focus) : "");
  // One sentence per case; the class name is set in bold where {label} stands.
  const [focusBefore, focusAfter = ""] = t(
    graph.truncated
      ? "Showing {shown} of {total} classes within two steps of {label} (more neighbours are hidden)."
      : "Showing {shown} of {total} classes within two steps of {label}.",
    { shown: graph.nodes.length, total: graph.totalClasses },
  ).split("{label}");

  return (
    <SpaceBetween size="s">
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        <div style={{ minWidth: 320 }}>
          <TermPicker
            ariaLabel={t("Find a class")}
            placeholder={t("Find a class to focus on…")}
            kind="class"
            search={search}
            onPick={(hit) => onFocus(hit.iri)}
          />
        </div>
        {graph.mode === "focus" && graph.totalClasses <= FULL_GRAPH_LIMIT && (
          <Button onClick={() => onFocus(null)}>{t("Show all classes")}</Button>
        )}
      </SpaceBetween>
      {graph.mode === "focus" && (
        <Box variant="small">
          {focusBefore}
          <strong>{focusLabel}</strong>
          {focusAfter} {t("Double-click a class to focus on it.")}
        </Box>
      )}
      {preview && (
        <Alert
          type="info"
          action={
            <Button onClick={() => onHidePreview?.()}>
              {t("Hide preview")}
            </Button>
          }
        >
          {t(
            "Previewing a change set: green is added, orange changed and red removed.",
          )}
        </Alert>
      )}
      {graph.abox && !graph.abox.current && (
        <Alert type="info">
          {graph.abox.reason
            ? t("Instance counts are dimmed: {reason}.", {
                reason: graph.abox.reason,
              })
            : t("Instance counts are dimmed.")}
        </Alert>
      )}
      <div ref={graphRef} style={{ height: 620, position: "relative" }}>
        <OntologyGraphFlow
          nodes={nodes}
          edges={edges}
          layout="force"
          collapseStandalone={false}
          readableLabels
          focusId={graph.mode === "focus" ? graph.focus : null}
          selectedIds={selected}
          onSelectionChange={onSelectionChange}
          onNodeExpand={(n) => onFocus(n.id)}
          onNodeContextMenu={(n, point) => {
            if (preview) return;
            const node = graph.nodes.find((x) => x.iri === n.id);
            setMenu({
              iri: n.id,
              label: n.label,
              editable: editable && !!node?.editable,
              x: Math.max(0, Math.min(point.x, window.innerWidth - MENU_WIDTH)),
              y: Math.max(
                0,
                Math.min(point.y, window.innerHeight - MENU_HEIGHT),
              ),
            });
          }}
        />
        {menu &&
          // Into the full-screen element when the graph is full screen: nothing
          // outside it is drawn then.
          createPortal(
            <div
              key={`${menu.iri}@${menu.x},${menu.y}`}
              ref={menuRef}
              role="menu"
              aria-label={t("Actions for {label}", { label: menu.label })}
              onKeyDown={onMenuKeyDown}
              onContextMenu={(e) => e.preventDefault()}
              style={{
                position: "fixed",
                left: menu.x,
                top: menu.y,
                width: MENU_WIDTH,
                zIndex: 1000,
                background: "#ffffff",
                border: "1px solid #d1d5db",
                borderRadius: 6,
                padding: 4,
                boxShadow: "0 4px 12px rgba(0,0,0,0.15)",
              }}
            >
              {MENU.map((item, i) => (
                <button
                  key={item.kind}
                  type="button"
                  role="menuitem"
                  // Not `disabled`: a disabled item cannot take focus, and the
                  // menu always has to be able to.
                  aria-disabled={menu.editable ? undefined : true}
                  tabIndex={-1}
                  onFocus={() => setActiveItem(i)}
                  onMouseEnter={(e) => e.currentTarget.focus()}
                  onClick={() => {
                    if (!menu.editable) return;
                    closeMenu(true);
                    const act = () =>
                      onAction({
                        kind: item.kind,
                        iri: menu.iri,
                        label: menu.label,
                      });
                    // The page opens the action's dialog outside the graph,
                    // where nothing is drawn while the graph is full screen.
                    // Opened once that is over, the dialog takes the focus too.
                    if (document.fullscreenElement)
                      void document.exitFullscreen().then(act, act);
                    else act();
                  }}
                  style={{
                    display: "block",
                    width: "100%",
                    padding: "6px 12px",
                    border: 0,
                    borderRadius: 4,
                    background: activeItem === i ? "#f2f8fd" : "transparent",
                    outline: activeItem === i ? "2px solid #0972d3" : "none",
                    outlineOffset: -2,
                    color: menu.editable ? "#000716" : "#8d99a8",
                    cursor: menu.editable ? "pointer" : "default",
                    font: "inherit",
                    fontSize: 14,
                    lineHeight: "20px",
                    textAlign: "left",
                  }}
                >
                  {t(item.text)}
                </button>
              ))}
            </div>,
            document.fullscreenElement ?? document.body,
          )}
      </div>
    </SpaceBetween>
  );
}
