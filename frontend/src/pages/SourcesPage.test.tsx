import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth/AuthContext";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const IMPORT_JOB_ID = "ffffffff-ffff-ffff-ffff-ffffffffffff";

function importJob(status: string, extra: Record<string, unknown> = {}) {
  return {
    id: IMPORT_JOB_ID,
    preset_id: null,
    job_kind: "lb_import",
    status,
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
  status: "running" | "succeeded" | "failed" | "paused",
  extra: Record<string, unknown> = {},
) {
  const events: Array<{
    id: number;
    step: string;
    message: string;
    payload: Record<string, unknown> | null;
    created_at: string;
  }> = [
    {
      id: 1,
      step: "match",
      message: "Matching 6 rows against TMDB",
      payload: null,
      created_at: "2026-11-22T12:00:01Z",
    },
  ];
  if (status === "succeeded") {
    events.push({
      id: 2,
      step: "import",
      message: "Imported 5 titles; 1 to review.",
      payload: {
        matched_count: 5,
        issue_count: 1,
        skipped_count: 0,
        source_count: 2,
      },
      created_at: "2026-11-22T12:00:05Z",
    });
  }
  return {
    job: importJob(status, extra),
    run: null,
    events,
  };
}

function renderSourcesApp() {
  return render(
    <MemoryRouter initialEntries={["/sources"]}>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("SourcesPage", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset();
  });

  afterEach(() => {
    // Unmount first: otherwise the page can issue a request after `fetch` is unstubbed, which
    // escapes the mock and hits the real network.
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /** Method and path of every request issued so far. */
  function issuedRequests(): string[] {
    return fetchMock.mock.calls.map(([input, init]) => {
      const req =
        input instanceof Request
          ? input
          : new Request(new URL(String(input), "http://localhost"), init);
      return `${req.method.toUpperCase()} ${new URL(req.url, "http://localhost").pathname}`;
    });
  }

  it("supports previewing/adding RSS, role updates, refresh, remove, ZIP import, and import issue resolution", async () => {
    const user = userEvent.setup();

    let sources = [
      {
        id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        kind: "lb_diary_rss",
        external_key: "alice",
        label: "Letterboxd RSS (@alice)",
        url: "https://letterboxd.com/alice/rss/",
        role: "self",
        item_count: 12,
        last_refreshed_at: "2026-11-22T10:00:00Z",
        last_error: null,
        created_at: "2026-11-22T09:00:00Z",
        updated_at: "2026-11-22T10:00:00Z",
      },
    ];

    let issues = [
      {
        id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        source_id: "cccccccc-cccc-cccc-cccc-cccccccccccc",
        item_kind: "watched",
        raw_title: "Ambiguous Film",
        raw_year: 2020,
        letterboxd_url: "https://letterboxd.com/film/ambiguous-film/",
        rating: 4.0,
        watched_at: "2026-11-20T00:00:00Z",
        confidence: 0.65,
        candidate_matches: [
          {
            media_type: "movie",
            tmdb_id: 603,
            name: "The Matrix",
            year: 1999,
            confidence: 0.65,
          },
        ],
        status: "pending",
        resolved_media_type: null,
        resolved_tmdb_id: null,
        created_at: "2026-11-22T09:30:00Z",
        updated_at: "2026-11-22T09:30:00Z",
      },
      {
        id: "dddddddd-dddd-dddd-dddd-dddddddddddd",
        source_id: "cccccccc-cccc-cccc-cccc-cccccccccccc",
        item_kind: "rating",
        raw_title: "Unknown Short",
        raw_year: 2021,
        letterboxd_url: null,
        rating: 3.5,
        watched_at: null,
        confidence: 0.0,
        candidate_matches: [],
        status: "pending",
        resolved_media_type: null,
        resolved_tmdb_id: null,
        created_at: "2026-11-22T09:31:00Z",
        updated_at: "2026-11-22T09:31:00Z",
      },
    ];

    let jobPolls = 0;
    fetchMock.mockImplementation(async (input, init) => {
      const req =
        input instanceof Request
          ? input
          : new Request(new URL(String(input), "http://localhost"), init);
      const url = new URL(req.url, "http://localhost");
      const method = req.method.toUpperCase();

      if (url.pathname === "/api/auth/me") {
        return jsonResponse({
          user_id: "00000000-0000-0000-0000-000000000001",
          username: "archit",
        });
      }
      if (url.pathname === "/api/sources" && method === "GET") {
        return jsonResponse({ sources });
      }
      if (url.pathname === "/api/sources/preview" && method === "POST") {
        return jsonResponse({
          kind: "lb_diary_rss",
          username: "bob",
          label: "Letterboxd RSS (@bob)",
          url: "https://letterboxd.com/bob/rss/",
          entry_count: 2,
          recent_titles: [
            {
              title: "The Matrix",
              year: 1999,
              rating: 4.5,
              watched_at: null,
              tmdb_id: 603,
            },
          ],
        });
      }
      if (url.pathname === "/api/sources" && method === "POST") {
        const created = {
          id: "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
          kind: "lb_diary_rss",
          external_key: "bob",
          label: "Letterboxd RSS (@bob)",
          url: "https://letterboxd.com/bob/rss/",
          role: "taste",
          item_count: 2,
          last_refreshed_at: "2026-11-22T11:00:00Z",
          last_error: null,
          created_at: "2026-11-22T11:00:00Z",
          updated_at: "2026-11-22T11:00:00Z",
        };
        sources = [created, ...sources];
        return jsonResponse(created, 201);
      }
      if (url.pathname.startsWith("/api/sources/") && method === "PATCH") {
        const id = url.pathname.split("/")[3];
        const body = (await req.json()) as { role: string };
        sources = sources.map((s) =>
          s.id === id ? { ...s, role: body.role } : s,
        );
        return jsonResponse(sources.find((s) => s.id === id));
      }
      if (url.pathname.endsWith("/refresh") && method === "POST") {
        const id = url.pathname.split("/")[3];
        sources = sources.map((s) =>
          s.id === id ? { ...s, item_count: 15 } : s,
        );
        return jsonResponse(sources.find((s) => s.id === id));
      }
      if (url.pathname.startsWith("/api/sources/") && method === "DELETE") {
        const id = url.pathname.split("/")[3];
        sources = sources.filter((s) => s.id !== id);
        return jsonResponse({ message: "Source removed." });
      }
      if (url.pathname === "/api/sources/import-zip" && method === "POST") {
        return jsonResponse(importJob("queued"), 202);
      }
      if (url.pathname === `/api/jobs/${IMPORT_JOB_ID}` && method === "GET") {
        jobPolls += 1;
        return jsonResponse(
          jobPolls < 2 ? jobStatus("running") : jobStatus("succeeded"),
        );
      }
      if (url.pathname === "/api/import-issues" && method === "GET") {
        return jsonResponse({ issues, pending_count: issues.length });
      }
      if (url.pathname.endsWith("/resolve") && method === "POST") {
        const id = url.pathname.split("/")[3];
        issues = issues.filter((i) => i.id !== id);
        return jsonResponse({ id, status: "resolved" });
      }
      if (url.pathname.endsWith("/dismiss") && method === "POST") {
        const id = url.pathname.split("/")[3];
        issues = issues.filter((i) => i.id !== id);
        return jsonResponse({ id, status: "dismissed" });
      }
      return jsonResponse(
        { detail: `Unexpected ${method} ${url.pathname}` },
        500,
      );
    });

    renderSourcesApp();

    await screen.findByText("Letterboxd RSS (@alice)");
    expect(screen.getByText("Import Issues (2)")).toBeInTheDocument();

    // Preview and add a new RSS source
    const input = screen.getByLabelText("Letterboxd profile URL or username");
    await user.type(input, "bob");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByText("Letterboxd RSS (@bob)");
    await user.click(screen.getByRole("button", { name: "Add source" }));
    await screen.findByText("Added Letterboxd RSS (@bob) (2 items).");

    // Update role on alice's source
    const aliceRole = screen.getByLabelText("Role for Letterboxd RSS (@alice)");
    await user.selectOptions(aliceRole, "taste");
    await waitFor(() => {
      expect((aliceRole as HTMLSelectElement).value).toBe("taste");
    });

    // Refresh alice's source
    const refreshButtons = screen.getAllByRole("button", { name: "Refresh" });
    await user.click(refreshButtons[1]);
    await screen.findByText("Refreshed Letterboxd RSS (@alice) (15 items).");

    // Upload a ZIP
    const fileInput = screen.getByLabelText("Letterboxd export ZIP file");
    const zipFile = new File(
      [new Uint8Array([80, 75, 5, 6])],
      "letterboxd.zip",
      {
        type: "application/zip",
      },
    );
    await user.upload(fileInput, zipFile);
    await user.click(screen.getByRole("button", { name: "Import ZIP" }));
    // The upload returns a job; the page polls it every 2 s until the worker is done.
    await screen.findByText("Importing: Matching 6 rows against TMDB");
    expect(screen.getByRole("button", { name: "Import ZIP" })).toBeDisabled();
    await screen.findByText(
      "Imported 5 items (1 issues to review).",
      {},
      { timeout: 6000 },
    );
    expect(jobPolls).toBe(2);
    // The source list and import issues are reloaded once the job succeeds.
    await waitFor(() => {
      const requests = issuedRequests();
      const afterJob = requests.slice(
        requests.lastIndexOf(`GET /api/jobs/${IMPORT_JOB_ID}`),
      );
      expect(afterJob).toEqual(
        expect.arrayContaining(["GET /api/sources", "GET /api/import-issues"]),
      );
    });

    // Resolve first issue via candidate button
    await user.click(
      screen.getByRole("button", { name: /Pick: The Matrix \(1999\) — #603/ }),
    );
    await screen.findByText("Resolved to TMDB #603.");

    // Dismiss second issue
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    await screen.findByText("No pending import issues.");

    // Remove a source
    const removeButtons = screen.getAllByRole("button", { name: "Remove" });
    await user.click(removeButtons[0]);
    await screen.findByText("Source removed.");
  }, 20000);

  /** Answer the requests the page makes on load, plus `extra` routes keyed "METHOD /path". */
  function installApi(
    extra: Record<string, (req: Request) => Response | Promise<Response>>,
  ) {
    fetchMock.mockImplementation(async (input, init) => {
      const req =
        input instanceof Request
          ? input
          : new Request(new URL(String(input), "http://localhost"), init);
      const url = new URL(req.url, "http://localhost");
      const key = `${req.method.toUpperCase()} ${url.pathname}`;
      const handler = extra[key];
      if (handler) return handler(req);
      if (key === "GET /api/auth/me") {
        return jsonResponse({
          user_id: "00000000-0000-0000-0000-000000000001",
          username: "archit",
        });
      }
      if (key === "GET /api/sources") return jsonResponse({ sources: [] });
      if (key === "GET /api/import-issues") {
        return jsonResponse({ issues: [], pending_count: 0 });
      }
      return jsonResponse({ detail: `Unexpected ${key}` }, 500);
    });
  }

  async function chooseZip(user: ReturnType<typeof userEvent.setup>) {
    await screen.findByText("No taste sources yet.", { exact: false });
    const file = new File([new Uint8Array([80, 75, 5, 6])], "letterboxd.zip", {
      type: "application/zip",
    });
    await user.upload(
      screen.getByLabelText("Letterboxd export ZIP file"),
      file,
    );
    await user.click(screen.getByRole("button", { name: "Import ZIP" }));
  }

  it.each([
    [422, "missing required API keys: tmdb"],
    [
      409,
      "TMDB key paused: rejected. Replace your TMDB key in Settings to resume.",
    ],
    [413, "Export ZIP exceeds the 50 MB size limit."],
  ])(
    "shows the %i detail when the ZIP upload is rejected",
    async (status, detail) => {
      const user = userEvent.setup();
      installApi({
        "POST /api/sources/import-zip": () => jsonResponse({ detail }, status),
      });
      renderSourcesApp();

      await chooseZip(user);

      await screen.findByText(detail);
      expect(screen.getByRole("button", { name: "Import ZIP" })).toBeEnabled();
      expect(issuedRequests()).not.toContain(`GET /api/jobs/${IMPORT_JOB_ID}`);
    },
  );

  it("shows why an import job failed and reloads the sources", async () => {
    const user = userEvent.setup();
    installApi({
      "POST /api/sources/import-zip": () =>
        jsonResponse(importJob("queued"), 202),
      [`GET /api/jobs/${IMPORT_JOB_ID}`]: () =>
        jsonResponse(
          jobStatus("failed", {
            last_error: "Uploaded file is not a valid ZIP archive.",
          }),
        ),
    });
    renderSourcesApp();

    await chooseZip(user);

    await screen.findByText(
      "Import failed: Uploaded file is not a valid ZIP archive.",
    );
    await waitFor(() => {
      const requests = issuedRequests();
      expect(requests.filter((r) => r === "GET /api/sources")).toHaveLength(2);
    });
    expect(screen.queryByText(/^Importing:/)).not.toBeInTheDocument();
  });

  it("stops following a paused import and shows the reason", async () => {
    const user = userEvent.setup();
    installApi({
      "POST /api/sources/import-zip": () =>
        jsonResponse(importJob("queued"), 202),
      [`GET /api/jobs/${IMPORT_JOB_ID}`]: () =>
        jsonResponse(
          jobStatus("paused", {
            paused_reason:
              "TMDB key paused: rejected. Replace your TMDB key in Settings to resume.",
          }),
        ),
    });
    renderSourcesApp();

    await chooseZip(user);

    await screen.findByText(/^Import paused: TMDB key paused/);
    await waitFor(() => {
      expect(
        issuedRequests().filter((r) => r === "GET /api/sources"),
      ).toHaveLength(2);
    });
  });

  it("shows when each source was last refreshed or imported", async () => {
    installApi({
      "GET /api/sources": () =>
        jsonResponse({
          sources: [
            {
              id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
              kind: "lb_profile_csv",
              external_key: "profile",
              label: "Letterboxd Export (@alice)",
              url: null,
              role: "self",
              item_count: 4,
              last_refreshed_at: "2026-11-22T10:00:00Z",
              last_error: null,
              created_at: "2026-11-22T09:00:00Z",
              updated_at: "2026-11-22T10:00:00Z",
            },
            {
              id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
              kind: "lb_diary_rss",
              external_key: "bob",
              label: "Letterboxd RSS (@bob)",
              url: "https://letterboxd.com/bob/rss/",
              role: "taste",
              item_count: 2,
              last_refreshed_at: null,
              last_error: null,
              created_at: "2026-11-22T09:00:00Z",
              updated_at: "2026-11-22T10:00:00Z",
            },
          ],
        }),
    });
    renderSourcesApp();

    await screen.findByText("Letterboxd Export (@alice)");
    const when = new Date("2026-11-22T10:00:00Z").toLocaleString();
    expect(screen.getByText(`Imported: ${when}`)).toBeInTheDocument();
    expect(screen.getByText("Refreshed: Never")).toBeInTheDocument();
  });

  it("resolves an issue to the candidate's own media type", async () => {
    const user = userEvent.setup();
    const resolved: unknown[] = [];
    installApi({
      "GET /api/import-issues": () =>
        jsonResponse({
          pending_count: 1,
          issues: [
            {
              id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
              source_id: "cccccccc-cccc-cccc-cccc-cccccccccccc",
              item_kind: "watched",
              raw_title: "Breaking Bad",
              raw_year: 2008,
              letterboxd_url: null,
              rating: null,
              watched_at: null,
              confidence: 0.6,
              candidate_matches: [
                {
                  media_type: "series",
                  tmdb_id: 1396,
                  name: "Breaking Bad",
                  year: 2008,
                  confidence: 0.6,
                },
              ],
              status: "pending",
              resolved_media_type: null,
              resolved_tmdb_id: null,
              created_at: "2026-11-22T09:30:00Z",
              updated_at: "2026-11-22T09:30:00Z",
            },
          ],
        }),
      "POST /api/import-issues/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/resolve":
        async (req) => {
          resolved.push(await req.json());
          return jsonResponse({ status: "resolved" });
        },
    });
    renderSourcesApp();

    await user.click(
      await screen.findByRole("button", { name: /Pick: Breaking Bad/ }),
    );

    await screen.findByText("Resolved to TMDB #1396.");
    expect(resolved).toEqual([{ media_type: "series", tmdb_id: 1396 }]);
  });
});
