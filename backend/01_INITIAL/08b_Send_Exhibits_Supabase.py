"""
08b_Send_Exhibits_Supabase.py — Phase 1, Step 8b: Upload Exhibit Documents

Reads the exhibit manifest from 07b_exhibit_split.py and creates:
  1. A document row for each exhibit (with parent_document_id pointing to the parent)
  2. Section rows for each exhibit (one section per exhibit initially —
     Phase 2's 00_section_refine.py will split them further if needed)

Requires: parent_document_id column on documents table:
    ALTER TABLE documents ADD COLUMN parent_document_id UUID REFERENCES documents(id);

Usage:
    python 08b_Send_Exhibits_Supabase.py <path_to_text_extraction.md>
"""

import json
import os
import sys

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

TEMP_DIR = os.path.join(os.path.dirname(__file__), '..', 'zz_temp_chunks')


def _get_client():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)
    return create_client(url, key)


def main():
    if len(sys.argv) < 2:
        print("Usage: python 08b_Send_Exhibits_Supabase.py <path_to_text_extraction.md>")
        sys.exit(1)

    text_path = sys.argv[1]
    basename = os.path.basename(text_path)
    doc_stem = basename.replace('_text_extraction.md', '')

    # Read the manifest
    manifest_path = os.path.join(TEMP_DIR, f"{doc_stem}_exhibit_manifest.json")
    if not os.path.exists(manifest_path):
        print(f"SUCCESS: No exhibit manifest found for '{doc_stem}'. Nothing to upload.")
        return

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    if not manifest:
        print(f"SUCCESS: Exhibit manifest is empty for '{doc_stem}'. Nothing to upload.")
        return

    supabase = _get_client()

    # Look up the parent document ID
    resp = supabase.table("documents").select("id").eq("file_name", doc_stem).execute()
    if not resp.data:
        print(f"ERROR: Parent document '{doc_stem}' not found in Supabase. Run 08_Send_Supabase.py first.")
        sys.exit(1)
    parent_doc_id = resp.data[0]["id"]

    # Also look up the parent's case_id if it has one
    resp2 = supabase.table("documents").select("case_id").eq("id", parent_doc_id).execute()
    parent_case_id = resp2.data[0].get("case_id") if resp2.data else None

    uploaded = 0
    errors = 0

    for exhibit in manifest:
        exhibit_stem = exhibit['exhibit_stem']
        label = exhibit['exhibit_label']
        title = exhibit['exhibit_title']
        doc_type = exhibit['document_type']
        start_page = exhibit.get('start_page')
        end_page = exhibit.get('end_page')

        # Read the exhibit's text
        text_path_ex = os.path.join(TEMP_DIR, f"{exhibit_stem}_text_extraction.md")
        if not os.path.exists(text_path_ex):
            print(f"  WARNING: Text file not found for exhibit {label}, skipping")
            errors += 1
            continue

        with open(text_path_ex, 'r', encoding='utf-8') as f:
            exhibit_text = f.read()

        # Strip null bytes — Postgres text columns reject \u0000
        exhibit_text = exhibit_text.replace('\x00', '')

        if not exhibit_text.strip():
            print(f"  WARNING: Empty text for exhibit {label}, skipping")
            errors += 1
            continue

        # Read classification written by 05_doc_classification (GPT-based).
        # This overwrites the pattern-based doc_type from the manifest.
        class_path = os.path.join(TEMP_DIR, f"{exhibit_stem}_text_extraction_classification.json")
        confidence = 0.6
        if os.path.exists(class_path):
            with open(class_path, 'r', encoding='utf-8') as f:
                class_data = json.load(f)
                confidence = class_data.get('confidence_score', 0.6)
                gpt_type = class_data.get('document_type', '').strip()
                if gpt_type:
                    doc_type = gpt_type

        # Calculate total pages
        total_pages = None
        if start_page is not None and end_page is not None:
            total_pages = int(end_page) - int(start_page) + 1

        # Upsert the exhibit as a document
        doc_row = {
            "file_name": exhibit_stem,
            "document_type": doc_type,
            "confidence_score": confidence,
            "full_text_md": exhibit_text,
            "has_native_toc": False,
            "total_pages": total_pages,
            "parent_document_id": parent_doc_id,
        }

        # Add case_id if parent has one
        if parent_case_id:
            doc_row["case_id"] = parent_case_id

        try:
            resp = supabase.table("documents").upsert(
                doc_row, on_conflict="file_name"
            ).execute()
            exhibit_doc_id = resp.data[0]["id"]
        except Exception as e:
            print(f"  WARNING: Failed to upsert document for exhibit {label} — {e}")
            errors += 1
            continue

        # Delete existing sections for this exhibit (in case of re-run)
        try:
            supabase.table("sections").delete().eq("document_id", exhibit_doc_id).execute()
        except Exception:
            pass

        # Create one section row for the exhibit
        # Phase 2's 00_section_refine.py will split it further if needed
        page_range = ""
        if start_page is not None and end_page is not None:
            page_range = f"{int(start_page)}-{int(end_page)}"

        section_row = {
            "document_id": exhibit_doc_id,
            "level": 0,
            "section_title": title,
            "section_text": exhibit_text,
            "page_range": page_range,
            "start_page": start_page,
            "end_page": end_page,
            "is_synthetic": False,
            "anchor_id": None,
        }

        try:
            supabase.table("sections").insert(section_row).execute()
        except Exception as e:
            print(f"  WARNING: Failed to insert section for exhibit {label} — {e}")
            errors += 1
            continue

        uploaded += 1
        print(f"  Uploaded exhibit {label}: '{title[:50]}...' as '{doc_type}' (id={exhibit_doc_id})")

    if errors:
        print(f"WARNING: Uploaded {uploaded} exhibit(s) with {errors} error(s) for '{doc_stem}'.")
        # Don't exit(1) — partial exhibit failures shouldn't kill the pipeline

    # Trim parent document: delete sections that fall inside exhibit page ranges.
    # The first exhibit's start_page is the cutoff — everything from that page
    # onwards in the parent belongs to exhibits, not the motion itself.
    first_exhibit_page = None
    for exhibit in manifest:
        sp = exhibit.get('start_page')
        if sp is not None:
            p = int(sp)
            if first_exhibit_page is None or p < first_exhibit_page:
                first_exhibit_page = p

    if first_exhibit_page is not None:
        try:
            supabase.table("sections").delete()\
                .eq("document_id", parent_doc_id)\
                .gte("start_page", first_exhibit_page)\
                .execute()
            print(f"  Trimmed parent sections: removed pages {first_exhibit_page}+ (now exhibit-only content).")
        except Exception as e:
            print(f"  WARNING: Could not trim parent sections — {e}")
    else:
        print(
            f"  WARNING: Exhibit page numbers unavailable — parent sections not trimmed. "
            f"Run Phase 2 on parent with caution (exhibit content still present)."
        )

    print(
        f"SUCCESS: Uploaded {uploaded} exhibit(s) as child documents of '{doc_stem}'. "
        f"Parent id={parent_doc_id}."
    )


if __name__ == "__main__":
    main()