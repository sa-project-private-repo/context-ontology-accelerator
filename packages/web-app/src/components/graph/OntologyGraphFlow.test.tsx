// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  act,
} from "@testing-library/react";
import { describe, it, expect, vi, onTestFinished } from "vitest";
import { getViewportForBounds } from "@xyflow/react";
import {
  OntologyGraphFlow,
  framingCentre,
  MAX_NODE_SCALE,
  MIN_READABLE_FRAME_ZOOM,
  nextSelection,
  parallelEdgeCurves,
  radialPositions,
  READABLE_ZOOM,
  readableNodeScale,
  readableViewport,
  type GraphNodeData,
  type GraphEdgeData,
} from "./OntologyGraphFlow";
import { layoutForceGraph } from "./layout";

// React Flow needs ResizeObserver
class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver =
  ResizeObserver as unknown as typeof globalThis.ResizeObserver;

const nodes: GraphNodeData[] = [
  { id: "ex:Claim", label: "Claim", kind: "class" },
  { id: "ex:Policy", label: "Policy", kind: "grounded" },
  { id: "ex:hasPolicy", label: "has policy", kind: "object-property" },
];

const edges: GraphEdgeData[] = [
  { id: "e1", source: "ex:Claim", target: "ex:Policy", label: "hasPolicy" },
];

describe("OntologyGraphFlow", () => {
  it("renders without crashing", () => {
    const { container } = render(
      <OntologyGraphFlow nodes={nodes} edges={edges} />,
    );
    expect(container.querySelector(".react-flow")).toBeTruthy();
  });

  it("renders nodes with labels", () => {
    render(<OntologyGraphFlow nodes={nodes} edges={edges} />);
    expect(screen.getByText("Claim")).toBeTruthy();
    expect(screen.getByText("Policy")).toBeTruthy();
  });

  it("calls onNodeSelect when a node is clicked", () => {
    const onNodeSelect = vi.fn();
    render(
      <OntologyGraphFlow
        nodes={nodes}
        edges={edges}
        onNodeSelect={onNodeSelect}
      />,
    );
    fireEvent.click(screen.getByText("Claim"));
    expect(onNodeSelect).toHaveBeenCalledWith(
      expect.objectContaining({ id: "ex:Claim" }),
    );
  });

  it("renders with empty nodes/edges", () => {
    const { container } = render(<OntologyGraphFlow nodes={[]} edges={[]} />);
    expect(container.querySelector(".react-flow")).toBeTruthy();
  });

  it("lays out edge-less nodes on a circle (not a single row)", () => {
    const pts = radialPositions(6);
    // A flat row collapses to one y; a circle uses many distinct y values.
    expect(new Set(pts.map((p) => Math.round(p.y))).size).toBeGreaterThan(1);
    // All points are roughly equidistant from the centre (on a circle).
    const radii = pts.map((p) => Math.hypot(p.x, p.y));
    const maxR = Math.max(...radii);
    const minR = Math.min(...radii);
    expect(maxR - minR).toBeLessThan(1);
  });

  it("places a single node at the origin", () => {
    expect(radialPositions(1)).toEqual([{ x: 0, y: 0 }]);
  });

  it("calls onNodeExpand when a node is double-clicked", () => {
    const onNodeExpand = vi.fn();
    render(
      <OntologyGraphFlow
        nodes={nodes}
        edges={edges}
        onNodeExpand={onNodeExpand}
      />,
    );
    fireEvent.doubleClick(screen.getByText("Claim"));
    expect(onNodeExpand).toHaveBeenCalledWith(
      expect.objectContaining({ id: "ex:Claim" }),
    );
  });

  it("shows an expand affordance for collapsed nodes with children", () => {
    const withChildren: GraphNodeData[] = [
      { id: "ex:Claim", label: "Claim", kind: "class", hasChildren: true },
    ];
    render(<OntologyGraphFlow nodes={withChildren} edges={[]} />);
    expect(screen.getByText("+")).toBeTruthy();
  });

  it("shows a collapse affordance for expanded nodes", () => {
    const expandedNode: GraphNodeData[] = [
      {
        id: "ex:Claim",
        label: "Claim",
        kind: "class",
        hasChildren: true,
        expanded: true,
      },
    ];
    render(<OntologyGraphFlow nodes={expandedNode} edges={[]} />);
    expect(screen.getByText("−")).toBeTruthy();
  });

  describe("force layout", () => {
    const schema: GraphNodeData[] = [
      { id: "ex:A", label: "A", kind: "class" },
      { id: "ex:B", label: "B", kind: "class" },
      { id: "ex:Lonely", label: "Lonely", kind: "class" },
      { id: "ex:Alone", label: "Alone", kind: "class" },
    ];
    const fk: GraphEdgeData[] = [
      { id: "e1", source: "ex:A", target: "ex:B", label: "b_id" },
    ];

    it("hides standalone classes by default and offers a toggle with the count", () => {
      render(<OntologyGraphFlow nodes={schema} edges={fk} layout="force" />);
      expect(screen.getByText("A")).toBeTruthy();
      expect(screen.queryByText("Lonely")).toBeNull();
      expect(screen.queryByText("Alone")).toBeNull();
      expect(screen.getByText(/Show 2 standalone classes/)).toBeTruthy();
    });

    it("reveals standalone classes when the toggle is switched on", () => {
      render(<OntologyGraphFlow nodes={schema} edges={fk} layout="force" />);
      fireEvent.click(screen.getByRole("checkbox"));
      expect(screen.getByText("Lonely")).toBeTruthy();
      expect(screen.getByText("Alone")).toBeTruthy();
    });

    it("does not offer the toggle in hierarchical mode (explorer root view is edge-less)", () => {
      render(<OntologyGraphFlow nodes={schema} edges={[]} />);
      expect(screen.getByText("Lonely")).toBeTruthy();
      expect(screen.queryByText(/standalone/)).toBeNull();
    });

    it("with collapseStandalone=false, shows standalone classes by default but still offers the toggle", () => {
      render(
        <OntologyGraphFlow
          nodes={schema}
          edges={fk}
          layout="force"
          collapseStandalone={false}
        />,
      );
      // Curated nodes are visible from the start …
      expect(screen.getByText("Lonely")).toBeTruthy();
      expect(screen.getByText("Alone")).toBeTruthy();
      // … and the toggle is still available to hide them.
      expect(screen.getByText(/Show 2 standalone classes/)).toBeTruthy();
    });

    describe("attribute ring", () => {
      const withAttrs: GraphNodeData[] = [
        {
          id: "ex:A",
          label: "A",
          kind: "class",
          attributes: [{ label: "a_status" }],
        },
        {
          id: "ex:B",
          label: "B",
          kind: "class",
          attributes: [{ label: "b_total" }],
        },
      ];
      // React Flow's wrapper of a node, which gets the pointer handlers.
      const wrapperOf = (label: string) =>
        screen.getByText(label).closest(".react-flow__node") as Element;

      it("shows a hovered class's attributes and hides them when the pointer leaves", () => {
        render(
          <OntologyGraphFlow nodes={withAttrs} edges={fk} layout="force" />,
        );
        fireEvent.mouseEnter(wrapperOf("A"));
        expect(screen.getByText("a_status")).toBeTruthy();
        fireEvent.mouseLeave(wrapperOf("A"));
        expect(screen.queryByText("a_status")).toBeNull();
      });

      it("keeps a clicked class's attributes after the pointer leaves", () => {
        render(
          <OntologyGraphFlow nodes={withAttrs} edges={fk} layout="force" />,
        );
        fireEvent.mouseEnter(wrapperOf("A"));
        fireEvent.click(screen.getByText("A"));
        fireEvent.mouseLeave(wrapperOf("A"));
        expect(screen.getByText("a_status")).toBeTruthy();
      });

      it("drops the pinned attributes on a pane click", () => {
        const { container } = render(
          <OntologyGraphFlow nodes={withAttrs} edges={fk} layout="force" />,
        );
        fireEvent.click(screen.getByText("A"));
        expect(screen.getByText("a_status")).toBeTruthy();
        fireEvent.click(
          container.querySelector(".react-flow__pane") as Element,
        );
        expect(screen.queryByText("a_status")).toBeNull();
      });

      it("follows the controlled selection, its last class first", () => {
        const { rerender } = render(
          <OntologyGraphFlow
            nodes={withAttrs}
            edges={fk}
            layout="force"
            selectedIds={["ex:B", "ex:A"]}
            onSelectionChange={() => {}}
          />,
        );
        expect(screen.getByText("a_status")).toBeTruthy();
        expect(screen.queryByText("b_total")).toBeNull();
        rerender(
          <OntologyGraphFlow
            nodes={withAttrs}
            edges={fk}
            layout="force"
            selectedIds={[]}
            onSelectionChange={() => {}}
          />,
        );
        expect(screen.queryByText("a_status")).toBeNull();
      });

      it("shows the hovered class's attributes over the pinned ones", () => {
        render(
          <OntologyGraphFlow
            nodes={withAttrs}
            edges={fk}
            layout="force"
            selectedIds={["ex:A"]}
            onSelectionChange={() => {}}
          />,
        );
        fireEvent.mouseEnter(wrapperOf("B"));
        expect(screen.getByText("b_total")).toBeTruthy();
        expect(screen.queryByText("a_status")).toBeNull();
        fireEvent.mouseLeave(wrapperOf("B"));
        expect(screen.getByText("a_status")).toBeTruthy();
      });
    });
  });

  it("draws a node badge with its accessible title", () => {
    render(
      <OntologyGraphFlow
        nodes={[
          {
            id: "ex:Claim",
            label: "Claim",
            kind: "class",
            badge: { text: "12", title: "12 instances", dimmed: true },
          },
        ]}
        edges={[]}
      />,
    );
    const badge = screen.getByLabelText("12 instances");
    expect(badge.textContent).toBe("12");
    expect(badge.style.opacity).toBe("0.45");
  });

  it("marks preview highlights on nodes and edges and lists them in the legend", () => {
    const { container, rerender } = render(
      <OntologyGraphFlow
        nodes={[
          { ...nodes[0], highlight: "added" },
          { ...nodes[1], highlight: "removed" },
        ]}
        edges={[{ ...edges[0], highlight: "removed" }]}
      />,
    );
    const mark = (label: string) =>
      screen
        .getByText(label)
        .closest("[data-highlight]")
        ?.getAttribute("data-highlight");
    expect(mark("Claim")).toBe("added");
    expect(mark("Policy")).toBe("removed");
    for (const word of ["Added", "Changed", "Removed"]) {
      expect(screen.getByText(word)).toBeTruthy();
    }
    rerender(<OntologyGraphFlow nodes={nodes} edges={edges} />);
    expect(screen.queryByText("Added")).toBeNull();
    expect(container.querySelector("[data-highlight]")).toBeNull();
  });

  it("selects one node on click and toggles with Shift", () => {
    const onSelectionChange = vi.fn();
    const { rerender } = render(
      <OntologyGraphFlow
        nodes={nodes}
        edges={edges}
        selectedIds={["ex:Claim"]}
        onSelectionChange={onSelectionChange}
      />,
    );
    fireEvent.click(screen.getByText("Policy"), { shiftKey: true });
    expect(onSelectionChange).toHaveBeenLastCalledWith([
      "ex:Claim",
      "ex:Policy",
    ]);
    fireEvent.click(screen.getByText("Policy"));
    expect(onSelectionChange).toHaveBeenLastCalledWith(["ex:Policy"]);
    rerender(
      <OntologyGraphFlow
        nodes={nodes}
        edges={edges}
        selectedIds={["ex:Claim", "ex:Policy"]}
        onSelectionChange={onSelectionChange}
      />,
    );
    const selectedOf = (label: string) =>
      screen
        .getByText(label)
        .closest("[data-selected]")
        ?.getAttribute("data-selected");
    expect(selectedOf("Claim")).toBe("true");
    expect(selectedOf("Policy")).toBe("true");
    rerender(
      <OntologyGraphFlow
        nodes={nodes}
        edges={edges}
        selectedIds={["ex:Claim"]}
        onSelectionChange={onSelectionChange}
      />,
    );
    expect(selectedOf("Claim")).toBe("true");
    expect(selectedOf("Policy")).toBeUndefined();
  });

  it("clears the selection when the pane is clicked", () => {
    const onSelectionChange = vi.fn();
    const { container } = render(
      <OntologyGraphFlow
        nodes={nodes}
        edges={edges}
        selectedIds={["ex:Claim"]}
        onSelectionChange={onSelectionChange}
      />,
    );
    const pane = container.querySelector(".react-flow__pane");
    expect(pane).toBeTruthy();
    fireEvent.click(pane as Element);
    expect(onSelectionChange).toHaveBeenLastCalledWith([]);
  });

  it("reports a right-click with the node and the pointer position", () => {
    const onNodeContextMenu = vi.fn();
    render(
      <OntologyGraphFlow
        nodes={nodes}
        edges={edges}
        onNodeContextMenu={onNodeContextMenu}
      />,
    );
    const notPrevented = fireEvent.contextMenu(screen.getByText("Claim"), {
      clientX: 40,
      clientY: 60,
    });
    expect(onNodeContextMenu).toHaveBeenCalledWith(
      expect.objectContaining({ id: "ex:Claim" }),
      { x: 40, y: 60 },
    );
    // The callback owns the right-click, so the browser menu is suppressed.
    expect(notPrevented).toBe(false);
  });

  it("changes nothing for callers that pass none of the new props", () => {
    const { container } = render(
      <OntologyGraphFlow nodes={nodes} edges={edges} />,
    );
    // The browser menu is left alone (the dispatch is not default-prevented).
    expect(fireEvent.contextMenu(screen.getByText("Claim"))).toBe(true);
    expect(container.querySelector("[data-selected]")).toBeNull();
    expect(container.querySelector(".coa-graph-node-badge")).toBeNull();
  });

  it("computes the next selection", () => {
    expect(nextSelection(["a"], "b", false)).toEqual(["b"]);
    expect(nextSelection(["a"], "b", true)).toEqual(["a", "b"]);
    expect(nextSelection(["a", "b"], "a", true)).toEqual(["b"]);
  });
});

describe("parallelEdgeCurves", () => {
  it("fans edges sharing a node pair symmetrically and leaves lone edges straight", () => {
    const edges: GraphEdgeData[] = [
      { id: "1", source: "colour", target: "hero" },
      { id: "2", source: "colour", target: "hero" },
      { id: "3", source: "colour", target: "hero" },
      { id: "4", source: "colour", target: "gender" },
    ];
    // Lone edge is straight; the triple fans out 44px apart around zero.
    expect(parallelEdgeCurves(edges).map((c) => c + 0)).toEqual([
      -44, 0, 44, 0,
    ]);
  });

  it("puts a reversed edge on the opposite side of its forward twin", () => {
    const edges: GraphEdgeData[] = [
      { id: "1", source: "a", target: "b" },
      { id: "2", source: "b", target: "a" },
    ];
    const [forward, reverse] = parallelEdgeCurves(edges);
    // Each edge's normal is derived from its own direction, so equal-signed
    // offsets would land on the same side; the sign flip separates them.
    expect(forward).toBe(-22);
    expect(reverse).toBe(-22);
  });
});

describe("readable labels", () => {
  /** A chain of `n` classes: from ten on, it settles far wider than a readable
   * zoom shows in the 500px pane React Flow falls back to under happy-dom. */
  function chain(n: number): {
    nodes: GraphNodeData[];
    edges: GraphEdgeData[];
  } {
    return {
      nodes: Array.from({ length: n }, (_, i) => ({
        id: `ex:N${i}`,
        label: `N${i}`,
        kind: "class" as const,
      })),
      edges: Array.from({ length: n - 1 }, (_, i) => ({
        id: `l${i}`,
        source: `ex:N${i}`,
        target: `ex:N${i + 1}`,
      })),
    };
  }

  /** The graph's viewport: its pan and its zoom. */
  function viewportOf(container: HTMLElement) {
    const transform = (
      container.querySelector(".react-flow__viewport") as HTMLElement
    ).style.transform;
    const pan = /translate\(([-\d.e]+)px,\s*([-\d.e]+)px\)/.exec(transform);
    const zoom = /scale\(([-\d.e]+)\)/.exec(transform);
    return {
      x: Number(pan?.[1]),
      y: Number(pan?.[2]),
      zoom: Number(zoom?.[1]),
    };
  }

  const settle = () => act(() => new Promise((r) => setTimeout(r, 600)));

  /**
   * Has the graph's nodes measured as a browser does, which happy-dom never
   * does: each is 64px square, and the ResizeObserver reports them together a
   * moment after they mount, once the graph has taken its own frame. React
   * Flow then measures its nodes, and fits the view then if its `fitView` prop
   * asked it to. Returns the function that undoes this.
   */
  function measureNodesLikeABrowser(): () => void {
    const proto = HTMLElement.prototype;
    const sizes = (["offsetWidth", "offsetHeight"] as const).map((key) => {
      const own = Object.getOwnPropertyDescriptor(proto, key);
      Object.defineProperty(proto, key, {
        configurable: true,
        get(this: HTMLElement) {
          return this.classList.contains("react-flow__node")
            ? 64
            : own?.get?.call(this);
        },
      });
      return { key, own };
    });
    const Observer = globalThis.ResizeObserver;
    class MeasuringObserver {
      private pending: Element[] = [];
      private timer: ReturnType<typeof setTimeout> | undefined;
      constructor(private readonly report: ResizeObserverCallback) {}
      observe(target: Element) {
        // Only the nodes: other observers (Cloudscape's) read more of an entry.
        if (!target.classList.contains("react-flow__node")) return;
        this.pending.push(target);
        this.timer ??= setTimeout(() => {
          const targets = this.pending;
          this.pending = [];
          this.timer = undefined;
          this.report(
            targets.map((t) => ({ target: t }) as ResizeObserverEntry),
            this as unknown as globalThis.ResizeObserver,
          );
        }, 20);
      }
      unobserve() {}
      disconnect() {
        clearTimeout(this.timer);
      }
    }
    globalThis.ResizeObserver =
      MeasuringObserver as unknown as typeof globalThis.ResizeObserver;
    return () => {
      globalThis.ResizeObserver = Observer;
      for (const { key, own } of sizes) {
        if (own) Object.defineProperty(proto, key, own);
      }
    };
  }

  it("draws nodes larger only below the readable zoom, and at most MAX_NODE_SCALE times", () => {
    expect(readableNodeScale(1.5)).toBe(1);
    expect(readableNodeScale(READABLE_ZOOM)).toBe(1);
    // In steps of a twentieth.
    expect(readableNodeScale(0.53)).toBe(1.5);
    expect(readableNodeScale(0.45)).toBe(1.8);
    expect(readableNodeScale(0.1)).toBe(MAX_NODE_SCALE);
    // At the readable frame's floor the enlarged labels read as at READABLE_ZOOM.
    expect(readableNodeScale(MIN_READABLE_FRAME_ZOOM)).toBe(MAX_NODE_SCALE);
  });

  it("frames the whole graph as React Flow does when it fits at a readable zoom", () => {
    const bounds = { x: 0, y: 0, width: 400, height: 300 };
    expect(
      readableViewport(bounds, 800, 600, MIN_READABLE_FRAME_ZOOM, 2.5, {
        x: 0,
        y: 0,
      }),
    ).toEqual(getViewportForBounds(bounds, 800, 600, 0, 2.5, 0.1));
  });

  it("stops at the floor and centres the given point when the graph does not fit", () => {
    const bounds = { x: 0, y: 0, width: 10000, height: 6000 };
    const at = readableViewport(bounds, 800, 600, 0.4, 2.5, {
      x: 7000,
      y: 2000,
    });
    expect(at.zoom).toBe(0.4);
    // The middle of the pane shows the point.
    expect((400 - at.x) / at.zoom).toBeCloseTo(7000);
    expect((300 - at.y) / at.zoom).toBeCloseTo(2000);
    const middle = readableViewport(bounds, 800, 600, 0.4, 2.5, null);
    expect((400 - middle.x) / middle.zoom).toBeCloseTo(5000);
    expect((300 - middle.y) / middle.zoom).toBeCloseTo(3000);
  });

  it("keeps the view on the graph near its edge, with the fit's padding", () => {
    const bounds = { x: 0, y: 0, width: 10000, height: 6000 };
    // A point near the top right corner: the view stops at those edges.
    const at = readableViewport(bounds, 800, 600, 0.4, 2.5, {
      x: 9990,
      y: 10,
    });
    const right = (800 - at.x) / at.zoom;
    const top = -at.y / at.zoom;
    // React Flow's padding 0.1: the bounds take 1/1.1 of the view.
    expect(right - 10000).toBeCloseTo((2000 - 2000 / 1.1) / 2);
    expect(0 - top).toBeCloseTo((1500 - 1500 / 1.1) / 2);
    // A graph wider than the view but not taller is centred top to bottom.
    const flat = readableViewport(
      { x: 0, y: 0, width: 10000, height: 600 },
      800,
      600,
      0.4,
      2.5,
      { x: 5000, y: 0 },
    );
    expect((300 - flat.y) / flat.zoom).toBeCloseTo(300);
  });

  it("centres the selected class, else the focus, else a hub, else the class nearest the middle", () => {
    const at = (x: number) => ({ x, y: 0 });
    const centres = new Map([
      ["a", at(0)],
      ["b", at(500)],
      ["c", at(1000)],
      ["d", at(1500)],
      ["e", at(2000)],
      ["f", at(2500)],
    ]);
    const bounds = { x: -32, y: -32, width: 2564, height: 64 };
    const line = [
      { source: "a", target: "b" },
      { source: "b", target: "c" },
    ];
    expect(framingCentre(centres, line, ["c", "a"], bounds)).toEqual(at(1000));
    expect(framingCentre(centres, line, ["gone", "a"], bounds)).toEqual(at(0));
    // No class has the five edges of a hub: the one nearest the middle.
    expect(framingCentre(centres, line, [null, undefined], bounds)).toEqual(
      at(1000),
    );
    const star = ["a", "b", "c", "d", "e"].map((id) => ({
      source: "f",
      target: id,
    }));
    expect(framingCentre(centres, star, [null], bounds)).toEqual(at(2500));
    expect(framingCentre(new Map(), [], [], bounds)).toBeNull();
  });

  it("opens a graph too large to read whole at the readable floor, its nodes drawn larger", async () => {
    const { nodes, edges } = chain(10);
    const { container } = render(
      <OntologyGraphFlow
        nodes={nodes}
        edges={edges}
        layout="force"
        readableLabels
      />,
    );
    await waitFor(
      () =>
        expect(viewportOf(container).zoom).toBeCloseTo(MIN_READABLE_FRAME_ZOOM),
      { timeout: 3000 },
    );
    const node = screen.getByText("N0").closest(".coa-graph-node");
    expect((node as HTMLElement).style.scale).toBe(String(MAX_NODE_SCALE));
  });

  it("leaves other graphs as they were: fitted whole, their nodes not enlarged", async () => {
    const { nodes, edges } = chain(10);
    const { container } = render(
      <OntologyGraphFlow nodes={nodes} edges={edges} layout="force" />,
    );
    await waitFor(
      () =>
        expect(viewportOf(container).zoom).toBeLessThan(
          MIN_READABLE_FRAME_ZOOM,
        ),
      { timeout: 3000 },
    );
    const node = screen.getByText("N0").closest(".coa-graph-node");
    expect((node as HTMLElement).style.scale).toBe("");
  });

  it("takes its first frame at once, not animated from React Flow's default view", async () => {
    const { nodes, edges } = chain(20);
    const { container } = render(
      <OntologyGraphFlow
        nodes={nodes}
        edges={edges}
        layout="force"
        readableLabels
      />,
    );
    // Read just after the frame is taken, before an animation's first step.
    const zoom = await new Promise<number>((resolve) =>
      setTimeout(() => resolve(viewportOf(container).zoom), 0),
    );
    expect(zoom).toBeCloseTo(MIN_READABLE_FRAME_ZOOM);
  });

  it("brings the selected class into view when the graph does not fit, once its nodes are measured", async () => {
    // React Flow's own fit on mount, which waits for the nodes to be measured,
    // must not replace the frame afterwards.
    const restore = measureNodesLikeABrowser();
    onTestFinished(restore);
    const { nodes, edges } = chain(20);
    /** Whether the pane shows where the layout puts the chain's last class. */
    const inView = (container: HTMLElement) => {
      const size = parseFloat(
        (screen.getByText("N19").closest(".coa-graph-node") as HTMLElement)
          .style.width,
      );
      const home = layoutForceGraph(
        nodes.map((n) => n.id),
        edges,
        { nodeSize: size },
      ).get("ex:N19");
      const { x, y, zoom } = viewportOf(container);
      const sx = ((home?.x ?? NaN) + size / 2) * zoom + x;
      const sy = ((home?.y ?? NaN) + size / 2) * zoom + y;
      return sx > 0 && sx < 500 && sy > 0 && sy < 500;
    };
    const readable = async (selected: string[]) => {
      const view = render(
        <OntologyGraphFlow
          nodes={nodes}
          edges={edges}
          layout="force"
          readableLabels
          selectedIds={selected}
          onSelectionChange={() => {}}
        />,
      );
      await waitFor(
        () =>
          expect(viewportOf(view.container).zoom).toBeCloseTo(
            MIN_READABLE_FRAME_ZOOM,
          ),
        { timeout: 3000 },
      );
      await settle();
      return view;
    };
    const unselected = await readable([]);
    expect(inView(unselected.container)).toBe(false);
    unselected.unmount();
    const selected = await readable(["ex:N19"]);
    expect(inView(selected.container)).toBe(true);
  });

  it("frames at a readable zoom again in and out of full screen", async () => {
    const { nodes, edges } = chain(20);
    const { container } = render(
      <OntologyGraphFlow
        nodes={nodes}
        edges={edges}
        layout="force"
        readableLabels
      />,
    );
    await waitFor(
      () =>
        expect(viewportOf(container).zoom).toBeCloseTo(MIN_READABLE_FRAME_ZOOM),
      { timeout: 3000 },
    );
    await settle();
    const zoomOut = container.querySelector(
      ".react-flow__controls-zoomout",
    ) as Element;
    fireEvent.click(zoomOut);
    fireEvent.click(zoomOut);
    expect(viewportOf(container).zoom).toBeLessThan(MIN_READABLE_FRAME_ZOOM);
    act(() => {
      document.dispatchEvent(new Event("fullscreenchange"));
    });
    await waitFor(() =>
      expect(viewportOf(container).zoom).toBeCloseTo(MIN_READABLE_FRAME_ZOOM),
    );
  });

  it("frames where the graph settles, at a readable zoom, after the standalone toggle", async () => {
    const { nodes, edges } = chain(10);
    const lonely: GraphNodeData[] = ["L1", "L2", "L3"].map((l) => ({
      id: `ex:${l}`,
      label: l,
      kind: "class",
    }));
    const { container } = render(
      <OntologyGraphFlow
        nodes={[...nodes, ...lonely]}
        edges={edges}
        layout="force"
        collapseStandalone={false}
        readableLabels
      />,
    );
    await waitFor(
      () =>
        expect(viewportOf(container).zoom).toBeCloseTo(MIN_READABLE_FRAME_ZOOM),
      { timeout: 3000 },
    );
    await settle();
    // Hide the standalone classes: the chain alone still does not fit.
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.queryByText("L1")).toBeNull();
    await settle();
    expect(viewportOf(container).zoom).toBeCloseTo(MIN_READABLE_FRAME_ZOOM);
  });
});
