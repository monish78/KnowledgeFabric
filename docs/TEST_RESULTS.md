# Test results (local laptop, qwen2.5:3b on CPU, 30 Sep 2026)

All prompts and rules are domain-neutral (dataset-specific examples and word lists were removed on 30 Sep);
every number below was measured after that change.

## Without the LLM
- Backend: 173 passed (`docker compose exec backend pytest -q`), including server-side sessions, CSRF protection, the
  Keycloak sign-in flow run by the backend against a real Keycloak, and a never-seen school workbook extracted and
  loaded correctly with the LLM switched off.
- Browser (Playwright, `e2e/`): 11/11 passed, including the Keycloak sign-in, the full upload -> extraction -> review ->
  build -> chat flow with the real LLM, and checks that the browser holds no session data (HttpOnly cookie, empty storage).
- Lint: ruff (backend, tests, e2e, data) and ESLint (frontend) report no issues.

## With the real LLM
| Area | Result |
|---|---|
| Graph extraction, retail workbook | 5/5 entity keys, 6/6 relationships, PII recall 1.0 / precision 1.0 (~7 min) |
| Graph extraction, unseen school workbook | 3/3 keys, link table recognised, PII names + email found; relationship names swapped (fix on Review) |
| Graph chat | 6/11 and 7/11 core questions on two runs (bar is 60%); "delete all suppliers" refused |
| RAG chat | 6/6 |
| Document PII | e-mails and phones exact; person names 1-3 of 4 depending on document |
| Browser with LLM | 3/3: upload -> extraction -> review -> build -> chat, graph chat, RAG chat |

Graph chat is the weak spot with a 3b model. Re-run `docker compose exec backend pytest -m llm -s` with the work
system's model before relying on it. The full code walkthrough is docs/Graphbase_Code_Guide.pdf.
