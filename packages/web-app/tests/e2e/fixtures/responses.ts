// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { Response } from "@playwright/test";

/**
 * A `page.waitForResponse` predicate for the API GET whose URL path matches
 * `path`, so a page object can read what the page itself fetched (CORS
 * preflights are OPTIONS, so they never match).
 */
export function isGet(path: RegExp): (response: Response) => boolean {
  return (response) =>
    response.request().method() === "GET" &&
    path.test(new URL(response.url()).pathname);
}

/** `value` when it is an array, else none: reads a list of an untyped body. */
export function listOf(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
