import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";

type UnauthorizedListener = () => void;

const unauthorizedListeners = new Set<UnauthorizedListener>();

export function onUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener);
  return () => {
    unauthorizedListeners.delete(listener);
  };
}

function notifyUnauthorized() {
  for (const listener of unauthorizedListeners) {
    listener();
  }
}

const unauthorizedMiddleware: Middleware = {
  async onResponse({ request, response }) {
    if (response.status === 401) {
      const url = new URL(request.url, window.location.origin);
      // Login failures return 401 by design; every other /api/* 401 means the session is invalid.
      if (url.pathname !== "/api/auth/login") {
        notifyUnauthorized();
      }
    }
    return response;
  },
};

export const apiClient = createClient<paths>({
  baseUrl: typeof window !== "undefined" ? window.location.origin : "",
  credentials: "same-origin",
  fetch: (input: Request) => globalThis.fetch(input),
});

apiClient.use(unauthorizedMiddleware);

export function extractErrorDetail(error: unknown, fallback: string): string {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) {
      return detail;
    }
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0];
      if (first && typeof first === "object" && "msg" in first && typeof first.msg === "string") {
        return first.msg;
      }
    }
  }
  return fallback;
}
