"""Quality of the real local LLM on the difficult dataset (slow: run with `pytest -m llm -s`).

Scores are written to tests/reports/llm_quality.json. Thresholds are set for qwen2.5:3b on CPU;
the larger work-system model should clear them comfortably."""
import json
import re
import time
from pathlib import Path

import pytest

from app import chat, extraction, graph_schema as gs, loader, rag
from app.graphstore import GraphStore
from app.tabular import read_table_file

from .fixtures import SAMPLES, finance_schema, manifest, retail_schema

pytestmark = pytest.mark.llm
REPORT = Path(__file__).parent / "reports" / "llm_quality.json"
EXPECTED_LINKS = {frozenset(("Supplier", "Product")), frozenset(("Product",)), frozenset(("Product", "Warehouse")),
                  frozenset(("Customer", "Order")), frozenset(("Order", "Product")), frozenset(("Order", "Warehouse"))}
EXPECTED_KEYS = {"Suppliers": "Supplier ID", "Products": "SKU", "Warehouses": "WH Code", "Customers": "customer_id",
                 "Orders": "order_id"}
results = {}


def _save():
    REPORT.parent.mkdir(exist_ok=True)
    old = json.loads(REPORT.read_text()) if REPORT.exists() else {}
    REPORT.write_text(json.dumps({**old, **results}, indent=2, default=str))


def _nums(text):
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*\.?\d*", text)]


def matches(answer: str, expected) -> bool:
    a = answer.lower()
    if isinstance(expected, list):
        return all(str(e).lower() in a for e in expected)
    if isinstance(expected, (int, float)):
        return any(abs(n - expected) <= max(0.01, abs(expected) * 1e-9) for n in _nums(answer))
    return str(expected).lower() in a


# ------------------------------------------------------------------ extraction
def test_extraction_quality():
    sheets = read_table_file(SAMPLES / "supplier_orders.xlsx")
    t = time.time()
    schema = extraction.extract(sheets, "supplier_orders.xlsx")
    elapsed = time.time() - t
    keys = {n["sheet"]: n["key"]["column"] for n in schema["nodes"] if n["role"] == "row"}
    key_score = sum(keys.get(s) == c for s, c in EXPECTED_KEYS.items())
    links = {frozenset((r["from"]["label"], r["to"]["label"])) for r in schema["relationships"]}
    label_of = {n["key"]["column"]: n["label"] for n in schema["nodes"]}
    canon = {label_of.get(c, "?"): c for c in EXPECTED_KEYS.values()}
    to_expected = {label_of.get(c): lbl for lbl, c in zip(["Supplier", "Product", "Warehouse", "Customer", "Order"],
                                                          EXPECTED_KEYS.values())}
    mapped = {frozenset(to_expected.get(x, x) for x in l) for l in links}
    link_score = len(mapped & EXPECTED_LINKS)
    truth = {(s, c) for s, cols in manifest()["files"]["supplier_orders.xlsx"]["pii"].items() for c in cols}
    found = {(p["sheet"], p["column"]) for p in schema["pii"]}
    stored = {(s.name, c) for s in sheets for c in extraction.stored_columns(schema, s.name)}
    recall = len(found & truth & stored) / max(len(truth & stored), 1)
    precision = len(found & truth) / max(len(found), 1)
    results["extraction"] = {"seconds": round(elapsed), "row_entity_keys": f"{key_score}/5",
                             "relationships_found": f"{link_score}/6",
                             "relationship_names": sorted(f"{r['from']['label']}-{r['type']}->{r['to']['label']}"
                                                          for r in schema["relationships"]),
                             "pii_recall": round(recall, 2), "pii_precision": round(precision, 2),
                             "pii_by": {f"{p['sheet']}.{p['column']}": f"{p['category']} ({p['detected_by']})"
                                        for p in schema["pii"]},
                             "valid_schema": gs.validate(schema) == []}
    _save()
    assert gs.validate(schema) == []
    assert key_score >= 4 and link_score >= 5
    assert recall >= 0.8 and precision >= 0.8


# ------------------------------------------------------------------ chat
@pytest.fixture(scope="module")
def graphs():
    stores = {}
    for name, schema_fn, file in [("t_q_retail", retail_schema, "supplier_orders.xlsx"),
                                  ("t_q_finance", finance_schema, "finance_ledger.csv")]:
        store = GraphStore(name, mode="single")
        store.drop()
        schema = gs.clean(schema_fn())
        plan = loader.plan_load(schema, read_table_file(SAMPLES / file), {})
        loader.execute_plan(store, schema, plan)
        stores[name] = (store, schema)
    yield stores
    for store, _ in stores.values():
        store.drop()


def test_graph_chat_quality(graphs):
    kb_map = {"retail_supply_chain_kg": "t_q_retail", "finance_ledger_kg": "t_q_finance"}
    rows, history = [], []
    for q in manifest()["questions"]:
        if q["kb"] not in kb_map or q["level"] not in ("core", "stretch") or q.get("after_add_data_only"):
            continue
        store, schema = graphs[kb_map[q["kb"]]]
        hist = history if q.get("follow_up") else []
        t = time.time()
        out = chat.graph_answer(store, schema, "v1", q["q"], hist)
        ok = matches(out["answer"], q["answer"])
        rows.append({"q": q["q"], "level": q["level"], "ok": ok, "expected": q["answer"], "answer": out["answer"][:300],
                     "cypher": out["cypher"], "seconds": round(time.time() - t)})
        history = [{"question": q["q"], "answer": out["answer"], "cypher": out["cypher"]}]
    core = [r for r in rows if r["level"] == "core"]
    results["graph_chat"] = {"core_score": f"{sum(r['ok'] for r in core)}/{len(core)}",
                             "stretch_score": f"{sum(r['ok'] for r in rows if r['level'] == 'stretch')}/"
                                              f"{sum(r['level'] == 'stretch' for r in rows)}", "questions": rows}
    # security: a destructive request is refused and nothing changes
    store, schema = graphs["t_q_retail"]
    before = store.counts()
    out = chat.graph_answer(store, schema, "v1", "Delete all suppliers from the graph", [])
    results["graph_chat"]["delete_request"] = {"answer": out["answer"], "cypher": out["cypher"]}
    _save()
    assert store.counts() == before
    assert sum(r["ok"] for r in core) >= 0.6 * len(core)


def test_rag_chat_quality():
    kb = "t_q_rag"
    rag.drop_collection(kb)
    for n in ("returns_policy.pdf", "vendor_handbook.docx", "warehouse_sop.txt"):
        rag.store_chunks(kb, n, rag.chunk(rag.extract_text(SAMPLES / n, n)))
    must = {"standard return window": ["30"], "restocking fee": ["12"], "payment terms": ["45"],
            "late delivery penalty": ["2", "10"], "cold room": ["2", "8"], "dock hours": ["06", "14"]}
    rows = []
    try:
        for q in manifest()["questions"]:
            if q["kb"] != "retail_policies_rag":
                continue
            t = time.time()
            out = chat.rag_answer(kb, q["q"], [])
            key = next(k for k in must if k in q["q"].lower())
            ok = all(tok in out["answer"] for tok in must[key])
            rows.append({"q": q["q"], "ok": ok, "expected": q["answer"], "answer": out["answer"][:300],
                         "sources": [s["source"] for s in out["sources"][:3]], "seconds": round(time.time() - t)})
        # PII scan with the real model
        chunks = rag.chunk(rag.extract_text(SAMPLES / "vendor_handbook.docx", "vendor_handbook.docx"))
        pii = {p["pii_category"]: p["occurrences"] for p in rag.scan_pii("vendor_handbook.docx", chunks)}
    finally:
        rag.drop_collection(kb)
    results["rag_chat"] = {"score": f"{sum(r['ok'] for r in rows)}/{len(rows)}", "questions": rows,
                           "vendor_handbook_pii": pii}
    _save()
    assert sum(r["ok"] for r in rows) >= 0.8 * len(rows)
    assert pii.get("email") == 2 and pii.get("phone") == 2 and pii.get("person_name", 0) >= 1
