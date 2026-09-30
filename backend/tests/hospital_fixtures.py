"""The hospital dataset (data/generate_hospital_dataset.py): paths, manifest and a reviewed schema."""

import json
from functools import lru_cache

from app.extraction import sheets_summary
from app.tabular import read_table_file

from .fixtures import DATA

HOSPITAL = DATA / "samples" / "hospital"
WORKBOOK = HOSPITAL / "hospital_operations.xlsx"
DOCS = ["infection_control_policy.pdf", "discharge_protocol.docx", "pharmacy_sop.txt"]


@lru_cache
def hospital_manifest() -> dict:
    return json.loads((DATA / "expected" / "hospital_manifest.json").read_text())


def _p(name, column, type_="string"):
    return {"name": name, "column": column, "type": type_}


def _rel(rid, rtype, sheet, frm, fcol, to, tcol, props=(), required=True):
    return {
        "id": rid,
        "type": rtype,
        "sheet": sheet,
        "from": {"label": frm, "column": fcol},
        "to": {"label": to, "column": tcol},
        "properties": list(props),
        "required": required,
    }


def hospital_schema() -> dict:
    """What an owner would approve on the Review screen for hospital_operations.xlsx."""
    return {
        "source_file": WORKBOOK.name,
        "source_path": str(WORKBOOK),
        "sheets": sheets_summary(read_table_file(WORKBOOK)),
        "nodes": [
            {
                "id": "n1",
                "label": "Department",
                "sheet": "Dept Master",
                "role": "row",
                "key": {"name": "code", "column": "Dept#"},
                "properties": [
                    _p("name", "Department"),
                    _p("floor", "Floor", "integer"),
                    _p("annual_budget", "Annual Budget", "integer"),
                ],
            },
            {
                "id": "n2",
                "label": "Doctor",
                "sheet": "Doctors",
                "role": "row",
                "key": {"name": "reg_no", "column": "Reg. No."},
                "properties": [
                    _p("name", "Doctor Name"),
                    _p("specialisation", "Specialisation"),
                    _p("mobile", "Mobile"),
                    _p("joined_on", "Joined On", "date"),
                ],
            },
            {
                "id": "n3",
                "label": "Patient",
                "sheet": "Patients",
                "role": "row",
                "key": {"name": "uhid", "column": "UHID"},
                "properties": [
                    _p("name", "Full Name"),
                    _p("gender", "Gender"),
                    _p("date_of_birth", "Date of Birth", "date"),
                    _p("blood_group", "Blood Group"),
                    _p("aadhaar", "Aadhaar"),
                    _p("phone", "Phone"),
                    _p("city", "City"),
                    _p("policy_no", "Policy No"),
                ],
            },
            {
                "id": "n4",
                "label": "Insurer",
                "sheet": "Patients",
                "role": "embedded",
                "key": {"name": "name", "column": "Insurer"},
                "properties": [],
            },
            {
                "id": "n5",
                "label": "Ward",
                "sheet": "Wards",
                "role": "row",
                "key": {"name": "code", "column": "Ward"},
                "properties": [_p("ward_type", "Ward Type"), _p("beds", "Beds", "integer")],
            },
            {
                "id": "n6",
                "label": "Procedure",
                "sheet": "Procedures",
                "role": "row",
                "key": {"name": "code", "column": "Code"},
                "properties": [
                    _p("name", "Procedure Name"),
                    _p("category", "Category"),
                    _p("base_price", "Base Price", "integer"),
                ],
            },
            {
                "id": "n7",
                "label": "Medication",
                "sheet": "Medications",
                "role": "row",
                "key": {"name": "drug_code", "column": "Drug Code"},
                "properties": [
                    _p("generic_name", "Generic Name"),
                    _p("brand", "Brand"),
                    _p("schedule", "Schedule"),
                    _p("unit_cost", "Unit Cost", "float"),
                ],
            },
            {
                "id": "n8",
                "label": "Admission",
                "sheet": "Admissions",
                "role": "row",
                "key": {"name": "adm_id", "column": "Adm ID"},
                "properties": [
                    _p("admit_date", "Admit Date", "date"),
                    _p("discharge_date", "Discharge Date", "date"),
                    _p("outcome", "Outcome"),
                ],
            },
            {
                "id": "n9",
                "label": "Prescription",
                "sheet": "Prescriptions",
                "role": "row",
                "key": {"name": "rx_no", "column": "Rx No"},
                "properties": [_p("dose", "Dose"), _p("days", "Days", "integer")],
            },
        ],
        "relationships": [
            _rel("r1", "HEADED_BY", "Dept Master", "Department", "Dept#", "Doctor", "Head Doctor"),
            _rel("r2", "WORKS_IN", "Doctors", "Doctor", "Reg. No.", "Department", "Dept"),
            _rel("r3", "SUPERVISED_BY", "Doctors", "Doctor", "Reg. No.", "Doctor", "Supervisor Reg", required=False),
            _rel("r4", "INSURED_BY", "Patients", "Patient", "UHID", "Insurer", "Insurer"),
            _rel("r5", "BELONGS_TO", "Wards", "Ward", "Ward", "Department", "Dept"),
            _rel("r6", "ADMITTED", "Admissions", "Patient", "UHID", "Admission", "Adm ID"),
            _rel("r7", "ADMITTED_BY", "Admissions", "Admission", "Adm ID", "Doctor", "Admitting Doctor"),
            _rel("r8", "IN_WARD", "Admissions", "Admission", "Adm ID", "Ward", "Ward"),
            _rel(
                "r9",
                "UNDERWENT",
                "Admissions",
                "Admission",
                "Adm ID",
                "Procedure",
                "Procedure Code",
                [
                    _p("procedure_date", "Procedure Date", "date"),
                    _p("cost", "Cost", "integer"),
                    _p("remarks", "Remarks"),
                ],
            ),
            _rel("r10", "FOR_ADMISSION", "Prescriptions", "Prescription", "Rx No", "Admission", "Adm ID"),
            _rel("r11", "OF_MEDICATION", "Prescriptions", "Prescription", "Rx No", "Medication", "Drug Code"),
            _rel("r12", "PRESCRIBED_BY", "Prescriptions", "Prescription", "Rx No", "Doctor", "Prescribed By"),
        ],
        "pii": [],
    }
