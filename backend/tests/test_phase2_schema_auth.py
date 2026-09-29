"""Phase 2: Postgres schema (audit columns, constraints) and both auth providers."""
import datetime as dt
import time

import httpx
import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.cli import create_user, main as cli_main
from app.db import get_conn

AUDITED = ["users", "kb_catalog", "knowledge_bases", "kb_access", "kb_pii_fields", "jobs", "pipeline_runs"]


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


def test_spec_columns_present():
    with get_conn() as conn:
        cols = lambda t: {r["column_name"] for r in conn.execute(  # noqa: E731
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (t,))}
        assert {"id", "kb_name", "user_id", "role", "granted_by", "granted_at", "revoked_at"} <= cols("kb_access")
        assert {"user_id", "kb_name", "kb_type", "domain", "sub_domain", "access"} <= cols("knowledge_bases")
        assert {"kb_name", "node_label", "property_name", "source_document", "pii_category", "sensitivity",
                "confidence", "detected_by"} <= cols("kb_pii_fields")


def _make_kb(conn, name="retail_supply_chain_kg", owner="priya.nair"):
    conn.execute("INSERT INTO kb_catalog (kb_name, kb_type, domain, sub_domain, owner_id, modified_by) "
                 "VALUES (%s, 'graph', 'Retail', 'Supply chain', %s, %s)", (name, owner, owner))


def test_updated_at_trigger_and_modified_by():
    create_user("priya.nair", "Priya Nair", password="x")
    create_user("arjun.mehta", "Arjun Mehta", password="x")
    with get_conn() as conn:
        _make_kb(conn)
        conn.execute("INSERT INTO kb_access (kb_name, user_id, role, granted_by, modified_by) "
                     "VALUES ('retail_supply_chain_kg', 'arjun.mehta', 'user', 'priya.nair', 'priya.nair')")
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
    grant = ("INSERT INTO kb_access (kb_name, user_id, role, granted_by, modified_by) "
             "VALUES ('retail_supply_chain_kg', 'arjun.mehta', 'user', 'priya.nair', 'priya.nair')")
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
        conn.execute("INSERT INTO kb_pii_fields (kb_name, pii_category, sensitivity, modified_by) "
                     "VALUES ('retail_supply_chain_kg', 'email', 'high', 'system')")


def _row(table):
    with get_conn() as conn:
        return conn.execute(f"SELECT * FROM {table}").fetchone()


# ------------------------------------------------------------------ local auth
@pytest.fixture
def demo_users():
    cli_main(["seed-demo-users"])


def _login(client, user="priya.nair", pw="test1234"):
    return client.post("/api/auth/login", json={"user_id": user, "password": pw})


def test_local_login_and_me(client, demo_users):
    r = _login(client)
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json() == {"user_id": "priya.nair", "display_name": "Priya Nair",
                         "email": "priya.nair@graphbase-retail.example"}
    with get_conn() as conn:
        row = conn.execute("SELECT last_login_at, modified_by FROM users WHERE user_id = 'priya.nair'").fetchone()
    assert row["last_login_at"] is not None and row["modified_by"] == "seed"


@pytest.mark.parametrize("user,pw", [("priya.nair", "wrong"), ("nobody", "test1234"), ("PRIYA.NAIR", "test1234")])
def test_local_login_rejects_bad_credentials_with_same_message(client, demo_users, user, pw):
    r = _login(client, user, pw)
    assert r.status_code == 401
    assert r.json()["detail"] == "Incorrect user ID or password"


def test_protected_route_needs_valid_token(client, demo_users, settings):
    s = settings()
    assert client.get("/api/auth/me").status_code == 401
    token = _login(client).json()["access_token"]
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {tampered}"}).status_code == 401
    expired = jwt.encode({"sub": "priya.nair", "iss": "graphbase-local",
                          "exp": dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)}, s.jwt_secret, "HS256")
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    forged = jwt.encode({"sub": "priya.nair", "iss": "graphbase-local", "exp": time.time() + 60}, "guessed", "HS256")
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_deactivated_user_loses_access(client, demo_users):
    token = _login(client).json()["access_token"]
    cli_main(["deactivate-user", "priya.nair"])
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert _login(client).status_code == 401


def test_auth_config_local(client):
    assert client.get("/api/auth/config").json() == {"provider": "local"}


# ------------------------------------------------------------------ keycloak auth
KC_PUBLIC = "http://localhost:8080"
KC_INTERNAL = "http://keycloak:8080"


def _keycloak_token(user="priya.nair", pw="test1234"):
    # Direct password grant is enabled in the local test realm only so tests can get tokens;
    # the app itself signs users in by redirecting to Keycloak's login page.
    try:
        r = httpx.post(f"{KC_INTERNAL}/realms/graphbase/protocol/openid-connect/token",
                       data={"grant_type": "password", "client_id": "graphbase-app", "username": user, "password": pw},
                       timeout=10)
    except httpx.HTTPError:
        pytest.skip("local Keycloak not running (compose profile 'keycloak')")
    r.raise_for_status()
    return r.json()["access_token"]


@pytest.fixture
def keycloak(settings):
    return settings(AUTH_PROVIDER="keycloak", KEYCLOAK_URL=KC_PUBLIC, KEYCLOAK_INTERNAL_URL=KC_INTERNAL,
                    KEYCLOAK_REALM="graphbase", KEYCLOAK_CLIENT_ID="graphbase-app")


def test_keycloak_token_accepted_and_user_provisioned(client, keycloak):
    token = _keycloak_token()
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == "priya.nair"
    row = _row("users")
    assert (row["user_id"], row["auth_source"], row["password_hash"], row["modified_by"]) == \
        ("priya.nair", "keycloak", None, "system")
    # second request doesn't create a duplicate
    client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 1


def test_keycloak_mode_rejects_other_tokens(client, keycloak, demo_users):
    local = jwt.encode({"sub": "priya.nair", "iss": "graphbase-local", "exp": time.time() + 60},
                       keycloak.jwt_secret, "HS256")
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {local}"}).status_code == 401
    # right claims, but signed with a key that isn't Keycloak's
    kid = jwt.get_unverified_header(_keycloak_token())["kid"]
    fake_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode({"sub": "x", "preferred_username": "priya.nair", "iss": f"{KC_PUBLIC}/realms/graphbase",
                         "aud": "graphbase-app", "exp": time.time() + 60}, fake_key, "RS256", headers={"kid": kid})
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    assert _login(client).status_code == 400  # password form disabled


def test_keycloak_wrong_audience_rejected(client, settings):
    settings(AUTH_PROVIDER="keycloak", KEYCLOAK_URL=KC_PUBLIC, KEYCLOAK_INTERNAL_URL=KC_INTERNAL,
             KEYCLOAK_REALM="graphbase", KEYCLOAK_CLIENT_ID="some-other-app")
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {_keycloak_token()}"})
    assert r.status_code == 401


def test_auth_config_keycloak(client, keycloak):
    assert client.get("/api/auth/config").json() == {
        "provider": "keycloak", "url": KC_PUBLIC, "realm": "graphbase", "client_id": "graphbase-app",
        "issuer": f"{KC_PUBLIC}/realms/graphbase"}
