// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
  act,
  within,
} from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { DomainDataTab } from "./DomainDataTab";
import { ApiError, type ApiClient } from "@components/ApiClientProvider";
import { formatTimestamp } from "@utils/helpers";

// The caller's role: true for one that can change the ontology (a data steward).
const access = vi.hoisted(() => ({ canManage: true }));
vi.mock("@api-hooks/use-can-manage-ontology", () => ({
  useCanManageOntology: () => access.canManage,
}));

const getDomainAbox = vi.fn();
const listDomainInstances = vi.fn();
const getDomainInstance = vi.fn();
const listAboxMappings = vi.fn();
const updateAboxMapping = vi.fn();
const resetAboxMapping = vi.fn();
const rebuildDomainAbox = vi.fn();
const searchEntities = vi.fn();
vi.mock("../../../services/ontology-engine", () => ({
  getDomainAbox: (...a: unknown[]) => getDomainAbox(...a),
  listDomainInstances: (...a: unknown[]) => listDomainInstances(...a),
  getDomainInstance: (...a: unknown[]) => getDomainInstance(...a),
  listAboxMappings: (...a: unknown[]) => listAboxMappings(...a),
  updateAboxMapping: (...a: unknown[]) => updateAboxMapping(...a),
  resetAboxMapping: (...a: unknown[]) => resetAboxMapping(...a),
  rebuildDomainAbox: (...a: unknown[]) => rebuildDomainAbox(...a),
  searchEntities: (...a: unknown[]) => searchEntities(...a),
}));

const api = {
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
} as unknown as ApiClient;

const CUSTOMER = "https://e/o#Customer";
const ORDER = "https://e/o#Order";

const BUILT_AT = "2026-10-15T00:00:00Z";
const LATER = "2026-10-16T09:30:00Z";

const READY = {
  status: "READY",
  current: true,
  conformsToVersion: 3,
  activeVersion: 3,
  buildId: "b-1",
  builtAt: BUILT_AT,
  counts: { instances: 12, relations: 7, attributes: 30, evidence: 41 },
  classCounts: [
    { classIri: CUSTOMER, label: "Customer", count: 2 },
    { classIri: ORDER, label: "Order", count: 10 },
  ],
  mappingRevision: "r-1",
  pending: false,
  lastError: null,
};
/** Nothing built yet (or cleared): no build id, no build time, nothing to count. */
const EMPTY = {
  ...READY,
  status: "EMPTY",
  current: false,
  conformsToVersion: null,
  buildId: null,
  builtAt: null,
  counts: { instances: 0, relations: 0, attributes: 0, evidence: 0 },
  classCounts: [],
  mappingRevision: null,
};

const ACME = {
  iri: "https://e/data/Customer/1",
  classIri: CUSTOMER,
  classLabel: "Customer",
  label: "Acme Corp",
  altLabels: ["ACME"],
  evidenceCount: 2,
};
const ORDER_1001 = {
  iri: "https://e/data/Order/1",
  classIri: ORDER,
  classLabel: "Order",
  label: "Order 1001",
  altLabels: [],
  evidenceCount: 1,
};

const ACME_DETAIL = {
  iri: ACME.iri,
  classes: [{ iri: CUSTOMER, label: "Customer" }],
  label: "Acme Corp",
  altLabels: ["ACME"],
  attributes: [
    {
      property: "https://e/o#email",
      label: "email",
      value: "info@acme.test",
      free: false,
      evidence: ["c-1"],
    },
    {
      property: "urn:coa:free#industry",
      label: "industry",
      value: "Retail",
      free: true,
      evidence: ["c-2"],
    },
  ],
  outgoing: [
    {
      property: "https://e/o#placed",
      label: "placed",
      target: { iri: ORDER_1001.iri, label: "Order 1001", classIri: ORDER },
      evidence: ["c-1"],
    },
  ],
  incoming: [],
  evidence: [
    {
      chunkId: "c-1",
      docSourceId: "d-1",
      sourceKey: "docs/2026/acme-profile.pdf",
      text: "Acme Corp placed order 1001.",
    },
    {
      chunkId: "c-2",
      docSourceId: "d-1",
      sourceKey: "docs/2026/acme-profile.pdf",
      text: "Acme Corp works in retail.",
    },
  ],
};
const ORDER_DETAIL = {
  iri: ORDER_1001.iri,
  classes: [{ iri: ORDER, label: "Order" }],
  label: "Order 1001",
  altLabels: [],
  attributes: [],
  outgoing: [],
  incoming: [
    {
      property: "https://e/o#placed",
      label: "placed",
      source: { iri: ACME.iri, label: "Acme Corp", classIri: CUSTOMER },
    },
  ],
  evidence: [],
};

const ROW = {
  direction: "forward",
  origin: "auto",
  status: "active",
  rationale: "Same concept",
  decidedForVersion: 3,
  updatedBy: "system",
  updatedAt: "2026-10-15T00:00:00Z",
};
const CLIENT_ROW = {
  ...ROW,
  id: "class.aa",
  kind: "class",
  key: ["Client"],
  target: CUSTOMER,
  targetLabel: "Customer",
};
const SHOPPER_ROW = {
  ...ROW,
  id: "class.bb",
  kind: "class",
  key: ["Shopper"],
  target: CUSTOMER,
  targetLabel: "Customer",
  origin: "manual",
  rationale: "",
  updatedBy: "alice",
};
const PLACED_ROW = {
  ...ROW,
  id: "relation.cc",
  kind: "relation",
  key: ["PLACED", CUSTOMER, ORDER],
  target: "https://e/o#placed",
  targetLabel: "placed",
};
const INDUSTRY_ROW = {
  ...ROW,
  id: "attribute.dd",
  kind: "attribute",
  key: [CUSTOMER, "industry"],
  target: "free",
  targetLabel: "free",
};
const ROWS = [CLIENT_ROW, SHOPPER_ROW, PLACED_ROW, INDUSTRY_ROW];

/** Advance fake timers inside act, so the state updates of the polls they fire are flushed. */
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function renderTab(namespace = "ns") {
  return render(<DomainDataTab apiClient={api} namespace={namespace} />);
}

/** The table row holding `text`. */
function rowOf(text: string): HTMLElement {
  const row = screen.getByText(text).closest("tr");
  if (!row) throw new Error(`No table row holds ${text}`);
  return row;
}

/** Pick `option` in the Cloudscape Select labelled `label` (it opens on mousedown, picks on mouseup). */
function pickOption(label: RegExp, option: string) {
  fireEvent.mouseDown(screen.getByRole("button", { name: label }));
  // Only among the open options: the same text can be in the table too.
  const match = screen
    .getAllByRole("option")
    .find((o) => within(o).queryAllByText(option).length > 0);
  if (!match) throw new Error(`No option ${option}`);
  fireEvent.mouseUp(match);
}

/** Open the change dialog of a mapping row, search `query` and pick the hit labelled `hit`. */
async function pickTarget(keyLabel: string, query: string, hit: string) {
  fireEvent.click(
    await screen.findByRole("button", { name: `Change target of ${keyLabel}` }),
  );
  const dialog = screen.getByRole("dialog");
  const input = within(dialog).getByRole("combobox", { name: "Target" });
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: query } });
  fireEvent.mouseUp(await within(dialog).findByText(hit));
  return dialog;
}

describe("DomainDataTab", () => {
  beforeEach(() => {
    cleanup();
    // Reset, not just clear: a test that fails early must not leave queued
    // mockResolvedValueOnce answers for the next one.
    vi.resetAllMocks();
    access.canManage = true;
    getDomainAbox.mockResolvedValue(READY);
    listDomainInstances.mockResolvedValue({
      instances: [ACME, ORDER_1001],
      nextToken: null,
    });
    getDomainInstance.mockImplementation((_a, _ns, iri: string) =>
      Promise.resolve(iri === ACME.iri ? ACME_DETAIL : ORDER_DETAIL),
    );
    listAboxMappings.mockImplementation(
      (_a, _ns, opts: { kind?: string } = {}) =>
        Promise.resolve({
          mappings: ROWS.filter((r) => !opts.kind || r.kind === opts.kind),
        }),
    );
    updateAboxMapping.mockImplementation((_a, _ns, id: string) =>
      Promise.resolve(ROWS.find((r) => r.id === id)),
    );
    resetAboxMapping.mockImplementation((_a, _ns, id: string) =>
      Promise.resolve(ROWS.find((r) => r.id === id)),
    );
    rebuildDomainAbox.mockResolvedValue({ requested: true });
    searchEntities.mockResolvedValue([
      {
        uri: "https://e/o#Shopper",
        label: "Shopper",
        kind: "class",
        graph_uris: [],
      },
    ]);
  });

  describe("state", () => {
    it("shows the status, the versions and the counts", async () => {
      renderTab();
      expect(await screen.findByText("Ready")).toBeInTheDocument();
      expect(screen.getByText("Built for v3, active v3")).toBeInTheDocument();
      expect(screen.getByText("12")).toBeInTheDocument();
      expect(screen.getByText("7")).toBeInTheDocument();
      expect(screen.getByText("30")).toBeInTheDocument();
      expect(screen.getByText("41")).toBeInTheDocument();
      expect(
        screen.getByText(`Last built ${formatTimestamp(BUILT_AT)}`),
      ).toBeInTheDocument();
      expect(screen.queryByText("Out of date")).not.toBeInTheDocument();
      expect(getDomainAbox).toHaveBeenCalledWith(api, "ns");
    });

    it("flags an ABox built for another version as out of date", async () => {
      getDomainAbox.mockResolvedValue({
        ...READY,
        current: false,
        activeVersion: 4,
      });
      renderTab();
      expect(await screen.findByText("Out of date")).toBeInTheDocument();
      expect(screen.getByText("Built for v3, active v4")).toBeInTheDocument();
    });

    it("does not flag an empty ABox as out of date", async () => {
      getDomainAbox.mockResolvedValue({ ...EMPTY, activeVersion: 1 });
      renderTab();
      expect(await screen.findByText("Not built")).toBeInTheDocument();
      expect(screen.getByText("Active v1")).toBeInTheDocument();
      expect(screen.queryByText("Out of date")).not.toBeInTheDocument();
      expect(screen.queryByText(/^Last built/)).not.toBeInTheDocument();
    });

    it("does not flag a failed first build as out of date", async () => {
      getDomainAbox.mockResolvedValue({
        ...EMPTY,
        status: "FAILED",
        lastError: "ValueError: no documents",
      });
      renderTab();
      expect(await screen.findByText("Build failed")).toBeInTheDocument();
      expect(screen.queryByText("Out of date")).not.toBeInTheDocument();
    });

    it("disables Rebuild when no ontology version is active", async () => {
      getDomainAbox.mockResolvedValue({ ...EMPTY, activeVersion: null });
      renderTab();
      await screen.findByText("No ontology version deployed");
      expect(screen.getByRole("button", { name: "Rebuild" })).toHaveAttribute(
        "aria-disabled",
        "true",
      );
    });

    it("disables Change target, but not Reset to auto, when no ontology version is active", async () => {
      // The ontology was deleted: the mappings stay, but no version takes a new target.
      getDomainAbox.mockResolvedValue({ ...EMPTY, activeVersion: null });
      renderTab();
      await screen.findByText("No ontology version deployed");
      const change = await screen.findByRole("button", {
        name: "Change target of Client",
      });
      expect(change).toBeDisabled();
      expect(
        screen.getByText(
          /A target can be changed again once an ontology version is deployed/,
        ),
      ).toBeInTheDocument();
      fireEvent.click(change);
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(
        screen.getByRole("button", { name: "Reset Shopper to auto" }),
      ).toBeEnabled();
    });

    it("reads the lists once on a Refresh that finds a new build", async () => {
      getDomainAbox
        .mockResolvedValueOnce(READY)
        .mockResolvedValue({ ...READY, builtAt: LATER });
      renderTab();
      await screen.findByRole("button", { name: "Change target of Client" });
      await screen.findByRole("button", { name: "Open Acme Corp (Customer)" });
      fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
      expect(
        await screen.findByText(`Last built ${formatTimestamp(LATER)}`),
      ).toBeInTheDocument();
      await waitFor(() => expect(listDomainInstances).toHaveBeenCalledTimes(2));
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(listDomainInstances).toHaveBeenCalledTimes(2);
      expect(listAboxMappings).toHaveBeenCalledTimes(2);
    });

    it("re-reads the state and the lists on Refresh", async () => {
      renderTab();
      await screen.findByRole("button", {
        name: "Change target of Client",
      });
      await screen.findByRole("button", { name: "Open Acme Corp (Customer)" });
      fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
      await waitFor(() => expect(getDomainAbox).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(listDomainInstances).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(listAboxMappings).toHaveBeenCalledTimes(2));
    });

    it("shows the last build error", async () => {
      getDomainAbox.mockResolvedValue({
        ...READY,
        status: "FAILED",
        lastError: "ValueError: no documents",
      });
      renderTab();
      expect(await screen.findByText("Build failed")).toBeInTheDocument();
      expect(screen.getByText("ValueError: no documents")).toBeInTheDocument();
    });

    it("shows why the state could not be loaded", async () => {
      getDomainAbox.mockRejectedValue(new Error("Service Unavailable"));
      renderTab();
      expect(
        await screen.findByText("Could not load the domain ABox"),
      ).toBeInTheDocument();
      expect(screen.getByText("Service Unavailable")).toBeInTheDocument();
    });
  });

  describe("polling", () => {
    it("polls while building and stops once ready", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        const building = { ...READY, status: "BUILDING", buildId: "b-2" };
        getDomainAbox
          .mockResolvedValueOnce(building)
          .mockResolvedValueOnce(building)
          .mockResolvedValue({ ...READY, buildId: "b-2", builtAt: LATER });
        renderTab();
        expect(await screen.findByText("Building")).toBeInTheDocument();

        await advance(3100);
        expect(getDomainAbox).toHaveBeenCalledTimes(2);
        await advance(3100);
        expect(getDomainAbox).toHaveBeenCalledTimes(3);
        expect(await screen.findByText("Ready")).toBeInTheDocument();

        for (let i = 0; i < 4; i++) await advance(3100);
        expect(getDomainAbox).toHaveBeenCalledTimes(3);
      } finally {
        vi.useRealTimers();
      }
    });

    it("polls while a rebuild is queued", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        getDomainAbox
          .mockResolvedValueOnce({ ...READY, pending: true })
          .mockResolvedValue(READY);
        renderTab();
        expect(await screen.findByText("Rebuild queued")).toBeInTheDocument();
        await advance(3100);
        expect(getDomainAbox).toHaveBeenCalledTimes(2);
        for (let i = 0; i < 3; i++) await advance(3100);
        expect(getDomainAbox).toHaveBeenCalledTimes(2);
      } finally {
        vi.useRealTimers();
      }
    });

    it("stops polling after repeated failures", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        getDomainAbox
          .mockResolvedValueOnce({ ...READY, status: "BUILDING" })
          .mockRejectedValue(new Error("Service Unavailable"));
        renderTab();
        expect(await screen.findByText("Building")).toBeInTheDocument();
        for (let i = 0; i < 10; i++) await advance(3100);
        // The building answer, then 4 failed polls.
        expect(getDomainAbox).toHaveBeenCalledTimes(5);
        expect(
          screen.getByText("Could not load the domain ABox"),
        ).toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
    });

    it("keeps polling a little after a rebuild is queued", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        // The worker took the request at once: the state reads READY with
        // nothing pending until the build marks itself BUILDING.
        renderTab();
        await screen.findByText("Ready");
        fireEvent.click(screen.getByRole("button", { name: "Rebuild" }));
        await waitFor(() => expect(getDomainAbox).toHaveBeenCalledTimes(2));

        await advance(3100);
        expect(getDomainAbox).toHaveBeenCalledTimes(3);
        await advance(3100);
        expect(getDomainAbox).toHaveBeenCalledTimes(4);
        for (let i = 0; i < 3; i++) await advance(3100);
        expect(getDomainAbox).toHaveBeenCalledTimes(4);
      } finally {
        vi.useRealTimers();
      }
    });

    it("keeps polling a little after a mapping save", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        renderTab();
        const dialog = await pickTarget("Client", "purchaser", "Shopper");
        fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
        await waitFor(() => expect(getDomainAbox).toHaveBeenCalledTimes(2));
        await advance(3100);
        expect(getDomainAbox).toHaveBeenCalledTimes(3);
        await advance(3100);
        expect(getDomainAbox).toHaveBeenCalledTimes(4);
        for (let i = 0; i < 3; i++) await advance(3100);
        expect(getDomainAbox).toHaveBeenCalledTimes(4);
      } finally {
        vi.useRealTimers();
      }
    });

    it("keeps polling a little after a mapping reset", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        renderTab();
        fireEvent.click(
          await screen.findByRole("button", { name: "Reset Shopper to auto" }),
        );
        await waitFor(() => expect(getDomainAbox).toHaveBeenCalledTimes(2));
        await advance(3100);
        expect(getDomainAbox).toHaveBeenCalledTimes(3);
        await advance(3100);
        expect(getDomainAbox).toHaveBeenCalledTimes(4);
        for (let i = 0; i < 3; i++) await advance(3100);
        expect(getDomainAbox).toHaveBeenCalledTimes(4);
      } finally {
        vi.useRealTimers();
      }
    });

    it("sees the build chained after an edit made during a build", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        const T2 = "2026-10-17T11:00:00Z";
        const building = { ...READY, status: "BUILDING", buildId: "b-2" };
        getDomainAbox
          .mockResolvedValueOnce(building)
          // The edit marked a rebuild pending while b-2 runs.
          .mockResolvedValueOnce({ ...building, pending: true })
          .mockResolvedValueOnce({ ...building, pending: true })
          .mockResolvedValueOnce({ ...building, pending: true })
          // b-2 landed and the worker took the request: READY, nothing pending.
          .mockResolvedValueOnce({ ...READY, buildId: "b-2", builtAt: LATER })
          .mockResolvedValueOnce({
            ...READY,
            status: "BUILDING",
            buildId: "b-3",
            builtAt: LATER,
          })
          .mockResolvedValue({ ...READY, buildId: "b-3", builtAt: T2 });
        renderTab();
        fireEvent.click(
          await screen.findByRole("button", { name: "Reset Shopper to auto" }),
        );
        await waitFor(() => expect(resetAboxMapping).toHaveBeenCalled());
        for (let i = 0; i < 8; i++) await advance(3100);
        expect(
          await screen.findByText(`Last built ${formatTimestamp(T2)}`),
        ).toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
    });

    it("does not poll a settled ABox", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        renderTab();
        await screen.findByText("Ready");
        await advance(10_000);
        expect(getDomainAbox).toHaveBeenCalledTimes(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it("clears the poll timer on unmount", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const setTimeoutSpy = vi.spyOn(window, "setTimeout");
      const clearTimeoutSpy = vi.spyOn(window, "clearTimeout");
      try {
        getDomainAbox.mockResolvedValue({ ...READY, status: "BUILDING" });
        const view = renderTab();
        await screen.findByText("Building");
        await advance(3100);
        expect(getDomainAbox).toHaveBeenCalledTimes(2);

        // Unmounting must clear the pending poll timer, not just stop re-rendering.
        const pollTimers = setTimeoutSpy.mock.calls.flatMap((call, i) =>
          call[1] === 3000 ? [setTimeoutSpy.mock.results[i].value] : [],
        );
        expect(pollTimers.length).toBeGreaterThan(0);
        view.unmount();
        expect(clearTimeoutSpy).toHaveBeenCalledWith(pollTimers.at(-1));
        const callsAtUnmount = getDomainAbox.mock.calls.length;
        await advance(10_000);
        expect(getDomainAbox.mock.calls.length).toBe(callsAtUnmount);
      } finally {
        // Restore the spies before the real timers, or restoring would put the
        // fake functions back.
        setTimeoutSpy.mockRestore();
        clearTimeoutSpy.mockRestore();
        vi.useRealTimers();
      }
    });
  });

  describe("rebuild", () => {
    it("queues a rebuild and reloads the state", async () => {
      renderTab();
      await screen.findByText("Ready");
      fireEvent.click(screen.getByRole("button", { name: "Rebuild" }));
      await waitFor(() =>
        expect(rebuildDomainAbox).toHaveBeenCalledWith(api, "ns"),
      );
      await waitFor(() => expect(getDomainAbox).toHaveBeenCalledTimes(2));
    });

    it("shows the server's message when there is no version to build from", async () => {
      const message =
        "There is no ontology version to build the domain ABox from";
      rebuildDomainAbox.mockRejectedValue(
        new ApiError(409, "UNKNOWN_ERROR", message, { detail: message }),
      );
      renderTab();
      await screen.findByText("Ready");
      fireEvent.click(screen.getByRole("button", { name: "Rebuild" }));
      expect(await screen.findByText(message)).toBeInTheDocument();
      expect(screen.getByText("Could not queue a rebuild")).toBeInTheDocument();
    });
  });

  describe("by role", () => {
    const CANNOT_CHANGE =
      "How the strings found in the documents map to ontology terms. Your role on this namespace cannot change them.";

    it("offers no rebuild or mapping change to a role that only reads, and says why", async () => {
      access.canManage = false;
      renderTab();
      const change = await screen.findByRole("button", {
        name: "Change target of Client",
      });
      expect(change).toBeDisabled();
      expect(
        screen.getByRole("button", { name: "Reset Shopper to auto" }),
      ).toBeDisabled();
      expect(screen.getByText(CANNOT_CHANGE)).toBeInTheDocument();
      const rebuild = screen.getByRole("button", { name: "Rebuild" });
      expect(rebuild).toHaveAttribute("aria-disabled", "true");
      expect(rebuild).toHaveAccessibleDescription(
        "Your role on this namespace does not allow this.",
      );
      fireEvent.click(rebuild);
      expect(rebuildDomainAbox).not.toHaveBeenCalled();
      // Reading stays offered.
      expect(
        screen.getByRole("button", { name: "Open Acme Corp (Customer)" }),
      ).not.toBeDisabled();
    });

    it("offers them to a role that can change the ontology", async () => {
      renderTab();
      expect(
        await screen.findByRole("button", { name: "Change target of Client" }),
      ).not.toBeDisabled();
      expect(
        screen.getByRole("button", { name: "Reset Shopper to auto" }),
      ).not.toBeDisabled();
      expect(
        screen.getByRole("button", { name: "Rebuild" }),
      ).not.toHaveAttribute("aria-disabled", "true");
      expect(screen.queryByText(CANNOT_CHANGE)).toBeNull();
    });
  });

  describe("instances", () => {
    it("lists instances with their class, other labels and evidence count", async () => {
      renderTab();
      expect(
        await screen.findByRole("button", {
          name: "Open Acme Corp (Customer)",
        }),
      ).toBeInTheDocument();
      const row = within(rowOf("Acme Corp"));
      expect(row.getByText("Customer")).toBeInTheDocument();
      expect(row.getByText("ACME")).toBeInTheDocument();
      expect(row.getByText("2")).toBeInTheDocument();
      expect(listDomainInstances).toHaveBeenCalledTimes(1);
      expect(listDomainInstances).toHaveBeenCalledWith(api, "ns", {
        classIri: undefined,
        q: undefined,
        maxResults: 50,
      });
    });

    it("re-queries with the class picked in the filter", async () => {
      renderTab();
      await screen.findByRole("button", { name: "Open Acme Corp (Customer)" });
      pickOption(/^Filter by class/, "Order (10)");
      await waitFor(() =>
        expect(listDomainInstances).toHaveBeenLastCalledWith(api, "ns", {
          classIri: ORDER,
          q: undefined,
          maxResults: 50,
        }),
      );
    });

    it("runs the text query on Enter", async () => {
      renderTab();
      await screen.findByRole("button", { name: "Open Acme Corp (Customer)" });
      const filter = screen.getByRole("searchbox", {
        name: "Filter instances",
      });
      fireEvent.change(filter, { target: { value: "ac" } });
      expect(listDomainInstances).toHaveBeenCalledTimes(1);
      fireEvent.keyDown(filter, { key: "Enter" });
      expect(listDomainInstances).toHaveBeenLastCalledWith(api, "ns", {
        classIri: undefined,
        q: "ac",
        maxResults: 50,
      });
    });

    it("runs the text query once typing pauses", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        renderTab();
        await screen.findByRole("button", {
          name: "Open Acme Corp (Customer)",
        });
        const filter = screen.getByRole("searchbox", {
          name: "Filter instances",
        });
        fireEvent.change(filter, { target: { value: "a" } });
        await advance(200);
        fireEvent.change(filter, { target: { value: "ac" } });
        await advance(300);
        expect(listDomainInstances).toHaveBeenCalledTimes(1);
        await advance(200);
        expect(listDomainInstances).toHaveBeenCalledTimes(2);
        expect(listDomainInstances).toHaveBeenLastCalledWith(api, "ns", {
          classIri: undefined,
          q: "ac",
          maxResults: 50,
        });
      } finally {
        vi.useRealTimers();
      }
    });

    it("loads the next page with the token", async () => {
      listDomainInstances
        .mockResolvedValueOnce({ instances: [ACME], nextToken: "tok==" })
        .mockResolvedValueOnce({ instances: [ORDER_1001], nextToken: null });
      renderTab();
      fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
      expect(
        await screen.findByRole("button", { name: "Open Order 1001 (Order)" }),
      ).toBeInTheDocument();
      expect(listDomainInstances).toHaveBeenLastCalledWith(api, "ns", {
        classIri: undefined,
        q: undefined,
        maxResults: 50,
        nextToken: "tok==",
      });
      expect(
        screen.getByRole("button", { name: "Open Acme Corp (Customer)" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Load more" }),
      ).not.toBeInTheDocument();
    });

    it("keeps the rows and shows why the next page could not be loaded", async () => {
      listDomainInstances
        .mockResolvedValueOnce({ instances: [ACME], nextToken: "tok==" })
        .mockRejectedValueOnce(new Error("Service Unavailable"));
      renderTab();
      fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
      expect(
        await screen.findByText("Service Unavailable"),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Open Acme Corp (Customer)" }),
      ).toBeInTheDocument();
      // The page can be asked for again.
      expect(screen.getByRole("button", { name: "Load more" })).toBeEnabled();
    });

    it("drops a list answer that a newer query superseded", async () => {
      let resolveSlow: (value: unknown) => void = () => {};
      listDomainInstances
        .mockResolvedValueOnce({ instances: [ACME], nextToken: null })
        .mockReturnValueOnce(new Promise((resolve) => (resolveSlow = resolve)))
        .mockResolvedValueOnce({ instances: [ORDER_1001], nextToken: null });
      renderTab();
      await screen.findByRole("button", { name: "Open Acme Corp (Customer)" });
      pickOption(/^Filter by class/, "Customer (2)");
      pickOption(/^Filter by class/, "Order (10)");
      expect(
        await screen.findByRole("button", { name: "Open Order 1001 (Order)" }),
      ).toBeInTheDocument();

      await act(async () =>
        resolveSlow({ instances: [ACME], nextToken: null }),
      );
      expect(
        screen.queryByRole("button", { name: "Open Acme Corp (Customer)" }),
      ).not.toBeInTheDocument();
    });

    it("re-reads the lists when a rebuild lands, not when it starts", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        // The new build id is written at BUILDING; only READY changes builtAt.
        getDomainAbox
          .mockResolvedValueOnce({ ...READY, pending: true })
          .mockResolvedValueOnce({
            ...READY,
            status: "BUILDING",
            buildId: "b-2",
          })
          .mockResolvedValue({ ...READY, buildId: "b-2", builtAt: LATER });
        renderTab();
        await screen.findByRole("button", {
          name: "Open Acme Corp (Customer)",
        });
        await screen.findByRole("button", { name: "Change target of Client" });
        expect(listDomainInstances).toHaveBeenCalledTimes(1);
        expect(listAboxMappings).toHaveBeenCalledTimes(1);

        await advance(3100);
        expect(await screen.findByText("Building")).toBeInTheDocument();
        expect(listDomainInstances).toHaveBeenCalledTimes(1);
        expect(listAboxMappings).toHaveBeenCalledTimes(1);

        await advance(3100);
        expect(await screen.findByText("Ready")).toBeInTheDocument();
        await waitFor(() =>
          expect(listDomainInstances).toHaveBeenCalledTimes(2),
        );
        await waitFor(() => expect(listAboxMappings).toHaveBeenCalledTimes(2));
      } finally {
        vi.useRealTimers();
      }
    });

    it("lists the instances of the first build once it lands", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        getDomainAbox
          .mockResolvedValueOnce({ ...EMPTY, pending: true })
          .mockResolvedValueOnce({
            ...EMPTY,
            status: "BUILDING",
            buildId: "b-1",
          })
          .mockResolvedValue(READY);
        listDomainInstances
          .mockResolvedValueOnce({ instances: [], nextToken: null })
          .mockResolvedValue({ instances: [ACME], nextToken: null });
        renderTab();
        expect(await screen.findByText("Rebuild queued")).toBeInTheDocument();
        await waitFor(() => expect(listAboxMappings).toHaveBeenCalledTimes(1));
        expect(listDomainInstances).toHaveBeenCalledTimes(1);

        await advance(3100);
        expect(await screen.findByText("Building")).toBeInTheDocument();
        expect(listDomainInstances).toHaveBeenCalledTimes(1);

        await advance(3100);
        expect(
          await screen.findByRole("button", {
            name: "Open Acme Corp (Customer)",
          }),
        ).toBeInTheDocument();
        expect(listDomainInstances).toHaveBeenCalledTimes(2);
        await waitFor(() => expect(listAboxMappings).toHaveBeenCalledTimes(2));
      } finally {
        vi.useRealTimers();
      }
    });

    it("shows an instance's attributes, links and evidence", async () => {
      renderTab();
      fireEvent.click(
        await screen.findByRole("button", {
          name: "Open Acme Corp (Customer)",
        }),
      );
      expect(getDomainInstance).toHaveBeenCalledWith(api, "ns", ACME.iri);
      expect(await screen.findByText("info@acme.test")).toBeInTheDocument();
      const industry = within(rowOf("Retail"));
      expect(industry.getByText("industry")).toBeInTheDocument();
      expect(industry.getByText("free")).toBeInTheDocument();
      expect(
        within(rowOf("info@acme.test")).queryByText("free"),
      ).not.toBeInTheDocument();
      expect(screen.getByText("placed")).toBeInTheDocument();
      expect(screen.queryByText(/^Showing the first/)).not.toBeInTheDocument();

      // Two chunks of the same file get distinct headers.
      fireEvent.click(screen.getByText("acme-profile.pdf, chunk 1"));
      expect(
        screen.getByText("Acme Corp placed order 1001."),
      ).toBeInTheDocument();
      fireEvent.click(screen.getByText("acme-profile.pdf, chunk 2"));
      expect(
        screen.getByText("Acme Corp works in retail."),
      ).toBeInTheDocument();
    });

    it("names each link with its relation", async () => {
      getDomainInstance.mockResolvedValue({
        ...ACME_DETAIL,
        outgoing: [
          ...ACME_DETAIL.outgoing,
          {
            property: "https://e/o#supplies",
            label: "supplies",
            target: ACME_DETAIL.outgoing[0].target,
            evidence: [],
          },
        ],
      });
      renderTab();
      fireEvent.click(
        await screen.findByRole("button", {
          name: "Open Acme Corp (Customer)",
        }),
      );
      expect(
        await screen.findByRole("button", { name: "placed Order 1001" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "supplies Order 1001" }),
      ).toBeInTheDocument();
    });

    it("moves focus to the heading of the opened instance", async () => {
      renderTab();
      fireEvent.click(
        await screen.findByRole("button", {
          name: "Open Acme Corp (Customer)",
        }),
      );
      await screen.findByText("info@acme.test");
      expect(
        screen.getByRole("heading", { name: "Acme Corp" }),
      ).toContainElement(document.activeElement as HTMLElement);
    });

    it("says when the engine cut the incoming links or the evidence", async () => {
      getDomainInstance.mockResolvedValue({
        ...ACME_DETAIL,
        incoming: ORDER_DETAIL.incoming,
        truncated: { incoming: true, evidence: true },
      });
      renderTab();
      fireEvent.click(
        await screen.findByRole("button", {
          name: "Open Acme Corp (Customer)",
        }),
      );
      expect(
        await screen.findByText("Showing the first 1 incoming links."),
      ).toBeInTheDocument();
      expect(
        screen.getByText("Showing the first 2 evidence chunks."),
      ).toBeInTheDocument();
    });

    it("re-reads the open instance when a build lands", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        getDomainAbox
          .mockResolvedValueOnce({ ...READY, pending: true })
          .mockResolvedValue({ ...READY, builtAt: LATER });
        getDomainInstance
          .mockResolvedValueOnce(ACME_DETAIL)
          .mockResolvedValueOnce({
            ...ACME_DETAIL,
            attributes: [
              { ...ACME_DETAIL.attributes[0], value: "sales@acme.test" },
            ],
          });
        renderTab();
        fireEvent.click(
          await screen.findByRole("button", {
            name: "Open Acme Corp (Customer)",
          }),
        );
        expect(await screen.findByText("info@acme.test")).toBeInTheDocument();

        await advance(3100);
        expect(await screen.findByText("sales@acme.test")).toBeInTheDocument();
        expect(getDomainInstance).toHaveBeenLastCalledWith(api, "ns", ACME.iri);
      } finally {
        vi.useRealTimers();
      }
    });

    it("re-reads the instance a link asked for when a build lands before its answer", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        getDomainAbox
          .mockResolvedValueOnce({ ...READY, pending: true })
          .mockResolvedValue({ ...READY, builtAt: LATER });
        let resolveStale: (value: unknown) => void = () => {};
        getDomainInstance
          .mockResolvedValueOnce(ACME_DETAIL)
          .mockReturnValueOnce(
            new Promise((resolve) => (resolveStale = resolve)),
          )
          .mockResolvedValueOnce(ORDER_DETAIL);
        renderTab();
        fireEvent.click(
          await screen.findByRole("button", {
            name: "Open Acme Corp (Customer)",
          }),
        );
        await screen.findByText("info@acme.test");
        // The click's answer is still pending when the build lands.
        fireEvent.click(
          screen.getByRole("button", { name: "placed Order 1001" }),
        );
        await advance(3100);

        expect(
          await screen.findByRole("button", { name: "Acme Corp placed" }),
        ).toBeInTheDocument();
        expect(getDomainInstance).toHaveBeenLastCalledWith(
          api,
          "ns",
          ORDER_1001.iri,
        );
        expect(screen.queryByText("info@acme.test")).not.toBeInTheDocument();

        // The answer read before the swap is dropped.
        await act(async () => resolveStale({ ...ORDER_DETAIL, incoming: [] }));
        expect(
          screen.getByRole("button", { name: "Acme Corp placed" }),
        ).toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
    });

    it("re-reads a first open whose answer predates a landed build", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        getDomainAbox
          .mockResolvedValueOnce({ ...READY, pending: true })
          .mockResolvedValue({ ...READY, builtAt: LATER });
        let resolveStale: (value: unknown) => void = () => {};
        let resolveFresh: (value: unknown) => void = () => {};
        getDomainInstance
          .mockReturnValueOnce(
            new Promise((resolve) => (resolveStale = resolve)),
          )
          .mockReturnValueOnce(
            new Promise((resolve) => (resolveFresh = resolve)),
          );
        renderTab();
        fireEvent.click(
          await screen.findByRole("button", {
            name: "Open Acme Corp (Customer)",
          }),
        );
        await advance(3100);
        await waitFor(() => expect(getDomainInstance).toHaveBeenCalledTimes(2));

        await act(async () => resolveStale(ACME_DETAIL));
        expect(screen.queryByText("info@acme.test")).not.toBeInTheDocument();
        await act(async () =>
          resolveFresh({
            ...ACME_DETAIL,
            attributes: [
              { ...ACME_DETAIL.attributes[0], value: "sales@acme.test" },
            ],
          }),
        );
        expect(screen.getByText("sales@acme.test")).toBeInTheDocument();
        expect(screen.queryByText("info@acme.test")).not.toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
    });

    it("keeps the open instance when its re-read after a build fails", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        getDomainAbox
          .mockResolvedValueOnce({ ...READY, pending: true })
          .mockResolvedValue({ ...READY, builtAt: LATER });
        getDomainInstance
          .mockResolvedValueOnce(ACME_DETAIL)
          .mockRejectedValueOnce(new Error("Service Unavailable"));
        renderTab();
        fireEvent.click(
          await screen.findByRole("button", {
            name: "Open Acme Corp (Customer)",
          }),
        );
        await screen.findByText("info@acme.test");
        await advance(3100);

        expect(
          await screen.findByText("Service Unavailable"),
        ).toBeInTheDocument();
        expect(
          screen.getByText("Could not load the instance"),
        ).toBeInTheDocument();
        expect(screen.getByText("info@acme.test")).toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
    });

    it("closes the open instance with a notice when the new build dropped it", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        getDomainAbox
          .mockResolvedValueOnce({ ...READY, pending: true })
          .mockResolvedValue({ ...READY, builtAt: LATER });
        getDomainInstance
          .mockResolvedValueOnce(ACME_DETAIL)
          .mockRejectedValueOnce(
            new ApiError(
              404,
              "UNKNOWN_ERROR",
              "Instance not found in the domain ABox",
              { detail: "Instance not found in the domain ABox" },
            ),
          );
        renderTab();
        fireEvent.click(
          await screen.findByRole("button", {
            name: "Open Acme Corp (Customer)",
          }),
        );
        expect(await screen.findByText("info@acme.test")).toBeInTheDocument();

        await advance(3100);
        const notice = await screen.findByText(
          "Acme Corp is not in the rebuilt domain ABox.",
        );
        expect(screen.queryByText("info@acme.test")).not.toBeInTheDocument();
        // Announced, and focus (which was in the closed panel) moves to it.
        const region = notice.closest('[aria-live="polite"]');
        expect(region).not.toBeNull();
        expect(region).toBe(document.activeElement);
      } finally {
        vi.useRealTimers();
      }
    });

    it("follows a link to the other instance", async () => {
      renderTab();
      fireEvent.click(
        await screen.findByRole("button", {
          name: "Open Acme Corp (Customer)",
        }),
      );
      await screen.findByText("info@acme.test");
      fireEvent.click(
        screen.getByRole("button", { name: "placed Order 1001" }),
      );
      await waitFor(() =>
        expect(getDomainInstance).toHaveBeenLastCalledWith(
          api,
          "ns",
          ORDER_1001.iri,
        ),
      );
      // The incoming link leads back.
      expect(
        await screen.findByRole("button", { name: "Acme Corp placed" }),
      ).toBeInTheDocument();
      expect(screen.queryByText("info@acme.test")).not.toBeInTheDocument();
    });

    it("ignores a detail answer that arrives after a later click", async () => {
      let resolveSlow: (value: unknown) => void = () => {};
      getDomainInstance
        .mockReturnValueOnce(new Promise((resolve) => (resolveSlow = resolve)))
        .mockResolvedValueOnce(ORDER_DETAIL);
      renderTab();
      fireEvent.click(
        await screen.findByRole("button", {
          name: "Open Acme Corp (Customer)",
        }),
      );
      fireEvent.click(
        screen.getByRole("button", { name: "Open Order 1001 (Order)" }),
      );
      expect(
        await screen.findByRole("button", { name: "Acme Corp placed" }),
      ).toBeInTheDocument();

      await act(async () => resolveSlow(ACME_DETAIL));
      expect(screen.queryByText("info@acme.test")).not.toBeInTheDocument();
    });

    it("shows why an instance could not be loaded", async () => {
      getDomainInstance.mockRejectedValue(
        new ApiError(
          404,
          "UNKNOWN_ERROR",
          "Instance not found in the domain ABox",
          {
            detail: "Instance not found in the domain ABox",
          },
        ),
      );
      renderTab();
      fireEvent.click(
        await screen.findByRole("button", {
          name: "Open Acme Corp (Customer)",
        }),
      );
      expect(
        await screen.findByText("Instance not found in the domain ABox"),
      ).toBeInTheDocument();
    });
  });

  describe("table counts", () => {
    /** A header counter such as `(12)` or `(50+)`. */
    const COUNTER = /^\(\d+\+?\)$/;

    it("counts no rows while the tables load, and the rows once they are read", async () => {
      let state: (value: unknown) => void = () => {};
      let instances: (value: unknown) => void = () => {};
      let mappings: (value: unknown) => void = () => {};
      getDomainAbox.mockReturnValue(
        new Promise((resolve) => (state = resolve)),
      );
      listDomainInstances.mockReturnValue(
        new Promise((resolve) => (instances = resolve)),
      );
      listAboxMappings.mockReturnValue(
        new Promise((resolve) => (mappings = resolve)),
      );
      renderTab();
      // The lists wait for the state, and then load: both tables say so, with no count.
      expect(screen.getByText("Loading instances")).toBeInTheDocument();
      expect(screen.getByText("Loading mappings")).toBeInTheDocument();
      expect(screen.queryByText(COUNTER)).not.toBeInTheDocument();
      await act(async () => state(READY));
      expect(listDomainInstances).toHaveBeenCalledTimes(1);
      expect(listAboxMappings).toHaveBeenCalledTimes(1);
      expect(screen.getByText("Loading instances")).toBeInTheDocument();
      expect(screen.getByText("Loading mappings")).toBeInTheDocument();
      expect(screen.queryByText(COUNTER)).not.toBeInTheDocument();

      await act(async () => {
        instances({ instances: [], nextToken: null });
        mappings({ mappings: [CLIENT_ROW] });
      });
      expect(screen.getByText("No instances yet.")).toBeInTheDocument();
      expect(screen.getByText("(0)")).toBeInTheDocument();
      expect(screen.getByText("(1)")).toBeInTheDocument();
    });

    it("counts no mappings of another kind while they load", async () => {
      renderTab();
      await screen.findByRole("button", { name: "Change target of Client" });
      // Two instances, and two class mappings.
      expect(screen.getAllByText("(2)")).toHaveLength(2);
      listAboxMappings.mockReturnValue(new Promise(() => {}));
      fireEvent.click(screen.getByRole("button", { name: "Relations" }));
      expect(await screen.findByText("Loading mappings")).toBeInTheDocument();
      expect(screen.getAllByText("(2)")).toHaveLength(1);
      expect(screen.queryByText("(0)")).not.toBeInTheDocument();
    });
  });

  describe("mappings", () => {
    it("lists the class mappings with their key, target and origin", async () => {
      renderTab();
      expect(
        await screen.findByRole("button", { name: "Change target of Client" }),
      ).toBeInTheDocument();
      expect(listAboxMappings).toHaveBeenCalledWith(api, "ns", {
        kind: "class",
      });
      const client = within(rowOf("Client"));
      expect(client.getByText("Customer")).toBeInTheDocument();
      expect(client.getByText("auto")).toBeInTheDocument();
      expect(client.getByText("Same concept")).toBeInTheDocument();
      expect(within(rowOf("Shopper")).getByText("manual")).toBeInTheDocument();
    });

    it("names the classes of a relation key and marks an inverse target", async () => {
      listAboxMappings.mockImplementation((_a, _ns, opts: { kind?: string }) =>
        Promise.resolve({
          mappings:
            opts.kind === "relation"
              ? [{ ...PLACED_ROW, direction: "inverse" }]
              : [CLIENT_ROW],
        }),
      );
      renderTab();
      await screen.findByText("Ready");
      fireEvent.click(screen.getByRole("button", { name: "Relations" }));
      await waitFor(() =>
        expect(listAboxMappings).toHaveBeenLastCalledWith(api, "ns", {
          kind: "relation",
        }),
      );
      expect(
        await screen.findByText("Customer -PLACED-> Order"),
      ).toBeInTheDocument();
      expect(screen.getByText("placed (inverse)")).toBeInTheDocument();
    });

    it("names key classes it has no label for from their IRI", async () => {
      listAboxMappings.mockImplementation((_a, _ns, opts: { kind?: string }) =>
        Promise.resolve({
          mappings:
            opts.kind === "relation"
              ? [
                  {
                    ...PLACED_ROW,
                    key: [
                      "SHIPS_TO",
                      "https://e/o#Sales%20Order",
                      "https://e/o/Address",
                    ],
                  },
                ]
              : [CLIENT_ROW],
        }),
      );
      renderTab();
      await screen.findByText("Ready");
      fireEvent.click(screen.getByRole("button", { name: "Relations" }));
      expect(
        await screen.findByText("Sales Order -SHIPS_TO-> Address"),
      ).toBeInTheDocument();
    });

    it("shows only the rows of the picked status", async () => {
      const rows = [CLIENT_ROW, { ...SHOPPER_ROW, status: "stale" }];
      let resolveSlow: (value: unknown) => void = () => {};
      listAboxMappings.mockImplementation(
        (_a, _ns, opts: { kind?: string; status?: string }) =>
          opts.status === "ignored"
            ? new Promise((resolve) => (resolveSlow = resolve))
            : Promise.resolve({
                mappings: rows.filter(
                  (r) => !opts.status || r.status === opts.status,
                ),
              }),
      );
      renderTab();
      await screen.findByRole("button", { name: "Change target of Client" });

      pickOption(/^Filter by status/, "Stale");
      expect(
        await screen.findByRole("button", { name: "Change target of Shopper" }),
      ).toBeInTheDocument();
      await waitFor(() =>
        expect(
          screen.queryByRole("button", { name: "Change target of Client" }),
        ).not.toBeInTheDocument(),
      );

      // The answer for Ignored is slow; Active answers first and wins.
      pickOption(/^Filter by status/, "Ignored");
      pickOption(/^Filter by status/, "Active");
      expect(
        await screen.findByRole("button", { name: "Change target of Client" }),
      ).toBeInTheDocument();
      await act(async () => resolveSlow({ mappings: [] }));
      expect(
        screen.getByRole("button", { name: "Change target of Client" }),
      ).toBeInTheDocument();
    });

    it("says when no row has the picked status", async () => {
      listAboxMappings.mockImplementation(
        (_a, _ns, opts: { status?: string }) =>
          Promise.resolve({
            mappings: ROWS.filter(
              (r) =>
                r.kind === "class" &&
                (!opts.status || r.status === opts.status),
            ),
          }),
      );
      renderTab();
      await screen.findByRole("button", { name: "Change target of Client" });
      pickOption(/^Filter by status/, "Stale");
      expect(
        await screen.findByText("No mappings match the filter."),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Change target of Client" }),
      ).not.toBeInTheDocument();
    });

    it("filters the mappings by status", async () => {
      renderTab();
      await screen.findByRole("button", { name: "Change target of Client" });
      pickOption(/^Filter by status/, "Stale");
      await waitFor(() =>
        expect(listAboxMappings).toHaveBeenLastCalledWith(api, "ns", {
          kind: "class",
          status: "stale",
        }),
      );
    });

    it("changes a class mapping to a searched target", async () => {
      renderTab();
      const dialog = await pickTarget("Client", "purchaser", "Shopper");
      expect(searchEntities).toHaveBeenCalledWith(api, "ns", "purchaser", {
        kind: "class",
        limit: 15,
      });
      expect(
        within(dialog).queryByRole("button", { name: /^Direction/ }),
      ).not.toBeInTheDocument();
      const callsBefore = getDomainAbox.mock.calls.length;
      fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
      await waitFor(() =>
        expect(updateAboxMapping).toHaveBeenCalledWith(api, "ns", "class.aa", {
          target: "https://e/o#Shopper",
        }),
      );
      // The dialog closes; the mappings and the state (a rebuild was queued) reload.
      await waitFor(() =>
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
      );
      await waitFor(() => expect(listAboxMappings).toHaveBeenCalledTimes(2));
      expect(getDomainAbox.mock.calls.length).toBeGreaterThan(callsBefore);
    });

    it("does not take typed text as a target on Enter", async () => {
      renderTab();
      fireEvent.click(
        await screen.findByRole("button", { name: "Change target of Client" }),
      );
      const dialog = screen.getByRole("dialog");
      const input = within(dialog).getByRole("combobox", { name: "Target" });
      fireEvent.focus(input);
      fireEvent.change(input, { target: { value: "purchaser" } });
      await within(dialog).findByText("Shopper");
      // Cloudscape reads keyCode; Enter with nothing highlighted selects the typed text.
      fireEvent.keyDown(input, { key: "Enter", keyCode: 13 });
      const save = within(dialog).getByRole("button", { name: "Save" });
      expect(save).toHaveAttribute("aria-disabled", "true");
      fireEvent.click(save);
      expect(updateAboxMapping).not.toHaveBeenCalled();
    });

    it("keeps Save disabled until a target is picked", async () => {
      renderTab();
      fireEvent.click(
        await screen.findByRole("button", { name: "Change target of Client" }),
      );
      expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute(
        "aria-disabled",
        "true",
      );
    });

    it("sends the direction picked for a relation", async () => {
      searchEntities.mockResolvedValue([
        {
          uri: "https://e/o#placedBy",
          label: "placed by",
          kind: "object-property",
          graph_uris: [],
        },
      ]);
      renderTab();
      await screen.findByText("Ready");
      fireEvent.click(screen.getByRole("button", { name: "Relations" }));
      const dialog = await pickTarget(
        "Customer -PLACED-> Order",
        "orders",
        "placed by",
      );
      expect(searchEntities).toHaveBeenCalledWith(api, "ns", "orders", {
        kind: "object-property",
        limit: 15,
      });
      pickOption(/^Direction/, "Inverse");
      fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
      await waitFor(() =>
        expect(updateAboxMapping).toHaveBeenCalledWith(
          api,
          "ns",
          "relation.cc",
          { target: "https://e/o#placedBy", direction: "inverse" },
        ),
      );
    });

    it("asks for the direction of the picked property instead of keeping the row's", async () => {
      // Which direction fits depends on the property's domain and range.
      listAboxMappings.mockImplementation((_a, _ns, opts: { kind?: string }) =>
        Promise.resolve({
          mappings:
            opts.kind === "relation"
              ? [{ ...PLACED_ROW, direction: "inverse" }]
              : [CLIENT_ROW],
        }),
      );
      searchEntities.mockResolvedValue([
        {
          uri: "https://e/o#placedOrder",
          label: "placed order",
          kind: "object-property",
          graph_uris: [],
        },
      ]);
      renderTab();
      await screen.findByText("Ready");
      fireEvent.click(screen.getByRole("button", { name: "Relations" }));
      const dialog = await pickTarget(
        "Customer -PLACED-> Order",
        "orders",
        "placed order",
      );
      const save = within(dialog).getByRole("button", { name: "Save" });
      expect(save).toHaveAttribute("aria-disabled", "true");
      fireEvent.click(save);
      expect(updateAboxMapping).not.toHaveBeenCalled();
      pickOption(/^Direction/, "Forward");
      fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
      await waitFor(() =>
        expect(updateAboxMapping).toHaveBeenCalledWith(
          api,
          "ns",
          "relation.cc",
          { target: "https://e/o#placedOrder", direction: "forward" },
        ),
      );
    });

    it("ignores a class mapping", async () => {
      renderTab();
      fireEvent.click(
        await screen.findByRole("button", { name: "Change target of Client" }),
      );
      expect(
        screen.queryByRole("button", { name: "Use free attribute" }),
      ).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Ignore" }));
      await waitFor(() =>
        expect(updateAboxMapping).toHaveBeenCalledWith(api, "ns", "class.aa", {
          target: "ignore",
        }),
      );
    });

    it("ignores a relation without sending a direction", async () => {
      renderTab();
      await screen.findByText("Ready");
      fireEvent.click(screen.getByRole("button", { name: "Relations" }));
      fireEvent.click(
        await screen.findByRole("button", {
          name: "Change target of Customer -PLACED-> Order",
        }),
      );
      pickOption(/^Direction/, "Inverse");
      fireEvent.click(screen.getByRole("button", { name: "Ignore" }));
      await waitFor(() =>
        expect(updateAboxMapping).toHaveBeenCalledWith(
          api,
          "ns",
          "relation.cc",
          { target: "ignore" },
        ),
      );
    });

    it("keeps an attribute as free but never offers to ignore it", async () => {
      renderTab();
      await screen.findByText("Ready");
      fireEvent.click(screen.getByRole("button", { name: "Attributes" }));
      fireEvent.click(
        await screen.findByRole("button", {
          name: "Change target of Customer: industry",
        }),
      );
      expect(
        screen.queryByRole("button", { name: "Ignore" }),
      ).not.toBeInTheDocument();
      fireEvent.click(
        screen.getByRole("button", { name: "Use free attribute" }),
      );
      await waitFor(() =>
        expect(updateAboxMapping).toHaveBeenCalledWith(
          api,
          "ns",
          "attribute.dd",
          { target: "free" },
        ),
      );
    });

    it("keeps the dialog open with the reason when the mapping changed meanwhile", async () => {
      const message = "The mapping changed meanwhile; reload and try again";
      updateAboxMapping.mockRejectedValue(
        new ApiError(409, "UNKNOWN_ERROR", message, { detail: message }),
      );
      renderTab();
      const dialog = await pickTarget("Client", "purchaser", "Shopper");
      fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
      expect(await within(dialog).findByText(message)).toBeInTheDocument();
      // The rows are re-read so the next attempt starts from the current one.
      await waitFor(() => expect(listAboxMappings).toHaveBeenCalledTimes(2));
      expect(screen.getByRole("dialog")).toBeInTheDocument();
    });

    it("offers Reset to auto only for manual rows", async () => {
      renderTab();
      await screen.findByRole("button", { name: "Change target of Client" });
      expect(
        screen.queryByRole("button", { name: "Reset Client to auto" }),
      ).not.toBeInTheDocument();
      fireEvent.click(
        screen.getByRole("button", { name: "Reset Shopper to auto" }),
      );
      await waitFor(() =>
        expect(resetAboxMapping).toHaveBeenCalledWith(api, "ns", "class.bb"),
      );
      await waitFor(() => expect(listAboxMappings).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(getDomainAbox).toHaveBeenCalledTimes(2));
    });

    it("shows a reset row as auto, its target left to the next build, at once", async () => {
      const reset = { ...SHOPPER_ROW, origin: "auto" };
      resetAboxMapping.mockImplementation(() => {
        // The re-read rows still name the old target: the matcher decides at the next build.
        listAboxMappings.mockResolvedValue({
          mappings: [CLIENT_ROW, reset],
        });
        return Promise.resolve(reset);
      });
      renderTab();
      fireEvent.click(
        await screen.findByRole("button", { name: "Reset Shopper to auto" }),
      );
      await waitFor(() => expect(listAboxMappings).toHaveBeenCalledTimes(2));
      expect(
        await screen.findByText("Chosen by the next build"),
      ).toBeInTheDocument();
      const row = rowOf("Shopper");
      expect(within(row).queryByText("Customer")).not.toBeInTheDocument();
      expect(within(row).getByText("auto")).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Reset Shopper to auto" }),
      ).not.toBeInTheDocument();
    });

    it("keeps a reset row's target to the next build until the rows of that build are read", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        const reset = { ...SHOPPER_ROW, origin: "auto" };
        // What the next build's matcher chose for the reset row.
        const rebuilt = {
          ...reset,
          target: "https://e/o#Buyer",
          targetLabel: "Buyer",
          rationale: "Same concept",
        };
        let answerRebuilt: (v: { mappings: unknown[] }) => void = () => {};
        listAboxMappings
          .mockResolvedValueOnce({ mappings: [CLIENT_ROW, SHOPPER_ROW] })
          .mockResolvedValueOnce({ mappings: [CLIENT_ROW, reset] })
          .mockReturnValueOnce(
            new Promise((resolve) => {
              answerRebuilt = resolve;
            }),
          );
        resetAboxMapping.mockResolvedValue(reset);
        getDomainAbox
          .mockResolvedValueOnce(READY)
          // Right after the reset: the rebuild it queued has not landed yet.
          .mockResolvedValueOnce(READY)
          .mockResolvedValue({ ...READY, buildId: "b-2", builtAt: LATER });
        renderTab();
        fireEvent.click(
          await screen.findByRole("button", { name: "Reset Shopper to auto" }),
        );
        expect(
          await screen.findByText("Chosen by the next build"),
        ).toBeInTheDocument();

        // The build lands (Ready, a new build time); its rows are still being read.
        await advance(3100);
        expect(
          await screen.findByText(`Last built ${formatTimestamp(LATER)}`),
        ).toBeInTheDocument();
        await waitFor(() => expect(listAboxMappings).toHaveBeenCalledTimes(3));
        const row = rowOf("Shopper");
        expect(
          within(row).getByText("Chosen by the next build"),
        ).toBeInTheDocument();
        expect(within(row).queryByText("Customer")).not.toBeInTheDocument();

        // The rows of the new build name the target its matcher chose.
        await act(async () =>
          answerRebuilt({ mappings: [CLIENT_ROW, rebuilt] }),
        );
        expect(
          await within(rowOf("Shopper")).findByText("Buyer"),
        ).toBeInTheDocument();
        expect(
          screen.queryByText("Chosen by the next build"),
        ).not.toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
    });

    it("leaves a row reset after a build landed to the build after it, even while the landed build's rows are read", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        const clientManual = { ...CLIENT_ROW, origin: "manual" };
        const clientReset = { ...CLIENT_ROW, origin: "auto" };
        const shopperReset = { ...SHOPPER_ROW, origin: "auto" };
        // What the build that lands chose for Shopper, reset before it.
        const shopperRebuilt = {
          ...shopperReset,
          target: "https://e/o#Buyer",
          targetLabel: "Buyer",
          rationale: "Same concept",
        };
        // What the build after it chose for Client.
        const clientRebuilt = {
          ...clientReset,
          target: "https://e/o#Account",
          targetLabel: "Account",
        };
        listAboxMappings
          .mockResolvedValueOnce({ mappings: [clientManual, SHOPPER_ROW] })
          .mockResolvedValueOnce({ mappings: [clientManual, shopperReset] })
          // The build landed: the read of its rows is still on its way.
          .mockReturnValueOnce(new Promise(() => {}))
          .mockResolvedValue({ mappings: [clientReset, shopperRebuilt] });
        resetAboxMapping.mockImplementation((_a, _ns, id: string) =>
          Promise.resolve(id === CLIENT_ROW.id ? clientReset : shopperReset),
        );
        getDomainAbox
          .mockResolvedValueOnce(READY)
          .mockResolvedValueOnce(READY)
          .mockResolvedValue({ ...READY, buildId: "b-2", builtAt: LATER });
        renderTab();
        fireEvent.click(
          await screen.findByRole("button", { name: "Reset Shopper to auto" }),
        );
        expect(
          await screen.findByText("Chosen by the next build"),
        ).toBeInTheDocument();

        // The build lands; the user resets Client before its rows are read.
        await advance(3100);
        expect(
          await screen.findByText(`Last built ${formatTimestamp(LATER)}`),
        ).toBeInTheDocument();
        await waitFor(() => expect(listAboxMappings).toHaveBeenCalledTimes(3));
        fireEvent.click(
          screen.getByRole("button", { name: "Reset Client to auto" }),
        );

        // The rows of the landed build settle Shopper, not Client.
        expect(
          await within(rowOf("Shopper")).findByText("Buyer"),
        ).toBeInTheDocument();
        const client = rowOf("Client");
        expect(
          within(client).getByText("Chosen by the next build"),
        ).toBeInTheDocument();
        expect(within(client).queryByText("Customer")).not.toBeInTheDocument();

        // The build after it lands, and its rows name Client's new target.
        listAboxMappings.mockResolvedValue({
          mappings: [clientRebuilt, shopperRebuilt],
        });
        getDomainAbox.mockResolvedValue({
          ...READY,
          buildId: "b-3",
          builtAt: "2026-10-17T08:00:00Z",
        });
        await advance(3100);
        expect(
          await within(rowOf("Client")).findByText("Account"),
        ).toBeInTheDocument();
        expect(
          screen.queryByText("Chosen by the next build"),
        ).not.toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
    });

    it("leaves a row whose reset answers after a build landed to the build after it", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        const reset = { ...SHOPPER_ROW, origin: "auto" };
        const rebuilt = {
          ...reset,
          target: "https://e/o#Buyer",
          targetLabel: "Buyer",
          rationale: "Same concept",
        };
        let answerReset: (v: unknown) => void = () => {};
        resetAboxMapping.mockReturnValueOnce(
          new Promise((resolve) => {
            answerReset = resolve;
          }),
        );
        let answerRows: (v: { mappings: unknown[] }) => void = () => {};
        listAboxMappings
          .mockResolvedValueOnce({ mappings: [CLIENT_ROW, SHOPPER_ROW] })
          // The build that ran started before the reset, so it kept Shopper's stored target.
          .mockResolvedValueOnce({ mappings: [CLIENT_ROW, reset] })
          .mockReturnValueOnce(
            new Promise((resolve) => {
              answerRows = resolve;
            }),
          );
        getDomainAbox
          .mockResolvedValueOnce({
            ...READY,
            status: "BUILDING",
            buildId: "b-2",
          })
          .mockResolvedValue({ ...READY, buildId: "b-2", builtAt: LATER });
        renderTab();
        fireEvent.click(
          await screen.findByRole("button", { name: "Reset Shopper to auto" }),
        );

        // The running build lands before the reset answers.
        await advance(3100);
        expect(
          await screen.findByText(`Last built ${formatTimestamp(LATER)}`),
        ).toBeInTheDocument();
        await waitFor(() => expect(listAboxMappings).toHaveBeenCalledTimes(2));
        await act(async () => answerReset(reset));
        await waitFor(() => expect(listAboxMappings).toHaveBeenCalledTimes(3));
        await act(async () => answerRows({ mappings: [CLIENT_ROW, reset] }));
        const row = rowOf("Shopper");
        expect(
          within(row).getByText("Chosen by the next build"),
        ).toBeInTheDocument();
        expect(within(row).queryByText("Customer")).not.toBeInTheDocument();

        // The build the reset queued lands, and its rows name the new target.
        listAboxMappings.mockResolvedValue({ mappings: [CLIENT_ROW, rebuilt] });
        getDomainAbox.mockResolvedValue({
          ...READY,
          buildId: "b-3",
          builtAt: "2026-10-17T08:00:00Z",
        });
        await advance(3100);
        expect(
          await within(rowOf("Shopper")).findByText("Buyer"),
        ).toBeInTheDocument();
        expect(
          screen.queryByText("Chosen by the next build"),
        ).not.toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
    });

    it("re-reads the rows when a reset finds the mapping changed meanwhile", async () => {
      const message = "The mapping changed meanwhile; reload and try again";
      resetAboxMapping.mockRejectedValue(
        new ApiError(409, "UNKNOWN_ERROR", message, { detail: message }),
      );
      renderTab();
      fireEvent.click(
        await screen.findByRole("button", { name: "Reset Shopper to auto" }),
      );
      expect(await screen.findByText(message)).toBeInTheDocument();
      expect(
        screen.getByText("Could not reset the mapping"),
      ).toBeInTheDocument();
      await waitFor(() => expect(listAboxMappings).toHaveBeenCalledTimes(2));
    });

    it("shows why a reset failed", async () => {
      resetAboxMapping.mockRejectedValue(
        new ApiError(404, "UNKNOWN_ERROR", "Mapping not found", {
          detail: "Mapping not found",
        }),
      );
      renderTab();
      fireEvent.click(
        await screen.findByRole("button", { name: "Reset Shopper to auto" }),
      );
      expect(await screen.findByText("Mapping not found")).toBeInTheDocument();
    });
  });

  it("starts from scratch when the namespace changes", async () => {
    const view = renderTab();
    fireEvent.click(
      await screen.findByRole("button", { name: "Open Acme Corp (Customer)" }),
    );
    expect(await screen.findByText("info@acme.test")).toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole("button", { name: "Change target of Client" }),
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // The other namespace is still loading.
    getDomainAbox.mockReturnValue(new Promise(() => {}));
    view.rerender(<DomainDataTab apiClient={api} namespace="other" />);

    expect(getDomainAbox).toHaveBeenLastCalledWith(api, "other");
    expect(screen.queryByText("Ready")).not.toBeInTheDocument();
    expect(screen.queryByText("info@acme.test")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Open Acme Corp (Customer)" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
