# Test results (local laptop, qwen2.5:3b on CPU)

All prompts and rules are domain-neutral; every number below comes from the current code.

## Without the LLM
- Backend: 202 passed (`docker compose exec backend pytest -q`): schema and audit columns, server-side sessions and
  CSRF, the Keycloak sign-in flow against a real Keycloak, exact counts and rejections for both datasets, add-data,
  KB isolation, read-only chat, access control, RAG retrieval, and extraction with the LLM switched off.
- Browser (Playwright, `e2e/`): 12/12 passed, including Keycloak sign-in, the full upload -> extraction -> review ->
  build -> chat flow, and editing on the Review screen (rename label/property, remove property, rename relationship,
  delete a node type, add a node type and a relationship) verified in Neo4j and Postgres after Submit.
- Lint: ruff and ESLint report no issues.

## With the real LLM (qwen2.5:3b)
| Area | Retail dataset | Hospital dataset (new domain) |
|---|---|---|
| Entity keys | 5/5 | 8/8 |
| Relationships found | 6/6 | 10/11 (the miss is now handled by a rule; covered by a test) |
| Spreadsheet PII | recall 1.0, precision 1.0 | recall 1.0, precision 1.0 (7 columns, no false positives) |
| Graph chat, core questions | 6-7 / 11 | 6 / 11 |
| RAG chat | 6/6 | 6/6 |
| Document PII | e-mails/phones exact, names 1-3 of 4 | exact for all 3 documents |
| Destructive chat request | refused | refused |

Relationship names from a 3b model are often awkward (e.g. `Department-TREATS->Doctor`) and need renaming on the
Review screen. Graph chat misses come from the model: dropping the "Dr." prefix from names, inventing a relationship,
invalid aggregation syntax. A larger model (Azure GPT-4.1 at work) is expected to do much better on these; re-run
`docker compose exec backend pytest -m llm -s` there to measure it.
