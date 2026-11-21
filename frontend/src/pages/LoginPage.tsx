import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router";
import { useAuth } from "../auth/useAuth";
import styles from "./LoginPage.module.css";

export function LoginPage() {
  const { user, loading, login } = useAuth();
  const navigate = useNavigate();
  const [token, setToken] = useState("");
  const [status, setStatus] = useState<{ text: string; kind: "" | "err" }>({
    text: "",
    kind: "",
  });
  const [submitting, setSubmitting] = useState(false);

  if (!loading && user) {
    return <Navigate to="/settings" replace />;
  }

  async function handleSubmit(ev: FormEvent<HTMLFormElement>) {
    ev.preventDefault();
    const trimmed = token.trim();
    if (!trimmed) {
      setStatus({ text: "Enter a login token.", kind: "err" });
      return;
    }

    setSubmitting(true);
    setStatus({ text: "Signing in...", kind: "" });
    try {
      const result = await login(trimmed);
      if (result.ok) {
        setToken("");
        setStatus({ text: "", kind: "" });
        navigate("/settings", { replace: true });
      } else {
        setStatus({ text: result.detail, kind: "err" });
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="container">
      <header className={styles.header}>
        <h1 className={styles.title}>Curio</h1>
      </header>

      <section className={`card ${styles.loginBox}`}>
        <h2>Sign in</h2>
        <p className="subtitle">Paste the login token issued by your instance operator.</p>
        <form onSubmit={(ev) => void handleSubmit(ev)}>
          <div className="row">
            <input
              aria-label="Login token"
              className="grow mono"
              type="password"
              placeholder="Login token"
              autoComplete="off"
              required
              value={token}
              onChange={(ev) => setToken(ev.target.value)}
              disabled={submitting}
            />
            <button className="btn primary" type="submit" disabled={submitting}>
              Sign in
            </button>
          </div>
          <div
            role="status"
            className={`status${status.kind ? ` ${status.kind}` : ""}`}
          >
            {status.text}
          </div>
        </form>
      </section>
    </div>
  );
}
