"""Document classification module: classifies legal documents into predefined document types.

This module handles:
1. Document type classification (from 180+ legal document types)
2. Exhibit detection and labeling
3. Confidence scoring
4. Multi-label classification support
5. GPT-4o-mini powered intelligent classification

Input: Canonical blocks (from canonicalization step)
Output: DocumentClassificationResult (document_type, confidence, exhibit_references, is_exhibit)
"""
from __future__ import annotations

import os
import sys
import json
import re
from typing import Any, List, Optional

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field
import pandas as pd

# Load environment variables
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.env'))

# CLI entrypoint for pipeline integration
def main():
    if len(sys.argv) != 2:
        print('Usage: python 05_doc_classification.py <text_extraction_md>')
        sys.exit(1)
        
    text_path = sys.argv[1]
    
    # Read extracted text
    with open(text_path, 'r', encoding='utf-8') as f:
        document_text = f.read()
        
    # Minimal canonical block for compatibility
    canonical_blocks = [{"text": document_text}]
    
    # Save to temp canonical file
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    temp_chunks_dir = os.path.join(backend_dir, 'zz_temp_chunks')
    os.makedirs(temp_chunks_dir, exist_ok=True)
    canonical_path = os.path.join(temp_chunks_dir, os.path.splitext(os.path.basename(text_path))[0] + '_canonical.json')
    
    with open(canonical_path, 'w', encoding='utf-8') as f:
        json.dump(canonical_blocks, f)
        
    # Run classification
    result = classify_document(canonical_path)
    
    # Save result as JSON
    out_json = os.path.join(temp_chunks_dir, os.path.splitext(os.path.basename(text_path))[0] + '_classification.json')
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2)

    # Save result as CSV
    out_csv = os.path.join(temp_chunks_dir, os.path.splitext(os.path.basename(text_path))[0] + '_classification.csv')
    pd.DataFrame([result]).to_csv(out_csv, index=False)

    # Log class and full outcome
    doc_class = result.get('document_type', 'UNKNOWN')
    print(f"SUCCESS: 05_doc_classification.py ran successfully. Document class: {doc_class}. Output written to: {out_json} and {out_csv}")
    print("Classification outcome:")
    print(pd.DataFrame([result]).to_string(index=False))

# ============================================================================
# COMPREHENSIVE LABEL SET
# ===========================================================================

LEGAL_DOCUMENT_LABELS = [
    # ============ PLEADINGS ============
    "Pleading - Complaint",
    "Pleading - Criminal Complaint",
    "Pleading - Amended Complaint",
    "Pleading - Answer",
    "Pleading - Counterclaim",
    "Pleading - Crossclaim",
    "Pleading - Third Party Complaint",
    "Pleading - Reply",
    "Pleading - Motion",
    "Pleading - Motion to Dismiss",
    "Pleading - Motion for Summary Judgment",
    "Pleading - Motion to Compel",
    "Pleading - Brief",
    "Pleading - Appeal Brief",
    "Pleading - Opposition Brief",
    "Pleading - Reply Brief",
    "Pleading - Order",
    "Pleading - Judgment",
    "Pleading - Court Opinion",
    "Pleading - Settlement Agreement",
    "Pleading - Consent Decree",
    "Pleading - Declaration",
    "Pleading - Affidavit",
    "Pleading - Sworn Statement",
    "Exhibit - ",
    
    # ============ DISCOVERY ============
    "Discovery - Interrogatories",
    "Discovery - Request for Production (RFP)",
    "Discovery - Request for Admission (RFA)",
    "Discovery - Deposition Transcript",
    "Discovery - Expert Report",
    "Discovery - Expert Disclosure",
    "Discovery - Subpoena",
    "Discovery - Privilege Log",
    "Discovery - Discovery Response",
    "Discovery - Discovery Objection",
    "Discovery - Document Production",
    "Discovery - ESI Production",
    "Discovery - Deposition Notice",
    "Discovery - Deposition Exhibit",
    "Discovery - Deposition Summary",
    "Discovery - Deposition Errata Sheet",
    "Discovery - Deposition Subpoena",
    "Discovery - Notice of Deposition",
    "Discovery - Expert CV",
    "Discovery - Expert Engagement Letter",
    "Discovery - Expert Invoice",
    "Discovery - Expert Deposition Transcript",
    "Discovery - Redacted Document",
    "Discovery - Privilege Assertion",
    "Discovery - Clawback Notice",
    
    # ============ CONTRACTS ============
    "Contract - Agreement",
    "Contract - Amendment",
    "Contract - Addendum",
    "Contract - NDA",
    "Contract - Employment Agreement",
    "Contract - Independent Contractor Agreement",
    "Contract - Vendor Agreement",
    "Contract - Customer Agreement",
    "Contract - License Agreement",
    "Contract - Content License Agreement",
    "Contract - Lease Agreement",
    "Contract - Partnership Agreement",
    "Contract - Shareholder Agreement",
    "Contract - Purchase Agreement",
    "Contract - Merger Agreement",
    "Contract - Settlement Agreement",
    "Contract - Statement of Work",
    "Contract - Terms of Service",
    "Contract - Service Level Agreement",
    "Contract - Release Agreement",
    "Contract - Settlement Release",
    "Contract - Asset Purchase Agreement",
    "Contract - Stock Purchase Agreement",
    "Contract - Licensing Amendment",
    "Contract - Distribution Agreement",
    "Contract - Franchise Agreement",
    "Contract - Joint Venture Agreement",
    "Contract - Commercial Agreement",
    "Contract - Insurance Policy",
    "Contract - Insurance Agreement",
    
    # ============ COMMUNICATIONS ============
    "Communication - Insurance Correspondence",
    "Communication - Email",
    "Communication - Email Thread",
    "Communication - Letter",
    "Communication - Demand Letter",
    "Communication - Notice",
    "Communication - Internal Memo",
    "Communication - Instant Message",
    "Communication - Text Message",
    "Communication - Slack Message",
    "Communication - Teams Message",
    "Communication - Fax",
    
    # ============ FINANCIAL ============
    "Financial - Financial Statement",
    "Financial - Balance Sheet",
    "Financial - Income Statement",
    "Financial - Cash Flow Statement",
    "Financial - Invoice",
    "Financial - Payment Record",
    "Financial - Accounting Ledger",
    "Financial - Audit Report",
    "Financial - Tax Return",
    "Financial - Bank Statement",
    "Financial - Expense Report",
    "Financial - SEC Filing",
    "Financial - 10-K",
    "Financial - 10-Q",
    "Financial - 8-K",
    "Financial - Insurance Claim",
    
    # ============ CORPORATE ============
    "Corporate - Board Minutes",
    "Corporate - Board Resolution",
    "Corporate - Corporate Charter",
    "Corporate - Bylaws",
    "Corporate - Articles of Incorporation",
    "Corporate - Shareholder Minutes",
    "Corporate - Shareholder Resolution",
    "Corporate - Corporate Policy",
    "Corporate - Organizational Chart",
    "Corporate - Internal Investigation Report",
    "Corporate - Compliance Report",
    "Corporate - Risk Assessment Report",
    
    # ============ EVIDENCE ============
    "Evidence - Photograph",
    "Evidence - Screenshot",
    "Evidence - Recording",
    "Evidence - Audio Recording",
    "Evidence - Video Recording",
    "Evidence - System Log",
    "Evidence - Database Export",
    "Evidence - Spreadsheet",
    "Evidence - Presentation",
    "Evidence - Report",
    "Evidence - Timeline",
    "Evidence - Contract Exhibit",
    "Evidence - Email Exhibit",
    "Evidence - Financial Exhibit",
    "Evidence - Deposition Exhibit",
    "Evidence - Trial Exhibit",
    "Evidence - Demonstrative Exhibit",
    "Evidence - Declaration",
    "Evidence - Affidavit",
    
    # ============ REGULATORY ============
    "Regulatory - Statute",
    "Regulatory - Regulation",
    "Regulatory - Case Law",
    "Regulatory - Court Opinion",
    "Regulatory - Agency Notice",
    "Regulatory - Compliance Filing",
    "Regulatory - Investigation Notice",
    "Regulatory - Enforcement Action",
    "Regulatory - Subpoena",
    "Regulatory - Civil Investigative Demand (CID)",
    "Regulatory - Government Inquiry",
    "Regulatory - Regulatory Complaint",
    
    # ============ COURT ============
    "Court - Docket Entry",
    "Court - Hearing Notice",
    "Court - Scheduling Order",
    "Court - Trial Transcript",
    "Court - Jury Instruction",
    "Court - Court Minutes",
    "Court - Filing Receipt",
    "Court - Service of Process",
    "Court - Notice of Filing",
    "Court - Notice of Motion",
    "Court - Notice of Appearance",
    "Court - Notice of Removal",
    "Court - Notice of Hearing",
    "Court - Notice of Entry of Judgment",
    "Court - Proof of Service",
    "Court - Certificate of Service",
    "Court - Settlement Order",
    "Court - Stipulation",
    "Court - Stipulation and Order",
    "Court - Case Management Order",
    "Court - Case Management Conference Notice",
    "Court - Pretrial Order",
    "Court - Trial Order",
    "Court - Minute Order",
    "Court - Clerk Notice",
    "Court - Arbitration Demand",
    "Court - Arbitration Award",
    "Court - Arbitration Order",
    "Court - Arbitration Agreement",
    
    # ============ ADMINISTRATIVE ============
    "Administrative - Intake Form",
    "Administrative - Case Summary",
    "Administrative - Conflict Check Report",
    "Administrative - Case Strategy Memo",
    "Administrative - Attorney Notes",
]


def _extract_exhibit_references(text_parts: List[str]) -> List[str]:
    """Extract explicit exhibit references from document text.
    
    Looks for patterns like:
    - Exhibit A, Exhibit 1, Exhibit I
    - Attached as Exhibit B
    - Marked Exhibit C
    """
    all_text = " ".join(text_parts).upper()
    
    patterns = [
        r"EXHIBIT\s+([A-Z0-9]+)\b",  # "Exhibit A", "Exhibit 1", "Exhibit I"
        r"ATTACHED\s+(?:AS\s+)?EXHIBIT\s+([A-Z0-9]+)",  # "Attached as Exhibit"
        r"(?:MARKED|IDENTIFIED)\s+(?:AS\s+)?EXHIBIT\s+([A-Z0-9]+)",  # "Marked Exhibit"
        r"(?:SCHEDULE|APPENDIX)\s+([A-Z0-9]+)",  # "Schedule A" as exhibit
    ]
    
    exhibits = set()
    for pattern in patterns:
        matches = re.findall(pattern, all_text)
        for match in matches:
            exhibits.add(f"Exhibit {match}")
    
    return sorted(list(exhibits))


def _is_standalone_exhibit(text_parts: List[str]) -> bool:
    """Determine if document is a standalone exhibit/attachment.
    
    Checks for:
    - "Exhibit" in header/title
    - "Attached Document"
    - Single chart/image/table without surrounding narrative
    - Minimal text (< 100 words) with structured data
    """
    all_text = " ".join(text_parts).lower()
    
    # Check for explicit exhibit markers
    if re.search(r"^\s*(?:exhibit|attachment|schedule|appendix|annex)", all_text[:500]):
        return True
    
    # Check for "attached document" / "attached hereto"
    if re.search(r"(?:attached|appended|annexed)(?:\s+hereto|as|document)", all_text):
        return True
    
    # If mostly structured data (tables, lists, minimal prose)
    word_count = len(all_text.split())
    table_indicators = all_text.count("|") + all_text.count("---")
    
    if word_count < 50 and table_indicators > 3:
        return True
    
    return False


class ClassificationResponse(BaseModel):
    selected_label: str = Field(description="MUST be one of the provided LEGAL_DOCUMENT_LABELS.")
    confidence: float = Field(description="Confidence score between 0.0 and 1.0.")
    is_exhibit_or_attachment: bool
    reasoning: str = Field(description="1-2 sentence explanation for this classification.")
    alternative_labels: List[str] = Field(
        default_factory=list,
        description="Other possible labels if confidence is low.",
    )


def _classify_with_gpt(
    document_text: str,
    is_exhibit: bool,
    exhibit_refs: List[str],
    max_tokens: int = 1024
) -> dict:
    """Use GPT-4o-mini to classify document into legal document types.
    
    Returns dict with:
    - primary_type: single best match from LEGAL_DOCUMENT_LABELS
    - confidence: float 0.0-1.0
    - reasoning: brief explanation
    - alternative_types: list of other possible types (if applicable)
    """
    client = OpenAI()
    
    # Build comprehensive prompt
    labels_str = json.dumps(LEGAL_DOCUMENT_LABELS, indent=2)
    
    system_prompt = """You are an expert legal document classifier. Your task is to classify legal documents into specific document types.

You have a predefined set of 180+ document type labels. Your job is to match the provided document to the SINGLE MOST APPROPRIATE label.

IMPORTANT RULES:
1. You MUST select EXACTLY ONE label from the provided list. It must only be from the list. 
2. Choose the most specific label (e.g., "Pleading - Motion to Dismiss" instead of "Pleading - Motion")
3. If it's an exhibit (it will say it in the first page typically), determine what TYPE of exhibit/attachment it is
4. Consider the document's:
   - Structural indicators (headers, footers, signatures)
   - Content patterns (legal language, procedural markers)
   - Metadata clues (dates, parties, jurisdictions)
   - Purpose and function in litigation

EXHIBIT DETECTION:
- If standalone exhibit/attachment: classify by its CONTENT TYPE
- Examples:
  - "Exhibit A - Financial Statement" → classify as "Financial - Financial Statement"
  - "Exhibit B - Email Thread" → classify as "Communication - Email Thread"
  - "Exhibit C - Expert Report" → classify as "Discovery - Expert Report"
"""
    
    user_prompt = f"""Classify this legal document:

AVAILABLE LABELS:
{labels_str}

DOCUMENT CONTENT:
{document_text[:10000]}

Is this an exhibit/attachment: {is_exhibit}
Exhibit references found: {exhibit_refs}

Return the best classification now."""
    
    response = client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
        response_format=ClassificationResponse,
    )

    parsed = response.choices[0].message.parsed
    return parsed.model_dump() if parsed else {
        "selected_label": "Administrative - Case Summary",
        "confidence": 0.5,
        "is_exhibit_or_attachment": is_exhibit,
        "reasoning": "Fallback due to empty parse.",
        "alternative_labels": [],
    }


def _validate_label(label: str) -> bool:
    """Ensure selected label is in our approved list."""
    return label in LEGAL_DOCUMENT_LABELS


# ============================================================================
# Task 6: classify_document (Refactored for local standalone use)
# ============================================================================

def classify_document(canonical_payload_path: str) -> dict:
    """Classify a legal document into a specific document type."""
    
    # 1. Load canonicalized blocks using standard JSON
    try:
        with open(canonical_payload_path, 'r', encoding='utf-8') as f:
            canonical_blocks = json.load(f)
    except Exception as e:
        print(f"[CLASSIFICATION] Error loading JSON: {e}")
        canonical_blocks = []

    if not canonical_blocks:
        print("[CLASSIFICATION] No blocks found; returning UNKNOWN")
        return {
            "document_type": "Administrative - Case Summary",
            "confidence_score": 0.0,
            "exhibit_references": [],
            "is_exhibit": False
        }
    
    # 2. Reconstruct document text
    text_parts: List[str] = []
    for block in canonical_blocks:
        payload = block.get("text", "")
        if isinstance(payload, str):
            text_parts.append(payload)
        elif isinstance(payload, list):
            text_parts.append(str(payload))

    reconstructed_text = "\n\n".join(part for part in text_parts if part)

    if not reconstructed_text:
        print("[CLASSIFICATION] Empty document; returning UNKNOWN")
        return {
            "document_type": "Administrative - Case Summary",
            "confidence_score": 0.0,
            "exhibit_references": [],
            "is_exhibit": False
        }

    # 3. Detect if this is an exhibit/attachment
    is_exhibit = _is_standalone_exhibit(text_parts)

    # 4. First pass: classify without exhibit references
    print(f"[CLASSIFICATION] Classifying document with GPT-4o-mini (no exhibit refs)...")
    classification_result = _classify_with_gpt(
        reconstructed_text,
        is_exhibit=is_exhibit,
        exhibit_refs=[],
    )

    selected_label = classification_result.get("selected_label", "Administrative - Case Summary")
    confidence = float(classification_result.get("confidence", 0.5))
    is_exhibit_detected = classification_result.get("is_exhibit_or_attachment", is_exhibit)
    reasoning = classification_result.get("reasoning", "")

    # 5. For document types that commonly attach exhibits, extract references and re-classify
    _EXHIBIT_BEARING_TYPES = {
        "Pleading - Complaint",
        "Pleading - Criminal Complaint",
        "Pleading - Amended Complaint",
        "Pleading - Third Party Complaint",
        "Pleading - Motion",
        "Pleading - Motion to Dismiss",
        "Pleading - Motion for Summary Judgment",
        "Pleading - Motion to Compel",
        "Pleading - Brief",
        "Pleading - Appeal Brief",
        "Pleading - Opposition Brief",
        "Pleading - Reply Brief",
        "Pleading - Declaration",
        "Pleading - Affidavit",
    }
    exhibit_references = []
    if selected_label in _EXHIBIT_BEARING_TYPES:
        exhibit_seen = set()
        for payload in text_parts:
            for ref in _extract_exhibit_references([payload]):
                if ref not in exhibit_seen:
                    exhibit_seen.add(ref)
                    exhibit_references.append(ref)
        print(f"[CLASSIFICATION] '{selected_label}' detected — extracting exhibit references and re-classifying...")
        classification_result = _classify_with_gpt(
            reconstructed_text,
            is_exhibit=is_exhibit,
            exhibit_refs=exhibit_references,
        )
        selected_label = classification_result.get("selected_label", selected_label)
        confidence = float(classification_result.get("confidence", confidence))
        is_exhibit_detected = classification_result.get("is_exhibit_or_attachment", is_exhibit_detected)
        reasoning = classification_result.get("reasoning", reasoning)

    if not _validate_label(selected_label):
        print(f"[CLASSIFICATION] WARNING: Invalid label '{selected_label}'; using fallback")
        selected_label = "Administrative - Case Summary"
        confidence = 0.4

    print(
        f"[PIPELINE] classify_document executed | "
        f"document_type='{selected_label}' | "
        f"confidence={confidence:.2f} | "
        f"is_exhibit={is_exhibit_detected} | "
        f"exhibits={len(exhibit_references)} | "
        f"reasoning='{reasoning}'"
    )

    # 6. Return a standard Python dictionary
    return {
        "document_type": selected_label,
        "confidence_score": round(confidence, 4),
        "exhibit_references": exhibit_references,
        "is_exhibit": is_exhibit_detected
    }

if __name__ == '__main__':
    main()


