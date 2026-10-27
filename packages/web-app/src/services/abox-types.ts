// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Domain ABox constants shared by the API client and the Domain data tab.
 *
 * Kept out of `ontology-engine.ts` so tests that mock that module with a few
 * functions still get the real values here.
 */

export const ABOX_STATUSES = ["EMPTY", "BUILDING", "READY", "FAILED"] as const;

export type DomainAboxStatus = (typeof ABOX_STATUSES)[number];

export const ABOX_MAPPING_KINDS = ["class", "relation", "attribute"] as const;

export type AboxMappingKind = (typeof ABOX_MAPPING_KINDS)[number];

export const ABOX_MAPPING_STATUSES = ["active", "stale", "ignored"] as const;

export type AboxMappingStatus = (typeof ABOX_MAPPING_STATUSES)[number];

export type AboxMappingDirection = "forward" | "inverse";
