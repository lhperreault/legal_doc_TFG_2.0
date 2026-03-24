"""
01_embed_sections.py — Phase 3, Step 1: Vector Embedding Pipeline

Reads all sections belonging to a case_id from Supabase, generates embeddings
via OpenAI text-embedding-3-small, and upserts into the section_embeddings table.

Safe to re-run:
  - Sections that already have an up-to-date embedding are skipped.
  - Sections whose metadata has changed (semantic_label, document_type) are re-embedded.
  - Deleted sections are cleaned up via CASCADE on the FK.

Usage:
    python 01_embed_sections.py <case_id>
    python 01_embed_sections.py <case_id> --document_id <doc_uuid>
    python 01_embed_sections.py <case_id> --force          # re-embed everything
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env from project root (two levels up from backend/03_SEARCH/)
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

from openai import OpenAI
from supabase import create_client, Client

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
BATCH_SIZE = 100          # OpenAI embeddings API accepts up to 2048 inputs
MAX_CHARS = 28000         # ~7000 tokens, safe buffer under 8191 token limit
SLEEP_BETWEEN_BATCHES = 0.1  # seconds, courtesy rate limiting

# Sections with these labels are typically empty/boilerplate — skip embedding
SKIP_LABELS = {"table_of_contents", "title_page", "signature_block", "certificate_of_service"}

# Minimum text length to embed (after stripping whitespace)
MIN_TEXT_LENGTH = 20


# ---------------------------------------------------------------------------
# Supabase + OpenAI clients
# ---------------------------------------------------------------------------

def _get_clients() -> tuple[Client, OpenAI]:
    """Initialize and return (supabase_client, openai_client)."""
    sb_url = os.getenv("SUPABASE_URL")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    oai_key = os.getenv("OPENAI_API_KEY")

    missing = []
    if not sb_url:
        missing.append("SUPABASE_URL")
    if not sb_key:
        missing.append("SUPABASE_SERVICE_ROLE_KEY")
    if not oai_key:
        missing.append("OPENAI_API_KEY")
    if missing:
        print(f"ERROR: Missing environment variables: {', '.join(missing)}")
        sys.exit(1)

    supabase = create_client(sb_url, sb_key)
    openai_client = OpenAI(api_key=oai_key)
    return supabase, openai_client


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def _fetch_sections(supabase: Client, case_id: str, document_id: str | None = None) -> list[dict]:
    """
    Fetch all sections for a case, joined with document metadata.

    Returns list of dicts with keys:
        id, document_id, case_id, section_title, section_text, level,
        page_range, start_page, end_page, is_synthetic, anchor_id,
        parent_section_id, semantic_label, semantic_confidence, label_source,
        document_type, file_name
    """
    # Step 1: Get document IDs for this case
    doc_query = supabase.table("documents").select("id, case_id, document_type, file_name").eq("case_id", case_id)
    if document_id:
        doc_query = doc_query.eq("id", document_id)
    doc_response = doc_query.execute()
    documents = doc_response.data

    if not documents:
        print(f"  No documents found for case_id={case_id}" +
              (f", document_id={document_id}" if document_id else ""))
        return []

    doc_map = {d["id"]: d for d in documents}
    doc_ids = list(doc_map.keys())

    print(f"  Found {len(documents)} document(s) in case")

    # Step 2: Fetch sections for those documents
    # Supabase-py .in_() has a practical limit, so batch if needed
    all_sections = []
    for i in range(0, len(doc_ids), 50):
        batch_ids = doc_ids[i:i + 50]
        sec_response = (
            supabase.table("sections")
            .select("id, document_id, section_title, section_text, level, "
                    "page_range, start_page, end_page, is_synthetic, anchor_id, "
                    "parent_section_id, semantic_label, semantic_confidence, label_source")
            .in_("document_id", batch_ids)
            .execute()
        )
        all_sections.extend(sec_response.data)

    # Step 3: Enrich each section with document-level metadata
    for sec in all_sections:
        doc = doc_map.get(sec["document_id"], {})
        sec["case_id"] = case_id
        sec["document_type"] = doc.get("document_type")
        sec["file_name"] = doc.get("file_name")

    print(f"  Found {len(all_sections)} total section(s)")
    return all_sections


def _fetch_existing_embeddings(supabase: Client, case_id: str) -> dict[str, dict]:
    """
    Fetch existing embedding metadata for this case.
    Returns dict: section_id -> {semantic_label, document_type, embedding_model}
    """
    existing = {}
    response = (
        supabase.table("section_embeddings")
        .select("section_id, semantic_label, document_type, embedding_model")
        .eq("case_id", case_id)
        .execute()
    )
    for row in response.data:
        existing[row["section_id"]] = row
    return existing


# ---------------------------------------------------------------------------
# Embedding input construction
# ---------------------------------------------------------------------------

def _build_embedding_input(section: dict) -> str:
    """
    Build the text string that gets sent to the embedding model.

    Format: [document_type] [semantic_label] section_title\n\nsection_text

    The metadata prefix causes sections with the same structural role to
    cluster together in vector space, improving search relevance.
    """
    parts = []

    doc_type = section.get("document_type") or "Unknown"
    parts.append(f"[{doc_type}]")

    label = section.get("semantic_label")
    if label:
        parts.append(f"[{label}]")

    title = (section.get("section_title") or "").strip()
    if title:
        parts.append(title)

    prefix = " ".join(parts)
    text = (section.get("section_text") or "").strip()

    embedding_input = f"{prefix}\n\n{text}" if text else prefix

    # Truncate if too long (should be rare after section refiner)
    if len(embedding_input) > MAX_CHARS:
        embedding_input = embedding_input[:MAX_CHARS]
        return embedding_input  # caller logs truncation

    return embedding_input


def _build_search_text(section: dict) -> str:
    """
    Build the keyword search text. No metadata prefix — just title + text.
    This is what pg_trgm indexes for exact term matching.
    """
    title = (section.get("section_title") or "").strip()
    text = (section.get("section_text") or "").strip()
    return f"{title}\n{text}" if title else text


# ---------------------------------------------------------------------------
# Filtering: which sections need (re-)embedding?
# ---------------------------------------------------------------------------

def _needs_embedding(section: dict, existing: dict[str, dict], force: bool) -> tuple[bool, str]:
    """
    Determine if a section needs embedding. Returns (needs_it, reason).

    Reasons:
      - "new": no existing embedding
      - "metadata_changed": semantic_label or document_type changed
      - "force": --force flag
      - "skip_empty": text too short
      - "skip_label": label in SKIP_LABELS
      - "up_to_date": already embedded with current metadata
    """
    sec_id = section["id"]
    text = (section.get("section_text") or "").strip()
    label = section.get("semantic_label") or ""

    # Skip empty/trivial sections
    if len(text) < MIN_TEXT_LENGTH:
        return False, "skip_empty"

    # Skip boilerplate labels
    if label in SKIP_LABELS:
        return False, "skip_label"

    if force:
        return True, "force"

    # Check if embedding exists
    existing_row = existing.get(sec_id)
    if not existing_row:
        return True, "new"

    # Check if metadata drifted (label or doc_type changed since last embed)
    if existing_row.get("semantic_label") != section.get("semantic_label"):
        return True, "metadata_changed"
    if existing_row.get("document_type") != section.get("document_type"):
        return True, "metadata_changed"

    return False, "up_to_date"


# ---------------------------------------------------------------------------
# OpenAI embedding calls
# ---------------------------------------------------------------------------

def _embed_batch(openai_client: OpenAI, texts: list[str]) -> list[list[float]]:
    """
    Call OpenAI embeddings API for a batch of texts.
    Returns list of embedding vectors (each a list of 1536 floats).
    Retries up to 3 times with exponential backoff.
    """
    for attempt in range(3):
        try:
            response = openai_client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=texts,
            )
            return [item.embedding for item in response.data]
        except Exception as e:
            wait = 2 ** attempt
            print(f"  WARNING: Embedding API error (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                print(f"  Retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


# ---------------------------------------------------------------------------
# Supabase upsert
# ---------------------------------------------------------------------------

def _upsert_embeddings(supabase: Client, rows: list[dict]) -> int:
    """
    Upsert embedding rows into section_embeddings.
    Uses ON CONFLICT (section_id) DO UPDATE so re-runs are safe.
    Returns number of errors.
    """
    errors = 0
    for i in range(0, len(rows), 50):
        batch = rows[i:i + 50]
        try:
            supabase.table("section_embeddings").upsert(
                batch, on_conflict="section_id"
            ).execute()
        except Exception as e:
            print(f"  WARNING: Upsert batch failed ({len(batch)} rows): {e}")
            errors += len(batch)
    return errors


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def embed_case(case_id: str, document_id: str | None = None, force: bool = False) -> dict:
    """
    Main entry point. Embeds all sections for a case.

    Returns summary dict:
        {total_sections, embedded, skipped_up_to_date, skipped_empty,
         skipped_label, re_embedded, truncated, errors}
    """
    print(f"\n{'='*60}")
    print(f"  Phase 3 — Embedding Sections")
    print(f"  Case ID: {case_id}")
    if document_id:
        print(f"  Document ID: {document_id}")
    if force:
        print(f"  Mode: FORCE re-embed all")
    print(f"{'='*60}\n")

    supabase, openai_client = _get_clients()

    # --- Fetch data ---
    print("[1/4] Fetching sections from Supabase...")
    sections = _fetch_sections(supabase, case_id, document_id)
    if not sections:
        print("  Nothing to embed.")
        return {"total_sections": 0, "embedded": 0}

    print("[2/4] Checking existing embeddings...")
    existing = _fetch_existing_embeddings(supabase, case_id)
    print(f"  Found {len(existing)} existing embedding(s)")

    # --- Filter sections that need embedding ---
    to_embed = []
    summary = {
        "total_sections": len(sections),
        "embedded": 0,
        "skipped_up_to_date": 0,
        "skipped_empty": 0,
        "skipped_label": 0,
        "re_embedded": 0,
        "truncated": 0,
        "errors": 0,
    }

    for sec in sections:
        needs_it, reason = _needs_embedding(sec, existing, force)
        if needs_it:
            to_embed.append((sec, reason))
        elif reason == "skip_empty":
            summary["skipped_empty"] += 1
        elif reason == "skip_label":
            summary["skipped_label"] += 1
        elif reason == "up_to_date":
            summary["skipped_up_to_date"] += 1

    print(f"\n  Sections to embed: {len(to_embed)}")
    print(f"  Skipped (up to date): {summary['skipped_up_to_date']}")
    print(f"  Skipped (empty/short): {summary['skipped_empty']}")
    print(f"  Skipped (boilerplate label): {summary['skipped_label']}")

    if not to_embed:
        print("\n  All sections are up to date. Nothing to do.")
        return summary

    # --- Build embedding inputs ---
    print(f"\n[3/4] Generating embeddings ({len(to_embed)} sections)...")
    embedding_inputs = []
    upsert_rows = []

    for sec, reason in to_embed:
        emb_input = _build_embedding_input(sec)
        search_text = _build_search_text(sec)

        if len(emb_input) >= MAX_CHARS:
            summary["truncated"] += 1
            print(f"  WARNING: Truncated section '{sec.get('section_title', '')[:50]}' "
                  f"(doc: {sec.get('file_name', '?')})")

        embedding_inputs.append(emb_input)
        upsert_rows.append({
            "section_id":       sec["id"],
            "document_id":      sec["document_id"],
            "case_id":          sec["case_id"],
            "embedding":        None,  # filled after API call
            "document_type":    sec.get("document_type"),
            "semantic_label":   sec.get("semantic_label"),
            "level":            sec.get("level"),
            "is_synthetic":     sec.get("is_synthetic"),
            "page_range":       sec.get("page_range"),
            "section_title":    sec.get("section_title"),
            "search_text":      search_text,
            "embedding_model":  EMBEDDING_MODEL,
        })

        if reason == "metadata_changed":
            summary["re_embedded"] += 1

    # --- Call OpenAI in batches ---
    all_embeddings = []
    for i in range(0, len(embedding_inputs), BATCH_SIZE):
        batch_texts = embedding_inputs[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        total_batches = (len(embedding_inputs) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"  Batch {batch_num}/{total_batches} ({len(batch_texts)} sections)...")

        try:
            batch_embeddings = _embed_batch(openai_client, batch_texts)
            all_embeddings.extend(batch_embeddings)
        except Exception as e:
            print(f"  ERROR: Batch {batch_num} failed permanently: {e}")
            summary["errors"] += len(batch_texts)
            # Pad with None so indices stay aligned
            all_embeddings.extend([None] * len(batch_texts))

        if i + BATCH_SIZE < len(embedding_inputs):
            time.sleep(SLEEP_BETWEEN_BATCHES)

    # --- Attach embeddings to upsert rows ---
    valid_rows = []
    for row, emb in zip(upsert_rows, all_embeddings):
        if emb is not None:
            row["embedding"] = emb
            valid_rows.append(row)

    # --- Upsert to Supabase ---
    print(f"\n[4/4] Upserting {len(valid_rows)} embedding(s) to Supabase...")
    errors = _upsert_embeddings(supabase, valid_rows)
    summary["errors"] += errors
    summary["embedded"] = len(valid_rows) - errors

    # --- Write summary to zz_temp_chunks ---
    _write_summary(case_id, summary)

    # --- Print results ---
    print(f"\n{'='*60}")
    print(f"  RESULTS")
    print(f"{'='*60}")
    print(f"  Total sections:       {summary['total_sections']}")
    print(f"  Newly embedded:       {summary['embedded']}")
    print(f"  Re-embedded (changed):{summary['re_embedded']}")
    print(f"  Skipped (up to date): {summary['skipped_up_to_date']}")
    print(f"  Skipped (empty):      {summary['skipped_empty']}")
    print(f"  Skipped (label):      {summary['skipped_label']}")
    print(f"  Truncated:            {summary['truncated']}")
    print(f"  Errors:               {summary['errors']}")

    if summary["errors"] > 0:
        print(f"\nERROR: {summary['errors']} section(s) failed to embed")
    else:
        print(f"\nSUCCESS: {summary['embedded']} section(s) embedded for case {case_id}")

    return summary


# ---------------------------------------------------------------------------
# Summary output file
# ---------------------------------------------------------------------------

def _write_summary(case_id: str, summary: dict):
    """Write embedding summary to zz_temp_chunks for pipeline tracking."""
    temp_dir = os.path.join(_SCRIPT_DIR, "..", "zz_temp_chunks")
    os.makedirs(temp_dir, exist_ok=True)

    summary_path = os.path.join(temp_dir, f"{case_id}_embedding_summary.json")
    output = {
        "case_id": case_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "embedding_model": EMBEDDING_MODEL,
        **summary,
    }
    with open(summary_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Summary written to {summary_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Embed sections for a case into the vector store."
    )
    parser.add_argument("case_id", help="UUID of the case to embed")
    parser.add_argument("--document_id", default=None,
                        help="Optional: embed only sections from this document")
    parser.add_argument("--force", action="store_true",
                        help="Re-embed all sections, ignoring existing embeddings")
    args = parser.parse_args()

    summary = embed_case(args.case_id, args.document_id, args.force)

    # Exit code for orchestrator
    sys.exit(1 if summary.get("errors", 0) > 0 else 0)


if __name__ == "__main__":
    main()