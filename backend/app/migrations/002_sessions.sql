-- Server-side sessions. The browser only holds an HttpOnly cookie with a random session token;
-- the table stores a SHA-256 hash of it, never the token itself.

CREATE TABLE sessions (
    id                   text PRIMARY KEY,               -- sha256(token), hex
    user_id              text NOT NULL REFERENCES users (user_id),
    auth_source          text NOT NULL CHECK (auth_source IN ('local', 'keycloak')),
    expires_at           timestamptz NOT NULL,           -- absolute limit
    idle_expires_at      timestamptz NOT NULL,           -- pushed forward while the session is used
    last_seen_at         timestamptz NOT NULL DEFAULT now(),
    revoked_at           timestamptz,
    revoked_reason       text,
    ip_address           text,
    user_agent           text,
    -- Keycloak sessions: tokens kept server-side, encrypted with SECRET_KEY
    kc_refresh_token     text,
    kc_id_token          text,
    kc_access_expires_at timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    modified_by          text NOT NULL
);
CREATE INDEX sessions_user_idx ON sessions (user_id) WHERE revoked_at IS NULL;
CREATE TRIGGER sessions_set_updated_at BEFORE UPDATE ON sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at('last_seen_at', 'idle_expires_at');

-- Pending Keycloak sign-ins (OIDC state, nonce and PKCE verifier), valid for a few minutes.
CREATE TABLE oidc_login_requests (
    state          text PRIMARY KEY,
    code_verifier  text NOT NULL,
    nonce          text NOT NULL,
    next_path      text NOT NULL DEFAULT '/workspace',
    expires_at     timestamptz NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    modified_by    text NOT NULL
);
CREATE TRIGGER oidc_login_requests_set_updated_at BEFORE UPDATE ON oidc_login_requests
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
