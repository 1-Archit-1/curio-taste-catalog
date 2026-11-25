import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth/AuthContext";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const SYNC_JOB_ID = "11111111-1111-1111-1111-111111111111";
const SIMKL_SOURCE_ID = "22222222-2222-2222-2222-222222222222";
const AUTHORIZE_URL =
  "https://simkl.com/oauth2/authorize?client_id=cid&state=abc&code_challenge=xyz";

function status(extra: Record<string, unknown> = {}) {
  return {
    configured: true,
    connected: true,
    source_id: SIMKL_SOURCE_ID,
    account_name: "alice",
    last_synced_at: "2026-11-22T12:00:00Z",
    last_error: null,
    needs_reconnect: false,
    sync_job_id: null,
    ...extra,
  };
}

function syncJob(jobStatus: string, extra: Record<string, unknown> = {}) {
  return {
    id: SYNC_JOB_ID,
    preset_id: null,
    job_kind: "simkl_sync",
    status: jobStatus,
    trigger: "manual",
    paused_key_type: null,
    paused_reason: null,
    last_error: null,
    created_at: "2026-11-22T12:00:00Z",
    finished_at: null,
    ...extra,
  };
}

function jobStatus(
  jobStatus: string,
  events: Array<{ step: string; message: string }>,
  extra: Record<string, unknown> = {},
) {
  return {
    job: syncJob(jobStatus, extra),
    run: null,
    events: events.map((e, i) => ({
      id: i + 1,
      payload: null,
      created_at: "2026-11-22T12:00:01Z",
      ...e,
    })),
  };
}

const simklSource = {
  id: SIMKL_SOURCE_ID,
  kind: "simkl",
  external_key: "account",
  label: "Simkl (@alice)",
  url: "https://simkl.com/",
  role: "self",
  item_count: 42,
  last_refreshed_at: "2026-11-22T12:00:00Z",
  last_error: null,
  created_at: "2026-11-22T09:00:00Z",
  updated_at: "2026-11-22T12:00:00Z",
};

/** Shows the router's query string, so a test can see what the page left in the URL. */
function SearchProbe() {
  return <div data-testid="search">{useLocation().search}</div>;
}

function renderSources(search = "") {
  return render(
    <MemoryRouter initialEntries={[`/sources${search}`]}>
      <AuthProvider>
        <AppRoutes />
        <SearchProbe />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("SimklCard", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let routes: Record<string, (req: Request) => Response | Promise<Response>>;
  let sources: unknown[];

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset();
    sources = [];
    routes = {};
    fetchMock.mockImplementation(async (input, init) => {
      const req =
        input instanceof Request
          ? input
          : new Request(new URL(String(input), "http://localhost"), init);
      const url = new URL(req.url, "http://localhost");
      const key = `${req.method.toUpperCase()} ${url.pathname}`;
      const handler = routes[key];
      if (handler) return handler(req);
      if (key === "GET /api/auth/me") {
        return jsonResponse({
          user_id: "00000000-0000-0000-0000-000000000001",
          username: "archit",
        });
      }
      if (key === "GET /api/sources") return jsonResponse({ sources });
      if (key === "GET /api/import-issues") {
        return jsonResponse({ issues: [], pending_count: 0 });
      }
      return jsonResponse({ detail: `Unexpected ${key}` }, 500);
    });
  });

  afterEach(() => {
    // Unmount first, so nothing is requested after `fetch` is unstubbed.
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function issuedRequests(): string[] {
    return fetchMock.mock.calls.map(([input, init]) => {
      const req =
        input instanceof Request
          ? input
          : new Request(new URL(String(input), "http://localhost"), init);
      return `${req.method.toUpperCase()} ${new URL(req.url, "http://localhost").pathname}`;
    });
  }

  function count(request: string): number {
    return issuedRequests().filter((r) => r === request).length;
  }

  function stubNavigation() {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    return assign;
  }

  it("says so when the server has no Simkl app, and offers no way to connect", async () => {
    routes["GET /api/simkl/connection"] = () =>
      jsonResponse({ configured: false, connected: false });
    renderSources();
    await screen.findByText(/Simkl isn't set up on this server/);
    expect(
      screen.queryByRole("button", { name: /Connect Simkl/ }),
    ).not.toBeInTheDocument();
    expect(issuedRequests()).not.toContain("POST /api/simkl/connect");
  });

  it("connects by sending the browser to the URL the server returns", async () => {
    const user = userEvent.setup();
    const assign = stubNavigation();
    routes["GET /api/simkl/connection"] = () =>
      jsonResponse({ configured: true, connected: false });
    let connectBody = "unset";
    routes["POST /api/simkl/connect"] = async (req) => {
      connectBody = await req.text();
      return jsonResponse({ authorize_url: AUTHORIZE_URL });
    };
    renderSources();
    await user.click(
      await screen.findByRole("button", { name: "Connect Simkl" }),
    );
    await waitFor(() => expect(assign).toHaveBeenCalledWith(AUTHORIZE_URL));
    // The browser sends no redirect URI or anything else: the server decides.
    expect(connectBody).toBe("");
  });

  it("shows a connected account and follows a sync started with Sync now", async () => {
    const user = userEvent.setup();
    routes["GET /api/simkl/connection"] = () => jsonResponse(status());
    sources = [simklSource];
    routes["POST /api/simkl/sync"] = () => jsonResponse(syncJob("queued"), 202);
    let polls = 0;
    routes[`GET /api/jobs/${SYNC_JOB_ID}`] = () => {
      polls += 1;
      return polls === 1
        ? jsonResponse(
            jobStatus("running", [
              { step: "plan", message: "Checking Simkl for changes" },
            ]),
          )
        : jsonResponse(
            jobStatus("succeeded", [
              { step: "sync", message: "Simkl: 42 titles synced." },
              { step: "completed", message: "Simkl synced." },
            ]),
          );
    };
    renderSources();
    await screen.findByText("alice");
    expect(screen.getByText(/Last sync:/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sync now" }));
    await screen.findByText("Syncing Simkl: Checking Simkl for changes");
    expect(screen.getByRole("button", { name: "Sync now" })).toBeDisabled();
    await screen.findByText("Simkl: 42 titles synced.", {}, { timeout: 4000 });
    expect(screen.queryByText(/^Syncing Simkl/)).not.toBeInTheDocument();
    // The sources list and the card are loaded again when the job ends.
    await waitFor(() => expect(count("GET /api/sources")).toBe(2));
    await waitFor(() => expect(count("GET /api/simkl/connection")).toBe(2));
    expect(screen.getByRole("button", { name: "Sync now" })).toBeEnabled();
  });

  it("follows a sync that is already running when the page opens", async () => {
    routes["GET /api/simkl/connection"] = () =>
      jsonResponse(status({ sync_job_id: SYNC_JOB_ID, last_synced_at: null }));
    routes[`GET /api/jobs/${SYNC_JOB_ID}`] = () =>
      jsonResponse(
        jobStatus("succeeded", [{ step: "sync", message: "Simkl: 3 titles." }]),
      );
    renderSources();
    await screen.findByText("Simkl: 3 titles.");
    expect(count("POST /api/simkl/sync")).toBe(0);
  });

  it("shows why a sync failed or paused", async () => {
    const user = userEvent.setup();
    routes["GET /api/simkl/connection"] = () => jsonResponse(status());
    routes["POST /api/simkl/sync"] = () => jsonResponse(syncJob("queued"), 202);
    routes[`GET /api/jobs/${SYNC_JOB_ID}`] = () =>
      jsonResponse(
        jobStatus("failed", [], {
          last_error:
            "Simkl's daily request limit for this account is used up.",
        }),
      );
    renderSources();
    await user.click(await screen.findByRole("button", { name: "Sync now" }));
    await screen.findByText(
      "Simkl sync failed: Simkl's daily request limit for this account is used up.",
    );
  });

  it("asks to reconnect when Simkl rejected the saved connection", async () => {
    const user = userEvent.setup();
    const assign = stubNavigation();
    routes["GET /api/simkl/connection"] = () =>
      jsonResponse(
        status({
          needs_reconnect: true,
          last_error:
            "Simkl no longer accepts this connection. Reconnect Simkl.",
        }),
      );
    routes["POST /api/simkl/connect"] = () =>
      jsonResponse({ authorize_url: AUTHORIZE_URL });
    renderSources();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Reconnect to keep syncing/);
    expect(screen.getByRole("button", { name: "Sync now" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Reconnect Simkl" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(AUTHORIZE_URL));
  });

  it("shows the last error of a connection that still works", async () => {
    routes["GET /api/simkl/connection"] = () =>
      jsonResponse(status({ last_error: "Simkl is unavailable (HTTP 503)." }));
    renderSources();
    await screen.findByText("Simkl is unavailable (HTTP 503).");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("disconnects only after the user confirms", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    let connected = true;
    routes["GET /api/simkl/connection"] = () =>
      jsonResponse(
        connected ? status() : { configured: true, connected: false },
      );
    routes["DELETE /api/simkl/connection"] = () => {
      connected = false;
      return jsonResponse({ message: "Simkl disconnected." });
    };
    sources = [simklSource];
    renderSources();
    await user.click(await screen.findByRole("button", { name: "Disconnect" }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(count("DELETE /api/simkl/connection")).toBe(0);

    confirm.mockReturnValue(true);
    sources = [];
    await user.click(screen.getByRole("button", { name: "Disconnect" }));
    await screen.findByText("Simkl disconnected.");
    await screen.findByRole("button", { name: "Connect Simkl" });
    await waitFor(() => expect(count("GET /api/sources")).toBe(2));
  });

  it.each([
    [
      "connected",
      "Simkl connected. Your library is syncing in the background.",
    ],
    ["cancelled", "Simkl sign-in was cancelled."],
    ["error", "Simkl could not be connected. Try again."],
  ])(
    "shows ?simkl=%s once and clears it from the URL",
    async (outcome, text) => {
      routes["GET /api/simkl/connection"] = () =>
        jsonResponse({ configured: true, connected: false });
      renderSources(`?simkl=${outcome}`);
      await screen.findByText(text);
      await waitFor(() =>
        expect(screen.getByTestId("search")).toHaveTextContent(/^$/),
      );
      await screen.findByRole("button", { name: "Connect Simkl" });
    },
  );

  it("ignores an unknown ?simkl value, clears it, and never reads an OAuth code", async () => {
    routes["GET /api/simkl/connection"] = () =>
      jsonResponse({ configured: true, connected: false });
    renderSources("?simkl=bogus&code=secret-code&state=abc");
    await screen.findByRole("button", { name: "Connect Simkl" });
    await waitFor(() =>
      expect(screen.getByTestId("search")).toHaveTextContent(
        "?code=secret-code&state=abc",
      ),
    );
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    for (const [input, init] of fetchMock.mock.calls) {
      const req =
        input instanceof Request
          ? input
          : new Request(new URL(String(input), "http://localhost"), init);
      expect(req.url).not.toContain("secret-code");
      expect(await req.clone().text()).not.toContain("secret-code");
    }
  });

  it("lists the Simkl source with a fixed role and no Remove button", async () => {
    routes["GET /api/simkl/connection"] = () => jsonResponse(status());
    sources = [simklSource];
    renderSources();
    await screen.findByText("Simkl (@alice)");
    // A fixed label, not a role dropdown (the other "Self" texts are <option>s).
    expect(
      screen
        .getAllByText("Self (my history)")
        .some((el) => el.tagName === "SPAN"),
    ).toBe(true);
    expect(
      screen.queryByLabelText("Role for Simkl (@alice)"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Remove" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/Synced:/)).toBeInTheDocument();
    expect(screen.getByText("42 items")).toBeInTheDocument();
  });
});
