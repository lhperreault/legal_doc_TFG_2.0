"""
02_MIDDLE/07C_case_context_classification.py — Case-Context Re-Classification

Re-classifies a document using full case context:
  - Who filed this? (party_perspective)
  - Is this the primary/operative version? (is_primary_filing)
  - Is it current or historical? (temporal_role)
  - Why does it exist in the case? (filing_purpose)
  - Should the document_type be corrected? (e.g., exhibit containing a historical complaint)

Also advances the case stage in Supabase when the document implies a later stage,
and creates a case_events row for the transition.

Requires: GOOGLE_API_KEY and SUPABASE_* in .env

Usage:
    python 07C_case_context_classification.py --document_id <uuid>
    python 07C_case_context_classification.py --file_name "complaint"
    python 07C_case_context_classification.py --document_id <uuid> --dry_run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from supabase import create_client, Client

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))


# ── Stage ordering ────────────────────────────────────────────────────────────

_STAGE_ORDER: dict[str, int] = {
    "filing":    0,
    "discovery": 1,
    "motions":   2,
    "trial":     3,
    "appeal":    4,
    "closed":    5,
}

_STAGE_PREFIXES: list[tuple[str, str]] = [
    ("Discovery",             "discovery"),
    ("Pleading - Appeal",     "appeal"),
    ("Pleading - Motion",     "motions"),
    ("Court - Trial",         "trial"),
    ("Court - Scheduling",    "motions"),
    ("Pleading - Complaint",  "filing"),
    ("Pleading - Amended",    "filing"),
]

# Valid enum values (used for fallback clamping)
VALID_PERSPECTIVES = {"plaintiff", "defendant", "court", "third_party", "unknown"}
VALID_TEMPORAL     = {"current", "historical", "superseded"}
VALID_PURPOSES     = {
    "operative_pleading", "motion", "brief", "evidence",
    "historical_context", "supporting_material", "court_order",
}


def _infer_stage(doc_type: str) -> str:
    for prefix, stage in _STAGE_PREFIXES:
        if doc_type.startswith(prefix):
            return stage
    return "filing"


def _stage_is_later(new: str, current: str) -> bool:
    return _STAGE_ORDER.get(new, 0) > _STAGE_ORDER.get(current, 0)


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _get_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)
    return create_client(url, key)


def _resolve_document(sb: Client, args: argparse.Namespace) -> dict:
    if getattr(args, "document_id", None):
        resp = sb.table("documents").select("*").eq("id", args.document_id).execute()
    else:
        resp = sb.table("documents").select("*").eq("file_name", args.file_name).execute()
    if not resp.data:
        key = getattr(args, "document_id", None) or args.file_name
        print(f"ERROR: No document found for '{key}'")
        sys.exit(1)
    return resp.data[0]


def _fetch_case(sb: Client, case_id: str) -> dict | None:
    resp = sb.table("cases").select("*").eq("id", case_id).execute()
    return resp.data[0] if resp.data else None


def _fetch_case_documents(sb: Client, case_id: str, exclude_id: str) -> list[dict]:
    resp = (
        sb.table("documents")
        .select("id, file_name, document_type, is_primary_filing, created_at")
        .eq("case_id", case_id)
        .neq("id", exclude_id)
        .order("created_at", desc=False)
        .execute()
    )
    return resp.data or []


def _case_has_primary(sb: Client, case_id: str, doc_type_prefix: str) -> bool:
    """Return True if any document in the case is already marked primary
    with a document_type that starts with the same top-level category."""
    resp = (
        sb.table("documents")
        .select("id, document_type")
        .eq("case_id", case_id)
        .eq("is_primary_filing", True)
        .execute()
    )
    for d in resp.data or []:
        if (d.get("document_type") or "").startswith(doc_type_prefix):
            return True
    return False


# ── Case summary builder ──────────────────────────────────────────────────────

def build_case_summary(case: dict, existing_docs: list[dict]) -> str:
    lines = [
        f"Case: {case.get('case_name', 'Unknown')}",
        f"Our role: {case.get('party_role', 'unknown')} "
        f"(representing {case.get('our_client') or 'unspecified'})",
        f"Opposing party: {case.get('opposing_party') or 'unspecified'}",
        f"Current stage: {case.get('case_stage') or 'filing'}",
        f"Court: {case.get('court_name') or 'unknown'}",
        f"Judge: {case.get('judge_name') or 'unknown'}",
        "",
        f"User-provided context: {case.get('case_context') or 'none'}",
        "",
        "Documents already in this case:",
    ]
    if existing_docs:
        for d in existing_docs:
            primary_flag = " [PRIMARY]" if d.get("is_primary_filing") else ""
            doc_type     = d.get("document_type") or "unclassified"
            lines.append(f"  - {d['file_name']}: {doc_type}{primary_flag}")
    else:
        lines.append("  (none yet — this is the first document)")
    return "\n".join(lines)


# ── Pydantic result model ─────────────────────────────────────────────────────

class ContextClassResult(BaseModel):
    is_primary_filing:          bool
    party_perspective:          str   = "unknown"
    temporal_role:              str   = "current"
    filing_purpose:             str   = "evidence"
    should_update_document_type: bool = False
    updated_document_type:      Optional[str] = None
    confidence:                 float = Field(default=0.75, ge=0.0, le=1.0)
    reasoning:                  str   = ""


# ── Gemini Flash prompt ───────────────────────────────────────────────────────

_PROMPT_TEMPLATE = """You are a legal document analyst with deep knowledge of litigation workflows.

## Case Context
{case_summary}

## Document Being Classified
File name: {file_name}
Initial classification (from prior pipeline step): {doc_type}
Classification confidence: {confidence:.0%}
{parent_block}
## Document Content (first 3000 characters)
{doc_content}

---

## Your Task

Analyze this document in the context of the case above and determine the following:

1. **is_primary_filing** (boolean): Is this THE operative/live document for its type in this case?
   - A primary filing is the document actually in effect (e.g., the current operative complaint, the live answer).
   - If the case already has a primary document of the same type, this is NOT primary.
   - Exhibits containing a copy of another document are NOT primary.
   - Historical background documents (prior complaints, superseded orders) are NOT primary.

2. **party_perspective** (one of: plaintiff | defendant | court | third_party | unknown):
   Whose position does this document represent or advance?

3. **temporal_role** (one of: current | historical | superseded):
   - current: operative, still in effect
   - historical: background or from an earlier stage of litigation
   - superseded: explicitly replaced by an amended version

4. **filing_purpose** (one of: operative_pleading | motion | brief | evidence | historical_context | supporting_material | court_order):
   Why does this document exist in the case file?

5. **should_update_document_type** (boolean): Should we correct the initial classification?
   Example: initial said "Pleading - Complaint" but closer inspection shows it is an exhibit
   containing an older complaint → update to "Evidence - Declaration" or similar.

6. **updated_document_type** (string, only if should_update_document_type is true):
   The corrected classification label. MUST be one of the valid legal document type labels
   used by the classifier (e.g. "Evidence - Contract Exhibit", "Pleading - Amended Complaint").

7. **confidence** (float 0.0–1.0): How confident are you in this assessment?

8. **reasoning** (string): 2–3 sentences explaining your determination.

Return a single valid JSON object with exactly these keys. No markdown, no extra text.
"""

_PARENT_TEMPLATE = """## Parent Document (this document is an exhibit/attachment to):
Parent file: {parent_file}
Parent type: {parent_type}
Exhibit label for this document: {exhibit_label}

"""


def _build_prompt(doc: dict, case_summary: str, parent: dict | None) -> str:
    parent_block = ""
    if parent:
        parent_block = _PARENT_TEMPLATE.format(
            parent_file   = parent.get("file_name", "unknown"),
            parent_type   = parent.get("document_type", "unknown"),
            exhibit_label = doc.get("exhibit_label") or "unlabeled exhibit",
        )

    content = (doc.get("full_text_md") or "")[:3000].strip() or "(no text available)"

    return _PROMPT_TEMPLATE.format(
        case_summary = case_summary,
        file_name    = doc.get("file_name", "unknown"),
        doc_type     = doc.get("document_type") or "unclassified",
        confidence   = float(doc.get("confidence_score") or 0.5),
        parent_block = parent_block,
        doc_content  = content,
    )


# ── Gemini Flash call ─────────────────────────────────────────────────────────

def _call_gemini(prompt: str) -> dict:
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        print("[07C] WARNING: GOOGLE_API_KEY not set — using fallback classification.")
        return {}

    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model    = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(prompt)
        raw      = response.text.strip()
        # Strip accidental markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
        return json.loads(raw)
    except Exception as e:
        print(f"[07C] WARNING: Gemini call failed ({e}) — using fallback classification.")
        return {}


def _parse_result(raw: dict, doc_type: str) -> ContextClassResult:
    """Parse Gemini JSON into a ContextClassResult, clamping invalid enum values."""
    perspective = raw.get("party_perspective", "unknown")
    if perspective not in VALID_PERSPECTIVES:
        perspective = "unknown"

    temporal = raw.get("temporal_role", "current")
    if temporal not in VALID_TEMPORAL:
        temporal = "current"

    purpose = raw.get("filing_purpose", "evidence")
    if purpose not in VALID_PURPOSES:
        purpose = "evidence"

    should_update  = bool(raw.get("should_update_document_type", False))
    updated_type   = raw.get("updated_document_type") if should_update else None
    # Sanity check: don't update if the value is empty or same as original
    if updated_type and updated_type.strip().lower() == doc_type.strip().lower():
        should_update = False
        updated_type  = None

    try:
        confidence = float(raw.get("confidence", 0.75))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.75

    return ContextClassResult(
        is_primary_filing           = bool(raw.get("is_primary_filing", False)),
        party_perspective           = perspective,
        temporal_role               = temporal,
        filing_purpose              = purpose,
        should_update_document_type = should_update,
        updated_document_type       = updated_type,
        confidence                  = confidence,
        reasoning                   = str(raw.get("reasoning", ""))[:1000],
    )


# ── Fallback when Gemini unavailable ─────────────────────────────────────────

def _fallback_result(doc: dict, is_first_doc: bool) -> ContextClassResult:
    """Rule-based fallback when Gemini is unavailable."""
    doc_type   = doc.get("document_type") or ""
    is_exhibit = bool(doc.get("parent_document_id"))

    if is_exhibit:
        return ContextClassResult(
            is_primary_filing  = False,
            party_perspective  = "unknown",
            temporal_role      = "current",
            filing_purpose     = "evidence",
            confidence         = 0.5,
            reasoning          = "Fallback: document is an exhibit/child — marked as evidence.",
        )

    is_complaint = doc_type.startswith("Pleading - Complaint") or doc_type.startswith("Pleading - Amended")
    return ContextClassResult(
        is_primary_filing  = is_complaint and is_first_doc,
        party_perspective  = "unknown",
        temporal_role      = "current",
        filing_purpose     = "operative_pleading" if is_complaint else "evidence",
        confidence         = 0.4,
        reasoning          = "Fallback: Gemini unavailable, basic rule-based classification applied.",
    )


# ── Write results to Supabase ─────────────────────────────────────────────────

def _update_document(
    sb: Client,
    document_id: str,
    result: ContextClassResult,
    original_doc_type: str,
    case_stage_at_filing: str,
    dry_run: bool,
) -> None:
    payload: dict = {
        "is_primary_filing":               result.is_primary_filing,
        "party_perspective":               result.party_perspective,
        "temporal_role":                   result.temporal_role,
        "filing_purpose":                  result.filing_purpose,
        "case_stage_at_filing":            case_stage_at_filing,
        "context_classification_confidence": result.confidence,
        "context_classification_reasoning":  result.reasoning,
    }

    if result.should_update_document_type and result.updated_document_type:
        payload["original_document_type"] = original_doc_type
        payload["document_type"]           = result.updated_document_type

    if dry_run:
        print(f"[07C] DRY RUN — would update document {document_id} with:")
        for k, v in payload.items():
            print(f"       {k}: {v}")
        return

    sb.table("documents").update(payload).eq("id", document_id).execute()
    print(f"[07C] Document updated (id={document_id})")


def _maybe_advance_stage(
    sb: Client,
    case_id: str,
    case: dict,
    effective_doc_type: str,
    doc_file_name: str,
    dry_run: bool,
) -> None:
    """Advance the case stage if this document implies a later stage."""
    current_stage = case.get("case_stage") or "filing"
    inferred      = _infer_stage(effective_doc_type)

    if not _stage_is_later(inferred, current_stage):
        return

    print(f"[07C] Stage advance: {current_stage} → {inferred} (triggered by '{doc_file_name}')")

    if dry_run:
        print(f"[07C] DRY RUN — would update cases.case_stage to '{inferred}'")
        print(f"[07C] DRY RUN — would insert case_events row for stage_change")
        return

    sb.table("cases").update({"case_stage": inferred}).eq("id", case_id).execute()

    sb.table("case_events").insert({
        "case_id":    case_id,
        "event_type": "stage_change",
        "description": f"Stage advanced from '{current_stage}' to '{inferred}' "
                       f"based on document '{doc_file_name}'",
    }).execute()


def _maybe_set_primary_complaint(
    sb: Client,
    case_id: str,
    document_id: str,
    result: ContextClassResult,
    effective_doc_type: str,
    dry_run: bool,
) -> None:
    """Update cases.primary_complaint_id or primary_document_id when appropriate."""
    if not result.is_primary_filing:
        return

    # Determine which FK to update
    is_complaint = effective_doc_type.startswith("Pleading - Complaint") or \
                   effective_doc_type.startswith("Pleading - Amended Complaint")

    update: dict = {}
    if is_complaint:
        update["primary_complaint_id"] = document_id
    update["primary_document_id"] = document_id

    if dry_run:
        print(f"[07C] DRY RUN — would update cases {update}")
        return

    sb.table("cases").update(update).eq("id", case_id).execute()
    print(f"[07C] Case primary document/complaint updated → {document_id}")


# ── Main ──────────────────────────────────────────────────────────────────────

def classify_document_with_context(document_id: str, dry_run: bool = False) -> ContextClassResult:
    """
    Core callable used when integrating into a pipeline.
    Returns the ContextClassResult for the caller.
    """
    sb  = _get_supabase()

    # 1. Fetch document
    resp = sb.table("documents").select("*").eq("id", document_id).execute()
    if not resp.data:
        print(f"ERROR: Document {document_id} not found in Supabase.")
        sys.exit(1)
    doc = resp.data[0]

    case_id = doc.get("case_id")
    if not case_id:
        print(f"[07C] SKIPPED — document '{doc['file_name']}' has no case_id. "
              "Assign the document to a case first.")
        # Return a minimal result so callers don't crash
        return ContextClassResult(
            is_primary_filing = False,
            party_perspective = "unknown",
            temporal_role     = "current",
            filing_purpose    = "evidence",
            confidence        = 0.0,
            reasoning         = "Skipped: no case_id on document.",
        )

    # 2. Fetch case + existing documents
    case = _fetch_case(sb, case_id)
    if not case:
        print(f"ERROR: Case {case_id} not found in Supabase.")
        sys.exit(1)

    existing_docs = _fetch_case_documents(sb, case_id, exclude_id=document_id)

    # 3. Fetch parent (if exhibit)
    parent = None
    if doc.get("parent_document_id"):
        p_resp = sb.table("documents").select("*").eq("id", doc["parent_document_id"]).execute()
        parent = p_resp.data[0] if p_resp.data else None

    # 4. Build prompt & call Gemini
    case_summary = build_case_summary(case, existing_docs)
    prompt       = _build_prompt(doc, case_summary, parent)

    print(f"[07C] Calling Gemini Flash for '{doc['file_name']}'…")
    raw_result = _call_gemini(prompt)

    original_doc_type = doc.get("document_type") or ""

    if raw_result:
        result = _parse_result(raw_result, original_doc_type)
    else:
        is_first = len(existing_docs) == 0
        result   = _fallback_result(doc, is_first)

    # 5. Post-decision rule: de-duplicate primary
    effective_type = result.updated_document_type or original_doc_type
    if result.is_primary_filing:
        top_category = original_doc_type.split(" - ")[0] if " - " in original_doc_type else original_doc_type
        if _case_has_primary(sb, case_id, top_category):
            print(f"[07C] Case already has a primary '{top_category}' — clearing is_primary_filing")
            result.is_primary_filing = False

    # 6. Persist
    case_stage_at_filing = case.get("case_stage") or "filing"

    _update_document(sb, document_id, result, original_doc_type, case_stage_at_filing, dry_run)
    _maybe_set_primary_complaint(sb, case_id, document_id, result, effective_type, dry_run)
    _maybe_advance_stage(sb, case_id, case, effective_type, doc["file_name"], dry_run)

    print(
        f"[07C] '{doc['file_name']}': "
        f"primary={result.is_primary_filing}, "
        f"perspective={result.party_perspective}, "
        f"temporal={result.temporal_role}, "
        f"purpose={result.filing_purpose}, "
        f"confidence={result.confidence:.0%}"
    )
    if result.should_update_document_type:
        print(f"[07C] Document type updated: '{original_doc_type}' → '{result.updated_document_type}'")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-classify a document using full case context (Gemini Flash)."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--document_id", help="Supabase document UUID")
    group.add_argument("--file_name",   help="Document file_name (stem) in Supabase")
    parser.add_argument(
        "--dry_run", action="store_true",
        help="Print what would be updated without writing to Supabase",
    )
    args = parser.parse_args()

    sb  = _get_supabase()
    doc = _resolve_document(sb, args)

    classify_document_with_context(doc["id"], dry_run=args.dry_run)
    print(f"\nSUCCESS: 07C_case_context_classification.py complete for '{doc['file_name']}'")


if __name__ == "__main__":
    main()
