import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { apiClient, extractErrorDetail } from "../api/client";
import type { components } from "../api/schema";
import styles from "./SettingsPage.module.css";

type KeyType = components["schemas"]["KeyType"];
type SettingsResponse = components["schemas"]["SettingsResponse"];
type AuditEntry = components["schemas"]["AuditEntryResponse"];

interface StatusMessage {
  text: string;
  kind: "" | "ok" | "err";
}

interface ProviderMeta {
  id: KeyType;
  label: string;
  placeholder: string;
}

const PROVIDERS: readonly ProviderMeta[] = [
  {
    id: "tmdb",
    label: "TMDB API Key",
    placeholder: "TMDB v3 API key or v4 Read Access Token",
  },
  {
    id: "gemini",
    label: "Gemini API Key",
    placeholder: "AIza... (dedicated, restricted to Gemini API with quota cap)",
  },
  {
    id: "openai",
    label: "OpenAI API Key",
    placeholder: "sk-... (dedicated key with spend cap)",
  },
  {
    id: "anthropic",
    label: "Anthropic API Key",
    placeholder: "sk-ant-... (dedicated key with spend cap)",
  },
  {
    id: "openrouter",
    label: "OpenRouter API Key",
    placeholder: "sk-or-... (dedicated key with credit limit)",
  },
];

const PROVIDER_DISPLAY_NAMES: Record<KeyType, string> = {
  tmdb: "TMDB",
  gemini: "Gemini",
  openai: "OpenAI",
  anthropic: "Anthropic",
  openrouter: "OpenRouter",
  // Shown in the decrypt audit: the worker opened a Simkl token. It has no key field here.
  simkl: "Simkl",
};

function formatUtcTimestamp(iso: string): string {
  return `${new Date(iso).toISOString().replace("T", " ").slice(0, 19)}Z`;
}

export function SettingsPage() {
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [auditEntries, setAuditEntries] = useState<AuditEntry[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [llmProvider, setLlmProvider] = useState<KeyType>("gemini");
  const [monthlyBudget, setMonthlyBudget] = useState("");
  const [manifestStatus, setManifestStatus] = useState<StatusMessage>({
    text: "",
    kind: "",
  });
  const [prefsStatus, setPrefsStatus] = useState<StatusMessage>({
    text: "",
    kind: "",
  });

  // Submitted API keys live only in ephemeral component state until the request is sent.
  const [keyInputs, setKeyInputs] = useState<Partial<Record<KeyType, string>>>(
    {},
  );
  const [keyStatuses, setKeyStatuses] = useState<
    Partial<Record<KeyType, StatusMessage>>
  >({});
  const [busyKey, setBusyKey] = useState<KeyType | null>(null);

  const manifestInputRef = useRef<HTMLInputElement | null>(null);

  const loadSettings = useCallback(async () => {
    const { data, error, response } = await apiClient.GET("/api/settings");
    if (response.ok && data) {
      setSettings(data);
      setLlmProvider(data.llm_provider);
      setMonthlyBudget(
        data.monthly_token_budget !== null &&
          data.monthly_token_budget !== undefined
          ? String(data.monthly_token_budget)
          : "",
      );
      setLoadError(null);
    } else if (response.status !== 401) {
      setLoadError(extractErrorDetail(error, "Could not load settings."));
    }
  }, []);

  const loadAudit = useCallback(async () => {
    const { data, response } = await apiClient.GET("/api/settings/audit");
    if (response.ok && data) {
      setAuditEntries(data.entries);
    }
  }, []);

  useEffect(() => {
    void Promise.all([loadSettings(), loadAudit()]);
  }, [loadSettings, loadAudit]);

  async function handleCopyManifest() {
    if (!settings) return;
    try {
      await navigator.clipboard.writeText(settings.addon_manifest_url);
      setManifestStatus({
        text: "Copied manifest URL to clipboard.",
        kind: "ok",
      });
    } catch {
      manifestInputRef.current?.select();
      setManifestStatus({
        text: "Selected URL — press Ctrl/Cmd+C to copy.",
        kind: "ok",
      });
    }
  }

  async function handleRotateManifest() {
    if (
      !window.confirm(
        "Rotate your addon URL? You will need to reinstall the new URL in Stremio/Nuvio.",
      )
    ) {
      return;
    }
    const { data, error, response } = await apiClient.POST(
      "/api/settings/addon-token/rotate",
    );
    if (response.ok && data) {
      setSettings((prev) =>
        prev ? { ...prev, addon_manifest_url: data.addon_manifest_url } : prev,
      );
      setManifestStatus({ text: "Addon URL rotated.", kind: "ok" });
    } else if (response.status !== 401) {
      setManifestStatus({
        text: extractErrorDetail(error, "Could not rotate URL."),
        kind: "err",
      });
    }
  }

  async function handleSavePreferences(ev: FormEvent<HTMLFormElement>) {
    ev.preventDefault();
    const trimmedBudget = monthlyBudget.trim();
    const parsedBudget = trimmedBudget ? Number(trimmedBudget) : null;

    const { data, error, response } = await apiClient.PATCH("/api/settings", {
      body: {
        llm_provider: llmProvider,
        monthly_token_budget: parsedBudget,
      },
    });
    if (response.ok && data) {
      setSettings(data);
      setPrefsStatus({ text: "Preferences saved.", kind: "ok" });
    } else if (response.status !== 401) {
      setPrefsStatus({
        text: extractErrorDetail(error, "Could not save preferences."),
        kind: "err",
      });
    }
  }

  function setKeyStatus(keyType: KeyType, msg: StatusMessage) {
    setKeyStatuses((prev) => ({ ...prev, [keyType]: msg }));
  }

  async function handleTestKey(keyType: KeyType) {
    const val = (keyInputs[keyType] ?? "").trim();
    if (!val) {
      setKeyStatus(keyType, { text: "Enter a key to test.", kind: "err" });
      return;
    }
    setBusyKey(keyType);
    setKeyStatus(keyType, { text: "Testing...", kind: "" });
    try {
      const { data, error, response } = await apiClient.POST(
        "/api/settings/keys/{key_type}/test",
        {
          params: { path: { key_type: keyType } },
          body: { api_key: val },
        },
      );
      if (response.ok && data) {
        setKeyStatus(keyType, {
          text:
            data.detail || (data.ok ? "Key is valid." : "Validation failed."),
          kind: data.ok ? "ok" : "err",
        });
      } else if (response.status !== 401) {
        setKeyStatus(keyType, {
          text: extractErrorDetail(error, "Validation failed."),
          kind: "err",
        });
      }
    } finally {
      setBusyKey(null);
    }
  }

  async function handleSaveKey(keyType: KeyType) {
    const val = (keyInputs[keyType] ?? "").trim();
    if (!val) {
      setKeyStatus(keyType, { text: "Enter a key to save.", kind: "err" });
      return;
    }
    setBusyKey(keyType);
    setKeyStatus(keyType, { text: "Testing and encrypting...", kind: "" });
    try {
      const { data, error, response } = await apiClient.PUT(
        "/api/settings/keys/{key_type}",
        {
          params: { path: { key_type: keyType } },
          body: { api_key: val },
        },
      );
      if (response.ok && data) {
        setKeyInputs((prev) => ({ ...prev, [keyType]: "" }));
        setKeyStatus(keyType, {
          text: `Saved (••••${data.last4}).`,
          kind: "ok",
        });
        await loadSettings();
      } else if (response.status !== 401) {
        setKeyStatus(keyType, {
          text: extractErrorDetail(error, "Could not save key."),
          kind: "err",
        });
      }
    } finally {
      setBusyKey(null);
    }
  }

  async function handleDeleteKey(keyType: KeyType) {
    setBusyKey(keyType);
    try {
      const { error, response } = await apiClient.DELETE(
        "/api/settings/keys/{key_type}",
        {
          params: { path: { key_type: keyType } },
        },
      );
      if (response.ok) {
        setKeyStatus(keyType, { text: "Key removed.", kind: "ok" });
        await loadSettings();
      } else if (response.status !== 401) {
        setKeyStatus(keyType, {
          text: extractErrorDetail(error, "Could not remove key."),
          kind: "err",
        });
      }
    } finally {
      setBusyKey(null);
    }
  }

  if (loadError) {
    return (
      <section className="card">
        <div className="notice notice-err">{loadError}</div>
      </section>
    );
  }

  if (!settings) {
    return (
      <section className="card">
        <p className="subtitle">Loading settings...</p>
      </section>
    );
  }

  return (
    <section aria-label="Settings">
      {settings.jobs_paused_reason && (
        <div className="notice notice-err" role="alert">
          <strong>Some background runs are paused:</strong>{" "}
          {settings.jobs_paused_reason}
        </div>
      )}

      <div className="card">
        <h2>Addon Manifest URL</h2>
        <p className="subtitle">
          Install this secret URL in Stremio or Nuvio. Anyone with this URL can
          read your catalogs; rotate it if leaked.
        </p>
        <div className="row">
          <input
            ref={manifestInputRef}
            aria-label="Addon Manifest URL"
            className="grow mono"
            type="text"
            readOnly
            value={settings.addon_manifest_url}
          />
          <button
            className="btn primary"
            type="button"
            onClick={() => void handleCopyManifest()}
          >
            Copy URL
          </button>
          <button
            className="btn danger"
            type="button"
            onClick={() => void handleRotateManifest()}
          >
            Rotate URL
          </button>
        </div>
        <div
          className={`status${manifestStatus.kind ? ` ${manifestStatus.kind}` : ""}`}
        >
          {manifestStatus.text}
        </div>
      </div>

      <div className="card">
        <h2>LLM Preferences &amp; Usage Controls</h2>
        <p className="subtitle">
          Choose which configured LLM provider background generation should use,
          and optionally cap monthly token usage.
        </p>
        <form className="row" onSubmit={(ev) => void handleSavePreferences(ev)}>
          <label className="field">
            Active LLM provider
            <select
              value={llmProvider}
              onChange={(ev) => setLlmProvider(ev.target.value as KeyType)}
            >
              {settings.supported_llm_providers.map((provider) => (
                <option key={provider} value={provider}>
                  {PROVIDER_DISPLAY_NAMES[provider] ?? provider}
                </option>
              ))}
            </select>
          </label>
          <label className="field grow">
            Monthly token budget (optional)
            <input
              type="number"
              min={1}
              step={1}
              placeholder="No cap"
              value={monthlyBudget}
              onChange={(ev) => setMonthlyBudget(ev.target.value)}
            />
          </label>
          <button className={`btn primary ${styles.alignEnd}`} type="submit">
            Save Preferences
          </button>
        </form>
        <div
          className={`status${prefsStatus.kind ? ` ${prefsStatus.kind}` : ""}`}
        >
          {prefsStatus.text}
        </div>
      </div>

      <div className="card">
        <h2>API Keys</h2>
        <div className="notice">
          <strong>Trust &amp; key safety notice:</strong> Stored API keys are
          encrypted with a public key so the API process cannot read them, and
          decrypted only by background workers during generation. However,{" "}
          <strong>
            the server operator can technically access stored keys
          </strong>{" "}
          (you can self-host Curio from the public repository if you prefer not
          to share keys). We strongly advise creating a{" "}
          <strong>
            dedicated Gemini API key restricted to the Gemini API with a quota
            cap
          </strong>{" "}
          in Google Cloud — and similarly using dedicated, spend-capped keys for
          OpenAI, Anthropic, or OpenRouter.
        </div>

        <div>
          {PROVIDERS.map((provider) => {
            const info = settings.keys[provider.id] ?? {
              configured: false,
              last4: null,
              updated_at: null,
            };
            const rowStatus = keyStatuses[provider.id] ?? {
              text: "",
              kind: "",
            };
            const isBusy = busyKey === provider.id;

            return (
              <div
                key={provider.id}
                className={styles.keyItem}
                data-key-type={provider.id}
              >
                <div className={styles.keyHeader}>
                  <span className={styles.keyLabel}>
                    {provider.label}{" "}
                    {info.configured ? (
                      <span className={`${styles.badge} ${styles.badgeOk}`}>
                        {`••••${info.last4 ?? ""}`}
                      </span>
                    ) : (
                      <span className={styles.badge}>Not set</span>
                    )}
                  </span>
                  <span className={styles.keyMeta}>
                    {info.updated_at
                      ? `Updated ${formatUtcTimestamp(info.updated_at)}`
                      : ""}
                  </span>
                </div>
                <div className="row">
                  <input
                    aria-label={provider.label}
                    type="password"
                    className="grow mono"
                    placeholder={provider.placeholder}
                    autoComplete="off"
                    value={keyInputs[provider.id] ?? ""}
                    onChange={(ev) =>
                      setKeyInputs((prev) => ({
                        ...prev,
                        [provider.id]: ev.target.value,
                      }))
                    }
                    disabled={isBusy}
                  />
                  <button
                    type="button"
                    className="btn"
                    disabled={isBusy}
                    onClick={() => void handleTestKey(provider.id)}
                  >
                    Test
                  </button>
                  <button
                    type="button"
                    className="btn primary"
                    disabled={isBusy}
                    onClick={() => void handleSaveKey(provider.id)}
                  >
                    Save
                  </button>
                  {info.configured && (
                    <button
                      type="button"
                      className="btn danger"
                      disabled={isBusy}
                      onClick={() => void handleDeleteKey(provider.id)}
                    >
                      Remove
                    </button>
                  )}
                </div>
                <div
                  className={`status${rowStatus.kind ? ` ${rowStatus.kind}` : ""}`}
                >
                  {rowStatus.text}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="card">
        <div className={styles.auditHeader}>
          <h2>Key Access Audit Log</h2>
          <button
            className="btn"
            type="button"
            onClick={() => void loadAudit()}
          >
            Refresh
          </button>
        </div>
        <p className="subtitle">
          Every time a worker decrypts one of your keys, an entry is recorded
          here.
        </p>
        <table>
          <thead>
            <tr>
              <th>Time (UTC)</th>
              <th>Key</th>
              <th>Job</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {auditEntries.length === 0 ? (
              <tr>
                <td colSpan={4} className={styles.mutedCell}>
                  No decryptions recorded yet.
                </td>
              </tr>
            ) : (
              auditEntries.map((entry) => (
                <tr key={entry.id}>
                  <td className="mono">
                    {formatUtcTimestamp(entry.created_at)}
                  </td>
                  <td className="mono">{entry.key_type}</td>
                  <td className="mono">{entry.job_id ?? "—"}</td>
                  <td>{entry.reason}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
