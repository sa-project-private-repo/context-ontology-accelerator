// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  acceptProposal,
  deployOntologyVersion,
  getOntologyDeployment,
  getOntologyVersion,
  listOntologyVersions,
  type Proposal,
} from "./ontology-engine";
import type { ApiClient } from "@components/ApiClientProvider";

describe("ontology version service functions", () => {
  let api: ApiClient;

  beforeEach(() => {
    api = {
      get: vi.fn().mockResolvedValue({}),
      post: vi.fn().mockResolvedValue({}),
      put: vi.fn(),
      del: vi.fn(),
    };
  });

  it("lists versions under the namespace ontology path", async () => {
    await listOntologyVersions(api, "ns 1");
    expect(api.get).toHaveBeenCalledWith(
      "/namespaces/ns%201/ontology/versions",
    );
  });

  it("collects every page and keeps the namespace fields of the last one", async () => {
    vi.mocked(api.get)
      .mockResolvedValueOnce({
        versions: [{ version: 3 }, { version: 2 }],
        nextToken: "t-1",
        activeVersion: 1,
      })
      .mockResolvedValueOnce({
        versions: [{ version: 1 }],
        activeVersion: 2,
        latestDeployment: { deploymentId: "d-2", version: 2 },
        servingDeployment: { deploymentId: "d-1", version: 1, partial: true },
        resetAfterVersion: null,
      });
    const list = await listOntologyVersions(api, "ns");
    expect(api.get).toHaveBeenNthCalledWith(
      1,
      "/namespaces/ns/ontology/versions",
    );
    expect(api.get).toHaveBeenNthCalledWith(
      2,
      "/namespaces/ns/ontology/versions?nextToken=t-1",
    );
    expect(list.versions.map((v) => v.version)).toEqual([3, 2, 1]);
    expect(list.activeVersion).toBe(2);
    expect(list.latestDeployment?.deploymentId).toBe("d-2");
    // The Redeploy banner and label follow the serving deployment.
    expect(list.servingDeployment?.deploymentId).toBe("d-1");
    expect(list.resetAfterVersion).toBeNull();
    expect(list.nextToken).toBeUndefined();
  });

  it("gets one version", async () => {
    await getOntologyVersion(api, "ns", 3);
    expect(api.get).toHaveBeenCalledWith("/namespaces/ns/ontology/versions/3");
  });

  it("deploys with the acknowledged metric ids", async () => {
    await deployOntologyVersion(api, "ns", 3, ["revenue"]);
    expect(api.post).toHaveBeenCalledWith(
      "/namespaces/ns/ontology/versions/3/deploy",
      {
        acknowledgedBreakingMetrics: ["revenue"],
      },
    );
  });

  it("polls a deployment by id", async () => {
    await getOntologyDeployment(api, "ns", "20261014T000000Z-abc");
    expect(api.get).toHaveBeenCalledWith(
      "/namespaces/ns/ontology/deployments/20261014T000000Z-abc",
    );
  });

  it("encodes the deployment id", async () => {
    await getOntologyDeployment(api, "ns", "d/1 x");
    expect(api.get).toHaveBeenCalledWith(
      "/namespaces/ns/ontology/deployments/d%2F1%20x",
    );
  });

  it("sends the deploy flag on accept", async () => {
    await acceptProposal(api, "ns", "p-1", { deploy: false });
    expect(api.post).toHaveBeenCalledWith(
      "/namespaces/ns/proposals/p-1/accept",
      { deploy: false },
    );
  });

  it("sends both the target ontology and the deploy flag on accept", async () => {
    await acceptProposal(api, "ns", "p-1", {
      ontology_id: "onto-1",
      deploy: true,
    });
    expect(api.post).toHaveBeenCalledWith(
      "/namespaces/ns/proposals/p-1/accept",
      { ontology_id: "onto-1", deploy: true },
    );
  });

  it("encodes the proposal id on accept", async () => {
    await acceptProposal(api, "ns", "p/1 x");
    expect(api.post).toHaveBeenCalledWith(
      "/namespaces/ns/proposals/p%2F1%20x/accept",
      {},
    );
  });

  it("still sends an empty body when accept has no options", async () => {
    await acceptProposal(api, "ns", "p-1");
    expect(api.post).toHaveBeenCalledWith(
      "/namespaces/ns/proposals/p-1/accept",
      {},
    );
  });

  it("sends the reviewed revision of a designer draft on accept", async () => {
    await acceptProposal(api, "ns", "p-1", {
      deploy: true,
      expectedRevision: 3,
    });
    expect(api.post).toHaveBeenCalledWith(
      "/namespaces/ns/proposals/p-1/accept",
      { deploy: true, expectedRevision: 3 },
    );
  });

  it("sends revision 0 (a draft nobody changed yet), which is not 'absent'", async () => {
    await acceptProposal(api, "ns", "p-1", { expectedRevision: 0 });
    expect(api.post).toHaveBeenCalledWith(
      "/namespaces/ns/proposals/p-1/accept",
      { expectedRevision: 0 },
    );
  });

  it("takes the revision from the proposal's studio field, and omits it for a proposal never opened", async () => {
    const base: Proposal = {
      proposal_id: "p-1",
      job_id: "j-1",
      namespace: "ns",
      ontology_id: "o",
      proposal_type: "induced",
      status: "updated",
      created_at: "2026-10-19T00:00:00Z",
      updated_at: "2026-10-19T00:00:00Z",
    };
    const inStudio: Proposal = {
      ...base,
      studio: {
        base_version: 2,
        revision: 5,
        opened_at: "2026-10-19T00:00:00Z",
        opened_by: "alice",
      },
    };
    await acceptProposal(api, "ns", "p-1", {
      expectedRevision: inStudio.studio?.revision,
    });
    expect(api.post).toHaveBeenLastCalledWith(
      "/namespaces/ns/proposals/p-1/accept",
      { expectedRevision: 5 },
    );
    await acceptProposal(api, "ns", "p-1", {
      expectedRevision: base.studio?.revision,
    });
    expect(api.post).toHaveBeenLastCalledWith(
      "/namespaces/ns/proposals/p-1/accept",
      {},
    );
  });
});
