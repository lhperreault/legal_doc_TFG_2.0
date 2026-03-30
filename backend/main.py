"""
backend/main.py — Root orchestrator (production entry point).
Runs phases: 01_INITIAL → 03_SEARCH → 04_AGENTIC_ARCHITECTURE
             02_MIDDLE runs silently in the background after Phase 1.

Usage (from project root):
    python backend/main.py <filename> [--case_id <uuid>]
"""
import argparse
import os
import subprocess
import sys

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))

PHASE1_MAIN      = os.path.join(BACKEND_DIR, "01_INITIAL",              "main.py")
PHASE2_MAIN      = os.path.join(BACKEND_DIR, "02_MIDDLE",               "main.py")
PHASE3_MAIN      = os.path.join(BACKEND_DIR, "03_SEARCH",               "main.py")
PHASE4_SUMMARY   = os.path.join(BACKEND_DIR, "04_AGENTIC_ARCHITECTURE", "document_summary.py")


def _run_phase(phase_main: str, *args):
    result = subprocess.run([sys.executable, phase_main] + list(args))
    if result.returncode != 0:
        print(f"[PIPELINE] Phase failed: {phase_main}")
        sys.exit(result.returncode)


def _run_background(phase_main: str, *args):
    """Fire-and-forget: start a subprocess and do not wait for it."""
    subprocess.Popen(
        [sys.executable, phase_main] + list(args),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the full legal document pipeline.")
    parser.add_argument("filename",   help="Document filename to process")
    parser.add_argument("--case_id",  default=None, help="Case UUID (required for Phase 3 & 4)")
    args = parser.parse_args()

    filename  = args.filename
    case_id   = args.case_id
    file_stem = os.path.splitext(filename)[0]

    # Phase 1: Initial processing (intake → text → classify → TOC → Supabase)
    cmd1 = [filename]
    if case_id:
        cmd1 += ["--case-id", case_id]
    _run_phase(PHASE1_MAIN, *cmd1)

    # Phase 2: AST + entity extraction — runs silently in the background
    print("[PIPELINE] Phase 2 (02_MIDDLE) started in background.")
    _run_background(PHASE2_MAIN, "--file_name", file_stem)

    if not case_id:
        print("[PIPELINE] No --case_id provided — skipping Phase 3 & 4.")
        sys.exit(0)

    # Phase 3: Section embedding → vector store (search-ready)
    _run_phase(PHASE3_MAIN, "--case_id", case_id)

    # Phase 4: Generate professional case summary (populates the legal pad UI immediately)
    # The full checklist is triggered by 02_MIDDLE when it finishes — this avoids
    # running 20+ Gemini checklist calls concurrently with 02_MIDDLE's own Gemini work.
    _run_phase(PHASE4_SUMMARY, "--case_id", case_id)
