"""LLM extraction of a graph schema (and PII columns) from a CSV/XLSX file.

The LLM makes the modelling decisions (what a row is, which columns identify it, which
entities are embedded, relationship names/direction, PII). Deterministic profiling gives it
evidence and checks its answers: value overlap finds references between sheets, and a
functional-dependency test finds line-level columns (e.g. qty on an order line) that belong on
a relationship rather than on the node. Everything the LLM says is validated; anything unusable
falls back to a heuristic so extraction always produces an editable draft.
"""

import logging
import re
from collections import defaultdict

from app import graph_schema as gs
from app import rules
from app.llm import ask_json
from app.tabular import Sheet, coerce, is_blank, norm_key

log = logging.getLogger(__name__)

PII_CATEGORIES = [
    "person_name",
    "email",
    "phone",
    "address",
    "date_of_birth",
    "government_id",
    "bank_account",
    "financial",
    "free_text",
    "other",
]
LINK_OVERLAP = 0.6
UNIQUE = 0.95  # distinct ratio treated as unique (tolerates a few duplicate rows)


# ------------------------------------------------------------------ prompts
def _column_lines(sheet: Sheet) -> str:
    lines = []
    for c in sheet.profile.values():
        samples = ", ".join(c.samples[:4])
        lines.append(f'- "{c.name}": {c.type}, {c.fill_rate:.0%} filled, {c.distinct} distinct values; e.g. {samples}')
    return "\n".join(lines)


ENTITY_SYSTEM = """You are a data modeller who designs Neo4j property graphs from spreadsheets.
Reply with a single JSON object and nothing else."""

ENTITY_PROMPT = """Workbook "{file}" has these sheets: {sheets}.

Sheet "{sheet}" has {rows} rows. Columns (type, fill, distinct values, examples):
{columns}
{hints}

Decide what one row of sheet "{sheet}" describes.
- "row_entity": the thing each row describes, as a singular PascalCase label (e.g. Employee, Invoice, Patient),
  its identifying column ("key_column", copied exactly), and its descriptive columns ("property_columns").
  Use null if every row only links things described in other sheets (a link/junction table, e.g. which
  student is enrolled in which course).
- "reference_columns": columns holding the ID of something described in ANOTHER sheet
  (e.g. department_id in an employees sheet).
- "embedded_entities": things that have no sheet of their own but are named in a column here and are worth
  their own node (e.g. a Manager column in a projects sheet). Each needs "label", "key_column",
  "property_columns". Do not list things that have their own sheet.
- "ignore_columns": row numbers, running indexes, empty or junk columns.

Use column names exactly as written above. JSON shape:
{{"row_entity": {{"label": "...", "key_column": "...", "property_columns": ["..."]}} | null,
  "reference_columns": ["..."],
  "embedded_entities": [{{"label": "...", "key_column": "...", "property_columns": ["..."]}}],
  "ignore_columns": ["..."]}}"""

REL_PROMPT = """Graph node types: {labels}.

These pairs of node types are linked in the data:
{links}

For each link write a short English sentence with the subject first (e.g. "Employee works in Department",
"Doctor treats Patient", "Invoice issued by Vendor"), then the relationship type in UPPER_SNAKE_CASE (the verb,
e.g. WORKS_IN, TREATS, ISSUED_BY), "from" = the
subject node type and "to" = the object node type. Both must be the two node types of that link.
JSON shape: {{"relationships": [{{"id": 1, "sentence": "...", "type": "...", "from": "...", "to": "..."}}]}}"""

PII_SYSTEM = "You are a data-privacy auditor. You classify spreadsheet columns. Reply with one JSON object only."

PII_PROMPT = """Classify every column of sheet "{sheet}" for personal data (PII) about individual people.

Columns (type; example values):
{columns}

For EACH column choose one category:
person_name (a person's name), email, phone, address, date_of_birth, government_id (PAN, Aadhaar, passport),
bank_account, financial (salary, card), free_text (notes/comments that can mention people), or none.
Company names, product names, cities, regions, record IDs/codes, quantities, prices and status values are none.

JSON, with one entry per column:
{{"columns": [{{"column": "<name>", "category": "<category>", "confidence": 0.0-1.0}}]}}"""


# ------------------------------------------------------------------ profiling helpers
_ID_NAME = re.compile(r"(^|[^a-z])(id|code|sku|no|number|key|ref)([^a-z]|$)", re.I)


def _key_ok(sheet: Sheet, column: str) -> bool:
    """Keys are text (or integers named like IDs); a quantity or a date can't identify anything."""
    p = sheet.profile[column]
    if p.fill_rate < 0.9:
        return False
    if p.type == "string":
        return True
    # integer codes (GL account 5100) are fine; measures (stock_qty) have many distinct values
    return p.type == "integer" and (bool(_ID_NAME.search(column)) or (p.distinct <= 100 and p.distinct_ratio <= 0.2))


def _heuristic_key(sheet: Sheet) -> str:
    for c in sheet.columns:
        p = sheet.profile[c]
        if _ID_NAME.search(c) and _key_ok(sheet, c) and p.distinct_ratio > 0.3:
            return c
    candidates = [c for c in sheet.columns if _key_ok(sheet, c)] or sheet.columns
    return max(candidates, key=lambda c: (sheet.profile[c].distinct_ratio, sheet.profile[c].fill_rate))


def _fix_column(name, sheet: Sheet) -> str | None:
    if not isinstance(name, str):
        return None
    if name in sheet.columns:
        return name
    wanted = re.sub(r"[^a-z0-9]", "", name.lower())
    for c in sheet.columns:
        if re.sub(r"[^a-z0-9]", "", c.lower()) == wanted:
            return c
    return None


def _columns(names, sheet: Sheet) -> list[str]:
    out = []
    for n in names or []:
        c = _fix_column(n, sheet)
        if c and c not in out:
            out.append(c)
    return out


def _line_level_columns(sheet: Sheet, key_col: str) -> set[str]:
    """Columns whose value varies between rows with the same key (the sheet is at line level)."""
    groups = defaultdict(list)
    for r in sheet.rows:
        k = norm_key(r.get(key_col))
        if k:
            groups[k].append(r)
    multi = [g for g in groups.values() if len(g) > 1]
    if not multi:
        return set()
    varying = set()
    for c in sheet.columns:
        if c == key_col:
            continue
        t = sheet.profile[c].type  # compare parsed values: one date written four ways is still one date
        inconsistent = sum(len({norm_key(coerce(r.get(c), t)) for r in g}) > 1 for g in multi)
        if inconsistent > 0.05 * len(multi):
            varying.add(c)
    return varying


def _looks_junk(sheet: Sheet, column: str) -> bool:
    """Unnamed, essentially empty, or a running row counter. Sparse free text (notes) is kept."""
    p = sheet.profile[column]
    if column.startswith("column_") or p.fill_rate < 0.005:
        return True
    if p.type == "integer" and p.distinct_ratio >= 0.99:
        vals = [v for v in sheet.values(column) if isinstance(v, int)]
        return len(vals) > 10 and all(b - a == 1 for a, b in zip(vals, vals[1:], strict=False))
    return False


def _heuristic_entity(sheet: Sheet) -> dict:
    return {"label": gs.singular(gs.to_pascal(sheet.name)), "key_column": _heuristic_key(sheet), "property_columns": []}


def key_values(sheet: Sheet, column: str) -> set[str]:
    return {k for k in (norm_key(v) for v in sheet.values(column)) if k}


# ------------------------------------------------------------------ step 2: entities per sheet
def reference_evidence(sheets: list[Sheet]) -> dict[str, set[str]]:
    """Columns (per sheet) whose values are the IDs of another sheet's rows."""
    return {
        name: {h.split('"')[1] for h in hints if " holds IDs from sheet " in h}
        for name, hints in reference_hints(sheets).items()
    }


def reference_hints(sheets: list[Sheet]) -> dict[str, list[str]]:
    """Evidence for the prompt: columns whose values are the IDs of another sheet's rows."""

    def unique_cols(s):
        return [
            c
            for c in s.columns
            if s.profile[c].type == "string" and s.profile[c].distinct_ratio >= UNIQUE and s.profile[c].fill_rate > 0.9
        ]

    keys = {}
    for s in sheets:
        cands = unique_cols(s)
        if cands:
            k = next((c for c in cands if re.search(r"id|code|sku|no\b|number|key", c, re.I)), cands[0])
            keys[s.name] = (k, key_values(s, k))
    hints = {s.name: [] for s in sheets}
    for s in sheets:
        for c in s.columns:
            vals = key_values(s, c)
            if not vals or s.profile[c].type != "string":
                continue
            for other in sheets:
                if other.name == s.name or other.name not in keys:
                    continue
                okey, ovals = keys[other.name]
                if len(vals & ovals) / len(vals) >= LINK_OVERLAP:
                    hints[s.name].append(f'"{c}" holds IDs from sheet "{other.name}" (column "{okey}")')
    uniq = {s.name: unique_cols(s) for s in sheets}
    for s in sheets:
        if uniq[s.name]:
            hints[s.name].append("Columns unique on every row: " + ", ".join(f'"{c}"' for c in uniq[s.name][:4]))
        else:
            hints[s.name].append(
                "No column is unique on every row, so several rows can describe the same thing "
                "(e.g. line items of one invoice) or each row links other things."
            )
    return hints


def ask_sheet_entities(sheet: Sheet, file_name: str, sheet_names: list[str], hints: list[str] | None = None) -> dict:
    hint_text = ("Evidence from the data:\n" + "\n".join(f"- {h}" for h in hints) + "\n") if hints else ""
    prompt = ENTITY_PROMPT.format(
        file=file_name,
        sheets=", ".join(sheet_names),
        sheet=sheet.name,
        rows=len(sheet.rows),
        columns=_column_lines(sheet),
        hints=hint_text,
    )
    try:
        return ask_json(ENTITY_SYSTEM, prompt)
    except Exception as exc:  # the draft must still be produced; the reviewer fixes it
        log.warning("entity extraction failed for sheet %s: %s", sheet.name, exc)
        return {}


def _clean_entity(raw, sheet: Sheet) -> dict | None:
    if not isinstance(raw, dict):
        return None
    key = _fix_column(raw.get("key_column"), sheet)
    label = raw.get("label")
    if not key or not isinstance(label, str) or not label.strip() or not _key_ok(sheet, key):
        return None
    return {
        "label": gs.singular(gs.to_pascal(label)),
        "key_column": key,
        "property_columns": [c for c in _columns(raw.get("property_columns"), sheet) if c != key],
    }


def _entity_from_id_column(sheet: Sheet, refs: set[str]) -> dict | None:
    """A sheet without a row entity may still carry its own ID column (order_id on order lines)."""
    for c in sheet.columns:
        if c in refs or not _ID_NAME.search(c) or not _key_ok(sheet, c) or not sheet.profile[c].id_like:
            continue
        base = re.sub(r"[\s_-]*(id|code|no|number|key|ref)$", "", c, flags=re.I).strip() or sheet.name
        return {"label": gs.singular(gs.to_pascal(base)), "key_column": c, "property_columns": []}
    return None


def build_nodes(
    sheets: list[Sheet], answers: dict[str, dict], evidence: dict[str, set] | None = None
) -> tuple[list[dict], dict]:
    """Turn per-sheet LLM answers into node definitions; resolve duplicates across sheets."""
    evidence = evidence if evidence is not None else reference_evidence(sheets)
    per_sheet = {}
    for s in sheets:
        a = answers.get(s.name) or {}
        row = _clean_entity(a.get("row_entity"), s)
        # a link table needs at least two columns that reference other sheets
        is_link = a.get("row_entity", "missing") is None and len(evidence.get(s.name, ())) >= 2
        if row is None and not is_link:  # unusable answer: fall back to a heuristic
            row = _heuristic_entity(s)
        embedded = [e for e in (_clean_entity(x, s) for x in (a.get("embedded_entities") or [])) if e]
        # only drop columns that also look like junk; small models over-use "ignore"
        ignore = {c for c in _columns(a.get("ignore_columns"), s) if _looks_junk(s, c)}
        ignore |= {c for c in s.columns if _looks_junk(s, c)}
        per_sheet[s.name] = {
            "row": row,
            "embedded": embedded,
            "ignore": ignore,
            "refs": set(_columns(a.get("reference_columns"), s)),
        }

    # A label that is the row entity of several sheets stays with the sheet where its key is most unique;
    # the other sheets become link tables / references.
    by_label = defaultdict(list)
    sheet_map = {s.name: s for s in sheets}
    for name, info in per_sheet.items():
        if info["row"]:
            by_label[info["row"]["label"]].append(name)
    for label, names in by_label.items():
        if len(names) > 1:
            keep = max(
                names,
                key=lambda n: (
                    sheet_map[n].profile[per_sheet[n]["row"]["key_column"]].distinct_ratio,
                    n.lower().startswith(label.lower()[:4]),
                ),
            )
            for n in names:
                if n != keep:  # the model reused a label; model this sheet from its own name instead
                    per_sheet[n]["refs"].add(per_sheet[n]["row"]["key_column"])
                    fallback = _heuristic_entity(sheet_map[n])
                    taken = {i["row"]["label"] for m, i in per_sheet.items() if i["row"] and m != n}
                    per_sheet[n]["row"] = fallback if fallback["label"] not in taken else None
    # A sheet whose "key" is really a reference to another sheet's entity (and isn't unique) is a
    # link table, e.g. Inventory keyed by sku.
    row_keys = {n: key_values(sheet_map[n], info["row"]["key_column"]) for n, info in per_sheet.items() if info["row"]}
    for name, info in per_sheet.items():
        if not info["row"]:
            continue
        key_col = info["row"]["key_column"]
        if sheet_map[name].profile[key_col].distinct_ratio >= UNIQUE:
            continue
        mine = row_keys[name]
        for other, keys in row_keys.items():
            if (
                other != name
                and mine
                and per_sheet[other]["row"]
                and len(mine & keys) / len(mine) >= LINK_OVERLAP
                and sheet_map[other].profile[per_sheet[other]["row"]["key_column"]].distinct_ratio >= UNIQUE
            ):
                info["refs"].add(key_col)
                info["row"] = None
                break
    taken = {info["row"]["label"] for info in per_sheet.values() if info["row"]}
    for name, info in per_sheet.items():
        if info["row"] is None:
            e = _entity_from_id_column(sheet_map[name], evidence.get(name, set()) | info["refs"])
            if e and e["label"] not in taken:
                info["row"] = e
                taken.add(e["label"])
    row_labels = {info["row"]["label"] for info in per_sheet.values() if info["row"]}
    for info in per_sheet.values():  # embedded entities that have their own sheet are references
        info["embedded"] = [e for e in info["embedded"] if e["label"] not in row_labels]
        seen = set()
        info["embedded"] = [e for e in info["embedded"] if not (e["label"] in seen or seen.add(e["label"]))]

    nodes, emb_seen = [], set()
    for s in sheets:
        info = per_sheet[s.name]
        if info["row"]:
            r = info["row"]
            nodes.append(
                {
                    "label": r["label"],
                    "sheet": s.name,
                    "role": "row",
                    "key": {"name": gs.to_snake(r["key_column"]), "column": r["key_column"]},
                    "properties": [],
                    "_llm_props": r["property_columns"],
                    "count": len(key_values(s, r["key_column"])),
                }
            )
        for e in info["embedded"]:
            if e["label"] in emb_seen:
                continue
            emb_seen.add(e["label"])
            nodes.append(
                {
                    "label": e["label"],
                    "sheet": s.name,
                    "role": "embedded",
                    "key": {"name": gs.to_snake(e["key_column"]), "column": e["key_column"]},
                    "properties": [
                        {"name": gs.to_snake(c), "column": c, "type": s.profile[c].type} for c in e["property_columns"]
                    ],
                    "count": len(key_values(s, e["key_column"])),
                }
            )
    return nodes, per_sheet


# ------------------------------------------------------------------ step 3: relationships
def detect_links(sheets: list[Sheet], nodes: list[dict], per_sheet: dict) -> list[dict]:
    """Find references between node types from value overlap; assign columns to nodes/relationships."""
    sheet_map = {s.name: s for s in sheets}
    key_sets = {n["label"]: key_values(sheet_map[n["sheet"]], n["key"]["column"]) for n in nodes if n["role"] == "row"}
    links = []
    for s in sheets:
        info = per_sheet[s.name]
        row = next((n for n in nodes if n["sheet"] == s.name and n["role"] == "row"), None)
        embedded = [n for n in nodes if n["sheet"] == s.name and n["role"] == "embedded"]
        used = set(info["ignore"])
        for e in embedded:
            used |= {e["key"]["column"]} | {p["column"] for p in e["properties"]}
        if row:
            used.add(row["key"]["column"])

        fks = []  # (column, label)
        for c in s.columns:
            if c in used or s.profile[c].type in ("boolean", "date", "float"):
                continue
            vals = key_values(s, c)
            if not vals:
                continue
            best = None
            for label, keys in key_sets.items():
                if row and label == row["label"] and c == row["key"]["column"]:
                    continue
                overlap = len(vals & keys) / len(vals)
                name_hint = re.sub(r"[^a-z]", "", label.lower())[:4] in c.lower().replace(" ", "")
                score = (overlap, name_hint)
                if overlap >= LINK_OVERLAP and (best is None or score > best[0]):
                    best = (score, label)
            if best:
                fks.append((c, best[1]))
        fk_cols = {c for c, _ in fks}
        free = [c for c in s.columns if c not in used and c not in fk_cols]

        if row:
            line_cols = (
                _line_level_columns(s, row["key"]["column"])
                if s.profile[row["key"]["column"]].distinct_ratio < UNIQUE
                else set()
            )
            line_fk = next((c for c, _ in fks if c in line_cols), None)
            line_props = [c for c in free if c in line_cols] if line_fk else []
            node_cols = [c for c in free if c not in line_props]
            llm_props = [c for c in row.pop("_llm_props", []) if c in node_cols]
            ordered = llm_props + [c for c in node_cols if c not in llm_props]
            row["properties"] = [{"name": gs.to_snake(c), "column": c, "type": s.profile[c].type} for c in ordered]
            for c, label in fks:
                links.append(
                    {
                        "sheet": s.name,
                        "a": row["label"],
                        "a_column": row["key"]["column"],
                        "b": label,
                        "b_column": c,
                        "required": s.profile[c].fill_rate >= 0.9,
                        "properties": line_props if c == line_fk else [],
                        "why": f"{row['label']} rows reference {label} through column '{c}' in sheet '{s.name}'",
                    }
                )
            for e in embedded:
                links.append(
                    {
                        "sheet": s.name,
                        "a": row["label"],
                        "a_column": row["key"]["column"],
                        "b": e["label"],
                        "b_column": e["key"]["column"],
                        "required": s.profile[e["key"]["column"]].fill_rate >= 0.9,
                        "properties": [],
                        "why": f"each {row['label']} row names a {e['label']} in column '{e['key']['column']}'",
                    }
                )
        elif len(fks) >= 2:
            (c1, l1), (c2, l2) = fks[0], fks[1]
            links.append(
                {
                    "sheet": s.name,
                    "a": l1,
                    "a_column": c1,
                    "b": l2,
                    "b_column": c2,
                    "required": True,
                    "properties": free + [c for c, _ in fks[2:]],
                    "why": f"sheet '{s.name}' links {l1} ('{c1}') to {l2} ('{c2}')"
                    + (f" with {', '.join(free)}" if free else ""),
                }
            )
        elif fks:  # a "link table" with one reference: keep its data as a node after all
            c, label = fks[0]
            key = _heuristic_key(s)
            n = {
                "label": gs.singular(gs.to_pascal(s.name)),
                "sheet": s.name,
                "role": "row",
                "key": {"name": gs.to_snake(key), "column": key},
                "properties": [
                    {"name": gs.to_snake(x), "column": x, "type": s.profile[x].type} for x in free if x != key
                ],
                "count": len(key_values(s, key)),
            }
            nodes.append(n)
            links.append(
                {
                    "sheet": s.name,
                    "a": n["label"],
                    "a_column": key,
                    "b": label,
                    "b_column": c,
                    "required": s.profile[c].fill_rate >= 0.9,
                    "properties": [],
                    "why": f"{n['label']} rows reference {label} through column '{c}'",
                }
            )
    for n in nodes:
        n.pop("_llm_props", None)
    return links


def _short_rel(raw) -> str | None:
    """Model names like PLACED_BY_CUSTOMER_TO_SHOP -> PLACED; STORED_IN_THE_WAREHOUSE -> STORED_IN."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    parts = gs.to_rel_type(raw).split("_")
    if len(parts) > 3 or len("_".join(parts)) > 24:
        keep2 = len(parts) > 1 and (parts[0] in ("HAS", "IS") or parts[1] in _PREPOSITIONS)
        parts = parts[:2] if keep2 else parts[:1]
    return "_".join(parts)


_PREPOSITIONS = {"IN", "TO", "FROM", "OF", "BY", "ON", "AT", "WITH", "FOR", "INTO", "UNDER", "OVER"}


def name_links(links: list[dict], labels: list[str], sheets: list[Sheet]) -> list[dict]:
    sheet_map = {s.name: s for s in sheets}
    answer = {}
    if links:
        text = "\n".join(f"{i}. {link['a']} and {link['b']}: {link['why']}" for i, link in enumerate(links, 1))
        try:
            data = ask_json(ENTITY_SYSTEM, REL_PROMPT.format(labels=", ".join(labels), links=text))
            for item in data.get("relationships") or []:
                if isinstance(item, dict) and str(item.get("id", "")).isdigit():
                    answer[int(item["id"])] = item
        except Exception as exc:
            log.warning("relationship naming failed: %s", exc)
    rels = []
    for i, link in enumerate(links, 1):
        a = answer.get(i, {})
        ends = {link["a"], link["b"]}
        frm, to = gs.to_pascal(str(a.get("from", ""))), gs.to_pascal(str(a.get("to", "")))
        frm, to = gs.singular(frm), gs.singular(to)
        forward = (frm, to) == (link["a"], link["b"]) or link["a"] == link["b"] or {frm, to} != ends
        rtype = _short_rel(a.get("type")) or f"HAS_{gs.to_rel_type(link['b'])}"
        if link["a"] == link["b"] and rtype.startswith("HAS_"):
            rtype = f"RELATED_{rtype[4:]}"
        src = (link["a"], link["a_column"]) if forward else (link["b"], link["b_column"])
        dst = (link["b"], link["b_column"]) if forward else (link["a"], link["a_column"])
        s = sheet_map[link["sheet"]]
        rels.append(
            {
                "type": rtype,
                "sheet": link["sheet"],
                "from": {"label": src[0], "column": src[1]},
                "to": {"label": dst[0], "column": dst[1]},
                "properties": [
                    {"name": gs.to_snake(c), "column": c, "type": s.profile[c].type} for c in link["properties"]
                ],
                "required": link["required"],
                "count": sum(
                    1 for r in s.rows if not is_blank(r.get(link["a_column"])) and not is_blank(r.get(link["b_column"]))
                ),
            }
        )
    # keep relationship types unique per (type, from, to)
    seen = set()
    for r in rels:
        base, n = r["type"], 2
        while (r["type"], r["from"]["label"], r["to"]["label"]) in seen:
            r["type"] = f"{base}_{n}"
            n += 1
        seen.add((r["type"], r["from"]["label"], r["to"]["label"]))
    return rels


# ------------------------------------------------------------------ step 5: PII
_NAME_RULES = [
    (
        re.compile(r"\b(bank|iban|ifsc|account\s*(no|number))\b", re.I),
        "bank_account",
        "high",
        "column holds bank account details",
    ),
    (
        re.compile(r"\b(dob|date\s*of\s*birth|birth\s*date)\b", re.I),
        "date_of_birth",
        "high",
        "column holds dates of birth",
    ),
    (
        re.compile(r"\b(pan|aadhaar|aadhar|ssn|passport|national\s*id)\b", re.I),
        "government_id",
        "high",
        "column holds a national ID",
    ),
    (re.compile(r"\b(address|street)\b", re.I), "address", "medium", "column holds postal addresses"),
]


def rule_pii(sheet: Sheet, column: str) -> dict | None:
    """Pattern and column-name evidence used to back up the LLM (never stores the values)."""
    vals = [str(v).strip() for v in sheet.values(column) if not is_blank(v)]
    if not vals:
        return None
    if sheet.profile[column].type in ("string", "date"):
        for rx, cat, sens, reason in _NAME_RULES:
            if rx.search(column):
                return {"category": cat, "sensitivity": sens, "confidence": 0.85, "reason": reason}
    n = len(vals)
    ratio = lambda rx, full=True: sum(bool(rx.fullmatch(v) if full else rx.search(v)) for v in vals) / n  # noqa: E731
    if ratio(rules.EMAIL) >= 0.5:
        return {
            "category": "email",
            "sensitivity": "medium",
            "confidence": 0.95,
            "reason": "values are e-mail addresses",
        }
    if ratio(rules.PHONE) >= 0.5:
        return {"category": "phone", "sensitivity": "medium", "confidence": 0.9, "reason": "values are phone numbers"}
    if ratio(rules.PAN) >= 0.5 or ratio(rules.AADHAAR) >= 0.5:
        return {
            "category": "government_id",
            "sensitivity": "high",
            "confidence": 0.9,
            "reason": "values match a national ID format",
        }
    if sheet.profile[column].type == "string" and sum(len(v) for v in vals) / n > 15:  # free text
        hits = sum(bool(rules.EMAIL.search(v) or rules.PHONE.search(v)) for v in vals)
        if hits and hits / n >= 0.02:
            return {
                "category": "free_text",
                "sensitivity": "medium",
                "confidence": 0.7,
                "reason": f"{hits} free-text values contain a phone number or e-mail",
            }
    return None


_SENSITIVITY = {
    "person_name": "low",
    "email": "medium",
    "phone": "medium",
    "address": "medium",
    "free_text": "medium",
    "date_of_birth": "high",
    "government_id": "high",
    "bank_account": "high",
    "financial": "high",
    "other": "medium",
}


def verify_pii(sheet: Sheet, column: str, category: str) -> bool:
    """The LLM proposes a category; the values have to be consistent with it."""
    import datetime as dt

    p = sheet.profile[column]
    vals = [v for v in sheet.values(column) if not is_blank(v)]
    if not vals:
        return False
    texts = [str(v).strip() for v in vals]
    share = lambda pred: sum(1 for t in texts if pred(t)) / len(texts)  # noqa: E731
    if category == "date_of_birth":
        if re.search(r"dob|birth", column, re.I):
            return True
        dates = sorted(d for d in (coerce(v, "date") for v in vals) if d)
        return bool(dates) and dates[len(dates) // 2] < dt.date.today() - dt.timedelta(days=16 * 365)
    if category in ("government_id", "bank_account"):
        rule = rule_pii(sheet, column)
        return bool(rule and rule["category"] == category)
    if category == "address":
        return p.type == "string" and share(lambda t: bool(re.search(r"\d", t)) and len(t.split()) >= 3) >= 0.5
    if category == "person_name":
        return (
            p.type == "string"
            and not rules.NOT_PERSON_HEADER.search(column)
            and share(lambda t: 2 <= len(t.split()) <= 5 and not re.search(r"\d", t)) >= 0.7
            and share(lambda t: bool(rules.COMPANY.search(t))) < 0.2
        )
    if category == "email":
        return share(lambda t: bool(rules.EMAIL.search(t))) >= 0.5
    if category == "phone":
        return share(lambda t: bool(rules.PHONE.search(t))) >= 0.5
    if category == "free_text":
        return p.type == "string" and sum(len(t) for t in texts) / len(texts) > 15
    if category == "financial":
        return bool(re.search(r"salary|income|wage|card|credit|payroll", column, re.I))
    return False


def detect_pii(sheet: Sheet, columns: list[str]) -> list[dict]:
    if not columns:
        return []
    lines = "\n".join(
        f'- "{c}": {sheet.profile[c].type}; e.g. {", ".join(sheet.profile[c].samples[:3])}' for c in columns
    )
    found = {}
    try:
        data = ask_json(PII_SYSTEM, PII_PROMPT.format(sheet=sheet.name, columns=lines))
        items = data.get("columns") or data.get("pii") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            col = _fix_column(item.get("column"), sheet)
            cat = str(item.get("category") or "none").strip().lower()
            if col not in columns or cat not in PII_CATEGORIES or cat == "other":
                continue
            try:
                conf = min(max(float(item.get("confidence", 0.7)), 0.0), 1.0)
            except (TypeError, ValueError):
                conf = 0.7
            if conf < 0.5 or not verify_pii(sheet, col, cat):
                log.info("PII suggestion rejected by evidence: %s.%s as %s", sheet.name, col, cat)
                continue
            found[col] = {
                "column": col,
                "category": cat,
                "sensitivity": _SENSITIVITY[cat],
                "confidence": conf,
                "reason": f"LLM classified the column as {cat.replace('_', ' ')}; values are consistent",
                "detected_by": "llm",
            }
    except Exception as exc:
        log.warning("PII detection failed for sheet %s: %s", sheet.name, exc)
    for col in columns:
        rule = rule_pii(sheet, col)
        if rule and col not in found:
            found[col] = {"column": col, **rule, "detected_by": "rules"}
    return list(found.values())


# ------------------------------------------------------------------ orchestration
STEPS = [
    "Read file and detect sheets",
    "LLM identifies nodes and entities",
    "LLM infers relationships",
    "Generate Cypher queries",
    "LLM scans for PII",
]


def extract(sheets: list[Sheet], file_name: str, step=None, cancelled=None) -> dict:
    """Run steps 2-5 (step 1, reading, is done by the caller). step(index, status, detail)."""
    step = step or (lambda *a: None)
    check = cancelled or (lambda: False)
    names = [s.name for s in sheets]

    step(1, "running", f"0 of {len(sheets)} sheets")
    answers = {}
    hints = reference_hints(sheets)
    for i, s in enumerate(sheets, 1):
        answers[s.name] = ask_sheet_entities(s, file_name, names, hints[s.name])
        step(1, "running", f"{i} of {len(sheets)} sheets")
        if check():
            return {}
    nodes, per_sheet = build_nodes(
        sheets, answers, {k: {h.split('"')[1] for h in v if " holds IDs from sheet " in h} for k, v in hints.items()}
    )
    step(1, "done", f"{len(nodes)} node types")

    step(2, "running", "")
    links = detect_links(sheets, nodes, per_sheet)
    rels = name_links(links, [n["label"] for n in nodes], sheets)
    step(2, "done", f"{len({r['type'] for r in rels})} relationship types")
    if check():
        return {}

    step(3, "running", "")
    schema = {"source_file": file_name, "sheets": sheets_summary(sheets), "nodes": nodes, "relationships": rels}
    schema = gs.normalize(schema)
    errors = gs.validate(schema)
    if errors:  # shouldn't happen: drop the offending relationships rather than fail the draft
        log.warning("draft schema problems: %s", errors)
        labels = {n["label"] for n in schema["nodes"]}
        schema["relationships"] = [
            r for r in schema["relationships"] if r["from"]["label"] in labels and r["to"]["label"] in labels
        ]
    step(3, "done", f"{len(gs.preview(schema))} statements")

    step(4, "running", "")
    pii = []
    for s in sheets:
        stored = stored_columns(schema, s.name)
        for item in detect_pii(s, stored):
            pii.append({"sheet": s.name, **item})
        if check():
            return {}
    schema["pii"] = pii
    step(4, "done", f"{len(pii)} PII columns")
    return schema


def sheets_summary(sheets: list[Sheet]) -> dict:
    return {
        s.name: {
            "header_row": s.header_row,
            "rows": len(s.rows),
            "columns": {
                c: {"type": p.type, "fill_rate": p.fill_rate, "distinct": p.distinct, "samples": p.samples[:3]}
                for c, p in s.profile.items()
            },
        }
        for s in sheets
    }


def stored_columns(schema: dict, sheet: str) -> list[str]:
    cols = []
    for n in schema["nodes"]:
        if n["sheet"] == sheet:
            cols += [n["key"]["column"]] + [p["column"] for p in n.get("properties", [])]
    for r in schema["relationships"]:
        if r["sheet"] == sheet:
            cols += [p["column"] for p in r["properties"]]
    return list(dict.fromkeys(cols))


def pii_targets(schema: dict) -> list[dict]:
    """PII entries mapped onto current node/relationship property names (for kb_pii_fields)."""
    out = []
    for item in schema.get("pii", []):
        for n in schema["nodes"]:
            if n["sheet"] != item["sheet"]:
                continue
            if n["key"]["column"] == item["column"]:
                out.append({**item, "node_label": n["label"], "property_name": n["key"]["name"]})
            for p in n.get("properties", []):
                if p["column"] == item["column"]:
                    out.append({**item, "node_label": n["label"], "property_name": p["name"]})
        for r in schema["relationships"]:
            if r["sheet"] != item["sheet"]:
                continue
            for p in r["properties"]:
                if p["column"] == item["column"]:
                    out.append({**item, "node_label": r["type"], "property_name": p["name"]})
    return out
