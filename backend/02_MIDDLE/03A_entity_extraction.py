"""
03A_entity_extraction.py — Phase 2, Step 3A: Simplified Universal Entity Extraction

Extracts the 7 "structural" entity types from EVERY section of EVERY document type.
Uses a single unified Gemini Flash prompt per section — no template routing.

Entity types extracted:
  party            — people and companies (with role: plaintiff/defendant/attorney/judge/etc.)
  date             — dates and deadlines
  monetary_amount  — dollar figures and financial values
  court            — court names and types
  judge            — judge names and titles
  attorney         — individual attorneys
  law_firm         — law firm names

Deliberately excludes claims, obligations, conditions — those are in 03B.

LexNLP runs as a free pre-pass for dates and monetary amounts, providing hints
that the LLM can validate and enrich.

Usage:
    python 03A_entity_extraction.py --document_id <uuid>
    python 03A_entity_extraction.py --file_name "complaint"
    python 03A_entity_extraction.py --document_id <uuid> --dry_run
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError
from supabase import create_client, Client

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))


# ── LexNLP — optional, graceful degradation ──────────────────────────────────

_LEXNLP_OK = False
try:
    import lexnlp.extract.en.dates   as _lex_dates
    import lexnlp.extract.en.money   as _lex_money
    import lexnlp.extract.en.amounts as _lex_amounts
    _LEXNLP_OK = True
except ImportError:
    pass


# ── Pydantic models (matching the spec) ──────────────────────────────────────

class PartyEntity(BaseModel):
    name:             str
    entity_type:      str = "unknown"   # person | corporation | llc | government | unknown
    role_in_document: str = "mentioned" # plaintiff | defendant | witness | mentioned | attorney | judge
    raw_text:         Optional[str] = None
    confidence:       float = Field(default=0.8, ge=0.0, le=1.0)

class DateEntity(BaseModel):
    description: str
    date_value:  str                    # ISO date or relative expression
    date_type:   str = "event"          # event | deadline | effective | filed | hearing
    raw_text:    Optional[str] = None
    confidence:  float = Field(default=0.8, ge=0.0, le=1.0)

class MonetaryEntity(BaseModel):
    description: str
    amount:      str                    # "500000" or "10% of revenue"
    currency:    str = "USD"
    context:     str = "other"          # damages_sought | contract_value | settlement | fee | other
    raw_text:    Optional[str] = None
    confidence:  float = Field(default=0.8, ge=0.0, le=1.0)

class CourtEntity(BaseModel):
    name:         str
    court_type:   str = "unknown"       # federal_district | federal_circuit | state_trial | state_appellate | supreme
    jurisdiction: Optional[str] = None  # "California", "9th Circuit"
    raw_text:     Optional[str] = None
    confidence:   float = Field(default=0.8, ge=0.0, le=1.0)

class JudgeEntity(BaseModel):
    name:      str
    title:     Optional[str] = None     # "Chief Judge", "Magistrate Judge"
    court:     Optional[str] = None
    raw_text:  Optional[str] = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)

class AttorneyEntity(BaseModel):
    name:         str
    bar_number:   Optional[str] = None
    firm:         Optional[str] = None
    representing: Optional[str] = None  # Party name they represent
    raw_text:     Optional[str] = None
    confidence:   float = Field(default=0.8, ge=0.0, le=1.0)

class LawFirmEntity(BaseModel):
    name:         str
    representing: Optional[str] = None
    raw_text:     Optional[str] = None
    confidence:   float = Field(default=0.8, ge=0.0, le=1.0)


class SectionExtractionResult(BaseModel):
    parties:          list[PartyEntity]    = Field(default_factory=list)
    dates:            list[DateEntity]     = Field(default_factory=list)
    monetary_amounts: list[MonetaryEntity] = Field(default_factory=list)
    courts:           list[CourtEntity]    = Field(default_factory=list)
    judges:           list[JudgeEntity]    = Field(default_factory=list)
    attorneys:        list[AttorneyEntity] = Field(default_factory=list)
    law_firms:        list[LawFirmEntity]  = Field(default_factory=list)


# ── Prompt ────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a legal document entity extractor. "
    "Extract exactly what is stated — do not infer or hallucinate. "
    "Return only valid JSON matching the schema provided. "
    "Use empty arrays when nothing is found for a category."
)

_SCHEMA_HINT = json.dumps(SectionExtractionResult.model_json_schema(), indent=2)

_USER_TEMPLATE = """Document type: {document_type}
Section title: {section_title}
Section semantic label: {semantic_label}
{lexnlp_block}
Section text:
{section_text}

---

Extract all of the following from the section text above:
- parties: people and companies mentioned (with their role: plaintiff / defendant / witness / attorney / judge / mentioned)
- dates: all dates and deadlines (ISO date where possible, or relative expression)
- monetary_amounts: dollar figures, settlements, fees, damages
- courts: court names and types
- judges: judge names and titles
- attorneys: individual attorney names
- law_firms: law firm names

Rules:
- Only extract what is explicitly stated in the text
- Set confidence < 0.5 for anything uncertain (it will be filtered out)
- role_in_document for parties: infer from context ("Plaintiff John Smith" → plaintiff)
- Return empty arrays for categories with nothing found

Return ONLY a JSON object matching this schema:
{schema}"""


def _build_prompt(
    section_title:  str,
    document_type:  str,
    section_text:   str,
    lexnlp_block:   str,
    semantic_label: str = "",
) -> str:
    return _USER_TEMPLATE.format(
        document_type  = document_type,
        section_title  = section_title or "(untitled section)",
        semantic_label = semantic_label or "(none)",
        section_text   = section_text[:3000],
        lexnlp_block   = lexnlp_block,
        schema         = _SCHEMA_HINT,
    )


# ── LexNLP hints ──────────────────────────────────────────────────────────────

def _lexnlp_hints(text: str) -> str:
    if not _LEXNLP_OK or not text.strip():
        return ""
    lines: list[str] = []
    try:
        raw_dates = list(_lex_dates.get_dates(text))[:10]
        if raw_dates:
            fmt = []
            for d in raw_dates:
                try:
                    fmt.append(d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d))
                except Exception:
                    fmt.append(str(d))
            lines.append(f"Dates found by pre-processor: {fmt}")
    except Exception:
        pass
    try:
        raw_money = list(_lex_money.get_money(text))[:10]
        if raw_money:
            lines.append(f"Money found by pre-processor: {[f'{a} {c}' for a, c in raw_money]}")
    except Exception:
        pass
    try:
        raw_amounts = list(_lex_amounts.get_amounts(text))[:10]
        # Filter bullet-point false positives (small integers)
        filtered = [a for a in raw_amounts if not (isinstance(a, (int, float)) and a < 10 and a == int(a))]
        if filtered:
            lines.append(f"Numeric amounts found by pre-processor: {filtered}")
    except Exception:
        pass
    if not lines:
        return ""
    return (
        "--- Pre-extracted hints (regex-based, may contain false positives) ---\n"
        + "\n".join(lines)
        + "\n--- Validate these against the actual text before using them.\n"
    )


# ── Gemini client ─────────────────────────────────────────────────────────────

_GEMINI_MODEL = "gemini-2.5-flash"


def _init_gemini():
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        print("ERROR: GEMINI_API_KEY (or GOOGLE_API_KEY) not set in .env")
        sys.exit(1)
    try:
        from google import genai as _genai
        return _genai.Client(api_key=key)
    except ImportError:
        print("ERROR: google-genai not installed. Run: pip install google-genai")
        sys.exit(1)


def _call_gemini(client, prompt: str) -> dict | None:
    """Call Gemini Flash in JSON mode. Retries up to 3 times with backoff."""
    from google import genai as _genai
    from google.genai import types as _gtypes

    full_prompt = f"{_SYSTEM_PROMPT}\n\n{prompt}"

    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model   = _GEMINI_MODEL,
                contents= full_prompt,
                config  = _gtypes.GenerateContentConfig(
                    response_mime_type = "application/json",
                    temperature        = 0.1,
                ),
            )
            return json.loads(resp.text)
        except Exception as e:
            err   = str(e)
            is_429 = "429" in err or "quota" in err.lower() or "rate" in err.lower()
            if attempt < 2:
                wait = (5 + 10 * attempt) if is_429 else (2 ** attempt)
                wait += random.uniform(0, 2)
                if is_429:
                    print(f"  [03A] Rate-limited — waiting {wait:.1f}s…")
                time.sleep(wait)
            else:
                print(f"  [03A] Gemini failed after 3 attempts: {e}")
    return None


# ── Result → extraction rows ──────────────────────────────────────────────────

def _result_to_rows(
    result:    SectionExtractionResult,
    section_id:  str,
    document_id: str,
    page_range:  str | None,
) -> list[dict]:
    rows: list[dict] = []

    def _row(etype: str, name: str, value: str | None, conf: float, props: dict, raw: str | None) -> dict:
        return {
            "section_id":        section_id,
            "document_id":       document_id,
            "extraction_type":   etype,
            "entity_name":       name,
            "entity_value":      value,
            "raw_text":          raw,
            "confidence":        round(conf, 4),
            "page_range":        page_range,
            "extraction_method": f"gemini-flash:{_GEMINI_MODEL}",
            "properties":        {k: v for k, v in props.items() if v is not None},
            "needs_review":      conf < 0.7,
        }

    # parties
    for e in result.parties:
        if e.confidence < 0.5 or not e.name.strip():
            continue
        rows.append(_row(
            "party", e.name, e.entity_type, e.confidence,
            {"entity_type": e.entity_type, "role_in_document": e.role_in_document},
            e.raw_text,
        ))

    # dates
    for e in result.dates:
        if e.confidence < 0.5 or not e.description.strip():
            continue
        rows.append(_row(
            "date", e.description, e.date_value, e.confidence,
            {"date_type": e.date_type, "date_value": e.date_value},
            e.raw_text,
        ))

    # monetary amounts
    for e in result.monetary_amounts:
        if e.confidence < 0.5 or not e.description.strip():
            continue
        rows.append(_row(
            "amount", e.description, e.amount, e.confidence,
            {"currency": e.currency, "context": e.context, "amount": e.amount},
            e.raw_text,
        ))

    # courts
    for e in result.courts:
        if e.confidence < 0.5 or not e.name.strip():
            continue
        rows.append(_row(
            "court", e.name, e.court_type, e.confidence,
            {"court_type": e.court_type, "jurisdiction": e.jurisdiction},
            e.raw_text,
        ))

    # judges
    for e in result.judges:
        if e.confidence < 0.5 or not e.name.strip():
            continue
        rows.append(_row(
            "judge", e.name, e.title, e.confidence,
            {"title": e.title, "court": e.court},
            e.raw_text,
        ))

    # attorneys
    for e in result.attorneys:
        if e.confidence < 0.5 or not e.name.strip():
            continue
        rows.append(_row(
            "attorney", e.name, e.firm, e.confidence,
            {"bar_number": e.bar_number, "firm": e.firm, "representing": e.representing},
            e.raw_text,
        ))

    # law firms
    for e in result.law_firms:
        if e.confidence < 0.5 or not e.name.strip():
            continue
        rows.append(_row(
            "law_firm", e.name, None, e.confidence,
            {"representing": e.representing},
            e.raw_text,
        ))

    return rows


# ── Per-section extraction ────────────────────────────────────────────────────

# Minimum section text length to bother calling Gemini
_MIN_CHARS = 40

# Sections that are pure boilerplate — skip entirely
_SKIP_TITLES = {
    "table of contents", "table of authorities", "certificate of service",
    "signature block", "signature page", "cover page", "index of exhibits",
    "statement of compliance",
}

# Semantic labels guaranteed to contain no extractable structural entities
_SKIP_LABELS = {
    "table_of_contents", "table_of_authorities",
    "certificate_of_service", "signature_block", "cover_page",
    "index_of_exhibits", "statement_of_compliance",
    "prayer_for_relief",   # asks for damages — no new entity introductions
    "exhibit_list",        # references only, no named entities worth extracting
    "recitals",            # intro boilerplate — parties captured in caption
    "notice_of_hearing",   # procedural filler
}


def _should_skip(section: dict) -> str | None:
    """Return a reason string if the section should be skipped, else None."""
    text  = (section.get("section_text") or "").strip()
    title = (section.get("section_title") or "").strip().lower()
    label = (section.get("semantic_label") or "").strip().lower().replace(" ", "_")

    if len(text) < _MIN_CHARS:
        return "too short"
    if title in _SKIP_TITLES:
        return f"boilerplate ({title})"
    if label and label in _SKIP_LABELS:
        return f"skip label ({label})"
    # Skip child micro-sections (numbered paragraphs already covered by parent)
    if section.get("parent_section_id") and len(text) < 200:
        return "child micro-section"
    return None


def _extract_section(
    section:       dict,
    document_type: str,
    gemini_client,
) -> list[dict]:
    """Extract entities from one section. Returns rows ready for the extractions table."""
    sec_id     = section["id"]
    doc_id     = section["document_id"]
    title      = section.get("section_title") or ""
    text       = section.get("section_text") or ""
    page_range = section.get("page_range")

    hints  = _lexnlp_hints(text)

    # LexNLP gate: if LexNLP finds nothing interesting in a short, non-entity section, skip Gemini
    _ENTITY_LABELS = {"parties", "caption", "jurisdiction", "venue"}
    label = (section.get("semantic_label") or "").strip().lower().replace(" ", "_")
    if _LEXNLP_OK and not hints and len(text) < 400 and label not in _ENTITY_LABELS:
        return []

    prompt = _build_prompt(title, document_type, text, hints, semantic_label=label)

    raw = _call_gemini(gemini_client, prompt)
    if raw is None:
        return []

    try:
        result = SectionExtractionResult(**raw)
    except (ValidationError, TypeError) as e:
        print(f"  [03A] Parse error for section '{title[:50]}': {e}")
        return []

    return _result_to_rows(result, sec_id, doc_id, page_range)


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _get_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)
    return create_client(url, key)


def _resolve_document(sb: Client, args: argparse.Namespace) -> tuple[str, str, str | None]:
    """Return (document_id, file_name, document_type)."""
    if getattr(args, "document_id", None):
        resp = sb.table("documents").select("id, file_name, document_type").eq("id", args.document_id).execute()
    else:
        resp = sb.table("documents").select("id, file_name, document_type").eq("file_name", args.file_name).execute()
    if not resp.data:
        key = getattr(args, "document_id", None) or args.file_name
        print(f"ERROR: No document found for '{key}'")
        sys.exit(1)
    row = resp.data[0]
    return row["id"], row["file_name"], row.get("document_type")


def _insert_rows(sb: Client, rows: list[dict], dry_run: bool) -> None:
    """Bulk-insert extraction rows in batches of 50."""
    # Strip private _* keys before inserting
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]

    if dry_run:
        print(f"  [DRY RUN] Would insert {len(clean)} extraction rows")
        return

    batch_size = 50
    for i in range(0, len(clean), batch_size):
        sb.table("extractions").insert(clean[i : i + batch_size]).execute()


# ── Main ──────────────────────────────────────────────────────────────────────

def extract_entities_03a(document_id: str, dry_run: bool = False) -> int:
    """
    Core callable for pipeline integration.
    Returns the total number of extraction rows written.
    """
    sb             = _get_supabase()
    gemini_client  = _init_gemini()

    # Fetch document
    resp = sb.table("documents").select("id, file_name, document_type").eq("id", document_id).execute()
    if not resp.data:
        print(f"ERROR: Document {document_id} not found.")
        sys.exit(1)
    doc           = resp.data[0]
    file_name     = doc["file_name"]
    document_type = doc.get("document_type") or "Unknown"

    if not _LEXNLP_OK:
        print("  INFO: lexnlp not installed — running without pre-extraction hints")

    print(f"  [03A] Extracting from '{file_name}' ({document_type})")

    # Fetch sections
    sec_resp = (
        sb.table("sections")
        .select("id, document_id, section_title, section_text, page_range, parent_section_id, semantic_label")
        .eq("document_id", document_id)
        .execute()
    )
    sections = sec_resp.data or []
    if not sections:
        print(f"  [03A] WARNING: No sections found for '{file_name}'")
        return 0

    total = len(sections)
    print(f"  [03A] {total} sections to process")

    # Clear existing 03A extractions for this document (makes re-runs idempotent).
    # kg_nodes references extractions via source_extraction_id (FK), so wipe KG first.
    # kg_edges cascade-delete when kg_nodes are removed.
    _OWN_TYPES = ("party", "date", "amount", "court", "judge", "attorney", "law_firm")
    if not dry_run:
        sb.table("kg_nodes").delete().eq("document_id", document_id).execute()
        for etype in _OWN_TYPES:
            sb.table("extractions").delete()\
              .eq("document_id", document_id)\
              .eq("extraction_type", etype)\
              .execute()

    # Classify sections into process / skip
    to_process: list[tuple[int, dict]] = []
    for idx, sec in enumerate(sections, 1):
        reason = _should_skip(sec)
        if reason:
            title = (sec.get("section_title") or "(untitled)")[:60]
            print(f"  [{idx:>3}/{total}] SKIP  '{title}' — {reason}")
        else:
            to_process.append((idx, sec))

    print(f"  [03A] Processing {len(to_process)} sections ({total - len(to_process)} skipped)")

    all_rows:   list[dict] = []
    by_type:    dict[str, int] = {}

    def _record(idx: int, sec: dict, rows: list[dict]) -> None:
        title = (sec.get("section_title") or "(untitled)")[:60]
        if rows:
            for r in rows:
                et = r.get("extraction_type", "?")
                by_type[et] = by_type.get(et, 0) + 1
            all_rows.extend(rows)
            print(f"  [{idx:>3}/{total}] OK    '{title}' → {len(rows)} entities")
        else:
            print(f"  [{idx:>3}/{total}] EMPTY '{title}' → nothing found")

    # 5 parallel workers (Gemini Flash handles concurrent requests well)
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(_extract_section, sec, document_type, gemini_client): (idx, sec)
            for idx, sec in to_process
        }
        for f in as_completed(futures):
            idx, sec = futures[f]
            try:
                _record(idx, sec, f.result())
            except Exception as e:
                title = (sec.get("section_title") or "(untitled)")[:40]
                print(f"  [{idx:>3}/{total}] ERROR '{title}' — {e}")

    # Persist
    _insert_rows(sb, all_rows, dry_run)

    # Summary
    print(f"\n  [03A] DONE — {len(all_rows)} extractions from '{file_name}'")
    if by_type:
        for etype, count in sorted(by_type.items()):
            print(f"         {etype:<18} {count}")

    return len(all_rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="03A: Extract parties, dates, courts, judges, attorneys, firms, and amounts."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--document_id", help="Supabase document UUID")
    group.add_argument("--file_name",   help="Document file_name stem in Supabase")
    parser.add_argument("--dry_run", action="store_true",
                        help="Run extraction but do not write to Supabase")
    args = parser.parse_args()

    sb = _get_supabase()
    document_id, file_name, _ = _resolve_document(sb, args)

    count = extract_entities_03a(document_id, dry_run=args.dry_run)
    print(f"\nSUCCESS: 03A_entity_extraction.py — {count} entities extracted from '{file_name}'")


if __name__ == "__main__":
    main()
