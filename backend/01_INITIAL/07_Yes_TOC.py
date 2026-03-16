"""
07_Yes_TOC.py - Process documents confirmed to have a Table of Contents.

Pipeline steps:
  1. Scan the markdown text (from 04_text_extraction.py) for TOC pages
  2. Extract TOC structure as Markdown via GPT-4o-mini (text-based, no file upload)
  3. Add sequential [Page N] markers to the main body text
  4. Remove repeated headers/footers
  5. Build section page-ranges from the TOC
  6. Detect and split exhibits
  7. Extract, clean, and save outputs

Outputs (to zz_temp_chunks/):
  - {stem}_07_toc_sections.csv   — secton table with extracted text
  - {stem}_07_final_document.md  — full pretty-printed document
  - {stem}_exhibits.md           — exhibit text (only if exhibits found)

Works with any file type (PDF, Word, email, HTML, etc.) because it reads
the markdown output of 04_text_extraction.py, not the original file.
"""

import sys
import os
import re
import pandas as pd
from difflib import SequenceMatcher
from dotenv import load_dotenv

# ── Locate and load .env ────────────────────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))          # backend/01_INITIAL
_BACKEND_DIR = os.path.dirname(_THIS_DIR)                        # backend/
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)                    # Projectfiles/
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))


# ════════════════════════════════════════════════════════════════════════════
# STEP 1 — TOC page detection from markdown text
# ════════════════════════════════════════════════════════════════════════════

# Matches all page marker formats produced by 04_text_extraction.py:
#   ## Page 3          (parse_standard_pdf)
#   ## Page 3 (Scanned)(parse_scanned_image)
#   [Page 3]           (_extract_best_effort_text)
_MD_PAGE_RE = re.compile(
    r"(?im)^(?:##\s*[Pp]age\s+(\d+)(?:\s*\([^)]*\))?|\[\s*[Pp]age\s+(\d+)\s*\])\s*$"
)

def _page_num(match) -> int:
    return int(match.group(1) or match.group(2))

def split_into_pages(text: str) -> list:
    """
    Split markdown text into [(page_number, page_text), ...] tuples.
    Handles all page marker formats from 04_text_extraction.py.
    Returns [] if no markers found.
    """
    matches = list(_MD_PAGE_RE.finditer(text))
    if not matches:
        return []
    pages = []
    for i, m in enumerate(matches):
        num = _page_num(m)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        pages.append((num, text[start:end].strip()))
    return pages

def _toc_signals(page_text: str) -> dict:
    text = page_text or ""
    return {
        "has_keyword": bool(re.search(r"\b(table\s+of\s+contents?|contents?|index)\b", text, re.IGNORECASE)),
        "dot_leaders": len(re.findall(r"\.{4,}", text)),
        "toc_lines": len(re.findall(r"(?im)^\s*[A-Za-z0-9][^\n]{5,}\.{3,}\s*\d+\s*$", text)),
    }

def _is_toc_page(sig: dict) -> bool:
    return (
        (sig["has_keyword"] and sig["dot_leaders"] >= 3)
        or sig["toc_lines"] >= 6
        or (sig["dot_leaders"] >= 8 and sig["toc_lines"] >= 3)
    )

def find_toc_pages_in_markdown(text: str) -> list:
    """
    Return page numbers that look like TOC pages, keeping the largest
    contiguous block. Works entirely from the markdown text.
    """
    pages = split_into_pages(text)
    candidates = [num for num, txt in pages if _is_toc_page(_toc_signals(txt))]
    if not candidates:
        return []
    blocks = [[candidates[0]]]
    for p in candidates[1:]:
        if p - blocks[-1][-1] <= 1:
            blocks[-1].append(p)
        else:
            blocks.append([p])
    return max(blocks, key=len)

def get_toc_text(text: str, toc_page_nums: list) -> str:
    """Concatenate the markdown text of the identified TOC pages."""
    page_dict = dict(split_into_pages(text))
    blocks = [page_dict.get(n, "") for n in sorted(toc_page_nums)]
    return "\n\n".join(b for b in blocks if b)

# Matches any common TOC header — table of contents, contents, index
_TOC_HEADER_RE = re.compile(
    r"(?im)^\s*(?:table\s+of\s+contents?|contents?|index)\s*$"
)

def extract_toc_region_from_text(text: str) -> str:
    """
    Fallback for documents with no page markers.
    Finds the TOC header (TABLE OF CONTENTS / CONTENTS / INDEX),
    then collects lines with dot leaders or spaced page numbers until they stop.
    Returns the raw TOC block as a string, or "" if not found.
    """
    m = _TOC_HEADER_RE.search(text)
    if not m:
        return ""

    lines = text[m.start():].splitlines()
    toc_lines = [lines[0].strip()]   # keep the header itself
    found_entries = False
    consecutive_blanks = 0

    for ln in lines[1:]:
        stripped = ln.strip()
        is_entry = bool(
            re.search(r"\.{3,}\s*\d+\s*$", stripped)
            or re.search(r"\s{3,}\d+\s*$", stripped)
        )
        if is_entry:
            toc_lines.append(stripped)
            found_entries = True
            consecutive_blanks = 0
        elif not stripped:
            consecutive_blanks += 1
            if found_entries and consecutive_blanks > 3:
                break   # too many blank lines after entries — TOC has ended
        else:
            if found_entries:
                # A non-entry non-blank line after entries = end of TOC
                break
            toc_lines.append(stripped)   # pre-entry context (e.g. "Page" column header)
            consecutive_blanks = 0

    return "\n".join(toc_lines) if found_entries else ""


# ════════════════════════════════════════════════════════════════════════════
# STEP 2 — Extract TOC markdown via GPT-4o-mini
# ════════════════════════════════════════════════════════════════════════════

def extract_toc_markdown_from_text(toc_text: str, model: str = "gpt-4o-mini") -> str:
    """Send the raw TOC page text to GPT-4o-mini and get back clean Markdown."""
    from openai import OpenAI as OpenAIClient

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY is not set in environment / .env.")

    client = OpenAIClient(api_key=api_key)

    response = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a legal document structure extractor.\n"
                    "Task: Extract only the Table of Contents structure from the provided text.\n"
                    "Output must be valid Markdown only (no code fences, no JSON, no commentary).\n"
                    "Requirements:\n"
                    "1) Return headings and sub-headings using nested bullet points (- item).\n"
                    "2) Preserve original order. Never merge two different section titles into one bullet.\n"
                    "3) Merge wrapped TOC lines (a title split across two lines of text is one entry); ignore layout artifacts.\n"
                    "4) Include page numbers after dot leaders (e.g. - Section Name .... 5).\n"
                    "5) Do not add or infer sections not present in the TOC.\n"
                    "6) Some TOCs use a two-column layout; after OCR the two columns collapse onto the same text line. "
                    "If you see two distinct section titles on the same line separated by whitespace or a dash, "
                    "output each as its own separate bullet point."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Extract the Table of Contents from the text below and return it as "
                    "clean hierarchical Markdown. Use nested bullet points with indentation "
                    "for sub-sections. Keep page numbers after dot leaders.\n\n"
                    f"---\n{toc_text}\n---"
                ),
            },
        ],
    )

    result = (response.choices[0].message.content or "").strip()
    if not result:
        raise ValueError("GPT-4o-mini returned empty TOC markdown.")
    return result


# Matches " - " where the following word is ALL-CAPS (two or more uppercase letters).
# This reliably identifies a merged two-column TOC entry boundary:
#   "TITLE1 ..... ii - TABLE OF CONTENTS"  →  split at " - TABLE"
#   "INTRODUCTION - STATEMENT OF COMPLIANCE"  →  split at " - STATEMENT"
# Hyphens inside words (NON-PARTY) and sub-heading markers (A., I.) are safe
# because they are not preceded by a space or not followed by two uppercase letters.
_MERGED_ENTRY_RE = re.compile(r"\s+-\s+(?=[A-Z]{2})")


def _split_merged_toc_entries(toc_markdown: str) -> str:
    """
    Post-process GPT's TOC markdown to undo merges caused by two-column layout.

    If a single bullet contains ' - UPPERCASE_SECTION' the content after the
    dash is a second entry that was collapsed onto the same line during OCR.
    Each piece is emitted as its own bullet at the same indentation level.
    """
    lines = toc_markdown.splitlines()
    result = []
    splits_made = 0

    for line in lines:
        m = re.match(r"^(\s*-\s+)(.*)", line)
        if not m:
            result.append(line)
            continue

        prefix  = m.group(1)                  # e.g. "  - "
        content = m.group(2)
        base    = prefix[: len(prefix) - 2]   # indentation without "- "

        parts = _MERGED_ENTRY_RE.split(content)
        if len(parts) > 1:
            splits_made += len(parts) - 1
            for part in parts:
                part = part.strip()
                if part:
                    result.append(f"{base}- {part}")
        else:
            result.append(line)

    if splits_made:
        print(f"    TOC post-process: split {splits_made} merged two-column entries.")
    return "\n".join(result)


# ════════════════════════════════════════════════════════════════════════════
# STEP 3 — Calibrate body-page offset, then stamp [Page N] markers
# ════════════════════════════════════════════════════════════════════════════

def _parse_first_toc_entries(toc_markdown: str, n: int = 5) -> list:
    """Return up to n (section_title, toc_page_number) pairs from toc_markdown."""
    entries = []
    for line in toc_markdown.splitlines():
        bullet = re.match(r"^(\s*)-\s+(.*)$", line)
        if not bullet:
            continue
        content = re.sub(r"\s+", " ", bullet.group(2)).strip()
        pm = re.search(r"\.{2,}\s*(\d+)\s*$", content) or re.search(r"\s(\d+)\s*$", content)
        if not pm:
            continue
        toc_page = int(pm.group(1))
        title = re.sub(r"\.{2,}$", "", content[:pm.start()].rstrip(" .")).strip()
        if len(title) >= 4:
            entries.append((title, toc_page))
        if len(entries) >= n:
            break
    return entries


def _fuzzy_find_offset(body_pages: list, entries: list) -> tuple:
    """
    Try to fuzzy-match each entry title against the first few lines of every
    body page.  Returns (offset, confidence) where confidence is the number
    of consistently matching entries.  offset = physical_page - toc_page.
    Returns (None, 0) if nothing confident is found.
    """
    from collections import Counter
    offsets = []

    for title, toc_page in entries:
        title_low = title.lower()[:60]
        for phys_num, page_text in body_pages:
            # Only check the first ~20 lines — section headers appear near the top
            for line in page_text.splitlines()[:20]:
                line_low = line.strip().lower()[:80]
                if not line_low:
                    continue
                ratio = SequenceMatcher(None, title_low, line_low).ratio()
                if ratio >= 0.72:
                    offsets.append(phys_num - toc_page)
                    break  # found this entry, move to next
            else:
                continue
            break  # stop searching pages for this entry

    if not offsets:
        return None, 0

    most_common_offset, count = Counter(offsets).most_common(1)[0]
    return most_common_offset, count


def _gpt_find_offset(body_pages: list, entries: list, model: str = "gpt-4o-mini") -> int | None:
    """
    Ask GPT-4o-mini to identify which physical page each of the first few
    section titles actually starts on, then derive the offset.
    Returns offset (int) or None if the call fails.
    """
    from openai import OpenAI as OpenAIClient

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    client = OpenAIClient(api_key=api_key)

    # Build a compact excerpt: first 5 lines of each of the first 30 body pages
    pages_excerpt = []
    for phys_num, page_text in body_pages[:30]:
        preview = "\n".join(page_text.splitlines()[:5])
        pages_excerpt.append(f"[Physical page {phys_num}]\n{preview}")
    pages_text = "\n\n".join(pages_excerpt)

    entries_text = "\n".join(f"- TOC page {tp}: {title}" for title, tp in entries)

    prompt = (
        "Below are the first few section titles from a document's Table of Contents "
        "with their stated page numbers, followed by a preview of each physical page "
        "of the document body (labelled with physical page numbers).\n\n"
        "For each TOC entry, identify the physical page where that section actually "
        "starts.  Return ONLY a JSON array of objects, e.g.:\n"
        '[{"title":"Introduction","toc_page":1,"physical_page":4}]\n'
        "If you cannot find a section, omit it.  No commentary.\n\n"
        f"TOC ENTRIES:\n{entries_text}\n\n"
        f"DOCUMENT PAGES:\n{pages_text}"
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
        )
        raw = (resp.choices[0].message.content or "").strip()
        # Parse JSON
        import json
        data = json.loads(raw)
        from collections import Counter
        offsets = [
            int(item["physical_page"]) - int(item["toc_page"])
            for item in data
            if "physical_page" in item and "toc_page" in item
        ]
        if offsets:
            return Counter(offsets).most_common(1)[0][0]
    except Exception as exc:
        print(f"    Page calibration (GPT): call failed — {exc}")
    return None


def calibrate_body_page_offset(
    text: str, toc_markdown: str, toc_page_nums: list
) -> int:
    """
    Determine how many physical pages to skip before [Page 1] by finding
    where the first document section actually starts.

    Strategy:
      1. Fuzzy-match the first 5 TOC titles against body page text.
         Accept if >= 2 entries agree on the same offset.
      2. If fuzzy fails, call GPT-4o-mini with page previews.
      3. Fall back to last_toc_page (original sequential behaviour).

    Returns body_page_offset where: document_page = physical_page - offset.
    """
    default_offset = max(toc_page_nums) if toc_page_nums else 0

    entries = _parse_first_toc_entries(toc_markdown, n=5)
    if not entries:
        print(f"    Page calibration: no parseable TOC entries → default offset {default_offset}")
        return default_offset

    body_pages = [
        (num, txt) for num, txt in split_into_pages(text)
        if num > default_offset
    ]
    if not body_pages:
        return default_offset

    # ── Try fuzzy matching ────────────────────────────────────────────────────
    offset, confidence = _fuzzy_find_offset(body_pages, entries)
    if offset is not None and confidence >= 2:
        print(f"    Page calibration (fuzzy): offset={offset}, confidence={confidence}/{len(entries)}")
        return offset
    if offset is not None and confidence == 1:
        print(f"    Page calibration (fuzzy): only 1 match (offset={offset}) — trying GPT to confirm")
    else:
        print(f"    Page calibration (fuzzy): no matches — trying GPT")

    # ── Fuzzy was insufficient — fall back to GPT ─────────────────────────────
    gpt_offset = _gpt_find_offset(body_pages, entries)
    if gpt_offset is not None:
        print(f"    Page calibration (GPT): offset={gpt_offset}")
        return gpt_offset

    print(f"    Page calibration: all methods failed → default offset {default_offset}")
    return default_offset


def add_page_numbers(text: str, toc_page_nums: list, body_page_offset: int = None) -> tuple:
    """
    Stamp [Page N] markers onto body pages using a calibrated offset so that
    [Page N] in the output corresponds to page N in the document's own numbering.

    body_page_offset  physical_page - body_page_offset = document_page_number
    If None, defaults to last_toc_page (original sequential behaviour).

    Pages that fall before document page 1 (i.e. between the TOC and first
    section) are included without a [Page N] marker.

    Returns (text_with_markers, highest_document_page_seen).
    """
    pages = split_into_pages(text)
    if not pages:
        return text, 0

    last_toc_page = max(toc_page_nums) if toc_page_nums else 0
    if body_page_offset is None:
        body_page_offset = last_toc_page

    first_match = _MD_PAGE_RE.search(text)
    pre_text = text[:first_match.start()].rstrip("\n") if first_match else ""

    blocks = []
    max_doc_page = 0
    for orig_num, page_text in pages:
        if orig_num <= last_toc_page:
            if page_text.strip():
                blocks.append(page_text)
        else:
            doc_page = orig_num - body_page_offset
            if doc_page >= 1:
                blocks.append(f"[Page {doc_page}]\n{page_text}")
                max_doc_page = max(max_doc_page, doc_page)
            else:
                # Pre-content page between TOC and first section — include unmarked
                if page_text.strip():
                    blocks.append(page_text)

    rebuilt = "\n\n".join(b for b in blocks if b.strip())
    result = (f"{pre_text}\n\n{rebuilt}".strip() if pre_text.strip() else rebuilt)
    return result, max_doc_page


def detect_intermediate_section(
    text: str, toc_page_nums: list, body_page_offset: int
) -> str:
    """
    Collect text from physical pages that sit between the end of the TOC and
    the first numbered body page (document_page < 1 with the calibrated offset).
    Returns the concatenated text, or "" if there are no such pages.
    """
    if not toc_page_nums or body_page_offset is None:
        return ""
    last_toc_page = max(toc_page_nums)
    parts = []
    for orig_num, page_text in split_into_pages(text):
        if orig_num <= last_toc_page:
            continue
        if orig_num - body_page_offset >= 1:
            break           # reached the first body section
        if page_text.strip():
            parts.append(page_text.strip())
    return "\n\n".join(parts)


def label_intermediate_section(text: str, model: str = "gpt-4o-mini") -> str:
    """
    Name the intermediate section.  First tries to use the first clean header
    line in the text.  Falls back to GPT-4o-mini if no clear header is found.
    """
    # Sniff the first non-empty, reasonably short line as a header
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        word_count = len(line.split())
        # Accept 1–10 word lines that aren't pure numbers / dates
        if 1 <= word_count <= 10 and len(line) <= 100:
            if not re.fullmatch(r"[\d\s\-/]+", line):   # skip pure number/date lines
                return line

    # GPT fallback
    from openai import OpenAI as OpenAIClient
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return "Preliminary Section"
    client = OpenAIClient(api_key=api_key)
    sample = text[:2000]
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[{
                "role": "user",
                "content": (
                    "The following text is a section of a legal/financial document that "
                    "appears between the Table of Contents and the first main section. "
                    "What is this section called? Return ONLY the section title (2–6 words), "
                    "nothing else.\n\n"
                    f"TEXT:\n{sample}"
                ),
            }],
            max_tokens=30,
        )
        label = (resp.choices[0].message.content or "").strip().strip('"\'')
        return label if label else "Preliminary Section"
    except Exception:
        return "Preliminary Section"


# ════════════════════════════════════════════════════════════════════════════
# STEP 4 — Remove repeated headers / broken page numbers
# ════════════════════════════════════════════════════════════════════════════

def remove_repeated_headers(text: str) -> str:
    """
    Remove lines that appear on >=80 % of pages (headers/footers) and
    stray OCR page-number lines.
    """
    raw = text.replace("\r\n", "\n").replace("\r", "\n")
    marker_re = re.compile(r"^\s*(?:\[\s*page\s*\d+\s*\]|##\s*page\s*\d+)", re.IGNORECASE | re.MULTILINE)

    def split_pages(t):
        ms = list(marker_re.finditer(t))
        if not ms:
            return []
        return [
            {"marker": m.group(0).strip(),
             "body": t[m.end(): ms[i+1].start() if i+1 < len(ms) else len(t)].lstrip("\n")}
            for i, m in enumerate(ms)
        ]

    pages = split_pages(raw)
    if not pages:
        return raw

    def norm(s):
        s = re.sub(r"\s+", " ", (s or "").strip().lower())
        s = re.sub(r"\bpage\s*\d{1,4}\b", "page #", s, re.IGNORECASE)
        return re.sub(r"^\d{1,4}$", "#", s)

    old_pagenum_re = re.compile(
        r"^\s*(?:\[?\s*page\s*\d{1,4}\s*\]?|[-–—]?\s*\d{1,4}\s*[-–—]?)\s*$", re.IGNORECASE
    )

    # Count how many pages each normalised short line appears on
    line_counts: dict = {}
    for p in pages:
        seen: set = set()
        for ln in (p["body"] or "").splitlines():
            raw_ln = ln.strip()
            if raw_ln and len(raw_ln) <= 150:
                n = norm(raw_ln)
                if n:
                    seen.add(n)
        for n in seen:
            line_counts[n] = line_counts.get(n, 0) + 1

    threshold = max(1, int(len(pages) * 0.8 + 0.9999))
    repeated = {n for n, c in line_counts.items() if c >= threshold}

    cleaned = []
    for p in pages:
        kept = []
        for ln in (p["body"] or "").splitlines():
            raw_ln = ln.strip()
            if not raw_ln:
                kept.append(ln)
                continue
            if old_pagenum_re.match(raw_ln) or (len(raw_ln) <= 150 and norm(raw_ln) in repeated):
                continue
            kept.append(ln)
        body = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip("\n")
        cleaned.append({"marker": p["marker"], "body": body})

    # Also remove lines that repeat at the same position after every page marker
    pos_counts: list = [{}, {}]
    for p in cleaned:
        lines = (p["body"] or "").splitlines()
        for pos in range(min(2, len(lines))):
            cand = lines[pos].strip()
            if cand:
                pos_counts[pos][cand] = pos_counts[pos].get(cand, 0) + 1
    post_thresh = max(3, int(len(cleaned) * 0.8 + 0.9999))
    repeated_post = {
        pos: {t for t, c in counts.items() if c >= post_thresh}
        for pos, counts in enumerate(pos_counts)
    }

    final = []
    for p in cleaned:
        lines = (p["body"] or "").splitlines()
        for pos in reversed(range(min(2, len(lines)))):
            if lines[pos].strip() in repeated_post.get(pos, set()):
                lines.pop(pos)
        body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")
        final.append({"marker": p["marker"], "body": body})

    first_m = marker_re.search(raw)
    pre = raw[:first_m.start()].rstrip("\n") if first_m else ""

    # Apply the same repeated-line filter to the title page (pre) text.
    # The `repeated` set and `old_pagenum_re` were built from body pages above,
    # so this removes the exact same running headers/footers from the cover pages.
    if pre:
        pre_lines = []
        for ln in pre.splitlines():
            raw_ln = ln.strip()
            if raw_ln and (
                old_pagenum_re.match(raw_ln)
                or (len(raw_ln) <= 150 and norm(raw_ln) in repeated)
            ):
                continue   # drop repeated header/footer line
            pre_lines.append(ln)
        pre = re.sub(r"\n{3,}", "\n\n", "\n".join(pre_lines)).rstrip("\n")

    rebuilt = "\n\n".join(f"{p['marker']}\n{p['body']}".rstrip() for p in final).strip()
    return (f"{pre}\n\n{rebuilt}".strip() if pre else rebuilt)


# ════════════════════════════════════════════════════════════════════════════
# STEP 5 — Parse TOC entries and assign page ranges
# ════════════════════════════════════════════════════════════════════════════

def parse_toc_entries(toc_markdown: str, total_pages: int) -> pd.DataFrame:
    entries = []
    for line in toc_markdown.splitlines():
        if not line.strip():
            continue
        if re.match(r"^\s*#+\s*table of contents\s*$", line, re.IGNORECASE):
            continue
        bullet = re.match(r"^(\s*)-\s+(.*)$", line)
        if not bullet:
            continue
        indent, content = bullet.groups()
        level = len(indent) // 2
        content = re.sub(r"\s+", " ", content).strip()
        pm = re.search(r"\.{2,}\s*(\d+)\s*$", content) or re.search(r"\s(\d+)\s*$", content)
        if not pm:
            continue
        start_page = int(pm.group(1))
        section = re.sub(r"\.{2,}$", "", content[:pm.start()].rstrip(" .")).strip()
        entries.append({"level": level, "section": section, "start_page": start_page})

    if not entries:
        raise ValueError("No TOC entries could be parsed from toc_markdown.")

    for i, e in enumerate(entries):
        end_page = total_pages
        for j in range(i + 1, len(entries)):
            if entries[j]["level"] <= e["level"]:
                end_page = entries[j]["start_page"] - 1
                break
        e["end_page"] = max(e["start_page"], end_page)
        e["page_range"] = f"{e['start_page']}-{e['end_page']}"

    df = pd.DataFrame(entries)
    df["section"] = df.apply(lambda r: f"{'  ' * int(r['level'])}{r['section']}", axis=1)
    df = df[["level", "section", "start_page", "end_page", "page_range"]]

    prefix = pd.DataFrame([
        {"level": 0, "section": "Title Page", "start_page": pd.NA, "end_page": pd.NA, "page_range": ""},
        {"level": 0, "section": "TOC",        "start_page": pd.NA, "end_page": pd.NA, "page_range": ""},
    ])
    return pd.concat([prefix, df], ignore_index=True)


# ════════════════════════════════════════════════════════════════════════════
# STEP 6 — Detect and split exhibits
# ════════════════════════════════════════════════════════════════════════════

def handle_exhibits(toc_df: pd.DataFrame, doc_text: str, toc_markdown: str) -> tuple:
    """
    Returns (main_text, exhibit_text, exhibit_page_number, updated_toc_df).
    exhibit_page_number is the [Page N] number where exhibits start, or None.
    """
    exhibit_re = re.compile(r"(?i)\b(?:exhibit|ex\s*[-]?\s*\d+|ex\d+)\b")

    non_na = toc_df[toc_df["start_page"].notna()]
    should_split = False
    if not non_na.empty:
        last = non_na.iloc[-1]
        last_span = max(0, int(last["end_page"]) - int(last["start_page"]) + 1)
        should_split = (
            last_span > 10
            or bool(exhibit_re.search(str(last["section"])))
            or bool(exhibit_re.search(toc_markdown))
        )

    if not should_split:
        return doc_text, "", None, toc_df

    # Scan [Page N] blocks to find first page containing an exhibit token
    page_marker_re = re.compile(r"(?im)^\s*\[page\s+(\d+)\]\s*$")
    matches = list(page_marker_re.finditer(doc_text))
    exhibit_start_page = None
    for i, m in enumerate(matches):
        block_text = doc_text[m.end(): matches[i+1].start() if i+1 < len(matches) else len(doc_text)]
        if exhibit_re.search(block_text):
            exhibit_start_page = int(m.group(1))
            break

    main_text = doc_text
    exhibit_text = ""

    if exhibit_start_page:
        pat = re.compile(rf"(?im)^\s*\[page\s+{exhibit_start_page}\s*\]\s*$")
        pm = pat.search(doc_text)
        if pm:
            main_text = doc_text[:pm.start()].strip()
            exhibit_text = doc_text[pm.start():].strip()

    # Fallback: split on first bare exhibit token
    if not exhibit_text:
        m2 = exhibit_re.search(doc_text)
        if m2:
            main_text = doc_text[:m2.start()].strip()
            exhibit_text = doc_text[m2.start():].strip()

    # Update last TOC entry's end_page
    toc_updated = toc_df.copy()
    if exhibit_text and exhibit_start_page:
        non_na_idx = toc_updated[toc_updated["start_page"].notna()].index
        if len(non_na_idx) > 0:
            last_idx = non_na_idx[-1]
            new_end = min(int(toc_updated.loc[last_idx, "end_page"]), exhibit_start_page - 1)
            if new_end >= int(toc_updated.loc[last_idx, "start_page"]):
                toc_updated.loc[last_idx, "end_page"] = new_end
                toc_updated.loc[last_idx, "page_range"] = (
                    f"{int(toc_updated.loc[last_idx, 'start_page'])}-{new_end}"
                )
            else:
                toc_updated = toc_updated.drop(last_idx).reset_index(drop=True)

    return main_text, exhibit_text, exhibit_start_page, toc_updated


# ════════════════════════════════════════════════════════════════════════════
# STEP 7a — Extract section text from paginated document
# ════════════════════════════════════════════════════════════════════════════

def extract_section_texts(
    toc_df: pd.DataFrame, paginated_text: str, toc_markdown: str
) -> pd.DataFrame:
    def fuzzy(a: str, b: str, threshold=0.8) -> bool:
        return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= threshold

    # ── Path A: page-marker-based lookup ─────────────────────────────────────
    page_marker_re = re.compile(r"(?im)^\s*\[page\s+(\d+)\]\s*$")
    matches = list(page_marker_re.finditer(paginated_text))
    page_dict = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i+1].start() if i+1 < len(matches) else len(paginated_text)
        page_dict[int(m.group(1))] = paginated_text[start:end].strip()

    def get_text_by_page(section: str, start_page, end_page, next_section=None) -> str:
        section = section.strip()
        next_section = next_section.strip() if next_section else None
        block = "\n".join(page_dict.get(p, "") for p in range(int(start_page), int(end_page) + 1))
        if not block.strip():
            return ""
        lines = block.splitlines()
        start_idx = next(
            (idx for idx, ln in enumerate(lines) if fuzzy(ln.strip(), section, 0.7)), 0
        )
        end_idx = len(lines)
        if next_section:
            for idx in range(start_idx + 1, len(lines)):
                if fuzzy(lines[idx].strip(), next_section, 0.7):
                    end_idx = idx
                    break
        return "\n".join(lines[start_idx:end_idx]).strip()

    # ── Path B: positional search (no page markers in document) ──────────────
    # Find each section header's character position in the full text, then slice
    # between consecutive headers.
    def build_position_index(sections: list) -> dict:
        """
        For each section name, find its start position in paginated_text.
        Returns {section_name: char_position} sorted by position.
        """
        index = {}
        for sec in sections:
            sec_stripped = sec.strip()
            if not sec_stripped:
                continue
            # Exact case-insensitive search first
            m = re.search(re.escape(sec_stripped), paginated_text, re.IGNORECASE)
            if m:
                index[sec] = m.start()
                continue
            # Fuzzy: scan every line for a close match
            for lm in re.finditer(r".+", paginated_text):
                if fuzzy(lm.group(0).strip(), sec_stripped, 0.75):
                    index[sec] = lm.start()
                    break
        return index

    def get_text_by_position(section: str, next_section=None, pos_index: dict = None) -> str:
        if pos_index is None or section not in pos_index:
            return ""
        start = pos_index[section]
        end = len(paginated_text)
        if next_section and next_section in pos_index:
            end = pos_index[next_section]
        return paginated_text[start:end].strip()

    # Decide which path to use
    use_page_lookup = bool(page_dict)

    # Build position index only when needed (no page markers)
    pos_index = {}
    if not use_page_lookup:
        body_sections = [
            str(row["section"])
            for _, row in toc_df.iterrows()
            if not pd.isna(row.get("start_page"))
        ]
        pos_index = build_position_index(body_sections)

    # ── Extract text for each TOC row ─────────────────────────────────────────
    rows = []
    df = toc_df.reset_index(drop=True)
    for i, row in df.iterrows():
        sp, ep = row["start_page"], row["end_page"]
        next_sec = str(df.iloc[i + 1]["section"]) if i + 1 < len(df) else None

        if pd.isna(sp) or pd.isna(ep):
            section_text = ""
        elif use_page_lookup:
            section_text = get_text_by_page(str(row["section"]), sp, ep, next_sec)
        else:
            section_text = get_text_by_position(str(row["section"]), next_sec, pos_index)

        new_row = dict(row)
        new_row["section_text"] = section_text
        rows.append(new_row)

    result = pd.DataFrame(rows)

    # Row 0 = Title Page: everything before the TOC header in the document.
    # Uses _TOC_HEADER_RE which covers TABLE OF CONTENTS, CONTENTS, and INDEX.
    toc_match = _TOC_HEADER_RE.search(paginated_text)
    result.loc[0, "section_text"] = paginated_text[:toc_match.start()].strip() if toc_match else ""

    # Row 1 = TOC: the raw markdown extracted by GPT
    result.loc[1, "section_text"] = toc_markdown

    return result


# ════════════════════════════════════════════════════════════════════════════
# STEP 7b — Clean section texts
# ════════════════════════════════════════════════════════════════════════════

def clean_section_texts(df: pd.DataFrame) -> pd.DataFrame:
    exhibit_re = re.compile(r"(?im)^(exhibit\b.*?)$")

    def fuzzy_remove_header(section_name: str, section_text: str, threshold=0.8) -> str:
        lines = section_text.splitlines()
        if lines and SequenceMatcher(None, section_name.strip().lower(), lines[0].strip().lower()).ratio() >= threshold:
            return "\n".join(lines[1:]).lstrip()
        return section_text

    def merge_gapped_lines(text: str) -> str:
        lines = text.splitlines()
        merged = []
        i = 0
        while i < len(lines):
            ln = lines[i]
            if not ln.strip():
                merged.append(ln)
                i += 1
                continue
            if i + 1 < len(lines):
                nxt = lines[i + 1]
                if re.match(r"^\s{2,}|\t", nxt) or re.search(r"[.:;]$", ln.strip()) or not nxt.strip():
                    merged.append(ln)
                    i += 1
                    continue
                merged.append(ln.rstrip() + " " + nxt.lstrip())
                i += 2
            else:
                merged.append(ln)
                i += 1
        return "\n".join(merged)

    def fix_spaces(text: str) -> str:
        return "\n".join(
            re.sub(r"(?<=\w)\s{2,}(?=\w)", " ", ln) if ln.strip() else ln
            for ln in text.splitlines()
        )

    df = df.copy()
    df["section_text"] = df.apply(
        lambda r: fuzzy_remove_header(str(r["section"]), str(r["section_text"])), axis=1
    )
    df["section_text"] = df["section_text"].apply(merge_gapped_lines)
    df["section_text"] = df["section_text"].apply(fix_spaces)

    # Strip exhibit overflow from the last real-content section
    last_idx = df.index[-1]
    last_text = str(df.loc[last_idx, "section_text"])
    m = exhibit_re.search(last_text)
    if m and len(last_text[m.start():]) > 500:
        df.loc[last_idx, "section_text"] = last_text[:m.start()].strip()

    return df


# ════════════════════════════════════════════════════════════════════════════
# STEP 7c — Build final pretty-printed markdown document
# ════════════════════════════════════════════════════════════════════════════

def build_final_document(df: pd.DataFrame) -> str:
    lines = []
    for _, row in df.iterrows():
        level = int(row.get("level", 0) or 0)
        section = str(row.get("section", "")).strip()
        page_range = str(row.get("page_range", "")).strip()
        section_text = str(row.get("section_text", "")).strip()

        h_indent = "  " * level
        m_indent = h_indent + "  "
        p_indent = h_indent + "    "

        lines.append(f"{h_indent}{section}")
        if page_range:
            lines.append(f"{m_indent}(Pages: {page_range})")
        if section_text:
            for para in re.split(r"\n\s*\n", section_text):
                if para.strip():
                    for ln in para.splitlines():
                        lines.append(f"{p_indent}{ln.rstrip()}")
                    lines.append("")
        else:
            lines.append(f"{p_indent}[No extracted text]")
            lines.append("")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) != 2:
        print("Usage: python 07_Yes_TOC.py <text_extraction_md>")
        sys.exit(1)

    text_md = sys.argv[1]
    if not os.path.isfile(text_md):
        print(f"ERROR: File not found: {text_md}")
        sys.exit(1)

    # Derive working paths
    stem = os.path.splitext(os.path.basename(text_md))[0]   # e.g. complaint_text_extraction
    doc_stem = stem.replace("_text_extraction", "")           # e.g. complaint
    temp_dir = os.path.dirname(os.path.abspath(text_md))      # backend/zz_temp_chunks/

    print("=" * 60)
    print("07_YES_TOC — Document with Table of Contents")
    print("=" * 60)
    print(f"  Text MD : {text_md}")

    with open(text_md, "r", encoding="utf-8") as f:
        combined_parsed_text = f.read()

    # ── [1/7] Find TOC pages in the markdown text ────────────────────────────
    print("\n[1/7] Detecting TOC pages in markdown text...")
    toc_page_nums = find_toc_pages_in_markdown(combined_parsed_text)
    has_page_markers = bool(split_into_pages(combined_parsed_text))

    if toc_page_nums:
        print(f"    TOC page numbers found : {toc_page_nums}")
        toc_raw_text = get_toc_text(combined_parsed_text, toc_page_nums)
    else:
        if not has_page_markers:
            print("    No page markers in text — extracting TOC region directly.")
        else:
            print("    No TOC pages matched heuristic — extracting TOC region directly.")
        toc_raw_text = extract_toc_region_from_text(combined_parsed_text)
        if toc_raw_text:
            print(f"    TOC region found in raw text ({len(toc_raw_text):,} chars).")
        else:
            print("    WARNING: Could not locate TOC region.")

    # ── [2/7] Extract TOC markdown via GPT-4o-mini ──────────────────────────
    print("\n[2/7] Extracting TOC structure with GPT-4o-mini...")
    toc_markdown = ""

    if toc_raw_text:
        toc_markdown = extract_toc_markdown_from_text(toc_raw_text)
        toc_markdown = _split_merged_toc_entries(toc_markdown)
        print(f"    TOC markdown extracted ({len(toc_markdown):,} chars).")
    else:
        print("    ERROR: Could not locate TOC content by any method.")
        sys.exit(1)

    # ── [3/7] Calibrate body-page offset, then stamp [Page N] markers ────────
    print("\n[3/7] Calibrating body-page offset and adding [Page N] markers...")
    if toc_page_nums and split_into_pages(combined_parsed_text):
        body_page_offset = calibrate_body_page_offset(
            combined_parsed_text, toc_markdown, toc_page_nums
        )
    else:
        body_page_offset = None   # no page markers → add_page_numbers is a no-op anyway

    # Capture intermediate section BEFORE page markers are rewritten
    inter_text = detect_intermediate_section(
        combined_parsed_text, toc_page_nums, body_page_offset
    )
    inter_label = None
    if inter_text:
        inter_label = label_intermediate_section(inter_text)
        print(f"    Intermediate section : '{inter_label}' ({len(inter_text):,} chars)")
    else:
        print("    Intermediate section : none")

    combined_parsed_text_1, total_pages = add_page_numbers(
        combined_parsed_text, toc_page_nums, body_page_offset
    )
    print(f"    Body page offset     : {body_page_offset}")
    print(f"    Total numbered pages : {total_pages}")

    # ── [4/7] Remove repeated headers/footers ───────────────────────────────
    print("\n[4/7] Removing repeated headers and broken page numbers...")
    combined_parsed_text_2 = remove_repeated_headers(combined_parsed_text_1)
    print(f"    Text length after cleaning : {len(combined_parsed_text_2):,} chars")

    # ── [5/7] Build section ranges ──────────────────────────────────────────
    print("\n[5/7] Building section page-ranges from TOC...")
    toc_df = parse_toc_entries(toc_markdown, total_pages)

    # Insert intermediate section row after the TOC row (index 1) if one was found
    if inter_label:
        inter_row = pd.DataFrame([{
            "level": 0, "section": inter_label,
            "start_page": pd.NA, "end_page": pd.NA, "page_range": "",
        }])
        toc_df = pd.concat(
            [toc_df.iloc[:2], inter_row, toc_df.iloc[2:]], ignore_index=True
        )
    print(f"    Sections (incl. Title Page & TOC rows) : {len(toc_df)}")

    # ── [6/7] Detect and split exhibits ─────────────────────────────────────
    print("\n[6/7] Checking for exhibits...")
    main_text, exhibit_text, exhibit_start_page, toc_df = handle_exhibits(
        toc_df, combined_parsed_text_2, toc_markdown
    )
    if exhibit_text:
        exhibit_md_path = os.path.join(temp_dir, doc_stem + "_exhibits.md")
        with open(exhibit_md_path, "w", encoding="utf-8") as f:
            f.write(exhibit_text)
        print(f"    ✅ EXHIBIT DETECTED — starts at [Page {exhibit_start_page}]")
        print(f"    Exhibit file saved : {exhibit_md_path}")
    else:
        print("    ❌ No exhibit detected.")

    # ── [7/7] Extract, clean, and save outputs ───────────────────────────────
    print("\n[7/7] Extracting section texts, cleaning, and saving outputs...")
    toc_with_text = extract_section_texts(toc_df, main_text, toc_markdown)
    toc_with_text = clean_section_texts(toc_with_text)

    # Fill the intermediate section's text (it has no [Page N] markers so
    # extract_section_texts would have returned "" for it)
    if inter_label:
        mask = toc_with_text["section"] == inter_label
        if mask.any():
            toc_with_text.loc[mask, "section_text"] = inter_text

    csv_out = os.path.join(temp_dir, doc_stem + "_07_toc_sections.csv")
    toc_with_text.to_csv(csv_out, index=False, encoding="utf-8")
    print(f"    Section table CSV  : {csv_out}")

    final_md = build_final_document(toc_with_text)
    md_out = os.path.join(temp_dir, doc_stem + "_07_final_document.md")
    with open(md_out, "w", encoding="utf-8") as f:
        f.write(final_md)
    print(f"    Final document MD  : {md_out}")

    print("\n" + "=" * 60)
    print("07_YES_TOC — COMPLETE")
    print(f"  Sections processed : {len(toc_with_text)}")
    print(f"  Exhibit detected   : {'YES — starts at [Page ' + str(exhibit_start_page) + ']' if exhibit_text else 'NO'}")
    print(f"  Output CSV         : {csv_out}")
    print(f"  Output MD          : {md_out}")
    print("=" * 60)


if __name__ == "__main__":
    main()
