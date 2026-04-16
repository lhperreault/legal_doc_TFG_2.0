"""Fine-grained folder routing — runs after 05_doc_classification.py.

Given:
  - the already-extracted text (from 04_text_extraction.py)
  - the broad doc_type from 05_doc_classification.py
  - the case's cases.folder_structure (per-case subfolders)

Picks the best (folder_parent, folder_subslug) for the document.

- parent is always one of the 7 defaults (pleadings, contracts, discovery,
  evidence, correspondence, court-orders, administrative).
- subslug may be null (no matching sub — file lives at the parent level).

Uses Gemini Flash. Cheap + fast. Only reads the first ~3000 tokens of the
document, since the parent is mostly already decided by 05 and the subslug
decision hinges on the document's opening / title / TOC.

Usage:
    python 05b_fine_routing.py <text_extraction_md> --case-id <uuid>

Writes output to zz_temp_chunks/<stem>_fine_routing.json:
    {"folder_parent": "pleadings", "folder_subslug": "motion-to-dismiss",
     "confidence": 0.88, "reasoning": "..."}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure utils on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from supabase import create_client

from backend.utils.slug import slugify


PARENT_FROM_DOC_TYPE = {
    # Broad doc_type prefix → default parent folder
    "Pleading":        "pleadings",
    "Exhibit":         "evidence",
    "Discovery":       "discovery",
    "Contract":        "contracts",
    "Communication":   "correspondence",
    "Financial":       "evidence",
    "Corporate":       "administrative",
    "Evidence":        "evidence",
    "Regulatory":      "administrative",
    "Court":           "court-orders",
    "Administrative":  "administrative",
}

ALLOWED_PARENTS = {
    "pleadings", "contracts", "discovery", "evidence",
    "correspondence", "court-orders", "administrative",
}


def _parent_from_doc_type(doc_type: str | None) -> str:
    if not doc_type:
        return "administrative"
    prefix = doc_type.split(" - ")[0].strip()
    return PARENT_FROM_DOC_TYPE.get(prefix, "administrative")


def _load_case_folder_structure(case_id: str) -> dict:
    """Fetch cases.folder_structure + folder_labels."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return {}
    sb = create_client(url, key)
    resp = sb.table("cases").select("folder_structure, folder_labels").eq("id", case_id).maybe_single().execute()
    return (resp.data or {}) if resp else {}


def _pick_subslug_with_gemini(
    text_head: str,
    doc_type: str,
    parent: str,
    candidates: list[str],
    candidate_labels: dict,
) -> tuple[str | None, float, str]:
    """Ask Gemini Flash to pick the best subslug (or none).

    Returns (subslug, confidence, reasoning). subslug is None if no good match.
    """
    if not candidates:
        return (None, 1.0, "no candidates in this parent")

    try:
        import google.generativeai as genai
    except ImportError:
        # Gemini not installed — fall back gracefully
        return (None, 0.0, "gemini sdk not installed")

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return (None, 0.0, "no GEMINI_API_KEY")
    genai.configure(api_key=api_key)

    # Build candidate list with labels for model context
    candidate_lines = []
    for slug in candidates:
        labels = candidate_labels.get(slug, {})
        en = labels.get("en", slug.replace("-", " ").title())
        es = labels.get("es", "")
        label_str = f"{en}" + (f" / {es}" if es else "")
        candidate_lines.append(f"- {slug}  ({label_str})")
    candidates_str = "\n".join(candidate_lines)

    prompt = f"""You are classifying a legal document into a case-specific subfolder.

The document has already been classified as: **{doc_type}**
It will live under the top-level folder: **{parent}**

Pick the BEST matching subfolder from this case's custom list. If NONE of the
subfolders fit, reply with "NONE" — do NOT force a bad match.

Available subfolders in '{parent}':
{candidates_str}

Document opening (first ~3000 chars):
\"\"\"
{text_head[:3000]}
\"\"\"

Reply in JSON only, with keys: subslug (string or "NONE"), confidence (0.0-1.0), reasoning (short).
"""

    model = genai.GenerativeModel("gemini-2.0-flash-exp")
    try:
        resp = model.generate_content(
            prompt,
            generation_config={
                "temperature": 0.0,
                "response_mime_type": "application/json",
            },
        )
        data = json.loads(resp.text)
    except Exception as e:
        return (None, 0.0, f"gemini error: {e}")

    picked = (data.get("subslug") or "").strip()
    confidence = float(data.get("confidence", 0.0) or 0.0)
    reasoning = str(data.get("reasoning", ""))[:200]
    if picked.upper() == "NONE" or picked == "":
        return (None, confidence, reasoning)
    if picked not in candidates:
        # Safety: model hallucinated a slug not in the candidates
        return (None, 0.0, f"model returned non-candidate slug: {picked}")
    return (picked, confidence, reasoning)


def route_document(text_md_path: str, case_id: str | None) -> dict:
    """Pick (folder_parent, folder_subslug) for a document."""
    temp_dir = os.path.dirname(text_md_path)
    stem = os.path.splitext(os.path.basename(text_md_path))[0]
    class_json = os.path.join(temp_dir, stem + "_classification.json")

    doc_type = None
    if os.path.isfile(class_json):
        with open(class_json, encoding="utf-8") as f:
            doc_type = json.load(f).get("document_type")

    parent = _parent_from_doc_type(doc_type)

    # Short-circuit if we have no case_id: just return the parent
    if not case_id:
        return {
            "folder_parent": parent,
            "folder_subslug": None,
            "confidence": 1.0,
            "reasoning": "no case_id → using doc_type-derived parent only",
        }

    case = _load_case_folder_structure(case_id)
    fs = case.get("folder_structure") or {}
    labels = case.get("folder_labels") or {}
    candidates = [slugify(s) for s in fs.get(parent, [])]
    candidates = [c for c in candidates if c]  # drop empties

    if not candidates:
        return {
            "folder_parent": parent,
            "folder_subslug": None,
            "confidence": 1.0,
            "reasoning": f"no subfolders configured under '{parent}' for this case",
        }

    # Read first 3k chars from the text markdown
    text_head = ""
    if os.path.isfile(text_md_path):
        with open(text_md_path, encoding="utf-8") as f:
            text_head = f.read(3500)

    subslug, confidence, reasoning = _pick_subslug_with_gemini(
        text_head, doc_type or "", parent, candidates, labels,
    )

    return {
        "folder_parent": parent,
        "folder_subslug": subslug,
        "confidence": confidence,
        "reasoning": reasoning,
        "doc_type": doc_type,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("text_md", help="Path to the text-extraction .md file")
    parser.add_argument("--case-id", default=None, help="Case UUID (looks up folder_structure)")
    args = parser.parse_args()

    result = route_document(args.text_md, args.case_id)

    temp_dir = os.path.dirname(args.text_md)
    stem = os.path.splitext(os.path.basename(args.text_md))[0]
    out_path = os.path.join(temp_dir, stem + "_fine_routing.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(
        f"SUCCESS: 05b_fine_routing.py ran. "
        f"parent={result['folder_parent']} subslug={result.get('folder_subslug')} "
        f"confidence={result.get('confidence', 0):.2f} "
        f"→ {out_path}"
    )


if __name__ == "__main__":
    main()
