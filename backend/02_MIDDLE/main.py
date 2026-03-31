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

PHASE_DIR   = os.path.dirname(os.path.abspath(__file__))
SEARCH_DIR  = os.path.join(PHASE_DIR, '..', '03_SEARCH')


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


def _sb():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)
    return create_client(url, key)


def _lookup_document(file_name: str) -> tuple[str, str | None]:
    """Resolve file_name → (document_id, case_id) from Supabase."""
    resp = _sb().table("documents").select("id, case_id").eq("file_name", file_name).execute()
    if not resp.data:
        print(f"ERROR: No document found with file_name='{file_name}'")
        sys.exit(1)
    row = resp.data[0]
    return row["id"], row.get("case_id")


def _fetch_doc(document_id: str) -> dict:
    """Fetch document_type and filing_purpose after 07C may have updated them."""
    resp = _sb().table("documents").select("document_type, filing_purpose").eq("id", document_id).single().execute()
    return resp.data or {}


_03B_PREFIXES = ("complaint", "brief", "motion", "appeal", "answer", "counterclaim", "pleading")
_03B_PURPOSES = {"operative_pleading", "motion", "brief"}


def _should_run_03b(document_id: str) -> bool:
    """Determine if 03B (legal structure extraction) should run on this document."""
    doc = _fetch_doc(document_id)
    doc_type       = (doc.get("document_type")  or "").lower()
    filing_purpose = (doc.get("filing_purpose") or "").lower()
    if any(doc_type.startswith(p) for p in _03B_PREFIXES):
        return True
    if filing_purpose in _03B_PURPOSES:
        return True
    # Exhibits that are themselves complaints/briefs filed as historical context
    if filing_purpose == "historical_context" and any(p in doc_type for p in _03B_PREFIXES):
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Phase 2: AST construction pipeline.")
    parser.add_argument("--file_name", required=True, help="Document stem (file_name in Supabase)")
    args = parser.parse_args()

    file_name             = args.file_name
    document_id, case_id  = _lookup_document(file_name)

    print(f"[Phase 2] Starting AST pipeline for '{file_name}' (id={document_id})")
    print("=" * 60)

    # Step 07C: Case-context re-classification (requires case_id; safe to skip if absent)
    if case_id:
        print("[Phase 2] Step 07C — Case-context re-classification…")
        _run("07C_case_context_classification.py", "--document_id", document_id)
    else:
        print("[Phase 2] Step 07C — SKIPPED (no case_id on document)")

    # Step 0: Section refinement (split oversized sections before tree build)
    print("[Phase 2] Step 0 — Refining section structure…")
    _run("00_section_refine.py", "--document_id", document_id)

    # Step 1: Tree reconstruction
    print("[Phase 2] Step 1 — Building parent-child tree…")
    _run("01_AST_tree_build.py", "--document_id", document_id)

    # Step 2: Semantic labeling
    print("[Phase 2] Step 2 — Assigning semantic labels…")
    _run("02_AST_semantic_label.py", "--document_id", document_id)

    # Step 3A: Entity extraction (parties, dates, amounts, courts, judges, attorneys, law firms)
    print("[Phase 2] Step 3A — Extracting entities…")
    _run("03A_entity_extraction.py", "--document_id", document_id)

    # Step 3B: Legal structure extraction (complaints, briefs, motions, appeals, answers)
    if _should_run_03b(document_id):
        print("[Phase 2] Step 3B — Extracting legal structure…")
        _run("03B_legal_structure_extraction.py", "--document_id", document_id)
    else:
        print("[Phase 2] Step 3B — SKIPPED (document type not eligible)")

    # Step 4A: Knowledge graph (intra-document)
    print("[Phase 2] Step 4A — Building knowledge graph…")
    _run("04A_kg_inner_build.py", "--document_id", document_id)

    print("=" * 60)
    print(f"[Phase 2] COMPLETE — '{file_name}' AST + entities + KG ready in Supabase.")

    # Process child exhibit documents if any exist
    try:
        sb = _sb()
    except SystemExit:
        sb = None
    if sb:
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
                if case_id:
                    _run("07C_case_context_classification.py", "--document_id", child_id)
                _run("00_section_refine.py",    "--document_id", child_id)
                _run("01_AST_tree_build.py",    "--document_id", child_id)
                _run("02_AST_semantic_label.py","--document_id", child_id)
                _run("03A_entity_extraction.py", "--document_id", child_id)
                if _should_run_03b(child_id):
                    _run("03B_legal_structure_extraction.py", "--document_id", child_id)
                _run("04A_kg_inner_build.py", "--document_id", child_id)
            print(f"\n[Phase 2] COMPLETE — all exhibits processed.")

    # Phase 3: Embed all sections for the case into the vector store
    if case_id:
        print(f"\n[Phase 3] Starting embedding for case {case_id}...")
        result = subprocess.run(
            [sys.executable, os.path.join(SEARCH_DIR, "main.py"), "--case_id", case_id],
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        if result.returncode != 0:
            print(f"[Phase 3] WARNING: Embedding step failed (exit {result.returncode}). "
                  "Pipeline data is intact — re-run: "
                  f"python backend/03_SEARCH/main.py --case_id {case_id}")
        else:
            print(f"[Phase 3] COMPLETE — case {case_id} is search-ready.")
    else:
        print(
            "\n[Phase 3] SKIPPED — document has no case_id set. "
            "Assign a case_id in Supabase, then run: "
            f"python backend/03_SEARCH/main.py --case_id <case_uuid>"
        )

    # Phase 4 refresh: re-generate the case summary and run the full checklist
    # now that extractions, legal structure, and the KG are all in Supabase.
    # Runs as a background subprocess so 02_MIDDLE exits immediately after
    # kicking it off — the refresh uses Gemini independently without overlap.
    if case_id:
        refresh_script = os.path.join(
            os.path.dirname(__file__), '..', '04_AGENTIC_ARCHITECTURE',
            'refresh_after_extraction.py',
        )
        log_dir  = os.path.join(os.path.dirname(__file__), '..', 'data_storage', 'logs')
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"refresh_{case_id[:8]}.log")
        print(f"\n[Phase 4] Triggering post-extraction refresh for case {case_id}...")
        print(f"[Phase 4] Refresh log → data_storage/logs/refresh_{case_id[:8]}.log")
        with open(log_path, 'a') as lf:
            subprocess.Popen(
                [sys.executable, refresh_script, '--case_id', case_id],
                stdout=lf,
                stderr=lf,
            )
        print("[Phase 4] Summary refresh + checklist started in background.")
    else:
        print(
            "\n[Phase 4] SKIPPED — no case_id. "
            "Run manually: python backend/04_AGENTIC_ARCHITECTURE/refresh_after_extraction.py "
            "--case_id <uuid>"
        )


if __name__ == "__main__":
    main()
