"""Phases 4-8 through the HTTP API: create -> extract -> review -> submit -> build -> access -> add data
-> chat, for graph and RAG bases, plus the access-control matrix. The LLM is scripted here (its real
behaviour is measured in test_llm_quality.py); Neo4j, Postgres, Chroma and embeddings are real."""

import json

import pytest

from app import chat, extraction, jobs, rag
from app.cli import main as cli_main
from app.db import get_conn
from app.graphstore import GraphStore

from .conftest import login
from .fixtures import SAMPLES, manifest, retail_schema

RETAIL = "t_api_retail"
RAG = "t_api_rag"


# ------------------------------------------------------------------ helpers
@pytest.fixture(autouse=True)
def users():
    cli_main(["seed-demo-users"])
    cli_main(["create-user", "outsider", "--name", "Out Sider", "--password", "test1234"])


def token(client, user):
    return login(client, user)


def upload(*names, folder=SAMPLES):
    return [("files", (n.split("/")[-1], (folder / n).read_bytes())) for n in names]


def wait(job_id):
    return jobs.wait(job_id, timeout=900)


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    """Extraction returns the reviewed schema; chat returns scripted Cypher / answers; PII scan is quiet."""

    def fake_extract(sheets, file_name, step=None, cancelled=None):
        for i in range(1, 5):
            step and step(i, "done", "scripted")
        s = retail_schema()
        s["pii"] = [
            {
                "sheet": "Customers",
                "column": "Email",
                "category": "email",
                "sensitivity": "medium",
                "confidence": 0.95,
                "reason": "e-mail addresses",
                "detected_by": "rules",
            },
            {
                "sheet": "Suppliers",
                "column": "Bank Account",
                "category": "bank_account",
                "sensitivity": "high",
                "confidence": 0.9,
                "reason": "bank details",
                "detected_by": "llm",
            },
        ]
        return s

    monkeypatch.setattr(extraction, "extract", fake_extract)
    monkeypatch.setattr(rag, "ask_json", lambda s, u, retries=1: {"people": []})

    cypher = {
        "chennai": "MATCH (s:Supplier)-[:SUPPLIES]->(p:Product)-[:STORED_IN]->(w:Warehouse {city: 'Chennai'}) "
        "RETURN DISTINCT s.name AS supplier ORDER BY supplier",
        "delete": "MATCH (n) DETACH DELETE n",
        "broken": "MATCH (n:Supplier RETURN n",
    }
    calls = {"n": 0}

    def fake_ask_json(system, prompt, retries=1):
        calls["n"] += 1
        q = prompt.lower()
        if "delete" in q:
            return {"cypher": cypher["delete"]}
        if "nonsense" in q:
            return {"cypher": cypher["broken"]}
        return {"cypher": cypher["chennai"]}

    monkeypatch.setattr(chat, "ask_json", fake_ask_json)
    monkeypatch.setattr(chat, "ask_text", lambda system, prompt: "ANSWER: " + prompt[-200:])
    return calls


@pytest.fixture(scope="module", autouse=True)
def cleanup():
    for name in (RETAIL, "t_api_bad", "t_api_empty"):
        GraphStore(name, mode="single").drop()
    rag.drop_collection(RAG)
    yield
    for name in (RETAIL, "t_api_bad", "t_api_empty"):
        GraphStore(name, mode="single").drop()
    rag.drop_collection(RAG)


def create_graph(client, h, name=RETAIL, file="supplier_orders.xlsx"):
    return client.post(
        "/api/kbs",
        headers=h,
        files=upload(file),
        data={"kb_name": name, "kb_type": "graph", "domain": "Retail", "sub_domain": "Supply chain"},
    )


def build_retail(client, h):
    r = create_graph(client, h)
    assert r.status_code == 201, r.text
    assert wait(r.json()["job_id"])["status"] == "succeeded"
    review = client.get(f"/api/kbs/{RETAIL}/review", headers=h).json()
    r = client.post(f"/api/kbs/{RETAIL}/submit", headers=h, json={"schema": review["schema"]})
    assert r.status_code == 200, r.text
    job = wait(r.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    return review


# ------------------------------------------------------------------ the whole graph flow
def test_graph_kb_end_to_end(client):
    priya = token(client, "priya.nair")
    r = create_graph(client, priya)
    assert r.status_code == 201, r.text
    job = wait(r.json()["job_id"])
    assert job["status"] == "succeeded" and [s["status"] for s in job["steps"]] == ["done"] * 5

    # listed as the owner's draft, awaiting review; nothing in Neo4j yet
    (row,) = [k for k in client.get("/api/kbs", headers=priya).json() if k["kb_name"] == RETAIL]
    assert (row["role"], row["status"], row["kb_type"]) == ("owner", "awaiting_review", "graph")
    assert GraphStore(RETAIL).counts()["nodes"] == {}

    # review: edit, preview, validation errors, save
    review = client.get(f"/api/kbs/{RETAIL}/review", headers=priya).json()
    assert len(review["cypher"]) == 11 and review["summary"]["node_types"] == 5
    schema = review["schema"]
    supplier = schema["nodes"][0]
    supplier["properties"] = [p for p in supplier["properties"] if p["name"] != "bank_account"]  # delete
    shipped = next(r for r in schema["relationships"] if r["type"] == "SHIPPED_FROM")
    shipped["type"] = "shipped from warehouse"  # rename
    preview = client.post(f"/api/kbs/{RETAIL}/review/preview", headers=priya, json={"schema": schema}).json()
    assert any("SHIPPED_FROM_WAREHOUSE" in c for c in preview["cypher"])
    assert not any("bank_account" in c for c in preview["cypher"])
    bad = json.loads(json.dumps(schema))
    bad["nodes"][1]["key"]["column"] = "Nope"
    r = client.put(f"/api/kbs/{RETAIL}/review", headers=priya, json={"schema": bad})
    assert r.status_code == 422 and "Nope" in json.dumps(r.json())
    assert client.put(f"/api/kbs/{RETAIL}/review", headers=priya, json={"schema": schema}).status_code == 200
    pii = client.get(f"/api/kbs/{RETAIL}/pii", headers=priya).json()
    assert {(p["node_label"], p["property_name"]) for p in pii} == {("Customer", "email")}  # deleted prop dropped
    # detected automatically and unchanged by the save, so the audit still says system
    assert pii[0]["modified_by"] == "system" and pii[0]["detected_by"] == "rules" and pii[0]["created_at"]

    # submit -> build
    r = client.post(f"/api/kbs/{RETAIL}/submit", headers=priya, json={"schema": schema})
    job = wait(r.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    kb = client.get(f"/api/kbs/{RETAIL}", headers=priya).json()
    assert kb["status"] == "ready" and kb["approved_by"] == "priya.nair"
    assert (
        kb["stats"]["entities"]
        == 48 + 307 + 9 + 120 + manifest()["files"]["supplier_orders.xlsx"]["expected_nodes"]["Orders.order_id"]
    )
    assert "SHIPPED_FROM_WAREHOUSE" in kb["stats"]["by_type"]
    with get_conn() as conn:
        cat = conn.execute(
            "SELECT approved_cypher, modified_by, storage_ref FROM kb_catalog WHERE kb_name = %s", (RETAIL,)
        ).fetchone()
    assert "SHIPPED_FROM_WAREHOUSE" in cat["approved_cypher"] and cat["storage_ref"] == f"label:KB_{RETAIL}"
    runs = client.get(f"/api/kbs/{RETAIL}/runs", headers=priya).json()
    assert [(x["run_no"], x["run_type"], x["status"], x["rows_rejected"]) for x in runs] == [
        (1, "initial_build", "completed", 32)
    ]
    report = client.get(f"/api/kbs/{RETAIL}/runs/{runs[0]['id']}/report", headers=priya).json()
    assert len(report["rejected_report"]) == 32 and {"sheet", "row", "reason"} <= set(report["rejected_report"][0])

    # chat: cypher + path chips + rows, read-only, retry on a broken query
    r = client.post(
        f"/api/kbs/{RETAIL}/chat",
        headers=priya,
        json={"question": "Which suppliers deliver products stored in the Chennai warehouse?"},
    ).json()
    assert [x["supplier"] for x in r["rows"]] == next(
        q["answer"] for q in manifest()["questions"] if "Chennai warehouse?" in q["q"]
    )
    assert [p["text"] for p in r["path"]] == ["Supplier", "SUPPLIES", "Product", "STORED_IN", "Warehouse: Chennai"]
    assert r["cypher"].startswith("MATCH (s:Supplier)")
    r = client.post(f"/api/kbs/{RETAIL}/chat", headers=priya, json={"question": "Please delete everything"}).json()
    assert "only read" in r["answer"] and GraphStore(RETAIL).counts()["nodes"]["Supplier"] == 48
    r = client.post(f"/api/kbs/{RETAIL}/chat", headers=priya, json={"question": "nonsense"}).json()
    assert r["error"] and "rephras" in r["answer"]


def test_access_control_matrix(client):
    priya, arjun, out = token(client, "priya.nair"), token(client, "arjun.mehta"), token(client, "outsider")
    build_retail(client, priya)
    chat_q = {"question": "Which suppliers deliver products stored in the Chennai warehouse?"}
    oct_file = upload("supplier_orders_october.xlsx")

    # outsiders get 403 everywhere, and the KB isn't listed for them
    for method, url, kw in [
        ("get", f"/api/kbs/{RETAIL}", {}),
        ("get", f"/api/kbs/{RETAIL}/review", {}),
        ("post", f"/api/kbs/{RETAIL}/chat", {"json": chat_q}),
        ("get", f"/api/kbs/{RETAIL}/runs", {}),
        ("get", f"/api/kbs/{RETAIL}/access", {}),
        ("get", f"/api/kbs/{RETAIL}/pii", {}),
        ("post", f"/api/kbs/{RETAIL}/access", {"json": {"user_id": "outsider"}}),
        ("post", f"/api/kbs/{RETAIL}/add-data", {"files": oct_file}),
    ]:
        assert getattr(client, method)(url, headers=out, **kw).status_code == 403, url
    assert all(k["kb_name"] != RETAIL for k in client.get("/api/kbs", headers=out).json())

    # grant: owner only, target must exist, no duplicates
    r = client.post(f"/api/kbs/{RETAIL}/access", headers=priya, json={"user_id": "arjun.mehta"})
    assert r.status_code == 201
    assert client.post(f"/api/kbs/{RETAIL}/access", headers=priya, json={"user_id": "arjun.mehta"}).status_code == 409
    assert client.post(f"/api/kbs/{RETAIL}/access", headers=priya, json={"user_id": "ghost"}).status_code == 404
    assert (
        client.post(
            f"/api/kbs/{RETAIL}/access", headers=priya, json={"user_id": "sneha.iyer", "role": "owner"}
        ).status_code
        == 422
    )

    # a 'user' can chat and add data, but not review, see the access page, grant or revoke
    assert client.get(f"/api/kbs/{RETAIL}", headers=arjun).json()["role"] == "user"
    assert client.post(f"/api/kbs/{RETAIL}/chat", headers=arjun, json=chat_q).status_code == 200
    for method, url, kw in [
        ("get", f"/api/kbs/{RETAIL}/review", {}),
        ("get", f"/api/kbs/{RETAIL}/access", {}),
        ("post", f"/api/kbs/{RETAIL}/access", {"json": {"user_id": "sneha.iyer"}}),
        ("delete", f"/api/kbs/{RETAIL}/access/priya.nair", {}),
        ("post", f"/api/kbs/{RETAIL}/submit", {"json": {"schema": {}}}),
    ]:
        r = getattr(client, method)(url, headers=arjun, **kw)
        assert r.status_code == 403 and "owner" in r.json()["detail"], url
    r = client.post(f"/api/kbs/{RETAIL}/add-data", headers=arjun, files=oct_file)
    assert r.status_code == 202, r.text
    assert wait(r.json()["job_id"])["status"] == "succeeded"
    runs = client.get(f"/api/kbs/{RETAIL}/runs", headers=arjun).json()
    assert runs[0]["run_no"] == 2 and runs[0]["started_by"] == "arjun.mehta" and runs[0]["rows_rejected"] == 10

    # revoke takes effect immediately; owner can't be revoked
    assert client.delete(f"/api/kbs/{RETAIL}/access/priya.nair", headers=priya).status_code == 400
    assert client.delete(f"/api/kbs/{RETAIL}/access/arjun.mehta", headers=priya).status_code == 200
    assert client.post(f"/api/kbs/{RETAIL}/chat", headers=arjun, json=chat_q).status_code == 403
    assert client.delete(f"/api/kbs/{RETAIL}/access/arjun.mehta", headers=priya).status_code == 404

    # audit trail in Postgres
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, role, granted_by, revoked_at, modified_by, created_at, updated_at "
            "FROM kb_access WHERE kb_name = %s ORDER BY id",
            (RETAIL,),
        ).fetchall()
        kbs = conn.execute("SELECT user_id, access FROM knowledge_bases WHERE kb_name = %s", (RETAIL,)).fetchall()
    assert [(r["user_id"], r["role"], r["granted_by"]) for r in rows] == [
        ("priya.nair", "owner", "system"),
        ("arjun.mehta", "user", "priya.nair"),
    ]
    assert (
        rows[1]["revoked_at"]
        and rows[1]["modified_by"] == "priya.nair"
        and rows[1]["updated_at"] > rows[1]["created_at"]
    )
    assert kbs == [{"user_id": "priya.nair", "access": "owner"}]

    # re-grant after revoke adds a new audit row; the log reads newest first
    assert client.post(f"/api/kbs/{RETAIL}/access", headers=priya, json={"user_id": "arjun.mehta"}).status_code == 201
    access = client.get(f"/api/kbs/{RETAIL}/access", headers=priya).json()
    assert [p["user_id"] for p in access["people"]] == ["priya.nair", "arjun.mehta"]
    assert [e["action"] for e in access["log"]] == ["granted", "revoked", "granted", "created"]
    assert all(e["actor"] == "priya.nair" for e in access["log"])

    # a deactivated user loses access even with a grant
    cli_main(["deactivate-user", "arjun.mehta"])
    assert client.get(f"/api/kbs/{RETAIL}", headers=arjun).status_code == 401


def test_every_route_requires_login(client):
    for method, url in [
        ("get", "/api/kbs"),
        ("post", "/api/kbs"),
        ("get", f"/api/kbs/{RETAIL}"),
        ("get", "/api/jobs/1"),
        ("post", f"/api/kbs/{RETAIL}/chat"),
        ("get", "/api/users/priya.nair"),
        ("get", f"/api/kbs/{RETAIL}/access"),
        ("delete", f"/api/kbs/{RETAIL}/access/x"),
    ]:
        assert getattr(client, method)(url).status_code == 401, url


def test_create_validation(client):
    h = token(client, "priya.nair")
    form = {"kb_type": "graph", "domain": "Retail", "sub_domain": "Supply chain"}
    assert (
        client.post(
            "/api/kbs", headers=h, files=upload("supplier_orders.xlsx"), data={**form, "kb_name": "Bad Name!"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/kbs", headers=h, files=upload("returns_policy.pdf"), data={**form, "kb_name": "t_api_pdf"}
        ).status_code
        == 415
    )
    assert (
        client.post(
            "/api/kbs",
            headers=h,
            files=upload("supplier_orders.xlsx"),
            data={**form, "kb_name": "t_api_x", "domain": " "},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/kbs",
            headers=h,
            files=upload("supplier_orders.xlsx", "finance_ledger.csv"),
            data={**form, "kb_name": "t_api_two"},
        ).status_code
        == 422
    )
    r = create_graph(client, h, "t_api_dup")
    assert r.status_code == 201
    assert create_graph(client, h, "t_api_dup").status_code == 409
    wait(r.json()["job_id"])


@pytest.mark.parametrize(
    "name,file,message",
    [
        ("t_api_bad", "edge_cases/corrupt.xlsx", "not a valid Excel"),
        ("t_api_empty", "edge_cases/empty.csv", "no data rows"),
    ],
)
def test_bad_files_fail_cleanly(client, name, file, message):
    h = token(client, "priya.nair")
    r = create_graph(client, h, name, file)
    assert r.status_code == 201
    job = wait(r.json()["job_id"])
    assert job["status"] == "failed" and message in job["error"]
    kb = client.get(f"/api/kbs/{name}", headers=h).json()
    assert kb["status"] == "failed" and message in kb["status_detail"]
    assert client.get(f"/api/jobs/{job['id']}", headers=h).json()["steps"][0]["status"] == "running"


def test_add_data_checks_and_strict_mode(client):
    h = token(client, "priya.nair")
    build_retail(client, h)
    check = client.post(
        f"/api/kbs/{RETAIL}/add-data/check",
        headers=h,
        files={"file": ("warehouse_update.csv", (SAMPLES / "warehouse_update.csv").read_bytes())},
    ).json()
    assert check["matched"] and check["sheets"][0]["schema_sheet"] == "Inventory" and check["total_rows"] == 56
    r = client.post(
        f"/api/kbs/{RETAIL}/add-data/check",
        headers=h,
        files={"file": ("finance_ledger.csv", (SAMPLES / "finance_ledger.csv").read_bytes())},
    ).json()
    assert r["matched"] is False
    assert client.post(f"/api/kbs/{RETAIL}/add-data", headers=h, files=upload("finance_ledger.csv")).status_code == 422
    # skip_invalid off: nothing written when any row is bad
    before = GraphStore(RETAIL).counts()
    r = client.post(
        f"/api/kbs/{RETAIL}/add-data", headers=h, files=upload("warehouse_update.csv"), data={"skip_invalid": "false"}
    )
    assert wait(r.json()["job_id"])["status"] == "failed"
    assert GraphStore(RETAIL).counts() == before
    run = client.get(f"/api/kbs/{RETAIL}/runs", headers=h).json()[0]
    assert run["status"] == "failed" and run["rows_rejected"] == 12 and "Nothing written" in run["summary"]
    r = client.post(f"/api/kbs/{RETAIL}/add-data", headers=h, files=upload("warehouse_update.csv"))
    assert wait(r.json()["job_id"])["status"] == "succeeded"
    assert GraphStore(RETAIL).counts()["relationships"]["STORED_IN"] == before["relationships"]["STORED_IN"] + 14


def test_rag_kb_end_to_end(client):
    priya, sneha = token(client, "priya.nair"), token(client, "sneha.iyer")
    r = client.post(
        "/api/kbs",
        headers=priya,
        files=upload("returns_policy.pdf", "vendor_handbook.docx", "warehouse_sop.txt"),
        data={"kb_name": RAG, "kb_type": "rag", "domain": "Retail", "sub_domain": "Policies"},
    )
    assert r.status_code == 201, r.text
    job = wait(r.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    kb = client.get(f"/api/kbs/{RAG}", headers=priya).json()
    assert kb["status"] == "ready" and kb["stats"]["documents"] == 3 and kb["stats"]["chunks"] > 3
    runs = client.get(f"/api/kbs/{RAG}/runs", headers=priya).json()
    assert sorted(x["source_file"] for x in runs) == ["returns_policy.pdf", "vendor_handbook.docx", "warehouse_sop.txt"]
    assert all(x["status"] == "completed" and x["chunks_added"] > 0 for x in runs)
    pii = client.get(f"/api/kbs/{RAG}/pii", headers=priya).json()
    by_doc = {(p["source_document"], p["pii_category"]): p["occurrences"] for p in pii}
    assert by_doc[("vendor_handbook.docx", "email")] == 2 and by_doc[("warehouse_sop.txt", "phone")] == 1
    assert all(p["node_label"] is None for p in pii)

    r = client.post(
        f"/api/kbs/{RAG}/chat", headers=priya, json={"question": "What is the restocking fee for electronics?"}
    ).json()
    assert r["kind"] == "rag" and "returns_policy.pdf" in [x["source"] for x in r["sources"][:3]]
    assert r["answer"].startswith("ANSWER") and "[1]" not in r["sources"][0]["snippet"]
    assert client.post(f"/api/kbs/{RAG}/chat", headers=sneha, json={"question": "x"}).status_code == 403

    # add data to a RAG base: same collection, re-upload replaces
    chunks = kb["stats"]["chunks"]
    r = client.post(f"/api/kbs/{RAG}/add-data", headers=priya, files=upload("warehouse_sop.txt"))
    assert wait(r.json()["job_id"])["status"] == "succeeded"
    assert client.get(f"/api/kbs/{RAG}", headers=priya).json()["stats"]["chunks"] == chunks
    assert (
        client.post(f"/api/kbs/{RAG}/add-data", headers=priya, files=upload("supplier_orders.xlsx")).status_code == 415
    )


def test_jobs_visibility_and_cancel(client):
    priya, out = token(client, "priya.nair"), token(client, "outsider")
    r = create_graph(client, priya, "t_api_jobs")
    job_id = r.json()["job_id"]
    wait(job_id)
    assert client.get(f"/api/jobs/{job_id}", headers=out).status_code == 403
    assert client.post(f"/api/jobs/{job_id}/cancel", headers=priya).status_code == 409  # already finished
    assert client.get("/api/jobs/999999", headers=priya).status_code == 404


def test_interrupted_jobs_are_marked_failed(client):
    h = token(client, "priya.nair")
    r = create_graph(client, h, "t_api_restart")
    wait(r.json()["job_id"])
    with get_conn() as conn:
        conn.execute("UPDATE jobs SET status = 'running' WHERE kb_name = 't_api_restart'")
        conn.execute("UPDATE kb_catalog SET status = 'building' WHERE kb_name = 't_api_restart'")
    jobs.recover_interrupted()
    kb = client.get("/api/kbs/t_api_restart", headers=h).json()
    assert kb["status"] == "failed" and "restart" in kb["status_detail"]
    # the owner can re-run extraction from the stored upload
    r = client.post("/api/kbs/t_api_restart/extract", headers=h)
    assert r.status_code == 200 and wait(r.json()["job_id"])["status"] == "succeeded"
