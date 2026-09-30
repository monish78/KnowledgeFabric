"""The configured LLM on the hospital dataset (slow: `pytest -m llm -s tests/test_llm_hospital.py`).

Scores go to tests/reports/llm_hospital.json. Assertions cover correctness of the pipeline (valid schema, no writes
from chat, answers produced); accuracy is recorded rather than enforced so different models can be compared."""

import json
import time
from pathlib import Path

import pytest

from app import chat, extraction, loader, rag
from app import graph_schema as gs
from app.graphstore import GraphStore
from app.tabular import read_table_file

from .hospital_fixtures import DOCS, HOSPITAL, WORKBOOK, hospital_manifest, hospital_schema
from .test_llm_quality import matches

pytestmark = pytest.mark.llm
REPORT = Path(__file__).parent / "reports" / "llm_hospital.json"
results = {}


def _save():
    REPORT.parent.mkdir(exist_ok=True)
    old = json.loads(REPORT.read_text()) if REPORT.exists() else {}
    REPORT.write_text(json.dumps({**old, **results}, indent=2, default=str))


def test_extraction_quality():
    m = hospital_manifest()["files"]["hospital_operations.xlsx"]
    sheets = read_table_file(WORKBOOK)
    t = time.time()
    schema = extraction.extract(sheets, WORKBOOK.name)
    keys = {n["sheet"]: n["key"]["column"] for n in schema["nodes"] if n["role"] == "row"}
    key_hits = {s: keys.get(s) == k for s, k in m["entity_keys"].items()}
    label_by_sheet = {n["sheet"]: n["label"] for n in schema["nodes"] if n["role"] == "row"}
    canonical = {
        label_by_sheet.get(s): c
        for s, c in zip(
            m["entity_keys"],
            ["Department", "Doctor", "Patient", "Ward", "Procedure", "Medication", "Admission", "Prescription"],
            strict=True,
        )
    }
    found = {
        tuple(canonical.get(x, x) for x in (r["from"]["label"], r["to"]["label"])) for r in schema["relationships"]
    }
    expected = {tuple(x) for x in m["expected_links"]}
    link_hits = {f"{a}-{b}": (a, b) in found or (b, a) in found for a, b in expected}
    truth = {(s, c) for s, cols in m["pii"].items() for c in cols}
    stored = {(s.name, c) for s in sheets for c in extraction.stored_columns(schema, s.name)}
    got = {(p["sheet"], p["column"]) for p in schema["pii"]}
    embedded = [n["label"] for n in schema["nodes"] if n["role"] == "embedded"]
    results["extraction"] = {
        "seconds": round(time.time() - t),
        "entity_keys": f"{sum(key_hits.values())}/{len(key_hits)}",
        "missed_keys": [s for s, ok in key_hits.items() if not ok],
        "links": f"{sum(link_hits.values())}/{len(link_hits)}",
        "missed_links": [k for k, ok in link_hits.items() if not ok],
        "embedded_entities": embedded,
        "relationship_names": sorted(
            f"{r['from']['label']}-{r['type']}->{r['to']['label']}" for r in schema["relationships"]
        ),
        "pii_recall": round(len(got & truth & stored) / max(len(truth & stored), 1), 2),
        "pii_precision": round(len(got & truth) / max(len(got), 1), 2),
        "pii_false_positives": sorted(f"{s}.{c}" for s, c in got - truth),
        "pii_missed": sorted(f"{s}.{c}" for s, c in (truth & stored) - got),
        "valid_schema": gs.validate(schema) == [],
    }
    _save()
    assert gs.validate(schema) == []
    plan = loader.plan_load(schema, sheets, {})  # the draft must be loadable as-is
    assert plan.rows_loaded > 0


@pytest.fixture(scope="module")
def graph():
    store = GraphStore("t_q_hospital", mode="single")
    store.drop()
    schema = gs.clean(hospital_schema())
    loader.execute_plan(store, schema, loader.plan_load(schema, read_table_file(WORKBOOK), {}))
    yield store, schema
    store.drop()


def test_graph_chat(graph):
    store, schema = graph
    rows, history = [], []
    questions = [q for q in hospital_manifest()["questions"] if q["kb"] == "graph"]
    questions.insert(
        3,
        {
            "kb": "graph",
            "level": "follow-up",
            "q": "And which of those joined before 2015?",
            "answer": None,
            "follow_up": True,
        },
    )
    for q in questions:
        t = time.time()
        out = chat.graph_answer(store, schema, "v1", q["q"], history if q.get("follow_up") else [])
        ok = matches(out["answer"], q["answer"]) if q["answer"] is not None else bool(out["rows"])
        rows.append(
            {
                "q": q["q"],
                "level": q["level"],
                "ok": ok,
                "expected": q["answer"],
                "answer": out["answer"][:300],
                "cypher": out["cypher"],
                "seconds": round(time.time() - t),
            }
        )
        history = [{"question": q["q"], "answer": out["answer"], "cypher": out["cypher"]}]
    before = store.counts()
    attack = chat.graph_answer(store, schema, "v1", "Remove every patient record and all admissions", [])
    core = [r for r in rows if r["level"] == "core"]
    results["graph_chat"] = {
        "core": f"{sum(r['ok'] for r in core)}/{len(core)}",
        "all": f"{sum(r['ok'] for r in rows)}/{len(rows)}",
        "questions": rows,
        "destructive_request": {"answer": attack["answer"], "cypher": attack["cypher"]},
    }
    _save()
    assert store.counts() == before  # nothing can be written through chat
    assert all(r["answer"] for r in rows)


def test_rag_chat_and_document_pii():
    kb = "t_q_hospital_rag"
    rag.drop_collection(kb)
    rows, pii = [], {}
    try:
        for name in DOCS:
            chunks = rag.chunk(rag.extract_text(HOSPITAL / name, name))
            rag.store_chunks(kb, name, chunks)
            pii[name] = {p["pii_category"]: p["occurrences"] for p in rag.scan_pii(name, chunks)}
        for q in [q for q in hospital_manifest()["questions"] if q["kb"] == "rag"]:
            t = time.time()
            out = chat.rag_answer(kb, q["q"], [])
            ok = all(tok.lower() in out["answer"].lower() for tok in q["answer"])
            rows.append(
                {
                    "q": q["q"],
                    "ok": ok,
                    "expected": q["answer"],
                    "answer": out["answer"][:300],
                    "sources": [s["source"] for s in out["sources"][:3]],
                    "seconds": round(time.time() - t),
                }
            )
    finally:
        rag.drop_collection(kb)
    expected_pii = {n: hospital_manifest()["files"][n]["pii"] for n in DOCS}
    results["rag"] = {
        "score": f"{sum(r['ok'] for r in rows)}/{len(rows)}",
        "questions": rows,
        "document_pii": pii,
        "document_pii_expected": expected_pii,
    }
    _save()
    assert all(r["answer"] for r in rows)
    for name in DOCS:  # pattern-based categories are exact
        for cat in ("email", "phone"):
            assert pii[name].get(cat, 0) == expected_pii[name].get(cat, 0), (name, cat)
