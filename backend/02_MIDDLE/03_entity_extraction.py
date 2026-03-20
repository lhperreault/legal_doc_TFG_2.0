"""
03_entity_extraction.py — Phase 2, Step 3: Entity Extraction

Extracts parties, dates, amounts, obligations, conditions, claims, and evidence
references from each AST node. Uses LexNLP as a free pre-pass for deterministic
hints, then routes to Gemini Flash (simple templates) or GPT-4o-mini (complex
templates / fallback). Results are stored in the `extractions` table.

Usage:
    python 03_entity_extraction.py --file_name "Complaint (Epic Games to Apple"
    python 03_entity_extraction.py --document_id "abc-123-uuid"
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError
from supabase import create_client

# Load .env from project root (two levels up from backend/02_MIDDLE/)
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))


# ---------------------------------------------------------------------------
# Pydantic extraction models
# ---------------------------------------------------------------------------

class PartyEntity(BaseModel):
    name: str
    role: str                        # plaintiff, defendant, licensor, service_provider, etc.
    entity_type: str                 # corporation, individual, government, llc
    jurisdiction: str | None = None
    address: str | None = None
    raw_text: str | None = None
    confidence: float = 0.8

class PartyExtraction(BaseModel):
    parties: list[PartyEntity]

class CaseCitationEntity(BaseModel):
    case_name: str              # "Mohawk Industries, Inc. v. Carpenter"
    citation: str               # "558 U.S. 100, 106 (2009)"
    court: str | None           # "U.S. Supreme Court", "9th Cir.", "Fed. Cir."
    year: str | None            # "2009"
    relevance: str | None       # brief description of why it's cited

class CaseCitationExtraction(BaseModel):
    citations: list[CaseCitationEntity]

class DateEntity(BaseModel):
    description: str                 # "Contract effective date", "Cure period deadline"
    date_value: str                  # ISO date or relative: "within 30 days of notice"
    date_type: str                   # effective, deadline, event, expiration, filing
    is_relative: bool = False
    reference_event: str | None = None
    raw_text: str | None = None
    confidence: float = 0.8

class DateExtraction(BaseModel):
    dates: list[DateEntity]


class ObligationEntity(BaseModel):
    description: str
    obligated_party: str
    beneficiary_party: str
    action: str                      # pay, deliver, report, refrain, etc.
    deadline: str | None = None
    condition: str | None = None
    amount: str | None = None
    raw_text: str | None = None
    confidence: float = 0.8

class ObligationExtraction(BaseModel):
    obligations: list[ObligationEntity]


class ClaimEntity(BaseModel):
    description: str
    claim_type: str                  # breach_of_contract, fraud, negligence, etc.
    plaintiff: str
    defendant: str
    alleged_facts: list[str] = []
    evidence_references: list[str] = []
    damages_sought: str | None = None
    raw_text: str | None = None
    confidence: float = 0.8

class ClaimExtraction(BaseModel):
    claims: list[ClaimEntity]


class AmountEntity(BaseModel):
    description: str
    value: str                       # "500000" or "10% of net revenue"
    currency: str = "USD"
    is_calculated: bool = False
    payer: str | None = None
    payee: str | None = None
    raw_text: str | None = None
    confidence: float = 0.8

class AmountExtraction(BaseModel):
    amounts: list[AmountEntity]


class ConditionEntity(BaseModel):
    description: str
    condition_type: str              # precedent, subsequent, termination_trigger
    trigger_event: str
    consequence: str
    affected_party: str | None = None
    raw_text: str | None = None
    confidence: float = 0.8

class ConditionExtraction(BaseModel):
    conditions: list[ConditionEntity]


class EvidenceRef(BaseModel):
    reference_label: str             # "Exhibit A", "Schedule 2"
    description: str | None = None
    referenced_in_context: str
    raw_text: str | None = None
    confidence: float = 0.8

class EvidenceRefExtraction(BaseModel):
    references: list[EvidenceRef]


class GenericEntity(BaseModel):
    entity_type: str                 # party, date, amount, obligation, claim, etc.
    entity_name: str
    entity_value: str | None = None
    description: str
    raw_text: str | None = None
    confidence: float = 0.6

class GenericExtraction(BaseModel):
    entities: list[GenericEntity]


# ---------------------------------------------------------------------------
# Allowed extraction types (closed set — reject anything outside this)
# ---------------------------------------------------------------------------

ALLOWED_EXTRACTION_TYPES = {
    "party", "date", "amount", "obligation", "condition",
    "claim", "cause_of_action", "evidence_ref", "case_citation",
    "statute", "court", "judge", "attorney", "law_firm",
    "legal_concept", "entity",
}

# When the LLM returns a type not in the allowed set, try to map it.
# If no mapping found, drop the extraction entirely.
EXTRACTION_TYPE_ALIASES: dict[str, str] = {
    # Date variants
    "date_filed":     "date",
    "filed_date":     "date",
    "filing_date":    "date",
    "deadline":       "date",
    "effective_date": "date",
    # Party variants
    "person":         "party",
    "appellant":      "party",
    "appellee":       "party",
    "plaintiff":      "party",
    "defendant":      "party",
    "intervenor":     "party",
    "witness":        "party",
    "counsel":        "attorney",
    # Case / citation variants
    "case":           "case_citation",
    "case_number":    "case_citation",
    "citation":       "case_citation",
    # Document / evidence variants
    "document":       "evidence_ref",
    "document_number":"evidence_ref",
    "exhibit":        "evidence_ref",
    "docket":         "evidence_ref",
    # Financial
    "money":          "amount",
    "damages":        "amount",
    "financial":      "amount",
    # Legal
    "legal_principle": "legal_concept",
    "outcome":        "legal_concept",
    # Junk — map to None to signal "drop this extraction"
    "page":           None,
    "page_number":    None,
    "phone":          None,
    "phone_number":   None,
    "fax":            None,
    "facsimile":      None,
    "address":        None,
    "email":          None,
}


def normalize_extraction_type(raw_type: str) -> str | None:
    """
    Normalize an extraction type to the allowed set.
    Returns the normalized type, or None if the extraction should be dropped.
    """
    t = raw_type.strip().lower().replace(" ", "_").replace("-", "_")
    if t in ALLOWED_EXTRACTION_TYPES:
        return t
    if t in EXTRACTION_TYPE_ALIASES:
        return EXTRACTION_TYPE_ALIASES[t]  # may be None (= drop)
    return None  # unknown type — drop


# ---------------------------------------------------------------------------
# Routing maps
# ---------------------------------------------------------------------------

SKIP_LABELS = {
    "severability", "entire_agreement", "amendment_procedure",
    "assignment", "general_provisions.assignment",
    "notices", "general_provisions.notices",
    "signature_block", "verification",
    "jury_demand", "certificate_of_service", "cover_page",
    "contract_root", "complaint_root", "financial_root",
    "annual_report_root", "error", "pending", "motion_root", "table_of_contents", "table_of_authorities",
    "argument",              # parent wrapper only — children get extracted
    "compliance_statement", "consent_statement", "certificate_of_service",
    "signature_block",  "table_of_contents", "title_page",
    "contract_root",
    "general_provisions",           # parent label only — children get extracted
    "general_provisions.severability",
    "general_provisions.entire_agreement",
    "general_provisions.waiver",
    "general_provisions.counterparts",
    "general_provisions.amendment",
    "signature_block",
    "complaint_root", "table_of_contents",
    "causes_of_action",     # parent wrapper only — children get extracted
    "damages",              # parent wrapper only — children get extracted
    "certificate_of_service", "signature_block",
    "verification",
    # Discovery
    "discovery_root", "instructions", "preliminary_statement",
    "deposition.cover", "deposition.appearances", "deposition.certification",
    "deposition.stipulations", "deposition.examination",  # parent wrapper
    "discovery_schedule",

    # Court Orders
    "order_root", "analysis",  # parent wrapper only
    "concurrence", "dissent",  # interesting but not structured for extraction
    # NOTE: "unrecognized" is intentionally NOT here — those sections get
    # generic extraction (GPT-4o-mini) with needs_review=True forced on all results.
}

# Labels that are in SKIP_LABELS as parent wrappers but should be extracted
# when they are leaf nodes (no children) — e.g. an exhibit whose refiner
# didn't split the argument section further.
LEAF_WRAPPER_LABELS = {"argument", "causes_of_action", "damages", "analysis"}

LABEL_TO_TEMPLATE: dict[str, str] = {
    # Complaint / Answer additions
    "nature_of_action":                     "claim",
    "parties.third_party":                  "party",
    "parties.related_entity":               "party",
    "factual_allegations.key_events":       "claim",
    "factual_allegations.damages_narrative": "claim",
    "factual_allegations.concealment":      "claim",
    "causes_of_action.breach_of_fiduciary": "claim",
    "causes_of_action.tortious_interference":"claim",
    "causes_of_action.conversion":          "claim",
    "causes_of_action.trade_secret":        "claim",
    "causes_of_action.ip_infringement":     "claim",
    "causes_of_action.antitrust":           "claim",
    "causes_of_action.unfair_competition":  "claim",
    "causes_of_action.consumer_protection": "claim",
    "causes_of_action.other":              "claim",
    "damages.injunctive_relief":            "amount",
    "damages.attorneys_fees":               "amount",
    "conditions_precedent":                 "condition",
    "admissions_denials":                   "claim",
    "affirmative_defense":                  "claim",
    "counterclaim":                         "claim",
    "crossclaim":                           "claim",
        # Party
    "preamble.parties":               "party",
    "parties":                        "party",
    "parties.plaintiff":              "party",
    "parties.defendant":              "party",
    "caption.parties":                "party",
    "caption":                        "party",
    "caption.court":                  "party",
    "caption.case_number":            "party",
    # Date
    "preamble.effective_date":        "date",
    "factual_allegations.timeline":   "date",
    "obligation.payment.schedule":    "date",
    "termination.expiration":         "date",
    # Obligation
    "obligation":                     "obligation",
    "obligation.performance":         "obligation",
    "obligation.payment":             "obligation",
    "obligation.delivery":            "obligation",
    "obligation.reporting":           "obligation",
    "obligation.notification":        "obligation",
    "covenant.non_compete":           "obligation",
    "covenant.non_solicitation":      "obligation",
    "covenant.non_disclosure":        "obligation",
    "covenant.exclusivity":           "obligation",
    # Claim
    "factual_allegations":                        "claim",
    "factual_allegations.background":             "claim",
    "factual_allegations.relationship":           "claim",
    "factual_allegations.breach_event":           "claim",
    "factual_allegations.damages_description":    "claim",
    "causes_of_action":                           "claim",
    "causes_of_action.breach_of_contract":        "claim",
    "causes_of_action.negligence":                "claim",
    "causes_of_action.fraud":                     "claim",
    "causes_of_action.statutory_violation":        "claim",
    "causes_of_action.unjust_enrichment":         "claim",
    "causes_of_action.declaratory_relief":        "claim",
    # Amount
    "damages":                        "amount",
    "damages.compensatory":           "amount",
    "damages.consequential":          "amount",
    "damages.punitive":               "amount",
    "damages.statutory":              "amount",
    "damages.equitable_relief":       "amount",
    "liability.cap":                  "amount",
    "indemnification.scope":          "amount",
    "obligation.payment.amount":      "amount",
    # Condition
    "condition":                      "condition",
    "condition.precedent":            "condition",
    "condition.subsequent":           "condition",
    "condition.concurrent":           "condition",
    "termination.for_cause":          "condition",
    # Evidence reference
    "exhibit_reference":              "evidence_ref",
    "schedule_reference":             "evidence_ref",
    # Motion/Brief labels → extraction templates
    "statement_of_facts":               "claim",
    "statement_of_facts.background":    "claim",
    "statement_of_facts.key_events":    "claim",
    "statement_of_facts.relationship":  "claim",
    "procedural_history":               "date",
    "argument.main":                    "claim",
    "argument.sub":                     "claim",
    "argument.likelihood_of_success":   "claim",
    "argument.irreparable_harm":        "claim",
    "argument.balance_of_equities":     "claim",
    "argument.public_interest":         "claim",
    "argument.legal_error":             "claim",
    "argument.factual_error":           "claim",
    "argument.abuse_of_discretion":     "claim",
    "argument.statutory_interpretation":"claim",
    "argument.policy":                  "claim",
    "index_of_exhibits":                "evidence_ref",
    # Discovery labels → extraction templates
    "interrogatory":                    "claim",      # questions are about facts/claims
    "interrogatory.answer":             "claim",
    "request_for_production":           "evidence_ref",
    "request_for_production.response":  "evidence_ref",
    "request_for_admission":            "claim",
    "request_for_admission.response":   "claim",
    "subpoena.command":                 "obligation",
    "subpoena.schedule":                "evidence_ref",
    "deposition.direct":                "claim",
    "deposition.cross":                 "claim",
    "privilege_log":                    "evidence_ref",

    # Court Order labels → extraction templates
    "procedural_posture":               "date",       # what motions are pending, when filed
    "factual_background":               "claim",
    "procedural_history":               "date",
    "analysis":                         "claim",
    "analysis.issue":                   "claim",
    "analysis.sub_issue":               "claim",
    "analysis.jurisdiction":            "claim",
    "analysis.standing":                "claim",
    "analysis.merits":                  "claim",
    "analysis.damages":                 "amount",
    "analysis.injunction":              "claim",
    "analysis.privilege":               "claim",
    "analysis.discovery":               "claim",
    "analysis.sanctions":               "amount",
    "analysis.summary_judgment":        "claim",
    "analysis.dismissal":               "claim",
    "holding":                          "claim",
    "finding_of_fact":                  "claim",
    "order":                            "obligation",
    "order.granted":                    "obligation",
    "order.denied":                     "obligation",
    "order.granted_in_part":            "obligation",
    "order.scheduling":                 "date",
    "order.sanctions":                  "amount",
    "judgment":                         "obligation",
    

    # --- Labels that were falling to generic (audit fix) ---
    "prayer_for_relief":                "claim",
    "conclusion":                       "claim",
    "introduction":                     "claim",
    "nature_of_action":                 "claim",
    "statement_of_issues":              "claim",
    "standing":                         "claim",
    "legal_standard":                   "claim",
    "legal_standard.review":            "claim",
    "legal_standard.governing_rule":    "claim",
    "exhibit_content":                  "evidence_ref",
    "schedule_content":                 "evidence_ref",
}

# Secondary templates — run these IN ADDITION to the primary template.
# Handles sections with mixed content (e.g., argument sections that cite cases).
LABEL_TO_SECONDARY_TEMPLATES: dict[str, list[str]] = {
    # Argument sections — cite cases and reference evidence heavily
    "argument.main":                        ["evidence_ref", "case_citation"],
    "argument.sub":                         ["evidence_ref", "case_citation"],
    "argument.likelihood_of_success":       ["evidence_ref", "case_citation"],
    "argument.irreparable_harm":            ["evidence_ref", "case_citation"],
    "argument.balance_of_equities":         ["evidence_ref", "case_citation"],
    "argument.public_interest":             ["evidence_ref", "case_citation"],
    "argument.legal_error":                 ["evidence_ref", "case_citation"],
    "argument.factual_error":               ["evidence_ref", "case_citation"],
    "argument.abuse_of_discretion":         ["evidence_ref", "case_citation"],
    "argument.statutory_interpretation":    ["evidence_ref", "case_citation"],
    "argument.policy":                      ["evidence_ref", "case_citation"],

    # Jurisdiction sections — cite statutes and cases
    "jurisdiction":                         ["evidence_ref", "case_citation"],
    "jurisdiction.subject_matter":          ["evidence_ref", "case_citation"],
    "jurisdiction.appellate":               ["evidence_ref", "case_citation"],

    # Legal standard sections — cite cases heavily
    "legal_standard":                       ["evidence_ref", "case_citation"],
    "legal_standard.review":                ["evidence_ref", "case_citation"],
    "legal_standard.governing_rule":        ["evidence_ref", "case_citation"],

    # Analysis sections — cite cases and may mention amounts
    "analysis.issue":                       ["evidence_ref", "case_citation"],
    "analysis.sub_issue":                   ["evidence_ref", "case_citation"],
    "analysis.merits":                      ["evidence_ref", "case_citation"],
    "analysis.damages":                     ["evidence_ref", "case_citation", "amount"],
    "analysis.sanctions":                   ["evidence_ref", "case_citation", "amount"],

    # Introductions and nature-of-action name parties
    "introduction":                         ["party"],
    "nature_of_action":                     ["party"],

    # Statement of facts mentions parties and dates
    "statement_of_facts":                   ["party", "date"],
    "statement_of_facts.background":        ["party", "date"],
    "statement_of_facts.key_events":        ["party", "date"],

    # Factual allegations mention parties and dates
    "factual_allegations":                  ["party", "date"],
    "factual_allegations.background":       ["party", "date"],
    "factual_allegations.key_events":       ["party", "date"],
    "factual_allegations.breach_event":     ["party", "date"],
    "factual_allegations.timeline":         ["party"],
}


# Gemini handles slot-filling; GPT handles legal reasoning
TEMPLATE_TO_MODEL: dict[str, str] = {
    "party":        "gemini-flash",
    "date":         "gemini-flash",
    "amount":       "gemini-flash",
    "evidence_ref": "gemini-flash",
    "obligation":   "gpt-4o-mini",
    "claim":        "gpt-4o-mini",
    "condition":    "gpt-4o-mini",
    "generic":      "gpt-4o-mini",
    "case_citation": "gemini-flash",    # slot-filling, pattern recognition
}

# Maps template name -> (Pydantic class, list field name)
TEMPLATE_TO_CLASS: dict[str, tuple[type, str]] = {
    "party":        (PartyExtraction,        "parties"),
    "date":         (DateExtraction,         "dates"),
    "amount":       (AmountExtraction,       "amounts"),
    "evidence_ref": (EvidenceRefExtraction,  "references"),
    "obligation":   (ObligationExtraction,   "obligations"),
    "claim":        (ClaimExtraction,        "claims"),
    "condition":    (ConditionExtraction,    "conditions"),
    "generic":      (GenericExtraction,      "entities"),
    "case_citation": (CaseCitationExtraction, "citations"),
}

TEMPLATE_DESCRIPTIONS: dict[str, str] = {
    "party":        "Extract all parties (individuals, companies, organizations). Include names, roles, entity types, and jurisdictions.",
    "date":         "Extract all dates and deadlines — both absolute (ISO format) and relative (e.g. '30 days after notice').",
    "obligation":   "Extract all obligations and duties. Identify who must do what, for whom, by when. An obligation is any duty a party must fulfill.",
    "claim": (
        "Extract all claims, allegations, causes of action, legal arguments, and legal conclusions. "
        "A claim is any assertion made by a party — this includes formal causes of action, factual allegations, "
        "legal arguments about how the law should be applied, and conclusions about the likely outcome. "
        "Also extract the key legal proposition of the section even if it is stated as an argument rather than a formal claim. "
        "If the section makes ANY substantive legal point, extract it. Only return empty if the section is truly procedural boilerplate."
    ),
    "amount":       "Extract all monetary amounts and financial figures. Include the amount, currency, who pays, and who receives.",
    "case_citation": "Extract all case law citations. Include the full case name, reporter citation, court, year, and a brief note on why it is cited.",
    "condition":    "Extract all conditions and conditional clauses. Identify type (precedent/subsequent), what triggers it, and what follows.",
    "evidence_ref": "Extract all references to exhibits, schedules, attachments, docket numbers, or other documents cited in this section.",
    "generic":      (
        "Extract all legally significant entities from this section.\n"
        "You MUST classify each entity using ONLY one of these types: "
        "party, date, amount, obligation, condition, claim, cause_of_action, "
        "evidence_ref, case_citation, statute, court, judge, attorney, law_firm, "
        "legal_concept, entity.\n"
        "Do not invent new types. Do not use: page, phone, address, email, fax."
    ),
}


# ---------------------------------------------------------------------------
# LexNLP pre-pass (optional, template-scoped)
# ---------------------------------------------------------------------------

LEXNLP_AVAILABLE = False
try:
    import lexnlp.extract.en.dates      as _lex_dates
    import lexnlp.extract.en.amounts    as _lex_amounts
    import lexnlp.extract.en.money      as _lex_money
    import lexnlp.extract.en.conditions as _lex_conditions
    import lexnlp.extract.en.definitions as _lex_definitions
    import lexnlp.extract.en.durations  as _lex_durations
    LEXNLP_AVAILABLE = True
except Exception:
    pass

# Which LexNLP extractors are relevant for which template.
# Only run (and include in prompt) the extractors that matter for the task.
_TEMPLATE_LEXNLP_SCOPE: dict[str, set[str]] = {
    "party":        {"definitions"},                          # defined terms sometimes name parties
    "date":         {"dates", "durations"},                   # core value of LexNLP for this template
    "amount":       {"amounts", "money"},                     # core value, but warn about false positives
    "evidence_ref": set(),                                    # LexNLP doesn't help here — skip entirely
    "obligation":   {"dates", "durations", "conditions"},     # deadlines and conditionals inform obligations
    "claim":        {"dates", "conditions"},                  # timeline and conditional facts help
    "condition":    {"conditions", "durations"},               # core value
    "generic":      {"dates", "amounts", "money", "conditions", "definitions", "durations"},
}


def _lexnlp_hints(text: str, template: str = "generic") -> str:
    """
    Run LexNLP extractors scoped to the current template and return
    a formatted hint block. Returns empty string if LexNLP is unavailable
    or if the template doesn't benefit from any extractors.
    """
    if not LEXNLP_AVAILABLE or not text.strip():
        return ""

    scope = _TEMPLATE_LEXNLP_SCOPE.get(template, set())
    if not scope:
        return ""

    lines: list[str] = []

    try:
        if "dates" in scope:
            raw_dates = list(_lex_dates.get_dates(text))[:10]
            # get_dates() returns datetime objects — format as ISO strings
            formatted = []
            for d in raw_dates:
                try:
                    formatted.append(d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d))
                except Exception:
                    formatted.append(str(d))
            if formatted:
                lines.append(f"Dates found: {formatted}")

        if "amounts" in scope:
            raw_amounts = list(_lex_amounts.get_amounts(text))[:10]
            # Filter obvious false positives: bullet numbers (small integers
            # without decimals that appear at line starts) and page numbers.
            filtered = [a for a in raw_amounts if not (isinstance(a, (int, float)) and a < 10 and a == int(a))]
            if filtered:
                lines.append(f"Amounts found: {filtered}")

        if "money" in scope:
            raw_money = list(_lex_money.get_money(text))[:10]
            formatted_money = [f"{a} {c}" for a, c in raw_money]
            if formatted_money:
                lines.append(f"Money found: {formatted_money}")

        if "conditions" in scope:
            raw_conds = list(_lex_conditions.get_conditions(text))[:5]
            if raw_conds:
                # Truncate long condition strings to keep prompt concise
                truncated = [c[:200] if len(c) > 200 else c for c in raw_conds]
                lines.append(f"Conditional clauses: {truncated}")

        if "definitions" in scope:
            raw_defs = list(_lex_definitions.get_definitions(text))[:5]
            if raw_defs:
                lines.append(f"Defined terms: {raw_defs}")

        if "durations" in scope:
            raw_durs = list(_lex_durations.get_durations(text))[:5]
            if raw_durs:
                formatted_durs = [str(d) for d in raw_durs]
                lines.append(f"Durations found: {formatted_durs}")

    except Exception:
        # LexNLP crashed on this text — continue without hints
        return ""

    if not lines:
        return ""

    return (
        "--- Pre-extracted hints (regex-based, may contain errors) ---\n"
        + "\n".join(lines)
        + "\n--- End hints. Validate these against the actual text. ---"
    )
# ---------------------------------------------------------------------------
# Client initialization
# ---------------------------------------------------------------------------

_GEMINI_MODEL = "gemini-2.5-flash"


def _init_clients():
    """Return (supabase, gemini_client, openai_client)."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)
    supabase = create_client(url, key)

    # Gemini Flash — optional
    gemini_client = None
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key:
        try:
            from google import genai as _genai
            gemini_client = _genai.Client(api_key=gemini_key)
        except ImportError:
            print("  WARNING: google-genai not installed — all extractions via GPT-4o-mini")
    else:
        print("  WARNING: GEMINI_API_KEY not set — all extractions via GPT-4o-mini")

    # OpenAI — required
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        print("ERROR: OPENAI_API_KEY not set in .env")
        sys.exit(1)
    from openai import OpenAI
    openai_client = OpenAI()

    return supabase, gemini_client, openai_client


# ---------------------------------------------------------------------------
# LLM call helpers
# ---------------------------------------------------------------------------

def _call_gemini(gemini_client, system_prompt: str, user_prompt: str, schema_class: type) -> dict | None:
    """
    Call Gemini Flash in JSON mode. Returns a raw dict or None on failure.
    Retries 3 times with 429-aware backoff.
    """
    from google import genai as _genai
    from google.genai import types as _gtypes

    schema_str = json.dumps(schema_class.model_json_schema(), indent=2)
    full_prompt = (
        f"{system_prompt}\n\n"
        f"Return ONLY a JSON object matching this schema (no markdown, no explanation):\n"
        f"{schema_str}\n\n"
        f"{user_prompt}"
    )

    for attempt in range(3):
        try:
            resp = gemini_client.models.generate_content(
                model=_GEMINI_MODEL,
                contents=full_prompt,
                config=_gtypes.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            return json.loads(resp.text)
        except Exception as e:
            err = str(e)
            is_429 = "429" in err or "quota" in err.lower() or "rate" in err.lower()
            if attempt < 2:
                wait = (15 * (attempt + 1)) if is_429 else (2 ** attempt)
                if is_429:
                    print(f"  [Gemini 429] waiting {wait}s before retry {attempt + 1}/2...")
                time.sleep(wait)
            else:
                print(f"  [Gemini] Failed after 3 attempts: {e}")
    return None


def _call_gpt(openai_client, system_prompt: str, user_prompt: str, schema_class: type) -> Any | None:
    """
    Call GPT-4o-mini with structured output. Returns parsed Pydantic object or None.
    Retries 3 times with 429-aware backoff.
    """
    for attempt in range(3):
        try:
            resp = openai_client.beta.chat.completions.parse(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                response_format=schema_class,
                temperature=0.1,
            )
            return resp.choices[0].message.parsed
        except Exception as e:
            err = str(e)
            is_429 = "429" in err or "rate_limit" in err
            if attempt < 2:
                wait = (15 * (attempt + 1)) if is_429 else (2 ** attempt)
                if is_429:
                    print(f"  [GPT 429] waiting {wait}s before retry {attempt + 1}/2...")
                time.sleep(wait)
            else:
                print(f"  [GPT] Failed after 3 attempts: {e}")
    return None


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompts(
    template: str,
    section_title: str,
    parent_title: str | None,
    document_type: str,
    lexnlp_hints: str,
    section_text: str,
) -> tuple[str, str]:
    description = TEMPLATE_DESCRIPTIONS.get(template, TEMPLATE_DESCRIPTIONS["generic"])
    system_prompt = (
        f"You are a legal document extraction engine reading a section from a '{document_type}' document.\n\n"
        f"{description}\n\n"
        "Be precise — extract only what is explicitly stated. Do not infer.\n"
        "If nothing is found, return an empty list for the main field."
    )
    hints_block = ""
    if lexnlp_hints:
        hints_block = (
            "\n--- Pre-extracted hints (regex-based, may have false positives) ---\n"
            f"{lexnlp_hints}\n"
            "--- Use these as starting points only. Correct errors, add context, discard false positives.\n"
            "    Bullet numbers like '2.' are NOT money amounts.\n"
        )
    user_prompt = (
        f"Section title: {section_title}\n"
        f"Parent section: {parent_title or 'Root level'}\n"
        f"Document type: {document_type}"
        f"{hints_block}\n"
        f"Section text:\n{section_text[:3000]}"
    )
    return system_prompt, user_prompt


# ---------------------------------------------------------------------------
# Convert extraction result -> rows for the extractions table
# ---------------------------------------------------------------------------

def _to_rows(
    template: str,
    result: Any,             # Pydantic object (from GPT) or dict (from Gemini)
    schema_class: type,
    list_field: str,
    section_id: str,
    document_id: str,
    page_range: str | None,
    extraction_method: str,
    section_title: str = "",
    semantic_label: str = "",
    force_review: bool = False,
) -> list[dict]:
    """Flatten a Pydantic/dict extraction result into flat rows."""
    # Normalize Gemini dict -> Pydantic
    if isinstance(result, dict):
        try:
            result = schema_class(**result)
        except (ValidationError, TypeError) as e:
            print(f"  [rows] Pydantic validation failed: {e}")
            return []

    entities = getattr(result, list_field, []) or []
    rows = []

    for ent in entities:
        conf = float(getattr(ent, "confidence", 0.8))
        if conf < 0.5:
            continue  # skip low-confidence

        needs_review = force_review or conf < 0.7

        if template == "party":
            entity_name  = ent.name
            entity_value = ent.entity_type
            props = {"role": ent.role, "jurisdiction": ent.jurisdiction, "address": ent.address}

        elif template == "date":
            entity_name  = ent.description
            entity_value = ent.date_value
            props = {"date_type": ent.date_type, "is_relative": ent.is_relative,
                     "reference_event": ent.reference_event}

        elif template == "obligation":
            entity_name  = ent.description
            entity_value = ent.action
            props = {"obligated_party": ent.obligated_party, "beneficiary_party": ent.beneficiary_party,
                     "deadline": ent.deadline, "condition": ent.condition, "amount": ent.amount}

        elif template == "claim":
            entity_name  = ent.description
            entity_value = ent.claim_type
            props = {"plaintiff": ent.plaintiff, "defendant": ent.defendant,
                     "alleged_facts": ent.alleged_facts,
                     "evidence_references": ent.evidence_references,
                     "damages_sought": ent.damages_sought}

        elif template == "amount":
            entity_name  = ent.description
            entity_value = ent.value
            props = {"currency": ent.currency, "is_calculated": ent.is_calculated,
                     "payer": ent.payer, "payee": ent.payee}

        elif template == "condition":
            entity_name  = ent.description
            entity_value = ent.condition_type
            props = {"trigger_event": ent.trigger_event, "consequence": ent.consequence,
                     "affected_party": ent.affected_party}

        elif template == "evidence_ref":
            entity_name  = ent.reference_label
            entity_value = ent.reference_label
            props = {"description": ent.description, "context": ent.referenced_in_context}

        else:  # generic
            entity_name  = ent.entity_name
            entity_value = ent.entity_value
            props = {"description": ent.description}

        rows.append({
            "section_id":        section_id,
            "document_id":       document_id,
            "extraction_type":   template if template != "generic" else getattr(ent, "entity_type", "generic"),
            "entity_name":       entity_name,
            "entity_value":      entity_value,
            "raw_text":          getattr(ent, "raw_text", None),
            "confidence":        conf,
            "page_range":        page_range,
            "extraction_method": extraction_method,
            "properties":        {k: v for k, v in props.items() if v is not None},
            "needs_review":      needs_review,
            # _private fields stripped before Supabase insert — used for output files only
            "_section_title":    section_title,
            "_semantic_label":   semantic_label,
        })

    return rows


# ---------------------------------------------------------------------------
# Per-section extraction
# ---------------------------------------------------------------------------

def _extract_section(
    section: dict,
    document_type: str,
    gemini_client,
    openai_client,
) -> list[dict]:
    """Extract entities from one section. Returns list of rows for extractions table."""
    sec_id      = section["id"]
    document_id = section["document_id"]
    title       = section.get("section_title") or ""
    text        = section.get("section_text") or ""
    label       = section.get("semantic_label") or "unrecognized"
    conf        = float(section.get("semantic_confidence") or 0.0)
    parent_title= section.get("_parent_title")
    page_range  = section.get("page_range")

    # Guard: skip low-confidence and empty sections.
    # SKIP_LABELS filtering is handled by the caller (main loop) which also
    # performs leaf-node detection for wrapper labels — don't re-check here.
    if label != "unrecognized" and conf < 0.5:
        return []
    if len(text.strip()) < 50:
        return []

    template    = LABEL_TO_TEMPLATE.get(label)
    is_generic  = template is None
    if is_generic:
        template = "generic"

    schema_class, list_field = TEMPLATE_TO_CLASS[template]
    use_gemini = TEMPLATE_TO_MODEL[template] == "gemini-flash" and gemini_client is not None

    hints = _lexnlp_hints(text, template)
    system_prompt, user_prompt = _build_prompts(
        template, title, parent_title, document_type, hints, text
    )

    lexnlp_used = bool(hints)
    result = None
    extraction_method = ""

    if use_gemini:
        raw = _call_gemini(gemini_client, system_prompt, user_prompt, schema_class)
        time.sleep(0.5)
        if raw is not None:
            extraction_method = "lexnlp+gemini" if lexnlp_used else "gemini-flash"
            result = raw
        else:
            # Fallback to GPT
            result = _call_gpt(openai_client, system_prompt, user_prompt, schema_class)
            time.sleep(0.5)
            extraction_method = "lexnlp+gpt" if lexnlp_used else "gpt-4o-mini"
    else:
        result = _call_gpt(openai_client, system_prompt, user_prompt, schema_class)
        time.sleep(0.5)
        extraction_method = "lexnlp+gpt" if lexnlp_used else "gpt-4o-mini"

    if result is None:
        return []

    return _to_rows(
        template, result, schema_class, list_field,
        sec_id, document_id, page_range, extraction_method,
        section_title=title,
        semantic_label=label,
        force_review=is_generic,
    )


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _resolve_document(supabase, args) -> tuple[str, str, str | None]:
    if args.document_id:
        resp = supabase.table("documents").select("id, file_name, document_type").eq("id", args.document_id).execute()
    else:
        resp = supabase.table("documents").select("id, file_name, document_type").eq("file_name", args.file_name).execute()
    if not resp.data:
        key = args.document_id or args.file_name
        print(f"ERROR: No document found for '{key}'")
        sys.exit(1)
    row = resp.data[0]
    return row["id"], row["file_name"], row.get("document_type")


def _write_rows(supabase, rows: list[dict]) -> int:
    """Batch-insert rows into extractions table in chunks of 100. Returns error count."""
    errors = 0
    for i in range(0, len(rows), 100):
        batch = rows[i:i + 100]
        try:
            supabase.table("extractions").insert(batch).execute()
        except Exception as e:
            print(f"  WARNING: Batch insert failed ({len(batch)} rows) — {e}")
            errors += len(batch)
    return errors


# ---------------------------------------------------------------------------
# Output file writers
# ---------------------------------------------------------------------------

def _write_output_files(
    all_rows: list[dict],
    file_name: str,
    document_type: str | None,
) -> tuple[str, str]:
    """
    Write extractions to JSON and MD files in zz_temp_chunks/.
    Returns (json_path, md_path).
    """
    temp_dir = os.path.join(
        os.path.dirname(__file__), '..', 'zz_temp_chunks'
    )
    os.makedirs(temp_dir, exist_ok=True)
    base = os.path.join(temp_dir, f"{file_name}_03_extractions")

    # --- Group rows by section ---
    sections_map: dict[str, dict] = {}
    for row in all_rows:
        sid = row["section_id"]
        if sid not in sections_map:
            sections_map[sid] = {
                "section_id":    sid,
                "section_title": row.get("_section_title", ""),
                "semantic_label":row.get("_semantic_label", ""),
                "page_range":    row.get("page_range"),
                "extractions":   [],
            }
        sections_map[sid]["extractions"].append({
            "extraction_type":   row["extraction_type"],
            "entity_name":       row["entity_name"],
            "entity_value":      row.get("entity_value"),
            "raw_text":          row.get("raw_text"),
            "confidence":        row["confidence"],
            "extraction_method": row["extraction_method"],
            "needs_review":      row["needs_review"],
            "properties":        row.get("properties", {}),
        })

    # Count by type
    by_type: dict[str, int] = {}
    for row in all_rows:
        t = row["extraction_type"]
        by_type[t] = by_type.get(t, 0) + 1

    # --- JSON ---
    payload = {
        "document":        file_name,
        "document_type":   document_type or "Unknown",
        "extracted_at":    datetime.now(timezone.utc).isoformat(),
        "total_entities":  len(all_rows),
        "by_type":         by_type,
        "sections":        list(sections_map.values()),
    }
    json_path = base + ".json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    # --- Markdown ---
    lines: list[str] = [
        f"# Entity Extraction — {file_name}",
        f"**Document type:** {document_type or 'Unknown'}  ",
        f"**Extracted at:** {payload['extracted_at']}  ",
        f"**Total entities:** {len(all_rows)}  ",
        "",
        "## Summary by type",
        "",
    ]
    for t, cnt in sorted(by_type.items()):
        lines.append(f"- **{t}**: {cnt}")
    lines.append("")

    # Group by extraction_type for a readable view
    by_type_sections: dict[str, list[dict]] = {}
    for row in all_rows:
        t = row["extraction_type"]
        if t not in by_type_sections:
            by_type_sections[t] = []
        by_type_sections[t].append(row)

    for t, rows in sorted(by_type_sections.items()):
        lines.append(f"## {t.replace('_', ' ').title()} ({len(rows)})")
        lines.append("")
        for row in rows:
            review_flag = " ⚠️" if row["needs_review"] else ""
            lines.append(f"### {row['entity_name']}{review_flag}")
            if row.get("entity_value") and row["entity_value"] != row["entity_name"]:
                lines.append(f"**Value:** {row['entity_value']}  ")
            lines.append(f"**Section:** {row.get('_section_title', '—')} `{row.get('_semantic_label', '')}`  ")
            if row.get("page_range"):
                lines.append(f"**Pages:** {row['page_range']}  ")
            lines.append(f"**Confidence:** {row['confidence']:.0%}  ")
            lines.append(f"**Method:** {row['extraction_method']}  ")
            if row.get("raw_text"):
                lines.append(f"> {row['raw_text'][:200]}")
            props = {k: v for k, v in (row.get("properties") or {}).items() if v}
            if props:
                lines.append(f"**Details:** {props}  ")
            lines.append("")

    md_path = base + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return json_path, md_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Extract entities from AST sections.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--document_id", help="UUID of the document in Supabase")
    group.add_argument("--file_name",   help="file_name stem of the document")
    args = parser.parse_args()

    supabase, gemini_client, openai_client = _init_clients()
    document_id, file_name, document_type = _resolve_document(supabase, args)

    if not LEXNLP_AVAILABLE:
        print("  INFO: lexnlp not installed — running without pre-extraction hints")

    print(f"  Extracting entities from '{file_name}' ({document_type or 'Unknown type'})")

    # Fetch all sections with semantic labels
    try:
        resp = (
            supabase.table("sections")
            .select("id, document_id, section_title, section_text, semantic_label, "
                    "semantic_confidence, parent_section_id, page_range, is_synthetic")
            .eq("document_id", document_id)
            .execute()
        )
    except Exception as e:
        print(f"ERROR: Failed to fetch sections — {e}")
        sys.exit(1)

    sections = resp.data or []
    if not sections:
        print(f"ERROR: No sections found for '{file_name}'")
        sys.exit(1)

    # Build parent title lookup
    id_to_title: dict[str, str] = {s["id"]: (s.get("section_title") or "") for s in sections}
    for sec in sections:
        pid = sec.get("parent_section_id")
        sec["_parent_title"] = id_to_title.get(pid) if pid else None

    # Sort by page order
    sections.sort(key=lambda s: (s.get("page_range") is None, s.get("page_range") or ""))

    # Clear existing extractions for this document (makes re-runs safe)
    try:
        supabase.table("extractions").delete().eq("document_id", document_id).execute()
    except Exception as e:
        print(f"  WARNING: Could not clear existing extractions — {e}")

    # --- Extract ---
    all_rows:    list[dict]      = []
    skipped:     int             = 0
    by_template: dict[str, int]  = {}
    via_gemini:  int             = 0
    via_gpt:     int             = 0

    # Pre-build set of section IDs that have at least one child for O(1) leaf detection
    parent_ids: set[str] = {
        s["parent_section_id"] for s in sections if s.get("parent_section_id")
    }

    total = len(sections)
    for idx, sec in enumerate(sections, 1):
        label  = sec.get("semantic_label") or "unrecognized"
        conf   = float(sec.get("semantic_confidence") or 0.0)
        text   = sec.get("section_text") or ""
        title  = (sec.get("section_title") or "untitled")[:60]
        sec_id = sec["id"]

        is_wrapper = label in SKIP_LABELS
        if is_wrapper and label in LEAF_WRAPPER_LABELS and sec_id not in parent_ids:
            # Label is normally a parent wrapper, but this section has no children
            # (refiner didn't split it) — treat as a leaf and extract from it
            is_wrapper = False

        if is_wrapper:
            print(f"  [{idx}/{total}] SKIP  '{title}' — boilerplate ({label})")
            skipped += 1
            continue
        # "unrecognized" bypasses the confidence gate — runs generic extraction
        if label != "unrecognized" and conf < 0.5:
            print(f"  [{idx}/{total}] SKIP  '{title}' — low label confidence ({conf:.0%})")
            skipped += 1
            continue
        if len(text.strip()) < 50 or text.startswith("[Split into"):
            print(f"  [{idx}/{total}] SKIP  '{title}' — refined parent (children have the text)")
            skipped += 1
            continue

        rows = _extract_section(sec, document_type or "Unknown", gemini_client, openai_client)

        if rows:
            template = LABEL_TO_TEMPLATE.get(label, "generic")
            by_template[template] = by_template.get(template, 0) + len(rows)
            method = rows[0].get("extraction_method", "?")
            for r in rows:
                if "gemini" in r.get("extraction_method", ""):
                    via_gemini += 1
                else:
                    via_gpt += 1
            all_rows.extend(rows)
            print(f"  [{idx}/{total}] OK    '{title}' ({label}) -> {len(rows)} entities [{method}]")
        else:
            print(f"  [{idx}/{total}] EMPTY '{title}' ({label}) -> no entities found")

    # Normalize extraction types — drop anything not in the allowed set
    dropped_count = 0
    normalized_rows: list[dict] = []
    for row in all_rows:
        raw_type   = row.get("extraction_type", "")
        normalized = normalize_extraction_type(raw_type)
        if normalized is None:
            dropped_count += 1
            continue
        row["extraction_type"] = normalized
        normalized_rows.append(row)
    all_rows = normalized_rows

    # Write output files (before stripping private fields)
    json_path, md_path = _write_output_files(all_rows, file_name, document_type)

    # Strip _private fields before Supabase insert
    supabase_rows = [
        {k: v for k, v in row.items() if not k.startswith("_")}
        for row in all_rows
    ]

    # Write to Supabase
    write_errors = _write_rows(supabase, supabase_rows)
    if write_errors:
        print(
            f"ERROR: Extraction complete but {write_errors} row(s) failed to write "
            f"for '{file_name}'."
        )
        sys.exit(1)

    by_type_str   = ", ".join(f"{k}: {v}" for k, v in sorted(by_template.items()))
    needs_review  = sum(1 for r in all_rows if r.get("needs_review"))
    with_lexnlp   = sum(1 for r in all_rows if "lexnlp" in r.get("extraction_method", ""))
    processed     = len(sections) - skipped

    print(
        f"SUCCESS: Extracted {len(all_rows)} entities from {processed} sections for '{file_name}'. "
        f"Skipped {skipped}. [{by_type_str or 'none'}] "
        f"{via_gemini} via Gemini, {via_gpt} via GPT, {with_lexnlp} with LexNLP hints, "
        f"{needs_review} flagged for review, {dropped_count} dropped (invalid type).\n"
        f"  JSON : {json_path}\n"
        f"  MD   : {md_path}"
    )


if __name__ == "__main__":
    main()
