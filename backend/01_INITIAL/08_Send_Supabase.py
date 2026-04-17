"""
08_Send_Supabase.py - Sends processed document data to Supabase.

Usage:
    python 08_Send_Supabase.py <path_to_text_extraction_md>

Reads from zz_temp_chunks/:
    {doc_stem}_structure_report.csv
    {doc_stem}_text_extraction_classification.csv   (stem includes _text_extraction)
    {doc_stem}_07_toc_sections.csv
    {doc_stem}_07_final_document.md
    ui_assets/{doc_stem}_tagged.xhtml               (HTML docs only, optional)

Writes to Supabase:
    documents table  — upsert by file_name
    sections  table  — delete existing + bulk insert fresh rows

-------------------------------------------------------------------------------
Required Supabase tables (run once in Supabase SQL editor):
-------------------------------------------------------------------------------
CREATE TABLE documents (
    id               UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    file_name        TEXT        UNIQUE NOT NULL,
    document_type    TEXT,
    confidence_score FLOAT,
    full_text_md     TEXT,
    tagged_xhtml_url TEXT,
    has_native_toc   BOOLEAN     DEFAULT FALSE,
    total_pages      INTEGER,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE sections (
    id            UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    document_id   UUID        REFERENCES documents(id) ON DELETE CASCADE,
    level         INTEGER,
    section_title TEXT,
    page_range    TEXT,
    start_page    FLOAT,
    end_page      FLOAT,
    anchor_id     TEXT,
    is_synthetic  BOOLEAN     DEFAULT FALSE,
    section_text  TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
-------------------------------------------------------------------------------
Storage bucket:
    Create a public (or private) bucket named "documents" in Supabase Storage.
    Tagged XHTML files for HTML docs will be uploaded there.
-------------------------------------------------------------------------------
"""

import ast
import json
import os
import sys
import math

import pandas as pd
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL          = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY  = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
STORAGE_BUCKET        = "documents"
ORIGINALS_SUBFOLDER   = "originals"   # subfolder inside the same bucket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(val) -> float | None:
    """Return float or None for NaN/missing values."""
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _safe_int(val) -> int | None:
    f = _safe_float(val)
    return None if f is None else int(f)


def _safe_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "1", "yes")


def _upload_original_file(supabase: Client, local_path: str, file_name_with_ext: str) -> "str | None":
    """Upload the original source file to storage. Returns the public URL or None on failure."""
    if not os.path.isfile(local_path):
        print(f"[08] WARNING: original file not found at {local_path}, skipping upload.")
        return None
    ext = os.path.splitext(file_name_with_ext)[1].lower()
    content_type_map = {
        ".pdf":   "application/pdf",
        ".docx":  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc":   "application/msword",
        ".html":  "text/html",
        ".htm":   "text/html",
        ".xhtml": "application/xhtml+xml",
        ".txt":   "text/plain",
    }
    content_type  = content_type_map.get(ext, "application/octet-stream")
    storage_path  = f"{ORIGINALS_SUBFOLDER}/{file_name_with_ext}"
    with open(local_path, "rb") as f:
        file_bytes = f.read()
    try:
        supabase.storage.from_(STORAGE_BUCKET).remove([storage_path])
    except Exception:
        pass
    try:
        supabase.storage.from_(STORAGE_BUCKET).upload(
            storage_path, file_bytes, {"content-type": content_type}
        )
        url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(storage_path)
        print(f"[08] Uploaded original → {url}")
        return url
    except Exception as exc:
        print(f"[08] WARNING: original file upload failed: {exc}")
        return None


def _get_total_pages(extraction_strategy_str: str) -> int | None:
    """Parse total_pages out of the extraction_strategy dict string."""
    try:
        d = ast.literal_eval(str(extraction_strategy_str))
        return int(d.get("diagnostics", {}).get("total_pages", 0)) or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Send processed document to Supabase.")
    parser.add_argument("text_md",   help="Path to the _text_extraction.md file")
    parser.add_argument("--case-id", default=None, help="Supabase case UUID to attach this document to")
    parser.add_argument("--primary", action="store_true", help="Set is_primary_filing=True on this document")
    parser.add_argument("--original-file", default=None,
        help="Filename with extension of the original source document (e.g. Complaint.pdf)")
    args = parser.parse_args()

    text_md    = args.text_md
    case_id    = args.case_id
    is_primary = args.primary

    temp_dir  = os.path.dirname(text_md)                              # …/zz_temp_chunks
    stem      = os.path.splitext(os.path.basename(text_md))[0]       # {doc_stem}_text_extraction
    doc_stem  = stem.replace("_text_extraction", "")                  # {doc_stem}
    file_name = doc_stem                                               # used as unique key

    print(f"[08] Sending '{doc_stem}' to Supabase…")

    # ------------------------------------------------------------------
    # 1. Read input files
    # ------------------------------------------------------------------

    # Structure report
    struct_csv = os.path.join(temp_dir, doc_stem + "_structure_report.csv")
    if not os.path.isfile(struct_csv):
        print(f"[08] ERROR: structure report not found: {struct_csv}")
        sys.exit(1)
    struct_df = pd.read_csv(struct_csv)
    struct_row = struct_df.iloc[0]

    has_native_toc = _safe_bool(struct_row.get("has_native_toc", False))
    total_pages    = _get_total_pages(struct_row.get("extraction_strategy", ""))

    # Classification
    class_csv = os.path.join(temp_dir, stem + "_classification.csv")
    document_type    = None
    confidence_score = None
    if os.path.isfile(class_csv):
        class_df         = pd.read_csv(class_csv)
        document_type    = str(class_df.iloc[0].get("document_type", "")) or None
        confidence_score = _safe_float(class_df.iloc[0].get("confidence_score"))
    else:
        print(f"[08] WARNING: classification CSV not found ({class_csv}), skipping.")

    # Fine-grained folder routing (from 05b_fine_routing.py)
    folder_parent  = None
    folder_subslug = None
    fine_json_path = os.path.join(temp_dir, stem + "_fine_routing.json")
    if os.path.isfile(fine_json_path):
        try:
            with open(fine_json_path, encoding="utf-8") as f:
                fine = json.load(f)
            folder_parent  = fine.get("folder_parent")
            folder_subslug = fine.get("folder_subslug")
        except Exception as e:
            print(f"[08] WARNING: could not parse fine_routing JSON: {e}")
    else:
        print(f"[08] WARNING: fine_routing JSON not found ({fine_json_path}), leaving folder cols null.")

    # TOC sections
    toc_csv  = os.path.join(temp_dir, doc_stem + "_07_toc_sections.csv")
    sections_df = None
    if os.path.isfile(toc_csv):
        sections_df = pd.read_csv(toc_csv)
    else:
        print(f"[08] WARNING: toc_sections CSV not found ({toc_csv}), no sections will be inserted.")

    # Full document markdown
    md_path      = os.path.join(temp_dir, doc_stem + "_07_final_document.md")
    full_text_md = None
    if os.path.isfile(md_path):
        with open(md_path, encoding="utf-8") as f:
            full_text_md = f.read()
        # Strip null bytes — Postgres text columns reject \u0000
        full_text_md = full_text_md.replace('\x00', '')
    else:
        print(f"[08] WARNING: final document MD not found ({md_path}).")

    # Tagged XHTML (HTML docs only)
    xhtml_path = os.path.join(temp_dir, "ui_assets", doc_stem + "_tagged.xhtml")
    has_xhtml  = os.path.isfile(xhtml_path)

    # ------------------------------------------------------------------
    # 2. Connect to Supabase
    # ------------------------------------------------------------------
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    # ------------------------------------------------------------------
    # 2b. Upload original source file to Storage
    # ------------------------------------------------------------------
    original_file_url = None
    if args.original_file:
        data_storage_dir  = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data_storage", "documents"
        )
        original_local_path = os.path.join(data_storage_dir, args.original_file)
        original_file_url   = _upload_original_file(supabase, original_local_path, args.original_file)

    # ------------------------------------------------------------------
    # 3. Upload tagged XHTML to Storage (if present)
    # ------------------------------------------------------------------
    tagged_xhtml_url = None
    if has_xhtml:
        storage_path = f"{doc_stem}_tagged.xhtml"
        with open(xhtml_path, "rb") as f:
            xhtml_bytes = f.read()
        try:
            # Remove old file first (ignore error if it doesn't exist)
            supabase.storage.from_(STORAGE_BUCKET).remove([storage_path])
        except Exception:
            pass
        supabase.storage.from_(STORAGE_BUCKET).upload(
            storage_path,
            xhtml_bytes,
            {"content-type": "application/xhtml+xml"},
        )
        tagged_xhtml_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(storage_path)
        print(f"[08] Uploaded tagged XHTML → {tagged_xhtml_url}")

    # ------------------------------------------------------------------
    # 4. Upsert document row
    # ------------------------------------------------------------------
    doc_payload = {
        "file_name":          file_name,
        "document_type":      document_type,
        "confidence_score":   confidence_score,
        "full_text_md":       full_text_md,
        "tagged_xhtml_url":   tagged_xhtml_url,
        "has_native_toc":     has_native_toc,
        "total_pages":        total_pages,
        "case_id":            case_id,          # None → not linked to a case yet
        "is_primary_filing":  is_primary if is_primary else None,
        "original_file_url":  original_file_url,
        "ai_extracted":       False,
        "folder_parent":      folder_parent,
        "folder_subslug":     folder_subslug,
    }
    # Remove None values — let DB defaults handle them
    doc_payload = {k: v for k, v in doc_payload.items() if v is not None}

    doc_resp = (
        supabase.table("documents")
        .upsert(doc_payload, on_conflict="file_name")
        .execute()
    )
    if not doc_resp.data:
        print(f"[08] ERROR: document upsert returned no data. Response: {doc_resp}")
        sys.exit(1)

    document_id = doc_resp.data[0]["id"]
    print(f"[08] Document upserted (id={document_id})")

    # ------------------------------------------------------------------
    # 5. Replace sections
    # ------------------------------------------------------------------
    if sections_df is None or sections_df.empty:
        print("[08] No sections to insert.")
    else:
        # Delete existing sections for this document
        supabase.table("sections").delete().eq("document_id", document_id).execute()

        # Build section rows
        section_rows = []
        for _, row in sections_df.iterrows():
            section_rows.append({
                "document_id":   document_id,
                "level":         _safe_int(row.get("level")),
                "section_title": str(row.get("section", "")) or None,
                "page_range":    str(row.get("page_range", "")) if not pd.isna(row.get("page_range", float("nan"))) else None,
                "start_page":    _safe_int(row.get("start_page")),
                "end_page":      _safe_int(row.get("end_page")),
                "anchor_id":     str(row.get("anchor_id", "")) if "anchor_id" in row and not pd.isna(row.get("anchor_id", float("nan"))) else None,
                "is_synthetic":  _safe_bool(row.get("is_synthetic", False)) if "is_synthetic" in row else False,
                "section_text":  str(row.get("section_text", "")) or None,
            })
            # Remove None values
            section_rows[-1] = {k: v for k, v in section_rows[-1].items() if v is not None}

        # Bulk insert in batches of 100
        batch_size = 100
        for i in range(0, len(section_rows), batch_size):
            batch = section_rows[i : i + batch_size]
            supabase.table("sections").insert(batch).execute()

        print(f"[08] Inserted {len(section_rows)} sections.")

    print(f"[08] Done — '{doc_stem}' is in Supabase.")


if __name__ == "__main__":
    main()
