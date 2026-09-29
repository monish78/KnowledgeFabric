// Sign-in: local (our form -> /api/auth/login) or Keycloak (redirect, authorization code + PKCE).
// Tokens live in sessionStorage so closing the tab signs out.
import { sha256 } from "./sha256.js";

const KEY = "graphbase.session";
let config = null;

export async function authConfig() {
  if (!config) config = await (await fetch("/api/auth/config")).json();
  return config;
}

export function session() {
  try {
    return JSON.parse(sessionStorage.getItem(KEY)) || null;
  } catch {
    return null;
  }
}

function save(s) {
  sessionStorage.setItem(KEY, JSON.stringify(s));
}

export function clearSession() {
  sessionStorage.removeItem(KEY);
}

// ---------------------------------------------------------------- local
export async function localLogin(userId, password) {
  const r = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId, password }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || "Sign-in failed");
  save({ provider: "local", access_token: body.access_token, expires_at: Date.now() + body.expires_in * 1000 });
  return body.user;
}

// ---------------------------------------------------------------- keycloak
function b64url(bytes) {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString(n = 48) {
  const bytes = new Uint8Array(n);
  crypto.getRandomValues(bytes);
  return b64url(bytes);
}

function oidc(cfg) {
  const base = `${cfg.url.replace(/\/$/, "")}/realms/${cfg.realm}/protocol/openid-connect`;
  return { auth: `${base}/auth`, token: `${base}/token`, logout: `${base}/logout` };
}

const redirectUri = () => `${window.location.origin}/auth/callback`;

export async function keycloakRedirect(next = "/workspace") {
  const cfg = await authConfig();
  const verifier = randomString();
  const state = randomString(16);
  sessionStorage.setItem("graphbase.pkce", JSON.stringify({ verifier, state, next }));
  const challenge = b64url(await sha256(verifier));
  const params = new URLSearchParams({
    client_id: cfg.client_id, redirect_uri: redirectUri(), response_type: "code", scope: "openid profile email",
    code_challenge: challenge, code_challenge_method: "S256", state,
  });
  window.location.assign(`${oidc(cfg).auth}?${params}`);
}

function storeTokens(t) {
  save({
    provider: "keycloak", access_token: t.access_token, refresh_token: t.refresh_token, id_token: t.id_token,
    expires_at: Date.now() + t.expires_in * 1000,
  });
}

export async function keycloakCallback(search) {
  const cfg = await authConfig();
  const params = new URLSearchParams(search);
  const pending = JSON.parse(sessionStorage.getItem("graphbase.pkce") || "{}");
  sessionStorage.removeItem("graphbase.pkce");
  if (params.get("error")) throw new Error(params.get("error_description") || params.get("error"));
  if (!pending.state || params.get("state") !== pending.state) throw new Error("Sign-in state mismatch; please try again");
  const r = await fetch(oidc(cfg).token, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code", client_id: cfg.client_id, code: params.get("code"),
      redirect_uri: redirectUri(), code_verifier: pending.verifier,
    }),
  });
  if (!r.ok) throw new Error("Keycloak did not accept the sign-in");
  storeTokens(await r.json());
  return pending.next || "/workspace";
}

async function refreshKeycloak(s) {
  const cfg = await authConfig();
  const r = await fetch(oidc(cfg).token, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "refresh_token", client_id: cfg.client_id, refresh_token: s.refresh_token }),
  });
  if (!r.ok) {
    clearSession();
    return null;
  }
  storeTokens(await r.json());
  return session();
}

// A valid access token, refreshing a Keycloak token that is about to expire.
export async function accessToken() {
  let s = session();
  if (!s) return null;
  if (s.expires_at - Date.now() < 30000) {
    if (s.provider === "keycloak" && s.refresh_token) s = await refreshKeycloak(s);
    else {
      clearSession();
      return null;
    }
  }
  return s && s.access_token;
}

export async function signOut() {
  const s = session();
  clearSession();
  if (s && s.provider === "keycloak") {
    const cfg = await authConfig();
    const params = new URLSearchParams({ client_id: cfg.client_id, post_logout_redirect_uri: `${window.location.origin}/login` });
    if (s.id_token) params.set("id_token_hint", s.id_token);
    window.location.assign(`${oidc(cfg).logout}?${params}`);
    return;
  }
  window.location.assign("/login");
}
