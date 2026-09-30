"""Shared test data: dataset paths, manifest, and 'reviewed' schemas (what an owner would approve)."""

import json
from functools import lru_cache
from pathlib import Path

from app import demo

DATA = Path("/data") if Path("/data/samples").exists() else Path(__file__).resolve().parents[2] / "data"
SAMPLES = DATA / "samples"


@lru_cache
def manifest() -> dict:
    return json.loads((DATA / "expected" / "manifest.json").read_text())


def questions(kb: str, level: str = "core") -> list[dict]:
    return [q for q in manifest()["questions"] if q["kb"] == kb and q["level"] == level]


def retail_schema() -> dict:
    return demo.retail_schema(SAMPLES)


def finance_schema() -> dict:
    return demo.finance_schema(SAMPLES)
