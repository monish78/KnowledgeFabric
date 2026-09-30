"""Browser tests of the React screens against the running stack (real backend, real LLM).

Prerequisites: `docker compose up -d` and `docker compose exec backend python -m app.cli seed-demo-data`.
Run from the repo root:  .venv/bin/python -m pytest e2e -q
Screenshots of every screen are written to e2e/screenshots/ for comparison with docs/mockups/.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "samples"
SHOTS = Path(__file__).parent / "screenshots"
BASE = os.environ.get("GRAPHBASE_URL", "http://localhost:5173")
LLM_TIMEOUT = 600_000  # ms; generous because local models on CPU are slow


def backend_python(code: str) -> str:
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "backend", "python", "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout


def reset_kb(name: str):
    backend_python(f"""
from app.db import get_conn, open_pool
from app.graphstore import GraphStore
from app import rag
open_pool()
GraphStore('{name}').drop(); rag.drop_collection('{name}')
with get_conn() as c:
    for t in ('pipeline_runs', 'jobs', 'kb_pii_fields', 'kb_access', 'knowledge_bases'):
        c.execute(f"DELETE FROM {{t}} WHERE kb_name = %s", ('{name}',))
    c.execute("DELETE FROM kb_catalog WHERE kb_name = %s", ('{name}',))
""")


@pytest.fixture(scope="session")
def browser():
    SHOTS.mkdir(exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    pg.on("dialog", lambda d: d.accept())
    yield pg
    ctx.close()


def login(page: Page, user="priya.nair", password="test1234"):
    page.goto(f"{BASE}/login")
    page.get_by_label("User ID").fill(user)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()
    expect(page).to_have_url(re.compile(r"/workspace"))


def shot(page: Page, name: str):
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=False)


# ------------------------------------------------------------------ 1. login
def test_login_rejects_bad_password_then_signs_in(page):
    page.goto(f"{BASE}/workspace")
    expect(page).to_have_url(re.compile(r"/login\?next="))  # protected route
    shot(page, "1_login")
    page.get_by_label("User ID").fill("priya.nair")
    page.get_by_label("Password").fill("wrong")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("alert")).to_contain_text("Incorrect user ID or password")
    page.get_by_label("Password").fill("test1234")
    page.get_by_role("button", name="Sign in").click()
    expect(page).to_have_url(re.compile(r"/workspace"))
    expect(page.get_by_text("Priya Nair")).to_be_visible()
    # the session lives on the server: scripts can't read the cookie and nothing is kept in storage
    assert "graphbase_session" not in page.evaluate("document.cookie")
    assert page.evaluate("sessionStorage.length + localStorage.length") == 0
    cookie = next(c for c in page.context.cookies() if c["name"] == "graphbase_session")
    assert cookie["httpOnly"] and cookie["sameSite"] == "Lax"


# ------------------------------------------------------------------ 2. workspace
def test_workspace_lists_bases_with_roles(page):
    login(page)
    rows = page.locator("tbody tr")
    expect(rows.filter(has_text="retail_supply_chain_kg")).to_contain_text("Owner")
    expect(rows.filter(has_text="retail_supply_chain_kg").get_by_role("button", name="Manage access")).to_be_visible()
    expect(rows.filter(has_text="finance_ledger_kg")).to_contain_text("User")
    expect(rows.filter(has_text="finance_ledger_kg").get_by_role("button", name="Chat")).to_be_visible()
    expect(rows.filter(has_text="retail_policies_rag")).to_contain_text("RAG")
    shot(page, "2_workspace")


# ------------------------------------------------------------------ 3 + 4. upload -> extraction (real LLM) -> review
def test_upload_extract_review_submit(page):
    reset_kb("e2e_inventory_kg")
    login(page)
    page.locator("input[type=file][accept='.csv,.xlsx,.xlsm']").set_input_files(str(SAMPLES / "warehouse_update.csv"))
    page.get_by_label("Knowledge graph name").fill("e2e_inventory_kg")
    page.get_by_label("Domain", exact=True).fill("Retail")
    page.get_by_label("Sub-domain").fill("Inventory")
    page.get_by_role("button", name="Create and extract graph").click()
    expect(page).to_have_url(re.compile(r"/kbs/e2e_inventory_kg/jobs/\d+"))
    expect(page.get_by_text("Extracting your knowledge graph")).to_be_visible()
    expect(page.get_by_text("LLM identifies nodes and entities")).to_be_visible()
    shot(page, "3_processing")
    expect(page).to_have_url(re.compile(r"/kbs/e2e_inventory_kg/review"), timeout=LLM_TIMEOUT)
    expect(page.get_by_text("Review extracted graph")).to_be_visible()
    expect(page.locator(".code pre")).to_contain_text("MERGE")
    # submit whatever the model proposed; the build must succeed and land in chat
    page.get_by_role("button", name="Submit and build graph").click()
    expect(page).to_have_url(re.compile(r"/chat\?kb=e2e_inventory_kg"), timeout=LLM_TIMEOUT)


def test_review_edit_delete_undo_live_cypher(page):
    reset_kb("supplier_orders_review_kg")
    backend_python("""
from app import demo
from app.db import open_pool
open_pool()
demo._graph('supplier_orders_review_kg', 'priya.nair', 'Retail', 'Supply chain', demo.retail_schema(), False,
            approve=False)
""")
    login(page)
    page.goto(f"{BASE}/kbs/supplier_orders_review_kg/review")
    nodes = page.locator("table").nth(0)
    rels = page.locator("table").nth(1)
    expect(nodes.locator("tbody tr")).to_have_count(5)
    expect(rels.locator("tbody tr")).to_have_count(6)
    expect(page.locator(".badge.pii").first).to_be_visible()  # PII flagged on properties
    # edit a label -> preview follows
    nodes.locator("tr", has_text="Warehouse").get_by_role("button", name="Edit").click()
    page.get_by_label("Label").fill("Depot")
    nodes.get_by_role("button", name="Save").click()
    expect(page.locator(".code pre")).to_contain_text("MERGE (n:Depot", timeout=10_000)
    expect(rels).to_contain_text("Depot")
    # delete a node type -> its relationships are removed too; undo restores
    nodes.locator("tr", has_text="Customer").get_by_role("button", name="Delete").click()
    expect(nodes.locator("tr.removed")).to_have_count(1)
    expect(rels.locator("tr.removed")).to_have_count(1)  # PLACED
    expect(page.get_by_text(re.compile(r"3 edits and 2 removals pending"))).to_be_visible()  # rename touches 2 rels
    shot(page, "4_review")
    nodes.locator("tr.removed").get_by_role("button", name="Undo").click()
    expect(rels.locator("tr.removed")).to_have_count(0)
    page.get_by_role("button", name="Discard").click()
    expect(page.locator(".code pre")).to_contain_text("MERGE (n:Warehouse")


# ------------------------------------------------------------------ 5. access
def test_grant_and_revoke_with_audit_log(page):
    login(page)
    page.goto(f"{BASE}/access?kb=retail_supply_chain_kg")
    expect(page.get_by_text("You own this knowledge base.")).to_be_visible()
    people = page.locator("table tbody")
    if people.locator("tr", has_text="meera.s").count():
        people.locator("tr", has_text="meera.s").get_by_role("button", name="Revoke").click()
        expect(people.locator("tr", has_text="meera.s")).to_have_count(0)
    page.get_by_label("User ID").fill("meera.s")
    page.get_by_role("button", name="Grant access").click()
    expect(people.locator("tr", has_text="meera.s")).to_contain_text("priya.nair")
    expect(page.get_by_text(re.compile(r"priya\.nair\s+granted\s+meera\.s\s+access")).first).to_be_visible()
    shot(page, "5_access")
    people.locator("tr", has_text="meera.s").get_by_role("button", name="Revoke").click()
    expect(people.locator("tr", has_text="meera.s")).to_have_count(0)
    expect(page.get_by_text(re.compile(r"revoked access for\s+meera\.s")).first).to_be_visible()
    page.get_by_label("User ID").fill("nobody.here")
    page.get_by_role("button", name="Grant access").click()
    expect(page.get_by_role("alert")).to_contain_text("No active user")


# ------------------------------------------------------------------ 6. add data
def test_add_data_with_rejected_rows_report(page):
    login(page)
    page.goto(f"{BASE}/add-data?kb=retail_supply_chain_kg")
    page.locator("input[type=file]").set_input_files(str(SAMPLES / "supplier_orders_october.xlsx"))
    expect(page.get_by_text("Schema matched")).to_be_visible(timeout=30_000)
    expect(page.get_by_text(re.compile(r"1,230 rows"))).to_be_visible()
    page.get_by_role("button", name="Ingest data").click()
    first = page.locator("text=supplier_orders_october.xlsx").first
    expect(first).to_be_visible()
    expect(page.get_by_text("10 rows rejected").first).to_be_visible(timeout=120_000)
    shot(page, "6_add_data")
    page.get_by_role("button", name="View report").first.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.locator("tbody tr")).to_have_count(10)
    expect(dialog).to_contain_text("unknown Customer")


# ------------------------------------------------------------------ 7. chat (real LLM)
def test_graph_chat_shows_answer_cypher_and_path(page):
    login(page)
    page.goto(f"{BASE}/chat?kb=retail_supply_chain_kg")
    page.get_by_label("Question").fill("How many suppliers are there?")
    page.get_by_role("button", name="Send").click()
    expect(page.get_by_text("Querying the graph…")).to_be_visible()
    expect(page.locator(".bubble.bot").last).to_contain_text("48", timeout=LLM_TIMEOUT)
    expect(page.locator(".code").last).to_contain_text("Supplier")
    shot(page, "7_chat")


def test_rag_chat_cites_sources(page):
    login(page)
    page.goto(f"{BASE}/chat?kb=retail_policies_rag")
    page.get_by_label("Question").fill("What is the restocking fee for electronics?")
    page.get_by_role("button", name="Send").click()
    expect(page.locator(".bubble.bot").last).to_contain_text("12", timeout=LLM_TIMEOUT)
    expect(page.get_by_text("Sources")).to_be_visible()
    expect(page.get_by_text(re.compile(r"returns_policy\.pdf")).first).to_be_visible()
    shot(page, "7_chat_rag")


# ------------------------------------------------------------------ roles in the UI
def test_user_role_and_outsider(page, browser):
    login(page, "arjun.mehta")
    row = page.locator("tbody tr", has_text="retail_supply_chain_kg")
    expect(row).to_contain_text("User")
    page.goto(f"{BASE}/kbs/retail_supply_chain_kg/review")
    expect(page.get_by_role("alert")).to_contain_text("Only the owner")
    other = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
    login(other, "rohan.d")
    expect(other.get_by_text("No knowledge bases yet")).to_be_visible()
    other.goto(f"{BASE}/chat?kb=retail_supply_chain_kg")
    expect(other.get_by_text("You have no knowledge bases that are ready for chat.")).to_be_visible()


def test_sign_out(page):
    login(page)
    page.get_by_role("button", name="Sign out").click()
    expect(page).to_have_url(re.compile(r"/login"))
    page.goto(f"{BASE}/workspace")
    expect(page).to_have_url(re.compile(r"/login"))


# ------------------------------------------------------------------ Keycloak redirect login (PKCE)
@pytest.fixture
def keycloak_backend():
    env = {**os.environ, "AUTH_PROVIDER": "keycloak"}
    subprocess.run(["docker", "compose", "up", "-d", "backend"], cwd=ROOT, env=env, check=True, capture_output=True)
    _wait_backend("keycloak")
    yield
    subprocess.run(["docker", "compose", "up", "-d", "backend"], cwd=ROOT, check=True, capture_output=True)
    _wait_backend("local")


def _wait_backend(provider):
    import json
    import time
    import urllib.request

    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{BASE}/api/auth/config", timeout=2) as r:
                if json.load(r)["provider"] == provider:
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"backend did not come up in {provider} mode")


def test_keycloak_redirect_login(page, keycloak_backend):
    page.goto(f"{BASE}/login")
    expect(page.get_by_text("You will be sent to the company sign-in page")).to_be_visible()
    page.get_by_role("button", name="Sign in").click()
    expect(page).to_have_url(re.compile(r"localhost:8080/realms/graphbase/protocol/openid-connect/auth"))
    page.locator("#username").fill("priya.nair")
    page.locator("#password").fill("test1234")
    page.locator("#kc-login").click()
    expect(page).to_have_url(re.compile(r"/workspace"), timeout=30_000)
    expect(page.get_by_text("Priya Nair")).to_be_visible()
    expect(page.locator("tbody tr", has_text="retail_supply_chain_kg")).to_contain_text("Owner")
    page.get_by_role("button", name="Sign out").click()
    expect(page).to_have_url(re.compile(r"/login"), timeout=30_000)


def test_review_edits_are_built_into_the_graph(page):
    """Edit, rename, remove, delete and add on the Review screen, submit, then check Neo4j and Postgres."""
    kb = "e2e_edited_kg"
    reset_kb(kb)
    backend_python(f"""
from app import demo
from app.db import open_pool
open_pool()
demo._graph('{kb}', 'priya.nair', 'Retail', 'Supply chain', demo.retail_schema(), False, approve=False)
""")
    login(page)
    page.goto(f"{BASE}/kbs/{kb}/review")
    nodes, rels = page.locator("table").nth(0), page.locator("table").nth(1)
    expect(nodes.locator("tbody tr")).to_have_count(5)

    # rename a node type, rename one property, remove another
    nodes.locator("tr", has_text="Warehouse").get_by_role("button", name="Edit").click()
    page.get_by_label("Label", exact=True).fill("Depot")
    page.get_by_label("Property City").fill("town")
    page.get_by_role("button", name="Remove manager_phone").click()
    nodes.get_by_role("button", name="Save").click()
    # rename a relationship type
    rels.locator("tr", has_text="SUPPLIES").get_by_role("button", name="Edit").click()
    page.get_by_label("Relationship type").fill("PROVIDES")
    rels.get_by_role("button", name="Save").click()
    # delete a node type (its PLACED relationship goes with it)
    nodes.locator("tr", has_text="Customer").get_by_role("button", name="Delete").click()
    # add a node type and a relationship to it
    page.get_by_role("button", name="Add node type").click()
    add = nodes.locator("tr.editing")
    add.get_by_label("Sheet", exact=True).select_option("Suppliers")
    add.get_by_label("New label", exact=True).fill("Country")
    add.get_by_label("Key column", exact=True).select_option("Country")
    add.get_by_role("button", name="Add").click()
    page.get_by_role("button", name="Add relationship").click()
    add = rels.locator("tr.editing")
    add.get_by_label("Sheet", exact=True).select_option("Suppliers")
    add.get_by_label("From", exact=True).select_option("Supplier")
    add.get_by_label("From column", exact=True).select_option("Supplier ID")
    add.get_by_label("Type", exact=True).fill("LOCATED_IN")
    add.get_by_label("To", exact=True).select_option("Country")
    add.get_by_label("To column", exact=True).select_option("Country")
    add.get_by_role("button", name="Add").click()

    cypher = page.locator(".code pre")
    page.get_by_role("button", name=re.compile(r"statements shown")).click()  # show all statements
    expect(cypher).to_contain_text("MERGE (n:Depot {warehouse_id: row.warehouse_id}) SET n.town = row.town")
    expect(cypher).to_contain_text("MERGE (n:Country {country: row.country})")
    expect(cypher).to_contain_text("MERGE (a)-[r:PROVIDES]->(b)")
    expect(cypher).to_contain_text("MERGE (a)-[r:LOCATED_IN]->(b)")
    expect(cypher).not_to_contain_text("Customer")
    expect(cypher).not_to_contain_text("manager_phone")
    shot(page, "4_review_edited")
    page.get_by_role("button", name="Submit and build graph").click()
    expect(page).to_have_url(re.compile(rf"/chat\?kb={kb}"), timeout=300_000)

    out = backend_python(f"""
import json
from app.db import get_conn, open_pool
from app.graphstore import GraphStore
open_pool()
s = GraphStore('{kb}')
c = s.counts()
props = s.read_internal("MATCH (d:" + s.label('Depot') + ") RETURN keys(d) AS k LIMIT 1")[0]['k']
with get_conn() as conn:
    pii = [(r['node_label'], r['property_name']) for r in conn.execute(
        "SELECT node_label, property_name FROM kb_pii_fields WHERE kb_name = %s", ('{kb}',))]
print(json.dumps({{"nodes": c['nodes'], "rels": c['relationships'], "depot_props": sorted(props), "pii": pii}}))
""")
    import json

    got = json.loads(out.strip().splitlines()[-1])
    # 1607 + 9: without Customer there is no PLACED rule, so orders missing a customer_id are no longer rejected
    assert got["nodes"] == {"Supplier": 48, "Product": 307, "Depot": 9, "Order": 1616, "Country": 4}
    assert "Customer" not in got["nodes"] and "Warehouse" not in got["nodes"]
    assert got["rels"]["PROVIDES"] == 307 and got["rels"]["LOCATED_IN"] == 48 and "PLACED" not in got["rels"]
    assert "SUPPLIES" not in got["rels"] and got["rels"]["STORED_IN"] > 0  # renamed end label kept its relationships
    assert (
        "town" in got["depot_props"] and "city" not in got["depot_props"] and "manager_phone" not in got["depot_props"]
    )
    pii = {tuple(x) for x in got["pii"]}
    # PII rows follow the edits: the removed property and the deleted node type leave no rows behind
    assert ("Supplier", "contact_email") in pii
    assert not any(label in ("Customer", "Warehouse") or prop == "manager_phone" for label, prop in pii)
    reset_kb(kb)
