// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  act,
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import {
  describe,
  it,
  expect,
  vi,
  beforeEach,
  afterEach,
  onTestFinished,
} from "vitest";
import { StudioGraph, FULL_GRAPH_LIMIT } from "./StudioGraph";
import { toFlowEdges, toFlowNodes } from "./studio-helpers";
import type { StudioGraph as StudioGraphData } from "../../../services/studio";
import { MIN_READABLE_FRAME_ZOOM } from "@components/graph/OntologyGraphFlow";
import { layoutForceGraph } from "@components/graph/layout";

// Counts how often the node array is rebuilt (a rebuild re-runs the layout).
vi.mock("./studio-helpers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./studio-helpers")>();
  return { ...actual, toFlowNodes: vi.fn(actual.toFlowNodes) };
});

class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver =
  ResizeObserver as unknown as typeof globalThis.ResizeObserver;

/**
 * Has the graph's nodes measured as a browser does, which happy-dom never
 * does: each is 64px square, and the ResizeObserver reports them together a
 * moment after they mount. React Flow then measures its nodes, and fits the
 * view then if its `fitView` prop asked it to. Returns the function that
 * undoes this.
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
    globalThis.ResizeObserver =
      ResizeObserver as unknown as typeof globalThis.ResizeObserver;
    for (const { key, own } of sizes) {
      if (own) Object.defineProperty(proto, key, own);
    }
  };
}

const CUSTOMER = "http://e/o#Customer";
const FIBO = "https://spec.edmcouncil.org/fibo/Agent";

function graph(overrides: Partial<StudioGraphData> = {}): StudioGraphData {
  return {
    revision: 2,
    mode: "full",
    focus: null,
    totalClasses: 2,
    truncated: false,
    nodes: [
      {
        iri: CUSTOMER,
        label: "Customer",
        grounded: false,
        editable: true,
        attributes: [],
        attributeCount: 0,
        aboxCount: 12,
        afterDeploy: false,
      },
      {
        iri: FIBO,
        label: "Agent",
        grounded: false,
        editable: false,
        attributes: [],
        attributeCount: 0,
        aboxCount: null,
        afterDeploy: true,
      },
    ],
    edges: [
      { id: "sub|c|a", kind: "subClassOf", source: CUSTOMER, target: FIBO },
    ],
    abox: {
      status: "READY",
      current: true,
      conformsToVersion: 3,
      activeVersion: 3,
      reason: null,
    },
    ...overrides,
  };
}

const props = () => ({
  selected: [] as string[],
  editable: true,
  onSelectionChange: vi.fn(),
  onFocus: vi.fn(),
  onAction: vi.fn(),
  search: vi.fn().mockResolvedValue([]),
});

/** The React Flow wrapper of a node: the element keyboard focus lives on. */
function flowNode(container: HTMLElement, iri: string): HTMLElement {
  const found = Array.from(
    container.querySelectorAll<HTMLElement>(".react-flow__node"),
  ).find((el) => el.getAttribute("data-id") === iri);
  if (!found) throw new Error(`no flow node for ${iri}`);
  return found;
}

function openMenu(label: string) {
  fireEvent.contextMenu(screen.getByText(label), { clientX: 5, clientY: 5 });
  return screen.getByRole("menu", { name: `Actions for ${label}` });
}

describe("StudioGraph", () => {
  beforeEach(() => vi.clearAllMocks());

  it("draws the classes with their ABox badges", () => {
    render(<StudioGraph graph={graph()} {...props()} />);
    expect(screen.getByText("Customer")).toBeTruthy();
    expect(screen.getByLabelText(/12 instances/).textContent).toBe("12");
    expect(screen.getByLabelText(/Filled after deploy/).textContent).toBe(
      "new",
    );
  });

  it("builds the node array again only when the graph changes", () => {
    const p = props();
    const g = graph();
    const { rerender } = render(<StudioGraph graph={g} {...p} />);
    expect(toFlowNodes).toHaveBeenCalledTimes(1);
    // Unrelated renders: a new callback, an open and a closed menu.
    rerender(<StudioGraph graph={g} {...p} onAction={vi.fn()} />);
    openMenu("Customer");
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(toFlowNodes).toHaveBeenCalledTimes(1);
    // A new graph response (an ABox change, say) is what rebuilds it.
    rerender(<StudioGraph graph={graph({ revision: 3 })} {...p} />);
    expect(toFlowNodes).toHaveBeenCalledTimes(2);
  });

  it("draws the selection it is given and reports clicks and double-clicks", () => {
    const p = props();
    const { container } = render(
      <StudioGraph graph={graph()} {...p} selected={[CUSTOMER]} />,
    );
    expect(container.querySelectorAll('[data-selected="true"]')).toHaveLength(
      1,
    );
    fireEvent.click(screen.getByText("Agent"));
    expect(p.onSelectionChange).toHaveBeenLastCalledWith([FIBO]);
    fireEvent.doubleClick(screen.getByText("Customer"));
    expect(p.onFocus).toHaveBeenCalledWith(CUSTOMER);
  });

  describe("context menu", () => {
    it("offers the edit actions on right-click and reports the chosen one", () => {
      const p = props();
      render(<StudioGraph graph={graph()} {...p} />);
      const menu = openMenu("Customer");
      expect(
        within(menu)
          .getAllByRole("menuitem")
          .map((i) => i.textContent),
      ).toEqual(["Add subclass", "Add relationship", "Rename", "Delete"]);
      fireEvent.click(within(menu).getByRole("menuitem", { name: "Delete" }));
      expect(p.onAction).toHaveBeenCalledWith({
        kind: "delete",
        iri: CUSTOMER,
        label: "Customer",
      });
      expect(screen.queryByRole("menu")).toBeNull();
    });

    it("moves focus to the first item when it opens, and the arrow keys walk the items", () => {
      render(<StudioGraph graph={graph()} {...props()} />);
      const menu = openMenu("Customer");
      const items = within(menu).getAllByRole("menuitem");
      expect(document.activeElement).toBe(items[0]);
      fireEvent.keyDown(menu, { key: "ArrowDown" });
      expect(document.activeElement).toBe(items[1]);
      fireEvent.keyDown(menu, { key: "End" });
      expect(document.activeElement).toBe(items[3]);
      fireEvent.keyDown(menu, { key: "ArrowDown" });
      expect(document.activeElement).toBe(items[0]);
      fireEvent.keyDown(menu, { key: "ArrowUp" });
      expect(document.activeElement).toBe(items[3]);
      fireEvent.keyDown(menu, { key: "Home" });
      expect(document.activeElement).toBe(items[0]);
    });

    it("closes on Escape and gives focus back to the node", () => {
      const { container } = render(
        <StudioGraph graph={graph()} {...props()} />,
      );
      openMenu("Customer");
      fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
      expect(screen.queryByRole("menu")).toBeNull();
      expect(document.activeElement).toBe(flowNode(container, CUSTOMER));
    });

    it("gives focus back to the node after an action too", () => {
      const { container } = render(
        <StudioGraph graph={graph()} {...props()} />,
      );
      const menu = openMenu("Customer");
      fireEvent.click(within(menu).getByRole("menuitem", { name: "Rename" }));
      expect(document.activeElement).toBe(flowNode(container, CUSTOMER));
    });

    it("closes on a click outside, and stays open on a click inside", () => {
      render(<StudioGraph graph={graph()} {...props()} />);
      const menu = openMenu("Customer");
      fireEvent.mouseDown(menu);
      expect(screen.getByRole("menu")).toBeTruthy();
      fireEvent.mouseDown(document.body);
      expect(screen.queryByRole("menu")).toBeNull();
    });

    it("closes on a press that the graph stops from bubbling", () => {
      render(<StudioGraph graph={graph()} {...props()} />);
      openMenu("Customer");
      // React Flow's pan and drag handlers do this to every press on the pane.
      const pane = document.body.appendChild(document.createElement("div"));
      pane.addEventListener("mousedown", (e) => e.stopImmediatePropagation());
      fireEvent.mouseDown(pane);
      pane.remove();
      expect(screen.queryByRole("menu")).toBeNull();
    });

    it("closes with Tab so it never stays open behind the page", () => {
      render(<StudioGraph graph={graph()} {...props()} />);
      const menu = openMenu("Customer");
      fireEvent.keyDown(menu, { key: "Tab" });
      expect(screen.queryByRole("menu")).toBeNull();
    });

    it("disables the actions on a class outside the namespace", () => {
      const p = props();
      render(<StudioGraph graph={graph()} {...p} />);
      const menu = openMenu("Agent");
      const rename = within(menu).getByRole("menuitem", { name: "Rename" });
      expect(rename.getAttribute("aria-disabled")).toBe("true");
      // Still reachable by keyboard, but it does nothing.
      expect(document.activeElement).toBe(
        within(menu).getByRole("menuitem", { name: "Add subclass" }),
      );
      fireEvent.click(rename);
      expect(p.onAction).not.toHaveBeenCalled();
      expect(screen.getByRole("menu")).toBeTruthy();
    });

    it("disables the actions of a read-only draft", () => {
      const p = props();
      render(<StudioGraph graph={graph()} {...p} editable={false} />);
      const menu = openMenu("Customer");
      for (const item of within(menu).getAllByRole("menuitem")) {
        expect(item.getAttribute("aria-disabled")).toBe("true");
      }
      fireEvent.click(within(menu).getByRole("menuitem", { name: "Delete" }));
      expect(p.onAction).not.toHaveBeenCalled();
    });

    describe("with the graph full screen", () => {
      /** Stubs the Fullscreen API, which happy-dom lacks: `element` is full screen, if any. */
      function fullScreen(element: Element | null) {
        const exitFullscreen = vi.fn().mockResolvedValue(undefined);
        Object.defineProperty(document, "fullscreenElement", {
          configurable: true,
          get: () => element,
        });
        Object.defineProperty(document, "exitFullscreen", {
          configurable: true,
          value: exitFullscreen,
        });
        return exitFullscreen;
      }

      afterEach(() => {
        delete (document as { fullscreenElement?: unknown }).fullscreenElement;
        delete (document as { exitFullscreen?: unknown }).exitFullscreen;
      });

      it("leaves it before an action, whose dialog the page opens outside the graph", async () => {
        const p = props();
        const { container } = render(<StudioGraph graph={graph()} {...p} />);
        // As OntologyGraphFlow's Full screen button does it.
        const exitFullscreen = fullScreen(container);
        let exited!: () => void;
        exitFullscreen.mockReturnValue(
          new Promise<void>((resolve) => (exited = resolve)),
        );
        const menu = openMenu("Customer");
        expect(container.contains(menu)).toBe(true);
        fireEvent.click(within(menu).getByRole("menuitem", { name: "Rename" }));
        expect(exitFullscreen).toHaveBeenCalledTimes(1);
        // Still full screen: a dialog opened now would not be drawn.
        expect(p.onAction).not.toHaveBeenCalled();
        exited();
        await waitFor(() =>
          expect(p.onAction).toHaveBeenCalledWith({
            kind: "rename",
            iri: CUSTOMER,
            label: "Customer",
          }),
        );
      });

      it("still runs the action when leaving full screen fails", async () => {
        const p = props();
        const { container } = render(<StudioGraph graph={graph()} {...p} />);
        fullScreen(container).mockRejectedValue(new TypeError("not allowed"));
        const menu = openMenu("Customer");
        fireEvent.click(within(menu).getByRole("menuitem", { name: "Delete" }));
        await waitFor(() => expect(p.onAction).toHaveBeenCalled());
      });

      it("does not try to leave it when nothing is full screen", () => {
        const p = props();
        const exitFullscreen = fullScreen(null);
        render(<StudioGraph graph={graph()} {...p} />);
        const menu = openMenu("Customer");
        fireEvent.click(within(menu).getByRole("menuitem", { name: "Delete" }));
        expect(p.onAction).toHaveBeenCalled();
        expect(exitFullscreen).not.toHaveBeenCalled();
      });
    });

    it("enables the actions of an editable class in an editable draft", () => {
      render(<StudioGraph graph={graph()} {...props()} />);
      const menu = openMenu("Customer");
      for (const item of within(menu).getAllByRole("menuitem")) {
        expect(item.getAttribute("aria-disabled")).toBeNull();
      }
    });
  });

  it("explains the focus view and focuses on a searched class", async () => {
    const p = props();
    p.search.mockResolvedValue([
      { iri: CUSTOMER, label: "Customer", kind: "class" },
    ]);
    render(
      <StudioGraph
        graph={graph({
          mode: "focus",
          focus: CUSTOMER,
          totalClasses: FULL_GRAPH_LIMIT + 250,
          truncated: true,
        })}
        {...p}
      />,
    );
    expect(screen.getByText(/Showing 2 of 400 classes/)).toBeTruthy();
    expect(screen.getByText(/more neighbours are hidden/)).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Show all classes" }),
    ).toBeNull();
    const input = screen.getByRole("combobox", { name: "Find a class" });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "buyer" } });
    fireEvent.mouseUp(await screen.findByRole("option", { name: /Customer/ }));
    expect(p.search).toHaveBeenCalledWith("buyer", "class");
    expect(p.onFocus).toHaveBeenCalledWith(CUSTOMER);
  });

  it("names the focus class by its IRI tail when the graph does not carry it", () => {
    render(
      <StudioGraph
        graph={graph({ mode: "focus", focus: "http://e/o#Ghost" })}
        {...props()}
      />,
    );
    expect(screen.getByText("Ghost").tagName).toBe("STRONG");
  });

  it("offers the full graph again at the limit, as null focus", () => {
    const p = props();
    const { rerender } = render(
      <StudioGraph
        graph={graph({
          mode: "focus",
          focus: CUSTOMER,
          totalClasses: FULL_GRAPH_LIMIT,
        })}
        {...p}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Show all classes" }));
    // null means "no focus"; undefined must never mean "all".
    expect(p.onFocus).toHaveBeenCalledWith(null);
    rerender(
      <StudioGraph
        graph={graph({
          mode: "focus",
          focus: CUSTOMER,
          totalClasses: FULL_GRAPH_LIMIT + 1,
        })}
        {...p}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "Show all classes" }),
    ).toBeNull();
  });

  describe("a change set preview", () => {
    const SUPPLIER = "http://e/o#Supplier";
    const preview = () => {
      const g = graph();
      return {
        ...g,
        nodes: [
          ...g.nodes.map((n) => ({ ...n, preview: null })),
          {
            ...g.nodes[0],
            iri: SUPPLIER,
            label: "Supplier",
            preview: "added" as const,
          },
        ],
        edges: g.edges.map((e) => ({ ...e, preview: "removed" as const })),
      };
    };

    it("draws the graph after the change set, marked, with a way back", () => {
      const onHidePreview = vi.fn();
      render(
        <StudioGraph
          graph={graph()}
          {...props()}
          preview={preview()}
          onHidePreview={onHidePreview}
        />,
      );
      const supplier = screen.getByText("Supplier");
      expect(
        supplier.closest("[data-highlight]")?.getAttribute("data-highlight"),
      ).toBe("added");
      expect(screen.getByText(/Previewing a change set/)).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "Hide preview" }));
      expect(onHidePreview).toHaveBeenCalled();
    });

    it("opens no action menu while it is shown", () => {
      render(<StudioGraph graph={graph()} {...props()} preview={preview()} />);
      fireEvent.contextMenu(screen.getByText("Customer"), {
        clientX: 5,
        clientY: 5,
      });
      expect(screen.queryByRole("menu")).toBeNull();
    });
  });

  it("says why counts are dimmed", () => {
    render(
      <StudioGraph
        graph={graph({
          abox: {
            status: "READY",
            current: false,
            conformsToVersion: 2,
            activeVersion: 3,
            reason: "The domain data was built for v2; v3 is active",
          },
        })}
        {...props()}
      />,
    );
    expect(screen.getByText(/built for v2; v3 is active/)).toBeTruthy();
    expect(
      (screen.getByLabelText(/12 instances/) as HTMLElement).style.opacity,
    ).toBe("0.45");
  });

  it("shows no ABox line and no full-graph button for a current ABox in the full view", () => {
    render(<StudioGraph graph={graph()} {...props()} />);
    expect(screen.queryByText(/dimmed/)).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Show all classes" }),
    ).toBeNull();
  });

  it("opens a draft too large to read whole at a readable zoom, showing the class it is focused on", async () => {
    // React Flow's own fit on mount, which waits for the nodes to be measured,
    // must not replace that view afterwards.
    onTestFinished(measureNodesLikeABrowser());
    // Twelve classes in a row of relationships: wider than the 500px pane
    // React Flow falls back to under happy-dom shows at a readable zoom.
    const iris = Array.from({ length: 12 }, (_, i) => `http://e/o#C${i}`);
    const wide = graph({
      mode: "focus",
      focus: iris[11],
      totalClasses: 200,
      nodes: iris.map((iri, i) => ({
        iri,
        label: `C${i}`,
        grounded: false,
        editable: true,
        attributes: [],
        attributeCount: 0,
        aboxCount: null,
        afterDeploy: false,
      })),
      edges: iris.slice(1).map((iri, i) => ({
        id: `rel|${i}`,
        kind: "objectProperty" as const,
        source: iris[i],
        target: iri,
        label: `next ${i}`,
      })),
    });
    const { container } = render(<StudioGraph graph={wide} {...props()} />);
    const viewport = () => {
      const transform = (
        container.querySelector(".react-flow__viewport") as HTMLElement
      ).style.transform;
      const [, x, y, zoom] =
        /translate\(([-\d.e]+)px,\s*([-\d.e]+)px\)\s*scale\(([-\d.e]+)\)/
          .exec(transform)
          ?.map(Number) ?? [];
      return { x, y, zoom };
    };
    await waitFor(
      () => expect(viewport().zoom).toBeCloseTo(MIN_READABLE_FRAME_ZOOM),
      { timeout: 3000 },
    );
    // Past the moment the nodes are measured.
    await act(() => new Promise((r) => setTimeout(r, 200)));
    // The focus class sits at one end of the row: the pane shows it.
    const size = parseFloat(
      (
        flowNode(container, iris[11]).querySelector(
          ".coa-graph-node",
        ) as HTMLElement
      ).style.width,
    );
    const home = layoutForceGraph(iris, toFlowEdges(wide), {
      nodeSize: size,
    }).get(iris[11]);
    const { x, y, zoom } = viewport();
    const sx = ((home?.x ?? NaN) + size / 2) * zoom + x;
    const sy = ((home?.y ?? NaN) + size / 2) * zoom + y;
    expect(sx > 0 && sx < 500 && sy > 0 && sy < 500).toBe(true);
  });
});
