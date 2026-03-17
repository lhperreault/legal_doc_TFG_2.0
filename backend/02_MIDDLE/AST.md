# Phase 2: AST Construction — Complete Planning Document

**Scope:** This document covers ONLY the AST construction step (scripts 01 and 02 in `backend/02_MIDDLE/`). Stop after these two scripts are built and tested. Do not build extraction, knowledge graphs, or vector search yet.

**For Claude Code:** Read this entire document before writing any code. The Supabase schema changes (Section 6) will be applied manually — do NOT run SQL migrations. Your job is to write the two Python scripts described in Section 4, following the conventions in Section 7.


---

## 1. Project Structure

### 1.1 Folder Layout

```
ProjectRoot/
├── backend/
│   ├── 01_INITIAL/          # Phase 1 — COMPLETE. Do not modify.
│   │   ├── 01_Intake.py
│   │   ├── 02_doc_detection.py
│   │   ├── 03_image_extraction.py
│   │   ├── 04_text_extraction.py
│   │   ├── 05_doc_classification.py
│   │   ├── 06_TOC_detection.py
│   │   ├── 07_Yes_TOC.py
│   │   ├── 07_No_TOC.py
│   │   ├── 07_Native_TOC.py
│   │   ├── 07_HTML_TOC.py
│   │   ├── 08_Send_Supabase.py
│   │   └── main.py
│   ├── 02_MIDDLE/            # Phase 2 — YOU BUILD THIS.
│   │   ├── 01_AST_tree_build.py
│   │   ├── 02_AST_semantic_label.py
│   │   └── main.py           # Orchestrator for Phase 2 scripts
│   ├── 03_SEARCH/            # Phase 3 — future
│   ├── 04_AGENTS/            # Phase 4 — future
│   ├── zz_temp_chunks/       # Intermediate files from all phases
│   └── data_storage/
│       ├── documents/        # Original uploaded files
│       └── info/images/      # Extracted images
├── frontend/                 # Phase 5 — future
├── zz_Mockfiles/             # Test documents
└── .env                      # API keys (project root)
```

### 1.2 Environment Variables Needed (.env at project root)

```
OPENAI_API_KEY=...            # Required for 02_AST_semantic_label.py
SUPABASE_URL=...              # Required for both scripts (read/write)
SUPABASE_SERVICE_ROLE_KEY=... # Required for both scripts
```


---

## 2. What You Have (Input from Phase 1)

### 2.1 Supabase `documents` Table

Each processed document has one row:

| Column | Type | What It Gives You |
|---|---|---|
| `id` | UUID (PK) | Foreign key for sections |
| `file_name` | TEXT (UNIQUE) | Document stem. Your lookup key. |
| `document_type` | TEXT | Legal classification from step 05 (e.g., "Contract - NDA", "Pleading - Complaint"). **This tells the AST which ontology to use.** |
| `confidence_score` | FLOAT | 0-1. Low confidence = flag for HITL review. |
| `full_text_md` | TEXT | Complete structured markdown. Backup text source. |
| `has_native_toc` | BOOLEAN | True = section hierarchy from embedded PDF bookmarks (most reliable). |
| `total_pages` | INTEGER | Page count. May be NULL. |
| `tagged_xhtml_url` | TEXT | Public URL to tagged XHTML. HTML docs only. |
| `created_at` | TIMESTAMPTZ | Auto-set. |
| `updated_at` | TIMESTAMPTZ | Updated on every upsert. |

### 2.2 Supabase `sections` Table — YOUR PRIMARY INPUT

Each document has multiple rows here — one per section. **This is what you read to build the AST.**

| Column | Type | What It Gives You |
|---|---|---|
| `id` | UUID (PK) | Becomes the AST node ID |
| `document_id` | UUID (FK) | References documents(id) ON DELETE CASCADE |
| `level` | INTEGER | **Hierarchy depth: 0 = top level, 1 = subsection, 2 = sub-subsection. This is the implicit tree you reconstruct.** |
| `section_title` | TEXT | The heading text (real or synthetic) |
| `section_text` | TEXT | **Full extracted text. This is what GPT-4o-mini reads to assign semantic labels.** |
| `page_range` | TEXT | String like "5-12". For provenance. |
| `start_page` | FLOAT | Numeric start. FLOAT because pandas outputs it that way. |
| `end_page` | FLOAT | Numeric end. |
| `is_synthetic` | BOOLEAN | True = AI-generated heading from 07_No_TOC. |
| `anchor_id` | TEXT | HTML element ID (ai-chunk-NNNNN). HTML docs only. |
| `created_at` | TIMESTAMPTZ | Auto-set. |

### 2.3 What You Do NOT Have Yet (and are building now)

- `parent_section_id` — parent-child relationships between sections (implicit in `level`, not explicit)
- `semantic_label` — what *kind* of legal element each section is
- `semantic_confidence` — how confident the labeler is
- `label_source` — whether the label came from pattern matching or GPT-4o-mini


---

## 3. What the AST Builds (Output)

The AST has two parts: **tree reconstruction** (script 01) and **semantic labeling** (script 02).

### 3.1 Part A: Tree Reconstruction — `01_AST_tree_build.py`

Takes the flat `sections` rows ordered by position and reconstructs parent-child relationships using the `level` integers.

**New column it writes to:**

| Column | Type | Description |
|---|---|---|
| `parent_section_id` | UUID (FK to sections.id, nullable) | Points to the parent section's `id`. NULL for root nodes (level 0). |

**Stack-based algorithm:**
```
Stack = []
For each section row ordered by (document_id, start_page, level):
    While stack is not empty AND stack[-1].level >= current.level:
        stack.pop()
    If stack is not empty:
        current.parent_section_id = stack[-1].id
    Else:
        current.parent_section_id = NULL  # root node
    stack.append(current)
```

What this gives you: every section knows its parent. You can walk up to find context ("this clause is inside Article III which is inside Payment Terms") or walk down to find children ("what subsections does Article III contain?").

### 3.2 Part B: Semantic Labeling — `02_AST_semantic_label.py`

Each AST node gets a semantic label describing *what kind of legal element* this section represents.

**New columns it writes to:**

| Column | Type | Description |
|---|---|---|
| `semantic_label` | TEXT | The ontology label. E.g., "obligation.payment.deadline" |
| `semantic_confidence` | FLOAT | GPT-4o-mini's confidence in the label. 0-1. |
| `label_source` | TEXT | "pattern" or "gpt-4o-mini". Tracks how the label was assigned. |

### 3.3 Where Output Goes

**Add columns to existing `sections` table** (see Section 6 for SQL). No new tables needed for AST. The sections table becomes your one-stop shop: Phase 1 data + Phase 2 AST data in one place.


---

## 4. Script Architecture

### 4.1 Script: `01_AST_tree_build.py`

**Location:** `backend/02_MIDDLE/01_AST_tree_build.py`

**Purpose:** Reconstruct parent-child relationships from level integers. Pure Python. No AI calls.

**Input:** document_id (CLI arg) OR file_name (CLI arg). Reads sections from Supabase.

**Output:** Updates `parent_section_id` column in sections table for the given document.

**Process:**
1. Load environment variables from `.env` at project root.
2. Initialize Supabase client.
3. Accept CLI arg: either `--document_id <uuid>` or `--file_name <stem>`. If file_name given, look up document_id from documents table.
4. Query all sections for the document, ordered by `start_page ASC, level ASC`.
5. Walk the rows using the stack algorithm from Section 3.1.
6. Batch update `parent_section_id` in Supabase for each section row.
7. Print `SUCCESS: Tree built for {file_name}. {N} sections processed, {R} root nodes, max depth {D}.` or `ERROR: {reason}`.

**Edge cases to handle:**
- Document has only 1 section (root only) — still valid, parent_section_id = NULL.
- All sections have level 0 (flat document, no hierarchy) — all get parent_section_id = NULL.
- Missing or NULL start_page values — fall back to ordering by `created_at` or row position.
- No sections found for document_id — print error, exit gracefully.

**Estimated size:** ~80-100 lines of code.

### 4.2 Script: `02_AST_semantic_label.py`

**Location:** `backend/02_MIDDLE/02_AST_semantic_label.py`

**Purpose:** Assign semantic ontology labels to each AST node using GPT-4o-mini.

**Input:** document_id or file_name (CLI arg). Reads sections (with parent_section_id already set) and document_type from Supabase.

**Output:** Updates `semantic_label`, `semantic_confidence`, `label_source` columns in sections table.

**Process:**
1. Load .env, initialize Supabase + OpenAI clients.
2. Accept CLI arg: `--document_id <uuid>` or `--file_name <stem>`.
3. Read `document_type` from documents table for this document.
4. Select the correct ontology label set based on document_type:
   - document_type starts with "Contract" → use contract ontology (Section 5.1)
   - document_type starts with "Pleading" → use complaint ontology (Section 5.2)
   - document_type contains "Financial" or "10-K" or "10-Q" → use financial ontology (Section 5.3)
   - document_type contains "Annual Report" → use annual report ontology (Section 5.4)
   - anything else → use a generic "unknown" label and flag for HITL review
5. Query all sections for the document, ordered by start_page.
6. For financial/annual report types, run **pattern matching first**:
   - Match section_title keywords against known labels (e.g., title contains "Balance Sheet" → `financial_root.balance_sheet`).
   - Mark `label_source = "pattern"` for matches.
7. For all remaining unlabeled sections (and all contract/complaint sections), call GPT-4o-mini:
   - **System prompt:** "You are a legal document analyst. Given a section from a {document_type}, classify it using ONLY the following ontology labels: {ontology_tree}. Return a JSON object with 'semantic_label' (string, must be from the provided list) and 'confidence' (float 0-1)."
   - **User prompt:** "Section title: {section_title}\nParent section title: {parent_section_title or 'None (root level)'}\nSection text (first 1500 chars): {section_text[:1500]}"
   - Use structured output (Pydantic response_format) returning `semantic_label` + `confidence`.
   - Mark `label_source = "gpt-4o-mini"`.
8. Apply confidence threshold rules:
   - confidence < 0.7 → label assigned but flagged for HITL review (store the label, log the flag)
   - confidence >= 0.7 → auto-approved
9. Batch update Supabase: `semantic_label`, `semantic_confidence`, `label_source` for each section.
10. Print `SUCCESS: Labeled {N} sections for {file_name}. {P} pattern-matched, {G} GPT-labeled, {F} flagged for review.` or `ERROR: {reason}`.

**Edge cases to handle:**
- GPT returns a label not in the ontology → set label to "unrecognized", confidence to 0.0, flag for review.
- GPT call fails (timeout, rate limit) → try/except with retry (max 2 retries per section), then mark as "error" with confidence 0.0.
- Section has empty or very short section_text (<50 chars) → still attempt labeling but use section_title only.
- document_type is NULL or empty → use generic label, flag entire document.
- OpenAI API key missing → print error, exit gracefully without crashing.

**Estimated size:** ~200-250 lines of code.

### 4.3 Script: `main.py`

**Location:** `backend/02_MIDDLE/main.py`

**Purpose:** Orchestrate Phase 2 scripts sequentially via subprocess, same pattern as `backend/01_INITIAL/main.py`.

**Process:**
1. Accept CLI arg: `--file_name <stem>` (same as Phase 1 orchestrator).
2. Look up document_id from Supabase using file_name.
3. Run `01_AST_tree_build.py --document_id {uuid}` via subprocess.
4. Check stdout for "SUCCESS:" or "ERROR:". If error, stop and report.
5. Run `02_AST_semantic_label.py --document_id {uuid}` via subprocess.
6. Check stdout. Report final status.

**Estimated size:** ~50-60 lines of code.


---

## 5. Ontology Label Sets

These are the bounded label sets you put in the GPT-4o-mini system prompt. The model MUST return one of these labels — nothing else.

### 5.1 Contract Ontology

Use when `document_type` starts with "Contract".

```
contract_root
preamble
preamble.title_block
preamble.parties
preamble.recitals
preamble.effective_date
definitions
definitions.term
scope
scope.subject_matter
scope.exclusions
obligation
obligation.performance
obligation.payment
obligation.payment.amount
obligation.payment.schedule
obligation.payment.method
obligation.delivery
obligation.reporting
obligation.notification
rights
rights.license_grant
rights.audit_rights
rights.step_in_rights
condition
condition.precedent
condition.subsequent
condition.concurrent
representation
representation.authority
representation.compliance
representation.financial
representation.no_litigation
warranty
warranty.product_quality
warranty.service_level
warranty.ip_ownership
covenant
covenant.non_compete
covenant.non_solicitation
covenant.non_disclosure
covenant.exclusivity
indemnification
indemnification.scope
indemnification.limitations
indemnification.procedure
liability
liability.limitation
liability.cap
liability.exclusion
termination
termination.for_cause
termination.for_convenience
termination.expiration
termination.effects
dispute_resolution
dispute_resolution.governing_law
dispute_resolution.jurisdiction
dispute_resolution.arbitration
dispute_resolution.mediation
confidentiality
confidentiality.scope
confidentiality.exceptions
confidentiality.duration
ip_rights
ip_rights.ownership
ip_rights.license
ip_rights.assignment
insurance
force_majeure
amendment_procedure
assignment
notices
severability
entire_agreement
signature_block
exhibit_reference
schedule_reference
```

### 5.2 Complaint Ontology

Use when `document_type` starts with "Pleading".

```
complaint_root
caption
caption.court
caption.parties
caption.case_number
introduction
jurisdiction
jurisdiction.subject_matter
jurisdiction.personal
venue
parties
parties.plaintiff
parties.defendant
factual_allegations
factual_allegations.background
factual_allegations.relationship
factual_allegations.breach_event
factual_allegations.damages_description
factual_allegations.timeline
causes_of_action
causes_of_action.breach_of_contract
causes_of_action.negligence
causes_of_action.fraud
causes_of_action.statutory_violation
causes_of_action.unjust_enrichment
causes_of_action.declaratory_relief
damages
damages.compensatory
damages.consequential
damages.punitive
damages.statutory
damages.equitable_relief
prayer_for_relief
jury_demand
verification
signature_block
exhibit_reference
certificate_of_service
```

### 5.3 Financial Statement Ontology

Use when `document_type` contains "Financial" or "10-K" or "10-Q". **Pattern-match first on section titles before calling GPT.**

```
financial_root
cover_page
management_discussion
auditor_report
balance_sheet
income_statement
cash_flow_statement
equity_statement
notes_to_financials
notes.accounting_policies
notes.revenue_recognition
notes.debt_obligations
notes.contingencies
notes.related_party
supplementary_schedules
signature_block
```

**Pattern matching rules for financial docs:**
- Title contains "Balance Sheet" or "Statement of Financial Position" → `balance_sheet`
- Title contains "Income Statement" or "Statement of Operations" or "Profit and Loss" → `income_statement`
- Title contains "Cash Flow" → `cash_flow_statement`
- Title contains "Stockholders' Equity" or "Changes in Equity" → `equity_statement`
- Title contains "MD&A" or "Management Discussion" or "Management's Discussion" → `management_discussion`
- Title contains "Auditor" or "Independent Registered" → `auditor_report`
- Title contains "Notes to" and ("Financial" or "Consolidated") → `notes_to_financials`
- Title contains "Accounting Polic" → `notes.accounting_policies`
- Title contains "Revenue Recognition" → `notes.revenue_recognition`
- Title contains "Debt" or "Borrowings" → `notes.debt_obligations`
- Title contains "Contingenc" → `notes.contingencies`
- Title contains "Related Part" → `notes.related_party`

### 5.4 Annual Report Ontology

Use when `document_type` contains "Annual Report".

```
annual_report_root
letter_to_shareholders
company_overview
business_segments
risk_factors
legal_proceedings
executive_compensation
corporate_governance
financial_statements
market_data
appendices
```


---

## 6. Supabase Schema Changes (MANUAL — Do Not Automate)

Run these SQL statements manually in the Supabase SQL Editor before running the scripts.

```sql
-- Add Phase 2 AST columns to existing sections table
ALTER TABLE sections ADD COLUMN parent_section_id UUID REFERENCES sections(id);
ALTER TABLE sections ADD COLUMN semantic_label TEXT;
ALTER TABLE sections ADD COLUMN semantic_confidence FLOAT;
ALTER TABLE sections ADD COLUMN label_source TEXT DEFAULT 'pending';

-- Optional: index for tree traversal queries
CREATE INDEX idx_sections_parent ON sections(parent_section_id);

-- Optional: index for semantic label queries
CREATE INDEX idx_sections_semantic ON sections(semantic_label);
```

**Verify the columns exist before running scripts.** Script 01 writes to `parent_section_id`. Script 02 writes to `semantic_label`, `semantic_confidence`, `label_source`.


---

## 7. Code Conventions (Match Phase 1 Patterns)

### 7.1 File Naming
- Scripts numbered: `01_`, `02_`, etc.
- All scripts in `backend/02_MIDDLE/`
- Intermediate files (if any) go to `backend/zz_temp_chunks/`

### 7.2 CLI Pattern
Every script accepts CLI args and can run standalone:
```bash
python 01_AST_tree_build.py --file_name "my_contract"
python 02_AST_semantic_label.py --document_id "abc-123-uuid"
```
Use `argparse` for argument parsing.

### 7.3 Output Pattern
Every script prints `SUCCESS: ...` or `ERROR: ...` as its final line. The orchestrator (`main.py`) parses this to decide whether to continue.

### 7.4 Environment Loading
```python
from dotenv import load_dotenv
import os

# Load .env from project root (two levels up from backend/02_MIDDLE/)
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
```

### 7.5 Supabase Client Pattern
```python
from supabase import create_client

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_ROLE_KEY"]
)
```

### 7.6 Error Handling
- Wrap all Supabase calls in try/except.
- Wrap all OpenAI calls in try/except with retry logic (max 2 retries).
- Missing API keys → print ERROR, exit gracefully, do not crash.
- Empty query results → print ERROR with helpful message, exit gracefully.

### 7.7 GPT-4o-mini Structured Output Pattern (from Phase 1)
```python
from pydantic import BaseModel
from openai import OpenAI

class SemanticLabel(BaseModel):
    semantic_label: str
    confidence: float

client = OpenAI()
response = client.beta.chat.completions.parse(
    model="gpt-4o-mini",
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ],
    response_format=SemanticLabel
)
result = response.choices[0].message.parsed
```

### 7.8 Dependencies
These should already be installed from Phase 1:
- `supabase-py`
- `openai`
- `pydantic`
- `python-dotenv`
- `pandas` (if needed for any data handling)

No new dependencies required for AST construction.


---

## 8. STOP HERE

After building and testing `01_AST_tree_build.py`, `02_AST_semantic_label.py`, and `main.py`, STOP.

Do NOT proceed to:
- Entity extraction (Step 2 — future)
- Knowledge graph construction (Step 3 — future)
- Graph analytics (Step 4 — future)
- Vector storage (Step 5 — future)
- Agentic workflows (Step 6 — future)

These will be planned in a separate document after the AST is validated against real documents.