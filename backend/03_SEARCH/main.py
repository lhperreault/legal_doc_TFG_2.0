"""
03_SEARCH/main.py — Phase 3 orchestrator: section embedding.

Called at the end of the full pipeline after 02_MIDDLE/main.py completes.
Embeds all sections for a case into the vector store (section_embeddings table).

Usage:
    python backend/03_SEARCH/main.py --case_id "uuid-of-case"
    python backend/03_SEARCH/main.py --case_id "uuid-of-case" --force
"""

import argparse
import os
import subprocess
import sys
import time

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

SEARCH_DIR = os.path.dirname(os.path.abspath(__file__))


def _run(script_name: str, *args) -> str:
    """
    Run a Phase 3 script, capture stdout, and check for SUCCESS/ERROR.
    Returns stdout text. Exits on ERROR.
    """
    result = subprocess.run(
        [sys.executable, os.path.join(SEARCH_DIR, script_name)] + list(args),
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
        print(f"[Phase 3] FAILED at {script_name}. Stopping.")
        sys.exit(result.returncode or 1)

    return output


def _upsert_step(
    document_id: str,
    case_id: str | None,
    step_name: str,
    display_label: str,
    status: str,
) -> None:
    """Write a progress step row for the frontend Realtime checklist."""
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
        sb.table("document_processing_steps").upsert(
            row, on_conflict="document_id,step_name"
        ).execute()
    except Exception as e:
        print(f"[steps] WARNING: could not write step '{step_name}': {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Phase 3: Embed case sections into the vector store."
    )
    parser.add_argument("--case_id", required=True,
                        help="UUID of the case whose sections should be embedded")
    parser.add_argument("--document_id", default="",
                        help="UUID of the triggering document (for step tracking)")
    parser.add_argument("--force", action="store_true",
                        help="Re-embed all sections even if already up to date")
    args = parser.parse_args()

    document_id = args.document_id or None

    print(f"[Phase 3] Starting embedding pipeline for case '{args.case_id}'")
    print("=" * 60)

    _upsert_step(document_id, args.case_id, "embeddings", "Vector embeddings", "running")

    embed_args = [args.case_id]
    if args.force:
        embed_args.append("--force")

    print("[Phase 3] Step 1 — Embedding sections...")
    t0 = time.perf_counter()
    _run("01_embed_sections.py", *embed_args)
    embed_elapsed = time.perf_counter() - t0

    _upsert_step(document_id, args.case_id, "embeddings", "Vector embeddings", "done")

    print("=" * 60)
    print(f"[Phase 3] COMPLETE — case '{args.case_id}' is now search-ready.")
    print(f"[Phase 3] ⏱  01_embed_sections: {embed_elapsed:.1f}s")

    # Fire Phase 4 summary now that embeddings exist — runs in background so
    # Phase 3 exits immediately. Phase 2's refresh_after_extraction will later
    # replace this with an enriched summary once entities + KG are ready.
    if document_id:
        phase4_script = os.path.join(
            os.path.dirname(SEARCH_DIR), "04_AGENTIC_ARCHITECTURE", "document_summary.py"
        )
        log_dir  = os.path.join(os.path.dirname(SEARCH_DIR), "data_storage", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"04_summary_{args.case_id[:8]}.log")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        import subprocess as _sp
        with open(log_path, "a", encoding="utf-8") as lf:
            _sp.Popen(
                [sys.executable, phase4_script,
                 "--case_id", args.case_id,
                 "--document_id", document_id],
                stdout=lf, stderr=lf, env=env,
            )
        print(f"[Phase 4] Case summary started in background → {log_path}")


if __name__ == "__main__":
    main()
