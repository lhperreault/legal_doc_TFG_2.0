"""
01_AST_tree_build.py — Phase 2, Step 1: AST Tree Reconstruction

Reads sections for a document from Supabase (ordered by start_page, level),
reconstructs parent-child relationships using a stack-based algorithm,
and writes parent_section_id back to each section row.

Usage:
    python 01_AST_tree_build.py --file_name "Complaint (Epic Games to Apple"
    python 01_AST_tree_build.py --document_id "abc-123-uuid"
"""

import argparse
import os
import sys

from dotenv import load_dotenv
from supabase import create_client

# Load .env from project root (two levels up from backend/02_MIDDLE/)
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))


# ---------------------------------------------------------------------------
# Supabase client
# ---------------------------------------------------------------------------

def _get_client():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)
    return create_client(url, key)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def _resolve_document_id(supabase, args) -> tuple[str, str]:
    """Return (document_id, file_name) from CLI args."""
    if args.document_id:
        resp = supabase.table("documents").select("id, file_name").eq("id", args.document_id).execute()
        if not resp.data:
            print(f"ERROR: No document found with id={args.document_id}")
            sys.exit(1)
        return resp.data[0]["id"], resp.data[0]["file_name"]
    else:
        resp = supabase.table("documents").select("id, file_name").eq("file_name", args.file_name).execute()
        if not resp.data:
            print(f"ERROR: No document found with file_name='{args.file_name}'")
            sys.exit(1)
        return resp.data[0]["id"], resp.data[0]["file_name"]


# ---------------------------------------------------------------------------
# Tree algorithm
# ---------------------------------------------------------------------------

def build_parent_map(sections: list[dict]) -> dict[str, str | None]:
    """
    Stack-based parent reconstruction.
    Returns {section_id: parent_section_id or None}.
    """
    parent_map: dict[str, str | None] = {}
    stack: list[dict] = []  # each entry: {"id": ..., "level": ...}

    for sec in sections:
        sec_id = sec["id"]
        level  = sec.get("level") or 0

        # Pop stack until top has a strictly smaller level
        while stack and stack[-1]["level"] >= level:
            stack.pop()

        parent_map[sec_id] = stack[-1]["id"] if stack else None
        stack.append({"id": sec_id, "level": level})

    return parent_map


def _sort_key(sec: dict):
    """Sort by start_page (nulls last), then by level."""
    sp = sec.get("start_page")
    return (sp is None, sp or 0, sec.get("level") or 0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Build AST parent-child relationships.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--document_id", help="UUID of the document in Supabase")
    group.add_argument("--file_name",   help="file_name stem of the document")
    args = parser.parse_args()

    supabase = _get_client()

    # Resolve document
    document_id, file_name = _resolve_document_id(supabase, args)

    # Fetch sections
    try:
        resp = (
            supabase.table("sections")
            .select("id, level, start_page, created_at")
            .eq("document_id", document_id)
            .execute()
        )
    except Exception as e:
        print(f"ERROR: Failed to fetch sections — {e}")
        sys.exit(1)

    sections = resp.data or []
    if not sections:
        print(f"ERROR: No sections found for document '{file_name}' (id={document_id})")
        sys.exit(1)

    # Sort: start_page ASC (nulls last), level ASC
    sections.sort(key=_sort_key)

    # Build parent map
    parent_map = build_parent_map(sections)

    # Stats
    root_count  = sum(1 for v in parent_map.values() if v is None)
    levels      = [s.get("level") or 0 for s in sections]
    max_depth   = max(levels) if levels else 0

    # Batch update in Supabase (individual updates — PostgREST has no bulk patch)
    errors = 0
    for sec_id, parent_id in parent_map.items():
        try:
            supabase.table("sections").update(
                {"parent_section_id": parent_id}
            ).eq("id", sec_id).execute()
        except Exception as e:
            print(f"  WARNING: Could not update section {sec_id} — {e}")
            errors += 1

    if errors:
        print(f"ERROR: Tree built with {errors} update failure(s) for '{file_name}'.")
        sys.exit(1)

    print(
        f"SUCCESS: Tree built for '{file_name}'. "
        f"{len(sections)} sections processed, "
        f"{root_count} root node(s), "
        f"max depth {max_depth}."
    )


if __name__ == "__main__":
    main()
