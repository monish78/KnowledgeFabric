# Test results (local laptop, qwen2.5:3b on CPU, 2026-09-30)

## Automated tests (no LLM needed)
- Backend: 156 passed (`docker compose exec backend pytest -q`), covering schema/audit columns, local and Keycloak auth,
  loader counts and rejections exactly matching `data/expected/manifest.json`, add-data, KB isolation, read-only chat
  Cypher, access-control matrix, RAG retrieval, API end-to-end.
- Browser (Playwright, `e2e/`): login, workspace, review edit/delete/undo with live Cypher, grant/revoke with audit log,
  add data with rejected-rows report, user/outsider roles, sign out: all passed. Not run to completion: LLM chat in the
  browser, upload -> extraction in the browser, Keycloak redirect login (the Keycloak login flow is covered by backend tests).

## Real-LLM quality (qwen2.5:3b)
| Area | Result |
|---|---|
| Graph extraction (2 runs, identical) | 5/5 entity keys, 6/6 relationships, PII recall 1.0 / precision 1.0, ~7-10 min |
| Graph chat | 7/11 core questions (before the schema-lint + error-hint changes; not re-measured) |
| RAG chat | 6/6 (correct 30-day window despite the superseded 45-day rule) |
| RAG PII | emails/phones exact; person names found 3 of 4 with the candidate-classification method (probe) |
| Safety | "delete all suppliers" -> refused, graph unchanged |

Known weak spots with the 3b model: some relationship names need renaming on the Review screen (e.g. STORAGED),
and multi-step graph questions. A larger model on the work system (qwen2.5:7b+ or Azure GPT-4.1) should do better;
rerun `docker compose exec backend pytest -m llm -s` there to measure.
