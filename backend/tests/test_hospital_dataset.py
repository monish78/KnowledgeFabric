"""The hospital dataset without the LLM: reading, loading, add-data, reference answers, RAG retrieval, PII rules."""

import re

import pytest

from app import extraction, loader, rag
from app import graph_schema as gs
from app.graphstore import GraphStore
from app.tabular import read_table_file

from .hospital_fixtures import DOCS, HOSPITAL, WORKBOOK, hospital_manifest, hospital_schema

REASONS = {
    "missing Adm ID": "missing_adm_id",
    "unknown Patient": "unknown_patient",
    "unknown Procedure": "unknown_procedure",
    "unknown Doctor": "unknown_doctor",
    "unknown Medication": "unknown_medication",
    "unknown Admission": "unknown_admission",
}


def by_reason(plan, sheet):
    out = {}
    for r in plan.rejected:
        if r["sheet"] == sheet:
            key = REASONS.get(r["reason"].split(":")[0], r["reason"])
            out[key] = out.get(key, 0) + 1
    return out


@pytest.fixture(scope="module")
def hospital():
    store = GraphStore("t_hospital", mode="single")
    store.drop()
    schema = gs.clean(hospital_schema())
    plan = loader.plan_load(schema, read_table_file(WORKBOOK), {})
    loader.execute_plan(store, schema, plan)
    yield store, schema, plan
    store.drop()


def one(store, cypher):
    return store.read_internal(cypher)[0]["a"]


def test_reader_skips_non_table_sheets_and_parses_indian_formats():
    sheets = {s.name: s for s in read_table_file(WORKBOOK)}
    spec = hospital_manifest()["files"]["hospital_operations.xlsx"]["sheets"]
    assert "ReadMe" not in sheets
    assert sheets["Dept Master"].header_row == 2
    for name, info in spec.items():
        if isinstance(info, dict):
            assert len(sheets[name].rows) == info["rows"], name
    assert sheets["Admissions"].profile["Cost"].type == "integer"  # "Rs. 1,25,000/-", "INR 125000"
    assert sheets["Doctors"].profile["Joined On"].type == "date"  # "05-Mar-2026" among other formats


def test_counts_match_manifest(hospital):
    store, _, plan = hospital
    e = hospital_manifest()["files"]["hospital_operations.xlsx"]
    counts = store.counts()
    assert counts["nodes"] == e["expected_nodes"]
    assert counts["relationships"] == e["expected_relationships"]
    assert by_reason(plan, "Admissions") == e["expected_rejections"]["Admissions"]
    assert by_reason(plan, "Prescriptions") == e["expected_rejections"]["Prescriptions"]


@pytest.mark.parametrize(
    "prefix",
    [
        "How many patients",
        "Which doctors work",
        "Who is the head",
        "Who supervises",
        "Which patient had",
        "How many admissions",
        "Which medication",
        "How many prescriptions",
        "Which insurer",
        "Which ward",
        "What is the base price",
        "How many procedures were performed",
    ],
)
def test_graph_supports_expected_answers(hospital, prefix):
    store, *_ = hospital
    L = store.label
    q = next(q for q in hospital_manifest()["questions"] if q["q"].startswith(prefix))
    name = re.search(r"(Dr\. [A-Z][a-z]+ [A-Z][a-z]+)", q["q"])
    ref = {
        "How many patients": f"MATCH (p:{L('Patient')}) RETURN count(p) AS a",
        "Which doctors work": f"MATCH (d:{L('Doctor')})-[:WORKS_IN]->(:{L('Department')} {{name: 'Cardiology'}}) "
        "RETURN collect(d.name) AS a",
        "Who is the head": f"MATCH (:{L('Department')} {{name: 'Neurology'}})-[:HEADED_BY]->(d) RETURN d.name AS a",
        "Who supervises": f"MATCH (:{L('Doctor')} {{name: '{name.group(1) if name else ''}'}})-[:SUPERVISED_BY]->(s) "
        "RETURN s.name AS a",
        "Which patient had": f"MATCH (p:{L('Patient')})-[:ADMITTED]->(:{L('Admission')})-[u:UNDERWENT]->() "
        "WITH p, sum(u.cost) AS c RETURN p.name AS a ORDER BY c DESC LIMIT 1",
        "How many admissions": f"MATCH (a:{L('Admission')} {{outcome: 'Transferred'}}) RETURN count(a) AS a",
        "Which medication": f"MATCH (:{L('Prescription')})-[:OF_MEDICATION]->(m) "
        "WITH m.generic_name AS g, count(*) AS c "
        "RETURN g AS a ORDER BY c DESC LIMIT 1",
        "How many prescriptions": f"MATCH (:{L('Prescription')})-[:PRESCRIBED_BY]->(:{L('Doctor')} "
        f"{{name: '{name.group(1) if name else ''}'}}) RETURN count(*) AS a",
        "Which insurer": f"MATCH (:{L('Patient')})-[:INSURED_BY]->(i) WITH i, count(*) AS c RETURN i.name AS a "
        "ORDER BY c DESC LIMIT 1",
        "Which ward": f"MATCH (w:{L('Ward')}) RETURN w.code AS a ORDER BY w.beds DESC LIMIT 1",
        "What is the base price": f"MATCH (p:{L('Procedure')} {{name: 'Total knee replacement'}}) "
        "RETURN p.base_price AS a",
        "How many procedures were performed": f"MATCH (:{L('Department')} {{name: 'Orthopaedics'}})<-[:WORKS_IN]-(d)"
        f"<-[:ADMITTED_BY]-(:{L('Admission')})-[u:UNDERWENT]->() RETURN count(u) AS a",
    }[prefix]
    got = one(store, ref)
    assert (sorted(got) if isinstance(got, list) else got) == q["answer"]


def test_add_data_april(hospital):
    store, schema, _ = hospital
    e = hospital_manifest()["files"]["admissions_april.xlsx"]["expected"]
    before = store.counts()
    plan = loader.plan_load(
        schema, read_table_file(HOSPITAL / "admissions_april.xlsx"), loader.existing_keys_for(store, schema)
    )
    loader.execute_plan(store, schema, plan)
    after = store.counts()
    assert [m.schema_sheet for m in plan.matches] == ["Admissions"]  # lower-case, reordered headers
    assert len(plan.rejected) == sum(e["rejected"].values())
    assert after["nodes"]["Admission"] - before["nodes"]["Admission"] == e["new_admissions"]
    assert after["relationships"]["UNDERWENT"] - before["relationships"]["UNDERWENT"] == e["new_procedure_lines"]


def test_extraction_without_llm_gives_a_loadable_draft(monkeypatch):
    monkeypatch.setattr(extraction, "ask_json", lambda *a, **k: (_ for _ in ()).throw(ValueError("no LLM")))
    sheets = read_table_file(WORKBOOK)
    schema = extraction.extract(sheets, WORKBOOK.name)
    assert gs.validate(schema) == []
    keys = {n["sheet"]: n["key"]["column"] for n in schema["nodes"] if n["role"] == "row"}
    expected = hospital_manifest()["files"]["hospital_operations.xlsx"]["entity_keys"]
    assert sum(keys.get(s) == k for s, k in expected.items()) >= 7
    store = GraphStore("t_hospital_draft", mode="single")
    store.drop()
    try:
        plan = loader.plan_load(schema, sheets, {})
        loader.execute_plan(store, schema, plan)
        assert store.counts()["nodes"].get("Patient", 0) == 400 or 400 in store.counts()["nodes"].values()
    finally:
        store.drop()


@pytest.fixture(scope="module")
def hospital_rag():
    kb = "t_hospital_rag"
    rag.drop_collection(kb)
    for name in DOCS:
        rag.store_chunks(kb, name, rag.chunk(rag.extract_text(HOSPITAL / name, name)))
    yield kb
    rag.drop_collection(kb)


@pytest.mark.parametrize(
    "q", [q for q in hospital_manifest()["questions"] if q["kb"] == "rag"], ids=lambda q: q["q"][:40]
)
def test_rag_retrieval_finds_the_fact(hospital_rag, q):
    top = " ".join(p["text"].lower() for p in rag.retrieve(hospital_rag, q["q"], k=3))
    assert all(tok in top for tok in q["answer"]), q["q"]


def test_document_pii_rules():
    for name in DOCS:
        found = {
            p["pii_category"]: p["occurrences"]
            for p in rag.scan_pii(name, rag.chunk(rag.extract_text(HOSPITAL / name, name)), use_llm=False)
        }
        expected = hospital_manifest()["files"][name]["pii"]
        for cat in ("email", "phone"):
            assert found.get(cat, 0) == expected.get(cat, 0), (name, cat)


def test_invented_embedded_entity_that_is_really_a_reference(monkeypatch):
    """The model calls the Wards 'Dept' column a new entity; its values are Department codes, so it becomes a link."""
    import re as _re

    sheets = read_table_file(WORKBOOK)
    keys = hospital_manifest()["files"]["hospital_operations.xlsx"]["entity_keys"]
    labels = {
        "Dept Master": "Department",
        "Doctors": "Doctor",
        "Patients": "Patient",
        "Wards": "Ward",
        "Procedures": "Procedure",
        "Medications": "Medication",
        "Admissions": "Admission",
        "Prescriptions": "Prescription",
    }

    def scripted(system, prompt, retries=1):
        m = _re.search(r'Decide what one row of sheet "([^"]+)"', prompt)
        if m:
            sheet = m.group(1)
            answer = {"row_entity": {"label": labels[sheet], "key_column": keys[sheet], "property_columns": []}}
            if sheet == "Wards":
                answer["embedded_entities"] = [{"label": "Dept", "key_column": "Dept", "property_columns": []}]
            if sheet == "Patients":
                answer["embedded_entities"] = [{"label": "Insurer", "key_column": "Insurer", "property_columns": []}]
            return answer
        raise ValueError("not scripted")

    monkeypatch.setattr(extraction, "ask_json", scripted)
    schema = extraction.extract(sheets, WORKBOOK.name)
    embedded = {n["label"] for n in schema["nodes"] if n["role"] == "embedded"}
    assert embedded == {"Insurer"}  # a real embedded entity stays
    assert any({r["from"]["label"], r["to"]["label"]} == {"Ward", "Department"} for r in schema["relationships"])
