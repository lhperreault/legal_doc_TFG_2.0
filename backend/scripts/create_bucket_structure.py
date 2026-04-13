"""
Create the bucket folder structure and routing criteria in Supabase Storage.

Bucket architecture:
  case-files    -> full pipeline (phases 1-3): client docs, pleadings, contracts, filings
  external-law  -> embed only (phase 3): case law, legislation, regulations from online sources
  reference     -> embed only (phase 3): firm knowledge, templates, precedents, memos
  intake-queue  -> staging: files land here first, get classified, then moved to correct bucket

Folder paths:
  case-files/{case_id}/{subfolder}/
  external-law/{case_id}/{subfolder}/
  reference/{firm_id}/{subfolder}/
  intake-queue/{case_id}/{subfolder}/
"""

from supabase import create_client
import json
import sys

import requests as http_requests

SUPABASE_URL = "https://wjxglyjitpqnldblxbew.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6IndqeGdseWppdHBxbmxkYmx4YmV3Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MzcxMzQxOCwiZXhwIjoyMDg5Mjg5NDE4fQ.9bUqLTde-gffQR-Ns5Ke1CGVbSrtic2EkWjDXno1nuk"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def run_sql(sql):
    """Execute raw SQL via Supabase REST API (pg-meta endpoint)."""
    resp = http_requests.post(
        f"{SUPABASE_URL}/rest/v1/rpc/",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        },
        json={"query": sql},
    )
    # Fallback: use the SQL endpoint directly
    if resp.status_code != 200:
        resp2 = http_requests.post(
            f"{SUPABASE_URL}/pg/query",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
            },
            json={"query": sql},
        )
        return resp2
    return resp

# Use existing case and firm IDs
CASE_ID = "a724c9ac-8faa-4459-9cf9-8b6e1dcc0efe"  # Epic vs Apple
FIRM_ID = "00000000-0000-4000-a000-000000000001"


def make_criteria(data):
    return json.dumps(data, indent=2).encode("utf-8")


def upload(bucket, path, content):
    try:
        supabase.storage.from_(bucket).upload(
            path, content, {"content-type": "application/json"}
        )
        print(f"  OK  {bucket}/{path}")
    except Exception as e:
        err = str(e)
        if "Duplicate" in err or "already exists" in err.lower():
            print(f"  EXISTS  {bucket}/{path}")
        else:
            print(f"  ERR {bucket}/{path} - {err[:120]}")


# ── CASE-FILES: full pipeline (phases 1-3) ──
print("\n=== CASE-FILES ===")
print("Full pipeline processing. Path: case-files/{case_id}/{subfolder}/\n")

case_file_folders = {
    "pleadings": {
        "description": "Complaints, answers, motions, briefs filed with court",
        "pipeline": "full",
        "phases": [1, 2, 3],
        "document_types": [
            "Pleading - Complaint",
            "Pleading - Answer",
            "Pleading - Motion",
            "Brief",
            "Pleading - Reply",
            "Pleading - Cross-Motion",
        ],
        "priority": "high",
    },
    "contracts": {
        "description": "Agreements, contracts, amendments, term sheets, licenses",
        "pipeline": "full",
        "phases": [1, 2, 3],
        "document_types": [
            "Contract - Agreement",
            "Contract - Amendment",
            "Contract - License",
            "Contract - Term Sheet",
            "Contract - NDA",
        ],
        "priority": "high",
    },
    "discovery": {
        "description": "Interrogatories, depositions, requests for production, subpoenas",
        "pipeline": "full",
        "phases": [1, 2, 3],
        "document_types": [
            "Discovery - Interrogatory",
            "Discovery - Deposition",
            "Discovery - Request for Production",
            "Discovery - Subpoena",
        ],
        "priority": "medium",
    },
    "evidence": {
        "description": "Exhibits, declarations, affidavits, expert reports",
        "pipeline": "full",
        "phases": [1, 2, 3],
        "document_types": [
            "Evidence - Exhibit",
            "Evidence - Declaration",
            "Evidence - Affidavit",
            "Evidence - Expert Report",
        ],
        "priority": "high",
    },
    "correspondence": {
        "description": "Letters, emails, notices between parties or counsel",
        "pipeline": "full",
        "phases": [1, 2, 3],
        "document_types": [
            "Correspondence - Letter",
            "Correspondence - Email",
            "Correspondence - Notice",
        ],
        "priority": "low",
    },
    "court-orders": {
        "description": "Orders, rulings, judgments, scheduling orders from court",
        "pipeline": "full",
        "phases": [1, 2, 3],
        "document_types": [
            "Court Order",
            "Ruling",
            "Judgment",
            "Scheduling Order",
        ],
        "priority": "high",
    },
    "administrative": {
        "description": "Civil cover sheets, case summaries, docket entries, filing receipts",
        "pipeline": "full",
        "phases": [1, 2, 3],
        "document_types": [
            "Administrative - Case Summary",
            "Administrative - Docket",
            "Administrative - Filing Receipt",
            "Administrative - Civil Cover Sheet",
        ],
        "priority": "low",
    },
}

for folder, criteria in case_file_folders.items():
    criteria["folder"] = folder
    upload("case-files", f"{CASE_ID}/{folder}/.criteria.json", make_criteria(criteria))


# ── EXTERNAL-LAW: embed only (phase 3) ──
print("\n=== EXTERNAL-LAW ===")
print("Embed only. Path: external-law/{case_id}/{subfolder}/\n")

external_law_folders = {
    "case-law": {
        "description": "Court opinions, rulings from other cases used as precedent",
        "pipeline": "embed-only",
        "phases": [3],
        "source": "external",
        "priority": "medium",
    },
    "legislation": {
        "description": "Statutes, codes, regulations relevant to the case",
        "pipeline": "embed-only",
        "phases": [3],
        "source": "external",
        "priority": "medium",
    },
    "legal-commentary": {
        "description": "Law review articles, treatises, legal analysis from external sources",
        "pipeline": "embed-only",
        "phases": [3],
        "source": "external",
        "priority": "low",
    },
}

for folder, criteria in external_law_folders.items():
    criteria["folder"] = folder
    upload("external-law", f"{CASE_ID}/{folder}/.criteria.json", make_criteria(criteria))


# ── REFERENCE: embed only, firm-wide ──
print("\n=== REFERENCE ===")
print("Embed only, firm-wide. Path: reference/{firm_id}/{subfolder}/\n")

reference_folders = {
    "templates": {
        "description": "Firm document templates, standard clauses, boilerplate",
        "pipeline": "embed-only",
        "phases": [3],
        "scope": "firm-wide",
        "priority": "low",
    },
    "precedents": {
        "description": "Past case work, successful strategies, firm memos",
        "pipeline": "embed-only",
        "phases": [3],
        "scope": "firm-wide",
        "priority": "medium",
    },
    "knowledge": {
        "description": "Firm knowledge base, training materials, practice area guides",
        "pipeline": "embed-only",
        "phases": [3],
        "scope": "firm-wide",
        "priority": "low",
    },
}

for folder, criteria in reference_folders.items():
    criteria["folder"] = folder
    upload("reference", f"{FIRM_ID}/{folder}/.criteria.json", make_criteria(criteria))


# ── INTAKE-QUEUE: staging area ──
print("\n=== INTAKE-QUEUE ===")
print("Staging area. Path: intake-queue/{case_id}/{subfolder}/\n")

intake_folders = {
    "unclassified": {
        "description": "Files awaiting classification - uploaded via chat, email, or bulk",
        "pipeline": "classify-then-route",
        "next_steps": ["classify document type", "route to correct bucket/folder"],
        "priority": "immediate",
    },
    "bulk": {
        "description": "Bulk uploads from Dropbox/GDrive - processed in batch",
        "pipeline": "classify-then-route",
        "next_steps": ["classify document type", "route to correct bucket/folder"],
        "source": "dropbox/gdrive",
        "priority": "immediate",
    },
}

for folder, criteria in intake_folders.items():
    criteria["folder"] = folder
    upload("intake-queue", f"{CASE_ID}/{folder}/.criteria.json", make_criteria(criteria))


# ── ROUTING CRITERIA TABLE ──
# Create a summary table in the DB for quick lookup
print("\n=== CREATING ROUTING CRITERIA TABLE ===\n")

create_table_sql = """
CREATE TABLE IF NOT EXISTS bucket_routing_criteria (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    bucket TEXT NOT NULL,
    folder TEXT NOT NULL,
    description TEXT,
    pipeline TEXT NOT NULL,           -- 'full', 'embed-only', 'classify-then-route'
    phases INTEGER[],                 -- which pipeline phases to run
    document_types TEXT[],            -- matching document types (for classification)
    source TEXT DEFAULT 'internal',   -- 'internal', 'external', 'dropbox', 'gdrive', 'email'
    scope TEXT DEFAULT 'case',        -- 'case' or 'firm-wide'
    priority TEXT DEFAULT 'medium',   -- 'immediate', 'high', 'medium', 'low'
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(bucket, folder)
);
"""

try:
    supabase.rpc("exec_sql", {"query": create_table_sql}).execute()
    print("  Created bucket_routing_criteria table via RPC")
except Exception as e:
    print(f"  RPC not available, trying postgrest... ({str(e)[:80]})")
    print("  NOTE: Run this SQL manually in Supabase SQL editor:")
    print(create_table_sql)

# Insert routing criteria rows
print("\n=== INSERTING ROUTING CRITERIA ===\n")

all_criteria = []

for folder, c in case_file_folders.items():
    all_criteria.append({
        "bucket": "case-files",
        "folder": folder,
        "description": c["description"],
        "pipeline": "full",
        "phases": c["phases"],
        "document_types": c.get("document_types", []),
        "source": "internal",
        "scope": "case",
        "priority": c["priority"],
    })

for folder, c in external_law_folders.items():
    all_criteria.append({
        "bucket": "external-law",
        "folder": folder,
        "description": c["description"],
        "pipeline": "embed-only",
        "phases": c["phases"],
        "document_types": [],
        "source": "external",
        "scope": "case",
        "priority": c["priority"],
    })

for folder, c in reference_folders.items():
    all_criteria.append({
        "bucket": "reference",
        "folder": folder,
        "description": c["description"],
        "pipeline": "embed-only",
        "phases": c["phases"],
        "document_types": [],
        "source": "internal",
        "scope": "firm-wide",
        "priority": c["priority"],
    })

for folder, c in intake_folders.items():
    all_criteria.append({
        "bucket": "intake-queue",
        "folder": folder,
        "description": c["description"],
        "pipeline": "classify-then-route",
        "phases": [],
        "document_types": [],
        "source": c.get("source", "any"),
        "scope": "case",
        "priority": c["priority"],
    })

try:
    result = supabase.table("bucket_routing_criteria").upsert(
        all_criteria, on_conflict="bucket,folder"
    ).execute()
    print(f"  Inserted {len(result.data)} routing criteria rows")
except Exception as e:
    print(f"  Could not insert (table may not exist yet): {str(e)[:150]}")
    print("  Create the table first, then re-run this script")

print("\n=== DONE ===")
print("""
Bucket structure:
  case-files/{case_id}/
    pleadings/      -> full pipeline | complaints, answers, motions, briefs
    contracts/      -> full pipeline | agreements, amendments, licenses
    discovery/      -> full pipeline | interrogatories, depositions, subpoenas
    evidence/       -> full pipeline | exhibits, declarations, expert reports
    correspondence/ -> full pipeline | letters, emails, notices
    court-orders/   -> full pipeline | orders, rulings, judgments
    administrative/ -> full pipeline | cover sheets, docket entries

  external-law/{case_id}/
    case-law/          -> embed only | court opinions, precedent
    legislation/       -> embed only | statutes, codes, regulations
    legal-commentary/  -> embed only | law review, treatises

  reference/{firm_id}/
    templates/   -> embed only | firm templates, boilerplate
    precedents/  -> embed only | past case work, firm memos
    knowledge/   -> embed only | training materials, practice guides

  intake-queue/{case_id}/
    unclassified/ -> classify then route | chat/email uploads
    bulk/         -> classify then route | Dropbox/GDrive batch uploads
""")
