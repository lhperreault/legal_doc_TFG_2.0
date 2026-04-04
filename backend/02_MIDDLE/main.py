"""
02_MIDDLE/main.py — Phase 2 orchestrator: AST construction + semantic labeling.

Usage:
    python backend/02_MIDDLE/main.py --file_name "Complaint (Epic Games to Apple"
    python backend/02_MIDDLE/main.py --file_name "Complaint_abc12345" --document_id <uuid>
    python backend/02_MIDDLE/main.py --file_name "Exhibit_A_abc12345" --no-recurse
"""

import argparse
import concurrent.futures
import os
import subprocess
import sys

from dotenv import load_dotenv

# Force UTF-8 output on Windows so Unicode characters in log messages
# (→, ✓, etc.) never cause UnicodeEncodeError on cp1252 terminals/log files.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Load .env from project root
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

PHASE_DIR   = os.path.dirname(os.path.abspath(__file__))
SEARCH_DIR  = os.path.join(PHASE_DIR, '..', '03_SEARCH')


def _run_safe(script_name: str, *args) -> str:
    """
    Like _run but raises RuntimeError instead of sys.exit.
    Safe to call from worker threads (ThreadPoolExecutor).
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, os.path.join(PHASE_DIR, script_name)] + list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    output = result.stdout.strip()
    if output:
        print(output)
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)

    last_line = output.splitlines()[-1] if output else ""
    if last_line.startswith("ERROR") or result.returncode != 0:
        raise RuntimeError(f"{script_name} failed (exit {result.returncode})")

    return output


def _sb():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)
    return create_client(url, key)


def _upsert_step(
    document_id: str,
    case_id: str | None,
    step_name: str,
    display_label: str,
    status: str,
    error: str | None = None,
) -> None:
    """Write a step row for the frontend Realtime checklist."""
    if not document_id:
        return
    try:
        from supabase import create_client
        from datetime import datetime, timezone
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            return
        sb = create_client(url, key)
        now = datetime.now(timezone.utc).isoformat()
        row: dict = {
            "document_id":   document_id,
            "step_name":     step_name,
            "display_label": display_label,
            "status":        status,
        }
        if case_id:
            row["case_id"] = case_id
        if status == "running":
            row["started_at"] = now
        if status in ("done", "error"):
            row["completed_at"] = now
        if error:
            row["error_message"] = error
        sb.table("document_processing_steps").upsert(
            row, on_conflict="document_id,step_name"
        ).execute()
    except Exception as e:
        print(f"[steps] WARNING: could not write step '{step_name}': {e}", file=sys.stderr)


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
    if filing_purpose == "historical_context" and any(p in doc_type for p in _03B_PREFIXES):
        return True
    return False


def _process_one_document(document_id: str, file_name: str, case_id: str | None) -> None:
    """
    Run the full Phase 2 pipeline for a single document.
    Steps 03A and 03B run in parallel after AST labeling.
    """
    print(f"\n[Phase 2] === Processing '{file_name}' (id={document_id}) ===")

    # Step 07C: Case-context re-classification
    if case_id:
        print("[Phase 2] Step 07C — Case-context re-classification…")
        _run_safe("07C_case_context_classification.py", "--document_id", document_id)

    # Step 0: Section refinement
    _upsert_step(document_id, case_id, "section_refine", "Refining section structure", "running")
    print("[Phase 2] Step 0 — Refining section structure…")
    _run_safe("00_section_refine.py", "--document_id", document_id)
    _upsert_step(document_id, case_id, "section_refine", "Refining section structure", "done")

    # Step 1: Tree reconstruction
    _upsert_step(document_id, case_id, "ast_tree", "Building document tree", "running")
    print("[Phase 2] Step 1 — Building parent-child tree…")
    _run_safe("01_AST_tree_build.py", "--document_id", document_id)
    _upsert_step(document_id, case_id, "ast_tree", "Building document tree", "done")

    # Step 2: Semantic labeling
    _upsert_step(document_id, case_id, "semantic_labeling", "Semantic labeling", "running")
    print("[Phase 2] Step 2 — Assigning semantic labels…")
    _run_safe("02_AST_semantic_label.py", "--document_id", document_id)
    _upsert_step(document_id, case_id, "semantic_labeling", "Semantic labeling", "done")

    # Steps 3A + 3B: Entity extraction and legal structure — run in PARALLEL
    # Both only require AST labels; neither depends on the other.
    run_3b = _should_run_03b(document_id)

    _upsert_step(document_id, case_id, "entity_extraction", "Entity extraction", "running")
    if run_3b:
        _upsert_step(document_id, case_id, "legal_structure", "Legal structure analysis", "running")

    print("[Phase 2] Steps 3A/3B — Extracting entities" +
          (" + legal structure (parallel)…" if run_3b else "…"))

    def _run_3a():
        _run_safe("03A_entity_extraction.py", "--document_id", document_id)

    def _run_3b():
        _run_safe("03B_legal_structure_extraction.py", "--document_id", document_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f3a = ex.submit(_run_3a)
        f3b = ex.submit(_run_3b) if run_3b else None
        # Propagate any exceptions
        f3a.result()
        if f3b:
            f3b.result()

    _upsert_step(document_id, case_id, "entity_extraction", "Entity extraction", "done")
    if run_3b:
        _upsert_step(document_id, case_id, "legal_structure", "Legal structure analysis", "done")

    # Step 07D: Promote high-confidence entities to cases row
    if case_id:
        print("[Phase 2] Step 07D — Promoting case metadata from extracted entities…")
        _run_safe("07D_case_meta_promotion.py", "--document_id", document_id)

    # Step 4A: Knowledge graph (intra-document)
    _upsert_step(document_id, case_id, "kg_build", "Knowledge graph", "running")
    print("[Phase 2] Step 4A — Building knowledge graph…")
    _run_safe("04A_kg_inner_build.py", "--document_id", document_id)
    _upsert_step(document_id, case_id, "kg_build", "Knowledge graph", "done")

    print(f"[Phase 2] === COMPLETE — '{file_name}' ===")


def main():
    parser = argparse.ArgumentParser(description="Phase 2: AST construction pipeline.")
    parser.add_argument("--file_name",   required=True,
                        help="Document stem (file_name in Supabase)")
    parser.add_argument("--document_id", default="",
                        help="Optional pre-resolved document UUID (skips lookup if provided)")
    parser.add_argument("--no-recurse",  action="store_true",
                        help="Skip child exhibit processing (used when exhibits are fired separately)")
    args = parser.parse_args()

    file_name = args.file_name

    # Resolve document_id — prefer the pre-supplied value to avoid an extra DB round-trip
    if args.document_id:
        document_id = args.document_id
        _, case_id  = _lookup_document(file_name)   # still need case_id
    else:
        document_id, case_id = _lookup_document(file_name)

    print(f"[Phase 2] Starting AST pipeline for '{file_name}' (id={document_id})")
    print("=" * 60)

    try:
        _process_one_document(document_id, file_name, case_id)
    except RuntimeError as e:
        print(f"[Phase 2] FAILED: {e}")
        sys.exit(1)

    print("=" * 60)
    print(f"[Phase 2] COMPLETE — '{file_name}' AST + entities + KG ready in Supabase.")

    # ── Child exhibit processing (parallel) ────────────────────────────────────
    if not args.no_recurse:
        try:
            sb = _sb()
        except SystemExit:
            sb = None

        if sb:
            child_resp = (
                sb.table("documents")
                .select("id, file_name")
                .eq("parent_document_id", document_id)
                .execute()
            )
            children = child_resp.data or []

            # Filter out tiny exhibits not worth the API calls
            eligible = []
            for child in children:
                child_id   = child["id"]
                child_name = child["file_name"]
                section_resp = sb.table("sections").select("section_text").eq("document_id", child_id).execute()
                total_chars  = sum(len(s.get("section_text") or "") for s in (section_resp.data or []))
                if total_chars < 2000:
                    print(f"\n[Phase 2] Skipping exhibit '{child_name}' — too short ({total_chars} chars)")
                    continue
                eligible.append((child_id, child_name, total_chars))

            if eligible:
                print(f"\n[Phase 2] Processing {len(eligible)} exhibit(s) in parallel…")
                max_workers = min(len(eligible), 3)  # cap at 3 parallel to respect API limits
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                    futures = {
                        ex.submit(_process_one_document, cid, cname, case_id): cname
                        for cid, cname, _ in eligible
                    }
                    for fut in concurrent.futures.as_completed(futures):
                        cname = futures[fut]
                        try:
                            fut.result()
                        except Exception as exc:
                            print(f"[Phase 2] WARNING: exhibit '{cname}' failed: {exc}", file=sys.stderr)

                print(f"\n[Phase 2] COMPLETE — all exhibits processed.")

    # ── Phase 3: Embed all sections for the case ────────────────────────────────
    if case_id:
        print(f"\n[Phase 3] Starting embedding for case {case_id}...")
        _p3_env = os.environ.copy()
        _p3_env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, os.path.join(SEARCH_DIR, "main.py"),
             "--case_id", case_id, "--document_id", document_id],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_p3_env,
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

    # ── Phase 4 refresh: re-generate summary + run full checklist ──────────────
    # Runs as a background subprocess after all extraction + embedding is done.
    if case_id:
        refresh_script = os.path.join(
            os.path.dirname(__file__), '..', '04_AGENTIC_ARCHITECTURE',
            'refresh_after_extraction.py',
        )
        log_dir  = os.path.join(os.path.dirname(__file__), '..', 'data_storage', 'logs')
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"refresh_{case_id[:8]}.log")
        print(f"\n[Phase 4] Triggering post-extraction refresh for case {case_id}...")
        print(f"[Phase 4] Refresh log -> data_storage/logs/refresh_{case_id[:8]}.log")
        with open(log_path, 'a', encoding='utf-8') as lf:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            subprocess.Popen(
                [sys.executable, refresh_script, '--case_id', case_id,
                 '--document_id', document_id],
                stdout=lf,
                stderr=lf,
                env=env,
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
