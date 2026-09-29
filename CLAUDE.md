# Graphbase: Knowledge Graph + RAG Studio

Read this file fully before writing code. UI mockups are in `docs/mockups/*.png` (also `docs/Graphbase_UI_Mockups.pdf`, HTML source in `docs/mockup-html/`). Look at the PNGs and match the layout closely.

## Stack (fixed)
- Frontend: React (Vite)
- Backend: FastAPI (Python), LangChain / LangGraph
- Graph DB: Neo4j | Vector DB: ChromaDB | Relational DB: Postgres
- Everything runs via Docker Compose
- LLM is swappable: **Ollama model is active**; **Azure OpenAI GPT-4.1 code is written but commented out**. Put both behind one factory (`get_llm()`, `get_embeddings()`) selected by an env var, so switching is a one-line change.

## Screens (see docs/mockups)
1. Login (user ID + password) -> `1_Main.png`
2. Workspace -> `2_Workspace.png`: left panel has RAG upload and Knowledge-graph upload (CSV/XLSX). Right main panel has the Create form: knowledge graph name, domain, sub-domain, Create button, plus a list of the user's knowledge bases with role (Owner/User).
3. Extraction running -> `3_Processing.png`
4. Review -> `4_Review.png`: shows LLM-extracted nodes/entities and relationships, each editable/deletable, live Cypher preview, Submit button. Nothing is written to Neo4j until Submit.
5. Manage access -> `5_Access.png` (owner only)
6. Add data -> `6_AddData.png`
7. Chat -> `7_Chat.png`
8. Postgres tables -> `8_Schema.png`

## Behaviour
**KG creation flow:** user uploads CSV/XLSX + fills name/domain/sub-domain -> backend runs in background: file goes to the LLM, which returns nodes, entities, relationships and Cypher -> status shown on screen 3 -> Review screen (4) lets user edit/delete -> on Submit, build the graph in Neo4j from the approved schema and store it.

**RAG flow:** upload PDF/DOCX/TXT -> chunk, embed, store in ChromaDB automatically in the background. No review screen.

**Ownership & access:** the user who creates a knowledge base becomes its owner. Only the owner can grant or revoke access to other users. Every grant/revoke is logged with who did it.

**Additional data pipeline:** for an existing KG, new CSV/XLSX is ingested using the already-approved schema (no review screen, merge on key properties, report rejected rows). For RAG bases, new docs are chunked and added to the same Chroma collection.

**Chat:** user picks a knowledge base they own or have access to. Graph bases: LangGraph flow that generates Cypher, runs it on Neo4j, answers from results (show Cypher + path). RAG bases: retrieve from Chroma and answer. Enforce access on the backend for every query, never only in the UI.

## Postgres tables
`kb_access`: id, kb_name, user_id, role (owner|user), granted_by, granted_at, revoked_at (null while active). This is the audit trail.
`knowledge_bases`: user_id, kb_name, kb_type (graph|rag), domain, sub_domain, access (owner|user). One row per user per base they can reach.
Also a `users` table for login (hashed passwords).
Creating a KB inserts an owner row in both tables. Granting adds a row to each. Revoking sets revoked_at and removes the user row.

**Audit columns (manager request):** every table also has `created_at`, `updated_at` (timestamptz) and `modified_by` (user_id of whoever last changed the row; `system` for automated jobs). `updated_at` is maintained by a DB trigger; `modified_by` is set by the app. A revoke sets `modified_by` to the revoker.

`kb_pii_fields` (manager request): one row per PII property found in a knowledge base, detected **automatically by the LLM** during extraction (no user action needed). Columns: id, kb_name, node_label, property_name, pii_category (e.g. person_name, email, phone, address, date_of_birth, government_id, bank_account), sensitivity (high|medium|low), confidence, reason, detected_by (`llm`), plus the audit columns. Shown on the Review screen as a PII badge on the affected node type/property.
RAG documents are PII-scanned too (LLM over each document's chunks during ingest): those rows have `source_document` set and `node_label`/`property_name` null, with an occurrence count per category. Never store the raw PII value in this table.

Supporting tables (not in the mockups): `kb_catalog` (one row per KB: type, owner, status, storage location, draft + approved graph schema/Cypher reused by add-data; `kb_name` primary key so names are globally unique), `jobs` (extraction/ingest progress for screen 3), `pipeline_runs` (add-data runs + rejected-row reports). Screen 8 is database documentation only, not an app page.

## Decisions (answered 2026-09-29)
- "user" role can **add data and chat**. Only the owner grants/revokes.
- RAG and graph bases in **one list**.
- Ollama: chat `qwen2.5` (7b-instruct default for the work system; local testing uses `qwen2.5:3b` because this laptop has 7 GB RAM and no GPU; model name is an env var), embeddings `nomic-embed-text`.
- Auth: **Keycloak in production** (user's work system runs Keycloak). Local testing uses Postgres users + JWT. Both implemented behind `AUTH_PROVIDER=local|keycloak`; with Keycloak, clicking Sign in **redirects to Keycloak's own login page** (authorization code + PKCE); the backend validates Keycloak-issued JWTs (JWKS) and maps `preferred_username` to `user_id`, creating the `users` row on first login. For local testing an optional Keycloak container (compose profile `keycloak`, realm file `keycloak/graphbase-realm.json`) provides the same test users.
- Neo4j: local testing uses **Community (single database)**, but the code must also work on **Enterprise with many KBs in parallel**. `NEO4J_MODE=single|multi`: `multi` = one Neo4j database per KB; `single` = shared database, every node carries a per-KB label so KBs stay isolated. All graph access goes through one GraphStore abstraction; chat Cypher runs read-only and is scoped to the KB.
- KG extraction sends the LLM each sheet's headers + sample rows (not the whole file); the approved Cypher is `UNWIND $rows`-style and runs over all rows in batches.
- `kb_name` is globally unique. RAG bases are created from the same Create form (switches to RAG mode). Users are created by a CLI/seed script (no signup screen).

## Local environment
- Local Python: `uv` venv at `.venv` (Python 3.11) for the dataset generator and running tests/tools outside Docker.
- Local credentials: Postgres `postgres`/`postgres` (host port 5433; 5432 is taken by a host Postgres). Neo4j `neo4j`/`neo4j123` (Neo4j 5 refuses `neo4j` as the password). Test users' password `test1234`.
- Host Ollama listens on 127.0.0.1:11434 only; the `ollama-proxy` compose profile exposes it to containers at `host.docker.internal:11435`.

## Testing
- A deliberately difficult test dataset lives in `data/` (generated by `data/generate_dataset.py`, deterministic seed) with expected answers for automated checks. Every phase is tested against it; code ships to the work system only after tests pass.

## Working style
- Build in phases and stop after each for review: 1) docker-compose + project skeleton, 2) Postgres schema + auth, 3) LLM/embedding factory, 4) KG extraction + review API, 5) RAG ingest, 6) access control, 7) add-data pipeline, 8) chat, 9) React screens.
- Keep secrets in `.env` (provide `.env.example`). Never commit real keys.
