// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Ontology designer teardown through the API, run from Node with the live
 * page's token, as the namespace teardown is: it works when the page itself is
 * what failed.
 */
import type { Page } from "@playwright/test";
import { objectField } from "@utils/object-field";
import { getApiEndpoint, readLiveToken } from "./control-plane-api";

/** A class label a spec changed, with the label and language it had. */
export interface RenamedTerm {
  namespaceId: string;
  proposalId: string;
  iri: string;
  label: string;
  lang: string | null;
  /** The draft revision the rename makes. */
  revision: number;
}

/**
 * Puts back each label a spec changed and did not restore (it failed before
 * its Undo). While the rename is still the draft's newest revision it is
 * undone, as the spec meant to; after any later change the label is set
 * again. Best effort: never throws.
 */
export async function restoreLabels(
  page: Page,
  renamed: RenamedTerm[],
): Promise<void> {
  const baseURL = process.env.E2E_BASE_URL;
  if (renamed.length === 0 || !baseURL) return;
  try {
    const token = await readLiveToken(page);
    const base = await getApiEndpoint(baseURL);
    if (!token || !base) return;
    const headers = {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    };
    // Newest first, so a term renamed twice ends with its first label.
    for (const term of [...renamed].reverse()) {
      const studio = `${base}/namespaces/${encodeURIComponent(term.namespaceId)}/proposals/${encodeURIComponent(term.proposalId)}/studio`;
      const shown: unknown = await (
        await fetch(`${studio}/term?iri=${encodeURIComponent(term.iri)}`, {
          headers,
        })
      ).json();
      if (objectField(shown, "label") === term.label) continue;
      const draft: unknown = await (await fetch(studio, { headers })).json();
      const revision = objectField(draft, "revision");
      if (typeof revision !== "number") continue;
      const undo = revision === term.revision;
      const op = {
        op: "set_label",
        iri: term.iri,
        label: term.label,
        ...(term.lang ? { lang: term.lang } : {}),
      };
      await fetch(`${studio}/${undo ? "undo" : "changes"}`, {
        method: "POST",
        headers,
        body: JSON.stringify(
          undo
            ? { expectedRevision: revision }
            : { ops: [op], expectedRevision: revision },
        ),
      });
    }
  } catch {
    // Best effort: teardown must never fail the test.
  }
}
