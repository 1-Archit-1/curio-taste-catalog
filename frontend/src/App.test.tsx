import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppRoutes } from "./App";
import { AuthProvider } from "./auth/AuthContext";

interface MockResponseInit {
  status?: number;
  body?: unknown;
}

function jsonResponse({ status = 200, body }: MockResponseInit): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeSettings(overrides: Record<string, unknown> = {}) {
  return {
    user_id: "00000000-0000-0000-0000-000000000001",
    username: "alice",
    addon_manifest_url: "http://localhost:8000/tok123/manifest.json",
    llm_provider: "gemini",
    supported_llm_providers: ["gemini", "openai", "anthropic", "openrouter"],
    monthly_token_budget: 250000,
    jobs_paused_reason: null,
    keys: {
      tmdb: { configured: false, last4: null, updated_at: null },
      gemini: {
        configured: true,
        last4: "<b>9999</b>",
        updated_at: "2026-11-21T12:00:00Z",
      },
      openai: { configured: false, last4: null, updated_at: null },
      anthropic: { configured: false, last4: null, updated_at: null },
      openrouter: { configured: false, last4: null, updated_at: null },
    },
    ...overrides,
  };
}

function renderApp(initialEntries: string[] = ["/settings"]) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("Curio Control Plane SPA", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset();
  });

  afterEach(() => {
    // Unmount before unstubbing fetch so no in-flight request escapes the mock.
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("redirects unauthenticated users to login and signs in with a login token", async () => {
    const user = userEvent.setup();
    const settingsPayload = makeSettings();

    fetchMock.mockImplementation(async (input, init) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof Request
            ? input.url
            : String(input);
      const method = (
        init?.method ?? (input instanceof Request ? input.method : "GET")
      ).toUpperCase();
      const pathname = new URL(url, "http://localhost").pathname;

      if (pathname === "/api/auth/me" && method === "GET") {
        return jsonResponse({
          status: 401,
          body: { detail: "Not authenticated." },
        });
      }
      if (pathname === "/api/auth/login" && method === "POST") {
        const raw =
          input instanceof Request
            ? await input.clone().text()
            : String(init?.body ?? "");
        const parsed = JSON.parse(raw) as { token: string };
        if (parsed.token === "bad-token") {
          return jsonResponse({
            status: 401,
            body: { detail: "Invalid login token." },
          });
        }
        return jsonResponse({
          status: 200,
          body: {
            user: {
              id: "00000000-0000-0000-0000-000000000001",
              username: "alice",
            },
          },
        });
      }
      if (pathname === "/api/settings" && method === "GET") {
        return jsonResponse({ body: settingsPayload });
      }
      if (pathname === "/api/settings/audit" && method === "GET") {
        return jsonResponse({ body: { entries: [] } });
      }
      return jsonResponse({ status: 404, body: { detail: "Not found" } });
    });

    renderApp(["/settings"]);

    const input = await screen.findByLabelText("Login token");
    await user.type(input, "bad-token");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Invalid login token.")).toBeInTheDocument();

    await user.clear(input);
    await user.type(input, "good-token");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("Addon Manifest URL")).toBeInTheDocument();
    expect(screen.getByText("alice")).toBeInTheDocument();
  });

  it("renders Settings parity: notice, providers, safe last4, manifest rotate, prefs, keys, and audit", async () => {
    const user = userEvent.setup();
    let currentSettings = makeSettings();
    const auditEntries = [
      {
        id: 1,
        key_type: "gemini",
        job_id: "job-42",
        reason: "catalog_generation",
        created_at: "2026-11-21T13:30:00Z",
      },
    ];

    vi.spyOn(window, "confirm").mockReturnValue(true);

    fetchMock.mockImplementation(async (input, init) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof Request
            ? input.url
            : String(input);
      const method = (
        init?.method ?? (input instanceof Request ? input.method : "GET")
      ).toUpperCase();
      const pathname = new URL(url, "http://localhost").pathname;

      if (pathname === "/api/auth/me" && method === "GET") {
        return jsonResponse({
          body: {
            id: "00000000-0000-0000-0000-000000000001",
            username: "alice",
          },
        });
      }
      if (pathname === "/api/settings" && method === "GET") {
        return jsonResponse({ body: currentSettings });
      }
      if (pathname === "/api/settings" && method === "PATCH") {
        const raw =
          input instanceof Request
            ? await input.clone().text()
            : String(init?.body ?? "");
        const body = JSON.parse(raw) as {
          llm_provider: string;
          monthly_token_budget: number | null;
        };
        currentSettings = makeSettings({
          llm_provider: body.llm_provider,
          monthly_token_budget: body.monthly_token_budget,
        });
        return jsonResponse({ body: currentSettings });
      }
      if (
        pathname === "/api/settings/addon-token/rotate" &&
        method === "POST"
      ) {
        currentSettings = makeSettings({
          addon_manifest_url: "http://localhost:8000/rotated456/manifest.json",
        });
        return jsonResponse({
          body: { addon_manifest_url: currentSettings.addon_manifest_url },
        });
      }
      if (pathname === "/api/settings/keys/openai/test" && method === "POST") {
        return jsonResponse({
          body: { ok: true, status_code: 200, detail: "Key is valid." },
        });
      }
      if (pathname === "/api/settings/keys/openai" && method === "PUT") {
        currentSettings = makeSettings({
          keys: {
            ...currentSettings.keys,
            openai: {
              configured: true,
              last4: "5678",
              updated_at: "2026-11-21T14:00:00Z",
            },
          },
        });
        return jsonResponse({
          body: {
            key_type: "openai",
            configured: true,
            last4: "5678",
            updated_at: "2026-11-21T14:00:00Z",
          },
        });
      }
      if (pathname === "/api/settings/keys/gemini" && method === "DELETE") {
        currentSettings = makeSettings({
          keys: {
            ...currentSettings.keys,
            gemini: { configured: false, last4: null, updated_at: null },
          },
        });
        return jsonResponse({ body: { deleted: true } });
      }
      if (pathname === "/api/settings/audit" && method === "GET") {
        return jsonResponse({ body: { entries: auditEntries } });
      }
      return jsonResponse({ status: 404, body: { detail: "Not found" } });
    });

    renderApp(["/settings"]);

    // Manifest URL + notice + safe text rendering of last4 (never parsed as HTML).
    const manifestInput = await screen.findByLabelText("Addon Manifest URL");
    expect(manifestInput).toHaveValue(
      "http://localhost:8000/tok123/manifest.json",
    );
    expect(
      screen.getByText(
        /the server operator can technically access stored keys/i,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /dedicated gemini api key restricted to the gemini api with a quota cap/i,
      ),
    ).toBeInTheDocument();

    // Verify last4 with HTML-like characters is rendered as literal text, not an HTML element.
    expect(screen.getByText("••••<b>9999</b>")).toBeInTheDocument();

    // Rotate manifest URL.
    await user.click(screen.getByRole("button", { name: "Rotate URL" }));
    await waitFor(() => {
      expect(manifestInput).toHaveValue(
        "http://localhost:8000/rotated456/manifest.json",
      );
    });

    // Update LLM provider & monthly budget.
    await user.selectOptions(
      screen.getByLabelText("Active LLM provider"),
      "openai",
    );
    const budgetInput = screen.getByLabelText(
      "Monthly token budget (optional)",
    );
    await user.clear(budgetInput);
    await user.type(budgetInput, "500000");
    await user.click(screen.getByRole("button", { name: "Save Preferences" }));
    expect(await screen.findByText("Preferences saved.")).toBeInTheDocument();

    // Test & Save OpenAI key.
    const openaiRow = screen
      .getByLabelText("OpenAI API Key")
      .closest<HTMLElement>("[data-key-type='openai']");
    if (!openaiRow) throw new Error("OpenAI row not found");
    await user.type(screen.getByLabelText("OpenAI API Key"), "sk-test-5678");
    await user.click(within(openaiRow).getByRole("button", { name: "Test" }));
    expect(
      await within(openaiRow).findByText("Key is valid."),
    ).toBeInTheDocument();

    await user.click(within(openaiRow).getByRole("button", { name: "Save" }));
    expect(await within(openaiRow).findByText("••••5678")).toBeInTheDocument();
    expect(screen.getByLabelText("OpenAI API Key")).toHaveValue("");

    // Remove Gemini key.
    const geminiRow = screen
      .getByLabelText("Gemini API Key")
      .closest<HTMLElement>("[data-key-type='gemini']");
    if (!geminiRow) throw new Error("Gemini row not found");
    await user.click(within(geminiRow).getByRole("button", { name: "Remove" }));
    expect(await within(geminiRow).findByText("Not set")).toBeInTheDocument();

    // Audit log table displays entry.
    expect(screen.getByText("job-42")).toBeInTheDocument();
    expect(screen.getByText("catalog_generation")).toBeInTheDocument();
  });

  it("redirects to login when any /api/* call returns 401", async () => {
    const user = userEvent.setup();
    let sessionRevoked = false;

    fetchMock.mockImplementation(async (input, init) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof Request
            ? input.url
            : String(input);
      const method = (
        init?.method ?? (input instanceof Request ? input.method : "GET")
      ).toUpperCase();
      const pathname = new URL(url, "http://localhost").pathname;

      if (pathname === "/api/auth/me" && method === "GET") {
        return jsonResponse({
          body: {
            id: "00000000-0000-0000-0000-000000000001",
            username: "alice",
          },
        });
      }
      if (pathname === "/api/settings" && method === "GET") {
        return jsonResponse({ body: makeSettings() });
      }
      if (pathname === "/api/settings/audit" && method === "GET") {
        if (sessionRevoked) {
          return jsonResponse({
            status: 401,
            body: { detail: "Session expired." },
          });
        }
        return jsonResponse({ body: { entries: [] } });
      }
      return jsonResponse({ status: 404, body: { detail: "Not found" } });
    });

    renderApp(["/settings"]);
    expect(await screen.findByText("Addon Manifest URL")).toBeInTheDocument();

    sessionRevoked = true;
    await user.click(screen.getByRole("button", { name: "Refresh" }));

    expect(await screen.findByLabelText("Login token")).toBeInTheDocument();
  });
});
