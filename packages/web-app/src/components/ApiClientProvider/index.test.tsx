// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientProvider, ApiError, useApiClient } from "./index";
import { RuntimeConfigContext } from "../RuntimeContext";
import { OIDCProvider } from "@auth";

vi.mock("@auth", () => ({
  OIDCProvider: { build: vi.fn() },
  UserProvider: ({ children }: { children: React.ReactNode }) => children,
}));

const runtimeContext = {
  apiEndpoint: "https://api.example.com/",
  oidcConfig: { authority: "https://auth", clientId: "client" },
} as unknown as React.ContextType<typeof RuntimeConfigContext>;

let captured: ReturnType<typeof useApiClient> | undefined;

function Probe() {
  captured = useApiClient();
  return null;
}

/** Render the provider and hand back the client its children would receive. */
function renderClient(): NonNullable<typeof captured> {
  render(
    <RuntimeConfigContext.Provider value={runtimeContext}>
      <ApiClientProvider>
        <Probe />
      </ApiClientProvider>
    </RuntimeConfigContext.Provider>,
  );
  if (!captured) throw new Error("provider did not supply a client");
  return captured;
}

/** Stub one failing fetch response with the given body. */
function mockFailure(status: number, body: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: false,
      status,
      headers: new Headers(),
      text: () => Promise.resolve(body),
    }),
  );
}

describe("ApiClientProvider error envelopes", () => {
  beforeEach(() => {
    captured = undefined;
    vi.clearAllMocks();
    vi.mocked(OIDCProvider.build).mockReturnValue({
      getIdToken: vi.fn().mockResolvedValue("id-token"),
    } as unknown as OIDCProvider);
  });

  it("uses a FastAPI `detail` as the error message", async () => {
    // Most ontology-engine 4xx responses are a bare `HTTPException`, whose
    // envelope is `detail` rather than the Smithy `message`. Reading only
    // `message` made every one of those fall through to the raw fallback, so a
    // page that rendered `error.message` showed the JSON envelope to the user
    // and the sentence the backend wrote was buried inside it.
    mockFailure(
      409,
      JSON.stringify({
        detail:
          "Grounding override could not be applied: no class matching '請求明細' in ontology Turtle",
      }),
    );
    const client = renderClient();

    await expect(client.post("/proposals/update", {})).rejects.toThrow(
      ApiError,
    );
    await expect(client.post("/proposals/update", {})).rejects.toThrow(
      "Grounding override could not be applied: no class matching '請求明細' in ontology Turtle",
    );
  });

  it("preserves the status and code alongside the detail", async () => {
    mockFailure(503, JSON.stringify({ code: "BUSY", detail: "try later" }));
    const client = renderClient();

    const error = await client.get("/whatever").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    if (!(error instanceof ApiError)) throw new Error("unreachable");
    expect(error.status).toBe(503);
    expect(error.code).toBe("BUSY");
    expect(error.message).toBe("try later");
  });

  it("prefers `message` when a response carries both", async () => {
    // The update route deliberately re-envelopes its 503 as `message` to match
    // the modeled ServiceUnavailableError, so `message` has to keep winning.
    mockFailure(
      503,
      JSON.stringify({ message: "modeled sentence", detail: "raw detail" }),
    );
    const client = renderClient();

    await expect(client.post("/x", {})).rejects.toThrow("modeled sentence");
  });

  it("falls back to the raw body when `detail` is not a string", async () => {
    // Some routes send a structured detail object. Interpolating it would
    // render "[object Object]" and lose the status the fallback carries.
    mockFailure(422, JSON.stringify({ detail: [{ loc: ["body"] }] }));
    const client = renderClient();

    await expect(client.post("/x", {})).rejects.toThrow(
      'API POST /x failed (422): {"detail":[{"loc":["body"]}]}',
    );
  });

  it("still folds a structured `errors` array into the message", async () => {
    mockFailure(
      400,
      JSON.stringify({
        message: "OSI import failed",
        errors: ["line 3: bad IRI", { ignored: true }, "line 9: bad IRI"],
      }),
    );
    const client = renderClient();

    await expect(client.post("/x", {})).rejects.toThrow(
      "OSI import failed: line 3: bad IRI; line 9: bad IRI",
    );
  });

  it("keeps the authorization wording for a 403", async () => {
    mockFailure(403, "");
    const client = renderClient();

    const error = await client.get("/x").catch((e: unknown) => e);
    if (!(error instanceof ApiError)) throw new Error("expected an ApiError");
    expect(error.status).toBe(403);
    expect(error.code).toBe("FORBIDDEN");
    expect(error.message).toContain("not authorized");
  });
});

function respondWith(status: number, body: string) {
  vi.mocked(fetch).mockResolvedValue(
    new Response(body, {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

async function rejection(promise: Promise<unknown>): Promise<ApiError> {
  const err = await promise.then(
    () => {
      throw new Error("expected the request to reject");
    },
    (e: unknown) => e,
  );
  if (!(err instanceof ApiError)) throw new Error("expected an ApiError");
  return err;
}

describe("ApiClientProvider request errors", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(OIDCProvider.build).mockReturnValue({
      getIdToken: vi.fn().mockResolvedValue("id-token"),
    } as unknown as OIDCProvider);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps a top-level code and message", async () => {
    respondWith(400, JSON.stringify({ code: "BAD", message: "Nope" }));
    const err = await rejection(renderClient().get("/x"));
    expect(err.status).toBe(400);
    expect(err.code).toBe("BAD");
    expect(err.message).toBe("Nope");
  });

  it("uses a string `detail` as the message", async () => {
    respondWith(
      409,
      JSON.stringify({ detail: "Another deployment is running" }),
    );
    const err = await rejection(renderClient().post("/deploy", {}));
    expect(err.status).toBe(409);
    expect(err.code).toBe("UNKNOWN_ERROR");
    expect(err.message).toBe("Another deployment is running");
    expect(err.body).toEqual({ detail: "Another deployment is running" });
  });

  it("uses `detail.message` and exposes the structured body", async () => {
    const breakingMetrics = [
      {
        metricId: "revenue",
        name: "revenue",
        missingClassIri: "https://e/o#Order",
      },
    ];
    respondWith(
      409,
      JSON.stringify({
        detail: { message: "Deploying breaks 1 metric", breakingMetrics },
      }),
    );
    const err = await rejection(renderClient().post("/deploy", {}));
    expect(err.message).toBe("Deploying breaks 1 metric");
    expect(err.body).toEqual({
      detail: { message: "Deploying breaks 1 metric", breakingMetrics },
    });
  });

  it("carries the body on a 403 too", async () => {
    respondWith(403, JSON.stringify({ detail: "Forbidden by policy" }));
    const err = await rejection(renderClient().get("/x"));
    expect(err.status).toBe(403);
    expect(err.code).toBe("FORBIDDEN");
    expect(err.message).toBe("Forbidden by policy");
    expect(err.body).toEqual({ detail: "Forbidden by policy" });
  });

  it("leaves the body undefined for a non-JSON error", async () => {
    respondWith(502, "Bad gateway");
    const err = await rejection(renderClient().get("/x"));
    expect(err.message).toBe("API GET /x failed (502): Bad gateway");
    expect(err.body).toBeUndefined();
  });

  it("uses `detail.code` when there is no top-level code", async () => {
    respondWith(
      409,
      JSON.stringify({ detail: { code: "LOCKED", message: "Busy" } }),
    );
    const err = await rejection(renderClient().post("/deploy", {}));
    expect(err.code).toBe("LOCKED");
    expect(err.message).toBe("Busy");
  });

  it("prefers a top-level code over `detail.code`", async () => {
    respondWith(
      409,
      JSON.stringify({ code: "TOP", detail: { code: "NESTED", message: "m" } }),
    );
    const err = await rejection(renderClient().post("/deploy", {}));
    expect(err.code).toBe("TOP");
  });
});

/** The client's optional `getText`, which this provider always supplies. */
function getText(path: string): Promise<string> {
  const { getText: fetchText } = renderClient();
  if (!fetchText) throw new Error("provider did not supply getText");
  return fetchText(path);
}

describe("ApiClientProvider getText errors", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    vi.mocked(OIDCProvider.build).mockReturnValue({
      getIdToken: vi.fn().mockResolvedValue("id-token"),
    } as unknown as OIDCProvider);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps a top-level code and message", async () => {
    respondWith(400, JSON.stringify({ code: "BAD", message: "Nope" }));
    const err = await rejection(getText("/download"));
    expect(err.code).toBe("BAD");
    expect(err.message).toBe("Nope");
  });

  it("uses a string `detail` as the message", async () => {
    respondWith(404, JSON.stringify({ detail: "Version 9 not found" }));
    const err = await rejection(getText("/download"));
    expect(err.status).toBe(404);
    expect(err.message).toBe("Version 9 not found");
    expect(err.body).toEqual({ detail: "Version 9 not found" });
  });

  it("uses `detail.code` and `detail.message`", async () => {
    respondWith(
      409,
      JSON.stringify({
        detail: { code: "INCOMPLETE", message: "No manifest" },
      }),
    );
    const err = await rejection(getText("/download"));
    expect(err.code).toBe("INCOMPLETE");
    expect(err.message).toBe("No manifest");
  });

  it("falls back to the raw text for a non-JSON error", async () => {
    respondWith(502, "Bad gateway");
    const err = await rejection(getText("/download"));
    expect(err.message).toBe("API GET /download failed (502): Bad gateway");
    expect(err.body).toBeUndefined();
  });
});
