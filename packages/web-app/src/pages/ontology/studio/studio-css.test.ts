// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The designer's stylesheet in light and dark mode. Vitest runs with
 * `css: false` and happy-dom resolves no chain of `var()` references, so there
 * is no computed style to read: each Cloudscape custom property that
 * studio.css reads is followed through the installed Cloudscape's own
 * declarations instead, on `body` for light mode and under `.awsui-dark-mode`
 * (which applyMode puts on the body) for dark mode. A Cloudscape upgrade that
 * renamed one would leave its rule on the light fallback in both modes.
 */
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it, expect } from "vitest";

const STUDIO_CSS = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "studio.css"),
  "utf8",
);
const CLOUDSCAPE_CSS = readFileSync(
  join(
    dirname(
      createRequire(import.meta.url).resolve(
        "@cloudscape-design/components/package.json",
      ),
    ),
    "internal/base-component/styles.scoped.css",
  ),
  "utf8",
);

/** The custom properties the Cloudscape rule for `selector` declares, with their values. */
function declarations(selector: string): Map<string, string> {
  const declared = new Map<string, string>();
  for (const [, selectors, body] of CLOUDSCAPE_CSS.matchAll(
    /([^{}]+)\{([^{}]*)\}/g,
  )) {
    if (selectors.trim() !== selector) continue;
    for (const [, name, value] of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g))
      declared.set(name, value.trim());
  }
  return declared;
}

const LIGHT = declarations("body");
const DARK = new Map([...LIGHT, ...declarations(".awsui-dark-mode:not(#\\9)")]);

/** What `name` comes to through its `var()` references, or undefined when one is not declared. */
function resolve(
  name: string,
  declared: Map<string, string>,
): string | undefined {
  const value = declared.get(name);
  const reference = value?.match(/^var\((--[\w-]+)\)$/);
  return reference ? resolve(reference[1], declared) : value;
}

const TOKENS = [
  ...new Set(
    [...STUDIO_CSS.matchAll(/var\(\s*(--[\w-]+)/g)].map(([, name]) => name),
  ),
];

describe("the designer stylesheet", () => {
  it("writes no colour of its own: each one is a Cloudscape token", () => {
    expect(TOKENS.length).toBeGreaterThan(0);
    // What is left without the comments and the var() expressions, whose
    // fallbacks are Cloudscape's light-mode values.
    const own = STUDIO_CSS.replace(/\/\*[\s\S]*?\*\//g, "").replace(
      /var\((?:[^()]|\([^()]*\))*\)/g,
      "",
    );
    expect(own).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i);
  });

  it.each(TOKENS)(
    "reads %s, which Cloudscape sets apart in light and dark mode",
    (name) => {
      const light = resolve(name, LIGHT);
      const dark = resolve(name, DARK);
      expect(light).toBeDefined();
      expect(dark).toBeDefined();
      expect(dark).not.toBe(light);
    },
  );
});
