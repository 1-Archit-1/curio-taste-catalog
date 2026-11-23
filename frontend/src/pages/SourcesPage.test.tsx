import { render, screen, waitFor } from "@testing-library/react";
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
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

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
        return jsonResponse({
          sources: [],
          matched_count: 5,
          issue_count: 1,
          skipped_count: 0,
        });
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
    await screen.findByText("Imported 5 items (1 issues to review).");

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
  });
});
