# Hospital Management (FHIR) for Odoo 20

A custom Odoo 20 module for running an outpatient clinic or small hospital: registration, appointments, consultations, vital signs, diagnoses, prescriptions, laboratory and billing. Clinical data is coded with international standards and exposed through an **HL7 FHIR R4** API, so other health systems can read it.

## Features

| Area | What it does |
|---|---|
| **Registration** | Patients with automatic medical record numbers (`MRN/2026/00001`), practitioners linked to Odoo users |
| **Appointments** | Booking with list and calendar views; a doctor cannot be double-booked; Check In automatically opens a visit |
| **Encounters (visits)** | Start → Finish workflow. A finished or cancelled visit is fully locked (notes, reason, type, vitals, diagnoses, prescriptions, lab orders); only an administrator can reopen it, with a reason. Finished or invoiced visits cannot be cancelled |
| **Vital signs** | Temperature, BP, pulse, respiratory rate, SpO₂, weight, height. Coded with **LOINC**, with range checks to catch typing errors |
| **Diagnoses** | Picked from **ICD-10** (26 common codes included; more can be added) |
| **Prescriptions** | Dose, route, frequency (OD/BD/TDS/QID/STAT/PRN), duration, auto-calculated quantity (PRN requires an explicit quantity). Drugs coded with **WHO ATC**, routes with **SNOMED CT**. Printable PDF prescription |
| **Laboratory** | Doctor orders → lab worklist → result released → doctor notified. Only the lab sets a result (and a numeric value must actually be entered); only the ordering doctor changes the order. Released results are corrected through **Correct Result** with a reason; the doctor is notified and FHIR shows `corrected`. Normal ranges with automatic Low / High / Positive flags. Tests coded with **LOINC** |
| **Billing** | Cash or insurance invoices through Odoo Invoicing; each item remembers the invoice line that charged it, so items added during an amendment are billed on a supplementary invoice and billed items cannot be changed silently |
| **Security & audit** | Role-based and field-level access, admin-only amendments and reassignments (logged), tamper-proof access log covering form views, list views, exports, external scripts (XML-RPC / JSON-RPC) and API reads |
| **Multi-company** | Patients, visits and their records belong to a company (branch); users only see their own companies |

## Roles

| Role | Can | Cannot |
|---|---|---|
| Receptionist | Register patients, book and check in appointments | See any clinical data, delete patients |
| Nurse | Everything a receptionist can + read all visits, record vital signs | Diagnose, prescribe, edit visits, delete vital signs |
| Doctor | Everything a nurse can + diagnose, prescribe and order tests **for their own patients** | Edit another doctor's visits, move a visit to another doctor, enter or release lab results |
| Lab Technician | Work the lab worklist, release and correct results | See appointments, visit reasons, notes, diagnoses or prescriptions; change what was ordered |
| Cashier | Invoice finished visits and take payments | See appointments, visit reasons or any clinical data |
| Administrator | Everything, including reopening closed visits, moving visits and reading the access log | — |

## FHIR R4 API

Base URL: `http://<server>:<port>/fhir/R4/`

| Resource | Source in Odoo | Example search |
|---|---|---|
| `Patient` | Patients | `Patient?family=wanjiru`, `Patient?identifier=MRN/2026/00001` |
| `Practitioner` | Practitioners | `Practitioner?name=otieno` |
| `Appointment` | Appointments | `Appointment?patient=Patient/1&status=booked` |
| `Encounter` | Visits | `Encounter?patient=Patient/1&status=finished` |
| `Observation` | Vital signs | `Observation?patient=Patient/1&code=http://loinc.org\|8310-5` |
| `Condition` | Diagnoses | `Condition?patient=Patient/1&code=B50.9` |
| `MedicationRequest` | Prescriptions | `MedicationRequest?patient=Patient/1&status=active` |
| `ServiceRequest` | Lab orders | `ServiceRequest?patient=Patient/1` |
| `DiagnosticReport` | Released lab results | `DiagnosticReport?patient=Patient/1` |
| `AuditEvent` | Access log (admins only) | `AuditEvent?patient=Patient/1` |

Search results are paged: `total` is the real number of matches, `_count` sets the page size (default 50, max 500) and the Bundle's `next` / `previous` links fetch the other pages. `_id` (comma-separated) is supported. Unsupported parameters are ignored and reported in an `OperationOutcome` entry, unless the client sends `Prefer: handling=strict`, which returns `400` instead.

Also available:
- `GET /fhir/R4/metadata`: CapabilityStatement
- `GET /fhir/R4/Patient/<id>/$everything`: the patient's complete record in one Bundle

**Authentication:** create an Odoo API key with scope **FHIR API** (My Preferences → Account Security → Create API Key) and send it as a bearer token:

```bash
curl -H "Authorization: Bearer <API_KEY>" http://localhost:8070/fhir/R4/Patient/1
```

A logged-in browser session also works. Results always follow the user's role: a receptionist's key gets `403` on clinical resources. Every API read is recorded in the access log.

## Installation

Requires **Odoo 20** (Python 3.12+, PostgreSQL 16+). Depends on `mail` and `account` (Invoicing); both are installed automatically.

1. Copy the `hospital_management` folder into your addons path (e.g. `~/odoo20/addons`).
2. Restart Odoo, enable developer mode, then **Apps → Update Apps List**.
3. Search for **Hospital Management (FHIR)** and click **Activate**.
4. **One-time accounting setup:** Invoicing → Configuration → Settings → **Fiscal Localization** → choose your country (e.g. Kenya) → Save.
5. Settings → Users: give each staff member a **Hospital** role (plus **Hospital Laboratory** / **Hospital Billing** where needed).

### Running with Docker

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    environment: [POSTGRES_DB=postgres, POSTGRES_USER=odoo, POSTGRES_PASSWORD=odoo]
    volumes: [odoo20-db-data:/var/lib/postgresql/data]
  web:
    image: odoo:20.0
    depends_on: [db]
    ports: ["8070:8069"]
    environment: [HOST=db, USER=odoo, PASSWORD=odoo]
    volumes:
      - odoo20-web-data:/var/lib/odoo
      - ./config:/etc/odoo
      - ./addons:/mnt/extra-addons
volumes:
  odoo20-db-data:
  odoo20-web-data:
```

In `config/odoo.conf`, set `http_interface = 0.0.0.0`. Odoo 20 listens only on `127.0.0.1` by default, which Docker cannot reach.

Upgrade after pulling changes:
```bash
docker compose exec web odoo -d <database> -u hospital_management --stop-after-init --no-http
docker compose restart web
```

## Configuration

Under **Hospital → Configuration**:
- **Practitioners**: link each doctor to their Odoo user (required for "own patients only" rules)
- **ICD-10 Codes**, **Medications**, **Lab Tests**: extend the starter lists; set prices and lab normal ranges
- **Insurers**: companies that receive insurance invoices
- **Access Log**: who viewed, exported or amended which patient's records

Starter prices are examples in **Kenyan shillings**. They are only loaded into a company that invoices in KES; for any other currency they stay empty. If you set up accounting (Kenya) after installing, use **Configuration → Load Example Prices (KES)**. Lab normal ranges are general adult values. Set your own before real use.

A patient with visits or appointments cannot be deleted (the medical record is kept); archive them instead. The access log stores its own copy of the patient's name and MRN, so it never blocks deleting a patient and stays readable afterwards.

## Project structure

```
hospital_management/
├── models/        patient, practitioner, appointment, encounter, observation, condition,
│                  medication, lab, billing, audit, fhir_utils
├── controllers/   fhir.py — the FHIR R4 REST API
├── views/         screens and menus
├── security/      roles (hms_security.xml) and access rules (ir.access.csv)
├── data/          sequences, ICD-10 codes, drugs, lab tests, consultation products
└── report/        printable prescription
```

## Odoo 20 notes for developers

Changes from Odoo 19 that this module uses:
- Access rights live in `security/ir.access.csv` (`id,name,model_id,group_id/id,operation,domain`). This replaces `ir.model.access.csv` and record rules.
- Groups use `privilege_id` / `user_ids`; users use `group_ids`.
- SQL constraints use `models.Constraint(...)` instead of `_sql_constraints`.
- `ir.actions.report` no longer has `report_file`.
- API keys must have an expiry date; routes can use `auth='bearer'` with a custom `bearer_scope`.

## Author

Muhammadswadiq ([@muhammadswadiq007](https://github.com/muhammadswadiq007))

## License

LGPL-3
