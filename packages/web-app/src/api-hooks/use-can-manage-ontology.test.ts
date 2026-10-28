// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// This file tests the REAL hook; opt out of the global mock in vitest-setup.ts.
vi.unmock("@api-hooks/use-can-manage-ontology");

import {
  canManageOntology,
  groupsAllListed,
  useCanManageOntology,
} from "./use-can-manage-ontology";

const mockGrants = vi.fn();
vi.mock("./use-list-principal-grants", () => ({
  useListPrincipalGrants: () => mockGrants(),
}));
const mockUser = vi.fn();
vi.mock("@auth", () => ({
  useUser: () => mockUser(),
}));

const grant = (role: string, namespaceId = "ns-a") => ({ role, namespaceId });

beforeEach(() => {
  mockGrants.mockReset();
  mockUser.mockReset();
  mockUser.mockReturnValue({ user: { groups: ["all-staff"] } });
});

describe("canManageOntology", () => {
  it("is false for a data analyst, whose role only reads", () => {
    expect(canManageOntology([grant("data-analyst")], "ns-a")).toBe(false);
  });

  it("is false for a platform viewer", () => {
    expect(
      canManageOntology([grant("platform-viewer", "GLOBAL")], "ns-a"),
    ).toBe(false);
  });

  it("is true for a data steward, an owner, a maintainer and a platform admin", () => {
    for (const g of [
      grant("data-steward"),
      grant("namespace-owner"),
      grant("namespace-maintainer"),
      grant("platform-admin", "GLOBAL"),
    ]) {
      expect(canManageOntology([g], "ns-a")).toBe(true);
    }
  });

  it("is true when another grant, such as a group's, adds a role that can", () => {
    expect(
      canManageOntology([grant("data-analyst"), grant("data-steward")], "ns-a"),
    ).toBe(true);
    expect(
      canManageOntology(
        [grant("platform-viewer", "GLOBAL"), grant("namespace-owner")],
        "ns-a",
      ),
    ).toBe(true);
  });

  it("ignores grants on other namespaces", () => {
    expect(
      canManageOntology(
        [grant("data-analyst"), grant("data-steward", "ns-b")],
        "ns-a",
      ),
    ).toBe(false);
  });

  it("leaves the decision to the API for a role it does not know, or no grant", () => {
    expect(canManageOntology([grant("auditor")], "ns-a")).toBe(true);
    expect(canManageOntology([], "ns-a")).toBe(true);
    expect(canManageOntology([grant("data-steward", "ns-b")], "ns-a")).toBe(
      true,
    );
  });
});

describe("canManageOntology and the seed Cedar policies", () => {
  // `fileURLToPath` is given the URL string: happy-dom replaces the global URL class.
  const SEED_DIR = join(
    dirname(fileURLToPath(import.meta.url)),
    "../../../../libs/common/src/coa_authorization/seed",
  );

  /**
   * Each built-in role the seed policies name, and whether one of its permits
   * allows manageOntology: by naming it, or by leaving the action open.
   */
  function seedRoles(): Map<string, boolean> {
    const roles = new Map<string, boolean>();
    for (const file of readdirSync(SEED_DIR).filter((f) =>
      f.endsWith(".cedar"),
    )) {
      const policies = readFileSync(join(SEED_DIR, file), "utf8").replace(
        /\/\/.*$/gm,
        "",
      );
      for (const policy of policies.split(";")) {
        const action = /permit\s*\(\s*principal\s*,([\s\S]*?),\s*resource/.exec(
          policy,
        )?.[1];
        const role =
          /globalRoles\.contains\("([\w-]+)"\)|"role":\s*"([\w-]+)"/.exec(
            policy,
          );
        if (action === undefined || !role) continue;
        const name = role[1] ?? role[2];
        const manages =
          action.trim() === "action" || action.includes('"manageOntology"');
        roles.set(name, (roles.get(name) ?? false) || manages);
      }
    }
    return roles;
  }

  it("treats as read-only exactly the built-in roles no seed policy lets manage the ontology", () => {
    const roles = seedRoles();
    // The parse found the roles, both kinds.
    expect(roles.get("data-analyst")).toBe(false);
    expect(roles.get("data-steward")).toBe(true);
    expect(roles.get("platform-admin")).toBe(true);
    for (const [role, manages] of roles) {
      const scope = role.startsWith("platform-") ? "GLOBAL" : "ns-a";
      expect(canManageOntology([grant(role, scope)], "ns-a"), role).toBe(
        manages,
      );
    }
  });
});

describe("groupsAllListed", () => {
  it("is true for group names ListPrincipalGrants reads", () => {
    expect(
      groupsAllListed(["all-staff", "data.stewards", "ops@example.com", "a_b"]),
    ).toBe(true);
    expect(groupsAllListed([])).toBe(true);
  });

  it("is false when a group name is one ListPrincipalGrants skips", () => {
    for (const name of ["Data Stewards", "team/eng", "a+b", "dept:sales"]) {
      expect(groupsAllListed(["all-staff", name]), name).toBe(false);
    }
  });

  it("is false past the 250 groups ListPrincipalGrants reads", () => {
    const groups = Array.from({ length: 250 }, (_, i) => `group-${i}`);
    expect(groupsAllListed(groups)).toBe(true);
    expect(groupsAllListed([...groups, "group-250"])).toBe(false);
  });
});

describe("useCanManageOntology", () => {
  it("reads the caller's own grants", () => {
    mockGrants.mockReturnValue({ data: { grants: [grant("data-analyst")] } });
    const { result } = renderHook(() => useCanManageOntology("ns-a"));
    expect(result.current).toBe(false);
  });

  it("offers the actions when the caller has a group whose grants the listing leaves out", () => {
    // The authorizer honours "Data Stewards"; ListPrincipalGrants skips it, so
    // a data-steward grant through it would be missing from the listing.
    mockUser.mockReturnValue({
      user: { groups: ["all-staff", "Data Stewards"] },
    });
    mockGrants.mockReturnValue({ data: { grants: [grant("data-analyst")] } });
    expect(renderHook(() => useCanManageOntology("ns-a")).result.current).toBe(
      true,
    );
  });

  it("offers the actions while the grants load, when they cannot be read, and without a namespace", () => {
    mockGrants.mockReturnValue({ data: undefined, isLoading: true });
    expect(renderHook(() => useCanManageOntology("ns-a")).result.current).toBe(
      true,
    );
    mockGrants.mockReturnValue({ data: undefined, error: new Error("403") });
    expect(renderHook(() => useCanManageOntology("ns-a")).result.current).toBe(
      true,
    );
    mockGrants.mockReturnValue({ data: { grants: [grant("data-analyst")] } });
    expect(
      renderHook(() => useCanManageOntology(undefined)).result.current,
    ).toBe(true);
  });
});
