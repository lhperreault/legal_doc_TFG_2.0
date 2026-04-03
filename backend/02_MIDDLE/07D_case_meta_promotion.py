"""
07D_case_meta_promotion.py — Case Metadata Promotion from Extracted Entities

Reads party, court, and judge entities already extracted by 03A and promotes
the highest-confidence values to the `cases` table — but ONLY for fields that
are currently null or empty.

Fields it can populate:
    our_client      — from the plaintiff (or defendant if we represent defendant)
    opposing_party  — from the defendant (or plaintiff if we represent defendant)
    court_name      — from the highest-confidence court entity
    judge_name      — from the highest-confidence judge entity
    case_name       — inferred as "Plaintiff v. Defendant" if currently null

It never overwrites a field that already has a value. The user always wins.

Must run AFTER 03A_entity_extraction.py.

Usage:
    python 07D_case_meta_promotion.py --document_id <uuid>
    python 07D_case_meta_promotion.py --document_id <uuid> --dry_run
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

# Minimum confidence to accept a party/court/judge name.
_MIN_CONFIDENCE = 0.65

# Roles that represent the opposing side for a plaintiff-perspective case.
_OUR_ROLES_AS_PLAINTIFF   = {"plaintiff", "appellant"}
_THEIR_ROLES_AS_PLAINTIFF = {"defendant", "appellee"}


def _best_name_for_role(
    extractions: list[dict],
    extraction_type: str,
    role_filter: set[str] | None = None,
) -> str | None:
    """Pick the most-mentioned, highest-confidence entity name.

    For party extractions, role_filter restricts to specific role_in_document values.
    For court/judge, role_filter is None and we just pick the best entity.
    """
    candidates: dict[str, list[float]] = defaultdict(list)  # name → [confidence, ...]

    for ext in extractions:
        if ext.get("extraction_type") != extraction_type:
            continue
        conf = float(ext.get("confidence") or 0)
        if conf < _MIN_CONFIDENCE:
            continue
        name = (ext.get("entity_name") or "").strip()
        if not name:
            continue

        if role_filter is not None:
            props = ext.get("properties") or {}
            role  = props.get("role_in_document", "")
            if role not in role_filter:
                continue

        candidates[name].append(conf)

    if not candidates:
        return None

    # Rank: most mentions first, tie-break by average confidence
    best = max(
        candidates.keys(),
        key=lambda n: (len(candidates[n]), sum(candidates[n]) / len(candidates[n])),
    )
    return best


def promote_case_metadata(
    document_id: str,
    sb,
    dry_run: bool = False,
) -> None:
    """Read 03A extractions for this document and update the parent case row."""

    # ── Resolve document → case ───────────────────────────────────────────────
    doc_resp = (
        sb.table("documents")
        .select("id, case_id, document_type, file_name")
        .eq("id", document_id)
        .single()
        .execute()
    )
    doc = doc_resp.data
    if not doc:
        print(f"[07D] Document {document_id} not found — skipping")
        return

    case_id = doc.get("case_id")
    if not case_id:
        print("[07D] Document has no case_id — skipping")
        return

    # ── Fetch current case (to know which fields are already filled) ──────────
    case_resp = (
        sb.table("cases")
        .select("case_name, party_role, our_client, opposing_party, court_name, judge_name")
        .eq("id", case_id)
        .single()
        .execute()
    )
    case = case_resp.data or {}

    # ── Fetch party/court/judge extractions for this document ─────────────────
    ext_resp = (
        sb.table("extractions")
        .select("extraction_type, entity_name, confidence, properties")
        .eq("document_id", document_id)
        .in_("extraction_type", ["party", "court", "judge"])
        .execute()
    )
    extractions = ext_resp.data or []

    if not extractions:
        print(f"[07D] No party/court/judge extractions found for {doc.get('file_name', document_id)}")
        return

    # ── Determine our side based on party_role ────────────────────────────────
    party_role = (case.get("party_role") or "").lower().strip()
    we_are_plaintiff  = party_role in _OUR_ROLES_AS_PLAINTIFF
    we_are_defendant  = party_role in _THEIR_ROLES_AS_PLAINTIFF

    updates: dict[str, str] = {}

    # ── Parties ───────────────────────────────────────────────────────────────
    if we_are_plaintiff or we_are_defendant:
        our_roles   = _OUR_ROLES_AS_PLAINTIFF  if we_are_plaintiff else _THEIR_ROLES_AS_PLAINTIFF
        their_roles = _THEIR_ROLES_AS_PLAINTIFF if we_are_plaintiff else _OUR_ROLES_AS_PLAINTIFF

        if not _nonempty(case.get("our_client")):
            name = _best_name_for_role(extractions, "party", our_roles)
            if name:
                updates["our_client"] = name

        if not _nonempty(case.get("opposing_party")):
            name = _best_name_for_role(extractions, "party", their_roles)
            if name:
                updates["opposing_party"] = name
    # If party_role is unknown we can't safely assign sides —
    # promote courts/judges but leave party fields alone.

    # ── Court ─────────────────────────────────────────────────────────────────
    if not _nonempty(case.get("court_name")):
        court = _best_name_for_role(extractions, "court")
        if court:
            updates["court_name"] = court

    # ── Judge ─────────────────────────────────────────────────────────────────
    if not _nonempty(case.get("judge_name")):
        judge = _best_name_for_role(extractions, "judge")
        if judge:
            updates["judge_name"] = judge

    # ── Case name (infer from parties if null) ────────────────────────────────
    if not _nonempty(case.get("case_name")):
        plaintiff = _best_name_for_role(extractions, "party", {"plaintiff", "appellant"})
        defendant = _best_name_for_role(extractions, "party", {"defendant", "appellee"})
        if plaintiff and defendant:
            updates["case_name"] = f"{plaintiff} v. {defendant}"
        elif plaintiff or defendant:
            updates["case_name"] = plaintiff or defendant  # type: ignore[assignment]

    # ── Apply ─────────────────────────────────────────────────────────────────
    if not updates:
        print("[07D] All metadata fields already populated — nothing to promote")
        return

    print(f"[07D] Promoting {len(updates)} field(s) to cases table:")
    for k, v in updates.items():
        print(f"      {k}: {v!r}")

    if not dry_run:
        sb.table("cases").update(updates).eq("id", case_id).execute()

    tag = "DRY RUN — " if dry_run else ""
    print(f"[07D] {tag}SUCCESS — case metadata updated for case {case_id}")


def _nonempty(value) -> bool:
    """Return True if the value is a non-blank string."""
    return bool(value and str(value).strip())


def main():
    parser = argparse.ArgumentParser(description="07D: Promote extracted entities to case metadata")
    parser.add_argument("--document_id", required=True)
    parser.add_argument("--dry_run", action="store_true")
    args = parser.parse_args()

    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)
    sb = create_client(url, key)

    promote_case_metadata(args.document_id, sb, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
