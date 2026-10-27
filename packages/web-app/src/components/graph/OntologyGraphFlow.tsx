// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * OntologyGraphFlow — React Flow-based ontology graph visualization.
 *
 * Two layouts:
 *  - `hierarchical` (default): dagre top-to-bottom. Right for taxonomies where
 *    `subClassOf` edges give a real "up" (the class explorer).
 *  - `force`: force-directed per connected component, then packed. Right for
 *    proposed schema ontologies, whose edges are mostly foreign keys and which
 *    split into several disconnected sub-schemas (see `layout.ts`).
 */
import {
  createContext,
  type MouseEvent as ReactMouseEvent,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  Panel,
  MarkerType,
  BaseEdge,
  EdgeLabelRenderer,
  useInternalNode,
  type InternalNode,
  type EdgeProps,
  type EdgeTypes,
  type Node,
  type Edge,
  type NodeTypes,
  type NodeChange,
  Handle,
  Position,
  ReactFlowProvider,
  applyNodeChanges,
  getViewportForBounds,
  useStore,
  useStoreApi,
  useReactFlow,
  type Rect,
  type Viewport,
  type XYPosition,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./graph.css";
import Dagre from "@dagrejs/dagre";
import Toggle from "@cloudscape-design/components/toggle";
import {
  HUB_DEGREE_THRESHOLD,
  isolatedNodeIds,
  layoutForceGraph,
  type LayoutEdge,
} from "./layout";
import {
  settledBounds,
  useLiveForceSimulation,
} from "./useLiveForceSimulation";
import { projectEdgeLabel } from "./labelProjection";
import { useAppI18n } from "../../i18n/LocaleProvider";
import type { AppMessage } from "../../i18n/appMessages";

// ─── Types ──────────────────────────────────────────────────────────

export interface GraphNodeData {
  id: string;
  label: string;
  kind:
    | "class"
    | "object-property"
    | "datatype-property"
    | "grounded"
    | "unknown";
  uri?: string;
  /** True when the node has sub-classes that can be revealed by expanding. */
  hasChildren?: boolean;
  /** True when the node's sub-classes are currently all visible. */
  expanded?: boolean;
  /** Datatype properties of this class, revealed as nodes on hover and after a click. */
  attributes?: GraphAttribute[];
  /** A small pill on the node (Ontology designer: the class's domain ABox instance count). */
  badge?: GraphNodeBadge;
  /** What a previewed change does to the node (Ontology designer change set preview). */
  highlight?: GraphHighlight;
  [key: string]: unknown;
}

/** A previewed change: drawn green, orange or red (dashed for removed). */
export type GraphHighlight = "added" | "changed" | "removed";

/** A pill drawn at the top of a node. */
export interface GraphNodeBadge {
  text: string;
  /** Tooltip; also the accessible name of the pill. */
  title?: string;
  /** Drawn faded (Ontology designer: the ABox does not match the active version). */
  dimmed?: boolean;
}

/** A datatype property (attribute) of a class. */
export interface GraphAttribute {
  label: string;
  range?: string;
}

export type GraphEdgeKind =
  | "subClassOf"
  | "suggestedSubClassOf"
  | "objectProperty";

export interface GraphEdgeData {
  id: string;
  source: string;
  target: string;
  label?: string;
  /** Drives edge styling: subclass edges are dashed purple with a hollow arrow. */
  kind?: GraphEdgeKind;
  /** What a previewed change does to the edge; overrides the kind's color. */
  highlight?: GraphHighlight;
}

export type GraphLayout = "hierarchical" | "force";

interface Props {
  nodes: GraphNodeData[];
  edges: GraphEdgeData[];
  /** See module doc. Defaults to `hierarchical`. */
  layout?: GraphLayout;
  /**
   * In force layout, whether standalone (relationship-less) classes start
   * hidden. Default true (proposal). The toggle to show/hide them is offered
   * either way; set false (Explorer) so curated search results aren't hidden
   * on first render.
   */
  collapseStandalone?: boolean;
  onNodeSelect?: (node: GraphNodeData | null) => void;
  /**
   * Called when a node is double-clicked. Used to lazily expand a class's
   * sub-classes (or collapse them again when already expanded).
   */
  onNodeExpand?: (node: GraphNodeData) => void;
  /**
   * Ids drawn as selected. With `onSelectionChange`, a click selects one node
   * and a Shift/Ctrl/Cmd-click adds or removes one (multi-select).
   */
  selectedIds?: readonly string[];
  onSelectionChange?: (ids: string[]) => void;
  /** Right-click on a node; the browser menu is suppressed only when this is set. */
  onNodeContextMenu?: (
    node: GraphNodeData,
    point: { x: number; y: number },
  ) => void;
  /**
   * Keep class labels readable, in the force layout (the Ontology designer).
   * Below READABLE_ZOOM, nodes, attribute rings and relationship labels keep
   * the size they have there, up to MAX_NODE_SCALE times their own, and the
   * graph frames itself (when it opens, when its classes change, in and out
   * of full screen) no further out than MIN_READABLE_FRAME_ZOOM. When the
   * whole graph does not fit there, the frame centres the selected class,
   * else `focusId`, else a hub or the class nearest the middle. The fit-view
   * control still shows the whole graph.
   */
  readableLabels?: boolean;
  /** The class a focus view is built around, centred as `readableLabels` says. */
  focusId?: string | null;
}

/** React Flow node carrying our typed ontology payload. */
type OntologyFlowNode = Node<GraphNodeData>;

/** Rendered node diameter (px). Shared with the layout so spacing matches. */
const NODE_SIZE = 64;

/**
 * Shortest chord where the inner-stagger t ≈ 0.35 still lands the label
 * past the hub's own radius + the label half-width (`(NODE_SIZE / 2 + ~44)
 * / 0.35 ≈ 220`). Below this, we fall back to the midpoint instead of
 * dropping the label on top of the hub node.
 */
const MIN_STAGGER_CHORD = 220;

/** Live force-simulation settings, shared with the settled-frame prediction. */
const FORCE_OPTIONS = { nodeSize: NODE_SIZE, linkDistance: 330 };

/** Edge labels are hidden below this zoom — they are unreadable confetti there. */
export const EDGE_LABEL_MIN_ZOOM = 0.55;

/** At this zoom a class label (11px) is drawn about 9px tall, about the least that reads. */
export const READABLE_ZOOM = 0.8;
/** The most a readable graph enlarges its nodes, so they do not swallow their neighbours. */
export const MAX_NODE_SCALE = 2;
/** Below this zoom even enlarged nodes no longer read: a readable graph frames no further out. */
export const MIN_READABLE_FRAME_ZOOM = READABLE_ZOOM / MAX_NODE_SCALE;
/** React Flow's default fit padding, which every frame here uses. */
const FIT_PADDING = 0.1;
/**
 * React Flow's fit on mount, once it has measured the nodes (the frame of
 * where the graph settles follows it). A readable graph does not take it: it
 * would come after the graph's own first frame and replace it.
 */
const FIT_OPTIONS = { padding: FIT_PADDING };

/**
 * How many times their own size nodes are drawn at `zoom` to keep their
 * labels readable: 1 from READABLE_ZOOM up, growing as the graph zooms out,
 * at most MAX_NODE_SCALE. Rounded to a twentieth, so a zoom gesture redraws
 * the nodes in steps rather than on every frame.
 */
export function readableNodeScale(zoom: number): number {
  if (zoom >= READABLE_ZOOM) return 1;
  return Math.round(Math.min(READABLE_ZOOM / zoom, MAX_NODE_SCALE) * 20) / 20;
}

/**
 * The viewport that frames `bounds` in a `width` by `height` pane as React
 * Flow's fit does, but zoomed out no further than `minZoom`. When the bounds
 * do not fit there, it centres `centre` (else their middle) as far as it can
 * while the view stays on the bounds, with the fit's padding at their edge:
 * what does not fit is cut on the side away from `centre`, and no empty
 * margin is shown on the other.
 */
export function readableViewport(
  bounds: Rect,
  width: number,
  height: number,
  minZoom: number,
  maxZoom: number,
  centre: XYPosition | null,
): Viewport {
  const whole = getViewportForBounds(
    bounds,
    width,
    height,
    0,
    maxZoom,
    FIT_PADDING,
  );
  if (whole.zoom >= minZoom) return whole;
  /** The flow coordinate the pane's middle shows along one axis. */
  const axis = (at: number, start: number, size: number, pane: number) => {
    const view = pane / minZoom;
    const pad = (view - view / (1 + FIT_PADDING)) / 2;
    if (size + 2 * pad <= view) return start + size / 2;
    return Math.min(
      Math.max(at, start - pad + view / 2),
      start + size + pad - view / 2,
    );
  };
  const c = centre ?? {
    x: bounds.x + bounds.width / 2,
    y: bounds.y + bounds.height / 2,
  };
  return {
    x: width / 2 - axis(c.x, bounds.x, bounds.width, width) * minZoom,
    y: height / 2 - axis(c.y, bounds.y, bounds.height, height) * minZoom,
    zoom: minZoom,
  };
}

/**
 * Where a readable frame is centred when the graph does not fit: the first of
 * `preferred` that is in `centres`; else the node with the most edges, when
 * the layout counts it a hub; else the node nearest the middle of `bounds`,
 * so the frame shows nodes rather than the gap between two clusters. Null
 * only without nodes.
 */
export function framingCentre(
  centres: ReadonlyMap<string, XYPosition>,
  edges: readonly LayoutEdge[],
  preferred: readonly (string | null | undefined)[],
  bounds: Rect,
): XYPosition | null {
  for (const id of preferred) {
    const c = id ? centres.get(id) : undefined;
    if (c) return c;
  }
  const degree = new Map<string, number>();
  for (const e of edges) {
    for (const id of [e.source, e.target]) {
      if (centres.has(id)) degree.set(id, (degree.get(id) ?? 0) + 1);
    }
  }
  let hub: XYPosition | null = null;
  let most = 0;
  for (const [id, c] of centres) {
    const d = degree.get(id) ?? 0;
    if (d > most) {
      hub = c;
      most = d;
    }
  }
  if (most >= HUB_DEGREE_THRESHOLD) return hub;
  const mx = bounds.x + bounds.width / 2;
  const my = bounds.y + bounds.height / 2;
  let nearest: XYPosition | null = null;
  let closest = Infinity;
  for (const c of centres.values()) {
    const d = Math.hypot(c.x - mx, c.y - my);
    if (d < closest) {
      nearest = c;
      closest = d;
    }
  }
  return nearest;
}

/** Each node's centre, by id. */
function centresOf(nodes: readonly Node[]): Map<string, XYPosition> {
  return new Map(
    nodes.map((n) => [
      n.id,
      { x: n.position.x + NODE_SIZE / 2, y: n.position.y + NODE_SIZE / 2 },
    ]),
  );
}

/** The box around the nodes. */
function boundsOf(nodes: readonly Node[]): Rect {
  const xs = nodes.map((n) => n.position.x);
  const ys = nodes.map((n) => n.position.y);
  const x = Math.min(...xs);
  const y = Math.min(...ys);
  return {
    x,
    y,
    width: Math.max(...xs) + NODE_SIZE - x,
    height: Math.max(...ys) + NODE_SIZE - y,
  };
}

/** Arrowhead size (px, user space) and the gap it needs before the node edge. */
const ARROW_SIZE = 16;
/** Attribute (datatype-property) node diameter, revealed around the hovered or clicked class. */
const ATTR_SIZE = 52;
const ARROW_GAP = 3;

// ─── Layout ─────────────────────────────────────────────────────────

/**
 * Evenly spaced points around a circle, starting at the top (12 o'clock)
 * and going clockwise. Radius grows with the node count so neighbours stay
 * ~110px apart along the circumference.
 */
export function radialPositions(count: number): { x: number; y: number }[] {
  if (count <= 0) return [];
  if (count === 1) return [{ x: 0, y: 0 }];
  const radius = Math.max(160, (count * 110) / (2 * Math.PI));
  return Array.from({ length: count }, (_, i) => {
    const angle = (2 * Math.PI * i) / count - Math.PI / 2;
    return { x: radius * Math.cos(angle), y: radius * Math.sin(angle) };
  });
}

/**
 * Arrange edge-less nodes evenly around a circle.
 *
 * Dagre stacks disconnected nodes into a single horizontal row, which is
 * what the initial "root classes" view is (no edges until a class is
 * expanded). A circular arrangement reads as a graph cluster instead of a
 * line and scales gracefully as the count grows.
 */
function circularLayout<T extends Node>(
  nodes: T[],
  edges: Edge[],
): {
  nodes: T[];
  edges: Edge[];
} {
  const positions = radialPositions(nodes.length);
  return {
    nodes: nodes.map((node, i) => ({ ...node, position: positions[i] })),
    edges,
  };
}

function layoutGraph<T extends Node>(
  nodes: T[],
  edges: Edge[],
  layout: GraphLayout,
): { nodes: T[]; edges: Edge[] } {
  if (layout === "force") {
    const positions = layoutForceGraph(
      nodes.map((n) => n.id),
      edges,
      { nodeSize: NODE_SIZE },
    );
    return {
      nodes: nodes.map((node) => ({
        ...node,
        position: positions.get(node.id) ?? { x: 0, y: 0 },
      })),
      edges,
    };
  }

  // No edges → circular cluster (initial root view). Dagre would otherwise
  // lay these out as one flat row.
  if (edges.length === 0) {
    return circularLayout(nodes, edges);
  }

  const g = new Dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "TB", ranksep: 100, nodesep: 80 });

  for (const node of nodes) {
    g.setNode(node.id, { width: NODE_SIZE, height: NODE_SIZE });
  }
  for (const edge of edges) {
    g.setEdge(edge.source, edge.target);
  }

  Dagre.layout(g);

  const half = NODE_SIZE / 2;
  return {
    nodes: nodes.map((node) => {
      const pos = g.node(node.id);
      // Defensive: dagre returns undefined for a node it never positioned
      // (shouldn't happen since every node is added above, but guard anyway).
      if (!pos) return { ...node, position: { x: 0, y: 0 } };
      return { ...node, position: { x: pos.x - half, y: pos.y - half } };
    }),
    edges,
  };
}

// ─── Custom Node ────────────────────────────────────────────────────

const KIND_COLORS: Record<string, { bg: string; border: string }> = {
  class: { bg: "#dbeafe", border: "#3b82f6" }, // blue — novel induced class
  grounded: { bg: "#ccfbf1", border: "#0d9488" }, // teal — grounded to reference ontology
  "object-property": { bg: "#fef3c7", border: "#f59e0b" }, // amber — relationship
  "datatype-property": { bg: "#fee2e2", border: "#ef4444" }, // red — attribute
  unknown: { bg: "#f3f4f6", border: "#9ca3af" },
};

const HIGHLIGHT_COLORS: Record<
  GraphHighlight,
  { bg: string; border: string; label: AppMessage }
> = {
  added: { bg: "#dcfce7", border: "#16a34a", label: "Added" },
  changed: { bg: "#ffedd5", border: "#ea580c", label: "Changed" },
  removed: { bg: "#fee2e2", border: "#dc2626", label: "Removed" },
};

const LEGEND_ITEMS: Array<{ kind: string; label: AppMessage }> = [
  { kind: "class", label: "Novel class" },
  { kind: "grounded", label: "Grounded class" },
  { kind: "object-property", label: "Relationship" },
  { kind: "datatype-property", label: "Attribute" },
];

/** Edge visuals by kind. Subclass edges: dashed purple, hollow arrow (UML). */
const EDGE_STYLES: Record<
  GraphEdgeKind,
  { stroke: string; dash?: string; marker: MarkerType }
> = {
  objectProperty: { stroke: "#94a3b8", marker: MarkerType.ArrowClosed },
  subClassOf: {
    stroke: "#7c3aed",
    dash: "6 4",
    marker: MarkerType.Arrow,
  },
  suggestedSubClassOf: {
    stroke: "#a78bfa",
    dash: "2 4",
    marker: MarkerType.Arrow,
  },
};

function styleEdge(e: GraphEdgeData, showLabel: boolean): Edge {
  const s = EDGE_STYLES[e.kind ?? "objectProperty"];
  const mark = e.highlight ? HIGHLIGHT_COLORS[e.highlight] : null;
  const stroke = mark?.border ?? s.stroke;
  return {
    id: e.id,
    source: e.source,
    target: e.target,
    label: showLabel ? e.label : undefined,
    style: {
      stroke,
      strokeWidth: mark ? 2.5 : 1.5,
      strokeDasharray: e.highlight === "removed" ? "4 4" : s.dash,
    },
    markerEnd: {
      type: s.marker,
      color: stroke,
      width: ARROW_SIZE,
      height: ARROW_SIZE,
      markerUnits: "userSpaceOnUse",
      strokeWidth: 1.5,
    },
    // Hierarchical mode still uses React Flow's SVG label; force mode renders
    // an HTML label (see FloatingEdge) styled by .coa-graph-edge-label.
    labelStyle: { fontSize: 10, fill: "#0f172a", fontWeight: 500 },
    labelBgStyle: { fill: "#ffffff", stroke: "#cbd5e1", strokeWidth: 1 },
    labelBgPadding: [5, 3],
    labelBgBorderRadius: 3,
  };
}

/**
 * Hover state (and the selection, and how large readable labels draw the
 * nodes) delivered to nodes via context rather than through node `data`, so
 * hovering or selecting never re-creates node objects (which would make React
 * Flow re-render every node and fight the live simulation's position updates).
 */
const HoverContext = createContext<{
  hoveredId: string | null;
  neighbourIds: ReadonlySet<string> | null;
  setHoveredEdgeId: (id: string | null) => void;
  selected: ReadonlySet<string>;
  /** How many times their own size nodes and edge labels are drawn (see readableNodeScale). */
  nodeScale: number;
}>({
  hoveredId: null,
  neighbourIds: null,
  setHoveredEdgeId: () => {},
  selected: new Set(),
  nodeScale: 1,
});

/** The ids after clicking `id`: Shift/Ctrl/Cmd toggles it, a plain click selects only it. */
export function nextSelection(
  current: readonly string[],
  id: string,
  additive: boolean,
): string[] {
  if (!additive) return [id];
  return current.includes(id)
    ? current.filter((x) => x !== id)
    : [...current, id];
}

function OntologyNode({ data }: { data: GraphNodeData }) {
  const { t } = useAppI18n();
  const colors = data.highlight
    ? HIGHLIGHT_COLORS[data.highlight]
    : (KIND_COLORS[data.kind] ?? KIND_COLORS.unknown);
  const { hoveredId, neighbourIds, selected, nodeScale } =
    useContext(HoverContext);
  const isSelected = selected.has(data.id);
  const dimmed =
    hoveredId !== null && neighbourIds !== null && !neighbourIds.has(data.id);
  return (
    <div
      className="coa-graph-node"
      title={
        data.hasChildren
          ? t("{label} — double-click to expand sub-classes", {
              label: data.label,
            })
          : data.label
      }
      style={{
        opacity: dimmed ? 0.25 : data.highlight === "removed" ? 0.75 : 1,
        position: "relative",
        width: NODE_SIZE,
        height: NODE_SIZE,
        borderRadius: "50%",
        border: `3px ${data.highlight === "removed" ? "dashed" : "solid"} ${colors.border}`,
        background: colors.bg,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontSize: 11,
        fontFamily: "system-ui, sans-serif",
        textAlign: "center",
        cursor: "grab",
        overflow: "hidden",
        padding: 6,
        boxShadow: isSelected ? "0 0 0 4px #0972d3" : undefined,
        // Around the node's centre, so the layout and the edges keep it.
        scale: nodeScale === 1 ? undefined : String(nodeScale),
      }}
      // Not aria-selected / aria-pressed: this div has no role and is not the
      // focusable element (React Flow's role="group" wrapper is), so ARIA
      // states are not valid here. A plain data attribute marks it for tests.
      data-selected={isSelected ? "true" : undefined}
      data-highlight={data.highlight}
    >
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <div
        style={{
          fontWeight: 600,
          lineHeight: 1.2,
          wordBreak: "break-word",
          color: "#000000",
          // Clamp long labels to three lines; the full name is in the tooltip.
          display: "-webkit-box",
          WebkitLineClamp: 3,
          WebkitBoxOrient: "vertical",
          overflow: "hidden",
        }}
      >
        {data.label}
      </div>
      {data.hasChildren && (
        <span
          style={{
            position: "absolute",
            bottom: -4,
            right: -4,
            width: 18,
            height: 18,
            borderRadius: "50%",
            background: colors.border,
            color: "#ffffff",
            fontSize: 13,
            fontWeight: 700,
            lineHeight: "16px",
            textAlign: "center",
            border: "2px solid #ffffff",
            overflow: "hidden",
          }}
        >
          {data.expanded ? "−" : "+"}
        </span>
      )}
      {data.badge && (
        <span
          className="coa-graph-node-badge"
          role="img"
          title={data.badge.title}
          aria-label={data.badge.title ?? data.badge.text}
          style={{
            position: "absolute",
            top: 4,
            left: "50%",
            transform: "translateX(-50%)",
            minWidth: 20,
            padding: "0 5px",
            borderRadius: 8,
            background: "#0f172a",
            color: "#ffffff",
            fontSize: 9,
            fontWeight: 700,
            lineHeight: "14px",
            textAlign: "center",
            opacity: data.badge.dimmed ? 0.45 : 1,
          }}
        >
          {data.badge.text}
        </span>
      )}
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  );
}

/**
 * Small red node for a class's datatype property (attribute). Rendered only
 * around the hovered class, or the one clicked last (see attributeOverlay), so
 * it is pointer-transparent: you hover the class, not its attributes.
 */
function AttributeNode({ data }: { data: GraphNodeData }) {
  const colors = KIND_COLORS["datatype-property"];
  const { nodeScale } = useContext(HoverContext);
  return (
    <div
      title={data.label}
      style={{
        scale: nodeScale === 1 ? undefined : String(nodeScale),
        width: ATTR_SIZE,
        height: ATTR_SIZE,
        borderRadius: "50%",
        border: `2px solid ${colors.border}`,
        background: colors.bg,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        textAlign: "center",
        fontSize: 9,
        fontFamily: "system-ui, sans-serif",
        color: "#000000",
        overflow: "hidden",
        padding: 4,
      }}
    >
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <div
        style={{
          fontWeight: 500,
          lineHeight: 1.15,
          wordBreak: "break-word",
          display: "-webkit-box",
          WebkitLineClamp: 3,
          WebkitBoxOrient: "vertical",
          overflow: "hidden",
        }}
      >
        {data.label}
      </div>
    </div>
  );
}

const nodeTypes: NodeTypes = {
  ontology: OntologyNode,
  attribute: AttributeNode,
};

// ─── Floating edge (force layout) ───────────────────────────────────

interface FloatingEdgeData extends Record<string, unknown> {
  /** Perpendicular offset (px) so parallel edges between one pair fan out. */
  curve?: number;
  /**
   * Position of the label along the edge, as a `t` in [0, 1]. The actual
   * coordinate is derived here from the current source/target centres and
   * the curve offset — so a label stays on the drawn edge (including the
   * Bézier bow for parallel edges), and so a sim tick never has to recompute
   * every label position upstream. Default (undefined) → `0.5`, the chord
   * midpoint.
   */
  labelT?: number;
}

/**
 * Edge drawn from circle boundary to circle boundary along the line between
 * node centres, WebVOWL-style. The default edges anchor to the top/bottom
 * handles, which reads as a hierarchy the force layout does not have.
 */
function FloatingEdge({
  id,
  source,
  target,
  data,
  markerEnd,
  style,
  label,
}: EdgeProps<Edge<FloatingEdgeData>>) {
  const sourceNode = useInternalNode(source);
  const targetNode = useInternalNode(target);
  const { setHoveredEdgeId, nodeScale } = useContext(HoverContext);
  if (!sourceNode || !targetNode) return null;

  const centre = (n: InternalNode) => ({
    x: n.internals.positionAbsolute.x + (n.measured.width ?? NODE_SIZE) / 2,
    y: n.internals.positionAbsolute.y + (n.measured.height ?? NODE_SIZE) / 2,
  });
  const s = centre(sourceNode);
  const t = centre(targetNode);
  // Both ends are drawn nodeScale times their measured size.
  const rSource = ((sourceNode.measured.width ?? NODE_SIZE) / 2) * nodeScale;
  const rTarget = ((targetNode.measured.width ?? NODE_SIZE) / 2) * nodeScale;
  const dx = t.x - s.x;
  const dy = t.y - s.y;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  const curve = data?.curve ?? 0;

  // Control point offset along the normal; the end points move slightly
  // around the circle in the same direction so the curve meets it cleanly.
  // The target end stops ARROW_GAP px short of the boundary: nodes paint
  // above edges, so a tip touching the circle is hidden under it.
  const nx = -uy;
  const ny = ux;
  const sx = s.x + ux * rSource + nx * curve * 0.3;
  const sy = s.y + uy * rSource + ny * curve * 0.3;
  const tx = t.x - ux * (rTarget + ARROW_GAP) + nx * curve * 0.3;
  const ty = t.y - uy * (rTarget + ARROW_GAP) + ny * curve * 0.3;

  let path: string;
  const labelT = data?.labelT ?? 0.5;
  const labelPoint = projectEdgeLabel(sx, sy, tx, ty, curve, labelT);
  const labelX = labelPoint.x;
  const labelY = labelPoint.y;
  if (curve === 0) {
    path = `M ${sx} ${sy} L ${tx} ${ty}`;
  } else {
    const cx = (sx + tx) / 2 + nx * curve;
    const cy = (sy + ty) / 2 + ny * curve;
    path = `M ${sx} ${sy} Q ${cx} ${cy} ${tx} ${ty}`;
  }

  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style} />
      {label !== undefined && label !== null && label !== "" && (
        // HTML label instead of React Flow's SVG <EdgeText>: that one measures
        // itself with getBBox() on every render, which thrashes layout when
        // dozens of labels move each simulation tick.
        <EdgeLabelRenderer>
          <div
            className="coa-graph-edge-label"
            style={{
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)${
                nodeScale === 1 ? "" : ` scale(${nodeScale})`
              }`,
              opacity: style?.opacity,
            }}
            onMouseEnter={() => setHoveredEdgeId(id)}
            onMouseLeave={() => setHoveredEdgeId(null)}
          >
            {label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

const edgeTypes: EdgeTypes = {
  floating: FloatingEdge,
};

/**
 * Spread edges that share the same (unordered) node pair so their labels do
 * not stack: offsets are symmetric around zero, 44px apart.
 */
export function parallelEdgeCurves(edges: readonly GraphEdgeData[]): number[] {
  const groups = new Map<string, number[]>();
  edges.forEach((e, i) => {
    const key = [e.source, e.target].sort().join("\u0000");
    const g = groups.get(key) ?? [];
    g.push(i);
    groups.set(key, g);
  });
  const curves = new Array<number>(edges.length).fill(0);
  for (const indices of groups.values()) {
    const n = indices.length;
    indices.forEach((edgeIndex, k) => {
      // The normal is computed from each edge's own direction, so a reversed
      // edge in the same group must flip its sign to land on the other side.
      const e = edges[edgeIndex];
      const sign = e.source <= e.target ? 1 : -1;
      curves[edgeIndex] = sign * (k - (n - 1) / 2) * 44;
    });
  }
  return curves;
}

/**
 * Keep the graph's focal point pinned when the pane is resized.
 *
 * React Flow's viewport transform is anchored at the flow origin, so growing
 * the pane opens the new space at the right/bottom and whatever you were
 * looking at drifts toward the top-left corner. To hold the pane CENTER
 * stationary we shift the viewport by half the size delta: the flow point shown
 * at screen-center is `((w / 2) - x) / zoom`; keeping it fixed as `w → w'` gives
 * `x' = x + (w' - w) / 2` (same for height/y). Zoom is untouched — unlike
 * fitView, this preserves the user's current zoom and pan.
 */
function usePreserveCenterOnResize() {
  const width = useStore((s) => s.width);
  const height = useStore((s) => s.height);
  const { getViewport, setViewport } = useReactFlow();
  const prev = useRef<{ width: number; height: number } | null>(null);

  useEffect(() => {
    if (!width || !height) return; // pane not measured yet
    const last = prev.current;
    prev.current = { width, height };
    if (!last) return; // first measurement — nothing to compensate against
    const dw = width - last.width;
    const dh = height - last.height;
    if (dw === 0 && dh === 0) return;
    const vp = getViewport();
    setViewport({ x: vp.x + dw / 2, y: vp.y + dh / 2, zoom: vp.zoom });
  }, [width, height, getViewport, setViewport]);
}

// ─── Component ──────────────────────────────────────────────────────

function OntologyGraphFlowInner({
  nodes,
  edges,
  layout = "hierarchical",
  collapseStandalone = true,
  onNodeSelect,
  onNodeExpand,
  selectedIds,
  onSelectionChange,
  onNodeContextMenu,
  readableLabels = false,
  focusId = null,
}: Props) {
  const isForce = layout === "force";
  // Only the force layout's edges follow the size a node is drawn at.
  const readable = readableLabels && isForce;

  // Classes with no relationships carry no information in a *relationship*
  // graph; in real proposals they are ~40% of the nodes. A toggle hides them
  // (WebVOWL's "collapsing filter"). `collapseStandalone` only sets the DEFAULT:
  // the proposal starts collapsed; the Explorer starts expanded so a curated
  // search result is never hidden — but the toggle is offered either way.
  const isolated = useMemo(
    () =>
      isForce
        ? isolatedNodeIds(
            nodes.map((n) => n.id),
            edges,
          )
        : new Set<string>(),
    [isForce, nodes, edges],
  );
  const [showIsolated, setShowIsolated] = useState(!collapseStandalone);
  const visibleNodes = useMemo(
    () =>
      showIsolated || isolated.size === 0
        ? nodes
        : nodes.filter((n) => !isolated.has(n.id)),
    [nodes, isolated, showIsolated],
  );

  // Edge labels only when zoomed in enough to read them. Selecting the boolean
  // (not the zoom) means the graph re-renders only when the threshold is
  // crossed, not on every pan/zoom frame.
  const showEdgeLabels = useStore((s) => s.transform[2] >= EDGE_LABEL_MIN_ZOOM);
  // A readable graph draws its nodes larger below READABLE_ZOOM. The value
  // moves in steps, so a zoom gesture re-renders them only now and then.
  const nodeScale = useStore((s) =>
    readable ? readableNodeScale(s.transform[2]) : 1,
  );

  const { t } = useAppI18n();
  const flowNodes: OntologyFlowNode[] = useMemo(
    () =>
      visibleNodes.map((n) => ({
        id: n.id,
        type: "ontology",
        position: { x: 0, y: 0 },
        data: n,
        draggable: true,
      })),
    [visibleNodes],
  );

  // Hovering a node highlights its relationships (labels shown regardless of
  // zoom) and dims everything else — the graph answers "what is this
  // connected to?" without a click.
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  // Hovering an edge label dims everything except that edge and its two
  // endpoint classes — same focus+context UX as hovering a node, but
  // triggered by the label and scoped to its single edge.
  const [hoveredEdgeId, setHoveredEdgeId] = useState<string | null>(null);

  // Perpendicular offsets for parallel edges — keyed by edge id, memoized on
  // edges only. Shared by `flowEdges` (used in path rendering) and
  // `attributeOverlay` (used when projecting a hovered edge's label).
  const edgeCurves = useMemo(() => {
    const map = new Map<string, number>();
    const curves = parallelEdgeCurves(edges);
    edges.forEach((e, i) => map.set(e.id, curves[i]));
    return map;
  }, [edges]);

  const flowEdges: Edge[] = useMemo(() => {
    return edges.map((e) => {
      const touchesHoveredNode =
        hoveredId !== null &&
        (e.source === hoveredId || e.target === hoveredId);
      const isHoveredEdge = hoveredEdgeId !== null && e.id === hoveredEdgeId;
      const touchesHovered = touchesHoveredNode || isHoveredEdge;
      const styled = styleEdge(e, showEdgeLabels || touchesHovered);
      if (hoveredId !== null || hoveredEdgeId !== null) {
        styled.style = {
          ...styled.style,
          opacity: touchesHovered ? 1 : 0.12,
          strokeWidth: touchesHovered ? 2.5 : 1.5,
        };
        styled.zIndex = touchesHovered ? 1 : 0;
      }
      return isForce
        ? {
            ...styled,
            type: "floating",
            data: { curve: edgeCurves.get(e.id) ?? 0 },
          }
        : styled;
    });
  }, [edges, edgeCurves, isForce, showEdgeLabels, hoveredId, hoveredEdgeId]);

  // Layout depends on topology only, not on label visibility — key it on the
  // structural inputs so toggling labels never re-runs the simulation.
  const structuralEdges = useMemo(
    () => edges.map((e) => ({ id: e.id, source: e.source, target: e.target })),
    [edges],
  );
  const initialNodes = useMemo(
    () => layoutGraph(flowNodes, structuralEdges, layout).nodes,
    [flowNodes, structuralEdges, layout],
  );

  // Hold the focal point centered across pane resizes (see hook doc).
  usePreserveCenterOnResize();

  const [liveNodes, setLiveNodes] = useState<OntologyFlowNode[]>(initialNodes);

  // Keep in sync when the computed layout changes (state sync is a side
  // effect, so it belongs in useEffect — React may skip a useMemo body).
  useEffect(() => {
    setLiveNodes(initialNodes);
  }, [initialNodes]);

  // On hover, inflate the hovered class's collider so forceCollide pushes its
  // neighbours outward live, carving room for the attribute ring + edge
  // labels. The ref lets the collider callback stay stable (so swapping the
  // hover focus never recreates the whole simulation).
  const hoverExpansionRef = useRef<{ id: string | null; radius: number }>({
    id: null,
    radius: 0,
  });
  const nodeRadius = useCallback((id: string): number => {
    if (hoverExpansionRef.current.id === id) {
      return hoverExpansionRef.current.radius;
    }
    return NODE_SIZE / 2;
  }, []);

  // Force mode keeps the physics running: nodes spring into place on load and
  // follow a dragged neighbour (see hook doc).
  const { fitView, fitBounds, setViewport } = useReactFlow();
  const store = useStoreApi();
  // Read when a frame is taken, so a click or a hover never moves the view.
  const pinnedRef = useRef<string | null>(null);
  const focusRef = useRef(focusId);
  focusRef.current = focusId;
  const liveNodesRef = useRef(liveNodes);
  liveNodesRef.current = liveNodes;
  /**
   * Frames `bounds` no further out than labels read; when the whole graph
   * does not fit there, centres the selected class, else the focus, else a
   * hub or the class nearest the middle (see framingCentre).
   */
  const frameReadable = useCallback(
    (
      bounds: Rect,
      centres: ReadonlyMap<string, XYPosition>,
      links: readonly LayoutEdge[],
      duration?: number,
    ) => {
      const { width, height, maxZoom } = store.getState();
      const centre = framingCentre(
        centres,
        links,
        [pinnedRef.current, focusRef.current],
        bounds,
      );
      void setViewport(
        readableViewport(
          bounds,
          width,
          height,
          MIN_READABLE_FRAME_ZOOM,
          maxZoom,
          centre,
        ),
        { duration },
      );
    },
    [store, setViewport],
  );
  const simulation = useLiveForceSimulation(
    initialNodes,
    structuralEdges,
    setLiveNodes,
    { enabled: isForce, ...FORCE_OPTIONS, nodeRadius },
  );

  // Re-frame promptly whenever the node SET changes (e.g. a search or expand),
  // by fitting to where the live simulation will SETTLE immediately rather than
  // waiting for the settle animation to finish (otherwise the viewport visibly
  // lags the results). The settle then animates inside the already-correct
  // frame. The packed layout is not that frame: the weak home forces let a
  // dense cluster spread well past it, and fitting to it cut nodes off.
  const nodeSetSig = useMemo(
    () =>
      `${initialNodes.length}:${initialNodes[0]?.id ?? ""}:${
        initialNodes[initialNodes.length - 1]?.id ?? ""
      }`,
    [initialNodes],
  );
  const frame = useMemo(
    () =>
      isForce
        ? settledBounds(initialNodes, structuralEdges, FORCE_OPTIONS)
        : null,
    [isForce, initialNodes, structuralEdges],
  );
  // A readable graph's first frame is the view it opens on, so it is taken at
  // once rather than animated from React Flow's default view.
  const framedRef = useRef(false);
  useEffect(() => {
    if (!frame) return;
    const handle = window.setTimeout(() => {
      if (readable) {
        frameReadable(
          frame,
          centresOf(initialNodes),
          structuralEdges,
          framedRef.current ? 300 : 0,
        );
        framedRef.current = true;
      } else void fitBounds(frame, { padding: 0.1, duration: 300 });
    }, 0);
    return () => window.clearTimeout(handle);
  }, [
    nodeSetSig,
    frame,
    fitBounds,
    readable,
    frameReadable,
    initialNodes,
    structuralEdges,
  ]);

  // Re-fit after the user toggles standalone classes: the graph's extent
  // changes substantially and the old viewport would show half of it.
  const isFirstRender = useRef(true);
  useEffect(() => {
    if (isFirstRender.current) {
      isFirstRender.current = false; // the `fitView` prop handles mount
      return;
    }
    // The node set changed with the toggle, so a readable graph is already
    // reframed above, where it settles; this fit would frame where it starts.
    if (readable) return;
    const handle = window.setTimeout(
      () => void fitView({ padding: 0.1, duration: 250 }),
      0,
    );
    return () => window.clearTimeout(handle);
  }, [showIsolated, fitView, readable]);

  const onNodesChange = useCallback(
    (changes: NodeChange<OntologyFlowNode>[]) =>
      setLiveNodes((nds) => applyNodeChanges(changes, nds)),
    [],
  );

  const handleNodeDragStart = useCallback(
    (_: unknown, node: OntologyFlowNode) =>
      simulation.onDragStart(node.id, node.position),
    [simulation],
  );
  const handleNodeDrag = useCallback(
    (_: unknown, node: OntologyFlowNode) =>
      simulation.onDrag(node.id, node.position),
    [simulation],
  );
  const handleNodeDragStop = useCallback(
    (_: unknown, node: OntologyFlowNode) => simulation.onDragStop(node.id),
    [simulation],
  );

  const handleNodeMouseEnter = useCallback(
    (_: unknown, node: OntologyFlowNode) => setHoveredId(node.id),
    [],
  );
  const handleNodeMouseLeave = useCallback(() => setHoveredId(null), []);

  // Edge-level hover: same focus+context behaviour as hovering the label,
  // but triggered by the edge body itself. Attribute-overlay spoke edges
  // (ids prefixed with `attre:`) are not interactive — ignore them.
  const handleEdgeMouseEnter = useCallback(
    (_: unknown, edge: { id: string }) => {
      if (edge.id.startsWith("attre:")) return;
      setHoveredEdgeId(edge.id);
    },
    [],
  );
  const handleEdgeMouseLeave = useCallback(() => setHoveredEdgeId(null), []);

  // When hover enters/leaves a class, update the collider ref so forceCollide
  // reserves space for its attribute ring + edge-label headroom, then force
  // d3-force to re-read the per-node radii (it caches them at `initialize()`
  // and never re-invokes the accessor per tick) and give the sim a one-shot
  // alpha boost so neighbours actually drift out — then settle naturally on
  // their own without holding alphaTarget above alphaMin. The hovered node
  // itself is pinned at its current position for the duration of the hover
  // so the one-shot kick doesn't drift it out from under the pointer.
  const { refreshCollide, kickAlpha, pinNode, unpinNode } = simulation;
  const pinnedHoverIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (!isForce) return;
    // Always release the previously pinned node unless it is still hovered.
    if (
      pinnedHoverIdRef.current !== null &&
      pinnedHoverIdRef.current !== hoveredId
    ) {
      unpinNode(pinnedHoverIdRef.current);
    }
    if (hoveredId === null) {
      pinnedHoverIdRef.current = null;
      hoverExpansionRef.current = { id: null, radius: 0 };
      refreshCollide();
      kickAlpha(0.15);
      return;
    }
    const host = visibleNodes.find((n) => n.id === hoveredId);
    const attrs = host?.attributes ?? [];
    const ringR =
      attrs.length === 0
        ? NODE_SIZE / 2
        : Math.max(140, ATTR_SIZE + attrs.length * 11);
    // Headroom past the ring for an edge-label worth of space.
    const LABEL_HEADROOM = 90;
    hoverExpansionRef.current = {
      id: hoveredId,
      radius: ringR + LABEL_HEADROOM,
    };
    pinNode(hoveredId);
    pinnedHoverIdRef.current = hoveredId;
    refreshCollide();
    kickAlpha(0.25);
  }, [
    isForce,
    hoveredId,
    visibleNodes,
    refreshCollide,
    kickAlpha,
    pinNode,
    unpinNode,
  ]);

  // The class clicked last keeps its attributes on screen once the pointer
  // leaves it. A caller that controls the selection decides it instead: the
  // last selected id (the one a Shift-click added last).
  const [clickedId, setClickedId] = useState<string | null>(null);
  const pinnedId =
    selectedIds !== undefined ? (selectedIds.at(-1) ?? null) : clickedId;
  pinnedRef.current = pinnedId;

  const handleNodeClick = useCallback(
    (event: ReactMouseEvent, node: OntologyFlowNode) => {
      if (node.type === "ontology") setClickedId(node.id);
      onNodeSelect?.(node.data);
      onSelectionChange?.(
        nextSelection(
          selectedIds ?? [],
          node.id,
          event.shiftKey || event.metaKey || event.ctrlKey,
        ),
      );
    },
    [onNodeSelect, onSelectionChange, selectedIds],
  );

  const handleNodeContextMenu = useCallback(
    (event: ReactMouseEvent, node: OntologyFlowNode) => {
      if (!onNodeContextMenu || node.type !== "ontology") return;
      event.preventDefault();
      onNodeContextMenu(node.data, { x: event.clientX, y: event.clientY });
    },
    [onNodeContextMenu],
  );

  const handleNodeDoubleClick = useCallback(
    (_: unknown, node: OntologyFlowNode) => {
      onNodeExpand?.(node.data);
    },
    [onNodeExpand],
  );

  const handlePaneClick = useCallback(() => {
    setClickedId(null);
    onNodeSelect?.(null);
    onSelectionChange?.([]);
  }, [onNodeSelect, onSelectionChange]);

  // Nodes not connected to the hovered one fade back (delivered via context —
  // see HoverContext).
  const neighbourIds = useMemo(() => {
    if (hoveredId !== null) {
      const ids = new Set<string>([hoveredId]);
      for (const e of edges) {
        if (e.source === hoveredId) ids.add(e.target);
        if (e.target === hoveredId) ids.add(e.source);
      }
      return ids;
    }
    if (hoveredEdgeId !== null) {
      const e = edges.find((edge) => edge.id === hoveredEdgeId);
      if (e) return new Set<string>([e.source, e.target]);
    }
    return null;
  }, [edges, hoveredId, hoveredEdgeId]);
  const hoverContext = useMemo(
    () => ({
      hoveredId,
      neighbourIds,
      setHoveredEdgeId,
      selected: new Set(selectedIds ?? []),
      nodeScale,
    }),
    [hoveredId, neighbourIds, selectedIds, nodeScale],
  );

  // Per-edge label `t` value (position along the chord as a fraction).
  // Topology-only: depends on `edges` and the packed `initialNodes`, NOT on
  // the live sim's positions, so this map is computed once per layout change
  // and never per tick. `FloatingEdge` projects the actual coordinate from
  // the current source/target node centres (and the curve bow for parallel
  // edges), so the label always sits on the drawn edge.
  //
  // Labels around a hub (degree ≥ `HUB_DEGREE_THRESHOLD`) get a staggered t
  // from `T_PATTERN` sorted by the angle of their spoke, so sibling-spoke
  // labels land on different radii from the hub rather than stacking at the
  // midpoint. Non-hub edges keep t = 0.5 (handled by the `?? 0.5` default in
  // `FloatingEdge`), with the curve offset preserving their fan-out.
  const labelTValues = useMemo(() => {
    const ts = new Map<string, number>();
    if (!isForce) return ts;

    const degree = new Map<string, number>();
    for (const e of edges) {
      degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
      degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
    }

    const edgesByHub = new Map<
      string,
      { edgeId: string; angle: number; hubIsSource: boolean }[]
    >();
    // Angle-sort uses the packed layout positions, which are stable across
    // sim ticks. The live sim will drift nodes a little, but the ordering
    // around any given hub stays the same (its spokes never pass each other
    // under the home force), so the stagger stays visually consistent.
    const nodeById = new Map(initialNodes.map((n) => [n.id, n]));

    for (const e of edges) {
      const sd = degree.get(e.source) ?? 0;
      const td = degree.get(e.target) ?? 0;
      const hubId = sd >= td ? e.source : e.target;
      const hubDeg = Math.max(sd, td);
      if (hubDeg < HUB_DEGREE_THRESHOLD) continue;
      // Skip self-loops — they render on top of the node regardless of t.
      if (e.source === e.target) continue;
      const hub = nodeById.get(hubId);
      const otherId = hubId === e.source ? e.target : e.source;
      const other = nodeById.get(otherId);
      if (!hub || !other) continue;
      // Shortest chord where the inner stagger t ≈ 0.35 still lands the
      // label past the hub's own radius + the label half-width. Below this
      // the staggered label sits on top of the hub, worse than the midpoint
      // fallback, so skip the stagger for this edge.
      const chordDx = other.position.x - hub.position.x;
      const chordDy = other.position.y - hub.position.y;
      const chordLen = Math.hypot(chordDx, chordDy);
      if (chordLen < MIN_STAGGER_CHORD) continue;
      const angle = Math.atan2(chordDy, chordDx);
      const list = edgesByHub.get(hubId) ?? [];
      list.push({ edgeId: e.id, angle, hubIsSource: hubId === e.source });
      edgesByHub.set(hubId, list);
    }

    const T_PATTERN = [0.42, 0.58, 0.35, 0.65, 0.5];
    for (const [, list] of edgesByHub) {
      list.sort((a, b) => a.angle - b.angle);
      list.forEach((item, i) => {
        const base = T_PATTERN[i % T_PATTERN.length];
        ts.set(item.edgeId, item.hubIsSource ? base : 1 - base);
      });
    }
    return ts;
  }, [isForce, edges, initialNodes]);

  // Attributes are drawn only for one class, the hovered one or else the one
  // clicked last: a ring of small red nodes around it, wired with red spokes.
  // They live outside the simulation (pure derived state) and are
  // pointer-transparent, so hovering stays on the class.
  const ringId = hoveredId ?? pinnedId;
  const attributeOverlay = useMemo(() => {
    const empty: { nodes: OntologyFlowNode[]; edges: Edge[] } = {
      nodes: [],
      edges: [],
    };
    if (!isForce || ringId === null) return empty;
    const host = liveNodes.find((n) => n.id === ringId);
    const attrs = host?.data.attributes ?? [];
    if (!host || attrs.length === 0) return empty;
    const cx = host.position.x + NODE_SIZE / 2;
    const cy = host.position.y + NODE_SIZE / 2;
    const radius = Math.max(140, ATTR_SIZE + attrs.length * 11);
    const attrColor = KIND_COLORS["datatype-property"].border;
    // A readable graph draws the ring, its class and their neighbours
    // nodeScale times larger: the ring is relaxed at their own size, in a frame
    // shrunk around the class, and then scaled back out.
    const toLocal = (x: number, y: number) => ({
      x: cx + (x - cx) / nodeScale,
      y: cy + (y - cy) / nodeScale,
    });

    // Seed the attributes on a ring around the class, then relax them so they
    // don't overlap other graph nodes or each other — the same "nodes keep
    // their distance" feel as the force layout. Cheap: one class's ring at a time.
    const centres = attrs.map((_, i) => {
      const angle = (2 * Math.PI * i) / attrs.length - Math.PI / 2;
      return {
        x: cx + radius * Math.cos(angle),
        y: cy + radius * Math.sin(angle),
      };
    });
    const obstacles = liveNodes
      .filter((n) => n.id !== ringId)
      .map((n) => ({
        ...toLocal(n.position.x + NODE_SIZE / 2, n.position.y + NODE_SIZE / 2),
        r: NODE_SIZE / 2,
      }));
    // Relationship / FK edges as segments between their endpoints' centres, so
    // attributes can be pushed off the lines too (not just the nodes).
    const centreById = new Map(
      liveNodes.map((n) => [
        n.id,
        toLocal(n.position.x + NODE_SIZE / 2, n.position.y + NODE_SIZE / 2),
      ]),
    );
    const segments = edges
      .map((e) => {
        const a = centreById.get(e.source);
        const b = centreById.get(e.target);
        return a && b ? { ax: a.x, ay: a.y, bx: b.x, by: b.y } : null;
      })
      .filter((s): s is NonNullable<typeof s> => s !== null);
    // Scope label obstacles to the ring host's incident edges only: the
    // visible labels at this zoom are the ones on those edges (focus+context
    // dims the rest to 0.12), so attributes only need to yield around them.
    // Project each label position from the current source/target centres at
    // its topology-derived t, keeping the Bézier bow for curved edges.
    const incidentLabelPositions: { x: number; y: number }[] = [];
    for (const e of edges) {
      if (e.source !== ringId && e.target !== ringId) continue;
      const a = centreById.get(e.source);
      const b = centreById.get(e.target);
      if (!a || !b) continue;
      const t = labelTValues.get(e.id) ?? 0.5;
      const curve = (edgeCurves.get(e.id) ?? 0) / nodeScale;
      incidentLabelPositions.push(
        projectEdgeLabel(a.x, a.y, b.x, b.y, curve, t),
      );
    }
    const attrR = ATTR_SIZE / 2;
    const gap = 12;
    // Approximate half-width of an edge label (chunky CSS block). Used to
    // push attribute glyphs off label positions so they don't share space.
    const LABEL_HALF = 44;
    const push = (
      p: { x: number; y: number },
      ox: number,
      oy: number,
      minDist: number,
      share: number,
    ) => {
      const dx = p.x - ox;
      const dy = p.y - oy;
      const d = Math.hypot(dx, dy) || 0.01;
      if (d < minDist) {
        const f = ((minDist - d) * share) / d;
        p.x += dx * f;
        p.y += dy * f;
      }
    };
    // Push a point off a line SEGMENT (its nearest point), so an attribute
    // never lands on a relationship/FK line.
    const pushOffSegment = (
      p: { x: number; y: number },
      ax: number,
      ay: number,
      bx: number,
      by: number,
      minDist: number,
    ) => {
      const abx = bx - ax;
      const aby = by - ay;
      const len2 = abx * abx + aby * aby || 1;
      const t = Math.max(
        0,
        Math.min(1, ((p.x - ax) * abx + (p.y - ay) * aby) / len2),
      );
      const qx = ax + t * abx;
      const qy = ay + t * aby;
      let dx = p.x - qx;
      let dy = p.y - qy;
      let d = Math.hypot(dx, dy);
      if (d < 0.5) {
        // On the line: shove along the segment's normal to break the tie.
        dx = -aby;
        dy = abx;
        d = Math.hypot(dx, dy) || 1;
      }
      if (d < minDist) {
        const f = (minDist - d) / d;
        p.x += dx * f;
        p.y += dy * f;
      }
    };
    for (let iter = 0; iter < 10; iter++) {
      for (const p of centres) {
        for (const o of obstacles) push(p, o.x, o.y, o.r + attrR + gap, 1);
        for (const s of segments)
          pushOffSegment(p, s.ax, s.ay, s.bx, s.by, attrR + gap);
        // Edge labels on this node's incident edges are ground truth — push
        // attribute glyphs off those label positions. Non-incident labels are
        // dimmed anyway (focus+context) so they do not need to be obstacles.
        for (const lp of incidentLabelPositions) {
          push(p, lp.x, lp.y, LABEL_HALF + attrR + gap, 1);
        }
      }
      for (let i = 0; i < centres.length; i++) {
        for (let j = i + 1; j < centres.length; j++) {
          const a = centres[i];
          const b = centres[j];
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const d = Math.hypot(dx, dy) || 0.01;
          const min = attrR * 2 + gap;
          if (d < min) {
            const f = (min - d) / 2 / d;
            a.x += dx * f;
            a.y += dy * f;
            b.x -= dx * f;
            b.y -= dy * f;
          }
        }
      }
    }

    const nodes: OntologyFlowNode[] = attrs.map((a, i) => {
      return {
        id: `attr:${ringId}:${i}`,
        type: "attribute",
        position: {
          x: cx + (centres[i].x - cx) * nodeScale - ATTR_SIZE / 2,
          y: cy + (centres[i].y - cy) * nodeScale - ATTR_SIZE / 2,
        },
        data: {
          id: `attr:${ringId}:${i}`,
          label: a.label,
          kind: "datatype-property",
        },
        draggable: false,
        selectable: false,
        zIndex: 5,
        style: { pointerEvents: "none" },
      };
    });
    const overlayEdges: Edge[] = attrs.map((a, i) => ({
      id: `attre:${ringId}:${i}`,
      source: ringId,
      target: `attr:${ringId}:${i}`,
      type: "floating",
      zIndex: 5,
      data: { curve: 0 },
      style: { stroke: attrColor, strokeWidth: 1.25 },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: attrColor,
        width: ARROW_SIZE * 0.7,
        height: ARROW_SIZE * 0.7,
        markerUnits: "userSpaceOnUse",
      },
    }));
    return { nodes, edges: overlayEdges };
  }, [isForce, ringId, liveNodes, edges, labelTValues, edgeCurves, nodeScale]);

  const displayNodes = useMemo(
    () =>
      attributeOverlay.nodes.length
        ? [...liveNodes, ...attributeOverlay.nodes]
        : liveNodes,
    [liveNodes, attributeOverlay],
  );

  const displayEdges = useMemo(() => {
    // Pass each edge's topology-derived `labelT` through on its data. The
    // `FloatingEdge` projects the actual coordinate from the current source
    // and target node centres (and the Bézier bow for curved edges), so a
    // sim tick never has to rebuild label positions here — React Flow only
    // re-renders the edges whose endpoint positions moved.
    const base = labelTValues.size
      ? flowEdges.map((e) => {
          const t = labelTValues.get(e.id);
          if (t === undefined) return e;
          return {
            ...e,
            data: { ...e.data, labelT: t },
          };
        })
      : flowEdges;
    return attributeOverlay.edges.length
      ? [...base, ...attributeOverlay.edges]
      : base;
  }, [flowEdges, attributeOverlay, labelTValues]);

  // A previewed change set marks nodes or edges; the legend then explains the marks.
  const highlighted = useMemo(
    () => nodes.some((n) => n.highlight) || edges.some((e) => e.highlight),
    [nodes, edges],
  );

  // Fullscreen the graph via the native Fullscreen API on a wrapper around the
  // canvas, and refit once the size settles.
  const wrapperRef = useRef<HTMLDivElement>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const toggleFullscreen = useCallback(() => {
    const el = wrapperRef.current;
    if (!el) return;
    if (document.fullscreenElement) {
      void document.exitFullscreen();
    } else {
      void el.requestFullscreen?.();
    }
  }, []);
  useEffect(() => {
    const onChange = () => {
      setIsFullscreen(document.fullscreenElement === wrapperRef.current);
      // The pane resizes after the fullscreen transition; reframe once settled.
      window.setTimeout(() => {
        const live = liveNodesRef.current;
        if (readable && live.length > 0)
          frameReadable(boundsOf(live), centresOf(live), structuralEdges);
        else void fitView({ padding: 0.1 });
      }, 60);
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, [fitView, readable, frameReadable, structuralEdges]);

  return (
    <HoverContext.Provider value={hoverContext}>
      <div
        ref={wrapperRef}
        style={{
          width: "100%",
          height: "100%",
          // Transparent so the graph shows the (dark) surrounding container
          // rather than painting its own background. In fullscreen the browser
          // backdrop shows through, which is also dark.
          background: "transparent",
        }}
      >
        <ReactFlow
          nodes={displayNodes}
          edges={displayEdges}
          onNodesChange={onNodesChange}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodeClick={handleNodeClick}
          onNodeDoubleClick={handleNodeDoubleClick}
          onNodeContextMenu={
            onNodeContextMenu ? handleNodeContextMenu : undefined
          }
          onNodeDragStart={handleNodeDragStart}
          onNodeDrag={handleNodeDrag}
          onNodeDragStop={handleNodeDragStop}
          onNodeMouseEnter={handleNodeMouseEnter}
          onNodeMouseLeave={handleNodeMouseLeave}
          onEdgeMouseEnter={handleEdgeMouseEnter}
          onEdgeMouseLeave={handleEdgeMouseLeave}
          onPaneClick={handlePaneClick}
          fitView={!readable}
          fitViewOptions={FIT_OPTIONS}
          minZoom={0.15}
          maxZoom={2.5}
          nodesDraggable
          proOptions={{ hideAttribution: true }}
        >
          <Background variant={BackgroundVariant.Dots} gap={16} size={1} />
          <Controls showInteractive={false} />
          <Panel position="top-left">
            <button
              type="button"
              onClick={toggleFullscreen}
              aria-label={
                isFullscreen ? t("Exit full screen") : t("Full screen")
              }
              title={isFullscreen ? t("Exit full screen") : t("Full screen")}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                background: "white",
                border: "1px solid #e5e7eb",
                borderRadius: 6,
                padding: "6px 10px",
                fontSize: 11,
                color: "#000000",
                cursor: "pointer",
              }}
            >
              <span aria-hidden="true">{isFullscreen ? "🡖" : "⛶"}</span>
              {isFullscreen ? t("Exit full screen") : t("Full screen")}
            </button>
          </Panel>
          <MiniMap
            nodeColor={(n) => {
              const mark = n.data?.highlight as GraphHighlight | undefined;
              if (mark) return HIGHLIGHT_COLORS[mark].border;
              const kind =
                typeof n.data?.kind === "string" ? n.data.kind : "unknown";
              return KIND_COLORS[kind]?.border ?? "#9ca3af";
            }}
            style={{ height: 80, width: 120 }}
          />
          <Panel position="top-right">
            <div
              style={{
                background: "white",
                borderRadius: 6,
                padding: "8px 12px",
                fontSize: 11,
                color: "#000000",
                border: "1px solid #e5e7eb",
              }}
            >
              {[
                ...LEGEND_ITEMS.map(({ kind, label }) => ({
                  key: kind,
                  label,
                  color: KIND_COLORS[kind].border,
                })),
                ...(highlighted
                  ? Object.entries(HIGHLIGHT_COLORS).map(([key, c]) => ({
                      key,
                      label: c.label,
                      color: c.border,
                    }))
                  : []),
              ].map(({ key, label, color }) => (
                <div
                  key={key}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    marginBottom: 3,
                  }}
                >
                  <span
                    style={{
                      width: 10,
                      height: 10,
                      borderRadius: "50%",
                      background: color,
                      display: "inline-block",
                    }}
                  />
                  <span>{t(label)}</span>
                </div>
              ))}
              {isolated.size > 0 && (
                <div
                  style={{
                    marginTop: 8,
                    paddingTop: 8,
                    borderTop: "1px solid #e5e7eb",
                  }}
                >
                  <Toggle
                    checked={showIsolated}
                    onChange={({ detail }) => setShowIsolated(detail.checked)}
                  >
                    {isolated.size === 1
                      ? t("Show {count} standalone class", {
                          count: isolated.size,
                        })
                      : t("Show {count} standalone classes", {
                          count: isolated.size,
                        })}
                  </Toggle>
                </div>
              )}
            </div>
          </Panel>
        </ReactFlow>
      </div>
    </HoverContext.Provider>
  );
}

export function OntologyGraphFlow(props: Props) {
  return (
    <ReactFlowProvider>
      <OntologyGraphFlowInner {...props} />
    </ReactFlowProvider>
  );
}
