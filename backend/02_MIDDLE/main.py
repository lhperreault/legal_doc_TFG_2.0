"""
02_MIDDLE/main.py — Phase 2 orchestrator: AST construction + semantic labeling.

Usage:
    python backend/02_MIDDLE/main.py --file_name "Complaint (Epic Games to Apple"
"""

import argparse
import os
import subprocess
import sys

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

PHASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _run(script_name: str, *args) -> str:
    """
    Run a Phase 2 script, capture stdout, and check for SUCCESS/ERROR.
    Returns stdout text. Exits on ERROR.
    """
    result = subprocess.run(
        [sys.executable, os.path.join(PHASE_DIR, script_name)] + list(args),
        capture_output=True,
        text=True,
    )
    output = result.stdout.strip()
    if output:
        print(output)
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)

    last_line = output.splitlines()[-1] if output else ""
    if last_line.startswith("ERROR") or result.returncode != 0:
        print(f"[Phase 2] FAILED at {script_name}. Stopping.")
        sys.exit(result.returncode or 1)

    return output


def _lookup_document_id(file_name: str) -> str:
    """Resolve file_name → document_id from Supabase."""
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)
    sb   = create_client(url, key)
    resp = sb.table("documents").select("id").eq("file_name", file_name).execute()
    if not resp.data:
        print(f"ERROR: No document found with file_name='{file_name}'")
        sys.exit(1)
    return resp.data[0]["id"]


def main():
    parser = argparse.ArgumentParser(description="Phase 2: AST construction pipeline.")
    parser.add_argument("--file_name", required=True, help="Document stem (file_name in Supabase)")
    args = parser.parse_args()

    file_name   = args.file_name
    document_id = _lookup_document_id(file_name)

    print(f"[Phase 2] Starting AST pipeline for '{file_name}' (id={document_id})")
    print("=" * 60)

    # Step 0: Section refinement (split oversized sections before tree build)
    print("[Phase 2] Step 0 — Refining section structure…")
    _run("00_section_refine.py", "--document_id", document_id)

    # Step 1: Tree reconstruction
    print("[Phase 2] Step 1 — Building parent-child tree…")
    _run("01_AST_tree_build.py", "--document_id", document_id)

    # Step 2: Semantic labeling
    print("[Phase 2] Step 2 — Assigning semantic labels…")
    _run("02_AST_semantic_label.py", "--document_id", document_id)

    # Step 3: Entity extraction
    print("[Phase 2] Step 3 — Extracting entities…")
    _run("03_entity_extraction.py", "--document_id", document_id)

    print("=" * 60)
    print(f"[Phase 2] COMPLETE — '{file_name}' AST + entities ready in Supabase.")

    # Process child exhibit documents if any exist
    from supabase import create_client
    _url = os.environ.get("SUPABASE_URL")
    _key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if _url and _key:
        sb = create_client(_url, _key)
        child_resp = sb.table("documents").select("id, file_name").eq("parent_document_id", document_id).execute()
        if child_resp.data:
            print(f"\n[Phase 2] Processing {len(child_resp.data)} child exhibit(s)...")
            for child in child_resp.data:
                child_id   = child["id"]
                child_name = child["file_name"]

                # Skip tiny exhibits — not worth the API calls
                section_resp = sb.table("sections").select("section_text").eq("document_id", child_id).execute()
                total_chars  = sum(len(s.get("section_text") or "") for s in (section_resp.data or []))
                if total_chars < 2000:
                    print(f"\n[Phase 2] Skipping '{child_name}' — too short ({total_chars} chars)")
                    continue

                print(f"\n[Phase 2] --- Exhibit: {child_name} ({total_chars:,} chars) ---")
                _run("00_section_refine.py",    "--document_id", child_id)
                _run("01_AST_tree_build.py",    "--document_id", child_id)
                _run("02_AST_semantic_label.py","--document_id", child_id)
                _run("03_entity_extraction.py", "--document_id", child_id)
            print(f"\n[Phase 2] COMPLETE — all exhibits processed.")


if __name__ == "__main__":
    main()
