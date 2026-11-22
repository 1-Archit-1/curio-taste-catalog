import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth/AuthContext";

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

function renderCatalogsApp() {
  return render(
    <MemoryRouter initialEntries={["/catalogs"]}>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("CatalogsPage", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("supports slot assignment, preset create/edit/delete, and candidate Seen / Not for me / Undo", async () => {
    const user = userEvent.setup();

    let presets = [
      {
        id: "11111111-1111-1111-1111-111111111111",
        name: "Noir Picks",
        media_type: "movie",
        min_rating: 7.5,
        criteria: null,
        created_at: "2026-11-21T12:00:00Z",
        updated_at: "2026-11-21T12:00:00Z",
      },
      {
        id: "22222222-2222-2222-2222-222222222222",
        name: "Sci-Fi Series",
        media_type: "series",
        min_rating: null,
        criteria: null,
        created_at: "2026-11-21T12:05:00Z",
        updated_at: "2026-11-21T12:05:00Z",
      },
    ];

    let slots: Array<{
      slot_id: string;
      name: string;
      media_type: string;
      preset_id: string | null;
      preset_name: string | null;
    }> = [
      {
        slot_id: "curio_movie_1",
        name: "Curio Movies 1",
        media_type: "movie",
        preset_id: "11111111-1111-1111-1111-111111111111",
        preset_name: "Noir Picks",
      },
      {
        slot_id: "curio_movie_2",
        name: "Curio Movies 2",
        media_type: "movie",
        preset_id: null,
        preset_name: null,
      },
      {
        slot_id: "curio_movie_3",
        name: "Curio Movies 3",
        media_type: "movie",
        preset_id: null,
        preset_name: null,
      },
      {
        slot_id: "curio_series_1",
        name: "Curio Series 1",
        media_type: "series",
        preset_id: null,
        preset_name: null,
      },
      {
        slot_id: "curio_series_2",
        name: "Curio Series 2",
        media_type: "series",
        preset_id: null,
        preset_name: null,
      },
      {
        slot_id: "curio_series_3",
        name: "Curio Series 3",
        media_type: "series",
        preset_id: null,
        preset_name: null,
      },
    ];

    const candidatesByPreset: Record<
      string,
      Array<{
        imdb_id: string;
        media_type: "movie" | "series";
        name: string;
        year: number | null;
        poster: string;
        description: string | null;
        rating: number | null;
        blurb: string | null;
        position: number;
        watch_status: string | null;
        veto_kind: "seen" | "not_for_me" | null;
      }>
    > = {
      "11111111-1111-1111-1111-111111111111": [
        {
          imdb_id: "tt0133093",
          media_type: "movie",
          name: "The Matrix",
          year: 1999,
          poster: "https://image.tmdb.org/t/p/w500/matrix.jpg",
          description: "A hacker learns the truth.",
          rating: 8.7,
          blurb: "Cyberpunk classic.",
          position: 1,
          watch_status: null,
          veto_kind: null,
        },
        {
          imdb_id: "tt0083658",
          media_type: "movie",
          name: "Blade Runner",
          year: 1982,
          poster: "https://image.tmdb.org/t/p/w500/blade.jpg",
          description: "Replicants in LA.",
          rating: 8.1,
          blurb: null,
          position: 2,
          watch_status: null,
          veto_kind: null,
        },
      ],
    };

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

      const readBody = async () => {
        const raw =
          input instanceof Request
            ? await input.clone().text()
            : String(init?.body ?? "{}");
        return JSON.parse(raw) as Record<string, unknown>;
      };

      if (pathname === "/api/auth/me" && method === "GET") {
        return jsonResponse({
          body: {
            id: "00000000-0000-0000-0000-000000000001",
            username: "alice",
          },
        });
      }
      if (pathname === "/api/slots" && method === "GET") {
        return jsonResponse({ body: { slots } });
      }
      if (pathname.startsWith("/api/slots/") && method === "PUT") {
        const slotId = pathname.split("/").pop() ?? "";
        const body = await readBody();
        const presetId = (body.preset_id as string | null) ?? null;
        const preset = presets.find((p) => p.id === presetId) ?? null;
        slots = slots.map((s) =>
          s.slot_id === slotId
            ? {
                ...s,
                preset_id: presetId,
                preset_name: preset ? preset.name : null,
              }
            : s,
        );
        const updated = slots.find((s) => s.slot_id === slotId);
        return jsonResponse({ body: updated });
      }
      if (pathname === "/api/presets" && method === "GET") {
        return jsonResponse({ body: { presets } });
      }
      if (pathname === "/api/presets" && method === "POST") {
        const body = await readBody();
        const created = {
          id: "33333333-3333-3333-3333-333333333333",
          name: String(body.name),
          media_type: String(body.media_type),
          min_rating: (body.min_rating as number | null) ?? null,
          criteria: null,
          created_at: "2026-11-21T12:10:00Z",
          updated_at: "2026-11-21T12:10:00Z",
        };
        presets = [...presets, created];
        candidatesByPreset[created.id] = [];
        return jsonResponse({ status: 201, body: created });
      }
      if (
        pathname.startsWith("/api/presets/") &&
        pathname.endsWith("/candidates") &&
        method === "GET"
      ) {
        const presetId = pathname.split("/")[3] ?? "";
        return jsonResponse({
          body: {
            preset_id: presetId,
            candidates: candidatesByPreset[presetId] ?? [],
          },
        });
      }
      if (pathname.startsWith("/api/presets/") && method === "PATCH") {
        const presetId = pathname.split("/").pop() ?? "";
        const body = await readBody();
        presets = presets.map((p) =>
          p.id === presetId
            ? {
                ...p,
                name: String(body.name),
                min_rating: (body.min_rating as number | null) ?? null,
              }
            : p,
        );
        const updated = presets.find((p) => p.id === presetId);
        return jsonResponse({ body: updated });
      }
      if (pathname.startsWith("/api/presets/") && method === "DELETE") {
        const presetId = pathname.split("/").pop() ?? "";
        presets = presets.filter((p) => p.id !== presetId);
        slots = slots.map((s) =>
          s.preset_id === presetId
            ? { ...s, preset_id: null, preset_name: null }
            : s,
        );
        return jsonResponse({ body: { message: "Preset deleted." } });
      }
      if (pathname.startsWith("/api/vetoes/") && method === "PUT") {
        const imdbId = pathname.split("/").pop() ?? "";
        const body = await readBody();
        const kind = body.kind as "seen" | "not_for_me";
        for (const list of Object.values(candidatesByPreset)) {
          for (const item of list) {
            if (item.imdb_id === imdbId) {
              item.veto_kind = kind;
            }
          }
        }
        return jsonResponse({ body: { imdb_id: imdbId, kind } });
      }
      if (pathname.startsWith("/api/vetoes/") && method === "DELETE") {
        const imdbId = pathname.split("/").pop() ?? "";
        for (const list of Object.values(candidatesByPreset)) {
          for (const item of list) {
            if (item.imdb_id === imdbId) {
              item.veto_kind = null;
            }
          }
        }
        return jsonResponse({ body: { message: "Veto removed." } });
      }
      return jsonResponse({ status: 404, body: { detail: "Not found" } });
    });

    renderCatalogsApp();

    // Slots & default preset candidates load.
    expect(await screen.findByText("Curio Movies 1")).toBeInTheDocument();
    expect(await screen.findByText("The Matrix")).toBeInTheDocument();
    expect(screen.getByText("Blade Runner")).toBeInTheDocument();

    // Assign a series slot to Sci-Fi Series.
    const seriesSlotSelect = screen.getByLabelText("Preset for Curio Series 1");
    await user.selectOptions(
      seriesSlotSelect,
      "22222222-2222-2222-2222-222222222222",
    );
    expect(
      await screen.findByText("Updated Curio Series 1."),
    ).toBeInTheDocument();

    // Create a new preset.
    await user.type(screen.getByLabelText("Preset name"), "Indie Gems");
    await user.selectOptions(screen.getByLabelText("Media type"), "movie");
    await user.type(screen.getByLabelText("Min rating (optional)"), "7.8");
    await user.click(screen.getByRole("button", { name: "Create Preset" }));
    expect(
      await screen.findByText('Created "Indie Gems".'),
    ).toBeInTheDocument();

    // Delete the newly created preset.
    const indieRow = screen
      .getByRole("button", { name: "Indie Gems" })
      .closest<HTMLElement>("[data-preset-id]");
    if (!indieRow) throw new Error("Indie Gems row not found");
    await user.click(within(indieRow).getByRole("button", { name: "Delete" }));
    expect(
      await screen.findByText('Deleted "Indie Gems".'),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "Indie Gems" }),
      ).not.toBeInTheDocument();
    });

    // Veto "The Matrix" as Seen, then Undo via the undo banner.
    const matrixCard = screen
      .getByText("The Matrix")
      .closest<HTMLElement>("[data-imdb-id='tt0133093']");
    if (!matrixCard) throw new Error("Matrix card not found");

    await user.click(within(matrixCard).getByRole("button", { name: "Seen" }));
    const undoBanner = await screen.findByRole("status");
    expect(undoBanner).toHaveTextContent(/Marked The Matrix as Seen\./i);

    await user.click(within(undoBanner).getByRole("button", { name: "Undo" }));
    await waitFor(() => {
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
    });
    expect(
      within(matrixCard).queryByRole("button", { name: "Undo" }),
    ).not.toBeInTheDocument();

    // Veto "Blade Runner" as Not for me, then Undo via the card's Undo button.
    const bladeCard = screen
      .getByText("Blade Runner")
      .closest<HTMLElement>("[data-imdb-id='tt0083658']");
    if (!bladeCard) throw new Error("Blade Runner card not found");

    await user.click(
      within(bladeCard).getByRole("button", { name: "Not for me" }),
    );
    expect(
      await within(bladeCard).findByRole("button", { name: "Undo" }),
    ).toBeInTheDocument();

    await user.click(within(bladeCard).getByRole("button", { name: "Undo" }));
    await waitFor(() => {
      expect(
        within(bladeCard).queryByRole("button", { name: "Undo" }),
      ).not.toBeInTheDocument();
    });
  });
});
