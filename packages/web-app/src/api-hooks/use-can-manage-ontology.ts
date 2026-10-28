// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { GrantSummary } from "@coa/control-plane-client";
import { useUser } from "@auth";
import { useListPrincipalGrants } from "./use-list-principal-grants";

/**
 * The built-in roles that read and query a namespace but hold no
 * `manageOntology` (see the seed Cedar policies in
 * libs/common/src/coa_authorization/seed): they cannot edit, accept, deploy
 * or rebuild anything of the namespace's ontology. A test checks this set
 * against the seed policies.
 */
const READ_ONLY_ROLES: ReadonlySet<string> = new Set([
  "data-analyst",
  "platform-viewer",
]);

/** What ListPrincipalGrants gives as the namespace of a platform role's grant. */
const PLATFORM_GRANT = "GLOBAL";

/**
 * The group names whose grants ListPrincipalGrants lists: word characters,
 * dots, @ and hyphens (the handler skips any other name), at most
 * MAX_LISTED_GROUPS of them. JavaScript's `\w` is ASCII only, stricter than
 * the handler's, so a name it rejects at worst makes the hook offer the
 * actions.
 */
const LISTED_GROUP = /^[\w.@-]+$/;
const MAX_LISTED_GROUPS = 250;

/**
 * Whether `grants`, the caller's own, let them change `namespaceId`'s
 * ontology. False only when every grant that applies there (the namespace's,
 * or a platform-wide one) is a read-only role. Any other role, or no grant at
 * all, counts as able: the API decides.
 */
export function canManageOntology(
  grants: readonly Pick<GrantSummary, "namespaceId" | "role">[],
  namespaceId: string,
): boolean {
  const applicable = grants.filter(
    (g) => g.namespaceId === namespaceId || g.namespaceId === PLATFORM_GRANT,
  );
  return (
    applicable.length === 0 ||
    applicable.some((g) => !READ_ONLY_ROLES.has(g.role ?? ""))
  );
}

/**
 * Whether ListPrincipalGrants lists the grants of every one of `groups`, the
 * caller's: false when one has a name it skips, or there are more than it reads.
 */
export function groupsAllListed(groups: readonly string[]): boolean {
  const names = groups.map((g) => g.trim()).filter((g) => g !== "");
  return (
    names.length <= MAX_LISTED_GROUPS &&
    names.every((g) => LISTED_GROUP.test(g))
  );
}

/**
 * Whether to offer the current user the actions that change a namespace's
 * ontology: the Cedar action `manageOntology` (every edit in the Ontology
 * designer, accepting, deploying, rebuilding the domain data and editing its
 * mappings).
 *
 * Reads the caller's grants with ListPrincipalGrants (`me`), which lists the
 * user's grants and those of their groups. The authorizer honours every group
 * of the caller's token, but the listing skips a group whose name is not one
 * of word characters, dots, @ and hyphens, and any past its 250th; when the
 * caller has such a group (as the console reads the token), a grant through
 * it may be missing, so the actions stay offered.
 *
 * Unlike useNamespace, which asks the server rather than re-deriving access
 * from grants, this restates which built-in roles hold `manageOntology`: no
 * API answers that for the caller. A test ties READ_ONLY_ROLES to the seed
 * policies.
 *
 * This is a UX signal, not a control: it only stops offering what the API
 * would refuse. It fails open, so while the grants load, when they cannot be
 * read, or for a role it does not know, the actions stay offered and the API,
 * the authority, answers.
 */
export function useCanManageOntology(namespaceId: string | undefined): boolean {
  const { data } = useListPrincipalGrants();
  const { user } = useUser();
  if (!namespaceId || !data) return true;
  if (!groupsAllListed(user?.groups ?? [])) return true;
  return canManageOntology(data.grants ?? [], namespaceId);
}
