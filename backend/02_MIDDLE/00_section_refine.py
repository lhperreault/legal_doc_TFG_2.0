"""
00_section_refine.py — Phase 2, Step 0: Section Structure Refinement

Finds oversized sections (>4000 chars), asks GPT-4o-mini to identify logical
sub-section boundaries, and inserts new child rows into the sections table.

Runs BEFORE tree build so new rows are included in parent-child reconstruction.

Usage:
    python 00_section_refine.py --file_name "Appeal_Waymo_v_Uber"
    python 00_section_refine.py --document_id "abc-123-uuid"
"""

import argparse
import os
import re
import sys
import time

from dotenv import load_dotenv
from pydantic import BaseModel
from supabase import create_client

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MIN_CHARS_TO_SPLIT = 4000   # sections shorter than this are never evaluated
MAX_SUB_SECTIONS   = 15     # cap on splits per parent section
MIN_CHILD_CHARS    = 300    # drop sub-sections shorter than this after splitting

# Section titles that are never split regardless of length
SKIP_TITLES = {
    "toc", "table of contents", "table of authorities", "index of exhibits",
    "signature block", "signature page", "certificate of service",
    "certificate of interest", "cover page", "exhibit", "schedule",
    "statement of compliance", "introduction", "conclusion",
}


# ---------------------------------------------------------------------------
# Pydantic model for GPT response
# ---------------------------------------------------------------------------

class SubSection(BaseModel):
    title: str
    start_text: str   # exact characters from the document that begin this sub-section


class SplitProposal(BaseModel):
    should_split: bool
    sub_sections: list[SubSection]


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _get_supabase():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
        sys.exit(1)
    return create_client(url, key)


def _resolve_document(supabase, args) -> tuple[str, str]:
    if args.document_id:
        resp = supabase.table("documents").select("id, file_name").eq("id", args.document_id).execute()
    else:
        resp = supabase.table("documents").select("id, file_name").eq("file_name", args.file_name).execute()
    if not resp.data:
        key = args.document_id or args.file_name
        print(f"ERROR: No document found for '{key}'")
        sys.exit(1)
    row = resp.data[0]
    return row["id"], row["file_name"]


# ---------------------------------------------------------------------------
# Structural hint extractor (used for large sections)
# ---------------------------------------------------------------------------

def _extract_structural_hints(text: str) -> str:
    """Find likely heading lines in the text to help GPT identify sections."""
    lines = text.split('\n')
    hints = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # ALL CAPS lines under 100 chars (likely headings)
        if stripped.isupper() and 5 < len(stripped) < 100:
            hints.append(f"  Line ~{i}: {stripped}")
        # Numbered sections: "I.", "II.", "A.", "1.", "Section 3"
        elif re.match(r'^(?:[IVX]+\.|[A-Z]\.|(?:Section|Article|SECTION|ARTICLE)\s+\d)', stripped):
            hints.append(f"  Line ~{i}: {stripped[:80]}")
        # Roman numeral / count headers
        elif re.match(r'^(?:FIRST|SECOND|THIRD|FOURTH|FIFTH|COUNT)', stripped):
            hints.append(f"  Line ~{i}: {stripped[:80]}")
        if len(hints) >= 30:
            break
    return '\n'.join(hints) if hints else "No obvious headings found"


# ---------------------------------------------------------------------------
# GPT split proposal
# ---------------------------------------------------------------------------

def _gpt_propose_split(openai_client, section_title: str, section_text: str) -> SplitProposal | None:
    """Ask GPT-4o-mini to propose logical sub-section splits for a large section."""
    system_prompt = (
        "You are a legal document structure analyst.\n"
        "You will receive a large section of legal text. Your job is to identify whether "
        "it contains multiple distinct logical sub-sections that should be split apart.\n\n"
        "A split is warranted when the section contains clearly distinct topics, arguments, "
        "or content blocks — not just because it is long.\n\n"
        "If you propose splits:\n"
        "- Each sub_section title should be descriptive (3-10 words)\n"
        "- The start_text MUST be copied EXACTLY from the document — character for character. "
        "Use the first 60-80 characters where that sub-section begins. "
        "Do NOT paraphrase. Do NOT summarize. Copy exactly.\n"
        "- Maximum 15 sub-sections.\n\n"
        "If the section is one coherent unit (even if long), set should_split=false."
    )

    # Build the text payload sent to GPT.
    # For very large sections, send structural hints (headings map) instead of
    # a raw truncated blob — GPT can't see structure from beginning+end alone.
    if len(section_text) > 20000:
        structural_hints = _extract_structural_hints(section_text)
        text_for_gpt = (
            f"Section text (first 3000 chars):\n{section_text[:3000]}\n\n"
            f"[... {len(section_text) - 4000:,} chars omitted ...]\n\n"
            f"Section text (last 1000 chars):\n{section_text[-1000:]}\n\n"
            f"Structural hints (lines that look like headings):\n{structural_hints}"
        )
    else:
        text_for_gpt = section_text[:6000]
        if len(section_text) > 6000:
            text_for_gpt += "\n\n[... middle content omitted ...]\n\n" + section_text[-1000:]

    user_prompt = (
        f"Section title: {section_title}\n\n"
        f"Section text:\n{text_for_gpt}\n\n"
        "Analyze this section. If it contains multiple distinct sub-sections, return "
        "should_split=true with each sub-section's title and EXACT start_text copied "
        "verbatim from the document above.\n"
        "If it is one coherent unit, return should_split=false with an empty list."
    )

    for attempt in range(3):
        try:
            resp = openai_client.beta.chat.completions.parse(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                response_format=SplitProposal,
                temperature=0.1,
            )
            return resp.choices[0].message.parsed
        except Exception as e:
            err = str(e)
            is_429 = "429" in err or "rate_limit" in err
            if attempt < 2:
                wait = (15 * (attempt + 1)) if is_429 else (2 ** attempt)
                if is_429:
                    print(f"  [GPT 429] waiting {wait}s before retry {attempt + 1}/2...")
                time.sleep(wait)
            else:
                print(f"  [GPT] Failed after 3 attempts: {e}")
    return None


# ---------------------------------------------------------------------------
# Text anchoring and splitting
# ---------------------------------------------------------------------------

def _find_anchor(text: str, anchor: str, from_pos: int = 0) -> int:
    """
    Find anchor in text starting from from_pos.
    Tries exact match first, then shorter prefixes down to 20 chars.
    Returns char position, or -1 if not found.
    """
    if not anchor:
        return -1

    pos = text.find(anchor, from_pos)
    if pos != -1:
        return pos

    for prefix_len in range(min(len(anchor), 70), 19, -5):
        prefix = anchor[:prefix_len].strip()
        if not prefix:
            continue
        pos = text.find(prefix, from_pos)
        if pos != -1:
            return pos

    return -1


def _split_text(
    section_text: str,
    sub_sections: list[SubSection],
) -> list[tuple[str, str, int, int]]:
    """
    Split section_text at the anchor points proposed by GPT.
    Returns list of (title, text_slice, char_start, char_end).
    Falls back to sequential position if an anchor can't be found.
    """
    if not sub_sections:
        return []

    positions: list[tuple[int, str]] = []
    search_from = 0

    for ss in sub_sections:
        pos = _find_anchor(section_text, ss.start_text, search_from)
        if pos == -1:
            pos = search_from   # fallback: use last known position
        positions.append((pos, ss.title))
        search_from = pos + 1

    results = []
    for i, (pos, title) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(section_text)
        text_slice = section_text[pos:end].strip()
        if len(text_slice) >= MIN_CHILD_CHARS:
            results.append((title, text_slice, pos, end))

    return results


# ---------------------------------------------------------------------------
# Page number estimation
# ---------------------------------------------------------------------------

def _estimate_pages(
    char_start: int,
    char_end: int,
    total_chars: int,
    parent_start: int | None,
    parent_end: int | None,
    child_index: int,
) -> tuple[int | None, int | None]:
    """Estimate start/end page for a child based on character position in parent."""
    if parent_start is None or parent_end is None or total_chars == 0:
        return parent_start, parent_end

    page_span = parent_end - parent_start
    if page_span == 0:
        return parent_start, parent_start

    ratio_start = char_start / total_chars
    ratio_end   = char_end   / total_chars

    # Small epsilon so children sort correctly when page estimates are equal
    est_start = parent_start + ratio_start * page_span + (child_index * 0.01)
    est_end   = parent_start + ratio_end   * page_span + (child_index * 0.01)

    return round(est_start), round(est_end)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Refine section structure by splitting oversized sections.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--document_id", help="UUID of the document in Supabase")
    group.add_argument("--file_name",   help="file_name stem of the document")
    args = parser.parse_args()

    supabase = _get_supabase()
    document_id, file_name = _resolve_document(supabase, args)

    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        print("ERROR: OPENAI_API_KEY not set in .env")
        sys.exit(1)
    from openai import OpenAI
    openai_client = OpenAI()

    # Fetch all sections
    try:
        resp = (
            supabase.table("sections")
            .select("id, section_title, section_text, level, start_page, end_page, page_range, is_synthetic")
            .eq("document_id", document_id)
            .execute()
        )
    except Exception as e:
        print(f"ERROR: Failed to fetch sections — {e}")
        sys.exit(1)

    sections = resp.data or []
    if not sections:
        print(f"ERROR: No sections found for '{file_name}'")
        sys.exit(1)

    # Identify candidates for splitting
    candidates = []
    for sec in sections:
        text  = sec.get("section_text") or ""
        title = (sec.get("section_title") or "").strip()

        if text.startswith("[Split into"):
            continue                          # already refined on a previous run
        if len(text) < MIN_CHARS_TO_SPLIT:
            continue                          # too short
        if title.lower() in SKIP_TITLES:
            continue                          # structural — never split

        candidates.append(sec)

    print(f"  {len(sections)} sections total, {len(candidates)} candidates for refinement")

    splits_done    = 0
    sections_added = 0
    skipped        = 0

    for sec in candidates:
        sec_id     = sec["id"]
        title      = sec.get("section_title") or "Untitled"
        text       = sec.get("section_text") or ""
        level      = sec.get("level") or 1
        start_page = sec.get("start_page")
        end_page   = sec.get("end_page")
        short_title = title[:55]

        print(f"  Evaluating '{short_title}' ({len(text):,} chars)...")

        proposal = _gpt_propose_split(openai_client, title, text)
        time.sleep(0.5)

        if proposal is None:
            print(f"    GPT failed — skipping")
            skipped += 1
            continue

        if not proposal.should_split or not proposal.sub_sections:
            print(f"    GPT: no split needed")
            skipped += 1
            continue

        sub_sections = proposal.sub_sections[:MAX_SUB_SECTIONS]
        slices = _split_text(text, sub_sections)

        if not slices:
            print(f"    Could not anchor any sub-sections — skipping")
            skipped += 1
            continue

        print(f"    Splitting into {len(slices)} sub-sections...")

        child_level = level + 1
        inserted = 0

        for i, (child_title, child_text, char_start, char_end) in enumerate(slices):
            est_start, est_end = _estimate_pages(
                char_start, char_end, len(text),
                start_page, end_page, i,
            )
            if est_start is not None and est_end is not None:
                child_page_range = (
                    f"{est_start}-{est_end}" if est_start != est_end else str(est_start)
                )
            else:
                child_page_range = sec.get("page_range")

            child_row = {
                "document_id":   document_id,
                "section_title": child_title,
                "section_text":  child_text,
                "level":         child_level,
                "start_page":    est_start,
                "end_page":      est_end,
                "page_range":    child_page_range,
                "is_synthetic":  True,
            }

            try:
                supabase.table("sections").insert(child_row).execute()
                inserted += 1
                sections_added += 1
            except Exception as e:
                print(f"    WARNING: Failed to insert '{child_title}' — {e}")

        if inserted == 0:
            print(f"    All inserts failed — not clearing parent text")
            skipped += 1
            continue

        # Replace parent text with placeholder so it won't be re-evaluated
        placeholder = f"[Split into {inserted} sub-sections by 00_section_refine.py]"
        try:
            supabase.table("sections").update({"section_text": placeholder}).eq("id", sec_id).execute()
        except Exception as e:
            print(f"    WARNING: Could not update parent placeholder — {e}")

        splits_done += 1
        print(f"    Done — {inserted} children inserted")

    print(
        f"SUCCESS: Section refinement complete for '{file_name}'. "
        f"{splits_done} sections split, {sections_added} new child sections added, "
        f"{skipped} skipped (coherent or GPT failure)."
    )


if __name__ == "__main__":
    main()
