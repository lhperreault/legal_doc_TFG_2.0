"""
07_No_TOC.py — Smart synthetic TOC generator for documents without a native TOC.

Two-stage pipeline:
  1. Pattern Pre-scan  — finds high-confidence headers using ONLY explicit numbered/named
                         patterns (1., 1.1, Article I, Section 5, etc.).  ALL CAPS lines
                         and "short isolated line" heuristics are intentionally excluded
                         because they produce too many false positives (letter salutations,
                         dates, party labels, address blocks, etc.).
                         If ≥ MIN_EXPLICIT headers are found, the GPT stage is skipped.

  2. GPT Smart Pass    — triggered when explicit patterns find < MIN_EXPLICIT headers.
                         A single GPT-4o-mini call:
                           a) Decides if the document actually needs a TOC.
                              Short letters, memos, forms, single-topic docs → needs_toc=false.
                           b) Identifies real section headers present in the text that
                              patterns missed (ALL CAPS headings, etc.) — these require
                              AI judgment to distinguish from noise.
                           c) Inserts SYNTHETIC headers (AI-invented 2-6 word titles)
                              for large topic-shift gaps that have no explicit heading.

Synthetic headers are flagged with is_synthetic=True in the output CSV.
Documents where needs_toc=false get a single "Document Content" section.

Outputs (to zz_temp_chunks/) — identical format to 07_Yes_TOC.py / 07_Native_TOC.py:
  - {stem}_07_toc_sections.csv
  - {stem}_07_final_document.md
"""

import sys
import os
import re
import json
import pandas as pd
from difflib import SequenceMatcher
from dotenv import load_dotenv

_THIS_DIR     = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR  = os.path.dirname(_THIS_DIR)
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

# When this many numbered/named headers are found, skip the GPT stage entirely
MIN_EXPLICIT = 3

# ── Page marker regex (same as all other 07_ scripts) ────────────────────────
_MD_PAGE_RE = re.compile(
    r"(?im)^(?:##\s*[Pp]age\s+(\d+)(?:\s*\([^)]*\))?|\[\s*[Pp]age\s+(\d+)\s*\])\s*$"
)

def _page_num(m) -> int:
    return int(m.group(1) or m.group(2))


# ════════════════════════════════════════════════════════════════════════════
# STEP 1 — High-confidence Pattern Classification
#
# DESIGN CHOICE: Only numbered/named patterns are used here.
# ALL CAPS lines and "short isolated line" heuristics are EXCLUDED.
# Reason: those heuristics produce too many false positives on real documents
# (letter salutations, dates, party labels, addresses, "Sincerely,", etc.).
# The GPT stage handles ambiguous cases with full context.
# ════════════════════════════════════════════════════════════════════════════

_H3_RE = re.compile(
    r"^("
    r"\d+\.\d+\.\d+[\s\.\:]"   # 1.1.1  /  1.1.1.  /  1.1.1:
    r"|\([ivxlc]+\)\s"          # (i)  (ii)
    r"|\([A-Z]\)\s"             # (A)  (B)
    r")"
)

_H2_RE = re.compile(
    r"^("
    r"\d+\.\d+[\s\.\:]"                # 1.1  /  1.1.  /  1.1:
    r"|\([a-z]\)\s+[A-Z0-9\"]"         # (a) Text
    r"|Section\s+\d+\.\d+"             # Section 2.3
    r")",
    re.IGNORECASE,
)

# H1 is split into two patterns to avoid a IGNORECASE side-effect:
# With re.IGNORECASE, [IVX]+\. would match lowercase roman numerals like "iii."
# which are list items, not section headings.  Named keywords need IGNORECASE;
# roman numerals and numbered sections must stay case-sensitive.
_H1_NAMED_RE = re.compile(
    r"^(Article|Chapter|Part|Schedule|Annex|Exhibit|Appendix)\s+[IVX\d]"
    r"|^Section\s+\d+(?!\.\d)",
    re.IGNORECASE,
)
_H1_NUMBERED_RE = re.compile(
    r"^\d{1,3}\.\s+[A-Z]"    # 1-3 digit number (prevents matching years like "2015.")
                               # requires capital letter after — avoids sentence fragments
    r"|^[IVX]+\.\s+[A-Z]"    # uppercase roman numerals only (case-sensitive, so "iii." won't match)
)


def _level_int(tag: str) -> int:
    return {"Header_1": 1, "Header_2": 2, "Header_3": 3}.get(tag, 99)


def find_explicit_headers(full_text: str) -> list:
    """
    Find high-confidence headers using numbered/named patterns only.
    Returns list of dicts: { type, level, text, page, synthetic }.
    """
    headers = []
    lines = full_text.splitlines()
    current_page = 1

    for line in lines:
        stripped = line.strip()
        m = _MD_PAGE_RE.match(stripped)
        if m:
            current_page = _page_num(m)
            continue
        if not stripped:
            continue

        if _H3_RE.match(stripped):
            tag = "Header_3"
        elif _H2_RE.match(stripped):
            tag = "Header_2"
        elif _H1_NUMBERED_RE.match(stripped) or _H1_NAMED_RE.match(stripped):
            tag = "Header_1"
        else:
            continue

        headers.append({
            "type":      tag,
            "level":     _level_int(tag),
            "text":      stripped,
            "page":      current_page,
            "synthetic": False,
        })

    return headers


# ════════════════════════════════════════════════════════════════════════════
# STEP 2 — GPT Smart Pass
# ════════════════════════════════════════════════════════════════════════════

def _count_pages(full_text: str) -> int:
    matches = list(_MD_PAGE_RE.finditer(full_text))
    if not matches:
        return 1
    return max(_page_num(m) for m in matches)


def _build_skeleton(full_text: str, max_chars: int = 16_000) -> str:
    """
    Two-tier document skeleton for the GPT prompt.

    Tier 1 — ALL short lines (≤ 70 chars) from the entire document.
      Section headers are almost always short (e.g. "VI. STATEMENT OF PROBABLE
      CAUSE" = 29 chars, "VIII. CONCLUSION" = 16 chars).  By including every
      short line we guarantee no header is ever cut off mid-document regardless
      of how long the file is.

    Tier 2 — Sampled prose context (lines > 70 chars) from the first and last
      portions of the document only, filling remaining space up to max_chars.
      This gives GPT enough narrative context to understand the document type
      without repeating the full body text.
    """
    short_lines = []   # tier 1: every short line from the whole document
    prose_start = []   # tier 2a: long lines from first ~25% of document
    prose_end   = []   # tier 2b: long lines from last ~25% of document

    all_lines   = full_text.splitlines()
    n           = len(all_lines)
    quarter     = max(1, n // 4)

    current_page = 1
    for i, line in enumerate(all_lines):
        s = line.strip()
        m = _MD_PAGE_RE.match(s)
        if m:
            current_page = _page_num(m)
            short_lines.append(f"[Page {current_page}]")
            continue
        if not s:
            continue
        if len(s) <= 70:
            short_lines.append(s)
        else:
            if i < quarter:
                prose_start.append(s[:120])
            elif i >= n - quarter:
                prose_end.append(s[:120])

    tier1 = "\n".join(short_lines)
    remaining = max_chars - len(tier1) - 200   # 200 char buffer for separators
    if remaining > 0:
        prose = "\n".join(prose_start) + "\n...\n" + "\n".join(prose_end)
        tier2 = prose[:remaining]
    else:
        tier2 = ""

    parts = [tier1]
    if tier2.strip():
        parts.append("--- prose context (sampled) ---")
        parts.append(tier2)
    return "\n".join(parts)


def gpt_smart_pass(full_text: str, candidate_headers: list) -> dict:
    """
    Single GPT-4o-mini call that:
      - decides if the document is complex enough to need a TOC
      - validates and extends the candidate header list
      - inserts synthetic headers for large topic-shift gaps

    Returns:
        {
            "needs_toc": bool,
            "reason":    str,
            "headers":   [{"level": int, "title": str, "page": int, "synthetic": bool}, ...]
        }
    """
    try:
        from openai import OpenAI
    except Exception:
        print("  [GPT] openai not available")
        return {
            "needs_toc": bool(candidate_headers),
            "reason":    "openai unavailable",
            "headers":   candidate_headers,
        }

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("  [GPT] OPENAI_API_KEY not set")
        return {
            "needs_toc": bool(candidate_headers),
            "reason":    "no API key",
            "headers":   candidate_headers,
        }

    client     = OpenAI(api_key=api_key)
    skeleton   = _build_skeleton(full_text)
    total_pgs  = _count_pages(full_text)

    cand_text = json.dumps(
        [{"level": h["level"], "title": h["text"], "page": h["page"]} for h in candidate_headers],
        indent=2,
    ) if candidate_headers else "[]"

    prompt = (
        f"You are building a Table of Contents for a {total_pgs}-page document "
        "that has no pre-existing TOC.\n\n"

        "DOCUMENT SKELETON (only lines ≤100 chars, with [Page N] markers):\n"
        f"{skeleton}\n\n"

        "CANDIDATE HEADERS already found by pattern matching:\n"
        f"{cand_text}\n\n"

        "YOUR TASKS:\n\n"

        "1. NEEDS_TOC — decide if this document is complex enough to benefit from a TOC.\n"
        "   Set needs_toc=FALSE for:\n"
        "     - Short letters, memos, or emails (even if 2-3 pages)\n"
        "     - Single-topic documents (one continuous argument or narrative)\n"
        "     - Pure data tables, forms, or certificates\n"
        "     - Any document where a reader would not need navigation\n"
        "   Set needs_toc=TRUE for:\n"
        "     - Multi-section reports, agreements, contracts, or filings (typically 5+ pages)\n"
        "     - Any document with distinct named sections a reader might want to jump to\n\n"

        "2. HEADERS — if needs_toc=true, return the definitive header list:\n"
        "   a) REAL headers: lines that are EXPLICITLY used as section titles in the text.\n"
        "      - Include candidates that are genuine section headings.\n"
        "      - EXCLUDE candidates that are: the opening words of a normal sentence,\n"
        "        dates, address lines, salutations, party labels (e.g. 'BETWEEN:'),\n"
        "        signatures, or any other document noise.\n"
        "   b) MISSED real headers: section headings present in the text that the pattern\n"
        "      matcher missed (e.g. standalone ALL CAPS titles, underlined/bold headings\n"
        "      visible as short isolated lines). Only include lines that clearly function\n"
        "      as headings — NOT regular sentences that happen to be short.\n"
        "   c) SYNTHETIC headers: if there is a large content gap (3+ pages) where the\n"
        "      topic clearly shifts but there is NO heading in the text, insert a synthetic\n"
        "      header with a concise 2-6 word title describing that section.\n"
        "      Mark these with \"synthetic\": true.\n\n"

        "3. LEVELS:\n"
        "   - level 1 = major section (top-level)\n"
        "   - level 2 = subsection\n"
        "   - level 3 = sub-subsection\n\n"

        "Return ONLY valid JSON (no markdown fences):\n"
        "{\n"
        '  "needs_toc": true,\n'
        '  "reason": "35-page contract with multiple distinct clauses",\n'
        '  "headers": [\n'
        '    {"level": 1, "title": "DEFINITIONS", "page": 2, "synthetic": false},\n'
        '    {"level": 2, "title": "Key Financial Terms", "page": 4, "synthetic": true},\n'
        '    {"level": 1, "title": "OBLIGATIONS", "page": 8, "synthetic": false}\n'
        "  ]\n"
        "}"
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise document structure analyst. "
                        "Output only valid JSON with no markdown fences."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$",       "", raw)
        result = json.loads(raw)
    except Exception as e:
        print(f"  [GPT] Error: {e}")
        return {
            "needs_toc": bool(candidate_headers),
            "reason":    f"GPT error: {e}",
            "headers":   candidate_headers,
        }

    _TAG = {1: "Header_1", 2: "Header_2", 3: "Header_3"}
    headers = []
    for e in result.get("headers", []):
        lvl = max(1, min(3, int(e.get("level", 1))))
        headers.append({
            "type":      _TAG[lvl],
            "level":     lvl,
            "text":      str(e.get("title", "")).strip(),
            "page":      int(e.get("page", 1)),
            "synthetic": bool(e.get("synthetic", False)),
        })

    print(f"  [GPT] needs_toc={result.get('needs_toc')}  |  headers returned: {len(headers)}")
    return {
        "needs_toc": bool(result.get("needs_toc", True)),
        "reason":    str(result.get("reason", "")),
        "headers":   headers,
    }


def gpt_validate_headers(headers: list, skeleton: str) -> list:
    """
    Focused GPT pass that reviews the final header list and removes false positives.
    Runs after both the pattern stage and the smart pass, so it catches anything
    that slipped through — garbled OCR, years parsed as section numbers, numbered
    list items that happen to match a roman-numeral pattern, etc.

    Sends the candidate list + document skeleton to GPT-4o-mini and asks it to
    return only the indices of entries that are genuine section headings.
    Returns the filtered list (same structure, subset of input).
    """
    if not headers:
        return headers

    try:
        from openai import OpenAI
    except Exception:
        return headers

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return headers

    client = OpenAI(api_key=api_key)

    candidates = json.dumps(
        [
            {"index": i, "level": h["level"], "title": h["text"], "page": h["page"]}
            for i, h in enumerate(headers)
        ],
        indent=2,
    )

    prompt = (
        "You are reviewing a list of candidate section headers detected from a document.\n"
        "Your default is to KEEP entries. Only remove an entry if you are confident it is wrong.\n\n"
        "DOCUMENT SKELETON (for context):\n"
        f"{skeleton[:8000]}\n\n"
        "CANDIDATE HEADERS:\n"
        f"{candidates}\n\n"
        "RULES — remove an entry ONLY if it clearly matches one of these:\n"
        "  - A regular sentence or running clause (e.g. '2015. I am a federal law enforcement officer within the...')\n"
        "  - A bare date, year, or timestamp that is not a section title\n"
        "  - Garbled OCR output or non-Latin / mixed-character noise\n"
        "  - A numbered sub-item deep inside a section body (e.g. 'iii. evidence of the attachment of other devices;')\n"
        "  - A partial sentence or mid-paragraph fragment\n\n"
        "IMPORTANT — always keep an entry if:\n"
        "  - It follows the same clear numbering pattern as other entries in the list\n"
        "    (e.g. if I., II., III., IV., V. are all present, keep VI. even if you are unsure about its content)\n"
        "  - It is a named document division (Article, Section, Schedule, Appendix, Conclusion, etc.)\n"
        "  - You are uncertain — when in doubt, keep it\n\n"
        "Return ONLY a JSON array of the INDEX numbers to KEEP (no markdown fences):\n"
        "[0, 1, 2, 3, 4, 5]"
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are a precise document analyst. Output only a JSON array of integers.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$",       "", raw)
        keep_indices = set(json.loads(raw))
        kept    = [h for i, h in enumerate(headers) if i in keep_indices]
        removed = len(headers) - len(kept)
        print(f"  [Validation] Removed {removed} false positive(s), kept {len(kept)} headers")
        return kept
    except Exception as e:
        print(f"  [Validation] Error: {e} — keeping all headers unchanged")
        return headers


# ════════════════════════════════════════════════════════════════════════════
# STEP 3 — Build Section Tree
# ════════════════════════════════════════════════════════════════════════════

def build_page_dict(text: str) -> dict:
    """Return {physical_page_number: page_text}. Falls back to {1: full_text}."""
    matches = list(_MD_PAGE_RE.finditer(text))
    if not matches:
        return {1: text.strip()}
    page_dict = {}
    for i, m in enumerate(matches):
        num   = _page_num(m)
        start = m.end()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        page_dict[num] = text[start:end].strip()
    return page_dict


def _fuzzy_line_idx(needle: str, page_text: str, threshold: float = 0.65) -> int:
    """Return the index of the line in page_text most similar to needle, or 0."""
    best_idx, best_ratio = 0, 0.0
    for i, ln in enumerate(page_text.splitlines()):
        r = SequenceMatcher(None, needle.lower(), ln.strip().lower()).ratio()
        if r > best_ratio:
            best_ratio, best_idx = r, i
    return best_idx if best_ratio >= threshold else 0


def get_section_text(
    title: str,
    start_page: int,
    end_page: int,
    page_dict: dict,
    next_title: str | None = None,
    synthetic: bool = False,
) -> str:
    """
    Extract section body text from the page dict.
    Real headers: fuzzy-trim to the header line.
    Synthetic headers: take from the top of the start page (no line to trim to).
    """
    block = "\n".join(page_dict.get(p, "") for p in range(start_page, end_page + 1))
    if not block.strip():
        return ""
    lines     = block.splitlines()
    start_idx = 0 if synthetic else _fuzzy_line_idx(title, block)
    end_idx   = len(lines)
    if next_title and not synthetic:
        for k in range(start_idx + 1, len(lines)):
            r = SequenceMatcher(None, lines[k].strip().lower(), next_title.lower()).ratio()
            if r >= 0.65:
                end_idx = k
                break
    return "\n".join(lines[start_idx:end_idx]).strip()


def build_sections_df(headers: list, page_dict: dict, total_pages: int) -> pd.DataFrame:
    """Build the full section DataFrame from the validated/enriched header list."""
    rows = []

    # ── Title Page ────────────────────────────────────────────────────────────
    first_body_page = headers[0]["page"] if headers else 1
    title_text = "\n".join(
        page_dict.get(p, "") for p in range(1, first_body_page)
    ).strip()
    if not title_text and first_body_page in page_dict and headers:
        pg_lines   = page_dict[first_body_page].splitlines()
        trim_idx   = _fuzzy_line_idx(headers[0]["text"], page_dict[first_body_page])
        title_text = "\n".join(pg_lines[:trim_idx]).strip()

    rows.append({
        "level": 0, "section": "Title Page", "is_synthetic": False,
        "start_page": pd.NA, "end_page": pd.NA, "page_range": "",
        "section_text": title_text,
    })

    # ── Synthetic TOC preview (starred entries = AI-generated titles) ─────────
    toc_lines = [
        ("  " * (h["level"] - 1))
        + ("* " if h.get("synthetic") else "")
        + h["text"]
        + f"  ....  {h['page']}"
        for h in headers
    ]
    rows.append({
        "level": 0, "section": "TOC (Synthetic)", "is_synthetic": False,
        "start_page": pd.NA, "end_page": pd.NA, "page_range": "",
        "section_text": "\n".join(toc_lines),
    })

    # ── Body sections ─────────────────────────────────────────────────────────
    for i, hdr in enumerate(headers):
        start_pg = hdr["page"]
        end_pg   = total_pages
        for j in range(i + 1, len(headers)):
            if headers[j]["level"] <= hdr["level"]:
                end_pg = headers[j]["page"] - 1
                break
        end_pg = max(start_pg, end_pg)

        synth      = hdr.get("synthetic", False)
        next_title = headers[i + 1]["text"] if i + 1 < len(headers) else None
        text       = get_section_text(
            hdr["text"], start_pg, end_pg, page_dict, next_title, synthetic=synth
        )

        indent = "  " * (hdr["level"] - 1)
        label  = ("[AI] " if synth else "") + hdr["text"]
        rows.append({
            "level":        hdr["level"],
            "section":      indent + label,
            "is_synthetic": synth,
            "start_page":   start_pg,
            "end_page":     end_pg,
            "page_range":   f"{start_pg}-{end_pg}",
            "section_text": text,
        })

    return pd.DataFrame(rows)


def build_single_section_df(page_dict: dict, total_pages: int) -> pd.DataFrame:
    """
    Fallback for documents that don't benefit from a TOC.
    Outputs the full content as one section.
    """
    full_text = "\n\n".join(page_dict.get(p, "") for p in range(1, total_pages + 1))
    pg_range  = f"1-{total_pages}" if total_pages > 1 else "1"
    return pd.DataFrame([{
        "level": 0, "section": "Document Content", "is_synthetic": False,
        "start_page": 1, "end_page": total_pages, "page_range": pg_range,
        "section_text": full_text.strip(),
    }])


# ════════════════════════════════════════════════════════════════════════════
# STEP 4 — Exhibit detection and splitting
# ════════════════════════════════════════════════════════════════════════════

# Matches "Exhibit A", "Exhibit 1", "Ex. 1", "EXHIBIT A" at the START of a line
# (line-anchored so inline mentions like "as shown in Exhibit A" don't trigger it)
_EXHIBIT_START_RE = re.compile(
    r"(?im)^(?:exhibit|attachment|schedule|appendix)\s*[A-Z0-9]"
)

# Broader inline pattern — used to scan section names / late pages
_EXHIBIT_INLINE_RE = re.compile(
    r"(?i)\b(?:exhibit|ex\s*[-]?\s*\d+|ex\d+)\b"
)


def detect_and_split_exhibits(
    df: pd.DataFrame, page_dict: dict, total_pages: int
) -> tuple:
    """
    Scan the DataFrame section names and page content for exhibit markers.
    Uses a line-anchored pattern so inline mentions don't trigger a false split.

    Returns (updated_df, exhibit_text, exhibit_start_page).
    exhibit_text is "" and exhibit_start_page is None when nothing is found.
    """
    # 1. Check whether any section name already contains "exhibit"
    section_names = " ".join(str(r.get("section", "")) for _, r in df.iterrows())
    toc_row = df[df["section"] == "TOC (Synthetic)"]
    toc_preview = str(toc_row["section_text"].values[0]) if len(toc_row) else ""

    exhibit_in_toc = bool(
        _EXHIBIT_INLINE_RE.search(section_names)
        or _EXHIBIT_INLINE_RE.search(toc_preview)
    )

    # 2. Find the first page where "Exhibit X" appears at the START of a line
    exhibit_start_page = None
    # Scan all pages if mentioned in TOC; otherwise only scan the last 50%
    scan_from = 1 if exhibit_in_toc else max(1, int(total_pages * 0.5))
    for p in range(scan_from, total_pages + 1):
        if _EXHIBIT_START_RE.search(page_dict.get(p, "")):
            exhibit_start_page = p
            break

    if not exhibit_start_page:
        return df, "", None

    # 3. Collect exhibit text from that page to the end
    exhibit_text = "\n\n".join(
        page_dict.get(p, "") for p in range(exhibit_start_page, total_pages + 1)
    ).strip()

    if not exhibit_text:
        return df, "", None

    # 4. Trim the last real section in df so it ends before the exhibit page
    updated_df = df.copy()
    non_na = updated_df[updated_df["start_page"].notna()]
    if len(non_na) > 0:
        last_idx = non_na.index[-1]
        new_end = min(int(updated_df.loc[last_idx, "end_page"]), exhibit_start_page - 1)
        start_of_last = int(updated_df.loc[last_idx, "start_page"])
        if new_end >= start_of_last:
            updated_df.loc[last_idx, "end_page"]   = new_end
            updated_df.loc[last_idx, "page_range"]  = f"{start_of_last}-{new_end}"
            # Re-extract the section text for the trimmed range
            raw_title = str(updated_df.loc[last_idx, "section"]).strip().removeprefix("[AI] ")
            synth     = bool(updated_df.loc[last_idx, "is_synthetic"])
            updated_df.loc[last_idx, "section_text"] = get_section_text(
                raw_title, start_of_last, new_end, page_dict, synthetic=synth
            )
        else:
            # The whole last section is exhibits — drop it
            updated_df = updated_df.drop(last_idx).reset_index(drop=True)

    return updated_df, exhibit_text, exhibit_start_page


# ════════════════════════════════════════════════════════════════════════════
# Final document builder (same indented format as 07_Native_TOC.py)
# ════════════════════════════════════════════════════════════════════════════

def build_final_document(df: pd.DataFrame) -> str:
    lines = []
    for _, row in df.iterrows():
        level        = int(row.get("level", 0) or 0)
        section      = str(row.get("section", "")).strip()
        page_range   = str(row.get("page_range", "")).strip()
        section_text = str(row.get("section_text", "")).strip()

        h_ind = "  " * level
        m_ind = h_ind + "  "
        p_ind = h_ind + "    "

        lines.append(f"{h_ind}{section}")
        if page_range:
            lines.append(f"{m_ind}(Pages: {page_range})")
        if section_text:
            for para in re.split(r"\n\s*\n", section_text):
                if para.strip():
                    for ln in para.splitlines():
                        lines.append(f"{p_ind}{ln.rstrip()}")
                    lines.append("")
        else:
            lines.append(f"{p_ind}[No extracted text]")
            lines.append("")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) != 2:
        print("Usage: python 07_No_TOC.py <text_extraction_md>")
        sys.exit(1)

    text_md = sys.argv[1]
    if not os.path.isfile(text_md):
        print(f"ERROR: File not found: {text_md}")
        sys.exit(1)

    stem     = os.path.splitext(os.path.basename(text_md))[0]
    doc_stem = stem.replace("_text_extraction", "")
    temp_dir = os.path.dirname(os.path.abspath(text_md))

    print("=" * 60)
    print("07_NO_TOC — Smart Synthetic TOC Generator")
    print("=" * 60)
    print(f"  Input : {text_md}")

    with open(text_md, "r", encoding="utf-8") as f:
        full_text = f.read()

    total_pages = _count_pages(full_text)
    print(f"  Pages : {total_pages}")

    # ── Stage 1: High-confidence explicit header detection ────────────────────
    print("\nStage 1 — Pattern detection (numbered/named headers only)...")
    explicit_headers = find_explicit_headers(full_text)
    print(f"  Explicit headers found : {len(explicit_headers)}")
    for h in explicit_headers[:10]:
        indent = "  " * (h["level"] - 1)
        print(f"    {indent}[H{h['level']}] {h['text'][:70]}  (p.{h['page']})")
    if len(explicit_headers) > 10:
        print(f"    ... and {len(explicit_headers) - 10} more")

    # ── Stage 2: Decide path ──────────────────────────────────────────────────
    needs_toc = True
    headers   = explicit_headers

    if len(explicit_headers) >= MIN_EXPLICIT:
        print(f"\nStage 2 — {len(explicit_headers)} explicit headers found; skipping GPT smart pass.")
        for h in headers:
            h["synthetic"] = False
    else:
        print(f"\nStage 2 — Fewer than {MIN_EXPLICIT} explicit headers; running GPT smart pass...")
        result    = gpt_smart_pass(full_text, explicit_headers)
        needs_toc = result["needs_toc"]
        headers   = result["headers"]
        print(f"  Reason : {result.get('reason', '')}")

    # ── Stage 2b: GPT validation — strip false positives from whatever path took ──
    if headers and needs_toc:
        print(f"\nStage 2b — Validating {len(headers)} header(s) with GPT...")
        skeleton = _build_skeleton(full_text)
        headers  = gpt_validate_headers(headers, skeleton)

    # ── Header preview ────────────────────────────────────────────────────────
    if headers:
        print("\n  ── Final header list ───────────────────────────────")
        for h in headers[:30]:
            indent = "  " * (h["level"] - 1)
            synth  = " [SYNTHETIC]" if h.get("synthetic") else ""
            print(f"    {indent}[H{h['level']}]{synth} {h['text'][:60]}  (p.{h['page']})")
        if len(headers) > 30:
            print(f"    ... and {len(headers) - 30} more")
        print("  ────────────────────────────────────────────────────")

    # ── Stage 3: Build section tree ───────────────────────────────────────────
    print("\nStage 3 — Building sections...")
    page_dict = build_page_dict(full_text)

    if not needs_toc or not headers:
        print("  No TOC needed — creating single-section output.")
        df = build_single_section_df(page_dict, total_pages)
    else:
        df = build_sections_df(headers, page_dict, total_pages)

    print(f"  Sections built : {len(df)}")

    # ── Stage 4: Exhibit detection and splitting ──────────────────────────────
    print("\nStage 4 — Checking for exhibits...")
    df, exhibit_text, exhibit_start_page = detect_and_split_exhibits(
        df, page_dict, total_pages
    )
    if exhibit_text:
        exhibit_md_path = os.path.join(temp_dir, doc_stem + "_exhibits.md")
        with open(exhibit_md_path, "w", encoding="utf-8") as f:
            f.write(exhibit_text)
        print(f"  EXHIBIT DETECTED — starts at page {exhibit_start_page}")
        print(f"  Exhibit file saved : {exhibit_md_path}")
    else:
        print("  No exhibits detected.")

    # ── Save outputs ──────────────────────────────────────────────────────────
    csv_out = os.path.join(temp_dir, doc_stem + "_07_toc_sections.csv")
    df.to_csv(csv_out, index=False, encoding="utf-8")

    final_md = build_final_document(df)
    md_out   = os.path.join(temp_dir, doc_stem + "_07_final_document.md")
    with open(md_out, "w", encoding="utf-8") as f:
        f.write(final_md)

    print("\n" + "=" * 60)
    print("07_NO_TOC — COMPLETE")
    print(f"  Sections processed : {len(df)}")
    print(f"  Exhibit detected   : {'YES — page ' + str(exhibit_start_page) if exhibit_text else 'NO'}")
    print(f"  Output CSV         : {csv_out}")
    print(f"  Output MD          : {md_out}")
    print("=" * 60)


if __name__ == "__main__":
    main()
