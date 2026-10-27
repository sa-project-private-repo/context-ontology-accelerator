// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Ontology version constants shared by the API client and the Versions tab.
 *
 * Kept out of `ontology-engine.ts` so tests that mock that module with a few
 * functions still get the real values here.
 */

/** Deploy steps in the order the ontology-engine runs them (deploy.DEPLOY_STEPS). */
export const DEPLOY_STEPS = [
  "stage",
  "swap",
  "embeddings",
  "registry",
  "latest",
  "notify",
] as const;

export type DeployStep = (typeof DEPLOY_STEPS)[number];
