"""
Backfill script: create active_case corpus per existing case,
link documents to their corpus, and verify data integrity.

Run AFTER migrations 001-003 have been applied.
Run BEFORE migration 005 (RLS policies) if you want to verify without RLS interference.

Usage:
    python backend/scripts/backfill_multi_tenant.py --dry-run
    python backend/scripts/backfill_multi_tenant.py
"""
import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))


def _get_sb():
    from supabase import create_client
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )


DEFAULT_FIRM_ID = "00000000-0000-4000-a000-000000000001"


def backfill(*, dry_run: bool = False) -> dict:
    sb = _get_sb()
    stats = {
        "cases_found": 0,
        "corpora_created": 0,
        "documents_linked": 0,
        "orphan_documents": 0,
        "agent_responses_updated": 0,
        "dps_updated": 0,
        "validation_passed": True,
    }

    # ── Step 1: Get all cases ────────────────────────────────────────────
    cases_resp = sb.table("cases").select("id, case_name, firm_id").execute()
    cases = cases_resp.data or []
    stats["cases_found"] = len(cases)
    print(f"[backfill] Found {len(cases)} cases")

    # ── Step 2: Create active_case corpus per case ───────────────────────
    for case in cases:
        # Check if corpus already exists for this case
        existing_resp = (
            sb.table("corpus")
            .select("id")
            .eq("name", case["case_name"])
            .eq("firm_id", case["firm_id"])
            .eq("type", "active_case")
            .execute()
        )
        existing = existing_resp.data[0] if existing_resp.data else None
        if existing:
            corpus_id = existing["id"]
            print(f"  [skip] Corpus already exists for case '{case['case_name']}'")
        else:
            if dry_run:
                print(f"  [dry-run] Would create corpus for case '{case['case_name']}'")
                corpus_id = None
            else:
                resp = (
                    sb.table("corpus")
                    .insert({
                        "name": case["case_name"],
                        "type": "active_case",
                        "firm_id": case["firm_id"],
                        "is_active_workspace": True,
                    })
                    .execute()
                )
                corpus_id = resp.data[0]["id"]
                stats["corpora_created"] += 1
                print(f"  [OK] Created corpus '{case['case_name']}' -> {corpus_id}")

        # ── Step 3: Link documents to their case's corpus ────────────────
        if corpus_id:
            docs_resp = (
                sb.table("documents")
                .select("id, file_name")
                .eq("case_id", case["id"])
                .is_("corpus_id", "null")
                .execute()
            )
            docs = docs_resp.data or []
            if docs:
                if dry_run:
                    print(f"  [dry-run] Would link {len(docs)} documents to corpus")
                else:
                    for doc in docs:
                        sb.table("documents").update(
                            {"corpus_id": corpus_id}
                        ).eq("id", doc["id"]).execute()
                    stats["documents_linked"] += len(docs)
                    print(f"  [OK] Linked {len(docs)} documents to corpus")

    # ── Step 4: Check for orphan documents ───────────────────────────────
    orphans_resp = (
        sb.table("documents")
        .select("id, file_name")
        .is_("corpus_id", "null")
        .is_("case_id", "null")
        .execute()
    )
    orphans = orphans_resp.data or []
    stats["orphan_documents"] = len(orphans)
    if orphans:
        print(f"\n[WARN] {len(orphans)} orphan documents (no corpus, no case):")
        for o in orphans[:10]:
            print(f"  - {o['file_name']} ({o['id']})")

    # ── Step 5: Backfill firm_id on agent_responses ──────────────────────
    ar_resp = (
        sb.table("agent_responses")
        .select("id, case_id")
        .is_("firm_id", "null")
        .execute()
    )
    ar_nulls = ar_resp.data or []
    if ar_nulls:
        if dry_run:
            print(f"[dry-run] Would update {len(ar_nulls)} agent_responses with firm_id")
        else:
            case_firm_map = {c["id"]: c["firm_id"] for c in cases}
            for ar in ar_nulls:
                firm = case_firm_map.get(ar["case_id"], DEFAULT_FIRM_ID)
                sb.table("agent_responses").update(
                    {"firm_id": firm}
                ).eq("id", ar["id"]).execute()
            stats["agent_responses_updated"] = len(ar_nulls)
            print(f"[OK] Updated {len(ar_nulls)} agent_responses with firm_id")

    # ── Step 6: Backfill firm_id on document_processing_steps ────────────
    dps_resp = (
        sb.table("document_processing_steps")
        .select("id, case_id")
        .is_("firm_id", "null")
        .execute()
    )
    dps_nulls = dps_resp.data or []
    if dps_nulls:
        if dry_run:
            print(f"[dry-run] Would update {len(dps_nulls)} dps rows with firm_id")
        else:
            case_firm_map = {c["id"]: c["firm_id"] for c in cases}
            for dps in dps_nulls:
                firm = case_firm_map.get(dps.get("case_id"), DEFAULT_FIRM_ID)
                sb.table("document_processing_steps").update(
                    {"firm_id": firm}
                ).eq("id", dps["id"]).execute()
            stats["dps_updated"] = len(dps_nulls)
            print(f"[OK] Updated {len(dps_nulls)} dps rows with firm_id")

    # ── Validation ───────────────────────────────────────────────────────
    print("\n=== Validation ===")

    # Check: no cases with NULL firm_id
    null_firm_cases = (
        sb.table("cases").select("id").is_("firm_id", "null").execute()
    )
    if null_firm_cases.data:
        print(f"[FAIL] {len(null_firm_cases.data)} cases with NULL firm_id")
        stats["validation_passed"] = False
    else:
        print("[OK] All cases have firm_id")

    # Check: documents with case_id should have corpus_id
    unlinked = (
        sb.table("documents")
        .select("id", count="exact")
        .is_("corpus_id", "null")
        .not_.is_("case_id", "null")
        .execute()
    )
    if unlinked.count and unlinked.count > 0:
        print(f"[WARN] {unlinked.count} documents with case_id but no corpus_id")
    else:
        print("[OK] All case documents linked to corpus")

    print(f"\n=== Summary ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill multi-tenant data")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without making changes")
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)
