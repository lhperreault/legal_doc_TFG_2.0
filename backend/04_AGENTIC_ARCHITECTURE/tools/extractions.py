"""
tools/extractions.py — query_extractions tool

Direct Supabase SQL queries on the extractions table.
case_id is injected at runtime via LangChain RunnableConfig.
"""

import os

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '..', '.env'))

VALID_EXTRACTION_TYPES = {
    "party", "date", "amount", "obligation", "claim",
    "condition", "evidence_ref", "case_citation",
}


def _get_supabase():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise EnvironmentError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set")
    return create_client(url, key)


@tool
def query_extractions(
    extraction_type: str,
    entity_name_contains: str | None = None,
    document_type: str | None = None,
    config: RunnableConfig = None,
) -> str:
    """Look up specific extracted entities from case documents using structured queries.

    Use this for precise lookups like 'find all parties', 'find all dates',
    or 'find obligations in the Developer Agreement'. Faster and more precise
    than search_sections for known entity types.

    Args:
        extraction_type: One of: party, date, amount, obligation, claim,
                         condition, evidence_ref, case_citation.
        entity_name_contains: Optional substring filter on entity name
                              (e.g. "Apple", "30%", "breach").
        document_type: Optional filter on document type
                       (e.g. "Pleading - Complaint", "Contract - License Agreement").
    """
    case_id = (config or {}).get("configurable", {}).get("case_id", "")
    if not case_id:
        return "ERROR: case_id not found in config."

    if extraction_type not in VALID_EXTRACTION_TYPES:
        valid = ", ".join(sorted(VALID_EXTRACTION_TYPES))
        return f"ERROR: Invalid extraction_type '{extraction_type}'. Valid types: {valid}"

    try:
        sb = _get_supabase()
    except Exception as e:
        return f"ERROR: Could not connect to database — {e}"

    # Get document IDs for this case
    docs_resp = (
        sb.table("documents")
        .select("id, file_name, document_type")
        .eq("case_id", case_id)
        .execute()
    )
    docs = docs_resp.data or []
    if not docs:
        return f"No documents found for case {case_id}."

    doc_map = {d["id"]: d for d in docs}

    # Apply document_type filter
    if document_type:
        filtered_ids = [
            d["id"] for d in docs
            if document_type.lower() in (d.get("document_type") or "").lower()
        ]
        if not filtered_ids:
            return f"No documents of type '{document_type}' found in this case."
    else:
        filtered_ids = list(doc_map.keys())

    # Get sections for filtered docs
    sections_resp = (
        sb.table("sections")
        .select("id, document_id")
        .in_("document_id", filtered_ids)
        .execute()
    )
    section_ids = [s["id"] for s in (sections_resp.data or [])]
    sec_to_doc  = {s["id"]: s["document_id"] for s in (sections_resp.data or [])}

    if not section_ids:
        return "No sections found matching the filters."

    # Query extractions
    ext_query = (
        sb.table("extractions")
        .select("id, section_id, extraction_type, entity_name, properties, confidence")
        .eq("extraction_type", extraction_type)
        .in_("section_id", section_ids[:500])  # batch limit
    )
    if entity_name_contains:
        ext_query = ext_query.ilike("entity_name", f"%{entity_name_contains}%")

    ext_resp = ext_query.limit(50).execute()
    extractions = ext_resp.data or []

    if not extractions:
        filter_desc = f"'{entity_name_contains}'" if entity_name_contains else "any"
        return f"No {extraction_type} extractions found matching {filter_desc}."

    lines = [f"{extraction_type.upper()} Extractions — {len(extractions)} result(s)\n"]
    for ex in extractions:
        doc_id   = sec_to_doc.get(ex.get("section_id", ""), "")
        doc      = doc_map.get(doc_id, {})
        fname    = doc.get("file_name", "unknown")
        dtype    = doc.get("document_type", "")
        name     = ex.get("entity_name") or "(unnamed)"
        props    = ex.get("properties") or {}
        conf     = ex.get("confidence", 0)

        lines.append(f"• {name}")
        lines.append(f"  Source: {fname} [{dtype}]")
        lines.append(f"  Confidence: {conf:.2f}")

        # Show relevant properties by type
        if extraction_type == "party":
            role   = props.get("role") or props.get("entity_type") or ""
            juris  = props.get("jurisdiction") or ""
            if role:
                lines.append(f"  Role: {role}")
            if juris:
                lines.append(f"  Jurisdiction: {juris}")
        elif extraction_type == "claim":
            ctype = props.get("claim_type") or props.get("cause_of_action") or ""
            if ctype:
                lines.append(f"  Claim type: {ctype}")
        elif extraction_type == "obligation":
            party = props.get("obligated_party") or ""
            if party:
                lines.append(f"  Obligated party: {party}")
        elif extraction_type == "amount":
            currency = props.get("currency") or ""
            context  = props.get("context") or ""
            if currency:
                lines.append(f"  Currency: {currency}")
            if context:
                lines.append(f"  Context: {context[:100]}")
        elif extraction_type == "date":
            dtype_d  = props.get("date_type") or ""
            if dtype_d:
                lines.append(f"  Date type: {dtype_d}")
        elif extraction_type == "evidence_ref":
            ref_label = props.get("reference_label") or props.get("description") or ""
            if ref_label:
                lines.append(f"  Reference: {ref_label}")

        lines.append("")

    return "\n".join(lines)
