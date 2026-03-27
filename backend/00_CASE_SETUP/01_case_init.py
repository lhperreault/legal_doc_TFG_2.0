"""
00_CASE_SETUP/01_case_init.py — Create a new case and optionally ingest the first document.

Usage:
    python 01_case_init.py --case-name "Smith v. Acme Corp" \\
                           --party-role plaintiff \\
                           [--context "Filed in NDCA; Acme breached NDA and misused trade secrets..."] \\
                           [--first-document "complaint.pdf"]

Outputs:
    Prints the new case_id (UUID) to stdout on the last line as: case_id=<uuid>
    If --first-document is supplied, also runs the full 01_INITIAL pipeline on it,
    marks it as is_primary_filing=True, and updates cases.primary_document_id.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

from dotenv import load_dotenv
from supabase import create_client, Client

# ── Environment ──────────────────────────────────────────────────────────────

# Walk up two directories from 00_CASE_SETUP to reach the project root .env
_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, "..", "..", ".env"))

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")


# ── Document-type → case-stage mapping ───────────────────────────────────────

_STAGE_PREFIXES: list[tuple[str, str]] = [
    ("Discovery",                "discovery"),
    ("Pleading - Appeal",        "appeal"),
    ("Pleading - Motion",        "motions"),
    ("Court - Trial",            "trial"),
    ("Pleading - Complaint",     "filing"),
    ("Pleading - Amended",       "filing"),
    ("Pleading - Criminal",      "filing"),
]

def _infer_stage(doc_type: str) -> str:
    for prefix, stage in _STAGE_PREFIXES:
        if doc_type.startswith(prefix):
            return stage
    return "filing"


# ── Party name parsing ────────────────────────────────────────────────────────

_V_SEPARATORS = (" v. ", " vs. ", " vs ", " V. ", " VS. ", " V ", " Vs. ")

def _parse_parties(case_name: str) -> tuple[str, str | None]:
    """Return (plaintiff/petitioner, defendant/respondent) from a case name."""
    for sep in _V_SEPARATORS:
        if sep in case_name:
            a, b = case_name.split(sep, 1)
            return a.strip(), b.strip()
    return case_name.strip(), None


# ── Gemini Flash context extraction ──────────────────────────────────────────

_CONTEXT_PROMPT = """You are a legal case analyst. Parse the following case context provided by a user and extract structured information.

User context: "{context}"

Extract and return ONLY valid JSON with these exact keys:
- case_stage: one of filing | discovery | motions | trial | appeal | closed  (or null if unclear)
- court_name: name of the court if mentioned, else null
- judge_name: judge's name if mentioned, else null
- key_issues: one-sentence summary of what the dispute is about, else null

Return only the raw JSON object. No markdown fences, no extra text."""


def _extract_context(context: str) -> dict:
    """Call Gemini Flash to extract structured info from free-form case context."""
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        print("[01_case_init] GOOGLE_API_KEY not set — skipping Gemini context extraction.")
        return {}
    try:
        import google.generativeai as genai  # pip install google-generativeai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = _CONTEXT_PROMPT.format(context=context)
        response = model.generate_content(prompt)
        raw = response.text.strip()
        # Strip accidental markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        print(f"[01_case_init] Gemini context extraction failed ({e}) — using defaults.")
        return {}


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> str:
    parser = argparse.ArgumentParser(
        description="Create a new case in Supabase and optionally ingest its first document."
    )
    parser.add_argument("--case-name",      required=True,
                        help='e.g. "Smith v. Acme Corp"')
    parser.add_argument("--party-role",     required=True,
                        choices=["plaintiff", "defendant", "appellant", "appellee"],
                        help="Which side are we representing?")
    parser.add_argument("--context",        default=None,
                        help="Free-form text about the case (fed to Gemini for structured extraction)")
    parser.add_argument("--first-document", default=None,
                        help="Filename in zz_Mockfiles to ingest as the primary document")
    args = parser.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)

    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # ── Party names ──────────────────────────────────────────────────────────
    first_party, second_party = _parse_parties(args.case_name)

    if args.party_role in ("plaintiff", "appellant"):
        our_client     = first_party
        opposing_party = second_party
    else:
        our_client     = second_party
        opposing_party = first_party

    # ── Build initial case payload ────────────────────────────────────────────
    case_payload: dict = {
        "case_name":      args.case_name,
        "party_role":     args.party_role,
        "our_client":     our_client,
        "opposing_party": opposing_party,
        "case_stage":     "filing",
        "case_context":   args.context,
    }

    # ── Optional: Gemini Flash context extraction ─────────────────────────────
    if args.context:
        print("[01_case_init] Extracting context with Gemini Flash…")
        extracted = _extract_context(args.context)
        if extracted.get("case_stage"):
            case_payload["case_stage"] = extracted["case_stage"]
        if extracted.get("court_name"):
            case_payload["court_name"] = extracted["court_name"]
        if extracted.get("judge_name"):
            case_payload["judge_name"] = extracted["judge_name"]
        print(f"[01_case_init] Gemini extracted: {extracted}")

    # Strip None values — let DB defaults handle them
    case_payload = {k: v for k, v in case_payload.items() if v is not None}

    # ── Insert case row ───────────────────────────────────────────────────────
    resp = supabase.table("cases").insert(case_payload).execute()
    if not resp.data:
        print(f"ERROR: Failed to create case row. Response: {resp}")
        sys.exit(1)

    case_id: str = resp.data[0]["id"]
    print(f"[01_case_init] Case created: '{args.case_name}'")
    print(f"  id={case_id}")
    print(f"  party_role={args.party_role}, our_client='{our_client}', opposing='{opposing_party}'")
    print(f"  case_stage={case_payload.get('case_stage', 'filing')}")

    # ── Optional: ingest first document ─────────────────────────────────────
    if args.first_document:
        initial_dir = os.path.join(_HERE, "..", "01_INITIAL")
        main_py     = os.path.join(initial_dir, "main.py")
        print(f"\n[01_case_init] Ingesting first document: {args.first_document}")

        result = subprocess.run(
            [sys.executable, main_py, args.first_document, "--case-id", case_id, "--primary"],
        )

        if result.returncode != 0:
            print(
                f"[01_case_init] WARNING: Pipeline returned exit code {result.returncode} "
                f"for '{args.first_document}'"
            )
        else:
            # Look up the document that was just upserted and link it to the case
            doc_stem = os.path.splitext(args.first_document)[0]
            doc_resp = (
                supabase.table("documents")
                .select("id, document_type")
                .eq("file_name", doc_stem)
                .execute()
            )
            if doc_resp.data:
                doc_id   = doc_resp.data[0]["id"]
                doc_type = doc_resp.data[0].get("document_type") or ""

                inferred_stage = _infer_stage(doc_type)
                supabase.table("cases").update(
                    {
                        "primary_document_id": doc_id,
                        "case_stage":          inferred_stage,
                    }
                ).eq("id", case_id).execute()

                print(
                    f"[01_case_init] Case updated: "
                    f"primary_document_id={doc_id}, case_stage={inferred_stage}"
                )
            else:
                print(
                    f"[01_case_init] WARNING: Could not find document '{doc_stem}' "
                    f"in Supabase after pipeline run."
                )

    # Always print case_id as the final line so callers can parse it
    print(f"\ncase_id={case_id}")
    return case_id


if __name__ == "__main__":
    main()
