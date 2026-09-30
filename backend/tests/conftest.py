"""Tests run against a throwaway database (graphbase_test), rebuilt once per session."""

import os

os.environ["POSTGRES_DB"] = "graphbase_test"

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402

TABLES = [
    "sessions",
    "oidc_login_requests",
    "pipeline_runs",
    "jobs",
    "kb_pii_fields",
    "kb_access",
    "knowledge_bases",
    "kb_catalog",
    "users",
]


@pytest.fixture(scope="session", autouse=True)
def test_database():
    get_settings.cache_clear()
    s = get_settings()
    admin_dsn = s.postgres_dsn.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS graphbase_test WITH (FORCE)")
        conn.execute("CREATE DATABASE graphbase_test")
    db.run_migrations()
    db.open_pool()
    yield
    db.close_pool()


@pytest.fixture(autouse=True)
def clean_tables():
    with db.get_conn() as conn:
        conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    yield


@pytest.fixture
def settings(monkeypatch):
    """Override settings via env for one test: settings(AUTH_PROVIDER='keycloak')."""

    def apply(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        return get_settings()

    yield apply
    get_settings.cache_clear()


@pytest.fixture
def client():
    """A browser-like client: sends the CSRF header; cookies are passed explicitly per user (see login())."""
    return TestClient(__import__("app.main", fromlist=["app"]).app, headers={"X-Requested-With": "graphbase"})


def login(client, user="priya.nair", password="test1234") -> dict:
    """Sign in and return the headers carrying that user's session cookie (the client keeps no cookies,
    so several users can be simulated with one client)."""
    r = client.post("/api/auth/login", json={"user_id": user, "password": password})
    assert r.status_code == 200, r.text
    token = r.cookies["graphbase_session"]
    client.cookies.clear()
    return {"Cookie": f"graphbase_session={token}"}
