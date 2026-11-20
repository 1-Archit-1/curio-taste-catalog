CREATE TABLE users (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username             text NOT NULL UNIQUE,
    addon_token          text NOT NULL UNIQUE,
    llm_provider         text NOT NULL DEFAULT 'gemini'
                         CHECK (llm_provider IN ('gemini', 'openai', 'anthropic', 'openrouter')),
    monthly_token_budget bigint CHECK (monthly_token_budget IS NULL OR monthly_token_budget > 0),
    jobs_paused_reason   text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE login_tokens (
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at   timestamptz,
    PRIMARY KEY (user_id, token_hash)
);

CREATE UNIQUE INDEX idx_login_tokens_hash ON login_tokens (token_hash);

CREATE TABLE sessions (
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    PRIMARY KEY (user_id, token_hash)
);

CREATE UNIQUE INDEX idx_sessions_hash ON sessions (token_hash);
CREATE INDEX idx_sessions_expires_at ON sessions (expires_at);

CREATE TABLE user_api_keys (
    user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_type      text NOT NULL
                  CHECK (key_type IN ('tmdb', 'gemini', 'openai', 'anthropic', 'openrouter')),
    encrypted_key bytea NOT NULL,
    last4         text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key_type)
);

CREATE TABLE key_decrypt_audit (
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    id         bigint GENERATED ALWAYS AS IDENTITY,
    key_type   text NOT NULL
               CHECK (key_type IN ('tmdb', 'gemini', 'openai', 'anthropic', 'openrouter')),
    job_id     text,
    reason     text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, id)
);

CREATE INDEX idx_key_decrypt_audit_user_created
    ON key_decrypt_audit (user_id, created_at DESC, id DESC);
