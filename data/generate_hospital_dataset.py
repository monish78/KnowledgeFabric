"""Generate the hospital-network test dataset (deterministic, unrelated to the retail dataset).

Writes
  data/samples/hospital/   hospital_operations.xlsx, admissions_april.xlsx and three policy documents
  data/expected/hospital_manifest.json   ground truth for the tests

Hard parts on purpose: a non-table "ReadMe" sheet, a title row above a header, Indian lakh-style
amounts ("Rs. 1,25,000/-"), "05-Mar-2026" dates, circular references (a department's head is a
doctor, every doctor belongs to a department), a supervisor self-reference, line-level admissions
(one row per procedure), prescriptions pointing at three other entities, an embedded insurer column,
PII in several forms (names, Aadhaar, date of birth, phones, free-text remarks) and bad rows.
"""

import datetime as dt
import json
import random
from collections import Counter
from pathlib import Path

from docx import Document
from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).parent
OUT = ROOT / "samples" / "hospital"
rng = random.Random(4242)

FIRST = [
    "Aditi",
    "Bhavesh",
    "Chitra",
    "Dinesh",
    "Esha",
    "Farooq",
    "Gayatri",
    "Harish",
    "Ishita",
    "Jatin",
    "Kavitha",
    "Lokesh",
    "Madhu",
    "Naveen",
    "Oviya",
    "Pranav",
    "Qadir",
    "Rekha",
    "Sameer",
    "Tanvi",
    "Uday",
    "Vidya",
    "Waseem",
    "Yamini",
    "Zubin",
    "Anand",
    "Bindu",
    "Charan",
    "Deepika",
    "Gokul",
]
LAST = [
    "Agarwal",
    "Bose",
    "Chandran",
    "Dsouza",
    "Easwaran",
    "Gill",
    "Hegde",
    "Ismail",
    "Jain",
    "Kapoor",
    "Lal",
    "Mishra",
    "Narayan",
    "Ojha",
    "Prabhu",
    "Qureshi",
    "Raman",
    "Saxena",
    "Thakur",
    "Upadhyay",
    "Venkatesh",
    "Wagle",
    "Yadav",
    "Zaveri",
]
DEPTS = [
    ("D-CARD", "Cardiology", 3),
    ("D-ORTH", "Orthopaedics", 2),
    ("D-NEUR", "Neurology", 4),
    ("D-ONCO", "Oncology", 5),
    ("D-PAED", "Paediatrics", 1),
    ("D-GENM", "General Medicine", 1),
    ("D-GSUR", "General Surgery", 2),
    ("D-NEPH", "Nephrology", 4),
    ("D-PULM", "Pulmonology", 3),
    ("D-EMER", "Emergency", 0),
]
SPECIALISATION = {
    "D-CARD": "Cardiologist",
    "D-ORTH": "Orthopaedic Surgeon",
    "D-NEUR": "Neurologist",
    "D-ONCO": "Oncologist",
    "D-PAED": "Paediatrician",
    "D-GENM": "Physician",
    "D-GSUR": "General Surgeon",
    "D-NEPH": "Nephrologist",
    "D-PULM": "Pulmonologist",
    "D-EMER": "Emergency Physician",
}
INSURERS = [
    "Star Health",
    "ICICI Lombard",
    "HDFC Ergo",
    "Niva Bupa",
    "Care Health",
    "New India Assurance",
    "Bajaj Allianz",
    "Self-pay",
]
DRUGS = [
    "Paracetamol",
    "Amoxicillin",
    "Metformin",
    "Atorvastatin",
    "Amlodipine",
    "Pantoprazole",
    "Ceftriaxone",
    "Ondansetron",
    "Heparin",
    "Insulin Glargine",
    "Morphine",
    "Salbutamol",
    "Prednisolone",
    "Furosemide",
    "Clopidogrel",
    "Levetiracetam",
    "Azithromycin",
    "Enoxaparin",
    "Tramadol",
    "Losartan",
]


def person(used):
    while True:
        n = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        if n not in used:
            used.add(n)
            return n


def phone():
    return f"+91-{rng.randint(70000, 99999)}-{rng.randint(10000, 99999)}"


def lakh(n: int) -> str:
    """Indian digit grouping: 125000 -> 1,25,000."""
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def money(n: int) -> str:
    return rng.choice([f"Rs. {lakh(n)}/-", f"₹{lakh(n)}", f"INR {n}", lakh(n), n])


def date_cell(d: dt.date):
    return rng.choice(
        [dt.datetime(d.year, d.month, d.day), d.strftime("%d-%b-%Y"), d.strftime("%d/%m/%Y"), d.isoformat()]
    )


def messy(key: str) -> str:
    return rng.choice([key, key, key, key.lower(), f" {key}", f"{key} "])


def build():
    people = set()
    doctors, depts = {}, {}
    for code, name, floor in DEPTS:
        depts[code] = {"code": code, "name": name, "floor": floor, "budget": rng.randint(40, 400) * 100000}
        for i in range(6):
            reg = f"KMC-{rng.randint(10000, 99999)}"
            while reg in doctors:
                reg = f"KMC-{rng.randint(10000, 99999)}"
            doctors[reg] = {
                "reg": reg,
                "name": f"Dr. {person(people)}",
                "dept": code,
                "spec": SPECIALISATION[code],
                "mobile": phone(),
                "joined": dt.date(2010, 1, 1) + dt.timedelta(days=rng.randint(0, 5500)),
                "supervisor": None,
            }
            if i == 0:
                depts[code]["head"] = reg
            else:
                doctors[reg]["supervisor"] = depts[code]["head"]
    by_dept = {c: [r for r, d in doctors.items() if d["dept"] == c] for c in depts}

    wards = {}
    for n in range(1, 21):
        code = f"W-{n:02d}"
        wards[code] = {
            "code": code,
            "type": rng.choice(["General", "ICU", "Private", "Semi-private"]),
            "dept": DEPTS[(n - 1) % 10][0],
            "beds": rng.randint(6, 30),
        }
    wards["W-07"]["beds"] = 48  # the largest ward

    procedures = {}
    for n in range(1, 51):
        code = f"PRC-{n:03d}"
        dept = DEPTS[(n - 1) % 10][0]
        procedures[code] = {
            "code": code,
            "dept": dept,
            "category": depts[dept]["name"],
            "name": f"{depts[dept]['name']} procedure {n}",
            "price": rng.randint(20, 400) * 500,
        }
    procedures["PRC-012"]["name"] = "Total knee replacement"  # an Orthopaedics procedure named in a question

    meds = {}
    for n in range(1, 81):
        code = f"DRG-{n:04d}"
        generic = DRUGS[(n - 1) % len(DRUGS)]
        meds[code] = {
            "code": code,
            "generic": generic,
            "brand": f"{generic[:4].upper()}-{rng.randint(10, 99)}",
            "schedule": rng.choice(["H", "H1", "X", "OTC"]),
            "cost": round(rng.uniform(2, 900), 2),
        }

    patients = {}
    ins_weights = [30, 12, 12, 10, 10, 8, 8, 10]  # Star Health covers the most
    for _ in range(400):
        uhid = f"UH{rng.randint(100000, 999999)}"
        while uhid in patients:
            uhid = f"UH{rng.randint(100000, 999999)}"
        patients[uhid] = {
            "uhid": uhid,
            "name": person(people),
            "gender": rng.choice(["M", "F", "Male", "female"]),
            "dob": dt.date(1940, 1, 1) + dt.timedelta(days=rng.randint(0, 29000)),
            "blood": rng.choice(["A+", "A-", "B+", "B-", "O+", "O-", "AB+", "AB-"]),
            "aadhaar": f"{rng.randint(2000, 9999)} {rng.randint(1000, 9999)} {rng.randint(1000, 9999)}",
            "phone": phone(),
            "city": rng.choice(["Mangaluru", "Udupi", "Mysuru", "Hubballi", "Bengaluru"]),
            "insurer": rng.choices(INSURERS, ins_weights)[0],
            "policy": f"POL{rng.randint(10**7, 10**8 - 1)}",
        }
    uhids = list(patients)
    top_patient = uhids[17]

    admissions, lines = {}, []
    for n in range(1, 951):
        adm = f"ADM/2026/{n:05d}"
        uhid = top_patient if n % 97 == 0 else rng.choice(uhids)
        dept = rng.choice(list(depts))
        doctor = rng.choice(by_dept[dept])
        ward = rng.choice([w for w, x in wards.items() if x["dept"] == dept])
        admit = dt.date(2025, 10, 1) + dt.timedelta(days=rng.randint(0, 170))
        outcome = rng.choices(["Recovered", "Improved", "Transferred", "Left against advice"], [55, 30, 10, 5])[0]
        admissions[adm] = {
            "adm": adm,
            "uhid": uhid,
            "doctor": doctor,
            "ward": ward,
            "admit": admit,
            "discharge": admit + dt.timedelta(days=rng.randint(1, 20)),
            "outcome": outcome,
            "dept": dept,
        }
        dept_procs = [p for p, x in procedures.items() if x["dept"] == dept]
        for proc in rng.sample(dept_procs, rng.randint(1, 3)):
            base = procedures[proc]["price"]
            cost = base * (5 if uhid == top_patient else 1) + rng.randint(0, 40) * 250
            remark = ""
            if rng.random() < 0.04:
                remark = rng.choice(
                    [
                        f"Attendant {rng.choice(FIRST)} reachable on {phone()}",
                        "Consent signed by spouse",
                        "Allergic to penicillin",
                        "Needs wheelchair at discharge",
                    ]
                )
            lines.append(
                {
                    "adm": adm,
                    "proc": proc,
                    "date": admit + dt.timedelta(days=rng.randint(0, 1)),
                    "cost": cost,
                    "remark": remark,
                }
            )

    rx = []
    drug_weights = [6 if c == "DRG-0011" else 1 for c in meds]  # Ceftriaxone is prescribed most
    n = 0
    for adm, a in admissions.items():
        for _ in range(rng.randint(0, 4)):
            n += 1
            code = rng.choices(list(meds), drug_weights)[0]
            prescriber = a["doctor"] if rng.random() < 0.7 else rng.choice(by_dept[a["dept"]])
            rx.append(
                {
                    "rx": f"RX-{n:06d}",
                    "adm": adm,
                    "drug": code,
                    "dose": rng.choice(["500 mg", "1 g", "10 units", "2 puffs", "40 mg", "5 ml"]),
                    "days": rng.randint(1, 14),
                    "by": prescriber,
                }
            )
    return {
        "doctors": doctors,
        "depts": depts,
        "wards": wards,
        "procedures": procedures,
        "meds": meds,
        "patients": patients,
        "admissions": admissions,
        "lines": lines,
        "rx": rx,
        "top_patient": top_patient,
    }


def write_workbook(m, path):
    wb = Workbook()
    ws = wb.active
    ws.title = "ReadMe"
    for line in [
        "Hospital operations extract",
        "Prepared by the MIS team for the quarterly review.",
        "Sheets: Dept Master, Doctors, Patients, Wards, Procedures, Medications, Admissions, Prescriptions.",
        "Amounts are in INR. Do not circulate outside the hospital.",
    ]:
        ws.append([line])

    ws = wb.create_sheet("Dept Master")
    ws.append(["Department master - FY 2025-26"])
    ws.append(["Dept#", "Department", "Floor", "Head Doctor", "Annual Budget"])
    for d in m["depts"].values():
        ws.append([d["code"], d["name"], d["floor"], messy(d["head"]), money(d["budget"])])

    ws = wb.create_sheet("Doctors")
    ws.append(["Reg. No.", "Doctor Name", "Specialisation", "Dept", "Supervisor Reg", "Mobile", "Joined On"])
    for d in m["doctors"].values():
        ws.append(
            [
                messy(d["reg"]),
                d["name"],
                d["spec"],
                messy(d["dept"]),
                d["supervisor"] or "",
                d["mobile"],
                date_cell(d["joined"]),
            ]
        )

    ws = wb.create_sheet("Patients")
    ws.append(
        [
            "UHID",
            "Full Name",
            "Gender",
            "Date of Birth",
            "Blood Group",
            "Aadhaar",
            "Phone",
            "City",
            "Insurer",
            "Policy No",
        ]
    )
    rows = list(m["patients"].values())
    rows += [dict(rows[i]) for i in (3, 44, 190, 300)]  # duplicate registrations
    for p in rows:
        ws.append(
            [
                messy(p["uhid"]),
                p["name"],
                p["gender"],
                date_cell(p["dob"]),
                p["blood"],
                p["aadhaar"],
                p["phone"],
                p["city"],
                p["insurer"],
                p["policy"],
            ]
        )

    ws = wb.create_sheet("Wards")
    ws.append(["Ward", "Ward Type", "Dept", "Beds"])
    for w in m["wards"].values():
        ws.append([w["code"], w["type"], w["dept"], w["beds"]])

    ws = wb.create_sheet("Procedures")
    ws.append(["Code", "Procedure Name", "Category", "Base Price"])
    for p in m["procedures"].values():
        ws.append([p["code"], p["name"], p["category"], money(p["price"])])

    ws = wb.create_sheet("Medications")
    ws.append(["Drug Code", "Generic Name", "Brand", "Schedule", "Unit Cost"])
    for d in m["meds"].values():
        ws.append([d["code"], d["generic"], d["brand"], d["schedule"], rng.choice([d["cost"], f"Rs {d['cost']:.2f}"])])

    ws = wb.create_sheet("Admissions")
    header = [
        "Adm ID",
        "UHID",
        "Admitting Doctor",
        "Ward",
        "Admit Date",
        "Discharge Date",
        "Procedure Code",
        "Procedure Date",
        "Cost",
        "Outcome",
        "Remarks",
    ]
    ws.append(header)
    out = []
    for ln in m["lines"]:
        a = m["admissions"][ln["adm"]]
        out.append(
            [
                ln["adm"],
                messy(a["uhid"]),
                a["doctor"],
                a["ward"],
                date_cell(a["admit"]),
                date_cell(a["discharge"]),
                ln["proc"],
                date_cell(ln["date"]),
                money(ln["cost"]),
                a["outcome"],
                ln["remark"],
            ]
        )
    bad = Counter()
    for i, (reason, count) in enumerate(
        [("missing_adm_id", 4), ("unknown_patient", 6), ("unknown_procedure", 5), ("unknown_doctor", 3)]
    ):
        for j in range(count):
            row = [
                f"ADM/2026/9{i}{j:03d}",
                rng.choice(list(m["patients"])),
                rng.choice(list(m["doctors"])),
                "W-01",
                date_cell(dt.date(2026, 3, 1)),
                "",
                "PRC-001",
                "",
                money(5000),
                "Improved",
                "",
            ]
            if reason == "missing_adm_id":
                row[0] = ""
            elif reason == "unknown_patient":
                row[1] = f"UH00000{j}"
            elif reason == "unknown_procedure":
                row[6] = "PRC-999"
            else:
                row[2] = "KMC-00000"
            out.insert(rng.randrange(len(out)), row)
            bad[reason] += 1
    for row in out:
        ws.append(row)

    ws = wb.create_sheet("Prescriptions")
    ws.append(["Rx No", "Adm ID", "Drug Code", "Dose", "Days", "Prescribed By"])
    rx_rows = [[r["rx"], r["adm"], r["drug"], r["dose"], r["days"], r["by"]] for r in m["rx"]]
    rx_bad = Counter()
    for j in range(4):
        rx_rows.append(
            [f"RX-9{j:05d}", rng.choice(list(m["admissions"])), "DRG-9999", "1 g", 3, rng.choice(list(m["doctors"]))]
        )
        rx_bad["unknown_medication"] += 1
    for j in range(3):
        rx_rows.append([f"RX-8{j:05d}", "ADM/2099/00001", "DRG-0001", "1 g", 3, rng.choice(list(m["doctors"]))])
        rx_bad["unknown_admission"] += 1
    rng.shuffle(rx_rows)
    for row in rx_rows:
        ws.append(row)
    wb.save(path)
    return dict(bad), dict(rx_bad)


def write_april(m, path):
    """Add-data file: new admissions with renamed, reordered headers and a few bad rows."""
    header = ["Procedure Code", "adm id", "Cost", "uhid", "admitting doctor", "ward", "Admit Date", "Outcome"]
    rows, new_adms, new_lines = [], set(), 0
    for n in range(1, 61):
        adm = f"ADM/2026/{2000 + n:05d}"
        a = rng.choice(list(m["admissions"].values()))
        for proc in rng.sample([p for p, x in m["procedures"].items() if x["dept"] == a["dept"]], rng.randint(1, 2)):
            rows.append(
                [
                    proc,
                    adm,
                    money(m["procedures"][proc]["price"]),
                    a["uhid"],
                    a["doctor"],
                    a["ward"],
                    date_cell(dt.date(2026, 4, rng.randint(1, 28))),
                    "Recovered",
                ]
            )
            new_lines += 1
        new_adms.add(adm)
    rows += [
        [
            "PRC-001",
            "ADM/2026/02999",
            money(1000),
            "UH999999",
            rng.choice(list(m["doctors"])),
            "W-01",
            "2026-04-02",
            "Recovered",
        ]
        for _ in range(2)
    ]  # unknown patient
    wb = Workbook()
    ws = wb.active
    ws.title = "April"
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return {"new_admissions": len(new_adms), "new_procedure_lines": new_lines, "rejected": {"unknown_patient": 2}}


def write_documents():
    styles = getSampleStyleSheet()
    story = [
        Paragraph("Infection Prevention and Control Policy", styles["Title"]),
        Paragraph("Policy IPC-04, revision 3, effective 1 July 2025", styles["Normal"]),
        Spacer(1, 10),
        Paragraph("1. Hand hygiene", styles["Heading2"]),
        Paragraph(
            "Staff must perform hand hygiene before and after every patient contact. Alcohol-based hand rub "
            "must be applied for at least 20 seconds; soap and water washing must last at least 40 seconds.",
            styles["Normal"],
        ),
        Paragraph("2. Isolation", styles["Heading2"]),
        Table(
            [
                ["Organism", "Precaution", "Room"],
                ["MRSA", "Contact precautions", "Single room"],
                ["Tuberculosis (pulmonary)", "Airborne precautions", "Negative-pressure room"],
                ["Influenza", "Droplet precautions", "Single room or cohort"],
            ],
            style=TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey)]),
        ),
        Spacer(1, 10),
        Paragraph("3. Urinary catheters", styles["Heading2"]),
        Paragraph(
            "Indwelling urinary catheters are reviewed daily and replaced only when clinically indicated. "
            "The earlier rule of routine replacement every 7 days was withdrawn in July 2025.",
            styles["Normal"],
        ),
        Paragraph("4. Contact", styles["Heading2"]),
        Paragraph(
            "Infection control nurse: Sister Mary Joseph, extension 4410, ipc@kmch-hospital.example.", styles["Normal"]
        ),
    ]
    SimpleDocTemplate(str(OUT / "infection_control_policy.pdf"), pagesize=A4).build(story)

    d = Document()
    d.add_heading("Discharge Protocol", 0)
    d.add_paragraph("This protocol applies to all inpatient wards.")
    d.add_heading("Timelines", 1)
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Step", "Deadline"
    for k, v in [
        ("Discharge summary signed by the consultant", "within 24 hours of discharge"),
        ("Follow-up phone call to the patient", "within 48 hours of discharge"),
        ("Final bill shared with the insurer", "within 6 hours of the discharge decision"),
    ]:
        c = t.add_row().cells
        c[0].text, c[1].text = k, v
    d.add_heading("Medicines at discharge", 1)
    d.add_paragraph(
        "Patients receive a printed medicine chart and at most 7 days of medicines from the hospital pharmacy."
    )
    d.add_heading("Escalation", 1)
    d.add_paragraph(
        "Discharge delays beyond 4 hours are escalated to the nursing superintendent, Ms. Leela Thomas, "
        "+91 98450 22110."
    )
    d.save(OUT / "discharge_protocol.docx")

    text = """PHARMACY STANDARD OPERATING PROCEDURE
====================================

High-alert medicines (insulin, heparin, morphine, potassium chloride) need an independent double check by two
nurses before administration.

Schedule X drugs are stored in a double-locked cabinet. The narcotics register is audited every Monday by the
chief pharmacist.

Vaccines and insulin are stored between 2 and 8 degrees Celsius; fridge temperatures are logged twice a day.

Pharmacy on-call number: +91 82170 44556 (Mr. Rohit Bhandari, chief pharmacist).
"""
    (OUT / "pharmacy_sop.txt").write_bytes(text.replace("\n", "\r\n").encode("utf-8"))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    m = build()
    bad_adm, bad_rx = write_workbook(m, OUT / "hospital_operations.xlsx")
    april = write_april(m, OUT / "admissions_april.xlsx")
    write_documents()

    docs, pats = m["doctors"], m["patients"]
    by_name = {d["name"]: d for d in docs.values()}
    cardio = sorted(d["name"] for d in docs.values() if d["dept"] == "D-CARD")
    neuro_head = docs[m["depts"]["D-NEUR"]["head"]]["name"]
    supervised = next(d for d in docs.values() if d["dept"] == "D-ONCO" and d["supervisor"])
    cost_by_patient = Counter()
    for ln in m["lines"]:
        cost_by_patient[m["admissions"][ln["adm"]]["uhid"]] += ln["cost"]
    (top, top_cost), (_, second) = cost_by_patient.most_common(2)
    assert top == m["top_patient"] and top_cost > second
    drug_count = Counter(m["meds"][r["drug"]]["generic"] for r in m["rx"])
    by_doc = Counter(r["by"] for r in m["rx"])
    busy_doc = by_doc.most_common(1)[0][0]
    insurer_count = Counter(p["insurer"] for p in pats.values())
    ortho_lines = sum(1 for ln in m["lines"] if docs[m["admissions"][ln["adm"]]["doctor"]]["dept"] == "D-ORTH")
    assert by_name  # names are unique

    manifest = {
        "seed": 4242,
        "files": {
            "hospital_operations.xlsx": {
                "sheets": {
                    "ReadMe": "not a table (must be skipped)",
                    "Dept Master": {"header_row": 2, "rows": 10},
                    "Doctors": {"rows": 60},
                    "Patients": {"rows": 404},
                    "Wards": {"rows": 20},
                    "Procedures": {"rows": 50},
                    "Medications": {"rows": 80},
                    "Admissions": {"rows": len(m["lines"]) + sum(bad_adm.values())},
                    "Prescriptions": {"rows": len(m["rx"]) + sum(bad_rx.values())},
                },
                "expected_nodes": {
                    "Department": 10,
                    "Doctor": 60,
                    "Patient": 400,
                    "Insurer": len(INSURERS),
                    "Ward": 20,
                    "Procedure": 50,
                    "Medication": 80,
                    "Admission": len(m["admissions"]),
                    "Prescription": len(m["rx"]),
                },
                "expected_relationships": {
                    "HEADED_BY": 10,
                    "WORKS_IN": 60,
                    "SUPERVISED_BY": 50,
                    "INSURED_BY": 400,
                    "BELONGS_TO": 20,
                    "ADMITTED": len(m["admissions"]),
                    "ADMITTED_BY": len(m["admissions"]),
                    "IN_WARD": len(m["admissions"]),
                    "UNDERWENT": len(m["lines"]),
                    "FOR_ADMISSION": len(m["rx"]),
                    "OF_MEDICATION": len(m["rx"]),
                    "PRESCRIBED_BY": len(m["rx"]),
                },
                "expected_rejections": {"Admissions": bad_adm, "Prescriptions": bad_rx},
                "entity_keys": {
                    "Dept Master": "Dept#",
                    "Doctors": "Reg. No.",
                    "Patients": "UHID",
                    "Wards": "Ward",
                    "Procedures": "Code",
                    "Medications": "Drug Code",
                    "Admissions": "Adm ID",
                    "Prescriptions": "Rx No",
                },
                "expected_links": [
                    ["Department", "Doctor"],
                    ["Doctor", "Department"],
                    ["Doctor", "Doctor"],
                    ["Ward", "Department"],
                    ["Admission", "Patient"],
                    ["Admission", "Doctor"],
                    ["Admission", "Ward"],
                    ["Admission", "Procedure"],
                    ["Prescription", "Admission"],
                    ["Prescription", "Medication"],
                    ["Prescription", "Doctor"],
                ],
                "pii": {
                    "Doctors": {"Doctor Name": "person_name", "Mobile": "phone"},
                    "Patients": {
                        "Full Name": "person_name",
                        "Date of Birth": "date_of_birth",
                        "Aadhaar": "government_id",
                        "Phone": "phone",
                    },
                    "Admissions": {"Remarks": "free_text"},
                },
                "not_pii_traps": [
                    "Doctors.Specialisation",
                    "Dept Master.Department",
                    "Medications.Generic Name",
                    "Procedures.Procedure Name",
                    "Patients.City",
                    "Patients.Insurer",
                    "Doctors.Joined On",
                ],
            },
            "admissions_april.xlsx": {"expected": april},
            "infection_control_policy.pdf": {"pii": {"person_name": 1, "email": 1}},
            "discharge_protocol.docx": {"pii": {"person_name": 1, "phone": 1}},
            "pharmacy_sop.txt": {"pii": {"person_name": 1, "phone": 1}},
        },
        "questions": [
            {"kb": "graph", "level": "core", "q": "How many patients are registered?", "answer": 400},
            {"kb": "graph", "level": "core", "q": "Which doctors work in the Cardiology department?", "answer": cardio},
            {"kb": "graph", "level": "core", "q": "Who is the head of the Neurology department?", "answer": neuro_head},
            {
                "kb": "graph",
                "level": "core",
                "q": f"Who supervises {supervised['name']}?",
                "answer": docs[supervised["supervisor"]]["name"],
            },
            {
                "kb": "graph",
                "level": "core",
                "q": "Which patient had the highest total procedure cost?",
                "answer": pats[top]["name"],
            },
            {
                "kb": "graph",
                "level": "core",
                "q": "How many admissions ended with the outcome Transferred?",
                "answer": sum(a["outcome"] == "Transferred" for a in m["admissions"].values()),
            },
            {
                "kb": "graph",
                "level": "core",
                "q": "Which medication (generic name) was prescribed most often?",
                "answer": drug_count.most_common(1)[0][0],
            },
            {
                "kb": "graph",
                "level": "core",
                "q": f"How many prescriptions did {docs[busy_doc]['name']} write?",
                "answer": by_doc[busy_doc],
            },
            {
                "kb": "graph",
                "level": "core",
                "q": "Which insurer covers the most patients?",
                "answer": insurer_count.most_common(1)[0][0],
            },
            {"kb": "graph", "level": "core", "q": "Which ward has the most beds?", "answer": "W-07"},
            {
                "kb": "graph",
                "level": "core",
                "q": "What is the base price of the Total knee replacement procedure?",
                "answer": m["procedures"]["PRC-012"]["price"],
            },
            {
                "kb": "graph",
                "level": "stretch",
                "q": "How many procedures were performed in admissions handled by Orthopaedics doctors?",
                "answer": ortho_lines,
            },
            {
                "kb": "graph",
                "level": "stretch",
                "q": "How many patients have blood group O negative?",
                "answer": sum(p["blood"] == "O-" for p in pats.values()),
                "trap": "stored as 'O-'",
            },
            {"kb": "rag", "level": "core", "q": "How long must alcohol hand rub be applied?", "answer": ["20"]},
            {
                "kb": "rag",
                "level": "core",
                "q": "What precautions are needed for a patient with tuberculosis?",
                "answer": ["airborne"],
            },
            {
                "kb": "rag",
                "level": "core",
                "q": "How often should urinary catheters be replaced?",
                "answer": ["clinically indicated"],
                "trap": "withdrawn 7-day rule is also in the document",
            },
            {"kb": "rag", "level": "core", "q": "When must the discharge summary be signed?", "answer": ["24 hours"]},
            {"kb": "rag", "level": "core", "q": "How often is the narcotics register audited?", "answer": ["monday"]},
            {
                "kb": "rag",
                "level": "core",
                "q": "Which medicines need an independent double check?",
                "answer": ["insulin", "heparin"],
            },
        ],
    }
    (ROOT / "expected" / "hospital_manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    print(json.dumps(manifest["files"]["hospital_operations.xlsx"]["expected_nodes"]))


if __name__ == "__main__":
    main()
