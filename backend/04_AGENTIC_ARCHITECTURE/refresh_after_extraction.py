"""
refresh_after_extraction.py — Post-02_MIDDLE refresh for Phase 4.

Called automatically at the end of 02_MIDDLE/main.py once entity extraction,
legal structure extraction, and the knowledge graph are written to Supabase.

What it does:
  1. Re-generates the case summary with the enriched extraction data
     (replaces the quick initial summary produced right after Phase 1).
  2. Runs the full two-tier checklist — now complaint agents, contract agents,
     and evidence tools can see allegations, legal_elements, counts, and
     extractions that were not available during the initial pipeline run.

Why it runs here and not earlier:
  - 02_MIDDLE populates the tables that the complaint/contract/evidence tools
    depend on (allegations, legal_elements, counts, extractions, kg nodes).
  - 02_MIDDLE also re-runs Phase 3 (section embedding) at its own end, so
    the vector store is up to date by the time this script starts.
  - Running the checklist (20+ Gemini calls) concurrently with 02_MIDDLE
    (which also uses Gemini for semantic labeling and entity extraction)
    risks hitting API rate limits. Running it here, after 02_MIDDLE exits,
    avoids that entirely.

Usage:
    python backend/04_AGENTIC_ARCHITECTURE/refresh_after_extraction.py \\
        --case_id "uuid-of-case"
"""

import argparse
import importlib.util as _ilu
import os
import sys

_ARCH_DIR     = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_ARCH_DIR, "..", ".."))

if _ARCH_DIR not in sys.path:
    sys.path.insert(0, _ARCH_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))


def _load_module(name: str, path: str):
    spec = _ilu.spec_from_file_location(name, path)
    mod  = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def refresh(case_id: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  POST-EXTRACTION REFRESH")
    print(f"  Case: {case_id}")
    print(f"{'=' * 60}")

    # ── 1. Re-generate summary (replaces the initial quick summary) ───────────
    print("\n[Refresh] Step 1 — Re-generating case summary with enriched data...")
    doc_summary = _load_module(
        "document_summary",
        os.path.join(_ARCH_DIR, "document_summary.py"),
    )
    result = doc_summary.generate_summary(case_id=case_id, verbose=True, refresh=True)

    if result.get("success"):
        print(f"[Refresh] Summary updated (confidence={result['confidence']:.2f}).")
    else:
        # Non-fatal: log and continue to checklist
        print(f"[Refresh] WARNING: Summary update failed — {result.get('error')}")

    # ── 2. Run the full two-tier checklist ────────────────────────────────────
    print("\n[Refresh] Step 2 — Running full case checklist...")
    checklist_runner = _load_module(
        "checklist_runner",
        os.path.join(_ARCH_DIR, "checklist_runner.py"),
    )
    summary = checklist_runner.run_checklist(
        case_id=case_id,
        skip_addons=False,
        verbose=True,
    )
    checklist_runner._print_checklist_results(summary)

    print(f"\n{'=' * 60}")
    print(f"  REFRESH COMPLETE")
    print(f"  Summary + checklist populated with full extraction data.")
    print(f"{'=' * 60}\n")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Re-generate the case summary and run the full checklist "
            "after 02_MIDDLE has finished extracting entities and legal structure."
        )
    )
    parser.add_argument("--case_id", required=True, help="UUID of the case to refresh.")
    args = parser.parse_args()

    refresh(case_id=args.case_id)


if __name__ == "__main__":
    main()
