// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * `value[key]` when `value` is an object that has `key`, else undefined. Reads
 * a field of an untyped value (a parsed error body) without a type assertion.
 */
export function objectField(value: unknown, key: string): unknown {
  return value && typeof value === "object" && key in value
    ? Reflect.get(value, key)
    : undefined;
}
