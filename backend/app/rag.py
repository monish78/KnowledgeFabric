"""RAG documents: parse PDF/DOCX/TXT, chunk, embed into a per-KB Chroma collection, PII scan."""
import hashlib
import logging
import re
from collections import Counter
from pathlib import Path

import chromadb
import chromadb.config
from docx import Document as DocxDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from app.config import get_settings
from app.extraction import _EMAIL, _PHONE, PII_CATEGORIES
from app.llm import ask_json, get_embeddings

log = logging.getLogger(__name__)

SUPPORTED = (".pdf", ".docx", ".txt", ".md")
CHUNK_SIZE, CHUNK_OVERLAP = 900, 150


class DocumentError(ValueError):
    pass


# ------------------------------------------------------------------ parsing
def _strip_repeated_lines(pages: list[str]) -> list[str]:
    """Drop headers/footers: lines (ignoring digits) that repeat on most pages."""
    if len(pages) < 2:
        return pages
    norm = lambda l: re.sub(r"\d+", "#", l.strip())  # noqa: E731
    counts = Counter(n for p in pages for n in {norm(l) for l in p.splitlines() if l.strip()})
    repeated = {l for l, c in counts.items() if c >= max(2, 0.6 * len(pages))}
    return ["\n".join(l for l in p.splitlines() if norm(l) not in repeated) for p in pages]


def extract_text(path: Path, filename: str) -> list[tuple[int | None, str]]:
    """[(page number or None, text)]"""
    name = filename.lower()
    try:
        if name.endswith(".pdf"):
            reader = PdfReader(str(path))
            pages = _strip_repeated_lines([p.extract_text() or "" for p in reader.pages])
            out = [(i, t) for i, t in enumerate(pages, 1) if t.strip()]
            if not out:
                raise DocumentError("The PDF has no extractable text (it may be a scanned image).")
            return out
        if name.endswith(".docx"):
            doc = DocxDocument(str(path))
            parts = []
            body = doc.element.body
            for child in body.iterchildren():
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "p":
                    text = "".join(t.text or "" for t in child.iter() if t.tag.endswith("}t"))
                    if text.strip():
                        parts.append(text)
                elif tag == "tbl":
                    for tr in child.iter():
                        if tr.tag.endswith("}tr"):
                            cells = []
                            for tc in tr.iterchildren():
                                if tc.tag.endswith("}tc"):
                                    cells.append("".join(t.text or "" for t in tc.iter() if t.tag.endswith("}t")).strip())
                            parts.append(" | ".join(cells))
            text = "\n".join(parts)
            if not text.strip():
                raise DocumentError("The document is empty.")
            return [(None, text)]
        if name.endswith((".txt", ".md")):
            data = path.read_bytes()
            for enc in ("utf-8-sig", "cp1252"):
                try:
                    text = data.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if not text.strip():
                raise DocumentError("The file is empty.")
            return [(None, text)]
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f"Could not read {filename}: {type(exc).__name__}") from exc
    raise DocumentError("Only PDF, DOCX and TXT files are supported for RAG.")


def chunk(pages: list[tuple[int | None, str]]) -> list[dict]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
                                              separators=["\n\n", "\n", ". ", " ", ""])
    out = []
    for page, text in pages:
        for piece in splitter.split_text(text):
            if piece.strip():
                out.append({"text": piece.strip(), "page": page})
    return out


# ------------------------------------------------------------------ chroma
def chroma_client():
    s = get_settings()
    return chromadb.HttpClient(host=s.chroma_host, port=s.chroma_port,
                               settings=chromadb.config.Settings(anonymized_telemetry=False))


def collection(kb_name: str):
    return chroma_client().get_or_create_collection(kb_name, metadata={"hnsw:space": "cosine"})


def drop_collection(kb_name: str) -> None:
    try:
        chroma_client().delete_collection(kb_name)
    except Exception:
        pass


def store_chunks(kb_name: str, filename: str, chunks: list[dict], run_id: int | None = None, progress=None) -> int:
    """Replace any earlier version of this document, then add its chunks."""
    col = collection(kb_name)
    col.delete(where={"source": filename})
    emb = get_embeddings()
    doc_id = hashlib.sha1(filename.encode()).hexdigest()[:12]
    batch = 32
    for i in range(0, len(chunks), batch):
        part = chunks[i:i + batch]
        # the file name is embedded with the text so questions naming a document find it
        vectors = emb.embed_documents([f"{filename}\n{c['text']}" for c in part])
        col.add(ids=[f"{doc_id}-{i + j}" for j in range(len(part))],
                documents=[c["text"] for c in part], embeddings=vectors,
                metadatas=[{"source": filename, "page": c["page"] or 0, "chunk": i + j, "run_id": run_id or 0}
                           for j, c in enumerate(part)])
        if progress:
            progress(min(i + batch, len(chunks)) / len(chunks))
    return len(chunks)


def retrieve(kb_name: str, question: str, k: int = 6) -> list[dict]:
    col = collection(kb_name)
    if col.count() == 0:
        return []
    res = col.query(query_embeddings=[get_embeddings().embed_query(question)], n_results=min(k, col.count()),
                    include=["documents", "metadatas", "distances"])
    return [{"text": d, "source": m["source"], "page": m.get("page") or None, "score": round(1 - dist, 4)}
            for d, m, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0])]


def documents(kb_name: str) -> dict[str, int]:
    col = collection(kb_name)
    got = col.get(include=["metadatas"])
    return dict(Counter(m["source"] for m in got["metadatas"]))


# ------------------------------------------------------------------ PII scan
PII_DOC_PROMPT = """Count the personal information (PII) about individual people in this passage from
"{doc}". For each category give how many distinct items appear (0 if none):
person_name (people's names, not companies or job titles), address (postal addresses of people),
date_of_birth, government_id, bank_account, financial.
(E-mails and phone numbers are counted separately; skip them.)
Never copy the personal data itself.
JSON: {{"counts": {{"person_name": 0, "address": 0, "date_of_birth": 0, "government_id": 0, "bank_account": 0,
"financial": 0}}}}

Passage:
\"\"\"{text}\"\"\""""


def scan_pii(filename: str, chunks: list[dict], use_llm: bool = True) -> list[dict]:
    """Per-category occurrence counts for one document. Emails/phones by pattern, the rest by the LLM."""
    full = "\n".join(c["text"] for c in chunks)
    counts = Counter()
    counts["email"] = len(set(_EMAIL.findall(full)))
    counts["phone"] = len({re.sub(r"\D", "", p) for p in _PHONE.findall(full)})
    llm_counts = Counter()
    llm_ok = False
    for c in (chunks if use_llm else []):
        try:
            data = ask_json("You are a data-privacy auditor. Reply with one JSON object only.",
                            PII_DOC_PROMPT.format(doc=filename, text=c["text"][:2500]))
            llm_ok = True
        except Exception as exc:
            log.warning("PII scan failed for a chunk of %s: %s", filename, exc)
            continue
        counts_in = data.get("counts") if isinstance(data.get("counts"), dict) else {}
        for cat, n in counts_in.items():
            if cat not in PII_CATEGORIES or cat in ("email", "phone"):
                continue
            try:
                n = max(int(n), 0)
            except (TypeError, ValueError):
                continue
            llm_counts[cat] += n
    counts.update(llm_counts)
    out = []
    sens = {"government_id": "high", "bank_account": "high", "date_of_birth": "high", "financial": "high",
            "person_name": "low"}
    for cat, n in counts.items():
        if n <= 0:
            continue
        by_rules = cat in ("email", "phone")
        out.append({"source_document": filename, "pii_category": cat, "occurrences": n,
                    "sensitivity": sens.get(cat, "medium"), "confidence": 0.95 if by_rules else 0.7,
                    "detected_by": "rules" if by_rules else "llm",
                    "reason": f"{n} {cat.replace('_', ' ')} occurrence(s) found" + ("" if llm_ok or by_rules else " (LLM unavailable)")})
    return out
