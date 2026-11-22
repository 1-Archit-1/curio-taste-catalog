import { type FormEvent, useCallback, useEffect, useState } from "react";
import { apiClient, extractErrorDetail } from "../api/client";
import type { components } from "../api/schema";
import styles from "./CatalogsPage.module.css";

type SlotOut = components["schemas"]["SlotOut"];
type PresetOut = components["schemas"]["PresetOut"];
type CandidateOut = components["schemas"]["CandidateOut"];
type VetoOut = components["schemas"]["VetoOut"];
type MediaType = PresetOut["media_type"];
type VetoKind = VetoOut["kind"];

interface StatusMessage {
  text: string;
  kind: "" | "ok" | "err";
}

/** Titles are keyed by TMDB `(media_type, tmdb_id)`; `imdb_id` may be missing. */
type TitleKey = Pick<CandidateOut, "media_type" | "tmdb_id">;

function sameTitle(a: TitleKey, b: TitleKey): boolean {
  return a.media_type === b.media_type && a.tmdb_id === b.tmdb_id;
}

function titleKey(title: TitleKey): string {
  return `${title.media_type}:${title.tmdb_id}`;
}

function vetoPath(title: TitleKey) {
  return {
    params: { path: { media_type: title.media_type, tmdb_id: title.tmdb_id } },
  };
}

interface RecentVeto extends TitleKey {
  name: string;
  kind: VetoKind;
  previousVeto: VetoKind | null;
}

function parseOptionalRating(raw: string): number | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

export function CatalogsPage() {
  const [slots, setSlots] = useState<SlotOut[]>([]);
  const [presets, setPresets] = useState<PresetOut[]>([]);
  const [selectedPresetId, setSelectedPresetId] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<CandidateOut[]>([]);
  const [loadingCandidates, setLoadingCandidates] = useState(false);

  const [loadError, setLoadError] = useState<string | null>(null);
  const [slotStatus, setSlotStatus] = useState<StatusMessage>({
    text: "",
    kind: "",
  });
  const [presetStatus, setPresetStatus] = useState<StatusMessage>({
    text: "",
    kind: "",
  });
  const [vetoStatus, setVetoStatus] = useState<StatusMessage>({
    text: "",
    kind: "",
  });
  const [lastVeto, setLastVeto] = useState<RecentVeto | null>(null);

  // Create preset form state
  const [newName, setNewName] = useState("");
  const [newMediaType, setNewMediaType] = useState<MediaType>("movie");
  const [newMinRating, setNewMinRating] = useState("");

  // Inline edit state
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editMinRating, setEditMinRating] = useState("");

  const loadCandidates = useCallback(async (presetId: string) => {
    setLoadingCandidates(true);
    const { data, error, response } = await apiClient.GET(
      "/api/presets/{preset_id}/candidates",
      { params: { path: { preset_id: presetId } } },
    );
    setLoadingCandidates(false);
    if (response.ok && data) {
      setCandidates(data.candidates);
    } else if (response.status !== 401) {
      setVetoStatus({
        text: extractErrorDetail(error, "Could not load candidates."),
        kind: "err",
      });
    }
  }, []);

  const loadInitial = useCallback(async () => {
    const [slotsRes, presetsRes] = await Promise.all([
      apiClient.GET("/api/slots"),
      apiClient.GET("/api/presets"),
    ]);

    if (!slotsRes.response.ok) {
      if (slotsRes.response.status !== 401) {
        setLoadError(
          extractErrorDetail(slotsRes.error, "Could not load slots."),
        );
      }
      return;
    }
    if (!presetsRes.response.ok) {
      if (presetsRes.response.status !== 401) {
        setLoadError(
          extractErrorDetail(presetsRes.error, "Could not load presets."),
        );
      }
      return;
    }

    const loadedSlots = slotsRes.data?.slots ?? [];
    const loadedPresets = presetsRes.data?.presets ?? [];
    setSlots(loadedSlots);
    setPresets(loadedPresets);
    setLoadError(null);

    const firstAssigned = loadedSlots.find((s) => s.preset_id !== null);
    const defaultPresetId =
      firstAssigned?.preset_id ?? loadedPresets[0]?.id ?? null;
    setSelectedPresetId(defaultPresetId);
  }, []);

  useEffect(() => {
    void loadInitial();
  }, [loadInitial]);

  useEffect(() => {
    if (!selectedPresetId) {
      setCandidates([]);
      return;
    }
    void loadCandidates(selectedPresetId);
  }, [selectedPresetId, loadCandidates]);

  async function handleAssignSlot(slotId: string, presetId: string) {
    setSlotStatus({ text: "", kind: "" });
    const { data, error, response } = await apiClient.PUT(
      "/api/slots/{slot_id}",
      {
        params: { path: { slot_id: slotId } },
        body: { preset_id: presetId || null },
      },
    );
    if (response.ok && data) {
      setSlots((prev) => prev.map((s) => (s.slot_id === slotId ? data : s)));
      setSlotStatus({ text: `Updated ${data.name}.`, kind: "ok" });
    } else if (response.status !== 401) {
      setSlotStatus({
        text: extractErrorDetail(error, "Could not update slot."),
        kind: "err",
      });
    }
  }

  async function handleCreatePreset(event: FormEvent) {
    event.preventDefault();
    const trimmed = newName.trim();
    if (!trimmed) {
      setPresetStatus({ text: "Preset name is required.", kind: "err" });
      return;
    }
    setPresetStatus({ text: "", kind: "" });
    const { data, error, response } = await apiClient.POST("/api/presets", {
      body: {
        name: trimmed,
        media_type: newMediaType,
        min_rating: parseOptionalRating(newMinRating),
      },
    });
    if (response.ok && data) {
      setPresets((prev) => [...prev, data]);
      setNewName("");
      setNewMinRating("");
      setPresetStatus({ text: `Created "${data.name}".`, kind: "ok" });
      if (!selectedPresetId) {
        setSelectedPresetId(data.id);
      }
    } else if (response.status !== 401) {
      setPresetStatus({
        text: extractErrorDetail(error, "Could not create preset."),
        kind: "err",
      });
    }
  }

  function startEditPreset(preset: PresetOut) {
    setEditingId(preset.id);
    setEditName(preset.name);
    setEditMinRating(
      preset.min_rating !== null && preset.min_rating !== undefined
        ? String(preset.min_rating)
        : "",
    );
  }

  async function handleSavePreset(presetId: string) {
    const trimmed = editName.trim();
    if (!trimmed) {
      setPresetStatus({ text: "Preset name is required.", kind: "err" });
      return;
    }
    setPresetStatus({ text: "", kind: "" });
    const { data, error, response } = await apiClient.PATCH(
      "/api/presets/{preset_id}",
      {
        params: { path: { preset_id: presetId } },
        body: {
          name: trimmed,
          min_rating: parseOptionalRating(editMinRating),
        },
      },
    );
    if (response.ok && data) {
      setPresets((prev) => prev.map((p) => (p.id === presetId ? data : p)));
      setSlots((prev) =>
        prev.map((s) =>
          s.preset_id === presetId ? { ...s, preset_name: data.name } : s,
        ),
      );
      setEditingId(null);
      setPresetStatus({ text: `Saved "${data.name}".`, kind: "ok" });
    } else if (response.status !== 401) {
      setPresetStatus({
        text: extractErrorDetail(error, "Could not update preset."),
        kind: "err",
      });
    }
  }

  async function handleDeletePreset(preset: PresetOut) {
    setPresetStatus({ text: "", kind: "" });
    const { error, response } = await apiClient.DELETE(
      "/api/presets/{preset_id}",
      { params: { path: { preset_id: preset.id } } },
    );
    if (response.ok) {
      const remaining = presets.filter((p) => p.id !== preset.id);
      setPresets(remaining);
      setSlots((prev) =>
        prev.map((s) =>
          s.preset_id === preset.id
            ? { ...s, preset_id: null, preset_name: null }
            : s,
        ),
      );
      if (selectedPresetId === preset.id) {
        setSelectedPresetId(remaining[0]?.id ?? null);
      }
      setPresetStatus({ text: `Deleted "${preset.name}".`, kind: "ok" });
    } else if (response.status !== 401) {
      setPresetStatus({
        text: extractErrorDetail(error, "Could not delete preset."),
        kind: "err",
      });
    }
  }

  async function handleVeto(candidate: CandidateOut, kind: VetoKind) {
    setVetoStatus({ text: "", kind: "" });
    const previousVeto = candidate.veto_kind ?? null;
    const { data, error, response } = await apiClient.PUT(
      "/api/vetoes/{media_type}/{tmdb_id}",
      { ...vetoPath(candidate), body: { kind } },
    );
    if (response.ok && data) {
      setCandidates((prev) =>
        prev.map((c) =>
          sameTitle(c, candidate) ? { ...c, veto_kind: data.kind } : c,
        ),
      );
      setLastVeto({
        media_type: candidate.media_type,
        tmdb_id: candidate.tmdb_id,
        name: candidate.name,
        kind: data.kind,
        previousVeto,
      });
    } else if (response.status !== 401) {
      setVetoStatus({
        text: extractErrorDetail(error, "Could not save veto."),
        kind: "err",
      });
    }
  }

  async function handleUndoVeto(
    title: TitleKey,
    previousVeto: VetoKind | null = null,
  ) {
    setVetoStatus({ text: "", kind: "" });
    if (previousVeto !== null) {
      const { data, error, response } = await apiClient.PUT(
        "/api/vetoes/{media_type}/{tmdb_id}",
        { ...vetoPath(title), body: { kind: previousVeto } },
      );
      if (response.ok && data) {
        setCandidates((prev) =>
          prev.map((c) =>
            sameTitle(c, title) ? { ...c, veto_kind: data.kind } : c,
          ),
        );
        setLastVeto((prev) => (prev && sameTitle(prev, title) ? null : prev));
      } else if (response.status !== 401) {
        setVetoStatus({
          text: extractErrorDetail(error, "Could not restore veto."),
          kind: "err",
        });
      }
      return;
    }

    const { error, response } = await apiClient.DELETE(
      "/api/vetoes/{media_type}/{tmdb_id}",
      vetoPath(title),
    );
    if (response.ok) {
      setCandidates((prev) =>
        prev.map((c) => (sameTitle(c, title) ? { ...c, veto_kind: null } : c)),
      );
      setLastVeto((prev) => (prev && sameTitle(prev, title) ? null : prev));
    } else if (response.status !== 401) {
      setVetoStatus({
        text: extractErrorDetail(error, "Could not remove veto."),
        kind: "err",
      });
    }
  }

  const selectedPreset = presets.find((p) => p.id === selectedPresetId) ?? null;

  return (
    <div>
      {loadError && (
        <div className="notice notice-err" role="alert">
          {loadError}
        </div>
      )}

      <section className="card">
        <h2>Slots</h2>
        <p className="subtitle">
          Assign any matching preset to the 6 fixed Stremio catalog rows.
        </p>
        <div className={styles.slotList}>
          {slots.map((slot) => {
            const matchingPresets = presets.filter(
              (p) => p.media_type === slot.media_type,
            );
            return (
              <div
                key={slot.slot_id}
                className={styles.slotRow}
                data-slot-id={slot.slot_id}
              >
                <div className={styles.slotInfo}>
                  <span className={styles.slotName}>{slot.name}</span>
                  <span className={styles.badge}>{slot.media_type}</span>
                </div>
                <select
                  aria-label={`Preset for ${slot.name}`}
                  value={slot.preset_id ?? ""}
                  onChange={(e) =>
                    void handleAssignSlot(slot.slot_id, e.target.value)
                  }
                >
                  <option value="">Unassigned</option>
                  {matchingPresets.map((preset) => (
                    <option key={preset.id} value={preset.id}>
                      {preset.name}
                    </option>
                  ))}
                </select>
              </div>
            );
          })}
        </div>
        {slotStatus.text && (
          <div className={`status ${slotStatus.kind}`}>{slotStatus.text}</div>
        )}
      </section>

      <section className="card">
        <h2>Presets</h2>
        <p className="subtitle">
          Create and manage catalog presets, then select one to inspect its
          candidates.
        </p>

        <form className={styles.presetForm} onSubmit={handleCreatePreset}>
          <label className="field grow">
            <span>Preset name</span>
            <input
              type="text"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="e.g. Late Night Thrillers"
            />
          </label>
          <label className="field">
            <span>Media type</span>
            <select
              value={newMediaType}
              onChange={(e) => setNewMediaType(e.target.value as MediaType)}
            >
              <option value="movie">movie</option>
              <option value="series">series</option>
            </select>
          </label>
          <label className="field">
            <span>Min rating (optional)</span>
            <input
              type="number"
              min="0"
              max="10"
              step="0.1"
              value={newMinRating}
              onChange={(e) => setNewMinRating(e.target.value)}
              placeholder="7.0"
            />
          </label>
          <button className="btn primary" type="submit">
            Create Preset
          </button>
        </form>

        <div className={styles.presetList}>
          {presets.length === 0 ? (
            <p className={styles.muted}>No presets yet.</p>
          ) : (
            presets.map((preset) => {
              const isSelected = preset.id === selectedPresetId;
              const isEditing = editingId === preset.id;
              return (
                <div
                  key={preset.id}
                  className={styles.presetRow}
                  data-preset-id={preset.id}
                >
                  {isEditing ? (
                    <div className="row grow">
                      <input
                        type="text"
                        aria-label="Edit preset name"
                        value={editName}
                        onChange={(e) => setEditName(e.target.value)}
                      />
                      <input
                        type="number"
                        aria-label="Edit min rating"
                        min="0"
                        max="10"
                        step="0.1"
                        value={editMinRating}
                        onChange={(e) => setEditMinRating(e.target.value)}
                        placeholder="Min rating"
                      />
                      <button
                        className="btn primary"
                        type="button"
                        onClick={() => void handleSavePreset(preset.id)}
                      >
                        Save
                      </button>
                      <button
                        className="btn"
                        type="button"
                        onClick={() => setEditingId(null)}
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <>
                      <div className={styles.presetMeta}>
                        <button
                          type="button"
                          className={
                            isSelected
                              ? `${styles.presetNameBtn} ${styles.presetSelected}`
                              : styles.presetNameBtn
                          }
                          onClick={() => setSelectedPresetId(preset.id)}
                        >
                          {preset.name}
                        </button>
                        <span
                          className={
                            isSelected
                              ? `${styles.badge} ${styles.badgeActive}`
                              : styles.badge
                          }
                        >
                          {preset.media_type}
                        </span>
                        {preset.min_rating !== null &&
                          preset.min_rating !== undefined && (
                            <span className={styles.muted}>
                              min {preset.min_rating.toFixed(1)}
                            </span>
                          )}
                      </div>
                      <div className="row">
                        <button
                          className="btn"
                          type="button"
                          onClick={() => setSelectedPresetId(preset.id)}
                        >
                          View candidates
                        </button>
                        <button
                          className="btn"
                          type="button"
                          onClick={() => startEditPreset(preset)}
                        >
                          Edit
                        </button>
                        <button
                          className="btn danger"
                          type="button"
                          onClick={() => void handleDeletePreset(preset)}
                        >
                          Delete
                        </button>
                      </div>
                    </>
                  )}
                </div>
              );
            })
          )}
        </div>
        {presetStatus.text && (
          <div className={`status ${presetStatus.kind}`}>
            {presetStatus.text}
          </div>
        )}
      </section>

      <section className="card">
        <h2>Candidates{selectedPreset ? `: ${selectedPreset.name}` : ""}</h2>

        {lastVeto && (
          <div className={styles.undoBanner} role="status">
            <span>
              Marked <strong>{lastVeto.name}</strong> as{" "}
              {lastVeto.kind === "seen" ? "Seen" : "Not for me"}.
            </span>
            <button
              className="btn"
              type="button"
              onClick={() =>
                void handleUndoVeto(lastVeto, lastVeto.previousVeto)
              }
            >
              Undo
            </button>
          </div>
        )}

        {vetoStatus.text && (
          <div className={`status ${vetoStatus.kind}`}>{vetoStatus.text}</div>
        )}

        {!selectedPreset ? (
          <p className={styles.muted}>
            Select or create a preset to view its candidates.
          </p>
        ) : loadingCandidates ? (
          <p className={styles.muted}>Loading candidates...</p>
        ) : candidates.length === 0 ? (
          <p className={styles.muted}>No candidates in this preset yet.</p>
        ) : (
          <div className={styles.candidateGrid}>
            {candidates.map((candidate) => (
              <article
                key={titleKey(candidate)}
                data-title-key={titleKey(candidate)}
                className={
                  candidate.veto_kind
                    ? `${styles.candidateCard} ${styles.candidateVetoed}`
                    : styles.candidateCard
                }
              >
                <img
                  className={styles.poster}
                  src={candidate.poster}
                  alt={candidate.name}
                />
                <div className={styles.candidateBody}>
                  <div className={styles.candidateTitle}>{candidate.name}</div>
                  <div className={styles.candidateSub}>
                    {candidate.year !== null &&
                      candidate.year !== undefined && (
                        <span>{candidate.year}</span>
                      )}
                    {candidate.rating !== null &&
                      candidate.rating !== undefined && (
                        <span>★ {candidate.rating.toFixed(1)}</span>
                      )}
                    {candidate.veto_kind && (
                      <span className={`${styles.badge} ${styles.badgeVeto}`}>
                        {candidate.veto_kind === "seen" ? "Seen" : "Not for me"}
                      </span>
                    )}
                  </div>
                  {(candidate.blurb || candidate.description) && (
                    <p className={styles.candidateDesc}>
                      {candidate.blurb || candidate.description}
                    </p>
                  )}
                  <div className={styles.candidateActions}>
                    <button
                      className="btn"
                      type="button"
                      onClick={() => void handleVeto(candidate, "seen")}
                    >
                      Seen
                    </button>
                    <button
                      className="btn"
                      type="button"
                      onClick={() => void handleVeto(candidate, "not_for_me")}
                    >
                      Not for me
                    </button>
                    {candidate.veto_kind && (
                      <button
                        className="btn"
                        type="button"
                        onClick={() => void handleUndoVeto(candidate)}
                      >
                        Undo
                      </button>
                    )}
                  </div>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
