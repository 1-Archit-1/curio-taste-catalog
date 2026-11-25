import { type FormEvent, useCallback, useEffect, useState } from "react";
import { apiClient, extractErrorDetail } from "../api/client";
import type { components } from "../api/schema";
import styles from "./SourcesPage.module.css";

type SourceOut = components["schemas"]["SourceOut"];
type SourceRole = SourceOut["role"];
type SourcePreviewResponse = components["schemas"]["SourcePreviewResponse"];
type ImportIssueOut = components["schemas"]["ImportIssueOut"];
type JobOut = components["schemas"]["JobOut"];
type MediaType = ImportIssueOut["candidate_matches"][number]["media_type"];

const ROLE_OPTIONS: Array<{ value: SourceRole; label: string }> = [
  { value: "self", label: "Self (my history)" },
  { value: "taste", label: "Taste (friend / inspiration)" },
  { value: "candidates", label: "Candidates (pool)" },
];

const KIND_LABELS: Record<SourceOut["kind"], string> = {
  lb_diary_rss: "Diary RSS",
  lb_profile_csv: "Profile ZIP",
  lb_list_csv: "List CSV",
};

/** How often the page asks for an import job's status. */
const JOB_POLL_INTERVAL_MS = 2000;
const TERMINAL_JOB_STATUSES = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "paused",
]);

interface ImportProgress {
  jobId: string;
  /** Latest step message, or a placeholder until the worker reports one. */
  message: string;
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "Never";
  return new Date(iso).toLocaleString();
}

/** Ratings are stored on a 0-10 scale; Letterboxd shows stars out of 5. */
function formatStars(rating: number): string {
  return `★ ${rating / 2}`;
}

function looksLikeLetterboxdUrl(value: string): boolean {
  const trimmed = value.trim().toLowerCase();
  return trimmed.includes("letterboxd.com/");
}

export function SourcesPage() {
  const [sources, setSources] = useState<SourceOut[]>([]);
  const [issues, setIssues] = useState<ImportIssueOut[]>([]);
  const [pendingCount, setPendingCount] = useState<number>(0);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<{
    kind: "ok" | "err";
    text: string;
  } | null>(null);

  const [sourceInput, setSourceInput] = useState("");
  const [addRole, setAddRole] = useState<SourceRole>("taste");
  const [preview, setPreview] = useState<SourcePreviewResponse | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [adding, setAdding] = useState(false);

  const [zipFile, setZipFile] = useState<File | null>(null);
  const [zipRole, setZipRole] = useState<SourceRole>("self");
  const [uploadingZip, setUploadingZip] = useState(false);
  const [importProgress, setImportProgress] = useState<ImportProgress | null>(
    null,
  );

  const [busySourceId, setBusySourceId] = useState<string | null>(null);
  const [busyIssueId, setBusyIssueId] = useState<string | null>(null);
  const [manualTmdbIds, setManualTmdbIds] = useState<Record<string, string>>(
    {},
  );
  const [manualMediaTypes, setManualMediaTypes] = useState<
    Record<string, MediaType>
  >({});
  const importJobId = importProgress?.jobId ?? null;

  const loadAll = useCallback(async () => {
    const [sourcesRes, issuesRes] = await Promise.all([
      apiClient.GET("/api/sources"),
      apiClient.GET("/api/import-issues", {
        params: { query: { status: "pending" } },
      }),
    ]);

    if (sourcesRes.data) {
      setSources(sourcesRes.data.sources);
    } else {
      setNotice({
        kind: "err",
        text: extractErrorDetail(
          sourcesRes.error,
          "Failed to load taste sources.",
        ),
      });
    }

    if (issuesRes.data) {
      setIssues(issuesRes.data.issues);
      setPendingCount(issuesRes.data.pending_count);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  // Follow the import job until it ends. The worker does the matching, so this can take minutes.
  useEffect(() => {
    if (!importJobId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll() {
      const { data, error } = await apiClient.GET("/api/jobs/{job_id}", {
        params: { path: { job_id: importJobId as string } },
      });
      if (cancelled) return;
      if (error || !data) {
        setImportProgress(null);
        setNotice({
          kind: "err",
          text: extractErrorDetail(error, "Could not check the import."),
        });
        return;
      }
      const { job } = data;
      const events = data.events ?? [];
      if (!TERMINAL_JOB_STATUSES.has(job.status)) {
        const latest = events[events.length - 1];
        setImportProgress({
          jobId: job.id,
          message: latest?.message ?? "Waiting for a worker...",
        });
        timer = setTimeout(() => void poll(), JOB_POLL_INTERVAL_MS);
        return;
      }
      setImportProgress(null);
      if (job.status === "succeeded") {
        const counts = events.find((e) => e.step === "import")?.payload as
          | { matched_count?: number; issue_count?: number }
          | null
          | undefined;
        setNotice({
          kind: "ok",
          text: `Imported ${counts?.matched_count ?? 0} items (${counts?.issue_count ?? 0} issues to review).`,
        });
      } else {
        const reason = job.paused_reason ?? job.last_error;
        const outcome = job.status === "paused" ? "paused" : "failed";
        setNotice({
          kind: "err",
          text: reason
            ? `Import ${outcome}: ${reason}`
            : `Import ${job.status}.`,
        });
      }
      await loadAll();
    }

    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [importJobId, loadAll]);

  async function runPreview(inputText: string) {
    const cleaned = inputText.trim();
    if (!cleaned) return;
    setPreviewing(true);
    setNotice(null);
    const { data, error } = await apiClient.POST("/api/sources/preview", {
      body: { input: cleaned },
    });
    setPreviewing(false);
    if (error || !data) {
      setPreview(null);
      setNotice({
        kind: "err",
        text: extractErrorDetail(error, "Could not preview Letterboxd feed."),
      });
      return;
    }
    setPreview(data);
  }

  async function handlePreviewSubmit(e: FormEvent) {
    e.preventDefault();
    await runPreview(sourceInput);
  }

  async function handleAddSource() {
    const cleaned = sourceInput.trim();
    if (!cleaned) return;
    setAdding(true);
    setNotice(null);
    const { data, error } = await apiClient.POST("/api/sources", {
      body: { input: cleaned, role: addRole },
    });
    setAdding(false);
    if (error || !data) {
      setNotice({
        kind: "err",
        text: extractErrorDetail(error, "Failed to add Letterboxd source."),
      });
      return;
    }
    setSourceInput("");
    setPreview(null);
    setNotice({
      kind: "ok",
      text: `Added ${data.label} (${data.item_count} items).`,
    });
    await loadAll();
  }

  async function handleRoleChange(sourceId: string, role: SourceRole) {
    setBusySourceId(sourceId);
    setNotice(null);
    const { data, error } = await apiClient.PATCH("/api/sources/{source_id}", {
      params: { path: { source_id: sourceId } },
      body: { role },
    });
    setBusySourceId(null);
    if (error || !data) {
      setNotice({
        kind: "err",
        text: extractErrorDetail(error, "Failed to update source role."),
      });
      return;
    }
    setSources((prev) => prev.map((s) => (s.id === sourceId ? data : s)));
  }

  async function handleRefresh(sourceId: string) {
    setBusySourceId(sourceId);
    setNotice(null);
    const { data, error } = await apiClient.POST(
      "/api/sources/{source_id}/refresh",
      {
        params: { path: { source_id: sourceId } },
      },
    );
    setBusySourceId(null);
    if (error || !data) {
      setNotice({
        kind: "err",
        text: extractErrorDetail(error, "Failed to refresh source."),
      });
      await loadAll();
      return;
    }
    setSources((prev) => prev.map((s) => (s.id === sourceId ? data : s)));
    setNotice({
      kind: "ok",
      text: `Refreshed ${data.label} (${data.item_count} items).`,
    });
  }

  async function handleRemove(sourceId: string) {
    setBusySourceId(sourceId);
    setNotice(null);
    const { error } = await apiClient.DELETE("/api/sources/{source_id}", {
      params: { path: { source_id: sourceId } },
    });
    setBusySourceId(null);
    if (error) {
      setNotice({
        kind: "err",
        text: extractErrorDetail(error, "Failed to remove source."),
      });
      return;
    }
    setSources((prev) => prev.filter((s) => s.id !== sourceId));
    setNotice({ kind: "ok", text: "Source removed." });
  }

  async function handleZipImport(e: FormEvent) {
    e.preventDefault();
    if (!zipFile) return;
    setUploadingZip(true);
    setNotice(null);
    const formData = new FormData();
    formData.append("file", zipFile);
    formData.append("role", zipRole);
    const response = await fetch("/api/sources/import-zip", {
      method: "POST",
      body: formData,
      credentials: "same-origin",
    });
    if (!response.ok) {
      setUploadingZip(false);
      let detail = "Failed to import Letterboxd ZIP.";
      try {
        const errBody = (await response.json()) as unknown;
        detail = extractErrorDetail(errBody, detail);
      } catch {
        // Ignore JSON parse errors on non-JSON error responses.
      }
      setNotice({ kind: "err", text: detail });
      return;
    }
    const job = (await response.json()) as JobOut;
    setUploadingZip(false);
    setZipFile(null);
    setImportProgress({ jobId: job.id, message: "Waiting for a worker..." });
  }

  async function handleResolveIssue(
    issueId: string,
    tmdbId: number,
    mediaType: MediaType,
  ) {
    if (!Number.isInteger(tmdbId) || tmdbId < 1) {
      setNotice({ kind: "err", text: "Enter a valid positive TMDB ID." });
      return;
    }
    setBusyIssueId(issueId);
    setNotice(null);
    const { error } = await apiClient.POST(
      "/api/import-issues/{issue_id}/resolve",
      {
        params: { path: { issue_id: issueId } },
        body: { media_type: mediaType, tmdb_id: tmdbId },
      },
    );
    setBusyIssueId(null);
    if (error) {
      setNotice({
        kind: "err",
        text: extractErrorDetail(error, "Failed to resolve import issue."),
      });
      return;
    }
    setNotice({ kind: "ok", text: `Resolved to TMDB #${tmdbId}.` });
    await loadAll();
  }

  async function handleDismissIssue(issueId: string) {
    setBusyIssueId(issueId);
    setNotice(null);
    const { error } = await apiClient.POST(
      "/api/import-issues/{issue_id}/dismiss",
      {
        params: { path: { issue_id: issueId } },
      },
    );
    setBusyIssueId(null);
    if (error) {
      setNotice({
        kind: "err",
        text: extractErrorDetail(error, "Failed to dismiss import issue."),
      });
      return;
    }
    await loadAll();
  }

  return (
    <div>
      {notice && (
        <div
          className={notice.kind === "err" ? "notice notice-err" : "notice"}
          role="status"
        >
          {notice.text}
        </div>
      )}

      <section className="card">
        <h2>Add Letterboxd RSS Source</h2>
        <p className="subtitle">
          Paste a Letterboxd profile URL or username to preview recent diary
          entries and subscribe.
        </p>
        <form className="row" onSubmit={(e) => void handlePreviewSubmit(e)}>
          <input
            type="text"
            className="grow"
            aria-label="Letterboxd profile URL or username"
            placeholder="https://letterboxd.com/alice/ or alice"
            value={sourceInput}
            onChange={(e) => {
              const next = e.target.value;
              setSourceInput(next);
              if (looksLikeLetterboxdUrl(next) && !previewing) {
                void runPreview(next);
              }
            }}
          />
          <button
            type="submit"
            className="btn"
            disabled={previewing || !sourceInput.trim()}
          >
            {previewing ? "Previewing..." : "Preview"}
          </button>
        </form>

        {preview && (
          <div className={styles.previewBox}>
            <div className="row">
              <strong>{preview.label}</strong>
              <span className={styles.kindBadge}>
                {preview.entry_count} recent entries
              </span>
            </div>
            {preview.recent_titles.length > 0 && (
              <ul className={styles.previewList}>
                {preview.recent_titles.map((item) => (
                  <li
                    key={`${item.tmdb_id ?? item.title}-${item.year ?? ""}-${item.watched_at ?? ""}`}
                  >
                    {item.title}
                    {item.year ? ` (${item.year})` : ""}
                    {item.rating !== null && item.rating !== undefined
                      ? ` — ${formatStars(item.rating)}`
                      : ""}
                  </li>
                ))}
              </ul>
            )}
            <div className="row">
              <label htmlFor="add-rss-role">Role</label>
              <select
                id="add-rss-role"
                aria-label="New source role"
                value={addRole}
                onChange={(e) => setAddRole(e.target.value as SourceRole)}
              >
                {ROLE_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn btn-primary"
                disabled={adding}
                onClick={() => void handleAddSource()}
              >
                {adding ? "Adding..." : "Add source"}
              </button>
            </div>
          </div>
        )}
      </section>

      <section className="card">
        <h2>Import Letterboxd Export ZIP</h2>
        <p className="subtitle">
          Upload the ZIP downloaded from your Letterboxd account settings
          (watched, ratings, diary, watchlist, and lists). It is matched in the
          background and needs your TMDB key. Importing again replaces what the
          earlier export added.
        </p>
        <form className="row" onSubmit={(e) => void handleZipImport(e)}>
          <input
            type="file"
            accept=".zip,application/zip"
            aria-label="Letterboxd export ZIP file"
            onChange={(e) => setZipFile(e.target.files?.[0] ?? null)}
          />
          <select
            aria-label="Export ZIP role"
            value={zipRole}
            onChange={(e) => setZipRole(e.target.value as SourceRole)}
          >
            {ROLE_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={!zipFile || uploadingZip || importJobId !== null}
          >
            {uploadingZip ? "Uploading..." : "Import ZIP"}
          </button>
        </form>
        {importProgress && (
          <p className="subtitle" role="status">
            Importing: {importProgress.message}
          </p>
        )}
      </section>

      <section className="card">
        <h2>Your Taste Sources</h2>
        {loading ? (
          <p className="subtitle">Loading sources...</p>
        ) : sources.length === 0 ? (
          <p className={styles.emptyState}>
            No taste sources yet. Add a Letterboxd RSS feed or upload an export
            ZIP above.
          </p>
        ) : (
          <div>
            {sources.map((src) => (
              <div key={src.id} className={styles.sourceRow}>
                <div className={styles.sourceInfo}>
                  <div className={styles.sourceTitleRow}>
                    <strong>{src.label}</strong>
                    <span className={styles.kindBadge}>
                      {KIND_LABELS[src.kind]}
                    </span>
                  </div>
                  <div className={styles.sourceMeta}>
                    <span>{src.item_count} items</span>
                    <span>
                      {src.kind === "lb_diary_rss"
                        ? "Refreshed: "
                        : "Imported: "}
                      {formatDate(src.last_refreshed_at)}
                    </span>
                  </div>
                  {src.last_error && (
                    <span className={styles.errorText}>{src.last_error}</span>
                  )}
                </div>

                <div className={styles.sourceActions}>
                  <select
                    aria-label={`Role for ${src.label}`}
                    value={src.role}
                    disabled={busySourceId === src.id}
                    onChange={(e) =>
                      void handleRoleChange(
                        src.id,
                        e.target.value as SourceRole,
                      )
                    }
                  >
                    {ROLE_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                  {src.kind === "lb_diary_rss" && (
                    <button
                      type="button"
                      className="btn"
                      disabled={busySourceId === src.id}
                      onClick={() => void handleRefresh(src.id)}
                    >
                      Refresh
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn btn-danger"
                    disabled={busySourceId === src.id}
                    onClick={() => void handleRemove(src.id)}
                  >
                    Remove
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="card">
        <h2>Import Issues ({pendingCount})</h2>
        <p className="subtitle">
          Low-confidence or unmatched export rows. Pick a TMDB candidate, enter
          a TMDB ID, or dismiss.
        </p>
        {issues.length === 0 ? (
          <p className={styles.emptyState}>No pending import issues.</p>
        ) : (
          <div>
            {issues.map((issue) => (
              <div key={issue.id} className={styles.issueCard}>
                <div className={styles.issueHeader}>
                  <div>
                    <strong>{issue.raw_title}</strong>
                    {issue.raw_year ? ` (${issue.raw_year})` : ""}
                    <span
                      className={styles.kindBadge}
                      style={{ marginLeft: 8 }}
                    >
                      {issue.item_kind}
                    </span>
                  </div>
                  <span className={styles.sourceMeta}>
                    {issue.confidence !== null && issue.confidence !== undefined
                      ? `Confidence: ${Math.round(issue.confidence * 100)}%`
                      : "Unmatched"}
                  </span>
                </div>

                {issue.candidate_matches.length > 0 && (
                  <div className={styles.candidateList}>
                    {issue.candidate_matches.map((cand) => (
                      <button
                        key={`${cand.media_type}-${cand.tmdb_id}`}
                        type="button"
                        className="btn"
                        disabled={busyIssueId === issue.id}
                        onClick={() =>
                          void handleResolveIssue(
                            issue.id,
                            cand.tmdb_id,
                            cand.media_type,
                          )
                        }
                      >
                        Pick: {cand.name}
                        {cand.year ? ` (${cand.year})` : ""} — #{cand.tmdb_id}
                      </button>
                    ))}
                  </div>
                )}

                <div className={styles.manualResolve}>
                  <input
                    type="number"
                    min={1}
                    className={styles.tmdbInput}
                    aria-label={`TMDB ID for ${issue.raw_title}`}
                    placeholder="TMDB ID"
                    value={manualTmdbIds[issue.id] ?? ""}
                    onChange={(e) =>
                      setManualTmdbIds((prev) => ({
                        ...prev,
                        [issue.id]: e.target.value,
                      }))
                    }
                  />
                  <select
                    aria-label={`Type for ${issue.raw_title}`}
                    value={manualMediaTypes[issue.id] ?? "movie"}
                    onChange={(e) =>
                      setManualMediaTypes((prev) => ({
                        ...prev,
                        [issue.id]: e.target.value as MediaType,
                      }))
                    }
                  >
                    <option value="movie">Movie</option>
                    <option value="series">Series</option>
                  </select>
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={
                      busyIssueId === issue.id ||
                      !(manualTmdbIds[issue.id] ?? "").trim()
                    }
                    onClick={() =>
                      void handleResolveIssue(
                        issue.id,
                        Number(manualTmdbIds[issue.id]),
                        manualMediaTypes[issue.id] ?? "movie",
                      )
                    }
                  >
                    Resolve
                  </button>
                  <button
                    type="button"
                    className="btn"
                    disabled={busyIssueId === issue.id}
                    onClick={() => void handleDismissIssue(issue.id)}
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
