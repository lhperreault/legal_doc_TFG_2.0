"""
03B_legal_structure_extraction.py — Phase 2, Step 3B: Legal Structure Extraction

Extracts the hierarchical legal structure (Claims → Counts → Elements / Allegations →
Evidence refs) from complaints, briefs, motions, appeals, answers, and counterclaims.

Does NOT run on contracts, discovery materials, court orders, or exhibits unless
the exhibit has filing_purpose ∈ {operative_pleading, motion, brief} (set by 07C).

Tables written:
    claims, counts, legal_elements, allegations, evidence_links

Usage:
    python 03B_legal_structure_extraction.py --document_id <uuid>
    python 03B_legal_structure_extraction.py --document_id <uuid> --dry_run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))


# ── Document-type gate ─────────────────────────────────────────────────────────

_QUALIFYING_PREFIXES = (
    "complaint",
    "brief",
    "motion",
    "appeal",
    "answer",
    "counterclaim",
    "pleading",
)
_QUALIFYING_PURPOSES = {
    "operative_pleading",
    "motion",
    "brief",
}


def _document_qualifies(doc: dict) -> bool:
    doc_type       = (doc.get("document_type")   or "").lower()
    filing_purpose = (doc.get("filing_purpose")  or "").lower()
    return (
        any(doc_type.startswith(p) for p in _QUALIFYING_PREFIXES)
        or filing_purpose in _QUALIFYING_PURPOSES
    )


# ── Semantic label filters ─────────────────────────────────────────────────────

_CAUSES_PREFIXES = (
    "causes_of_action",
    "cause_of_action",
    "claims_for_relief",
    "claim_for_relief",
    "counts",
)
_FACTUAL_LABELS = {
    "factual_allegations",
    "factual_background",
    "factual_history",
    "statement_of_facts",
    "background",
    "purpose_of_affidavit",
    "affidavit_purpose",
}

# Title keywords that identify a Count/Cause-of-Action section even if the
# semantic labeler missed it (e.g. label is None or was set to something generic).
_COUNT_TITLE_KEYWORDS = (
    "count i", "count ii", "count iii", "count iv", "count v",
    "count vi", "count vii", "count viii", "count ix", "count x",
    "count 1", "count 2", "count 3", "count 4", "count 5",
    "cause of action",
    "causes of action",
    "claims for relief",
    "claim for relief",
    "purpose of affidavit",
    "purpose of the affidavit",
)


def _is_causes_section(label: str) -> bool:
    n = (label or "").lower().replace(" ", "_")
    return any(n == p or n.startswith(p) for p in _CAUSES_PREFIXES)


def _is_factual_section(label: str) -> bool:
    return (label or "").lower().replace(" ", "_") in _FACTUAL_LABELS


def _title_looks_like_count(title: str) -> bool:
    """Safety net: catch COUNT/CAUSE-OF-ACTION sections the labeler missed."""
    t = (title or "").lower()
    return any(kw in t for kw in _COUNT_TITLE_KEYWORDS)


# ── Pydantic models ────────────────────────────────────────────────────────────

class LegalElement(BaseModel):
    element_number:  Optional[int]   = None
    element_text:    str
    element_source:  str             = "extracted"  # extracted | inferred_from_schema | needs_schema_inference
    legal_standard:  Optional[str]   = None
    confidence:      float           = 0.8


class Allegation(BaseModel):
    allegation_number:   Optional[int]   = None
    allegation_text:     str
    allegation_type:     str             = "factual"  # factual | legal_conclusion | damages
    supports_element:    Optional[int]   = None       # element_number this allegation supports
    evidence_references: list[str]       = Field(default_factory=list)
    confidence:          float           = 0.8


class Count(BaseModel):
    count_number:   Optional[int]          = None
    count_label:    str
    count_type:     str
    legal_elements: list[LegalElement]     = Field(default_factory=list)
    allegations:    list[Allegation]       = Field(default_factory=list)
    summary:        Optional[str]          = None
    confidence:     float                  = 0.8


class Claim(BaseModel):
    claim_label: Optional[str]       = None
    claim_type:  str
    plaintiff:   Optional[str]       = None
    defendant:   Optional[str]       = None
    counts:      list[Count]         = Field(default_factory=list)
    summary:     Optional[str]       = None
    confidence:  float               = 0.8


class LegalStructureExtraction(BaseModel):
    claims:                 list[Claim]      = Field(default_factory=list)
    standalone_allegations: list[Allegation] = Field(default_factory=list)


# ── Element schemas for inference ─────────────────────────────────────────────

_ELEMENT_SCHEMAS: dict[str, list[str]] = {
    "negligence": [
        "Defendant owed plaintiff a duty of care",
        "Defendant breached that duty",
        "Plaintiff suffered damages",
        "Defendant's breach proximately caused plaintiff's damages",
    ],
    "breach_of_contract": [
        "A valid contract existed between the parties",
        "Plaintiff performed or was excused from performance",
        "Defendant breached the contract",
        "Plaintiff suffered damages as a result",
    ],
    "fraud": [
        "Defendant made a false representation of material fact",
        "Defendant knew the representation was false",
        "Defendant intended to induce plaintiff's reliance",
        "Plaintiff justifiably relied on the representation",
        "Plaintiff suffered damages as a result",
    ],
    "misrepresentation": [
        "Defendant made a false statement of material fact",
        "Defendant knew or should have known the statement was false",
        "Plaintiff reasonably relied on the false statement",
        "Plaintiff suffered damages as a result of the reliance",
    ],
    "unjust_enrichment": [
        "Defendant received a benefit",
        "At plaintiff's expense",
        "Under circumstances making it unjust for defendant to retain the benefit without compensation",
    ],
    "intentional_infliction_of_emotional_distress": [
        "Defendant engaged in extreme and outrageous conduct",
        "Defendant intended to cause or recklessly disregarded causing severe emotional distress",
        "Plaintiff suffered severe emotional distress",
        "Defendant's conduct caused plaintiff's distress",
    ],
    "defamation": [
        "Defendant made a false statement of fact",
        "Defendant published the statement to a third party",
        "Defendant acted with the requisite level of fault",
        "The statement caused harm to plaintiff's reputation",
    ],
    "tortious_interference": [
        "Plaintiff had a valid business relationship or contract",
        "Defendant had knowledge of the relationship",
        "Defendant intentionally and improperly interfered",
        "Plaintiff suffered damages as a result",
    ],
    "antitrust": [
        "Defendant engaged in anticompetitive conduct",
        "The conduct had an actual adverse effect on competition",
        "Plaintiff was harmed as a direct result",
        "Plaintiff suffered antitrust injury",
    ],
    "statutory": [
        "The applicable statute covers the conduct at issue",
        "Defendant violated the statute",
        "Plaintiff is within the class protected by the statute",
        "Plaintiff suffered injury as a result of the violation",
    ],
    "conversion": [
        "Plaintiff owned or had the right to possess the property",
        "Defendant intentionally interfered with plaintiff's property",
        "Plaintiff was damaged by the interference",
    ],
    "breach_of_fiduciary_duty": [
        "A fiduciary relationship existed between plaintiff and defendant",
        "Defendant breached their fiduciary duty",
        "Plaintiff suffered damages as a result of the breach",
    ],
    "civil_rights_violation": [
        "Defendant acted under color of state law",
        "Defendant's conduct deprived plaintiff of a constitutional or federal statutory right",
        "Plaintiff suffered damages as a result",
    ],
}


def _fill_missing_elements(count: Count) -> Count:
    needs_fill = not count.legal_elements or any(
        e.element_source == "needs_schema_inference" for e in count.legal_elements
    )
    if needs_fill:
        schema = _ELEMENT_SCHEMAS.get(count.count_type, [])
        if schema:
            count.legal_elements = [
                LegalElement(
                    element_number=i + 1,
                    element_text=text,
                    element_source="inferred_from_schema",
                    confidence=0.7,
                )
                for i, text in enumerate(schema)
            ]
    return count


# ── Gemini ─────────────────────────────────────────────────────────────────────

_GEMINI_MODEL = "gemini-2.5-flash"
_SCHEMA_HINT  = json.dumps(LegalStructureExtraction.model_json_schema(), indent=2)

_SYSTEM_PROMPT = (
    "You are a legal document analyst specializing in pleading structure. "
    "Extract exactly what is stated — do not infer facts not present. "
    "Return only valid JSON matching the schema provided."
)

_PROMPT_TEMPLATE = """\
## Context
Document type: {document_type}
Party perspective: {party_perspective}
Case: {case_name} (We represent: {our_client} as {party_role})

## Section Being Analyzed
Section title: {section_title}
Semantic label: {semantic_label}
Parent section: {parent_section_title}

Section text:
{section_text}

---

## Your Task

Extract the legal structure from this section following this hierarchy:

  Claim (L2): The broad legal demand
    └─ Count (L3): Specific numbered cause of action
         ├─ Legal Element (L4a): What must be proven
         └─ Allegation (L4b): What is specifically asserted

### Rules:

1. **Claims** group related Counts. If the section has "Count I" without a parent claim
   label, create a Claim with claim_type inferred from the count.

2. **Counts** are the numbered causes of action ("COUNT I", "FIRST CAUSE OF ACTION", etc.)

3. **Legal Elements** — Extract if explicitly stated ("To prevail, Plaintiff must prove:
   (1)... (2)..."). If NOT stated, set element_source: "needs_schema_inference" and
   leave legal_elements empty — they will be filled from standard legal schemas.

4. **Allegations** — Specific factual assertions. Include:
   - Paragraph numbers if present ("42. Defendant knew...")
   - allegation_type: 'factual' (what happened), 'legal_conclusion' (characterization),
     or 'damages' (harm suffered)
   - supports_element: which element_number this supports (null if unclear)
   - evidence_references: extract exhibit/declaration citations ["Exhibit A", "Smith Decl."]

5. **Standalone allegations** — For factual background sections not tied to a specific
   count, put assertions in standalone_allegations (no count/claim linkage needed).

Return ONLY a JSON object matching this schema:
{schema}"""

# Separate, stripped-down prompt for factual/background sections.
# These sections contain supporting facts, not causes of action — the AI must
# NOT invent claims or counts from them.
_FACTUAL_PROMPT_TEMPLATE = """\
## Context
Document type: {document_type}
Case: {case_name}

## Section Being Analyzed
Section title: {section_title}
Semantic label: {semantic_label}

Section text:
{section_text}

---

## Your Task

This is a FACTUAL BACKGROUND section (e.g. "Statement of Facts", "Factual Allegations",
"Background"). It contains supporting facts, not causes of action.

STRICT RULES — you MUST follow these exactly:
- Leave "claims" as an empty list [].
- Leave any claim's "counts" as an empty list [].
- Do NOT create any Claim or Count objects.
- Extract each numbered paragraph or factual assertion as a separate entry in
  "standalone_allegations".
- For each standalone allegation:
    - allegation_number: the paragraph number if present, otherwise null
    - allegation_text: the verbatim or close-paraphrase text of the assertion
    - allegation_type: "factual" (what happened), "legal_conclusion" (characterization),
      or "damages" (harm)
    - evidence_references: any exhibit/declaration citations found ["Exhibit A", etc.]

Return ONLY a JSON object matching this schema:
{schema}"""


def _init_gemini():
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        print("ERROR: GEMINI_API_KEY not set in .env")
        sys.exit(1)
    try:
        from google import genai as _genai
        return _genai.Client(api_key=key)
    except ImportError:
        print("ERROR: google-genai not installed. Run: pip install google-genai")
        sys.exit(1)


def _call_gemini(client, prompt: str) -> dict | None:
    from google.genai import types as _gtypes

    full_prompt = f"{_SYSTEM_PROMPT}\n\n{prompt}"

    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model   =_GEMINI_MODEL,
                contents=full_prompt,
                config  =_gtypes.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            return json.loads(resp.text)
        except Exception as e:
            err    = str(e)
            is_429 = "429" in err or "quota" in err.lower() or "rate" in err.lower()
            if attempt < 2:
                wait = (25 * (attempt + 1)) if is_429 else (2 ** attempt)
                if is_429:
                    print(f"  [03B] Rate-limited — waiting {wait}s…")
                time.sleep(wait)
            else:
                print(f"  [03B] Gemini failed after 3 attempts: {e}", file=sys.stderr)
    return None


# ── Section processing ─────────────────────────────────────────────────────────

_MIN_CHARS = 80


def _process_section(
    client,
    section:    dict,
    parent_title: str,
    doc:        dict,
    case:       dict,
    is_factual: bool = False,
) -> LegalStructureExtraction | None:
    text = section.get("section_text") or ""
    if len(text) < _MIN_CHARS:
        return None

    if is_factual:
        prompt = _FACTUAL_PROMPT_TEMPLATE.format(
            document_type  = doc.get("document_type") or "Unknown",
            case_name      = case.get("case_name") or "Unknown Case",
            section_title  = section.get("section_title") or "(untitled)",
            semantic_label = section.get("semantic_label") or "",
            section_text   = text[:6000],
            schema         = _SCHEMA_HINT,
        )
    else:
        prompt = _PROMPT_TEMPLATE.format(
            document_type        = doc.get("document_type") or "Unknown",
            party_perspective    = doc.get("party_perspective") or "unknown",
            case_name            = case.get("case_name") or "Unknown Case",
            our_client           = case.get("our_client") or "unknown party",
            party_role           = case.get("party_role") or "unknown",
            section_title        = section.get("section_title") or "(untitled)",
            semantic_label       = section.get("semantic_label") or "",
            parent_section_title = parent_title or "(none)",
            section_text         = text[:6000],
            schema               = _SCHEMA_HINT,
        )

    raw = _call_gemini(client, prompt)
    if raw is None:
        return None

    try:
        result = LegalStructureExtraction.model_validate(raw)
    except Exception as exc:
        print(f"  [03B] Validation error in section {section.get('id')}: {exc}", file=sys.stderr)
        return None

    if is_factual:
        # Hard guard: discard any claims/counts the model hallucinated from a
        # factual section. Move allegations buried inside them to standalone.
        for claim in result.claims:
            for count in claim.counts:
                result.standalone_allegations.extend(count.allegations)
        result.claims = []
    else:
        # Post-process: fill missing elements from standard schemas
        for claim in result.claims:
            for i, count in enumerate(claim.counts):
                claim.counts[i] = _fill_missing_elements(count)

    return result


# ── Supabase write ─────────────────────────────────────────────────────────────

def _infer_evidence_type(ref: str) -> str:
    r = ref.lower()
    if "exhibit"     in r: return "exhibit"
    if "decl"        in r: return "declaration"
    if "dep"         in r: return "deposition"
    if "affidavit"   in r: return "declaration"
    return "document"


def _insert_structure(
    result:      LegalStructureExtraction,
    section:     dict,
    document_id: str,
    case_id:     str,
    sb,
    dry_run:     bool,
) -> int:
    section_id = section.get("id")
    page_range = (
        section.get("page_range")
        or (
            f"{section['start_page']}-{section['end_page']}"
            if section.get("start_page") and section.get("end_page")
            else None
        )
    )
    total = 0

    def _insert(table: str, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        if dry_run:
            return [{"id": f"dry-{i}"} for i in range(len(rows))]
        resp = sb.table(table).insert(rows).execute()
        return resp.data or []

    def _strip_none(d: dict) -> dict:
        return {k: v for k, v in d.items() if v is not None}

    # ── Claims ──────────────────────────────────────────────────────────────────
    for claim_model in result.claims:
        claim_rows = _insert("claims", [_strip_none({
            "document_id": document_id,
            "case_id":     case_id,
            "claim_type":  claim_model.claim_type,
            "claim_label": claim_model.claim_label,
            "plaintiff":   claim_model.plaintiff,
            "defendant":   claim_model.defendant,
            "summary":     claim_model.summary,
            "section_id":  section_id,
            "page_range":  page_range,
            "confidence":  claim_model.confidence,
        })])
        if not claim_rows:
            continue
        claim_id = claim_rows[0]["id"]
        total += 1

        # ── Counts ──────────────────────────────────────────────────────────────
        for count_model in claim_model.counts:
            count_rows = _insert("counts", [_strip_none({
                "claim_id":     claim_id,
                "document_id":  document_id,
                "case_id":      case_id,
                "count_number": count_model.count_number,
                "count_label":  count_model.count_label,
                "count_type":   count_model.count_type,
                "summary":      count_model.summary,
                "section_id":   section_id,
                "page_range":   page_range,
                "confidence":   count_model.confidence,
            })])
            if not count_rows:
                continue
            count_id = count_rows[0]["id"]
            total += 1

            # ── Legal elements ───────────────────────────────────────────────────
            el_id_by_number: dict[int, str] = {}
            if count_model.legal_elements:
                el_rows = [_strip_none({
                    "count_id":       count_id,
                    "document_id":    document_id,
                    "element_number": el.element_number,
                    "element_text":   el.element_text,
                    "element_source": el.element_source,
                    "legal_standard": el.legal_standard,
                    "section_id":     section_id,
                    "page_range":     page_range,
                    "confidence":     el.confidence,
                }) for el in count_model.legal_elements]
                inserted_els = _insert("legal_elements", el_rows)
                for i, ins in enumerate(inserted_els):
                    num = count_model.legal_elements[i].element_number
                    if num is not None:
                        el_id_by_number[num] = ins["id"]
                total += len(el_rows)

            # ── Allegations ──────────────────────────────────────────────────────
            if count_model.allegations:
                al_payloads = []
                for al in count_model.allegations:
                    sup_el_id = (
                        el_id_by_number.get(al.supports_element)
                        if al.supports_element is not None
                        else None
                    )
                    al_payloads.append(_strip_none({
                        "count_id":              count_id,
                        "claim_id":              claim_id,
                        "document_id":           document_id,
                        "allegation_number":     al.allegation_number,
                        "allegation_text":       al.allegation_text,
                        "allegation_type":       al.allegation_type,
                        "supporting_element_id": sup_el_id,
                        "section_id":            section_id,
                        "page_range":            page_range,
                        "confidence":            al.confidence,
                    }))
                inserted_als = _insert("allegations", al_payloads)
                total += len(al_payloads)

                # ── Evidence links ─────────────────────────────────────────────
                ev_rows = []
                for i, ins in enumerate(inserted_als):
                    al_id  = ins["id"]
                    al_obj = count_model.allegations[i]
                    for ref in al_obj.evidence_references:
                        ev_rows.append(_strip_none({
                            "document_id":        document_id,
                            "allegation_id":      al_id,
                            "count_id":           count_id,
                            "evidence_reference": ref,
                            "evidence_type":      _infer_evidence_type(ref),
                            "section_id":         section_id,
                            "page_range":         page_range,
                        }))
                if ev_rows:
                    _insert("evidence_links", ev_rows)
                    total += len(ev_rows)

    # ── Standalone allegations ─────────────────────────────────────────────────
    if result.standalone_allegations:
        sa_rows = [_strip_none({
            "document_id":       document_id,
            "allegation_number": al.allegation_number,
            "allegation_text":   al.allegation_text,
            "allegation_type":   al.allegation_type,
            "section_id":        section_id,
            "page_range":        page_range,
            "confidence":        al.confidence,
        }) for al in result.standalone_allegations]
        _insert("allegations", sa_rows)
        total += len(sa_rows)

    return total


# ── Main ───────────────────────────────────────────────────────────────────────

def extract_legal_structure(document_id: str, dry_run: bool = False) -> int:
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)
    sb = create_client(url, key)

    # Fetch document
    doc_resp = sb.table("documents").select("*").eq("id", document_id).single().execute()
    doc = doc_resp.data
    if not doc:
        print(f"ERROR: Document {document_id} not found")
        sys.exit(1)

    case_id = doc.get("case_id")
    if not case_id:
        print(f"[03B] SKIP — no case_id on document {document_id}")
        return 0

    if not _document_qualifies(doc):
        print(
            f"[03B] SKIP — document type '{doc.get('document_type', 'unknown')}' "
            f"(filing_purpose='{doc.get('filing_purpose', '')}') not eligible"
        )
        return 0

    # Fetch case context
    case_resp = sb.table("cases").select("*").eq("id", case_id).single().execute()
    case      = case_resp.data or {}

    print(f"[03B] Legal structure extraction: {doc.get('file_name', document_id)}")
    print(f"      Type: {doc.get('document_type')} | Case: {case.get('case_name', 'Unknown')}")

    # Clear prior results (reverse FK order)
    if not dry_run:
        sb.table("evidence_links").delete().eq("document_id", document_id).execute()
        sb.table("allegations").delete().eq("document_id", document_id).execute()
        sb.table("legal_elements").delete().eq("document_id", document_id).execute()
        sb.table("counts").delete().eq("document_id", document_id).execute()
        sb.table("claims").delete().eq("document_id", document_id).execute()

    # Fetch sections
    sections_resp = sb.table("sections").select(
        "id, section_title, semantic_label, section_text, start_page, end_page, page_range, parent_section_id, level"
    ).eq("document_id", document_id).order("start_page", desc=False).execute()
    sections = sections_resp.data or []

    if not sections:
        print(f"[03B] No sections found")
        return 0

    id_to_title = {s["id"]: s.get("section_title") or "" for s in sections}

    # Filter to qualifying sections, tagging factual ones separately so they
    # get the stripped-down prompt and cannot produce claims/counts.
    targets: list[tuple[dict, str, bool]] = []
    for s in sections:
        label      = s.get("semantic_label") or ""
        parent_id  = s.get("parent_section_id")
        parent_ttl = id_to_title.get(parent_id, "") if parent_id else ""
        title      = s.get("section_title") or ""
        if _is_factual_section(label):
            targets.append((s, parent_ttl, True))   # factual — standalone allegations only
        elif _is_causes_section(label) or _title_looks_like_count(title):
            targets.append((s, parent_ttl, False))  # causes-of-action — full extraction

    if not targets:
        available = sorted({s.get("semantic_label") for s in sections if s.get("semantic_label")})
        print(f"[03B] No qualifying sections found. Available labels: {available}")
        return 0

    factual_count = sum(1 for _, _, f in targets if f)
    claims_count  = len(targets) - factual_count
    print(f"[03B] Processing {len(targets)} qualifying section(s) "
          f"({claims_count} claims, {factual_count} factual)…")

    client = _init_gemini()
    total  = 0

    def _process_one(item: tuple[dict, str, bool]) -> tuple[dict, int]:
        section, parent_title, is_factual = item
        result = _process_section(client, section, parent_title, doc, case, is_factual=is_factual)
        if result is None:
            return section, 0
        n = _insert_structure(result, section, document_id, case_id, sb, dry_run)
        return section, n

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_process_one, item): item for item in targets}
        for future in as_completed(futures):
            try:
                section, n = future.result()
                label = section.get("semantic_label", "")
                title = (section.get("section_title") or "?")[:50]
                print(f"  ✓ [{label}] {title!r}: {n} rows")
                total += n
            except Exception as exc:
                item    = futures[future]
                section = item[0]  # item is (section, parent_title, is_factual)
                print(f"  ✗ {section.get('section_title', '?')}: {exc}", file=sys.stderr)

    tag = "DRY RUN" if dry_run else "SUCCESS"
    print(f"[03B] {tag} — {total} rows inserted for document {document_id}")
    return total


def main():
    parser = argparse.ArgumentParser(description="03B: Legal structure extraction")
    parser.add_argument("--document_id", required=True)
    parser.add_argument("--dry_run", action="store_true")
    args = parser.parse_args()
    extract_legal_structure(args.document_id, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
