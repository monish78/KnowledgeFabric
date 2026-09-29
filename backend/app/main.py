import chromadb
import httpx
import psycopg
from fastapi import FastAPI
from neo4j import GraphDatabase

from app.config import get_settings

app = FastAPI(title="Graphbase API")


def _check_postgres(s) -> str:
    with psycopg.connect(s.postgres_dsn, connect_timeout=3) as conn:
        return conn.execute("SELECT version()").fetchone()[0].split(",")[0]


def _check_neo4j(s) -> str:
    with GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password)) as driver:
        info = driver.execute_query(
            "CALL dbms.components() YIELD name, versions, edition RETURN versions[0] AS v, edition"
        ).records[0]
        return f"{info['v']} {info['edition']} (mode={s.neo4j_mode})"


def _check_chroma(s) -> str:
    client = chromadb.HttpClient(host=s.chroma_host, port=s.chroma_port)
    client.heartbeat()
    return f"ok ({client.get_version()})"


def _check_ollama(s) -> str:
    tags = httpx.get(f"{s.ollama_base_url}/api/tags", timeout=3).json()
    models = {m["name"] for m in tags.get("models", [])}
    missing = [m for m in (s.ollama_chat_model, s.ollama_embed_model) if m not in models and f"{m}:latest" not in models]
    return "ok" if not missing else f"reachable, missing models: {', '.join(missing)}"


@app.get("/api/health")
def health():
    s = get_settings()
    checks = {"postgres": _check_postgres, "neo4j": _check_neo4j, "chroma": _check_chroma}
    if s.llm_provider == "ollama":
        checks["ollama"] = _check_ollama
    result = {}
    for name, fn in checks.items():
        try:
            result[name] = {"ok": True, "detail": fn(s)}
        except Exception as exc:  # report every dependency, don't stop at the first failure
            result[name] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": all(v["ok"] for v in result.values()), "services": result}
