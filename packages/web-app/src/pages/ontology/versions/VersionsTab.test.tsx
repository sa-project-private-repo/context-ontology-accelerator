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
} from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { VersionsTab } from "./VersionsTab";
import { ApiError, type ApiClient } from "@components/ApiClientProvider";

// The caller's role: true for one that can change the ontology (a data steward).
const access = vi.hoisted(() => ({ canManage: true }));
vi.mock("@api-hooks/use-can-manage-ontology", () => ({
  useCanManageOntology: () => access.canManage,
}));

const listOntologyVersions = vi.fn();
const getOntologyVersion = vi.fn();
const deployOntologyVersion = vi.fn();
vi.mock("../../../services/ontology-engine", () => ({
  listOntologyVersions: (...a: unknown[]) => listOntologyVersions(...a),
  getOntologyVersion: (...a: unknown[]) => getOntologyVersion(...a),
  deployOntologyVersion: (...a: unknown[]) => deployOntologyVersion(...a),
}));

const api = {
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
} as unknown as ApiClient;

const V1 = {
  version: 1,
  status: "PUBLISHED",
  ontologyId: "o",
  source: "BOOTSTRAP",
  createdBy: "system",
};
const V2 = {
  version: 2,
  status: "PUBLISHED",
  parentVersion: 1,
  ontologyId: "o",
  source: "PROPOSAL",
  createdBy: "alice",
  diffSummary: { classes: { added: 3, removed: 1, changed: 0 } },
};

function detail(breakingMetrics: unknown[] = []) {
  return {
    version: V2,
    manifest: {},
    diff: {},
    downloadUrls: {},
    breakingMetrics,
  };
}

const ORDER_METRIC = {
  metricId: "revenue",
  name: "revenue",
  missingClassIri: "https://e/o#Order",
};

/** A deployment of v2 that failed after its swap, so serving is half-swapped. */
const PARTIAL_V2 = {
  deploymentId: "d",
  version: 2,
  status: "DEPLOY_FAILED",
  partial: true,
  progress: { stage: "done", swap: "done", embeddings: "failed" },
};

/** Advance fake timers inside act, so the state updates of the polls they fire are flushed. */
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

const PUBLISHING_LIST = {
  versions: [{ ...V2, status: "PUBLISHING" }, V1],
  activeVersion: 1,
  latestDeployment: null,
};
const PUBLISHED_LIST = {
  versions: [V2, V1],
  activeVersion: 1,
  latestDeployment: null,
};

async function openDeployDialog() {
  fireEvent.click(
    await screen.findByRole("button", { name: "Deploy version 2" }),
  );
  const confirm = await screen.findByRole("button", { name: "Deploy v2" });
  await waitFor(() =>
    expect(confirm).not.toHaveAttribute("aria-disabled", "true"),
  );
  return confirm;
}

describe("VersionsTab", () => {
  beforeEach(() => {
    cleanup();
    // Reset, not just clear: a test that fails early must not leave queued
    // mockResolvedValueOnce answers for the next one.
    vi.resetAllMocks();
    access.canManage = true;
    listOntologyVersions.mockResolvedValue({
      versions: [V2, V1],
      activeVersion: 1,
      latestDeployment: null,
    });
    deployOntologyVersion.mockResolvedValue({ deploymentId: "d-1" });
  });

  describe("by role", () => {
    const CANNOT_DEPLOY =
      "Your role on this namespace cannot deploy or roll back versions.";

    it("offers no deploy or redeploy to a role that only reads, and says why", async () => {
      access.canManage = false;
      render(<VersionsTab apiClient={api} namespace="ns" />);
      const deploy = await screen.findByRole("button", {
        name: "Deploy version 2",
      });
      expect(deploy).toBeDisabled();
      expect(
        screen.getByRole("button", { name: "Redeploy version 1" }),
      ).toBeDisabled();
      expect(screen.getByText(CANNOT_DEPLOY)).toBeInTheDocument();
      // Reading stays offered.
      expect(
        screen.getByRole("button", { name: "Details of version 2" }),
      ).not.toBeDisabled();
      fireEvent.click(deploy);
      expect(getOntologyVersion).not.toHaveBeenCalled();
    });

    it("offers deploys to a role that can change the ontology", async () => {
      render(<VersionsTab apiClient={api} namespace="ns" />);
      expect(
        await screen.findByRole("button", { name: "Deploy version 2" }),
      ).not.toBeDisabled();
      expect(screen.queryByText(CANNOT_DEPLOY)).toBeNull();
    });
  });

  it("lists versions with the active badge and change summary", async () => {
    render(<VersionsTab apiClient={api} namespace="ns" />);
    expect(await screen.findByText("v2")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText("Classes +3 / −1")).toBeInTheDocument();
  });

  it("says the next version builds on the newest version when an older one is active", async () => {
    render(<VersionsTab apiClient={api} namespace="ns" />);
    expect(
      await screen.findByText(
        "The next version builds on v2, not the active v1; its changes carry over unless you revert them.",
      ),
    ).toBeInTheDocument();
  });

  it("says nothing about the next version when the newest version is active", async () => {
    listOntologyVersions.mockResolvedValue({
      versions: [V2, V1],
      activeVersion: 2,
      latestDeployment: null,
    });
    render(<VersionsTab apiClient={api} namespace="ns" />);
    expect(await screen.findByText("v2")).toBeInTheDocument();
    expect(screen.queryByText(/The next version builds on/)).toBeNull();
  });

  it("waits for a running deployment before saying what the next version builds on", async () => {
    listOntologyVersions.mockResolvedValue({
      versions: [V2, V1],
      activeVersion: 1,
      latestDeployment: {
        deploymentId: "d",
        version: 2,
        status: "DEPLOYING",
        partial: false,
        progress: {},
      },
    });
    render(<VersionsTab apiClient={api} namespace="ns" />);
    expect(await screen.findByText("v2")).toBeInTheDocument();
    expect(screen.queryByText(/The next version builds on/)).toBeNull();
  });

  it("does not count a version still publishing as the one the next version builds on", async () => {
    listOntologyVersions.mockResolvedValue({
      versions: [{ ...V2, status: "PUBLISHING" }, V1],
      activeVersion: 1,
      latestDeployment: null,
    });
    render(<VersionsTab apiClient={api} namespace="ns" />);
    expect(await screen.findByText("v2")).toBeInTheDocument();
    expect(screen.queryByText(/The next version builds on/)).toBeNull();
  });

  it("says the deploy asks the VKG to reload, which takes a few minutes", async () => {
    getOntologyVersion.mockResolvedValue(detail());
    render(<VersionsTab apiClient={api} namespace="ns" />);
    await openDeployDialog();
    expect(
      screen.getByText(
        "This replaces the served ontology graph and its embeddings with v2, and asks the VKG to reload its mappings, which takes effect within a few minutes.",
      ),
    ).toBeInTheDocument();
  });

  it("deploys a version with no breaking metrics", async () => {
    getOntologyVersion.mockResolvedValue(detail());
    render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Deploy version 2" }),
    );
    const confirm = await screen.findByRole("button", { name: "Deploy v2" });
    // The confirm button stays disabled until the impact check (getOntologyVersion) resolves.
    await waitFor(() =>
      expect(confirm).not.toHaveAttribute("aria-disabled", "true"),
    );
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(deployOntologyVersion).toHaveBeenCalledWith(api, "ns", 2, []),
    );
  });

  it("requires acknowledging breaking metrics before deploying", async () => {
    getOntologyVersion.mockResolvedValue(
      detail([
        {
          metricId: "revenue",
          name: "revenue",
          missingClassIri: "https://e/o#Order",
        },
      ]),
    );
    render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Deploy version 2" }),
    );
    expect(await screen.findByText("https://e/o#Order")).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Deploy v2" });
    expect(confirm).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Deploy v2" }));
    await waitFor(() =>
      expect(deployOntologyVersion).toHaveBeenCalledWith(api, "ns", 2, [
        "revenue",
      ]),
    );
  });

  it("does not ask to redeploy a partially deployed version that is now retired", async () => {
    listOntologyVersions.mockResolvedValue({
      versions: [V2, V1],
      activeVersion: null,
      resetAfterVersion: 2,
      latestDeployment: PARTIAL_V2,
      servingDeployment: PARTIAL_V2,
    });
    render(<VersionsTab apiClient={api} namespace="ns" />);
    expect(
      await screen.findByText("Retired: its ontology was deleted"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Partially deployed: redeploy to finish"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Redeploy required")).not.toBeInTheDocument();
  });

  it("does not report a retired version's deployment as deployed", async () => {
    const deployed = {
      deploymentId: "d",
      version: 2,
      status: "DEPLOYED",
      partial: false,
      progress: {},
    };
    listOntologyVersions.mockResolvedValue({
      versions: [V2, V1],
      activeVersion: null,
      resetAfterVersion: 2,
      latestDeployment: deployed,
      servingDeployment: deployed,
    });
    render(<VersionsTab apiClient={api} namespace="ns" />);
    expect(
      await screen.findByText("Retired: its ontology was deleted"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Deployed")).not.toBeInTheDocument();
  });

  it("names who requested the latest deployment", async () => {
    listOntologyVersions.mockResolvedValue({
      versions: [V2, V1],
      activeVersion: 1,
      latestDeployment: {
        deploymentId: "d",
        version: 1,
        status: "DEPLOYED",
        partial: false,
        progress: {},
        requestedBy: "alice@example.com",
      },
    });
    render(<VersionsTab apiClient={api} namespace="ns" />);
    expect(
      await screen.findByText("Requested by alice@example.com"),
    ).toBeInTheDocument();
  });

  it("warns when the latest deployment is partial", async () => {
    listOntologyVersions.mockResolvedValue({
      versions: [V2, V1],
      activeVersion: 2,
      latestDeployment: PARTIAL_V2,
      servingDeployment: PARTIAL_V2,
    });
    render(<VersionsTab apiClient={api} namespace="ns" />);
    expect(
      await screen.findByText("Partially deployed: redeploy to finish"),
    ).toBeInTheDocument();
  });

  it("shows a redeploy-required banner and offers Redeploy for the partially deployed version", async () => {
    listOntologyVersions.mockResolvedValue({
      versions: [V2, V1],
      // The registry step never ran, so the registry still names v1 as active.
      activeVersion: 1,
      latestDeployment: PARTIAL_V2,
      servingDeployment: PARTIAL_V2,
    });
    render(<VersionsTab apiClient={api} namespace="ns" />);
    expect(await screen.findByText("Redeploy required")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Redeploy version 2" }),
    ).toHaveTextContent("Redeploy");
  });

  it("keeps asking for the redeploy after a later attempt failed before its swap", async () => {
    listOntologyVersions.mockResolvedValue({
      versions: [V2, V1],
      // v2's MOVE landed but ACTIVE was never moved off v1.
      activeVersion: 1,
      // The redeploy failed while staging, so serving is still what PARTIAL_V2 left.
      latestDeployment: {
        deploymentId: "d-2",
        version: 2,
        status: "DEPLOY_FAILED",
        partial: false,
        progress: { stage: "failed" },
      },
      servingDeployment: PARTIAL_V2,
    });
    render(<VersionsTab apiClient={api} namespace="ns" />);
    expect(await screen.findByText("Redeploy required")).toBeInTheDocument();
    expect(screen.getByText("Deploy failed")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Redeploy version 2" }),
    ).toHaveTextContent("Redeploy");
  });

  it("marks versions of a deleted ontology as retired and offers no deploy for them", async () => {
    listOntologyVersions.mockResolvedValue({
      versions: [{ ...V2, parentVersion: null, ontologyId: "o2" }, V1],
      activeVersion: null,
      latestDeployment: null,
      resetAfterVersion: 1,
    });
    render(<VersionsTab apiClient={api} namespace="ns" />);
    expect(
      await screen.findByRole("button", { name: "Deploy version 2" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Retired")).toHaveLength(1);
    expect(
      screen.queryByRole("button", {
        name: /^(Deploy|Redeploy|Roll back to) version 1$/,
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Details of version 1" }),
    ).toBeInTheDocument();
  });

  it("names roll back and redeploy in the row, the dialog header and the confirm button", async () => {
    listOntologyVersions.mockResolvedValue({
      versions: [V2, V1],
      activeVersion: 2,
      latestDeployment: null,
    });
    getOntologyVersion.mockResolvedValue({ ...detail(), version: V1 });
    render(<VersionsTab apiClient={api} namespace="ns" />);
    expect(
      await screen.findByRole("button", { name: "Redeploy version 2" }),
    ).toHaveTextContent("Redeploy");

    fireEvent.click(
      screen.getByRole("button", { name: "Roll back to version 1" }),
    );
    expect(await screen.findByText("Roll back to v1?")).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Roll back to v1" });
    await waitFor(() =>
      expect(confirm).not.toHaveAttribute("aria-disabled", "true"),
    );
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(deployOntologyVersion).toHaveBeenCalledWith(api, "ns", 1, []),
    );
  });

  it("closes the dialog and re-fetches the list after a successful deploy", async () => {
    getOntologyVersion.mockResolvedValue(detail());
    render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(await openDeployDialog());

    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Deploy v2" }),
      ).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(listOntologyVersions).toHaveBeenCalledTimes(2));
  });

  it("explains a failed impact check and keeps deploy disabled", async () => {
    getOntologyVersion.mockRejectedValue(
      new ApiError(409, "UNKNOWN_ERROR", "Version 2 has no snapshot manifest", {
        detail: "Version 2 has no snapshot manifest",
      }),
    );
    render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Deploy version 2" }),
    );

    expect(
      await screen.findByText("Could not check deployment impact"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Version 2 has no snapshot manifest"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Deploy failed")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deploy v2" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("counts each governed metric once in the breaking-metrics warning", async () => {
    getOntologyVersion.mockResolvedValue(
      detail([
        ORDER_METRIC,
        { ...ORDER_METRIC, missingClassIri: "https://e/o#Customer" },
      ]),
    );
    render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Deploy version 2" }),
    );
    expect(
      await screen.findByText("1 governed metric will lose its linked class"),
    ).toBeInTheDocument();
  });

  it("re-asks for acknowledgement when the deploy finds breaking metrics the check did not", async () => {
    getOntologyVersion.mockResolvedValue(detail());
    deployOntologyVersion.mockRejectedValueOnce(
      new ApiError(
        409,
        "UNKNOWN_ERROR",
        "Deploying v2 breaks governed metrics",
        {
          detail: {
            message: "Deploying v2 breaks governed metrics",
            breakingMetrics: [ORDER_METRIC],
          },
        },
      ),
    );
    render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(await openDeployDialog());

    expect(await screen.findByText("https://e/o#Order")).toBeInTheDocument();
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Deploy v2" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.queryByText("Deploy failed")).not.toBeInTheDocument();

    // The refused deploy also re-reads the list.
    await waitFor(() => expect(listOntologyVersions).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Deploy v2" }));
    await waitFor(() =>
      expect(deployOntologyVersion).toHaveBeenLastCalledWith(api, "ns", 2, [
        "revenue",
      ]),
    );
  });

  it("disables confirm when the reload after a refusal finds a running deployment", async () => {
    getOntologyVersion.mockResolvedValue(detail());
    deployOntologyVersion.mockRejectedValueOnce(
      new ApiError(409, "UNKNOWN_ERROR", "Deployment d-auto is in progress", {
        detail: "Deployment d-auto is in progress",
      }),
    );
    listOntologyVersions
      .mockResolvedValueOnce(PUBLISHED_LIST)
      .mockResolvedValue({
        ...PUBLISHED_LIST,
        latestDeployment: {
          deploymentId: "d-auto",
          version: 2,
          status: "DEPLOYING",
          partial: false,
          progress: {},
        },
      });
    render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(await openDeployDialog());

    expect(await screen.findByText("Deploying…")).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Deploy v2" });
    expect(confirm).toHaveAttribute("aria-disabled", "true");
    expect(confirm).toHaveAccessibleDescription("A deployment is running");
  });

  it("ignores a late impact-check failure from a dialog that was closed and reopened", async () => {
    let failFirstCheck: (e: unknown) => void = () => {};
    getOntologyVersion
      .mockReturnValueOnce(
        new Promise((_, reject) => (failFirstCheck = reject)),
      )
      .mockResolvedValueOnce(detail());
    render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Deploy version 2" }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    const confirm = await openDeployDialog();

    await act(async () => failFirstCheck(new Error("stale failure")));
    expect(
      screen.queryByText("Could not check deployment impact"),
    ).not.toBeInTheDocument();
    expect(confirm).not.toHaveAttribute("aria-disabled", "true");
  });

  it("ignores a late impact-check result from a dialog that was closed and reopened", async () => {
    let finishFirstCheck: (value: unknown) => void = () => {};
    getOntologyVersion
      .mockReturnValueOnce(
        new Promise((resolve) => (finishFirstCheck = resolve)),
      )
      .mockResolvedValueOnce(detail());
    render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Deploy version 2" }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    // Same version, new request: only the reopened dialog's check may apply.
    const confirm = await openDeployDialog();

    await act(async () => finishFirstCheck(detail([ORDER_METRIC])));
    expect(screen.queryByText("https://e/o#Order")).not.toBeInTheDocument();
    expect(confirm).not.toHaveAttribute("aria-disabled", "true");
  });

  it("closes the dialog on Escape or its close button when idle", async () => {
    getOntologyVersion.mockResolvedValue(detail());
    render(<VersionsTab apiClient={api} namespace="ns" />);

    const confirm = await openDeployDialog();
    fireEvent.keyDown(confirm, { key: "Escape", keyCode: 27 });
    expect(
      screen.queryByRole("button", { name: "Deploy v2" }),
    ).not.toBeInTheDocument();

    await openDeployDialog();
    fireEvent.click(screen.getByRole("button", { name: "Close dialog" }));
    expect(
      screen.queryByRole("button", { name: "Deploy v2" }),
    ).not.toBeInTheDocument();
  });

  it("ignores Escape and the close button while the deploy request is in flight", async () => {
    getOntologyVersion.mockResolvedValue(detail());
    let finishDeploy: (value: unknown) => void = () => {};
    deployOntologyVersion.mockReturnValueOnce(
      new Promise((resolve) => (finishDeploy = resolve)),
    );
    render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(await openDeployDialog());
    await waitFor(() => expect(deployOntologyVersion).toHaveBeenCalled());

    fireEvent.keyDown(screen.getByRole("button", { name: "Deploy v2" }), {
      key: "Escape",
      keyCode: 27,
    });
    fireEvent.click(screen.getByRole("button", { name: "Close dialog" }));
    expect(
      screen.getByRole("button", { name: "Deploy v2" }),
    ).toBeInTheDocument();

    await act(async () => finishDeploy({ deploymentId: "d-1" }));
    expect(
      screen.queryByRole("button", { name: "Deploy v2" }),
    ).not.toBeInTheDocument();
  });

  it("starts from scratch when the namespace changes", async () => {
    getOntologyVersion.mockResolvedValue(detail());
    const view = render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Details of version 2" }),
    );
    expect(await screen.findByText("v2 details")).toBeInTheDocument();
    await openDeployDialog();

    // The other namespace is still loading.
    listOntologyVersions.mockReturnValue(new Promise(() => {}));
    view.rerender(<VersionsTab apiClient={api} namespace="other" />);

    expect(listOntologyVersions).toHaveBeenLastCalledWith(api, "other");
    expect(screen.getByText("Loading versions")).toBeInTheDocument();
    expect(screen.queryByText("v2")).not.toBeInTheDocument();
    expect(screen.queryByText("v2 details")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Deploy v2" }),
    ).not.toBeInTheDocument();
  });

  it("shows the server's message when a deploy is refused", async () => {
    getOntologyVersion.mockResolvedValue(detail());
    deployOntologyVersion.mockRejectedValueOnce(
      new ApiError(409, "UNKNOWN_ERROR", "Another deployment is running", {
        detail: "Another deployment is running",
      }),
    );
    render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(await openDeployDialog());

    expect(
      await screen.findByText("Another deployment is running"),
    ).toBeInTheDocument();
    expect(screen.getByText("Deploy failed")).toBeInTheDocument();
    // The refusal is not final: the user can try again once the lock frees up.
    const confirm = screen.getByRole("button", { name: "Deploy v2" });
    expect(confirm).toBeEnabled();
    expect(confirm).not.toHaveAttribute("aria-disabled", "true");
    // And the list is re-read so the running deployment shows up.
    await waitFor(() => expect(listOntologyVersions).toHaveBeenCalledTimes(2));
  });

  it("marks the running step of an in-flight deployment", async () => {
    listOntologyVersions.mockResolvedValue({
      versions: [V2, V1],
      activeVersion: 1,
      latestDeployment: {
        deploymentId: "d",
        version: 2,
        status: "DEPLOYING",
        partial: false,
        progress: { stage: "done" },
      },
    });
    const { container } = render(
      <VersionsTab apiClient={api} namespace="ns" />,
    );
    await screen.findByText("Swap graph");
    const running = Array.from(
      container.querySelectorAll('[class*="status-in-progress"]'),
    ).map((el) => el.textContent);
    expect(running).toContain("Swap graph");
    expect(running).not.toContain("Stage graph");
    expect(running).not.toContain("Replace embeddings");
    expect(
      screen.getByRole("img", { name: "In progress" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: "Done" })).toHaveLength(1);
    expect(screen.getAllByRole("img", { name: "Not started" })).toHaveLength(4);
  });

  it("polls while a deployment runs and stops when unmounted", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const setTimeoutSpy = vi.spyOn(window, "setTimeout");
    const clearTimeoutSpy = vi.spyOn(window, "clearTimeout");
    try {
      listOntologyVersions.mockResolvedValue({
        versions: [V2, V1],
        activeVersion: 1,
        latestDeployment: {
          deploymentId: "d",
          version: 2,
          status: "DEPLOYING",
          partial: false,
          progress: {},
        },
      });
      const view = render(<VersionsTab apiClient={api} namespace="ns" />);
      await screen.findByText("v2");
      const callsBefore = listOntologyVersions.mock.calls.length;

      await advance(3100);
      await waitFor(() =>
        expect(listOntologyVersions.mock.calls.length).toBeGreaterThan(
          callsBefore,
        ),
      );

      // Unmounting must clear the pending poll timer, not just stop re-rendering.
      const pollTimers = setTimeoutSpy.mock.calls.flatMap((call, i) =>
        call[1] === 3000 ? [setTimeoutSpy.mock.results[i].value] : [],
      );
      expect(pollTimers.length).toBeGreaterThan(0);
      view.unmount();
      expect(clearTimeoutSpy).toHaveBeenCalledWith(pollTimers.at(-1));
      const callsAtUnmount = listOntologyVersions.mock.calls.length;
      await advance(10_000);
      expect(listOntologyVersions.mock.calls.length).toBe(callsAtUnmount);
    } finally {
      // Restore the spies before the real timers, or restoring would put the
      // fake functions back.
      setTimeoutSpy.mockRestore();
      clearTimeoutSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it("applies a poll answer that takes longer than the poll interval", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const deploying = {
        deploymentId: "d",
        version: 2,
        status: "DEPLOYING",
        partial: false,
        progress: {},
      };
      listOntologyVersions
        .mockResolvedValueOnce({
          versions: [V2, V1],
          activeVersion: 1,
          latestDeployment: deploying,
        })
        .mockImplementation(
          () =>
            new Promise((resolve) =>
              setTimeout(
                () =>
                  resolve({
                    versions: [V2, V1],
                    activeVersion: 2,
                    latestDeployment: { ...deploying, status: "DEPLOYED" },
                  }),
                5000,
              ),
            ),
        );
      render(<VersionsTab apiClient={api} namespace="ns" />);
      expect(await screen.findByText("Deploying…")).toBeInTheDocument();

      await advance(3100);
      expect(listOntologyVersions).toHaveBeenCalledTimes(2);
      // A background poll does not spin the Refresh button.
      expect(
        screen.getByRole("button", { name: "Refresh" }),
      ).not.toHaveAttribute("aria-disabled", "true");

      // No further poll starts (and supersedes it) while the slow one is in flight.
      await advance(3100);
      expect(listOntologyVersions).toHaveBeenCalledTimes(2);

      await advance(2000);
      expect(await screen.findByText("Deployed")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("polls while a version is still publishing", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      listOntologyVersions.mockResolvedValue({
        versions: [{ ...V2, status: "PUBLISHING" }, V1],
        activeVersion: 1,
        latestDeployment: null,
      });
      render(<VersionsTab apiClient={api} namespace="ns" />);
      await screen.findByText("v2");
      expect(listOntologyVersions).toHaveBeenCalledTimes(1);

      await advance(3100);
      await waitFor(() =>
        expect(listOntologyVersions).toHaveBeenCalledTimes(2),
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps polling after publishing until the auto-deploy shows up", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      listOntologyVersions
        .mockResolvedValueOnce(PUBLISHING_LIST)
        // Published, but the auto-deploy has not written its DEPLOYING row yet.
        .mockResolvedValueOnce(PUBLISHED_LIST)
        .mockResolvedValueOnce(PUBLISHED_LIST)
        .mockResolvedValue({
          ...PUBLISHED_LIST,
          latestDeployment: {
            deploymentId: "d-auto",
            version: 2,
            status: "DEPLOYING",
            partial: false,
            progress: { stage: "done" },
          },
        });
      render(<VersionsTab apiClient={api} namespace="ns" />);
      expect(await screen.findByText("Publishing…")).toBeInTheDocument();

      await advance(3100);
      await advance(3100);
      await advance(3100);
      expect(await screen.findByText("Deploying…")).toBeInTheDocument();
      expect(
        screen.getByRole("img", { name: "In progress" }),
      ).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops polling a few polls after publishing when no deployment appears", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      listOntologyVersions
        .mockResolvedValueOnce(PUBLISHING_LIST)
        .mockResolvedValue(PUBLISHED_LIST);
      render(<VersionsTab apiClient={api} namespace="ns" />);
      expect(await screen.findByText("Publishing…")).toBeInTheDocument();

      for (let i = 0; i < 10; i++) await advance(3100);
      // The publishing answer, then 4 trailing polls.
      expect(listOntologyVersions).toHaveBeenCalledTimes(5);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops polling a few polls after publishing when the list keeps failing", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      listOntologyVersions
        .mockResolvedValueOnce(PUBLISHING_LIST)
        .mockRejectedValue(new Error("Service Unavailable"));
      render(<VersionsTab apiClient={api} namespace="ns" />);
      expect(await screen.findByText("Publishing…")).toBeInTheDocument();

      for (let i = 0; i < 10; i++) await advance(3100);
      // The publishing answer, then 4 failed trailing polls.
      expect(listOntologyVersions).toHaveBeenCalledTimes(5);
      expect(screen.getByText("Could not load versions")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps polling a running deployment through one failed fetch", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const deploying = {
        deploymentId: "d",
        version: 2,
        status: "DEPLOYING",
        partial: false,
        progress: {},
      };
      const DEPLOYING_LIST = {
        versions: [V2, V1],
        activeVersion: 1,
        latestDeployment: deploying,
      };
      listOntologyVersions
        .mockResolvedValueOnce(DEPLOYING_LIST)
        .mockRejectedValueOnce(new Error("Service Unavailable"))
        .mockResolvedValueOnce(DEPLOYING_LIST)
        .mockResolvedValue({
          versions: [V2, V1],
          activeVersion: 2,
          latestDeployment: { ...deploying, status: "DEPLOYED" },
        });
      render(<VersionsTab apiClient={api} namespace="ns" />);
      expect(await screen.findByText("Deploying…")).toBeInTheDocument();

      // One failure is below MAX_FAILED_POLLS: the running deployment still drives the poll.
      await advance(3100);
      expect(listOntologyVersions).toHaveBeenCalledTimes(2);
      await advance(3100);
      expect(listOntologyVersions).toHaveBeenCalledTimes(3);
      await advance(3100);
      expect(listOntologyVersions).toHaveBeenCalledTimes(4);
      expect(await screen.findByText("Deployed")).toBeInTheDocument();

      // DEPLOYED ends the polling.
      for (let i = 0; i < 5; i++) await advance(3100);
      expect(listOntologyVersions).toHaveBeenCalledTimes(4);
      expect(screen.getByText("Deployed")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not poll when no deployment is running", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<VersionsTab apiClient={api} namespace="ns" />);
      await screen.findByText("v2");
      await advance(10_000);
      expect(listOntologyVersions).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("lists the snapshot downloads with their sizes in the details panel", async () => {
    getOntologyVersion.mockResolvedValue({
      ...detail(),
      manifest: {
        formatVersion: 1,
        files: { "ontology.ttl": { sha256: "abc", bytes: 2048 } },
      },
      downloadUrls: { "ontology.ttl": "https://s3.example/ontology.ttl?sig=1" },
    });
    render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Details of version 2" }),
    );

    const link = await screen.findByRole("link", { name: /ontology\.ttl/ });
    expect(link).toHaveAttribute(
      "href",
      "https://s3.example/ontology.ttl?sig=1",
    );
    expect(screen.getByText("(2.0 KB)")).toBeInTheDocument();
  });

  it("clears the previous details when loading another version fails", async () => {
    getOntologyVersion
      .mockResolvedValueOnce(detail())
      .mockRejectedValueOnce(new Error("Version 1 has a corrupt diff.json"));
    render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Details of version 2" }),
    );
    expect(await screen.findByText("v2 details")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Details of version 1" }),
    );
    expect(
      await screen.findByText("Version 1 has a corrupt diff.json"),
    ).toBeInTheDocument();
    expect(screen.queryByText("v2 details")).not.toBeInTheDocument();
  });

  it("ignores a details answer that arrives after a later click", async () => {
    let resolveSlow: (value: unknown) => void = () => {};
    getOntologyVersion
      .mockReturnValueOnce(new Promise((resolve) => (resolveSlow = resolve)))
      .mockResolvedValueOnce({ ...detail(), version: V1 });
    render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Details of version 2" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Details of version 1" }),
    );
    expect(await screen.findByText("v1 details")).toBeInTheDocument();

    resolveSlow(detail());
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByText("v1 details")).toBeInTheDocument();
    expect(screen.queryByText("v2 details")).not.toBeInTheDocument();
  });

  it("keeps the details panel closed when Close beats a pending details load", async () => {
    let resolveV1: (value: unknown) => void = () => {};
    getOntologyVersion
      .mockResolvedValueOnce(detail())
      .mockReturnValueOnce(new Promise((resolve) => (resolveV1 = resolve)));
    render(<VersionsTab apiClient={api} namespace="ns" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Details of version 2" }),
    );
    expect(await screen.findByText("v2 details")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Details of version 1" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await act(async () => resolveV1({ ...detail(), version: V1 }));

    expect(screen.queryByText("v1 details")).not.toBeInTheDocument();
    expect(screen.queryByText("v2 details")).not.toBeInTheDocument();
  });
});
