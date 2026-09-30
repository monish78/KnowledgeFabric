"""Phase 1: every service the backend depends on is reachable (run inside the backend container)."""

from fastapi.testclient import TestClient

from app.main import app


def test_core_services_reachable():
    body = TestClient(app).get("/api/health").json()
    for name in ("postgres", "neo4j", "chroma"):
        assert body["services"][name]["ok"], body["services"][name]
