# Phase 2 Continued: Extraction → Knowledge Graphs → Graph Analytics

**Status:** AST construction (scripts 01 + 02) is complete. Every section in Supabase now has `parent_section_id`, `semantic_label`, `semantic_confidence`, and `label_source`.

**Scope:** This document covers the three remaining steps inside `backend/02_MIDDLE/`:

| Step | Script | What It Does |
|------|--------|--------------|
| 3 | `03_entity_extraction.py` | Extracts parties, dates, amounts, obligations, claims from each AST node |
| 4 | `04_kg_build.py` | Builds a knowledge graph (nodes + edges) from extractions |
| 5 | `05_graph_analytics.py` | Runs cross-document analytics: timelines, conflicts, claim→evidence paths |

After these three scripts are built and tested, Phase 2 is done. Phase 3 (vector search / RAG) and Phase 4 (agents) remain separate.

---

## 0. Model & Tool Strategy

This section defines which model or tool handles which task across all three scripts. Follow this routing — do not default everything to one model.

### 0.1 Model Routing

| Task | Model / Tool | Why |
|------|-------------|-----|
| Date, amount, citation pre-extraction | **LexNLP** (deterministic) | Free, fast, no API cost. Runs first as a pre-filter. |
| Party names, simple dates, amounts, evidence refs | **Gemini Flash** (`gemini-2.0-flash`) | Cheap, fast, good at slot-filling with bounded Pydantic schemas |
| Obligations, claims, conditions, causes of action | **GPT-4o-mini** | Needs legal reasoning about who owes what to whom |
| Fuzzy entity deduplication (Step 4) | **GPT-4o-mini** | "Is J. Smith the same as John Smith?" requires judgment |
| Generic fallback extraction (unlabeled sections) | **GPT-4o-mini** | Open-ended extraction needs stronger reasoning |
| Vector embeddings (Phase 3 — NOT NOW) | **Legal-BERT** (`nlpaueb/legal-bert-base-uncased`) | Domain-specific similarity for legal text. Save for Phase 3. |
| Agentic QA (Phase 4 — NOT NOW) | **Claude API** | Long-context reasoning over KG + documents. Save for Phase 4. |

### 0.2 LexNLP as Pre-Extraction Filter

LexNLP is a Python library for extracting structured data from legal text using regex and pattern matching. It handles dates, amounts, definitions, conditional statements, and legal citations well — but it overpredicts on some entity types (especially money/currency) and uses regex internally, not NER models.

**DO NOT trust LexNLP alone.** Use it as a pre-pass: run LexNLP extractors on the section text first, collect what it finds, then pass those preliminary results to the LLM as hints. The LLM validates, corrects, and enriches them with relationship context (who pays whom, what a date means, etc.).

**Install:** `pip install lexnlp` (AGPLv3 license — check if acceptable for your use case). Last release was 2.3.0 (Nov 2022), Python 3.8+. It works on 3.10+ but may throw deprecation warnings.

**Useful extractors for this pipeline:**
```python
import lexnlp.extract.en.dates as lexnlp_dates
import lexnlp.extract.en.amounts as lexnlp_amounts
import lexnlp.extract.en.money as lexnlp_money
import lexnlp.extract.en.conditions as lexnlp_conditions
import lexnlp.extract.en.definitions as lexnlp_definitions
import lexnlp.extract.en.durations as lexnlp_durations

# Example usage on a section's text:
dates = list(lexnlp_dates.get_dates(section_text))
amounts = list(lexnlp_amounts.get_amounts(section_text))
money = list(lexnlp_money.get_money(section_text))
conditions = list(lexnlp_conditions.get_conditions(section_text))
definitions = list(lexnlp_definitions.get_definitions(section_text))
durations = list(lexnlp_durations.get_durations(section_text))
```

**LexNLP results are passed TO the LLM, not stored directly.** The LLM prompt includes a "Pre-extracted hints" section with whatever LexNLP found, and the LLM decides what to keep, discard, or correct.

### 0.3 Gemini Flash Client Setup

Gemini Flash is already in the codebase. Use `google.generativeai` for structured output. The pattern:

```python
import google.generativeai as genai

genai.configure(api_key=os.environ.get("GOOGLE_API_KEY"))
gemini_model = genai.GenerativeModel("gemini-2.0-flash")
```

For structured output, Gemini Flash supports JSON mode. Send the Pydantic schema as part of the system prompt and instruct it to return JSON only. Parse the response with the same Pydantic model. If Gemini returns invalid JSON or a label not in the schema, fall back to GPT-4o-mini for that section.

### 0.4 Template → Model Routing Map

```python
# Which model handles which extraction template
TEMPLATE_TO_MODEL = {
    "party":        "gemini-flash",    # slot-filling, bounded schema
    "date":         "gemini-flash",    # slot-filling, LexNLP pre-hints help a lot here
    "amount":       "gemini-flash",    # slot-filling, LexNLP pre-hints help a lot here
    "evidence_ref": "gemini-flash",    # simple pattern recognition
    "obligation":   "gpt-4o-mini",     # needs legal reasoning
    "claim":        "gpt-4o-mini",     # needs legal reasoning
    "condition":    "gpt-4o-mini",     # needs legal reasoning
    "generic":      "gpt-4o-mini",     # open-ended, needs stronger model
}
```

### 0.5 Environment Variables Needed

Add to `.env` at project root:
```
GOOGLE_API_KEY=...            # Required for Gemini Flash in 03_entity_extraction.py
OPENAI_API_KEY=...            # Required for GPT-4o-mini in 03, 04
SUPABASE_URL=...              # Required for all scripts
SUPABASE_SERVICE_ROLE_KEY=... # Required for all scripts
```

### 0.6 Fallback Chain

If a Gemini Flash call fails (timeout, rate limit, invalid response), retry once, then fall back to GPT-4o-mini for that section. If GPT-4o-mini also fails after retries, mark the section as `extraction_method = "error"` and move on. Do not crash the pipeline over a single failed extraction.

---

## 1. What You Have Now (Post-AST)

### 1.1 Sections Table (Updated)

Every section row now carries:

| Column | Source | What It Gives Step 3 |
|--------|--------|---------------------|
| `id` | Phase 1 | Becomes the provenance anchor — every extraction links back here |
| `document_id` | Phase 1 | Groups sections by document |
| `level` | Phase 1 | Hierarchy depth |
| `section_title` | Phase 1 | Used as context for GPT extraction prompts |
| `section_text` | Phase 1 | The actual text GPT reads to extract entities |
| `page_range`, `start_page`, `end_page` | Phase 1 | Provenance — which pages the extraction came from |
| `is_synthetic` | Phase 1 | Synthetic headings get lower extraction priority |
| `parent_section_id` | AST Step 1 | Lets you walk up for context ("this clause sits inside Article III → Payment Terms") |
| `semantic_label` | AST Step 2 | Tells you *what kind* of section this is — routes to the right extraction template |
| `semantic_confidence` | AST Step 2 | Low confidence = skip extraction or flag for review |
| `label_source` | AST Step 2 | "pattern" vs "gpt-4o-mini" |

### 1.2 Documents Table

| Column | What It Gives Step 3 |
|--------|---------------------|
| `document_type` | Routes to the right extraction template (Contract → obligation extraction, Complaint → allegation extraction) |
| `confidence_score` | If the document-level classification is low, flag the whole document |
| `file_name` | Lookup key |

### 1.3 What You Do NOT Have Yet

- Entity extractions (parties, dates, amounts, obligations, conditions, claims)
- Cross-reference links between entities
- Knowledge graph (nodes + edges)
- Timeline / conflict / evidence-path analytics


---

## 2. New Supabase Tables

Run these manually in the Supabase SQL Editor before writing scripts.

```sql
-- ============================================================
-- Step 3: Extractions table
-- ============================================================
CREATE TABLE extractions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    section_id UUID NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,

    -- What was extracted
    extraction_type TEXT NOT NULL,         -- "party", "date", "amount", "obligation",
                                          -- "condition", "claim", "evidence_ref",
                                          -- "deadline", "cause_of_action"
    entity_name TEXT NOT NULL,            -- human-readable label: "Apple Inc.", "30-day cure period"
    entity_value TEXT,                    -- normalized value: ISO date, numeric amount, structured JSON
    raw_text TEXT,                        -- the exact span from section_text that was extracted

    -- Provenance & confidence
    confidence FLOAT NOT NULL DEFAULT 0.0,
    page_range TEXT,                      -- copied from parent section
    extraction_method TEXT NOT NULL,      -- "gemini-flash", "gpt-4o-mini", "lexnlp+gemini", "lexnlp+gpt", "pattern"

    -- Metadata
    properties JSONB DEFAULT '{}',        -- flexible: role ("plaintiff"/"defendant"),
                                          -- currency, direction ("owes"/"owed"), etc.
    needs_review BOOLEAN DEFAULT FALSE,   -- flagged for HITL
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_extractions_section ON extractions(section_id);
CREATE INDEX idx_extractions_document ON extractions(document_id);
CREATE INDEX idx_extractions_type ON extractions(extraction_type);

-- ============================================================
-- Step 4: Knowledge Graph tables
-- ============================================================
CREATE TABLE kg_nodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID REFERENCES documents(id) ON DELETE CASCADE,
    case_id TEXT,                          -- groups nodes across docs in the same case

    node_type TEXT NOT NULL,              -- "party", "claim", "obligation", "evidence",
                                          -- "date_event", "amount", "condition"
    node_label TEXT NOT NULL,             -- display name: "Apple Inc.", "Breach of Contract"
    properties JSONB DEFAULT '{}',

    -- Provenance chain
    source_extraction_id UUID REFERENCES extractions(id),
    source_section_id UUID REFERENCES sections(id),

    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_kg_nodes_document ON kg_nodes(document_id);
CREATE INDEX idx_kg_nodes_type ON kg_nodes(node_type);
CREATE INDEX idx_kg_nodes_case ON kg_nodes(case_id);

CREATE TABLE kg_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_node_id UUID NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
    target_node_id UUID NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,

    edge_type TEXT NOT NULL,              -- "alleged_by", "obligated_to", "breached_by",
                                          -- "referenced_in", "occurred_on", "condition_for",
                                          -- "damages_claimed_by", "payable_to"
    properties JSONB DEFAULT '{}',
    confidence FLOAT,

    -- Provenance
    source_section_id UUID REFERENCES sections(id),

    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_kg_edges_source ON kg_edges(source_node_id);
CREATE INDEX idx_kg_edges_target ON kg_edges(target_node_id);
CREATE INDEX idx_kg_edges_type ON kg_edges(edge_type);
```


---

## 3. Step 3: Entity Extraction (`03_entity_extraction.py`)

### 3.1 Purpose

Read each section's `semantic_label` and `section_text`, run LexNLP pre-extraction to collect deterministic hints (dates, amounts, conditions), then pass those hints + the section text to the appropriate LLM (Gemini Flash for simple templates, GPT-4o-mini for complex ones) for structured extraction. Store results in the `extractions` table with provenance back to the source section.

### 3.2 Why Semantic Labels Matter Here

The AST's semantic labels are what make extraction targeted instead of brute-force. You don't run the same prompt on every section. Instead:

- `obligation.payment` → extract: amount, currency, schedule, payer, payee, deadline
- `preamble.parties` → extract: party names, roles, addresses, entity types
- `factual_allegations.breach_event` → extract: what happened, who did it, when, what was violated
- `termination.for_cause` → extract: trigger conditions, notice period, cure period
- `causes_of_action.breach_of_contract` → extract: which contract, which clause, what was breached

A section labeled `severability` or `entire_agreement` gets skipped — there's nothing to extract from boilerplate.

### 3.3 Extraction Templates

Each template is a Pydantic model that defines what the LLM returns for a given semantic label (or group of labels). Templates A, B, E, G route to **Gemini Flash**. Templates C, D, F route to **GPT-4o-mini**. See Section 0.4 for the full routing map.

#### Template A: Party Extraction
**Triggers on:** `preamble.parties`, `parties`, `parties.plaintiff`, `parties.defendant`, `caption.parties`

```python
class PartyExtraction(BaseModel):
    parties: list[PartyEntity]

class PartyEntity(BaseModel):
    name: str                    # "Apple Inc."
    role: str                    # "plaintiff", "defendant", "licensor", "service_provider"
    entity_type: str             # "corporation", "individual", "government", "llc"
    jurisdiction: str | None     # "Delaware", "Spain"
    address: str | None
```

#### Template B: Date/Deadline Extraction
**Triggers on:** `preamble.effective_date`, `factual_allegations.timeline`, `obligation.payment.schedule`, `termination.expiration`, any label containing "deadline"

```python
class DateExtraction(BaseModel):
    dates: list[DateEntity]

class DateEntity(BaseModel):
    description: str             # "Contract effective date", "Cure period deadline"
    date_value: str              # ISO format: "2024-03-15" or "within 30 days of notice"
    date_type: str               # "effective", "deadline", "event", "expiration", "filing"
    is_relative: bool            # True if "30 days after X" rather than absolute date
    reference_event: str | None  # What the relative date is relative to
```

#### Template C: Obligation Extraction
**Triggers on:** `obligation.*`, `covenant.*`

```python
class ObligationExtraction(BaseModel):
    obligations: list[ObligationEntity]

class ObligationEntity(BaseModel):
    description: str             # "Licensee shall pay royalties quarterly"
    obligated_party: str         # who must do it
    beneficiary_party: str       # who benefits
    action: str                  # the verb: "pay", "deliver", "report", "refrain"
    deadline: str | None         # when it's due
    condition: str | None        # "if X happens" / "provided that Y"
    amount: str | None           # "$50,000" or "10% of net revenue"
```

#### Template D: Claim/Allegation Extraction
**Triggers on:** `factual_allegations.*`, `causes_of_action.*`

```python
class ClaimExtraction(BaseModel):
    claims: list[ClaimEntity]

class ClaimEntity(BaseModel):
    description: str             # "Defendant breached Section 4.2 by failing to deliver"
    claim_type: str              # "breach_of_contract", "fraud", "negligence"
    plaintiff: str
    defendant: str
    alleged_facts: list[str]     # key factual assertions
    evidence_references: list[str]  # "Exhibit A", "See email dated Jan 12"
    damages_sought: str | None
```

#### Template E: Amount/Financial Extraction
**Triggers on:** `obligation.payment.amount`, `damages.*`, `liability.cap`, `indemnification.scope`

```python
class AmountExtraction(BaseModel):
    amounts: list[AmountEntity]

class AmountEntity(BaseModel):
    description: str             # "Licensing fee", "Damages claimed"
    value: str                   # "500000" or "10% of net revenue"
    currency: str                # "USD", "EUR"
    is_calculated: bool          # True if formula-based rather than fixed
    payer: str | None
    payee: str | None
```

#### Template F: Condition Extraction
**Triggers on:** `condition.*`, `termination.for_cause`

```python
class ConditionExtraction(BaseModel):
    conditions: list[ConditionEntity]

class ConditionEntity(BaseModel):
    description: str             # "If Licensee fails to cure within 30 days"
    condition_type: str          # "precedent", "subsequent", "termination_trigger"
    trigger_event: str           # what must happen
    consequence: str             # what follows
    affected_party: str | None
```

#### Template G: Evidence Reference Extraction
**Triggers on:** `exhibit_reference`, `schedule_reference`, `certificate_of_service`

```python
class EvidenceRefExtraction(BaseModel):
    references: list[EvidenceRef]

class EvidenceRef(BaseModel):
    reference_label: str         # "Exhibit A", "Schedule 2", "Attachment B"
    description: str | None      # "Copy of the original agreement"
    referenced_in_context: str   # the sentence where it's mentioned
```

### 3.4 Label → Template Routing

The script needs a routing map. Not every label triggers extraction — boilerplate sections get skipped.

```python
SKIP_LABELS = {
    "severability", "entire_agreement", "amendment_procedure",
    "assignment", "notices", "signature_block", "verification",
    "jury_demand", "certificate_of_service", "cover_page",
    "contract_root", "complaint_root", "financial_root",
    "annual_report_root", "unrecognized", "error", "pending",
}

LABEL_TO_TEMPLATE = {
    # Party extraction
    "preamble.parties": "party",
    "parties": "party",
    "parties.plaintiff": "party",
    "parties.defendant": "party",
    "caption.parties": "party",

    # Date extraction
    "preamble.effective_date": "date",
    "factual_allegations.timeline": "date",
    "obligation.payment.schedule": "date",
    "termination.expiration": "date",

    # Obligation extraction
    "obligation": "obligation",
    "obligation.performance": "obligation",
    "obligation.payment": "obligation",
    "obligation.payment.amount": "obligation",  # also triggers amount
    "obligation.delivery": "obligation",
    "obligation.reporting": "obligation",
    "obligation.notification": "obligation",
    "covenant.non_compete": "obligation",
    "covenant.non_solicitation": "obligation",
    "covenant.non_disclosure": "obligation",
    "covenant.exclusivity": "obligation",

    # Claim extraction
    "factual_allegations": "claim",
    "factual_allegations.background": "claim",
    "factual_allegations.relationship": "claim",
    "factual_allegations.breach_event": "claim",
    "factual_allegations.damages_description": "claim",
    "causes_of_action": "claim",
    "causes_of_action.breach_of_contract": "claim",
    "causes_of_action.negligence": "claim",
    "causes_of_action.fraud": "claim",
    "causes_of_action.statutory_violation": "claim",
    "causes_of_action.unjust_enrichment": "claim",
    "causes_of_action.declaratory_relief": "claim",

    # Amount extraction
    "damages": "amount",
    "damages.compensatory": "amount",
    "damages.consequential": "amount",
    "damages.punitive": "amount",
    "damages.statutory": "amount",
    "damages.equitable_relief": "amount",
    "liability.cap": "amount",
    "indemnification.scope": "amount",

    # Condition extraction
    "condition": "condition",
    "condition.precedent": "condition",
    "condition.subsequent": "condition",
    "condition.concurrent": "condition",
    "termination.for_cause": "condition",

    # Evidence reference extraction
    "exhibit_reference": "evidence_ref",
    "schedule_reference": "evidence_ref",
}
```

Labels not in `SKIP_LABELS` and not in `LABEL_TO_TEMPLATE` get a **generic extraction** pass — GPT reads the section and extracts whatever entities it finds (parties, dates, amounts) without a specific template. This catches sections like `dispute_resolution.governing_law` (might mention a jurisdiction) or `confidentiality.duration` (might mention a date/period).

### 3.5 Script Process

```
1. Accept CLI: --document_id or --file_name
2. Read document_type from documents table
3. Initialize clients: Supabase, Gemini Flash, OpenAI (GPT-4o-mini)
4. Fetch all sections for the document (with semantic_label, section_text, parent info)
5. For each section:
   a. Check semantic_label against SKIP_LABELS → skip if match
   b. Check semantic_confidence → skip if < 0.5 (label too unreliable to route on)
   c. --- LexNLP PRE-PASS ---
      Run LexNLP extractors on section_text to collect hints:
        - lexnlp_dates.get_dates() → list of date strings
        - lexnlp_amounts.get_amounts() → list of numeric amounts
        - lexnlp_money.get_money() → list of (amount, currency) tuples
        - lexnlp_conditions.get_conditions() → list of conditional phrases
        - lexnlp_definitions.get_definitions() → list of defined terms
        - lexnlp_durations.get_durations() → list of duration phrases
      Wrap in try/except — if LexNLP crashes on weird text, continue without hints.
      Format hints as a string block for the LLM prompt.
   d. Look up template from LABEL_TO_TEMPLATE
   e. Look up model from TEMPLATE_TO_MODEL (gemini-flash or gpt-4o-mini)
   f. If template found → call the routed model with:
        - The specific Pydantic response_format
        - The LexNLP hints included in the user prompt
   g. If no template but label not in SKIP_LABELS → call GPT-4o-mini with generic extraction
      (always GPT for generic — needs stronger reasoning)
   h. If Gemini Flash fails → retry once → fall back to GPT-4o-mini for that section
   i. For each extracted entity → write a row to extractions table
6. Print summary: "SUCCESS: Extracted {N} entities from {M} sections for '{file_name}'.
   {P} parties, {D} dates, {O} obligations, {C} claims, {A} amounts,
   {G} via Gemini, {T} via GPT, {L} LexNLP hints used, {R} flagged for review."
```

### 3.6 LLM Prompt Structure

All prompts (both Gemini Flash and GPT-4o-mini) follow the same structure. The only difference is which model receives the call.

**System prompt (example for obligation extraction — GPT-4o-mini):**

```
You are a legal document extraction engine. You are reading a section
from a "{document_type}" that has been classified as "{semantic_label}".

Extract all obligations found in this section. An obligation is any duty,
requirement, or commitment that a party must fulfill.

Return ONLY a JSON object matching the provided schema. If no obligations
are found, return {"obligations": []}.

Be precise. Extract the exact parties, actions, and deadlines mentioned.
Do not infer obligations that are not stated in the text.
```

**User prompt (note the LexNLP hints block):**

```
Section title: {section_title}
Parent section: {parent_section_title or "Root level"}
Document type: {document_type}

--- Pre-extracted hints (from deterministic parser, may contain errors) ---
Dates found: {lexnlp_dates or "None"}
Amounts found: {lexnlp_amounts or "None"}
Money found: {lexnlp_money or "None"}
Conditions found: {lexnlp_conditions or "None"}
Durations found: {lexnlp_durations or "None"}
Definitions found: {lexnlp_definitions or "None"}
--- End hints ---

Use the hints above as starting points but DO NOT trust them blindly.
Correct any errors, add context (who, what, why), and extract entities
the hints missed. The hints are from a regex-based parser and may
include false positives (e.g., bullet numbers detected as amounts).

Section text:
{section_text[:3000]}
```

The LexNLP hints block is included for ALL templates, not just date/amount. Even an obligation extraction benefits from knowing that LexNLP found "$50,000" and "30 days" in the text — the LLM can reference those values directly instead of searching for them.

**Gemini Flash JSON mode:** Gemini does not support Pydantic `response_format` the same way OpenAI does. Instead, include the JSON schema in the system prompt and set `generation_config={"response_mime_type": "application/json"}`. Parse the response string with `json.loads()` then validate with the Pydantic model. If validation fails, fall back to GPT-4o-mini.

```python
# Gemini Flash call pattern
response = gemini_model.generate_content(
    [system_prompt, user_prompt],
    generation_config=genai.GenerationConfig(
        response_mime_type="application/json",
        temperature=0.1,
    ),
)
raw = json.loads(response.text)
result = PartyExtraction(**raw)  # Pydantic validation
```

```python
# GPT-4o-mini call pattern (same as AST phase)
response = openai_client.beta.chat.completions.parse(
    model="gpt-4o-mini",
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ],
    response_format=ObligationExtraction,
)
result = response.choices[0].message.parsed
```

**Text limit:** 3000 chars per call (up from 1500 in AST labeling) because extraction needs more context. For sections longer than 3000 chars, make multiple calls with overlapping windows (last 200 chars of previous window prepended to next) and deduplicate results.

### 3.7 Confidence and HITL Rules

| Confidence | Action |
|-----------|--------|
| < 0.5 | Do not store. Log as skipped. |
| 0.5–0.7 | Store with `needs_review = TRUE`. |
| 0.7–0.9 | Store normally. Optional review. |
| > 0.9 | Store. Auto-approved. |

The confidence comes from GPT's self-reported confidence in the extraction. Separate from the AST label confidence — a section can have a high-confidence label but low-confidence extraction if the text is ambiguous.

### 3.8 `raw_text` Field

For every extraction, store the exact substring from `section_text` that the entity was pulled from. This is your provenance link at the text level. The frontend can highlight this span in the document viewer. GPT should be instructed to include the relevant quote in its response.

### 3.9 Edge Cases

- **Empty section_text** (<50 chars): Skip extraction entirely. Log it.
- **Synthetic headings** (`is_synthetic = True`): Still extract — the text under a synthetic heading is real, only the heading was invented.
- **Sections with `semantic_label = "unrecognized"`**: Run generic extraction (GPT-4o-mini) but flag all results with `needs_review = TRUE`.
- **Duplicate entities across sections**: Two sections both mention "Apple Inc." — that's fine, store both. Deduplication happens in Step 4 (KG build) when merging into a single node.
- **Rate limiting**: 0.5s delay between calls for both Gemini and GPT. Exponential backoff on 429 (Gemini) or rate_limit errors (GPT).
- **LexNLP crashes on section text**: Wrap all LexNLP calls in try/except. If it throws, continue with empty hints — the LLM can still extract without them.
- **LexNLP false positives**: Common issue: bullet numbers like "2." detected as money amounts. The LLM prompt explicitly warns about this. The LLM is the authority — LexNLP is just a hint provider.
- **Gemini Flash returns invalid JSON**: Parse with `json.loads()` in try/except. If it fails, retry once with a stricter prompt. If still bad, fall back to GPT-4o-mini.
- **Gemini Flash returns entities not matching Pydantic schema**: Validate with the Pydantic model. If `ValidationError`, fall back to GPT-4o-mini.
- **GOOGLE_API_KEY missing**: If missing but OPENAI_API_KEY is present, run everything through GPT-4o-mini. Print a WARNING that Gemini is unavailable. Do not crash.

### 3.10 Estimated Size

~400–500 lines. The bulk is the template definitions, the routing map, the LexNLP pre-pass helper, and the dual-model call wrappers (Gemini + GPT with fallback).


---

## 4. Step 4: Knowledge Graph Build (`04_kg_build.py`)

### 4.1 Purpose

Read all extractions for a document (or a set of documents in the same case), deduplicate entities into `kg_nodes`, and create `kg_edges` representing relationships between them. This is where cross-document reasoning starts.

### 4.2 Node Creation

Each unique entity becomes one `kg_node`. The challenge is deduplication — "Apple Inc.", "Apple", and "Defendant Apple Inc." should all map to the same node.

**Deduplication strategy (two-pass):**

**Pass 1: Exact + normalized match (no AI).** Normalize entity names: strip "Inc.", "LLC", "Corp.", lowercase, collapse whitespace. If two extractions produce the same normalized name and the same `extraction_type`, they merge into one node.

**Pass 2: Fuzzy match with GPT-4o-mini (for remaining ambiguity).** After Pass 1, take all remaining unmerged nodes of the same type and ask GPT-4o-mini: "Are any of these the same entity?" This handles cases like "J. Smith" vs "John Smith" or "the Agreement" vs "Software License Agreement dated March 2024." Always use GPT-4o-mini for this — it needs judgment, not slot-filling.

```python
# Pass 1: Deterministic merge
normalize("Apple Inc.") → "apple"
normalize("Apple")      → "apple"
→ Same node.

# Pass 2: GPT-assisted merge (only when needed)
["John Smith", "J. Smith", "Smith"] → GPT says: merge all three → one node
```

**Node properties (stored in JSONB `properties`):**

```json
{
  "aliases": ["Apple Inc.", "Apple", "Defendant"],
  "entity_type": "corporation",
  "role": "defendant",
  "jurisdiction": "California",
  "first_seen_page": 1,
  "extraction_ids": ["uuid1", "uuid2", "uuid3"]
}
```

### 4.3 Edge Creation

Edges come from two sources:

**Source A: Intra-extraction edges.** Some extraction templates naturally produce relationships. An obligation extraction gives you: `Party A --[obligated_to]--> Party B` with the obligation as a connecting node. A claim extraction gives you: `Plaintiff --[alleged_by]--> Claim --[against]--> Defendant`.

**Source B: Cross-extraction edges (GPT-assisted).** After all nodes exist, run a pass that looks for implicit relationships:
- A party mentioned in a contract section and the same party mentioned in a complaint → `party --[referenced_in]--> document`
- An exhibit reference in a complaint and an actual exhibit document → `exhibit_ref --[resolved_to]--> exhibit_document`
- A date in one section that's a deadline for an obligation in another → `date_event --[deadline_for]--> obligation`

### 4.4 Edge Types

```
# Complaint edges
alleged_by          — plaintiff alleges a claim
against             — claim is against defendant
evidenced_by        — claim references evidence
occurred_on         — event happened on date
damages_claimed_by  — plaintiff claims damages

# Contract edges
obligated_to        — party has obligation to another party
condition_for       — condition must be met before obligation triggers
payable_to          — amount payable to party
governed_by         — contract governed by jurisdiction
terminable_by       — party can terminate under conditions

# Cross-document edges
referenced_in       — entity appears in document/section
contradicts         — obligation in doc A conflicts with allegation in doc B
supports            — evidence supports a claim
resolves_to         — exhibit reference resolves to actual exhibit
```

### 4.5 Script Process

```
1. Accept CLI: --document_id or --file_name or --case_id (new: process all docs in a case)
2. Fetch all extractions for the target scope
3. Pass 1: Deterministic node deduplication (normalize names, merge same-type matches)
4. Pass 2: GPT-assisted fuzzy dedup (batch groups of similar names)
5. Insert kg_nodes into Supabase
6. Create intra-extraction edges (from template relationships)
7. Create cross-extraction edges (GPT-assisted relationship discovery)
8. Insert kg_edges into Supabase
9. Print summary: "SUCCESS: Built KG for '{file_name}'. {N} nodes, {E} edges,
   {D} cross-document links."
```

### 4.6 The `case_id` Concept

Right now, documents are independent — there's no concept of "these 5 documents belong to the same legal case." For the knowledge graph to do cross-document reasoning, you need this grouping.

**Option A (simple, recommended to start):** Add a `case_id TEXT` column to the `documents` table. Set it manually or via a simple CLI tool. The KG script uses it to fetch all docs in a case.

```sql
ALTER TABLE documents ADD COLUMN case_id TEXT;
CREATE INDEX idx_documents_case ON documents(case_id);
```

**Option B (later):** A full `cases` table with metadata (case name, jurisdiction, date opened, etc.) and a many-to-many `case_documents` join table. Build this when the frontend needs it.

### 4.7 Edge Cases

- **Single document, no cross-references:** KG still works — it just has intra-document edges only.
- **Entity appears in 20+ sections:** One node, many `extraction_ids` in properties, many edges.
- **Contradictory extractions:** Party A is "plaintiff" in the complaint but "licensee" in the contract. Both roles stored in `properties.aliases` / `properties.roles` list.
- **No extractions found for document:** Skip KG build, print warning.

### 4.8 Estimated Size

~250–350 lines. The GPT dedup pass is the trickiest part.


---

## 5. Step 5: Graph Analytics (`05_graph_analytics.py`)

### 5.1 Purpose

Run analytical queries over the knowledge graph to produce high-value outputs for lawyers. This script does not modify the KG — it reads nodes and edges and produces reports.

### 5.2 Analytics to Implement

#### A. Timeline Construction

Walk all `date_event` nodes, sort by `entity_value` (ISO date), and produce an ordered event timeline.

**Output:** A JSON array and a markdown summary.

```json
[
  {
    "date": "2020-08-13",
    "event": "Epic Games updated Fortnite to include direct payment option",
    "source_document": "Complaint (Epic v Apple)",
    "source_page": "12",
    "related_parties": ["Epic Games", "Apple"],
    "related_claims": ["Breach of Developer Agreement"]
  }
]
```

**Where it's stored:** Write to `zz_temp_chunks/{case_id}_timeline.json` and `{case_id}_timeline.md`. Optionally upsert into a new `analytics_outputs` table if you want persistence (define later).

#### B. Claim → Evidence Path

For each claim node, walk edges to find what evidence supports it. Produces a "proof chain."

```
Claim: "Apple breached Section 3.3.1 of the DPLA"
  ↓ evidenced_by
Evidence: "Exhibit A — Developer Program License Agreement"
  ↓ referenced_in
Section: Contract - DPLA, Section 3.3.1, pages 8-10
  ↓ contains
Obligation: "Developer shall not distribute apps outside the App Store"
```

This directly feeds the "Evidence-Timeline Matrix" UI concept from the competitive analysis doc.

#### C. Obligation Conflict Detection

Compare obligations from contracts against allegations from complaints. If a complaint says "Defendant failed to pay by March 15" and the contract says "Payment due within 30 days of invoice", flag the potential conflict/connection.

**Logic:**
1. Get all `obligation` nodes from contract documents in the case.
2. Get all `claim` / `breach_event` nodes from complaint documents.
3. For each claim, check if it references an obligation (by party name + action keyword overlap).
4. If match found → create a `contradicts` or `supports` edge and include in report.

#### D. Party Relationship Map

Produce a summary of all parties and their relationships.

```
Apple Inc. (Defendant)
  ├── obligated_to: Epic Games (pay developer proceeds)
  ├── alleged_by: Epic Games (breach of DPLA Section 3.3.1)
  └── governed_by: California (Northern District)

Epic Games (Plaintiff)
  ├── obligated_to: Apple (comply with App Store guidelines)
  ├── claims_against: Apple (antitrust violation, breach of contract)
  └── seeks: Injunctive relief, damages
```

### 5.3 Script Process

```
1. Accept CLI: --case_id or --document_id or --file_name
2. Load all kg_nodes and kg_edges for the scope
3. Build an in-memory graph (NetworkX or plain dict-of-lists)
4. Run each analytic:
   a. Timeline construction → write JSON + MD
   b. Claim-evidence paths → write JSON + MD
   c. Obligation conflict scan → write JSON + MD
   d. Party relationship map → write JSON + MD
5. Write combined report to zz_temp_chunks/{case_id}_analytics_report.md
6. Print summary: "SUCCESS: Analytics complete for case '{case_id}'.
   {T} timeline events, {P} claim-evidence paths, {C} conflicts detected."
```

### 5.4 NetworkX vs. Plain Python

Use NetworkX only if you need shortest-path or graph traversal algorithms. For the initial analytics above, a plain `dict[str, list[dict]]` adjacency list is enough and avoids a new dependency. If you add more complex graph queries later (shortest path, connected components, centrality), add NetworkX then.

### 5.5 Estimated Size

~200–300 lines. Most of it is formatting the output reports.


---

## 6. Updated Orchestrator

### 6.1 `backend/02_MIDDLE/main.py` Updates

Add Steps 3, 4, and 5 to the subprocess chain:

```python
# Step 1: Tree reconstruction
_run("01_AST_tree_build.py", "--document_id", document_id)

# Step 2: Semantic labeling
_run("02_AST_semantic_label.py", "--document_id", document_id)

# Step 3: Entity extraction
_run("03_entity_extraction.py", "--document_id", document_id)

# Step 4: Knowledge graph build
_run("04_kg_build.py", "--document_id", document_id)

# Step 5: Graph analytics
_run("05_graph_analytics.py", "--document_id", document_id)
```

For cross-document analytics (Step 5 with `--case_id`), add a separate orchestrator mode:

```bash
python backend/02_MIDDLE/main.py --file_name "my_contract"       # single doc: steps 1-4 + single-doc analytics
python backend/02_MIDDLE/main.py --case_id "epic_v_apple"        # case-wide: steps 4-5 re-run across all docs in case
```

### 6.2 `backend/main.py` (Root Orchestrator)

No changes needed — it already calls Phase 2's main.py with `--file_name`.


---

## 7. Build Order and Dependencies

Build these scripts one at a time. Each depends on the previous.

```
03_entity_extraction.py
  ├── Reads: sections table (needs semantic_label from script 02)
  ├── Uses: LexNLP (pre-extraction hints), Gemini Flash (simple templates), GPT-4o-mini (complex templates + fallback)
  ├── Writes: extractions table
  └── Test: Run on a contract doc, verify extractions make sense. Check extraction_method column to confirm model routing is working.

04_kg_build.py
  ├── Reads: extractions table (needs data from script 03)
  ├── Uses: GPT-4o-mini (fuzzy entity dedup only)
  ├── Writes: kg_nodes + kg_edges tables
  └── Test: Run on same contract doc, verify nodes are deduped and edges exist

05_graph_analytics.py
  ├── Reads: kg_nodes + kg_edges (needs data from script 04)
  ├── Uses: No LLM calls — pure Python analytics
  ├── Writes: zz_temp_chunks/ report files
  └── Test: Run on a complaint + contract pair, verify timeline and claim-evidence paths
```

### 7.1 Testing Strategy

Start with two test documents already in Supabase from Phase 1:
1. A **contract** (tests obligation, party, amount, condition extraction)
2. A **complaint** (tests claim, allegation, evidence reference extraction)

Run scripts 03 → 04 → 05 on each individually, then set both to the same `case_id` and re-run 04 + 05 to test cross-document linking.

**Validation queries to run in Supabase:**

```sql
-- Check extraction counts by type
SELECT extraction_type, COUNT(*), AVG(confidence)
FROM extractions
WHERE document_id = '{uuid}'
GROUP BY extraction_type;

-- Check KG node counts
SELECT node_type, COUNT(*)
FROM kg_nodes
WHERE document_id = '{uuid}'
GROUP BY node_type;

-- Check edges exist
SELECT e.edge_type, COUNT(*)
FROM kg_edges e
JOIN kg_nodes src ON e.source_node_id = src.id
WHERE src.document_id = '{uuid}'
GROUP BY e.edge_type;

-- Find orphan nodes (no edges)
SELECT n.id, n.node_label, n.node_type
FROM kg_nodes n
LEFT JOIN kg_edges e1 ON n.id = e1.source_node_id
LEFT JOIN kg_edges e2 ON n.id = e2.target_node_id
WHERE e1.id IS NULL AND e2.id IS NULL;
```


---

## 8. Code Conventions (Same as AST Phase)

All conventions from the AST planning doc still apply:

- Scripts numbered `03_`, `04_`, `05_`
- CLI via `argparse`, accept `--document_id` or `--file_name`
- Print `SUCCESS:` or `ERROR:` as final line
- `.env` loaded from project root (two levels up)
- Supabase client via `create_client(url, key)`
- LLM calls: Pydantic validation, retry with backoff, 0.5s delay between calls
- All intermediate files go to `backend/zz_temp_chunks/`

### 8.1 New Convention: `--case_id`

Scripts 04 and 05 should also accept `--case_id` to operate across multiple documents. When `--case_id` is provided, the script fetches all `document_id`s where `documents.case_id` matches, then processes all of them together.

### 8.2 New Dependencies

```
pip install lexnlp --break-system-packages       # Legal NLP pre-extraction (AGPLv3)
pip install google-generativeai --break-system-packages  # Gemini Flash client
```

Existing deps still needed: `openai`, `supabase-py`, `pydantic`, `python-dotenv`. NetworkX is optional and only needed if you implement shortest-path later.

### 8.3 Client Initialization Pattern

```python
# Initialize all three clients at script start
# Supabase — required
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

# Gemini Flash — optional (falls back to GPT if missing)
gemini_model = None
google_key = os.environ.get("GOOGLE_API_KEY")
if google_key:
    import google.generativeai as genai
    genai.configure(api_key=google_key)
    gemini_model = genai.GenerativeModel("gemini-2.0-flash")
else:
    print("  WARNING: GOOGLE_API_KEY not set — all extractions will use GPT-4o-mini")

# OpenAI — required (used for complex templates + fallback)
openai_key = os.environ.get("OPENAI_API_KEY")
if not openai_key:
    print("ERROR: OPENAI_API_KEY not set in .env")
    sys.exit(1)
from openai import OpenAI
openai_client = OpenAI()

# LexNLP — optional (gracefully degrade if import fails)
try:
    import lexnlp.extract.en.dates as lexnlp_dates
    import lexnlp.extract.en.amounts as lexnlp_amounts
    import lexnlp.extract.en.money as lexnlp_money
    import lexnlp.extract.en.conditions as lexnlp_conditions
    import lexnlp.extract.en.definitions as lexnlp_definitions
    import lexnlp.extract.en.durations as lexnlp_durations
    LEXNLP_AVAILABLE = True
except ImportError:
    print("  WARNING: lexnlp not installed — running without pre-extraction hints")
    LEXNLP_AVAILABLE = False
```


---

## 9. What This Enables (But Don't Build Yet)

Once Phase 2 is complete, the data is ready for:

**Phase 3 — Vector Search / RAG:**
- Embed each section's text using **Legal-BERT** (`nlpaueb/legal-bert-base-uncased` from HuggingFace) — a BERT model pre-trained on 12GB of legal text (legislation, court cases, contracts). It produces better similarity scores for legal text than general-purpose embedders because it understands that "indemnification" and "hold harmless" are semantically close. There is also a contracts-specific variant (`nlpaueb/bert-base-uncased-contracts`) worth testing.
- Each vector carries AST metadata: `semantic_label`, `document_type`, `level`
- Semantic search scoped by structure: "find obligation clauses similar to X"
- The extractions table gives you structured filters: search by party, date range, claim type
- **Do NOT use Legal-BERT in Phase 2.** It is an embedding model, not a generative one. It produces vectors, not extractions.

**Phase 4 — Agentic Workflows:**
- Master agent routes queries to sub-agents
- Contract Agent uses AST + KG to answer: "What are the payment obligations in this NDA?"
- Complaint Agent walks claim→evidence paths to answer: "What evidence supports Count III?"
- Each agent response carries provenance back to source sections and pages
- **This is where Claude API comes in** — long-context reasoning over KG + documents. Do NOT use Claude in Phase 2.

**Phase 5 — Frontend:**
- The timeline JSON from Step 5 feeds directly into the "Evidence-Timeline Matrix" UI
- The party relationship map feeds a "Cast of Characters" view
- The claim→evidence paths feed a "Proof Chain" view
- Confidence scores and `needs_review` flags power the HITL review workflow
- `raw_text` in extractions enables text-level highlighting in the document viewer


---

## 10. STOP HERE

After building and testing `03_entity_extraction.py`, `04_kg_build.py`, and `05_graph_analytics.py`, Phase 2 is done.

Do NOT proceed to:
- Vector storage / embedding (Phase 3)
- Agent construction (Phase 4)
- Frontend (Phase 5)

Those get their own planning documents once the KG is validated against real case documents.