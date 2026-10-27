// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  getDomainAbox,
  getDomainInstance,
  listAboxMappings,
  listDomainInstances,
  rebuildDomainAbox,
  resetAboxMapping,
  updateAboxMapping,
} from "./ontology-engine";
import type { ApiClient } from "@components/ApiClientProvider";

describe("domain ABox service functions", () => {
  let api: ApiClient;

  beforeEach(() => {
    api = {
      get: vi.fn().mockResolvedValue({}),
      post: vi.fn().mockResolvedValue({}),
      put: vi.fn().mockResolvedValue({}),
      del: vi.fn().mockResolvedValue({}),
    };
  });

  it("gets the ABox state under the encoded namespace", async () => {
    await getDomainAbox(api, "ns 1");
    expect(api.get).toHaveBeenCalledWith("/namespaces/ns%201/abox");
  });

  it("lists instances with every filter encoded", async () => {
    await listDomainInstances(api, "ns", {
      classIri: "http://e.com/o#C",
      q: "ac",
      maxResults: 20,
      nextToken: "t==",
    });
    expect(api.get).toHaveBeenCalledWith(
      "/namespaces/ns/abox/instances?classIri=http%3A%2F%2Fe.com%2Fo%23C&q=ac&maxResults=20&nextToken=t%3D%3D",
    );
  });

  it("lists instances without a query string when no filter is set", async () => {
    await listDomainInstances(api, "ns");
    expect(api.get).toHaveBeenCalledWith("/namespaces/ns/abox/instances");
  });

  it("gets one instance by its IRI in the query string", async () => {
    await getDomainInstance(api, "ns", "http://x/#a");
    expect(api.get).toHaveBeenCalledWith(
      "/namespaces/ns/abox/instance?iri=http%3A%2F%2Fx%2F%23a",
    );
  });

  it("lists mappings of one kind", async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ mappings: [] });
    await listAboxMappings(api, "ns", { kind: "class" });
    expect(api.get).toHaveBeenCalledWith(
      "/namespaces/ns/abox/mappings?kind=class&maxResults=200",
    );
  });

  it("lists mappings of one kind and status", async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ mappings: [] });
    await listAboxMappings(api, "ns", { kind: "relation", status: "stale" });
    expect(api.get).toHaveBeenCalledWith(
      "/namespaces/ns/abox/mappings?kind=relation&status=stale&maxResults=200",
    );
  });

  it("follows nextToken and concatenates the mapping pages", async () => {
    vi.mocked(api.get)
      .mockResolvedValueOnce({ mappings: [{ id: "class.a" }], nextToken: "p2" })
      .mockResolvedValueOnce({ mappings: [{ id: "class.b" }] });
    const result = await listAboxMappings(api, "ns", { kind: "class" });
    expect(result.mappings.map((m) => m.id)).toEqual(["class.a", "class.b"]);
    expect(api.get).toHaveBeenNthCalledWith(
      2,
      "/namespaces/ns/abox/mappings?kind=class&maxResults=200&nextToken=p2",
    );
  });

  it("puts a manual mapping target", async () => {
    await updateAboxMapping(api, "ns", "class.ab", { target: "t" });
    expect(api.put).toHaveBeenCalledWith(
      "/namespaces/ns/abox/mappings/class.ab",
      { target: "t" },
    );
  });

  it("encodes the mapping id", async () => {
    await updateAboxMapping(api, "ns", "a/b c", {
      target: "t",
      direction: "inverse",
    });
    expect(api.put).toHaveBeenCalledWith(
      "/namespaces/ns/abox/mappings/a%2Fb%20c",
      { target: "t", direction: "inverse" },
    );
  });

  it("resets a mapping with DELETE", async () => {
    await resetAboxMapping(api, "ns", "class.ab");
    expect(api.del).toHaveBeenCalledWith(
      "/namespaces/ns/abox/mappings/class.ab",
    );
  });

  it("requests a rebuild with an empty body", async () => {
    await rebuildDomainAbox(api, "ns");
    expect(api.post).toHaveBeenCalledWith("/namespaces/ns/abox/rebuild", {});
  });
});
