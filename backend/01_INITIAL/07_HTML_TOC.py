"""
07_HTML_TOC.py — HTML / HTM / XHTML document processor.

Handles native TOC anchor links, semi-structured TOC containers,
heading-based synthetic TOCs, visible-text TOC scanning, and an
AI fallback via GPT-4o-mini for irregular documents (iXBRL, etc.).

Works with the tagged XHTML file produced by 04_text_extraction.py
(_inject_html_tracking_ids) so existing IDs are always preserved.

Outputs (to zz_temp_chunks/):
  - {stem}_07_toc_sections.csv      — section table with text per row
  - {stem}_07_final_document.md     — full pretty-printed document
  - {stem}_07_toc_preview.md        — human-readable TOC preview
  - zz_temp_chunks/ui_assets/{stem}_tagged.xhtml  (already written by step 4)
"""

import sys
import os
import re
import warnings
from difflib import SequenceMatcher
import pandas as pd
from bs4 import BeautifulSoup, Tag, NavigableString
from dotenv import load_dotenv

_THIS_DIR   = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_THIS_DIR)
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
TEXT_TAGS    = {"p", "h1", "h2", "h3", "h4", "h5", "h6",
                "li", "td", "th", "dt", "dd", "span", "figcaption"}
SKIP_TAGS    = {"script", "style", "noscript", "head", "meta", "link", "svg"}

# Matches XBRL identifiers and date strings to filter from visible text
_XBRL_LINE_RE = re.compile(
    r"[a-z][a-zA-Z]+:[A-Z]"          # namespace prefix  san:DividendsMember
    r"|^[A-Z0-9]{10,}$"               # LEI codes         5493006QMFDDMYWIAM13
    r"|^\d{4}-\d{2}-\d{2}$"           # ISO dates         2022-01-01
)


# ════════════════════════════════════════════════════════════════════════════
# STEP 1 — Load HTML and ensure tracking IDs exist
# ════════════════════════════════════════════════════════════════════════════

def _choose_parser(path: str) -> str:
    """Return the appropriate BeautifulSoup parser for the file extension."""
    ext = os.path.splitext(path)[1].lower()
    return "lxml-xml" if ext in (".xhtml", ".xml") else "lxml"


def load_and_tag(html_path: str, ui_assets_dir: str) -> tuple:
    """
    Load the HTML file using the correct parser.
    If a _tagged.xhtml already exists (written by 04_text_extraction.py) use
    that — the IDs are already injected.  Otherwise inject them now and save.
    Returns (soup, tagged_path).
    """
    stem        = os.path.splitext(os.path.basename(html_path))[0]
    tagged_path = os.path.join(ui_assets_dir, stem + "_tagged.xhtml")
    parser      = _choose_parser(html_path)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

        if os.path.isfile(tagged_path):
            print(f"    Using pre-tagged file : {tagged_path}")
            print(f"    Parser                : {parser}")
            with open(tagged_path, "r", encoding="utf-8", errors="ignore") as f:
                soup = BeautifulSoup(f.read(), parser)
            return soup, tagged_path

        # Tagged file not found — inject now
        with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
            soup = BeautifulSoup(f.read(), parser)

    target_tags = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "div"]
    counter = 1
    for tag in soup.find_all(target_tags):
        if not tag.get_text(strip=True):
            continue
        if not tag.has_attr("id"):
            tag["id"] = f"ai-chunk-{counter:05d}"
            counter += 1

    os.makedirs(ui_assets_dir, exist_ok=True)
    with open(tagged_path, "w", encoding="utf-8") as f:
        f.write(str(soup))
    print(f"    Tracking IDs injected : {counter - 1} elements → {tagged_path}")
    return soup, tagged_path


# ════════════════════════════════════════════════════════════════════════════
# STEP 2 — TOC extraction (five strategies, A → B → C → D → E)
# ════════════════════════════════════════════════════════════════════════════

def _is_internal(href: str) -> bool:
    return bool(href and href.strip().startswith("#"))

def _is_footnote(a_tag: Tag) -> bool:
    """Short numeric/bracketed text = footnote reference."""
    return bool(re.match(r"^\s*[\[\(]?\d{1,3}[\]\)]?\s*$", a_tag.get_text(strip=True)))

def _nesting_level(tag: Tag) -> int:
    """Count how deeply a tag is nested inside <ul>/<ol> lists."""
    depth, parent = 0, tag.parent
    while parent and parent.name:
        if parent.name in ("ul", "ol"):
            depth += 1
        parent = parent.parent
    return min(depth, 5)


def strategy_a_nav_container(soup: BeautifulSoup) -> list:
    """
    Look for a dedicated TOC container:
      <nav ...>, <div id="toc">, <div class="table-of-contents">, etc.
    Returns [{text, href, level}, ...] or [].
    """
    toc_kws = ["toc", "table-of-contents", "tableofcontents",
                "contents", "index", "navigation", "outline"]

    container = None

    for nav in soup.find_all("nav"):
        attrs = " ".join(str(v) for vals in nav.attrs.values()
                         for v in (vals if isinstance(vals, list) else [vals])).lower()
        if any(kw in attrs for kw in toc_kws):
            container = nav
            break

    if not container:
        for tag in soup.find_all(["div", "section", "aside"]):
            id_val  = (tag.get("id") or "").lower()
            cls_val = " ".join(tag.get("class") or []).lower()
            combined = id_val + " " + cls_val
            if any(kw in combined for kw in toc_kws):
                container = tag
                break

    if not container:
        return []

    entries = []
    for a in container.find_all("a", href=True):
        href = a["href"].strip()
        if not _is_internal(href) or _is_footnote(a):
            continue
        entries.append({
            "text":  a.get_text(separator=" ", strip=True),
            "href":  href.lstrip("#"),
            "level": _nesting_level(a),
        })
    return entries


def strategy_b_anchor_links(soup: BeautifulSoup) -> list:
    """
    Scan all internal <a href="#..."> links.
    Filter out footnotes and links whose target has no real content.
    """
    id_map = {tag["id"]: tag for tag in soup.find_all(id=True)}

    candidates = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not _is_internal(href) or _is_footnote(a):
            continue
        target_id = href.lstrip("#")
        target = id_map.get(target_id)
        if target is None:
            continue
        is_heading   = target.name in HEADING_TAGS
        has_content  = len(target.get_text(strip=True)) > 15
        if is_heading or has_content:
            candidates.append({
                "text":        a.get_text(separator=" ", strip=True),
                "href":        target_id,
                "level":       _nesting_level(a),
                "_is_heading": is_heading,
            })

    if not candidates:
        return []

    seen, deduped = set(), []
    for e in candidates:
        if e["href"] not in seen:
            seen.add(e["href"])
            deduped.append(e)

    heading_links = [e for e in deduped if e["_is_heading"]]
    result = heading_links if len(heading_links) >= 3 else deduped
    return [{k: v for k, v in e.items() if k != "_is_heading"} for e in result]


def strategy_c_headings(soup: BeautifulSoup) -> list:
    """
    Build a synthetic TOC from the heading hierarchy (h1–h4).
    Uses IDs already present on headings (either native or injected).
    """
    entries = []
    min_level = None
    for tag in soup.find_all(HEADING_TAGS):
        text = tag.get_text(separator=" ", strip=True)
        if not text or len(text) < 2:
            continue
        lvl = int(tag.name[1])
        if min_level is None:
            min_level = lvl
        entries.append({
            "text":  text,
            "href":  tag.get("id", ""),
            "level": lvl - min_level,
        })
    return entries


def strategy_d_text_scan(soup: BeautifulSoup) -> list:
    """
    Detect TOC from visible text using number-density analysis.

    Many structured / iXBRL documents place the section title and its page
    number on *separate* lines (no dot leaders).  Standard keyword search
    fails because the "Contents" label often appears *after* the actual TOC
    block.  Instead we:
      1. Scan 80-line windows and find the region with the highest ratio of
         standalone 1-3 digit numbers (page numbers).
      2. Walk that region pairing text lines with the number line that
         follows them.  Multi-line titles are joined when the continuation
         looks like a parenthetical or a very short phrase.

    anchor IDs are resolved later by _resolve_anchors().
    """
    text  = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.splitlines()]

    # Page numbers are 1-3 digits (avoids matching years like 2024)
    _NUM_RE = re.compile(r'^\d{1,3}$')

    def is_num(l):   return bool(_NUM_RE.match(l))
    def is_xbrl(l):  return bool(_XBRL_LINE_RE.search(l))
    def is_title(l): return len(l) >= 3 and not is_num(l) and not is_xbrl(l)
    def is_cont(l):
        """True if line looks like a continuation of the previous title."""
        return (l.startswith("(")
                or l.lower().startswith("and ")
                or len(l.split()) <= 2)

    # ── Step 1: find number-dense window ────────────────────────────────────
    WINDOW = 80
    best_start, best_density = 0, 0.0
    for i in range(0, max(1, len(lines) - WINDOW), 15):
        seg = [l for l in lines[i:i + WINDOW] if l]
        if not seg:
            continue
        d = sum(1 for l in seg if is_num(l)) / len(seg)
        if d > best_density:
            best_density, best_start = d, i

    if best_density < 0.10:
        print(f"    [D] No number-dense TOC region found (max={best_density:.1%})")
        return []

    print(f"    [D] TOC region starts at line {best_start} "
          f"(density={best_density:.1%})")

    # ── Step 2: extract title/page pairs ────────────────────────────────────
    entries, pending = [], None
    consecutive_text = 0   # non-number text lines in a row (exit signal)

    for line in lines[best_start:]:
        if not line:
            continue

        if is_num(line):
            if pending:
                entries.append({
                    "text":  pending,
                    "href":  "",
                    "level": 0,
                    "_page": int(line),
                })
                pending = None
            consecutive_text = 0

        elif is_title(line):
            if pending and is_cont(line):
                # continuation: append to running title
                pending = pending + " " + line
            else:
                pending = line
            consecutive_text += 1
            # If we've seen many text lines in a row with no numbers,
            # we've drifted past the TOC into body text
            if entries and consecutive_text > 15:
                break

        else:
            # XBRL garbage or unreadable line — reset
            pending = None

    return entries


def strategy_e_ai_fallback(soup: BeautifulSoup) -> list:
    """
    GPT-4o-mini fallback: extract visible text from the document body,
    send the first ~12 000 chars to the model and ask it to identify
    and return the TOC entries in a structured format.
    anchor IDs are resolved later by _resolve_anchors().
    """
    import openai
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("    [E] OPENAI_API_KEY not set — skipping AI fallback.")
        return []

    client = openai.OpenAI(api_key=api_key)

    # Get visible text (skip scripts/styles already handled by get_text)
    text_body = soup.get_text(separator="\n")
    # First 14 000 chars usually covers the front matter + TOC
    sample = text_body[:14000]

    prompt = (
        "The following is the beginning of a financial document (annual report). "
        "Find the Table of Contents or Index if present and return its entries.\n\n"
        "Return ONLY the entries, one per line, in this exact format:\n"
        "LEVEL|TITLE\n"
        "Where LEVEL is 0 for top-level sections and 1 for sub-sections.\n"
        "Do NOT include page numbers, dots, or any other text.\n"
        "If no TOC is present, return: NONE\n\n"
        f"DOCUMENT:\n{sample}"
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
    except Exception as exc:
        print(f"    [E] AI call failed: {exc}")
        return []

    if raw.strip().upper() == "NONE":
        print("    [E] Model found no TOC.")
        return []

    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|", 1)
        try:
            level = int(parts[0].strip())
        except ValueError:
            level = 0
        title = parts[1].strip() if len(parts) > 1 else ""
        if title:
            entries.append({"text": title, "href": "", "level": level})

    print(f"    [E] AI returned {len(entries)} TOC entries.")
    return entries


def _resolve_anchors(soup: BeautifulSoup, entries: list) -> list:
    """
    For entries that have no href, fuzzy-match their title against the
    text content of all elements that carry an ID.  Sets entry['href'] to
    the best matching element ID.
    """
    needs_resolution = [e for e in entries if not e.get("href")]
    if not needs_resolution:
        return entries

    # Build lookup: id → text (capped to avoid huge strings)
    id_els = [
        (tag["id"], tag.get_text(" ", strip=True)[:300])
        for tag in soup.find_all(id=True)
        if tag.get("id")
    ]

    for entry in needs_resolution:
        title = entry["text"].strip()
        if not title:
            continue
        best_id, best_ratio = "", 0.0
        for eid, etext in id_els:
            # Compare title against the start of the element text
            window = etext[:len(title) + 30]
            ratio  = SequenceMatcher(None, title.lower(), window.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_id    = eid
        if best_ratio >= 0.55:
            entry["href"] = best_id

    resolved = sum(1 for e in needs_resolution if e.get("href"))
    print(f"    Anchor resolution : {resolved}/{len(needs_resolution)} entries matched")
    return entries


def extract_toc(soup: BeautifulSoup) -> tuple:
    """
    Try strategies A → B → C → D → E in order.
    Returns (entries, strategy_label).
    entries: [{text, href, level}, ...]
    """
    strategies = [
        (strategy_a_nav_container, "A — native nav/div container"),
        (strategy_b_anchor_links,  "B — internal anchor links"),
        (strategy_c_headings,      "C — heading structure (synthetic)"),
        (strategy_d_text_scan,     "D — visible text TOC scan"),
        (strategy_e_ai_fallback,   "E — AI fallback (GPT-4o-mini)"),
    ]
    for fn, label in strategies:
        print(f"    Trying strategy {label[0]}...")
        entries = fn(soup)
        if len(entries) >= 2:
            # Resolve anchors for D/E results that have no hrefs
            entries = _resolve_anchors(soup, entries)
            # Drop private keys (_page, etc.)
            entries = [{k: v for k, v in e.items() if not k.startswith("_")} for e in entries]
            return entries, label

    return [], "none"


# ════════════════════════════════════════════════════════════════════════════
# STEP 3 — Section text extraction
# ════════════════════════════════════════════════════════════════════════════

def _flat_text_elements(soup: BeautifulSoup) -> list:
    """Return all leaf-ish text-bearing elements in document order."""
    results = []
    for el in soup.descendants:
        if isinstance(el, Tag) and el.name in TEXT_TAGS and el.name not in SKIP_TAGS:
            results.append(el)
    return results


def extract_section_texts(soup: BeautifulSoup, toc_entries: list) -> list:
    """
    For each TOC entry, find its target element by ID, then collect text
    from that point until the next entry's element starts.
    Entries without an href produce an empty section_text.
    """
    if not toc_entries:
        return []

    id_map  = {tag["id"]: tag for tag in soup.find_all(id=True)}
    all_els = _flat_text_elements(soup)
    el_idx  = {id(el): i for i, el in enumerate(all_els)}

    for e in toc_entries:
        target = id_map.get(e.get("href", ""))
        e["_idx"] = el_idx.get(id(target), -1) if target is not None else -1

    ordered = sorted(toc_entries, key=lambda e: e["_idx"])

    def collect(start: int, end: int) -> str:
        if start < 0:
            return ""
        stop = end if end >= 0 else len(all_els)
        seen, texts = set(), []
        for el in all_els[start:stop]:
            eid = id(el)
            if eid in seen:
                continue
            seen.add(eid)
            t = el.get_text(separator=" ", strip=True)
            if t:
                texts.append(t)
        return "\n".join(texts)

    results = []
    for i, entry in enumerate(ordered):
        end_idx = ordered[i + 1]["_idx"] if i + 1 < len(ordered) else -1
        text    = collect(entry["_idx"], end_idx)
        row     = {k: v for k, v in entry.items() if not k.startswith("_")}
        row["section_text"] = text
        results.append(row)

    return results


# ════════════════════════════════════════════════════════════════════════════
# STEP 4 — Build output DataFrame and files
# ════════════════════════════════════════════════════════════════════════════

def get_title_text(soup: BeautifulSoup, first_entry_id: str) -> str:
    """Collect text from <body> before the first TOC section element."""
    id_map   = {tag["id"]: tag for tag in soup.find_all(id=True)}
    first_el = id_map.get(first_entry_id)
    if not first_el or not soup.body:
        return ""

    parts = []
    for el in soup.body.descendants:
        if not isinstance(el, Tag):
            continue
        if el is first_el or first_el in el.parents:
            break
        if el.name in TEXT_TAGS:
            t = el.get_text(separator=" ", strip=True)
            if t:
                parts.append(t)
    return "\n".join(parts)


def build_dataframe(toc_with_text: list, title_text: str, toc_plain: str) -> pd.DataFrame:
    rows = [
        {"level": 0, "section": "Title Page", "anchor_id": "", "section_text": title_text},
        {"level": 0, "section": "TOC",        "anchor_id": "", "section_text": toc_plain},
    ]
    for e in toc_with_text:
        lvl = int(e.get("level", 0))
        rows.append({
            "level":        lvl,
            "section":      ("  " * lvl) + e.get("text", "").strip(),
            "anchor_id":    e.get("href", ""),
            "section_text": e.get("section_text", ""),
        })
    return pd.DataFrame(rows)


def build_final_document(df: pd.DataFrame) -> str:
    lines = []
    for _, row in df.iterrows():
        level   = int(row.get("level", 0) or 0)
        section = str(row.get("section", "")).strip()
        anchor  = str(row.get("anchor_id", "")).strip()
        text    = str(row.get("section_text", "")).strip()

        h_ind = "  " * level
        m_ind = h_ind + "  "
        p_ind = h_ind + "    "

        lines.append(f"{h_ind}{section}")
        if anchor:
            lines.append(f"{m_ind}(#{anchor})")
        if text:
            for para in re.split(r"\n\s*\n", text):
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
        print("Usage: python 07_HTML_TOC.py <text_extraction_md>")
        sys.exit(1)

    text_md = sys.argv[1]
    if not os.path.isfile(text_md):
        print(f"ERROR: File not found: {text_md}")
        sys.exit(1)

    stem        = os.path.splitext(os.path.basename(text_md))[0]  # e.g. report_text_extraction
    doc_stem    = stem.replace("_text_extraction", "")              # e.g. report
    temp_dir    = os.path.dirname(os.path.abspath(text_md))         # zz_temp_chunks/
    backend_dir = os.path.dirname(temp_dir)
    docs_dir    = os.path.join(backend_dir, "data_storage", "documents")
    ui_dir      = os.path.join(temp_dir, "ui_assets")

    # Find the original HTML file
    html_path = None
    for ext in [".html", ".htm", ".xhtml", ".HTML", ".HTM", ".XHTML"]:
        candidate = os.path.join(docs_dir, doc_stem + ext)
        if os.path.isfile(candidate):
            html_path = candidate
            break

    if not html_path:
        print(f"ERROR: No HTML/HTM/XHTML file found for '{doc_stem}' in {docs_dir}")
        sys.exit(1)

    print("=" * 60)
    print("07_HTML_TOC — HTML/HTM/XHTML Document Processor")
    print("=" * 60)
    print(f"  Source : {html_path}")

    # ── [1/4] Load + ensure tracking IDs ────────────────────────────────────
    print("\n[1/4] Loading document and verifying tracking IDs...")
    soup, tagged_path = load_and_tag(html_path, ui_dir)

    # ── [2/4] Extract TOC ────────────────────────────────────────────────────
    print("\n[2/4] Extracting Table of Contents...")
    toc_entries, strategy = extract_toc(soup)
    print(f"    Strategy used : {strategy}")
    print(f"    TOC entries   : {len(toc_entries)}")

    if not toc_entries:
        print("    ERROR: Could not extract any TOC entries by any strategy.")
        sys.exit(1)

    # Print TOC to terminal
    print("\n    ── TOC preview ─────────────────────────────────")
    for e in toc_entries:
        indent = "  " * e.get("level", 0)
        anchor = f"  →  #{e['href']}" if e.get("href") else "  (no anchor)"
        print(f"    {indent}{e['text']}{anchor}")
    print("    ────────────────────────────────────────────────")

    # Save TOC preview file
    toc_preview_lines = [f"# TOC Preview — {doc_stem}", f"Strategy: {strategy}", ""]
    for e in toc_entries:
        indent = "  " * e.get("level", 0)
        anchor = f" (#{e['href']})" if e.get("href") else ""
        toc_preview_lines.append(f"{indent}- {e['text']}{anchor}")
    toc_preview_path = os.path.join(temp_dir, doc_stem + "_07_toc_preview.md")
    with open(toc_preview_path, "w", encoding="utf-8") as f:
        f.write("\n".join(toc_preview_lines))
    print(f"\n    TOC preview saved : {toc_preview_path}")

    # ── [3/4] Extract section texts ──────────────────────────────────────────
    print("\n[3/4] Extracting section texts...")
    toc_with_text = extract_section_texts(soup, toc_entries)

    first_id   = toc_entries[0].get("href", "") if toc_entries else ""
    title_text = get_title_text(soup, first_id)
    toc_plain  = "\n".join(
        ("  " * e.get("level", 0)) + e.get("text", "") for e in toc_entries
    )

    # ── [4/4] Save outputs ───────────────────────────────────────────────────
    print("\n[4/4] Saving outputs...")
    df = build_dataframe(toc_with_text, title_text, toc_plain)

    csv_out = os.path.join(temp_dir, doc_stem + "_07_toc_sections.csv")
    df.to_csv(csv_out, index=False, encoding="utf-8")
    print(f"    Section table CSV  : {csv_out}")

    final_md = build_final_document(df)
    md_out   = os.path.join(temp_dir, doc_stem + "_07_final_document.md")
    with open(md_out, "w", encoding="utf-8") as f:
        f.write(final_md)
    print(f"    Final document MD  : {md_out}")

    print("\n" + "=" * 60)
    print("07_HTML_TOC — COMPLETE")
    print(f"  TOC strategy       : {strategy}")
    print(f"  Sections processed : {len(df)}")
    print(f"  Tagged HTML saved  : {tagged_path}")
    print(f"  Output CSV         : {csv_out}")
    print(f"  Output MD          : {md_out}")
    print("=" * 60)


if __name__ == "__main__":
    main()
