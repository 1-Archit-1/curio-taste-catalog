import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { apiClient, extractErrorDetail } from "../api/client";
import type { components } from "../api/schema";
import styles from "./SourcesPage.module.css";

type SimklStatus = components["schemas"]["SimklStatusResponse"];

export interface Notice {
  kind: "ok" | "err";
  text: string;
}

interface SimklCardProps {
  onNotice: (notice: Notice | null) => void;
  /** Called when a sync, disconnect or connection changes what the sources list shows. */
  onChanged: () => void;
}

/** How often the card asks for a sync job's status. */
const JOB_POLL_INTERVAL_MS = 2000;
const TERMINAL_JOB_STATUSES = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "paused",
]);

/** What `/api/simkl/callback` redirects back with. The page never sees the OAuth `code`. */
const RETURN_MESSAGES: Record<string, Notice> = {
  connected: {
    kind: "ok",
    text: "Simkl connected. Your library is syncing in the background.",
  },
  cancelled: { kind: "err", text: "Simkl sign-in was cancelled." },
  error: { kind: "err", text: "Simkl could not be connected. Try again." },
};

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "Never";
  return new Date(iso).toLocaleString();
}

export function SimklCard({ onNotice, onChanged }: SimklCardProps) {
  const [status, setStatus] = useState<SimklStatus | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [progress, setProgress] = useState("");
  const [searchParams, setSearchParams] = useSearchParams();
  const handledReturn = useRef(false);

  const load = useCallback(async () => {
    const { data, error } = await apiClient.GET("/api/simkl/connection");
    if (error || !data) {
      setLoadFailed(true);
      return;
    }
    setLoadFailed(false);
    setStatus(data);
    // A sync that is queued or running (just connected, or started elsewhere) is followed.
    if (data.sync_job_id) {
      setJobId((current) => current ?? data.sync_job_id ?? null);
      setProgress((current) => current || "Waiting for a worker...");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // The callback redirects here with ?simkl=connected|cancelled|error. Show it once, then take
  // it out of the URL so a reload doesn't repeat it.
  useEffect(() => {
    const outcome = searchParams.get("simkl");
    if (outcome === null || handledReturn.current) return;
    handledReturn.current = true;
    const message = RETURN_MESSAGES[outcome];
    if (message) onNotice(message);
    const next = new URLSearchParams(searchParams);
    next.delete("simkl");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams, onNotice]);

  // Follow the sync job until it ends, the same way the page follows an import.
  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      const { data, error } = await apiClient.GET("/api/jobs/{job_id}", {
        params: { path: { job_id: jobId as string } },
      });
      if (cancelled) return;
      if (error || !data) {
        setJobId(null);
        setProgress("");
        onNotice({
          kind: "err",
          text: extractErrorDetail(error, "Could not check the Simkl sync."),
        });
        return;
      }
      const { job } = data;
      const events = data.events ?? [];
      if (!TERMINAL_JOB_STATUSES.has(job.status)) {
        const latest = events[events.length - 1];
        setProgress(latest?.message ?? "Waiting for a worker...");
        timer = setTimeout(() => void poll(), JOB_POLL_INTERVAL_MS);
        return;
      }
      setJobId(null);
      setProgress("");
      if (job.status === "succeeded") {
        const summary = events.find((e) => e.step === "sync")?.message;
        onNotice({ kind: "ok", text: summary ?? "Simkl synced." });
      } else if (job.status !== "cancelled") {
        const reason = job.paused_reason ?? job.last_error;
        const outcome = job.status === "paused" ? "paused" : "failed";
        onNotice({
          kind: "err",
          text: reason
            ? `Simkl sync ${outcome}: ${reason}`
            : `Simkl sync ${job.status}.`,
        });
      }
      await load();
      onChanged();
    }
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [jobId, load, onChanged, onNotice]);

  async function handleConnect() {
    setBusy(true);
    onNotice(null);
    const { data, error } = await apiClient.POST("/api/simkl/connect");
    if (error || !data) {
      setBusy(false);
      onNotice({
        kind: "err",
        text: extractErrorDetail(error, "Could not start the Simkl sign-in."),
      });
      return;
    }
    // Simkl's consent page is on another site, so this leaves the app.
    window.location.assign(data.authorize_url);
  }

  async function handleSync() {
    setBusy(true);
    onNotice(null);
    const { data, error } = await apiClient.POST("/api/simkl/sync");
    setBusy(false);
    if (error || !data) {
      onNotice({
        kind: "err",
        text: extractErrorDetail(error, "Could not start the Simkl sync."),
      });
      await load();
      return;
    }
    setProgress("Waiting for a worker...");
    setJobId(data.id);
  }

  async function handleDisconnect() {
    if (
      !window.confirm(
        "Disconnect Simkl? Its titles and the watch history they added are removed from Curio.",
      )
    ) {
      return;
    }
    setBusy(true);
    onNotice(null);
    const { error } = await apiClient.DELETE("/api/simkl/connection");
    setBusy(false);
    if (error) {
      onNotice({
        kind: "err",
        text: extractErrorDetail(error, "Could not disconnect Simkl."),
      });
      return;
    }
    setJobId(null);
    setProgress("");
    onNotice({ kind: "ok", text: "Simkl disconnected." });
    await load();
    onChanged();
  }

  const syncing = jobId !== null;
  return (
    <section className="card" aria-label="Simkl">
      <h2>Simkl</h2>
      <p className="subtitle">
        Bring in your movie, series and anime history, ratings and dropped
        shows, including what you watch in Nuvio. Data from{" "}
        <a href="https://simkl.com" target="_blank" rel="noreferrer">
          Simkl
        </a>
        .
      </p>
      {loadFailed && !status && (
        <p className={styles.errorText}>Could not load the Simkl connection.</p>
      )}
      {!status && !loadFailed && <p className="subtitle">Loading Simkl...</p>}
      {status && !status.configured && (
        <p className={styles.emptyState}>
          Simkl isn't set up on this server. The operator needs to register a
          Simkl app and set SIMKL_CLIENT_ID, SIMKL_CLIENT_SECRET and
          PUBLIC_BASE_URL.
        </p>
      )}
      {status?.configured && !status.connected && (
        <div className="row">
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy}
            onClick={() => void handleConnect()}
          >
            Connect Simkl
          </button>
        </div>
      )}
      {status?.configured && status.connected && (
        <div>
          <div className={styles.sourceMeta}>
            <span>
              Connected as{" "}
              <strong>{status.account_name ?? "your account"}</strong>
            </span>
            <span>Last sync: {formatDate(status.last_synced_at)}</span>
          </div>
          {status.needs_reconnect && (
            <p className={styles.errorText} role="alert">
              Simkl no longer accepts the saved connection. Reconnect to keep
              syncing.
            </p>
          )}
          {status.last_error && !status.needs_reconnect && (
            <p className={styles.errorText}>{status.last_error}</p>
          )}
          {syncing && (
            <p className="subtitle" role="status">
              Syncing Simkl: {progress}
            </p>
          )}
          <div className="row">
            {status.needs_reconnect && (
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy}
                onClick={() => void handleConnect()}
              >
                Reconnect Simkl
              </button>
            )}
            <button
              type="button"
              className="btn"
              disabled={busy || syncing || status.needs_reconnect}
              onClick={() => void handleSync()}
            >
              Sync now
            </button>
            <button
              type="button"
              className="btn btn-danger"
              disabled={busy}
              onClick={() => void handleDisconnect()}
            >
              Disconnect
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
