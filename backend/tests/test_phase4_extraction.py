"""Phase 4: schema extraction logic with a scripted LLM (deterministic). The script imitates the
mistakes qwen2.5:3b really makes (everything in ignore_columns, a reused label, a link table
modelled as an entity) so the post-processing that corrects them is tested."""
import re

import pytest

from app import extraction, graph_schema as gs, loader
from app.graphstore import GraphStore
from app.tabular import read_table_file

from .fixtures import SAMPLES, manifest

ALL = object()

RETAIL_ANSWERS = {
    "Suppliers": {"row_entity": {"label": "Suppliers", "key_column": "supplier id", "property_columns": ["Supplier Name"]},
                  "ignore_columns": ALL},  # real 3b behaviour: every column listed as ignored
    "Products": {"row_entity": {"label": "Product", "key_column": "SKU", "property_columns": ["Product", "Category"]},
                 "reference_columns": ["Supplier"]},
    "Warehouses": {"row_entity": {"label": "Warehouse", "key_column": "WH Code", "property_columns": ["City"]}},
    "Customers": {"row_entity": {"label": "Customer", "key_column": "customer_id", "property_columns": ["Name"]}},
    "Orders": {"row_entity": {"label": "Customer", "key_column": "customer_id", "property_columns": []},  # wrong label
               "ignore_columns": ["row_no", "notes"]},
    "Inventory": {"row_entity": {"label": "Inventory", "key_column": "sku", "property_columns": ["stock_qty"]}},
}
REL_NAMES = {("Product", "Supplier"): ("SUPPLIES", "Supplier", "Product"),
             ("Product", "Product"): ("SUBSTITUTE_OF", "Product", "Product"),
             ("Product", "Warehouse"): ("STORED_IN_THE_WAREHOUSE_LOCATION", "Product", "Warehouse"),
             ("Order", "Customer"): ("PLACED", "Customer", "Order"),
             ("Order", "Product"): ("CONTAINS", "Order", "Product"),
             ("Order", "Warehouse"): ("SHIPPED_FROM", "Order", "Warehouse")}
PERSON_COLUMNS = {"Contact Person", "Manager", "Name", "Address", "DOB", "Bank Account"}


def scripted_llm(answers):
    def ask(system, prompt, retries=1):
        m = re.search(r'Decide what one row of sheet "([^"]+)"', prompt)
        if m:
            sheet = read_sheets[m.group(1)]
            a = dict(answers.get(m.group(1), {}))
            if a.get("ignore_columns") is ALL:
                a["ignore_columns"] = list(sheet.columns)
            return a
        if "UPPER_SNAKE_CASE" in prompt:
            out = []
            for i, a, b in re.findall(r"^(\d+)\. (\w+) and (\w+):", prompt, re.M):
                t, f, to = REL_NAMES.get((a, b)) or REL_NAMES.get((b, a)) or ("LINKS", a, b)
                out.append({"id": int(i), "type": t, "from": f, "to": to})
            return {"relationships": out}
        if "Classify every column" in prompt:
            cols = re.findall(r'^- "([^"]+)"', prompt, re.M)
            # like the real model: right on people, wrong on a company name and on dates
            wrong = {"Supplier Name": "person_name", "Onboarded": "date_of_birth", "City": "address",
                     "customer_id": "government_id"}
            return {"columns": [{"column": c, "confidence": 0.9, "category":
                                 "person_name" if c in ("Contact Person", "Manager", "Name", "Approved By")
                                 else "address" if c == "Address" else "date_of_birth" if c == "DOB"
                                 else "bank_account" if c == "Bank Account" else wrong.get(c, "none")}
                                for c in cols]}
        raise AssertionError(f"unexpected prompt: {prompt[:120]}")
    return ask


read_sheets = {}


@pytest.fixture(scope="module")
def retail_sheets():
    sheets = read_table_file(SAMPLES / "supplier_orders.xlsx")
    read_sheets.update({s.name: s for s in sheets})
    return sheets


@pytest.fixture
def extracted(retail_sheets, monkeypatch):
    monkeypatch.setattr(extraction, "ask_json", scripted_llm(RETAIL_ANSWERS))
    steps = []
    schema = extraction.extract(retail_sheets, "supplier_orders.xlsx", step=lambda *a: steps.append(a))
    return schema, steps


def rel(schema, a, b):
    return [r for r in schema["relationships"] if {r["from"]["label"], r["to"]["label"]} == {a, b}
            and (a != b or r["from"]["label"] == r["to"]["label"])]


def test_nodes_recovered_from_bad_answers(extracted):
    schema, _ = extracted
    nodes = {n["label"]: n for n in schema["nodes"]}
    assert set(nodes) == {"Supplier", "Product", "Warehouse", "Customer", "Order"}
    assert nodes["Order"]["key"]["column"] == "order_id"  # reused "Customer" label corrected
    assert nodes["Supplier"]["key"] == {"name": "supplier_id", "column": "Supplier ID"}  # case-fixed column
    sup_cols = {p["column"] for p in nodes["Supplier"]["properties"]}
    assert {"Supplier Name", "Contact Email", "Rating (1-5)"} <= sup_cols  # "ignore everything" not honoured
    assert gs.validate(schema) == []


def test_relationships_from_value_overlap(extracted):
    schema, _ = extracted
    (sup,) = rel(schema, "Supplier", "Product")
    assert (sup["type"], sup["from"], sup["to"]) == ("SUPPLIES", {"label": "Supplier", "column": "Supplier"},
                                                     {"label": "Product", "column": "SKU"})
    (sub,) = rel(schema, "Product", "Product")
    assert sub["required"] is False and sub["to"]["column"] == "Substitute SKU"  # 10% filled -> optional
    (stock,) = rel(schema, "Product", "Warehouse")
    assert stock["type"] == "STORED_IN"  # long model name shortened
    assert stock["sheet"] == "Inventory" and {p["column"] for p in stock["properties"]} == {"stock_qty", "last_counted"}
    (contains,) = rel(schema, "Order", "Product")
    assert {p["column"] for p in contains["properties"]} >= {"qty"}  # line-level column moved to relationship
    order = next(n for n in schema["nodes"] if n["label"] == "Order")
    order_cols = {p["column"] for p in order["properties"]}
    assert "qty" not in order_cols and {"status", "order_date"} <= order_cols
    assert rel(schema, "Order", "Customer")[0]["from"]["label"] == "Customer"
    assert rel(schema, "Order", "Warehouse")
    assert all("row_no" not in {p["column"] for p in x.get("properties", [])}
               for x in schema["nodes"] + schema["relationships"])  # junk column dropped


def test_pii_from_llm_and_rules(extracted):
    schema, _ = extracted
    found = {(p["sheet"], p["column"]): p for p in schema["pii"]}
    expected = manifest()["files"]["supplier_orders.xlsx"]["pii"]
    for sheet, cols in expected.items():
        for col, cat in cols.items():
            assert (sheet, col) in found, f"missed PII {sheet}.{col}"
    assert found[("Customers", "Email")]["detected_by"] == "rules"  # scripted LLM didn't say it; patterns did
    assert found[("Customers", "PAN")]["category"] == "government_id"
    assert found[("Orders", "notes")]["category"] == "free_text"
    for fp in [("Suppliers", "Supplier Name"), ("Suppliers", "Onboarded"), ("Warehouses", "City"),
               ("Orders", "customer_id"), ("Products", "SKU")]:
        assert fp not in found, f"false positive {fp} not rejected by the evidence check"
    assert found[("Suppliers", "Contact Person")]["detected_by"] == "llm"
    targets = {(t["node_label"], t["property_name"]) for t in extraction.pii_targets(schema)}
    assert ("Customer", "email") in targets and ("CONTAINS", "notes") in targets


def test_progress_steps_reported(extracted):
    _, steps = extracted
    assert [s for s in steps if s[1] == "done"] and {s[0] for s in steps} == {1, 2, 3, 4}


def test_extracted_schema_loads_to_manifest_counts(extracted):
    """End to end: the corrected LLM schema builds the same graph as the reviewed one."""
    schema, _ = extracted
    store = GraphStore("t_llmschema", mode="single")
    store.drop()
    try:
        plan = loader.plan_load(schema, list(read_sheets.values()), {})
        loader.execute_plan(store, schema, plan)
        e = manifest()["files"]["supplier_orders.xlsx"]
        counts = store.counts()
        assert counts["nodes"] == {"Supplier": 48, "Product": 307, "Warehouse": 9, "Customer": 120,
                                   "Order": e["expected_nodes"]["Orders.order_id"]}
        assert counts["relationships"]["CONTAINS"] == e["expected_relationships"]["order->product (line)"]
    finally:
        store.drop()


def test_garbage_llm_still_gives_editable_draft(retail_sheets, monkeypatch):
    def broken(system, prompt, retries=1):
        raise ValueError("model returned nonsense")
    monkeypatch.setattr(extraction, "ask_json", broken)
    schema = extraction.extract(retail_sheets, "supplier_orders.xlsx")
    assert gs.validate(schema) == []
    labels = {n["label"] for n in schema["nodes"]}
    assert {"Supplier", "Product", "Warehouse", "Customer", "Order"} <= labels
    assert len(schema["relationships"]) >= 5  # value overlap still finds the links
    assert {p["column"] for p in schema["pii"]} >= {"Contact Email", "Email", "PAN", "Phone"}  # rules still work


def test_finance_single_sheet_with_embedded_entities(monkeypatch):
    sheets = read_table_file(SAMPLES / "finance_ledger.csv")
    read_sheets.update({s.name: s for s in sheets})
    answers = {"finance_ledger": {
        "row_entity": {"label": "LedgerEntry", "key_column": "Entry No", "property_columns": ["Amount"]},
        "embedded_entities": [{"label": "Vendor", "key_column": "Vendor", "property_columns": []},
                              {"label": "Approver", "key_column": "Approved By", "property_columns": ["Approver Email"]},
                              {"label": "Account", "key_column": "GL Account", "property_columns": ["Account Name"]}]}}
    monkeypatch.setattr(extraction, "ask_json", scripted_llm(answers))
    schema = extraction.extract(sheets, "finance_ledger.csv")
    labels = {n["label"]: n for n in schema["nodes"]}
    assert set(labels) == {"LedgerEntry", "Vendor", "Approver", "Account"}
    assert labels["Vendor"]["role"] == "embedded"
    assert {r["to"]["label"] for r in schema["relationships"]} >= {"Vendor", "Approver", "Account"}
    assert "Approver Email" in {p["column"] for p in schema["pii"]}


def test_rule_pii_ignores_company_names():
    s = read_table_file(SAMPLES / "supplier_orders.xlsx")[0]
    assert extraction.rule_pii(s, "Supplier Name") is None
    assert extraction.rule_pii(s, "Contact Email")["category"] == "email"
    assert extraction.rule_pii(s, "Contact Phone")["category"] == "phone"
