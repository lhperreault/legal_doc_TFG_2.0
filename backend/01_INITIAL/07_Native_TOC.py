"""
07_Native_TOC.py — Fast processor for "smart" PDFs with embedded bookmarks.

Skips GPT extraction entirely.  Reads the native TOC saved by parse_smart_pdf
in 04_text_extraction.py ({stem}_native_toc.json) and maps every section to
the page range derived from the embedded bookmark page numbers.

Outputs (to zz_temp_chunks/) — identical format to 07_Yes_TOC.py:
  - {stem}_07_toc_sections.csv
  - {stem}_07_final_document.md
"""

import sys
import os
import re
import json
import pandas as pd
from difflib import SequenceMatcher

_THIS_DIR    = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_THIS_DIR)
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)

# Matches ## Page N  /  ## Page N (Scanned)  /  [Page N]
_MD_PAGE_RE = re.compile(
    r"(?im)^(?:##\s*[Pp]age\s+(\d+)(?:\s*\([^)]*\))?|\[\s*[Pp]age\s+(\d+)\s*\])\s*$"
)

def _page_num(m) -> int:
    return int(m.group(1) or m.group(2))


# ════════════════════════════════════════════════════════════════════════════
# Repeated header / footer removal  (ported from 07_Yes_TOC.py)
# ════════════════════════════════════════════════════════════════════════════

def remove_repeated_headers(text: str) -> str:
    """
    Remove lines that appear on ≥ 80 % of pages (running headers/footers)
    and stray OCR page-number lines.  Applied to full_text before building
    the page dict so every section's extracted text is already clean.
    """
    raw = text.replace("\r\n", "\n").replace("\r", "\n")
    marker_re = re.compile(
        r"^\s*(?:\[\s*page\s*\d+\s*\]|##\s*page\s*\d+)", re.IGNORECASE | re.MULTILINE
    )

    def split_pages(t):
        ms = list(marker_re.finditer(t))
        if not ms:
            return []
        return [
            {
                "marker": m.group(0).strip(),
                "body":   t[m.end(): ms[i+1].start() if i+1 < len(ms) else len(t)].lstrip("\n"),
            }
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
        r"^\s*(?:\[?\s*page\s*\d{1,4}\s*\]?|[-–—]?\s*\d{1,4}\s*[-–—]?)\s*$",
        re.IGNORECASE,
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
    repeated  = {n for n, c in line_counts.items() if c >= threshold}

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

    if pre:
        pre_lines = []
        for ln in pre.splitlines():
            raw_ln = ln.strip()
            if raw_ln and (
                old_pagenum_re.match(raw_ln)
                or (len(raw_ln) <= 150 and norm(raw_ln) in repeated)
            ):
                continue
            pre_lines.append(ln)
        pre = re.sub(r"\n{3,}", "\n\n", "\n".join(pre_lines)).rstrip("\n")

    rebuilt = "\n\n".join(f"{p['marker']}\n{p['body']}".rstrip() for p in final).strip()
    return (f"{pre}\n\n{rebuilt}".strip() if pre else rebuilt)


# ════════════════════════════════════════════════════════════════════════════
# Page-dict builder
# ════════════════════════════════════════════════════════════════════════════

def build_page_dict(text: str) -> dict:
    """Return {physical_page_number: page_text} from the text-extraction MD."""
    matches = list(_MD_PAGE_RE.finditer(text))
    page_dict = {}
    for i, m in enumerate(matches):
        num   = _page_num(m)
        start = m.end()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        page_dict[num] = text[start:end].strip()
    return page_dict


# ════════════════════════════════════════════════════════════════════════════
# Section text extraction
# ════════════════════════════════════════════════════════════════════════════

def _fuzzy(a: str, b: str, threshold=0.72) -> bool:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= threshold


def get_section_text(
    title: str, start_page: int, end_page: int,
    page_dict: dict, next_title: str | None = None
) -> str:
    block = "\n".join(page_dict.get(p, "") for p in range(start_page, end_page + 1))
    if not block.strip():
        return ""
    lines = block.splitlines()

    # Find the heading line, then start AFTER it so the section title is not
    # repeated in the body text (it is already shown as the section label above).
    heading_idx = next(
        (i for i, ln in enumerate(lines) if _fuzzy(ln.strip(), title, 0.65)), None
    )
    start_idx = (heading_idx + 1) if heading_idx is not None else 0

    end_idx = len(lines)
    if next_title:
        for i in range(start_idx, len(lines)):
            if _fuzzy(lines[i].strip(), next_title, 0.65):
                end_idx = i
                break
    return "\n".join(lines[start_idx:end_idx]).strip()


# ════════════════════════════════════════════════════════════════════════════
# DataFrame builder
# ════════════════════════════════════════════════════════════════════════════

# Matches native TOC bookmark titles that ARE the table-of-contents page itself
_TOC_ENTRY_TITLE_RE = re.compile(
    r"(?i)^\s*(table\s+of\s+contents?|contents?|toc)\s*$"
)

# Matches exhibit / appendix section entries and individual exhibit bookmarks
# e.g. "Exhibits Combined", "Exhibit A", "Ex 1", "Ex 10", "Appendix A"
_EXHIBIT_ENTRY_RE = re.compile(
    r"(?i)^\s*(exhibits?\s*(combined|section|list|index)?|ex\.?\s*\d+|appendix\s+[A-Z0-9])\s*"
)


def _filter_exhibit_entries(entries: list) -> list:
    """
    Remove exhibit section entries and ALL their children from the TOC list.
    Exhibits are extracted and saved separately, so they should not appear in
    the navigational TOC.

    Algorithm: walk entries in order; once an entry whose title matches an
    exhibit pattern is encountered, record its level as skip_at_level and drop
    all subsequent entries at that level or deeper.  Resume including entries
    when a non-exhibit entry at a shallower level is found.
    """
    result = []
    skip_at_level = None

    for e in entries:
        lvl = e["level"]

        if skip_at_level is not None:
            if lvl <= skip_at_level:
                # Returned to the same or higher level — check if still exhibit
                if _EXHIBIT_ENTRY_RE.match(e["title"]):
                    skip_at_level = lvl  # another sibling exhibit — keep skipping
                else:
                    skip_at_level = None  # genuine non-exhibit — resume
            else:
                continue  # still inside the exhibit subtree

        if skip_at_level is None:
            if _EXHIBIT_ENTRY_RE.match(e["title"]):
                skip_at_level = lvl  # start skipping from here
            else:
                result.append(e)

    return result


def build_toc_df(
    toc_entries: list, page_dict: dict, total_pages: int, full_text: str
) -> pd.DataFrame:
    """
    toc_entries: [{"level": int, "title": str, "page": int}, ...]
    page_dict  : {physical_page: text}
    """
    # Drop any entry whose title IS the TOC page itself (e.g. "Table of Contents",
    # "Contents").  Keeping it would create a body section that just contains the
    # TOC text, duplicating the synthetic "TOC" row we add below.
    toc_entries = [e for e in toc_entries if not _TOC_ENTRY_TITLE_RE.match(e["title"])]

    # Drop exhibit section entries and all their children.  Exhibits are split
    # out and saved separately, so they must not pollute the navigational TOC.
    before = len(toc_entries)
    toc_entries = _filter_exhibit_entries(toc_entries)
    if len(toc_entries) < before:
        print(f"  [TOC filter] Removed {before - len(toc_entries)} exhibit bookmark(s)")

    rows = []

    # Title Page — everything before the first TOC entry's page
    first_body_page = toc_entries[0]["page"] if toc_entries else 1
    title_text = "\n".join(
        page_dict.get(p, "") for p in range(1, first_body_page)
    ).strip()
    rows.append({
        "level": 0, "section": "Title Page",
        "start_page": pd.NA, "end_page": pd.NA, "page_range": "",
        "section_text": title_text,
    })

    # TOC row — list all entries as plain text
    toc_plain = "\n".join(
        ("  " * (e["level"] - 1)) + e["title"] + f"  ....  {e['page']}"
        for e in toc_entries
    )
    rows.append({
        "level": 0, "section": "TOC",
        "start_page": pd.NA, "end_page": pd.NA, "page_range": "",
        "section_text": toc_plain,
    })

    # Body sections
    for i, entry in enumerate(toc_entries):
        start_pg = entry["page"]
        # End page = one before the next section at the same or higher level
        end_pg = total_pages
        for j in range(i + 1, len(toc_entries)):
            if toc_entries[j]["level"] <= entry["level"]:
                end_pg = toc_entries[j]["page"] - 1
                break
        end_pg = max(start_pg, end_pg)

        next_title = toc_entries[i + 1]["title"] if i + 1 < len(toc_entries) else None
        text = get_section_text(
            entry["title"], start_pg, end_pg, page_dict, next_title
        )

        indent = "  " * (entry["level"] - 1)
        rows.append({
            "level":        entry["level"] - 1,   # normalise: fitz levels start at 1
            "section":      indent + entry["title"],
            "start_page":   start_pg,
            "end_page":     end_pg,
            "page_range":   f"{start_pg}-{end_pg}",
            "section_text": text,
        })

    return pd.DataFrame(rows)


# ════════════════════════════════════════════════════════════════════════════
# Exhibit detection and splitting
# ════════════════════════════════════════════════════════════════════════════

# "Exhibit A" / "Attachment B" at the START of a line — avoids inline mentions
_EXHIBIT_START_RE = re.compile(
    r"(?im)^(?:exhibit|attachment|schedule|appendix)\s*[A-Z0-9]"
)
# Broader match for scanning section names / TOC text
_EXHIBIT_INLINE_RE = re.compile(
    r"(?i)\b(?:exhibit|ex\s*[-]?\s*\d+|ex\d+)\b"
)


def detect_and_split_exhibits(
    df: pd.DataFrame, page_dict: dict, total_pages: int
) -> tuple:
    """
    Scan section names and page content for exhibit markers.
    Returns (updated_df, exhibit_text, exhibit_start_page).
    exhibit_text is "" and exhibit_start_page is None when nothing is found.
    """
    # Check section names and the TOC row for "exhibit" mentions
    section_names = " ".join(str(r.get("section", "")) for _, r in df.iterrows())
    toc_row       = df[df["section"] == "TOC"]
    toc_preview   = str(toc_row["section_text"].values[0]) if len(toc_row) else ""

    exhibit_in_toc = bool(
        _EXHIBIT_INLINE_RE.search(section_names)
        or _EXHIBIT_INLINE_RE.search(toc_preview)
    )

    # Find first page where actual exhibit content starts.
    # Skip pages that have 3+ exhibit-start matches — those are a "Table of
    # Exhibits" or index listing (e.g. "Exhibit A — Smith Decl.  /  Exhibit B
    # — Email thread  /  ...") which should stay in the body.
    scan_from = 1 if exhibit_in_toc else max(1, int(total_pages * 0.5))
    exhibit_start_page = None
    for p in range(scan_from, total_pages + 1):
        page_text = page_dict.get(p, "")
        matches   = list(_EXHIBIT_START_RE.finditer(page_text))
        if not matches:
            continue
        if len(matches) >= 3:
            # Multiple exhibit references on one page → it's a list/index, not content
            continue
        exhibit_start_page = p
        break

    if not exhibit_start_page:
        return df, "", None

    exhibit_text = "\n\n".join(
        page_dict.get(p, "") for p in range(exhibit_start_page, total_pages + 1)
    ).strip()

    if not exhibit_text:
        return df, "", None

    # Trim the last real section so it ends before the exhibit page
    updated_df = df.copy()
    non_na = updated_df[updated_df["start_page"].notna()]
    if len(non_na) > 0:
        last_idx   = non_na.index[-1]
        new_end    = min(int(updated_df.loc[last_idx, "end_page"]), exhibit_start_page - 1)
        start_last = int(updated_df.loc[last_idx, "start_page"])
        if new_end >= start_last:
            updated_df.loc[last_idx, "end_page"]    = new_end
            updated_df.loc[last_idx, "page_range"]  = f"{start_last}-{new_end}"
            updated_df.loc[last_idx, "section_text"] = get_section_text(
                str(updated_df.loc[last_idx, "section"]).strip(),
                start_last, new_end, page_dict
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
        level       = int(row.get("level", 0) or 0)
        section     = str(row.get("section", "")).strip()
        page_range  = str(row.get("page_range", "")).strip()
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
        print("Usage: python 07_Native_TOC.py <text_extraction_md>")
        sys.exit(1)

    text_md = sys.argv[1]
    if not os.path.isfile(text_md):
        print(f"ERROR: File not found: {text_md}")
        sys.exit(1)

    stem      = os.path.splitext(os.path.basename(text_md))[0]   # e.g. report_text_extraction
    doc_stem  = stem.replace("_text_extraction", "")               # e.g. report
    temp_dir  = os.path.dirname(os.path.abspath(text_md))

    toc_json  = os.path.join(temp_dir, doc_stem + "_native_toc.json")
    if not os.path.isfile(toc_json):
        print(f"ERROR: Native TOC JSON not found: {toc_json}")
        print("       Re-run 04_text_extraction.py first.")
        sys.exit(1)

    print("=" * 60)
    print("07_NATIVE_TOC — Smart PDF (embedded bookmarks)")
    print("=" * 60)
    print(f"  Text MD  : {text_md}")
    print(f"  TOC JSON : {toc_json}")

    # ── Load inputs ──────────────────────────────────────────────────────────
    with open(text_md,  "r", encoding="utf-8") as f:
        full_text = f.read()
    with open(toc_json, "r", encoding="utf-8") as f:
        toc_data = json.load(f)

    toc_entries = toc_data.get("entries", [])
    if not toc_entries:
        print("ERROR: Native TOC JSON has no entries.")
        sys.exit(1)

    print("\nRemoving repeated headers and stray page numbers...")
    full_text   = remove_repeated_headers(full_text)

    page_dict   = build_page_dict(full_text)
    total_pages = max(page_dict.keys()) if page_dict else 1

    print(f"\n  TOC entries  : {len(toc_entries)}")
    print(f"  Total pages  : {total_pages}")

    # TOC preview
    print("\n    ── TOC preview ─────────────────────────────────")
    for e in toc_entries[:30]:
        indent = "  " * (e["level"] - 1)
        print(f"    {indent}{e['title']}  (p.{e['page']})")
    if len(toc_entries) > 30:
        print(f"    ... and {len(toc_entries) - 30} more entries")
    print("    ────────────────────────────────────────────────")

    # ── Save exhibit bookmarks before they're filtered out ───────────────────
    # build_toc_df calls _filter_exhibit_entries which discards exhibit TOC entries.
    # We save them here so 07b_exhibit_split can use exact page boundaries.
    exhibit_bookmarks = [
        {"title": e["title"], "page": e["page"], "level": e["level"]}
        for e in toc_entries
        if _EXHIBIT_ENTRY_RE.match(e["title"])
    ]
    if exhibit_bookmarks:
        bm_path = os.path.join(temp_dir, doc_stem + "_exhibit_bookmarks.json")
        with open(bm_path, "w", encoding="utf-8") as f:
            json.dump(exhibit_bookmarks, f, indent=2)
        print(f"\n  Saved {len(exhibit_bookmarks)} exhibit bookmark(s) → {bm_path}")

    # ── Build section table ───────────────────────────────────────────────────
    print("\nBuilding section table...")
    df = build_toc_df(toc_entries, page_dict, total_pages, full_text)

    # Drop the Title Page row if it has no content (TOC starts on page 1)
    title_mask = (df["section"] == "Title Page") & (df["section_text"].fillna("").str.strip() == "")
    if title_mask.any():
        df = df[~title_mask].reset_index(drop=True)
        print("  Title Page row removed (no content before first section)")

    # ── Exhibit detection ─────────────────────────────────────────────────────
    print("\nChecking for exhibits...")
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
    print(f"\n  Section table CSV : {csv_out}")

    final_md = build_final_document(df)
    md_out   = os.path.join(temp_dir, doc_stem + "_07_final_document.md")
    with open(md_out, "w", encoding="utf-8") as f:
        f.write(final_md)
    print(f"  Final document MD : {md_out}")

    print("\n" + "=" * 60)
    print("07_NATIVE_TOC — COMPLETE")
    print(f"  Sections processed : {len(df)}")
    print(f"  Exhibit detected   : {'YES — page ' + str(exhibit_start_page) if exhibit_text else 'NO'}")
    print(f"  Output CSV         : {csv_out}")
    print(f"  Output MD          : {md_out}")
    print("=" * 60)


if __name__ == "__main__":
    main()
