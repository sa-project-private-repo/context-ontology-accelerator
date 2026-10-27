// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, {
  createContext,
  PropsWithChildren,
  useContext,
  useMemo,
} from "react";
import { RuntimeConfigContext } from "../RuntimeContext";
import { OIDCProvider } from "@auth";

export interface ApiClient {
  get: <T>(path: string) => Promise<T>;
  post: <T>(path: string, body: unknown) => Promise<T>;
  put: <T>(path: string, body: unknown) => Promise<T>;
  del: <T>(path: string) => Promise<T>;
  /** Fetch a non-JSON `text/*` response body as a raw string (e.g. an
   *  ontology exported as `text/turtle` from `/download`). Optional so the
   *  many JSON-only mock clients in tests stay valid. */
  getText?: (path: string) => Promise<string>;
}

/** Structured API error with status code and optional server-provided details. */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    /** Parsed JSON error body, when the response had one. Lets callers read
     *  structured fields such as a 409's `detail.breakingMetrics`. */
    public readonly body?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** `value[key]` when `value` is an object holding a string there. */
function stringField(value: unknown, key: string): string | undefined {
  if (!value || typeof value !== "object" || !(key in value)) return undefined;
  const field: unknown = Reflect.get(value, key);
  return typeof field === "string" ? field : undefined;
}

/**
 * `code` / `message` from a parsed JSON error body. Top-level fields win; else
 * they come from FastAPI's HTTPException `detail` (e.g. ontology-engine), which
 * is `"<message>"` or `{"message": ..., "code"?: ...}`.
 */
function errorFields(parsed: unknown): { code?: string; message?: string } {
  const detail: unknown =
    parsed && typeof parsed === "object" && "detail" in parsed
      ? Reflect.get(parsed, "detail")
      : undefined;
  return {
    code: stringField(parsed, "code") ?? stringField(detail, "code"),
    message:
      stringField(parsed, "message") ??
      (typeof detail === "string" ? detail : stringField(detail, "message")),
  };
}

const ApiClientContext = createContext<ApiClient | undefined>(undefined);

export const useApiClient = (): ApiClient => {
  const ctx = useContext(ApiClientContext);
  if (!ctx)
    throw new Error("useApiClient must be used within ApiClientProvider");
  return ctx;
};

export const ApiClientProvider: React.FC<PropsWithChildren> = ({
  children,
}) => {
  const runtimeContext = useContext(RuntimeConfigContext);

  const client = useMemo<ApiClient | undefined>(() => {
    if (!runtimeContext?.apiEndpoint || !runtimeContext?.oidcConfig)
      return undefined;
    const baseUrl = runtimeContext.apiEndpoint.replace(/\/$/, "");
    const provider = OIDCProvider.build(runtimeContext.oidcConfig);

    const request = async <T,>(
      method: string,
      path: string,
      body?: unknown,
    ): Promise<T> => {
      const token = await provider.getIdToken();
      const headers: Record<string, string> = {
        Authorization: `Bearer ${token}`,
      };
      if (body) {
        headers["Content-Type"] = "application/json";
      }
      const res = await fetch(`${baseUrl}${path}`, {
        method,
        headers,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        const rawFallback = `API ${method} ${path} failed (${res.status}): ${text}`;
        // Left undefined until the body supplies them, so the 403 branch below
        // can tell "the server said nothing" from "the server said this". With
        // both pre-seeded to a fallback its `||` defaults were unreachable, and
        // a 403 with an empty body rendered the bare `API GET … failed (403):`
        // string at the user instead of the authorization sentence.
        let code: string | undefined;
        let message: string | undefined;
        let errorBody: unknown;

        try {
          const parsed = JSON.parse(text);
          errorBody = parsed;
          // Smithy-modeled errors carry `message`; a FastAPI `HTTPException`
          // uses `detail` instead, and most ontology-engine 4xx responses are
          // the latter. Reading only `message` left those falling through to
          // the raw fallback, so a caller that surfaced `error.message` showed
          // the user `API POST … failed (409): {"detail":"…"}` with the real
          // sentence buried in JSON. An object `detail` contributes only its
          // string `message` and `code` (see `errorFields`), so nothing renders
          // as "[object Object]".
          const fields = errorFields(parsed);
          code = fields.code;
          message = fields.message;
          // Some endpoints return a structured `errors` array with the
          // specific reasons (e.g. OSI import parse errors). Fold the string
          // entries into the message so the UI surfaces actionable detail
          // instead of just the generic top-level message. Non-string entries
          // are ignored so we never render "[object Object]".
          if (Array.isArray(parsed.errors)) {
            const details = parsed.errors.filter(
              (e: unknown): e is string => typeof e === "string",
            );
            if (details.length > 0) {
              message = `${message ?? rawFallback}: ${details.join("; ")}`;
            }
          }
        } catch {
          // not JSON, use defaults
        }

        if (res.status === 403) {
          throw new ApiError(
            403,
            code ?? "FORBIDDEN",
            message ?? "You are not authorized to perform this action.",
            errorBody,
          );
        }

        throw new ApiError(
          res.status,
          code ?? "UNKNOWN_ERROR",
          message ?? rawFallback,
          errorBody,
        );
      }
      if (res.status === 204 || res.headers.get("content-length") === "0") {
        return undefined as T;
      }
      return res.json();
    };

    return {
      get: <T,>(path: string) => request<T>("GET", path),
      post: <T,>(path: string, body: unknown) => request<T>("POST", path, body),
      put: <T,>(path: string, body: unknown) => request<T>("PUT", path, body),
      del: <T,>(path: string) => request<T>("DELETE", path),
      getText: async (path: string): Promise<string> => {
        const token = await provider.getIdToken();
        const res = await fetch(`${baseUrl}${path}`, {
          method: "GET",
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) {
          const text = await res.text().catch(() => "");
          let code = "UNKNOWN_ERROR";
          let message = `API GET ${path} failed (${res.status}): ${text}`;
          let errorBody: unknown;
          try {
            const parsed = JSON.parse(text);
            errorBody = parsed;
            // Same two envelopes as `request`; see the comment there.
            const fields = errorFields(parsed);
            code = fields.code ?? code;
            message = fields.message ?? message;
          } catch {
            // not JSON, use defaults
          }
          throw new ApiError(res.status, code, message, errorBody);
        }
        return res.text();
      },
    };
  }, [runtimeContext]);

  return (
    <ApiClientContext.Provider value={client}>
      {children}
    </ApiClientContext.Provider>
  );
};
