"""
07_No_TOC.py — Smart synthetic TOC generator for documents without a native TOC.

Pipeline:
  Stage 1 — GPT Primary
    ≤ 20 pages: GPT reads the full text and builds the complete TOC.
    > 20 pages: GPT reads the first 20 pages, builds a partial TOC, and
                reports the heading styles it observed (e.g. "I.", "A.", "1.").
                Python then uses those learned styles to scan the remaining pages.

  Stage 2 — GPT Validation
    Runs always when headers exist. Removes false positives with conservative bias.

  Stage 3 — Build Section Tree
  Stage 4 — Exhibit Detection and Splitting

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

# How many pages GPT reads in the initial pass for long documents
GPT_PAGE_LIMIT = 20

# ── Page marker regex (same as all other 07_ scripts) ────────────────────────
_MD_PAGE_RE = re.compile(
    r"(?im)^(?:##\s*[Pp]age\s+(\d+)(?:\s*\([^)]*\))?|\[\s*[Pp]age\s+(\d+)\s*\])\s*$"
)

def _page_num(m) -> int:
    return int(m.group(1) or m.group(2))


# ════════════════════════════════════════════════════════════════════════════
# STAGE 1 — GPT-Primary Header Detection
# ════════════════════════════════════════════════════════════════════════════

def _count_pages(full_text: str) -> int:
    matches = list(_MD_PAGE_RE.finditer(full_text))
    if not matches:
        return 1
    return max(_page_num(m) for m in matches)


def _extract_pages(full_text: str, start_page: int, end_page: int) -> str:
    """Return only the text for pages start_page..end_page inclusive."""
    lines = full_text.splitlines(keepends=True)
    result = []
    current_page = 0
    in_range = False

    for line in lines:
        m = _MD_PAGE_RE.match(line.strip())
        if m:
            current_page = _page_num(m)
            in_range = start_page <= current_page <= end_page
        if current_page == 0 and start_page == 1:
            result.append(line)
        elif in_range:
            result.append(line)
        elif current_page > end_page:
            break

    return "".join(result)


# ── Heading style → regex mapping ────────────────────────────────────────────
# GPT returns style descriptors like "I.", "A.", "1." — these map to (pattern, level).
_STYLE_PATTERNS: dict[str, tuple[str, int]] = {
    "I.":      (r"^[IVX]+\.\s+[A-Z]",          1),
    "1.":      (r"^\d{1,3}\.\s+[A-Z]",          1),
    "A.":      (r"^[A-Z]\.\s+[A-Z]",            2),
    "a.":      (r"^[a-z]\.\s+[A-Za-z]",         3),
    "1)":      (r"^\d+\)\s+[A-Za-z]",           2),
    "a)":      (r"^[a-z]\)\s+[A-Za-z]",         3),
    "A)":      (r"^[A-Z]\)\s+[A-Za-z]",         2),
    "(a)":     (r"^\([a-z]\)\s+[A-Za-z]",       3),
    "(A)":     (r"^\([A-Z]\)\s+[A-Za-z]",       2),
    "(i)":     (r"^\([ivx]+\)\s+[A-Za-z]",      3),
    "1.1":     (r"^\d+\.\d+[\s\.\:]",           2),
    "1.1.1":   (r"^\d+\.\d+\.\d+[\s\.\:]",      3),
    "Article": (r"^Article\s+[IVX\d]",          1),
    "Chapter": (r"^Chapter\s+[IVX\d]",          1),
    "Section": (r"^Section\s+\d+(?!\.\d)",      1),
    "Part":    (r"^Part\s+[IVX\d]",             1),
}


def _styles_to_patterns(styles: list) -> list:
    """Convert GPT's style descriptors to compiled (re.Pattern, level) tuples."""
    result = []
    seen = set()
    for style in styles:
        key = style.strip()
        if key in _STYLE_PATTERNS and key not in seen:
            pattern_str, level = _STYLE_PATTERNS[key]
            result.append((re.compile(pattern_str), level))
            seen.add(key)
    return result


def _find_original_doc(doc_stem: str, backend_dir: str) -> str | None:
    """Find the original document file in data_storage/documents/."""
    docs_dir = os.path.join(backend_dir, "data_storage", "documents")
    if not os.path.isdir(docs_dir):
        return None
    for ext in (".pdf", ".PDF", ".docx", ".DOCX"):
        candidate = os.path.join(docs_dir, doc_stem + ext)
        if os.path.isfile(candidate):
            return candidate
    return None


def _render_pages_as_images(doc_path: str, max_pages: int) -> list:
    """
    Render the first max_pages of a PDF as base64-encoded PNG strings.
    Returns a list of base64 strings (one per page).
    """
    import base64
    try:
        import fitz
    except ImportError:
        return []

    images = []
    try:
        doc = fitz.open(doc_path)
        n   = min(max_pages, len(doc))
        mat = fitz.Matrix(1.5, 1.5)   # 1.5× zoom — readable but not huge
        for i in range(n):
            pix       = doc[i].get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("png")
            images.append(base64.b64encode(img_bytes).decode())
        doc.close()
    except Exception as e:
        print(f"  [Visual] Could not render pages: {e}")
    return images


def gpt_build_toc_visual(doc_path: str, total_pages: int, is_partial: bool) -> dict:
    """
    Gemini Flash reads the actual rendered PDF pages as images and builds a TOC.
    Same return shape as gpt_build_toc().
    Falls back to empty result on any error — caller falls back to text mode.
    """
    try:
        from google import genai
        from google.genai import types as genai_types
    except Exception:
        return {"needs_toc": False, "reason": "google-genai unavailable",
                "headers": [], "heading_formats": []}

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"needs_toc": False, "reason": "no GEMINI_API_KEY",
                "headers": [], "heading_formats": []}

    pages_to_render = GPT_PAGE_LIMIT if is_partial else total_pages
    print(f"  [Visual] Rendering {pages_to_render} page(s) from PDF...")
    images = _render_pages_as_images(doc_path, pages_to_render)
    if not images:
        return {"needs_toc": False, "reason": "could not render pages",
                "headers": [], "heading_formats": []}
    print(f"  [Visual] Rendered {len(images)} page image(s) — sending to Gemini Flash...")

    scope = (
        f"the first {len(images)} pages of a {total_pages}-page document"
        if is_partial else
        f"a {total_pages}-page document"
    )

    partial_task = (
        "\n5. HEADING_FORMATS — because this is only the first portion of a longer "
        "document, identify which heading styles you observed. Return them as a JSON "
        "array using ONLY descriptors from this list:\n"
        "   I.  1.  A.  a.  1)  a)  A)  (a)  (A)  (i)  1.1  1.1.1  "
        "Article  Chapter  Section  Part\n"
        "   Only include styles you actually saw.\n"
    ) if is_partial else ""

    partial_json_example = '\n  "heading_formats": ["I.", "A."],' if is_partial else ""

    text_prompt = (
        f"You are building a Table of Contents for {scope} that has no pre-existing TOC.\n"
        "The pages are provided as images — use the visual layout, font sizes, "
        "bold text, and indentation to identify section headings.\n\n"
        "YOUR TASKS:\n\n"
        "1. NEEDS_TOC — decide if this document is complex enough to need a TOC.\n"
        "   Set needs_toc=FALSE for letters, memos, single-topic docs, forms.\n"
        "   Set needs_toc=TRUE for multi-section reports, contracts, filings.\n\n"
        "2. HEADERS — list every real section heading you can see.\n"
        "   - Use visual cues: larger font, bold, ALL CAPS, centered, numbered.\n"
        "   - EXCLUDE: dates, salutations, addresses, signatures, body sentences.\n"
        "   - For page number: ALWAYS use the image sequence number (image 1 = page 1,\n"
        "     image 2 = page 2, etc.). Never use the printed page number on the page.\n\n"
        "3. SYNTHETIC headers — for large topic-shift gaps with no heading,\n"
        "   insert a 2-6 word title. Mark with \"synthetic\": true.\n\n"
        "4. LEVELS: level 1 = major, level 2 = subsection, level 3 = sub-subsection.\n\n"
        f"{partial_task}"
        "Return ONLY valid JSON (no markdown fences):\n"
        "{\n"
        '  "needs_toc": true,\n'
        '  "reason": "multi-section contract",'
        f"{partial_json_example}\n"
        '  "headers": [\n'
        '    {"level": 1, "title": "DEFINITIONS", "page": 2, "synthetic": false},\n'
        '    {"level": 1, "title": "OBLIGATIONS", "page": 8, "synthetic": false}\n'
        "  ]\n"
        "}"
    )

    # Build content parts: text prompt + one inline image per page
    import base64
    parts = [genai_types.Part.from_text(text=text_prompt)]
    for img_b64 in images:
        img_bytes = base64.b64decode(img_b64)
        parts.append(genai_types.Part.from_bytes(data=img_bytes, mime_type="image/png"))

    client = genai.Client(api_key=api_key)
    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=parts,
            config=genai_types.GenerateContentConfig(
                system_instruction=(
                    "You are a precise document structure analyst reading PDF page images. "
                    "Output only valid JSON with no markdown fences."
                ),
                temperature=0,
            ),
        )
        raw = resp.text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$",       "", raw)
        result = json.loads(raw)
    except Exception as e:
        print(f"  [Visual] Gemini error: {e}")
        return {"needs_toc": False, "reason": f"visual Gemini error: {e}",
                "headers": [], "heading_formats": []}

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

    print(f"  [Visual] needs_toc={result.get('needs_toc')}  |  headers returned: {len(headers)}")
    return {
        "needs_toc":       bool(result.get("needs_toc", True)),
        "reason":          str(result.get("reason", "")),
        "headers":         headers,
        "heading_formats": result.get("heading_formats", []),
    }


def gpt_build_toc(page_text: str, total_pages: int, is_partial: bool) -> dict:
    """
    GPT-4o-mini reads page_text and builds a TOC.

    is_partial=True  → first GPT_PAGE_LIMIT pages of a longer doc.
                       GPT also returns heading_formats so Python can scan the rest.
    is_partial=False → full document text, GPT returns the complete TOC.

    Returns:
        {
            "needs_toc":       bool,
            "reason":          str,
            "headers":         [{"level": int, "title": str, "page": int, "synthetic": bool}],
            "heading_formats": ["I.", "A.", ...]   # only meaningful when is_partial=True
        }
    """
    try:
        from openai import OpenAI
    except Exception:
        return {"needs_toc": False, "reason": "openai unavailable",
                "headers": [], "heading_formats": []}

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {"needs_toc": False, "reason": "no API key",
                "headers": [], "heading_formats": []}

    client = OpenAI(api_key=api_key)

    scope = (
        f"the first {GPT_PAGE_LIMIT} pages of a {total_pages}-page document"
        if is_partial else
        f"a {total_pages}-page document"
    )

    partial_task = (
        "\n5. HEADING_FORMATS — because this is only the first portion of a longer "
        "document, identify which heading styles you observed. Return them as a JSON "
        "array using ONLY descriptors from this list:\n"
        "   I.  1.  A.  a.  1)  a)  A)  (a)  (A)  (i)  1.1  1.1.1  "
        "Article  Chapter  Section  Part\n"
        "   Only include styles you actually saw in the text.\n"
    ) if is_partial else ""

    partial_json_example = (
        '\n  "heading_formats": ["I.", "A."],'
    ) if is_partial else ""

    prompt = (
        f"You are building a Table of Contents for {scope} that has no pre-existing TOC.\n\n"
        "DOCUMENT TEXT:\n"
        f"{page_text[:28_000]}\n\n"
        "YOUR TASKS:\n\n"
        "1. NEEDS_TOC — decide if this document is complex enough to benefit from a TOC.\n"
        "   Set needs_toc=FALSE for:\n"
        "     - Short letters, memos, emails, or single-topic documents\n"
        "     - Pure data tables, forms, or certificates\n"
        "     - Any document where a reader would not need navigation\n"
        "   Set needs_toc=TRUE for:\n"
        "     - Multi-section reports, agreements, contracts, or filings\n"
        "     - Any document with distinct named sections a reader might want to jump to\n\n"
        "2. HEADERS — if needs_toc=true, list every real section heading you find.\n"
        "   - Only include lines that clearly function as section headings.\n"
        "   - EXCLUDE: dates, salutations, party labels, signatures, address blocks,\n"
        "     'Sincerely,', the opening words of normal sentences.\n"
        "   - Include the exact page number where each heading appears.\n\n"
        "3. SYNTHETIC headers — if there is a content gap (3+ pages) with a clear\n"
        "   topic shift but no heading in the text, insert a synthetic header with\n"
        "   a concise 2-6 word title. Mark with \"synthetic\": true.\n\n"
        "4. LEVELS:\n"
        "   - level 1 = major section (top-level)\n"
        "   - level 2 = subsection\n"
        "   - level 3 = sub-subsection\n\n"
        f"{partial_task}"
        "Return ONLY valid JSON (no markdown fences):\n"
        "{\n"
        '  "needs_toc": true,\n'
        '  "reason": "35-page contract with multiple distinct clauses",'
        f"{partial_json_example}\n"
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
        return {"needs_toc": True, "reason": f"GPT error: {e}",
                "headers": [], "heading_formats": []}

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
        "needs_toc":       bool(result.get("needs_toc", True)),
        "reason":          str(result.get("reason", "")),
        "headers":         headers,
        "heading_formats": result.get("heading_formats", []),
    }


def scan_remaining_pages(full_text: str, patterns: list, from_page: int) -> list:
    """
    Scan pages from_page onwards using learned (pattern, level) tuples from GPT.
    Returns list of header dicts in the same format as gpt_build_toc headers.
    """
    _TAG = {1: "Header_1", 2: "Header_2", 3: "Header_3"}
    headers = []
    current_page = 1

    for line in full_text.splitlines():
        stripped = line.strip()
        m = _MD_PAGE_RE.match(stripped)
        if m:
            current_page = _page_num(m)
            continue
        if current_page < from_page or not stripped:
            continue

        for pattern, level in patterns:
            if pattern.match(stripped):
                headers.append({
                    "type":      _TAG[level],
                    "level":     level,
                    "text":      stripped,
                    "page":      current_page,
                    "synthetic": False,
                })
                break  # first matching pattern wins

    return headers


# ════════════════════════════════════════════════════════════════════════════
# STAGE 2 — GPT Validation (strip false positives)
# ════════════════════════════════════════════════════════════════════════════

def _build_skeleton(full_text: str, max_chars: int = 16_000) -> str:
    """
    Two-tier document skeleton for the validation prompt.

    Tier 1 — ALL short lines (≤ 70 chars) from the entire document.
    Tier 2 — Sampled prose from the first and last quarter.
    """
    short_lines = []
    prose_start = []
    prose_end   = []

    all_lines = full_text.splitlines()
    n         = len(all_lines)
    quarter   = max(1, n // 4)

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

    tier1     = "\n".join(short_lines)
    remaining = max_chars - len(tier1) - 200
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


def gpt_validate_headers(headers: list, skeleton: str) -> list:
    """
    Focused GPT pass that removes false positives from the final header list.
    Conservative: default is to KEEP. Only removes entries it is confident are wrong.
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
        "  - A regular sentence or running clause\n"
        "  - A bare date, year, or timestamp that is not a section title\n"
        "  - Garbled OCR output or non-Latin / mixed-character noise\n"
        "  - A numbered sub-item deep inside a section body\n"
        "  - A partial sentence or mid-paragraph fragment\n\n"
        "IMPORTANT — always keep an entry if:\n"
        "  - It follows the same clear numbering pattern as other entries in the list\n"
        "    (e.g. if I., II., III., IV., V. are all present, keep VI. even if unsure)\n"
        "  - It is a named document division (Article, Section, Schedule, Appendix, Conclusion)\n"
        "  - You are uncertain — when in doubt, keep it\n\n"
        "Return ONLY a JSON array of the INDEX numbers to KEEP (no markdown fences):\n"
        "[0, 1, 2, 3, 4, 5]"
    )

    import time
    for attempt in range(4):
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
            err_str = str(e)
            is_rate_limit = "429" in err_str or "rate_limit" in err_str
            if attempt < 3:
                wait = (15 * (attempt + 1)) if is_rate_limit else (2 ** attempt)
                if is_rate_limit:
                    print(f"  [Validation] Rate limit — waiting {wait}s before retry {attempt + 1}/3...")
                time.sleep(wait)
            else:
                print(f"  [Validation] Error: {e} — keeping all headers unchanged")
                return headers


# ════════════════════════════════════════════════════════════════════════════
# STAGE 3 — Build Section Tree
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
    Real headers: fuzzy-trim to start AFTER the header line.
    Synthetic headers: take from the top of the start page.
    """
    block = "\n".join(page_dict.get(p, "") for p in range(start_page, end_page + 1))
    if not block.strip():
        return ""
    lines     = block.splitlines()
    if synthetic:
        start_idx = 0
    else:
        heading_idx = next(
            (i for i, ln in enumerate(lines) if
             SequenceMatcher(None, ln.strip().lower(), title.lower()).ratio() >= 0.65),
            None,
        )
        start_idx = (heading_idx + 1) if heading_idx is not None else 0

    end_idx = len(lines)
    if next_title and not synthetic:
        for k in range(start_idx, len(lines)):
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

    # ── Synthetic TOC preview ─────────────────────────────────────────────────
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
    """Fallback for documents that don't need a TOC. One section with full content."""
    full_text = "\n\n".join(page_dict.get(p, "") for p in range(1, total_pages + 1))
    pg_range  = f"1-{total_pages}" if total_pages > 1 else "1"
    return pd.DataFrame([{
        "level": 0, "section": "Document Content", "is_synthetic": False,
        "start_page": 1, "end_page": total_pages, "page_range": pg_range,
        "section_text": full_text.strip(),
    }])


# ════════════════════════════════════════════════════════════════════════════
# STAGE 4 — Exhibit detection and splitting
# ════════════════════════════════════════════════════════════════════════════

_EXHIBIT_START_RE = re.compile(
    r"(?im)^(?:exhibit|attachment|schedule|appendix)\s*[A-Z0-9]"
)
_EXHIBIT_INLINE_RE = re.compile(
    r"(?i)\b(?:exhibit|ex\s*[-]?\s*\d+|ex\d+)\b"
)


def detect_and_split_exhibits(
    df: pd.DataFrame, page_dict: dict, total_pages: int
) -> tuple:
    """
    Scan section names and page content for exhibit markers.
    Returns (updated_df, exhibit_text, exhibit_start_page).
    """
    section_names = " ".join(str(r.get("section", "")) for _, r in df.iterrows())
    toc_row = df[df["section"] == "TOC (Synthetic)"]
    toc_preview = str(toc_row["section_text"].values[0]) if len(toc_row) else ""

    exhibit_in_toc = bool(
        _EXHIBIT_INLINE_RE.search(section_names)
        or _EXHIBIT_INLINE_RE.search(toc_preview)
    )

    exhibit_start_page = None
    scan_from = 1 if exhibit_in_toc else max(1, int(total_pages * 0.5))
    for p in range(scan_from, total_pages + 1):
        page_text = page_dict.get(p, "")
        matches   = list(_EXHIBIT_START_RE.finditer(page_text))
        if len(matches) >= 3:
            # 3+ matches on one page = exhibit index/table, not the actual start
            continue
        if matches:
            exhibit_start_page = p
            break

    if not exhibit_start_page:
        return df, "", None

    exhibit_text = "\n\n".join(
        page_dict.get(p, "") for p in range(exhibit_start_page, total_pages + 1)
    ).strip()

    if not exhibit_text:
        return df, "", None

    updated_df = df.copy()
    non_na = updated_df[updated_df["start_page"].notna()]
    if len(non_na) > 0:
        last_idx  = non_na.index[-1]
        new_end   = min(int(updated_df.loc[last_idx, "end_page"]), exhibit_start_page - 1)
        start_last = int(updated_df.loc[last_idx, "start_page"])
        if new_end >= start_last:
            updated_df.loc[last_idx, "end_page"]    = new_end
            updated_df.loc[last_idx, "page_range"]  = f"{start_last}-{new_end}"
            raw_title = str(updated_df.loc[last_idx, "section"]).strip().removeprefix("[AI] ")
            synth     = bool(updated_df.loc[last_idx, "is_synthetic"])
            updated_df.loc[last_idx, "section_text"] = get_section_text(
                raw_title, start_last, new_end, page_dict, synthetic=synth
            )
        else:
            updated_df = updated_df.drop(last_idx).reset_index(drop=True)

    return updated_df, exhibit_text, exhibit_start_page


# ════════════════════════════════════════════════════════════════════════════
# Final document builder
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

    # ── Stage 1: GPT-primary header detection ─────────────────────────────────
    needs_toc  = True
    headers    = []
    is_partial = total_pages > GPT_PAGE_LIMIT

    # Try visual mode first (original PDF pages as images)
    orig_doc = _find_original_doc(doc_stem, _BACKEND_DIR)
    if orig_doc:
        print(f"\nStage 1 — Visual mode: sending PDF page images to Gemini Flash...")
        print(f"  Source : {orig_doc}")
        result    = gpt_build_toc_visual(orig_doc, total_pages, is_partial=is_partial)
        needs_toc = result["needs_toc"]
        headers   = result["headers"]
        print(f"  Reason : {result.get('reason', '')}")
    else:
        print(f"  [Visual] Original PDF not found — falling back to text mode.")

    # Fall back to text mode if visual failed or no PDF found
    if not orig_doc or (not headers and needs_toc):
        if orig_doc:
            print(f"  [Visual] No headers returned — falling back to text mode.")
        if not is_partial:
            print(f"\nStage 1 — Text mode: {total_pages} pages (≤{GPT_PAGE_LIMIT}), GPT reading full document...")
            result    = gpt_build_toc(full_text, total_pages, is_partial=False)
            needs_toc = result["needs_toc"]
            headers   = result["headers"]
            print(f"  Reason : {result.get('reason', '')}")
        else:
            print(
                f"\nStage 1 — Text mode: {total_pages} pages (>{GPT_PAGE_LIMIT}), "
                f"GPT reading first {GPT_PAGE_LIMIT} pages..."
            )
            first_pages = _extract_pages(full_text, 1, GPT_PAGE_LIMIT)
            result      = gpt_build_toc(first_pages, total_pages, is_partial=True)
            needs_toc   = result["needs_toc"]
            headers     = result["headers"]
            styles      = result.get("heading_formats", [])
            print(f"  Reason          : {result.get('reason', '')}")
            print(f"  Heading styles  : {styles if styles else '(none identified)'}")

            if needs_toc and styles:
                patterns = _styles_to_patterns(styles)
                if patterns:
                    print(
                        f"\n  Scanning pages {GPT_PAGE_LIMIT + 1}–{total_pages} "
                        f"with {len(patterns)} learned pattern(s)..."
                    )
                    rest = scan_remaining_pages(full_text, patterns, from_page=GPT_PAGE_LIMIT + 1)
                    print(f"  Found {len(rest)} additional header(s) in remaining pages")
                    headers = headers + rest
                else:
                    print("  No styles mapped to patterns — using GPT headers only")
            elif needs_toc:
                print("  No heading styles returned — using GPT headers for first 20 pages only")

    # For long docs in visual mode: scan remaining pages with learned patterns
    if orig_doc and headers and needs_toc and is_partial:
        styles   = result.get("heading_formats", [])
        patterns = _styles_to_patterns(styles)
        if patterns:
            print(
                f"\n  Scanning pages {GPT_PAGE_LIMIT + 1}–{total_pages} "
                f"with {len(patterns)} pattern(s) learned from visual pass..."
            )
            rest = scan_remaining_pages(full_text, patterns, from_page=GPT_PAGE_LIMIT + 1)
            print(f"  Found {len(rest)} additional header(s) in remaining pages")
            headers = headers + rest

    print(f"\n  Total headers before validation : {len(headers)}")

    # ── Stage 2: Validation — strip false positives ───────────────────────────
    if headers and needs_toc:
        print(f"\nStage 2 — Validating {len(headers)} header(s) with GPT...")
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

    # ── Stage 4: Exhibit detection ────────────────────────────────────────────
    print("\nStage 4 — Checking for exhibits...")
    df, exhibit_text, exhibit_start_page = detect_and_split_exhibits(
        df, page_dict, total_pages
    )
    if exhibit_text:
        exhibit_md_path = os.path.join(temp_dir, doc_stem + "_exhibits.md")
        with open(exhibit_md_path, "w", encoding="utf-8") as f:
            f.write(exhibit_text)
        print(f"  Exhibit detected — starts at page {exhibit_start_page}")
        print(f"  Exhibit file    : {exhibit_md_path}")
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
