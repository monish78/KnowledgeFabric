# Graphbase

Knowledge Graph + RAG studio. See `CLAUDE.md` for the spec and decisions.

## Run

```bash
cp .env.example .env        # then set passwords / secrets
docker compose up -d --build
curl localhost:8000/api/health
```

| Service  | URL |
|----------|-----|
| Frontend | http://localhost:5173 |
| Backend  | http://localhost:8000/docs |
| Neo4j    | http://localhost:7474 (user `neo4j`, password from `.env`) |
| Postgres | localhost:5433 |
| Chroma   | localhost:8001 |

Ollama runs on the host at `localhost:11434`. The `ollama-proxy` profile (on by default via
`COMPOSE_PROFILES`) forwards the Docker bridge address `172.17.0.1:11435` to it, so the backend uses
`http://host.docker.internal:11435`. On Docker Desktop, or if Ollama listens on `0.0.0.0`, drop the
profile and use `http://host.docker.internal:11434`.

## Users (no sign-up screen)

```bash
docker compose exec backend python -m app.cli seed-demo-users        # local testing, password test1234
docker compose exec backend python -m app.cli create-user meera.s --name "Meera S"
docker compose exec backend python -m app.cli create-user meera.s --name "Meera S" --keycloak  # pre-provision
docker compose exec backend python -m app.cli deactivate-user meera.s
```

With `AUTH_PROVIDER=keycloak`, users are created automatically on first sign-in; pre-provisioning only
lets an owner grant access to someone who hasn't signed in yet.

## Tests

```bash
docker compose exec backend pytest -q tests
```

Tests use a separate `graphbase_test` database that is rebuilt on every run.

## Test dataset

`data/generate_dataset.py` builds a deliberately messy, deterministic dataset in `data/samples/`
and the ground truth in `data/expected/manifest.json` (expected counts, rejected rows, PII columns,
and question/answer pairs). Regenerate with:

```bash
.venv/bin/python data/generate_dataset.py
```

## Local Python environment

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r backend/requirements.txt -r data/requirements.txt
```

## Local test logins

With `COMPOSE_PROFILES` including `keycloak`, Keycloak runs at http://localhost:8080 (admin/admin) with
realm `graphbase` and test users `priya.nair`, `arjun.mehta`, `sneha.iyer`, `karthik.r`, `meera.s`,
`rohan.d` (password `test1234`). The local Postgres login uses the same user IDs.

## Switching to production

- `AUTH_PROVIDER=keycloak` and the `KEYCLOAK_*` variables
- Neo4j Enterprise: `NEO4J_IMAGE=neo4j:5-enterprise`, `NEO4J_ACCEPT_LICENSE=yes`, `NEO4J_MODE=multi`
- Azure OpenAI: `LLM_PROVIDER=azure` (after uncommenting the Azure code) and the `AZURE_OPENAI_*` variables
