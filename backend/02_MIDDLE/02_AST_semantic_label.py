"""
02_AST_semantic_label.py — Phase 2, Step 2: Semantic Labeling

Assigns ontology labels to each AST node using pattern matching (financial/
annual report docs) and GPT-4o-mini (contracts, complaints, and fallbacks).

Usage:
    python 02_AST_semantic_label.py --file_name "Complaint (Epic Games to Apple"
    python 02_AST_semantic_label.py --document_id "abc-123-uuid"
"""

import argparse
import os
import sys
import time

from dotenv import load_dotenv
from pydantic import BaseModel
from supabase import create_client

# Load .env from project root (two levels up from backend/02_MIDDLE/)
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))


# ---------------------------------------------------------------------------
# Ontology label sets
# ---------------------------------------------------------------------------

CONTRACT_LABELS = [
    "contract_root", "preamble", "preamble.title_block", "preamble.parties",
    "preamble.recitals", "preamble.effective_date", "definitions", "definitions.term",
    "scope", "scope.subject_matter", "scope.exclusions", "obligation",
    "obligation.performance", "obligation.payment", "obligation.payment.amount",
    "obligation.payment.schedule", "obligation.payment.method", "obligation.delivery",
    "obligation.reporting", "obligation.notification", "rights", "rights.license_grant",
    "rights.audit_rights", "rights.step_in_rights", "condition", "condition.precedent",
    "condition.subsequent", "condition.concurrent", "representation",
    "representation.authority", "representation.compliance", "representation.financial",
    "representation.no_litigation", "warranty", "warranty.product_quality",
    "warranty.service_level", "warranty.ip_ownership", "covenant",
    "covenant.non_compete", "covenant.non_solicitation", "covenant.non_disclosure",
    "covenant.exclusivity", "indemnification", "indemnification.scope",
    "indemnification.limitations", "indemnification.procedure", "liability",
    "liability.limitation", "liability.cap", "liability.exclusion", "termination",
    "termination.for_cause", "termination.for_convenience", "termination.expiration",
    "termination.effects", "dispute_resolution", "dispute_resolution.governing_law",
    "dispute_resolution.jurisdiction", "dispute_resolution.arbitration",
    "dispute_resolution.mediation", "confidentiality", "confidentiality.scope",
    "confidentiality.exceptions", "confidentiality.duration", "ip_rights",
    "ip_rights.ownership", "ip_rights.license", "ip_rights.assignment", "insurance",
    "force_majeure", "amendment_procedure", "assignment", "notices", "severability",
    "entire_agreement", "signature_block", "exhibit_reference", "schedule_reference",
]

COMPLAINT_LABELS = [
    "complaint_root", "caption", "caption.court", "caption.parties",
    "caption.case_number", "introduction", "jurisdiction", "jurisdiction.subject_matter",
    "jurisdiction.personal", "venue", "parties", "parties.plaintiff", "parties.defendant",
    "factual_allegations", "factual_allegations.background", "factual_allegations.relationship",
    "factual_allegations.breach_event", "factual_allegations.damages_description",
    "factual_allegations.timeline", "causes_of_action", "causes_of_action.breach_of_contract",
    "causes_of_action.negligence", "causes_of_action.fraud",
    "causes_of_action.statutory_violation", "causes_of_action.unjust_enrichment",
    "causes_of_action.declaratory_relief", "damages", "damages.compensatory",
    "damages.consequential", "damages.punitive", "damages.statutory",
    "damages.equitable_relief", "prayer_for_relief", "jury_demand", "verification",
    "signature_block", "exhibit_reference", "certificate_of_service",
]

FINANCIAL_LABELS = [
    "financial_root", "cover_page", "management_discussion", "auditor_report",
    "balance_sheet", "income_statement", "cash_flow_statement", "equity_statement",
    "notes_to_financials", "notes.accounting_policies", "notes.revenue_recognition",
    "notes.debt_obligations", "notes.contingencies", "notes.related_party",
    "supplementary_schedules", "signature_block",
]

ANNUAL_REPORT_LABELS = [
    "annual_report_root", "letter_to_shareholders", "company_overview",
    "business_segments", "risk_factors", "legal_proceedings", "executive_compensation",
    "corporate_governance", "financial_statements", "market_data", "appendices",
]


# ---------------------------------------------------------------------------
# Ontology selection
# ---------------------------------------------------------------------------

def _select_ontology(document_type: str | None) -> tuple[list[str], str]:
    """Return (label_list, ontology_name). ontology_name used in system prompt."""
    dt = (document_type or "").strip()
    if dt.startswith("Contract"):
        return CONTRACT_LABELS, "Contract"
    if dt.startswith("Pleading"):
        return COMPLAINT_LABELS, "Pleading / Legal Complaint"
    if any(k in dt for k in ("Financial", "10-K", "10-Q")):
        return FINANCIAL_LABELS, "Financial Statement"
    if "Annual Report" in dt:
        return ANNUAL_REPORT_LABELS, "Annual Report"
    return [], "Unknown"


# ---------------------------------------------------------------------------
# Pattern matching (financial / annual report docs)
# ---------------------------------------------------------------------------

_FINANCIAL_PATTERNS: list[tuple[list[str], str]] = [
    (["Balance Sheet", "Statement of Financial Position"],          "balance_sheet"),
    (["Income Statement", "Statement of Operations", "Profit and Loss"], "income_statement"),
    (["Cash Flow"],                                                 "cash_flow_statement"),
    (["Stockholders' Equity", "Changes in Equity"],                 "equity_statement"),
    (["MD&A", "Management Discussion", "Management's Discussion"],  "management_discussion"),
    (["Auditor", "Independent Registered"],                         "auditor_report"),
    (["Accounting Polic"],                                          "notes.accounting_policies"),
    (["Revenue Recognition"],                                       "notes.revenue_recognition"),
    (["Debt", "Borrowings"],                                        "notes.debt_obligations"),
    (["Contingenc"],                                                "notes.contingencies"),
    (["Related Part"],                                              "notes.related_party"),
]

_ANNUAL_PATTERNS: list[tuple[list[str], str]] = [
    (["Letter to Shareholder", "Dear Shareholder", "Message from"],  "letter_to_shareholders"),
    (["Company Overview", "About Us", "Who We Are"],                 "company_overview"),
    (["Business Segment", "Operating Segment"],                      "business_segments"),
    (["Risk Factor"],                                                "risk_factors"),
    (["Legal Proceeding"],                                           "legal_proceedings"),
    (["Executive Compensation", "Compensation Discussion"],          "executive_compensation"),
    (["Corporate Governance", "Board of Directors"],                 "corporate_governance"),
    (["Financial Statement", "Consolidated Statement"],              "financial_statements"),
    (["Market Data", "Stock Price", "Dividend"],                     "market_data"),
    (["Appendix", "Appendices", "Exhibit"],                          "appendices"),
]


def _pattern_match(title: str, ontology_name: str) -> str | None:
    """Return label if title matches a pattern, else None."""
    t = title.lower()
    patterns = _FINANCIAL_PATTERNS if "Financial" in ontology_name else _ANNUAL_PATTERNS
    for keywords, label in patterns:
        if any(kw.lower() in t for kw in keywords):
            return label
    # Check "Notes to Financial/Consolidated" specially
    if "notes to" in t and ("financial" in t or "consolidated" in t):
        return "notes_to_financials"
    return None


def _use_pattern_matching(ontology_name: str) -> bool:
    return ontology_name in ("Financial Statement", "Annual Report")


# ---------------------------------------------------------------------------
# GPT-4o-mini structured output
# ---------------------------------------------------------------------------

class SemanticLabel(BaseModel):
    semantic_label: str
    confidence: float


def _gpt_label(
    client,
    section_title: str,
    parent_title: str | None,
    section_text: str,
    document_type: str,
    ontology_labels: list[str],
) -> tuple[str, float]:
    """Call GPT-4o-mini. Returns (label, confidence). Retries up to 2 times."""
    labels_str = "\n".join(f"  - {l}" for l in ontology_labels)
    system_prompt = (
        f"You are a legal document analyst. Given a section from a '{document_type}', "
        f"classify it using ONLY the following ontology labels:\n{labels_str}\n\n"
        "Return a JSON object with 'semantic_label' (string, must be exactly one label "
        "from the provided list) and 'confidence' (float 0-1)."
    )
    parent_str = parent_title if parent_title else "None (root level)"
    text_snippet = (section_text or "")[:1500]
    user_prompt = (
        f"Section title: {section_title}\n"
        f"Parent section title: {parent_str}\n"
        f"Section text (first 1500 chars): {text_snippet}"
    )

    for attempt in range(4):
        try:
            response = client.beta.chat.completions.parse(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                response_format=SemanticLabel,
            )
            result = response.choices[0].message.parsed
            label      = result.semantic_label
            confidence = float(result.confidence)

            if label not in ontology_labels:
                return "unrecognized", 0.0

            return label, confidence

        except Exception as e:
            err_str = str(e)
            is_rate_limit = "429" in err_str or "rate_limit" in err_str
            if attempt < 3:
                # Rate limit: back off much longer; other errors: short backoff
                wait = (15 * (attempt + 1)) if is_rate_limit else (2 ** attempt)
                if is_rate_limit:
                    print(f"  [Rate limit] waiting {wait}s before retry {attempt + 1}/3...")
                time.sleep(wait)
            else:
                print(f"  WARNING: GPT call failed after 4 attempts — {e}")
                return "error", 0.0

    return "error", 0.0


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _get_client():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)
    return create_client(url, key)


def _resolve_document(supabase, args) -> tuple[str, str, str | None]:
    """Return (document_id, file_name, document_type)."""
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Assign semantic labels to AST nodes.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--document_id", help="UUID of the document in Supabase")
    group.add_argument("--file_name",   help="file_name stem of the document")
    args = parser.parse_args()

    # Check OpenAI key early
    openai_key = os.environ.get("OPENAI_API_KEY")

    supabase = _get_client()
    document_id, file_name, document_type = _resolve_document(supabase, args)

    ontology_labels, ontology_name = _select_ontology(document_type)
    use_patterns = _use_pattern_matching(ontology_name)

    if ontology_name == "Unknown":
        print(
            f"  WARNING: document_type='{document_type}' not recognized — "
            "all sections will be labeled 'unrecognized' and flagged for review."
        )

    # Fetch sections with parent titles
    try:
        resp = (
            supabase.table("sections")
            .select("id, section_title, section_text, parent_section_id, start_page")
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

    # Build id → title map for parent lookup
    id_to_title: dict[str, str] = {
        s["id"]: (s.get("section_title") or "") for s in sections
    }

    # Sort by start_page
    sections.sort(key=lambda s: (s.get("start_page") is None, s.get("start_page") or 0))

    # Initialize OpenAI client only if needed
    openai_client = None
    if not use_patterns or ontology_name == "Unknown":
        if not openai_key:
            print("ERROR: OPENAI_API_KEY not set in .env — required for GPT labeling")
            sys.exit(1)
        from openai import OpenAI
        openai_client = OpenAI()

    # Label each section
    pattern_count = 0
    gpt_count     = 0
    flagged_count = 0
    error_count   = 0

    updates: list[dict] = []

    for sec in sections:
        sec_id       = sec["id"]
        title        = sec.get("section_title") or ""
        text         = sec.get("section_text") or ""
        parent_id    = sec.get("parent_section_id")
        parent_title = id_to_title.get(parent_id) if parent_id else None

        label      = "unrecognized"
        confidence = 0.0
        source     = "pattern"

        if ontology_name == "Unknown":
            label      = "unrecognized"
            confidence = 0.0
            source     = "pattern"
            flagged_count += 1

        elif use_patterns:
            matched = _pattern_match(title, ontology_name)
            if matched:
                label      = matched
                confidence = 1.0
                source     = "pattern"
                pattern_count += 1
            else:
                # Fall back to GPT for unmatched sections
                label, confidence = _gpt_label(
                    openai_client, title, parent_title, text,
                    document_type or ontology_name, ontology_labels,
                )
                source = "gpt-4o-mini"
                gpt_count += 1
                if label in ("unrecognized", "error"):
                    error_count += 1
                elif confidence < 0.7:
                    flagged_count += 1
                time.sleep(0.5)

        else:
            # Contract / Complaint / fallback — always GPT
            label, confidence = _gpt_label(
                openai_client, title, parent_title, text,
                document_type or ontology_name, ontology_labels,
            )
            source = "gpt-4o-mini"
            gpt_count += 1
            if label in ("unrecognized", "error"):
                error_count += 1
            elif confidence < 0.7:
                flagged_count += 1

        # Small delay between GPT calls to stay under TPM rate limit
        if source == "gpt-4o-mini":
            time.sleep(0.5)

        updates.append({
            "id":                  sec_id,
            "semantic_label":      label,
            "semantic_confidence": confidence,
            "label_source":        source,
        })

    # Write back to Supabase
    write_errors = 0
    for upd in updates:
        sec_id = upd.pop("id")
        try:
            supabase.table("sections").update(upd).eq("id", sec_id).execute()
        except Exception as e:
            print(f"  WARNING: Could not update section {sec_id} — {e}")
            write_errors += 1

    if write_errors:
        print(
            f"ERROR: Labeling completed but {write_errors} section(s) failed to write "
            f"for '{file_name}'."
        )
        sys.exit(1)

    print(
        f"SUCCESS: Labeled {len(sections)} sections for '{file_name}'. "
        f"{pattern_count} pattern-matched, {gpt_count} GPT-labeled, "
        f"{flagged_count} flagged for review."
    )


if __name__ == "__main__":
    main()
