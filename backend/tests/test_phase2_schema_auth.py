"""Phase 2: Postgres schema (audit columns, constraints), server-side sessions and both sign-in providers."""

import datetime as dt
import time
from urllib.parse import parse_qs, urlparse

import httpx
import psycopg
import pytest

from app import auth
from app.cli import create_user
from app.cli import main as cli_main
from app.db import get_conn

from .conftest import login

AUDITED = [
    "users",
    "kb_catalog",
    "knowledge_bases",
    "kb_access",
    "kb_pii_fields",
    "jobs",
    "pipeline_runs",
    "sessions",
    "oidc_login_requests",
]


# ------------------------------------------------------------------ schema
def test_every_table_has_audit_columns():
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT table_name, column_name, is_nullable FROM information_schema.columns
               WHERE table_schema = 'public' AND column_name IN ('created_at', 'updated_at', 'modified_by')"""
        ).fetchall()
    found = {(r["table_name"], r["column_name"]) for r in rows if r["is_nullable"] == "NO"}
    for t in AUDITED:
        for c in ("created_at", "updated_at", "modified_by"):
            assert (t, c) in found, f"{t}.{c} missing or nullable"


def _columns(conn, table):
    rows = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
    return {r["column_name"] for r in rows}


def test_spec_columns_present():
    with get_conn() as conn:
        assert {"id", "kb_name", "user_id", "role", "granted_by", "granted_at", "revoked_at"} <= _columns(
            conn, "kb_access"
        )
        assert {"user_id", "kb_name", "kb_type", "domain", "sub_domain", "access"} <= _columns(conn, "knowledge_bases")
        assert {
            "kb_name",
            "node_label",
            "property_name",
            "source_document",
            "pii_category",
            "sensitivity",
            "confidence",
            "detected_by",
        } <= _columns(conn, "kb_pii_fields")


def _make_kb(conn, name="retail_supply_chain_kg", owner="priya.nair"):
    conn.execute(
        "INSERT INTO kb_catalog (kb_name, kb_type, domain, sub_domain, owner_id, modified_by) "
        "VALUES (%s, 'graph', 'Retail', 'Supply chain', %s, %s)",
        (name, owner, owner),
    )


def test_updated_at_trigger_and_modified_by():
    create_user("priya.nair", "Priya Nair", password="x")
    create_user("arjun.mehta", "Arjun Mehta", password="x")
    with get_conn() as conn:
        _make_kb(conn)
        conn.execute(
            "INSERT INTO kb_access (kb_name, user_id, role, granted_by, modified_by) "
            "VALUES ('retail_supply_chain_kg', 'arjun.mehta', 'user', 'priya.nair', 'priya.nair')"
        )
    before = _row("kb_access")
    time.sleep(0.05)
    with get_conn() as conn:
        conn.execute("UPDATE kb_access SET revoked_at = now(), modified_by = 'priya.nair'")
    after = _row("kb_access")
    assert after["updated_at"] > before["updated_at"]
    assert after["created_at"] == before["created_at"]
    assert after["modified_by"] == "priya.nair"


def test_login_timestamp_does_not_count_as_modification():
    create_user("priya.nair", "Priya Nair", password="x", actor="admin")
    before = _row("users")
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_login_at = now(), modified_by = 'someone-else'")
    after = _row("users")
    assert after["last_login_at"] is not None
    assert (after["updated_at"], after["modified_by"]) == (before["updated_at"], "admin")


def test_one_active_grant_per_user_and_kb():
    create_user("priya.nair", "Priya Nair", password="x")
    create_user("arjun.mehta", "Arjun Mehta", password="x")
    grant = (
        "INSERT INTO kb_access (kb_name, user_id, role, granted_by, modified_by) "
        "VALUES ('retail_supply_chain_kg', 'arjun.mehta', 'user', 'priya.nair', 'priya.nair')"
    )
    with get_conn() as conn:
        _make_kb(conn)
        conn.execute(grant)
    with pytest.raises(psycopg.errors.UniqueViolation), get_conn() as conn:
        conn.execute(grant)
    with get_conn() as conn:  # after a revoke the user can be granted again
        conn.execute("UPDATE kb_access SET revoked_at = now(), modified_by = 'priya.nair'")
        conn.execute(grant)


@pytest.mark.parametrize("bad_name", ["Retail KG", "x", "1kg", "kg;drop", "a" * 64])
def test_kb_name_rules(bad_name):
    create_user("priya.nair", "Priya Nair", password="x")
    with pytest.raises(psycopg.errors.CheckViolation), get_conn() as conn:
        _make_kb(conn, bad_name)


def test_pii_row_needs_a_target():
    create_user("priya.nair", "Priya Nair", password="x")
    with get_conn() as conn:
        _make_kb(conn)
    with pytest.raises(psycopg.errors.CheckViolation), get_conn() as conn:
        conn.execute(
            "INSERT INTO kb_pii_fields (kb_name, pii_category, sensitivity, modified_by) "
            "VALUES ('retail_supply_chain_kg', 'email', 'high', 'system')"
        )


def _row(table):
    with get_conn() as conn:
        return conn.execute(f"SELECT * FROM {table}").fetchone()


# ------------------------------------------------------------------ local sign-in and sessions
@pytest.fixture
def demo_users():
    cli_main(["seed-demo-users"])


def _session_row(user="priya.nair"):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM sessions WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (user,)
        ).fetchone()


def test_login_creates_server_side_session_cookie(client, demo_users):
    r = client.post("/api/auth/login", json={"user_id": "priya.nair", "password": "test1234"})
    assert r.status_code == 200
    assert r.json() == {
        "user": {"user_id": "priya.nair", "display_name": "Priya Nair", "email": "priya.nair@graphbase-retail.example"}
    }
    assert "token" not in r.text  # nothing secret in the body
    cookie = r.headers["set-cookie"]
    assert "graphbase_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    token = r.cookies["graphbase_session"]
    row = _session_row()
    assert row["id"] == auth.hash_token(token) and token not in str(row)  # only the hash is stored
    assert row["auth_source"] == "local" and row["revoked_at"] is None and row["modified_by"] == "priya.nair"
    client.cookies.clear()
    me = client.get("/api/auth/me", headers={"Cookie": f"graphbase_session={token}"})
    assert me.json()["user_id"] == "priya.nair"


@pytest.mark.parametrize("user,pw", [("priya.nair", "wrong"), ("nobody", "test1234"), ("PRIYA.NAIR", "test1234")])
def test_bad_credentials_same_message_and_no_session(client, demo_users, user, pw):
    r = client.post("/api/auth/login", json={"user_id": user, "password": pw})
    assert r.status_code == 401 and r.json()["detail"] == "Incorrect user ID or password"
    assert "set-cookie" not in r.headers
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"] == 0


def test_protected_routes_need_a_valid_session(client, demo_users):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Cookie": "graphbase_session=forged-value"}).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer anything"}).status_code == 401


@pytest.mark.parametrize("column", ["expires_at", "idle_expires_at"])
def test_expired_sessions_are_refused(client, demo_users, column):
    headers = login(client)
    with get_conn() as conn:
        conn.execute(f"UPDATE sessions SET {column} = now() - interval '1 second'")
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_activity_extends_the_idle_timeout(client, demo_users):
    headers = login(client)
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET last_seen_at = now() - interval '5 minutes', "
            "idle_expires_at = now() + interval '1 minute'"
        )
    before = _session_row()
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    after = _session_row()
    assert after["idle_expires_at"] > before["idle_expires_at"] and after["last_seen_at"] > before["last_seen_at"]
    assert after["updated_at"] == before["updated_at"]  # activity is not an audited change


def test_logout_revokes_the_session_on_the_server(client, demo_users):
    headers = login(client)
    r = client.post("/api/auth/logout", headers=headers)
    assert r.status_code == 200 and 'graphbase_session=""' in r.headers["set-cookie"]
    assert client.get("/api/auth/me", headers=headers).status_code == 401  # the old cookie is dead
    row = _session_row()
    assert row["revoked_at"] and row["revoked_reason"] == "logout" and row["modified_by"] == "priya.nair"


def test_deactivation_ends_sessions(client, demo_users):
    headers = login(client)
    cli_main(["deactivate-user", "priya.nair"])
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    assert _session_row()["revoked_reason"] == "user deactivated"
    assert client.post("/api/auth/login", json={"user_id": "priya.nair", "password": "test1234"}).status_code == 401


def test_state_changing_requests_need_the_csrf_header(client, demo_users):
    headers = login(client)
    bare = type(client)(client.app)  # no X-Requested-With header
    r = bare.post("/api/auth/logout", headers=headers)
    assert r.status_code == 403 and r.json()["detail"] == "Missing CSRF header"
    r = bare.post("/api/auth/login", json={"user_id": "priya.nair", "password": "test1234"})
    assert r.status_code == 403
    assert bare.get("/api/auth/me", headers=headers).status_code == 200  # reads are fine
    assert client.get("/api/auth/me", headers=headers).status_code == 200  # still signed in


def test_switching_provider_invalidates_old_sessions(client, demo_users, settings):
    headers = login(client)
    settings(
        AUTH_PROVIDER="keycloak",
        KEYCLOAK_URL="http://localhost:8080",
        KEYCLOAK_REALM="graphbase",
        KEYCLOAK_CLIENT_ID="graphbase-app",
    )
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_auth_config_local(client):
    assert client.get("/api/auth/config").json() == {"provider": "local", "login_url": None}


# ------------------------------------------------------------------ Keycloak sign-in run by the backend
KC_PUBLIC = "http://localhost:8080"
KC_INTERNAL = "http://keycloak:8080"


def _keycloak_tokens(user="priya.nair", pw="test1234"):
    # The local test realm allows the password grant so tests can get real tokens without a browser;
    # the app itself uses the authorization-code flow (see e2e/test_ui.py for the browser run).
    try:
        r = httpx.post(
            f"{KC_INTERNAL}/realms/graphbase/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "graphbase-app",
                "username": user,
                "password": pw,
                "scope": "openid profile email",
            },
            timeout=10,
        )
    except httpx.HTTPError:
        pytest.skip("local Keycloak not running (compose profile 'keycloak')")
    r.raise_for_status()
    return r.json()


@pytest.fixture
def keycloak(settings):
    return settings(
        AUTH_PROVIDER="keycloak",
        KEYCLOAK_URL=KC_PUBLIC,
        KEYCLOAK_INTERNAL_URL=KC_INTERNAL,
        KEYCLOAK_REALM="graphbase",
        KEYCLOAK_CLIENT_ID="graphbase-app",
        APP_URL="http://localhost:5173",
    )


def test_keycloak_login_redirects_with_pkce(client, keycloak):
    r = client.get("/api/auth/login?next=/chat?kb=x", follow_redirects=False)
    assert r.status_code == 302
    url = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{KC_PUBLIC}/realms/graphbase/protocol/openid-connect/auth"
    assert q["client_id"] == "graphbase-app" and q["redirect_uri"] == "http://localhost:5173/api/auth/callback"
    assert q["code_challenge_method"] == "S256" and q["response_type"] == "code" and "openid" in q["scope"]
    with get_conn() as conn:
        pending = conn.execute("SELECT * FROM oidc_login_requests WHERE state = %s", (q["state"],)).fetchone()
    assert pending["nonce"] == q["nonce"] and pending["next_path"] == "/chat?kb=x"
    assert auth._pkce_challenge(pending["code_verifier"]) == q["code_challenge"]  # verifier never leaves the server


@pytest.mark.parametrize("query", ["code=abc&state=unknown", "error=access_denied&error_description=Nope", ""])
def test_keycloak_callback_rejects_bad_responses(client, keycloak, query):
    r = client.get(f"/api/auth/callback?{query}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/login?error=")
    assert "set-cookie" not in r.headers


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/chat?kb=a", "/chat?kb=a"),
        ("//evil.example", "/workspace"),
        ("https://evil.example", "/workspace"),
        (None, "/workspace"),
    ],
)
def test_no_open_redirect_after_sign_in(path, expected):
    assert auth.safe_next(path) == expected


def _keycloak_session(client, tokens):
    """Create a session exactly as the callback does, from real Keycloak tokens."""
    from fastapi import Response
    from starlette.requests import Request

    user = auth.keycloak_user(auth.verify_id_token(tokens["id_token"], auth.get_settings(), None))
    response = Response()
    auth.create_session(
        response, Request({"type": "http", "headers": [], "client": ("test", 0)}), user.user_id, "keycloak", tokens
    )
    token = response.headers["set-cookie"].split("graphbase_session=")[1].split(";")[0]
    return {"Cookie": f"graphbase_session={token}"}


def test_keycloak_session_user_provisioning_and_encrypted_tokens(client, keycloak):
    tokens = _keycloak_tokens()
    headers = _keycloak_session(client, tokens)
    assert client.get("/api/auth/me", headers=headers).json()["user_id"] == "priya.nair"
    row = _session_row()
    assert row["auth_source"] == "keycloak" and tokens["refresh_token"] not in (row["kc_refresh_token"] or "")
    assert auth.decrypt(row["kc_refresh_token"]) == tokens["refresh_token"]  # stored encrypted
    with get_conn() as conn:
        u = conn.execute(
            "SELECT auth_source, password_hash, modified_by FROM users WHERE user_id = 'priya.nair'"
        ).fetchone()
    assert (u["auth_source"], u["password_hash"], u["modified_by"]) == ("keycloak", None, "system")


def test_keycloak_id_token_checks(keycloak):
    tokens = _keycloak_tokens()
    with pytest.raises(auth.HTTPException, match="does not match"):
        auth.verify_id_token(tokens["id_token"], auth.get_settings(), nonce="some-other-nonce")
    with pytest.raises(auth.HTTPException):
        auth.verify_id_token(tokens["access_token"], auth.get_settings(), None)  # wrong audience/type
    forged = tokens["id_token"][:-6] + "AAAAAA"
    with pytest.raises(auth.HTTPException):
        auth.verify_id_token(forged, auth.get_settings(), None)


def test_keycloak_session_is_rechecked_with_keycloak(client, keycloak):
    headers = _keycloak_session(client, _keycloak_tokens())
    with get_conn() as conn:  # access token expired -> backend refreshes with Keycloak
        conn.execute("UPDATE sessions SET kc_access_expires_at = now() - interval '1 second'")
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    assert _session_row()["kc_access_expires_at"] > dt.datetime.now(dt.UTC)
    with get_conn() as conn:  # Keycloak no longer accepts the refresh token -> session ends
        conn.execute(
            "UPDATE sessions SET kc_refresh_token = %s, kc_access_expires_at = now() - interval '1 second'",
            (auth.encrypt("not-a-valid-refresh-token"),),
        )
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    assert _session_row()["revoked_reason"] == "keycloak session ended"


def test_keycloak_logout_ends_the_keycloak_session_too(client, keycloak):
    tokens = _keycloak_tokens()
    headers = _keycloak_session(client, tokens)
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    r = httpx.post(
        f"{KC_INTERNAL}/realms/graphbase/protocol/openid-connect/token",
        data={"grant_type": "refresh_token", "client_id": "graphbase-app", "refresh_token": tokens["refresh_token"]},
        timeout=10,
    )
    assert r.status_code == 400  # Keycloak killed its session as well


def test_password_login_disabled_in_keycloak_mode(client, keycloak):
    r = client.post("/api/auth/login", json={"user_id": "priya.nair", "password": "test1234"})
    assert r.status_code == 400
    assert client.get("/api/auth/config").json() == {"provider": "keycloak", "login_url": "/api/auth/login"}
