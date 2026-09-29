"""Phase 5: RAG ingest — parsing, chunking, Chroma storage, retrieval quality, PII scan."""
import re

import pytest

from app import rag

from .fixtures import SAMPLES, manifest

DOCS = ["returns_policy.pdf", "vendor_handbook.docx", "warehouse_sop.txt"]
FACTS = {  # manifest question -> text the right passage must contain
    "What is the standard return window?": "30 days",
    "What is the restocking fee for electronics?": "12%",
    "What are the vendor payment terms?": "net 45",
    "What is the late delivery penalty?": "2% of the purchase order value",
    "What temperature must the Chennai cold room be kept at?": "between 2 and 8 degrees",
    "What are the inbound dock hours?": "06:00 to 14:00",
}


def test_pdf_headers_and_footers_are_removed():
    pages = rag.extract_text(SAMPLES / "returns_policy.pdf", "returns_policy.pdf")
    assert len(pages) >= 2 and pages[0][0] == 1
    text = "\n".join(t for _, t in pages)
    assert "CONFIDENTIAL" not in text and "Page 1" not in text
    assert "Electronics" in text and "12%" in text  # table text survives


def test_docx_tables_and_txt_unicode():
    (_, docx), = rag.extract_text(SAMPLES / "vendor_handbook.docx", "vendor_handbook.docx")
    assert "On time in full (OTIF) | 95% or higher" in docx
    (_, txt), = rag.extract_text(SAMPLES / "warehouse_sop.txt", "warehouse_sop.txt")
    assert "\r" not in txt and "குளிர் அறை" in txt


@pytest.mark.parametrize("name,message", [("corrupt.xlsx", "supported"), ("empty.csv", "supported")])
def test_unsupported_documents_rejected(name, message):
    with pytest.raises(rag.DocumentError, match=message):
        rag.extract_text(SAMPLES / "edge_cases" / name, name)


def test_corrupt_pdf_is_a_clear_error(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 garbage")
    with pytest.raises(rag.DocumentError):
        rag.extract_text(bad, "bad.pdf")


@pytest.fixture(scope="module")
def indexed():
    kb = "t_rag"
    rag.drop_collection(kb)
    for name in DOCS:
        rag.store_chunks(kb, name, rag.chunk(rag.extract_text(SAMPLES / name, name)))
    yield kb
    rag.drop_collection(kb)


def test_documents_indexed(indexed):
    docs = rag.documents(indexed)
    assert set(docs) == set(DOCS) and all(n > 0 for n in docs.values())


def test_reupload_replaces_instead_of_duplicating(indexed):
    before = rag.documents(indexed)["warehouse_sop.txt"]
    rag.store_chunks(indexed, "warehouse_sop.txt", rag.chunk(rag.extract_text(SAMPLES / "warehouse_sop.txt", "warehouse_sop.txt")))
    assert rag.documents(indexed)["warehouse_sop.txt"] == before


@pytest.mark.parametrize("question", list(FACTS))
def test_retrieval_finds_the_fact(indexed, question):
    assert question in {q["q"] for q in manifest()["questions"]}
    top = rag.retrieve(indexed, question, k=3)
    assert any(FACTS[question].lower() in p["text"].lower() for p in top), [p["text"][:80] for p in top]


def test_pii_scan_counts_by_category(monkeypatch):
    people = {"Deepa Menon", "Karthik Pillai", "Priya Nair", "Venkat Rao"}
    monkeypatch.setattr(rag, "ask_json", lambda s, u, retries=1: {
        "people": [c for c in re.findall(r"^- (.+)$", u, re.M) if c in people] + ["Invented Person"]})
    for name in DOCS:
        chunks = rag.chunk(rag.extract_text(SAMPLES / name, name))
        found = {p["pii_category"]: p for p in rag.scan_pii(name, chunks)}
        expected = manifest()["files"][name]["pii"]
        for cat in ("email", "phone"):
            assert found.get(cat, {}).get("occurrences", 0) == expected.get(cat, 0), (name, cat)
        assert found["person_name"]["occurrences"] == expected["person_name"]  # invented names are dropped
        assert all(p["reason"] and "@" not in p["reason"] and "Nair" not in p["reason"] for p in found.values())
