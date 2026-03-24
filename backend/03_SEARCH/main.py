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


def main():
    parser = argparse.ArgumentParser(
        description="Phase 3: Embed case sections into the vector store."
    )
    parser.add_argument("--case_id", required=True,
                        help="UUID of the case whose sections should be embedded")
    parser.add_argument("--force", action="store_true",
                        help="Re-embed all sections even if already up to date")
    args = parser.parse_args()

    print(f"[Phase 3] Starting embedding pipeline for case '{args.case_id}'")
    print("=" * 60)

    embed_args = [args.case_id]
    if args.force:
        embed_args.append("--force")

    print("[Phase 3] Step 1 — Embedding sections...")
    _run("01_embed_sections.py", *embed_args)

    print("=" * 60)
    print(f"[Phase 3] COMPLETE — case '{args.case_id}' is now search-ready.")


if __name__ == "__main__":
    main()
