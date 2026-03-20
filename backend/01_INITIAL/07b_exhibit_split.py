"""
07b_exhibit_split.py — Phase 1, Step 7b: Exhibit Separation

Runs after 07_*_TOC.py scripts and before 08_Send_Supabase.py.

Problem: Phase 1's 07_* scripts detect exhibits within documents and split
them into a {stem}_exhibits.md file. But they don't create separate document
entries. For a 309-page appeal where pages 32-309 are exhibits, those exhibits
are either lost or crammed into the parent document's sections.

Solution: This script reads the exhibits.md file (or detects exhibit boundaries
in the text extraction), creates a new document row for each exhibit, and creates
section rows for each exhibit's content. The parent document gets a reference
to each child exhibit via a parent_document_id relationship.

The child documents then flow through the rest of the pipeline independently:
- 08_Send_Supabase.py uploads them
- Phase 2 labels and extracts from them as separate documents
- The KG links them back to the parent via exhibit_reference edges

Requires: A new column on the documents table:
    ALTER TABLE documents ADD COLUMN parent_document_id UUID REFERENCES documents(id);
    CREATE INDEX idx_documents_parent ON documents(parent_document_id);

Usage:
    python 07b_exhibit_split.py <path_to_text_extraction.md>
"""

import os
import re
import sys
import json
import uuid

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TEMP_DIR = os.path.join(os.path.dirname(__file__), '..', 'zz_temp_chunks')

# Patterns that mark exhibit boundaries in legal documents
EXHIBIT_PATTERNS = [
    # "EXHIBIT A", "Exhibit 1", "Exhibit 13", "EXHIBIT A-1", etc.
    r'^(?:EXHIBIT|Exhibit|exhibit)\s+([A-Z][A-Z0-9]*|[0-9]+(?:[-–][A-Z0-9]+)?)\b',
    # "Appendix A", "APPENDIX 1", "Appendix 13"
    r'^(?:APPENDIX|Appendix|appendix)\s+([A-Z][A-Z0-9]*|[0-9]+)\b',
    # "Attachment A", "ATTACHMENT 1", "Attachment 13"
    r'^(?:ATTACHMENT|Attachment|attachment)\s+([A-Z][A-Z0-9]*|[0-9]+)\b',
    # "Schedule A", "SCHEDULE 1", "Schedule 13"
    r'^(?:SCHEDULE|Schedule|schedule)\s+([A-Z][A-Z0-9]*|[0-9]+)\b',
]

# Page marker regex (from Phase 1 convention) — used line-by-line in Strategy 2
PAGE_MARKER_RE = re.compile(r'^(?:##\s*Page\s+(\d+)|\[Page\s+(\d+)\])$', re.MULTILINE)

# More permissive page marker for full-text finditer (handles \r, extra spaces, etc.)
_PAGE_POS_RE = re.compile(
    r'(?im)^(?:##\s*[Pp]age\s+(\d+)(?:\s*\([^)]*\))?|\[\s*[Pp]age\s+(\d+)\s*\])\s*$'
)


# ---------------------------------------------------------------------------
# Strategy 0: Native TOC exhibit bookmarks (exact page boundaries)
# ---------------------------------------------------------------------------

def _read_exhibit_bookmarks(doc_stem: str) -> list[dict] | None:
    """
    Read the exhibit bookmarks saved by 07_Native_TOC.py.
    Returns the raw list of {title, page, level} dicts, or None if not present.
    """
    bookmarks_path = os.path.join(TEMP_DIR, f"{doc_stem}_exhibit_bookmarks.json")
    if not os.path.exists(bookmarks_path):
        return None
    with open(bookmarks_path, 'r', encoding='utf-8') as f:
        bookmarks = json.load(f)
    return bookmarks if bookmarks else None


def _split_by_bookmarks(text_path: str, bookmarks: list[dict]) -> list[dict]:
    """
    Slice the text extraction markdown into per-exhibit chunks using the
    exact page numbers from native TOC bookmarks.

    Steps:
      1. Build a map of page_number → char position from ## Page N markers.
      2. Deduplicate bookmarks with the same page (keep first occurrence).
      3. For each bookmark, slice from its page's char position to the next
         bookmark's page start.
      4. Return exhibit dicts compatible with _write_exhibit_files.
    """
    with open(text_path, 'r', encoding='utf-8') as f:
        full_text = f.read()

    # Map: page number → char position of the "## Page N" marker in the text
    page_positions: dict[int, int] = {}
    for m in _PAGE_POS_RE.finditer(full_text):
        pg = int(m.group(1) or m.group(2))
        if pg not in page_positions:          # keep first occurrence per page
            page_positions[pg] = m.start()

    if not page_positions:
        return []

    max_page = max(page_positions.keys())

    # Deduplicate bookmarks by page — keep first occurrence per unique page
    seen_pages: set = set()
    deduped = []
    for bm in bookmarks:
        pg = bm.get('page')
        if pg is not None and pg not in seen_pages and pg in page_positions:
            seen_pages.add(pg)
            deduped.append(bm)

    if not deduped:
        return []

    deduped.sort(key=lambda b: b['page'])

    exhibits = []
    for i, bm in enumerate(deduped):
        start_page = bm['page']
        end_page   = deduped[i + 1]['page'] - 1 if i + 1 < len(deduped) else max_page

        char_start = page_positions[start_page]
        char_end   = page_positions.get(deduped[i + 1]['page'], len(full_text)) \
                     if i + 1 < len(deduped) else len(full_text)

        text = full_text[char_start:char_end].strip()
        if len(text) < 100:
            continue

        # Derive a clean label from the bookmark title (e.g. "Ex 13" → "13")
        num_match = re.search(r'(\d+)$', bm['title'].strip())
        label = num_match.group(1) if num_match else re.sub(r'[^\w\-]', '_', bm['title'])

        exhibits.append({
            'label':      label,
            'title':      bm['title'],
            'text':       text,
            'start_page': start_page,
            'end_page':   end_page,
        })

    return exhibits


# ---------------------------------------------------------------------------
# Exhibit detection from text
# ---------------------------------------------------------------------------

def _detect_exhibits_from_text(text: str) -> list[dict]:
    """
    Scan the text extraction markdown for exhibit boundaries.
    Returns a list of dicts: {label, title, start_char, end_char, start_page, end_page, text}
    """
    lines = text.split('\n')
    exhibits: list[dict] = []
    current_exhibit = None
    current_page = None
    char_offset = 0

    for line in lines:
        # Track page numbers
        page_match = PAGE_MARKER_RE.match(line)
        if page_match:
            current_page = int(page_match.group(1) or page_match.group(2))
            char_offset += len(line) + 1
            continue

        # Check for exhibit boundary
        is_exhibit_start = False
        for pattern in EXHIBIT_PATTERNS:
            match = re.match(pattern, line.strip())
            if match:
                # Close previous exhibit
                if current_exhibit:
                    current_exhibit['end_char'] = char_offset
                    current_exhibit['end_page'] = current_page
                    current_exhibit['text'] = text[current_exhibit['start_char']:current_exhibit['end_char']].strip()
                    if len(current_exhibit['text']) > 100:  # skip tiny fragments
                        exhibits.append(current_exhibit)

                label = match.group(1)
                # Try to get a descriptive title from the next few lines
                title = _extract_exhibit_title(lines, lines.index(line) if line in lines else 0)

                current_exhibit = {
                    'label': label,
                    'title': title or f"Exhibit {label}",
                    'start_char': char_offset,
                    'end_char': None,
                    'start_page': current_page,
                    'end_page': None,
                    'text': None,
                }
                is_exhibit_start = True
                break

        char_offset += len(line) + 1

    # Close last exhibit
    if current_exhibit:
        current_exhibit['end_char'] = len(text)
        current_exhibit['end_page'] = current_page
        current_exhibit['text'] = text[current_exhibit['start_char']:current_exhibit['end_char']].strip()
        if len(current_exhibit['text']) > 100:
            exhibits.append(current_exhibit)

    return exhibits


def _extract_exhibit_title(lines: list[str], start_idx: int) -> str | None:
    """
    Look at the lines immediately following an exhibit marker to find
    a descriptive title. Exhibits often have a title line like:
    "EXHIBIT A"
    "Declaration of Eric A. Tate in Support of..."
    """
    title_parts = []
    for i in range(start_idx + 1, min(start_idx + 5, len(lines))):
        line = lines[i].strip()
        if not line:
            continue
        # Stop if we hit another exhibit marker, page marker, or body text
        if PAGE_MARKER_RE.match(line):
            break
        if any(re.match(p, line) for p in EXHIBIT_PATTERNS):
            break
        if len(line) > 200:  # probably body text, not a title
            break
        title_parts.append(line)
        if len(' '.join(title_parts)) > 150:
            break

    return ' '.join(title_parts).strip() if title_parts else None


# ---------------------------------------------------------------------------
# Read exhibits from Phase 1's exhibits.md file
# ---------------------------------------------------------------------------

def _read_exhibits_md(doc_stem: str) -> list[dict] | None:
    """
    Try to read the exhibits.md file that Phase 1's 07_* scripts created.
    Returns a list of exhibit dicts or None if the file doesn't exist.
    """
    exhibits_path = os.path.join(TEMP_DIR, f"{doc_stem}_exhibits.md")
    if not os.path.exists(exhibits_path):
        return None

    with open(exhibits_path, 'r', encoding='utf-8') as f:
        content = f.read()

    if not content.strip():
        return None

    # Parse the exhibits.md — it uses ## Exhibit A, ## Exhibit B markers
    exhibits = []
    sections = re.split(r'^##\s+', content, flags=re.MULTILINE)

    for section in sections:
        if not section.strip():
            continue

        lines = section.strip().split('\n')
        header = lines[0].strip()

        # Extract label from header
        label_match = re.match(r'(?:Exhibit|EXHIBIT|Appendix|Schedule|Attachment)\s+([A-Z0-9](?:[-–][A-Z0-9])?)', header)
        if label_match:
            label = label_match.group(1)
        else:
            label = header[:20]

        text = '\n'.join(lines[1:]).strip()
        if len(text) < 100:
            continue

        # Try to extract page numbers from the text
        pages = PAGE_MARKER_RE.findall(text)
        start_page = int(pages[0][0] or pages[0][1]) if pages else None
        end_page = int(pages[-1][0] or pages[-1][1]) if pages else None

        exhibits.append({
            'label': label,
            'title': header,
            'text': text,
            'start_page': start_page,
            'end_page': end_page,
        })

    return exhibits if exhibits else None


# ---------------------------------------------------------------------------
# Exhibit classification (lightweight — just guess the document type)
# ---------------------------------------------------------------------------

def _classify_exhibit(title: str, text_snippet: str) -> str:
    """
    Quick pattern-based classification of an exhibit's document type.
    This is a rough guess — Phase 2's 05_doc_classification would give
    a better answer, but we need something for the initial document row.
    """
    t = (title + ' ' + text_snippet[:500]).lower()

    if any(k in t for k in ['declaration of', 'i declare', 'under penalty of perjury']):
        return 'Declaration'
    if any(k in t for k in ['order', 'it is hereby ordered', 'it is so ordered']):
        return 'Order'
    if any(k in t for k in ['agreement', 'contract', 'license', 'term sheet']):
        return 'Contract'
    if any(k in t for k in ['subpoena']):
        return 'Subpoena'
    if any(k in t for k in ['deposition', 'transcript', 'q.']):
        return 'Deposition'
    if any(k in t for k in ['motion', 'memorandum', 'brief', 'reply']):
        return 'Motion'
    if any(k in t for k in ['complaint', 'petition']):
        return 'Complaint'
    if any(k in t for k in ['letter', 'correspondence', 'email', 'dear']):
        return 'Correspondence'
    if any(k in t for k in ['engagement letter']):
        return 'Contract'
    if any(k in t for k in ['privilege log']):
        return 'Discovery'
    return 'Exhibit'  # generic fallback


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _write_exhibit_files(doc_stem: str, exhibits: list[dict]):
    """
    Write each exhibit's text to a separate file in zz_temp_chunks/ and
    write a manifest JSON that 08_Send_Supabase.py will read.
    """
    manifest = []

    for i, ex in enumerate(exhibits):
        label = ex['label']
        safe_label = re.sub(r'[^\w\-]', '_', label)
        exhibit_stem = f"{doc_stem}_exhibit_{safe_label}"

        # Write the exhibit text as its own markdown file
        text_path = os.path.join(TEMP_DIR, f"{exhibit_stem}_text_extraction.md")
        with open(text_path, 'w', encoding='utf-8') as f:
            f.write(ex['text'])

        # Write a simple sections CSV (one section per exhibit for now —
        # Phase 2's 00_section_refine will split it further if needed)
        sections_path = os.path.join(TEMP_DIR, f"{exhibit_stem}_07_toc_sections.csv")
        with open(sections_path, 'w', encoding='utf-8') as f:
            f.write("level,section,start_page,end_page,page_range,section_text,is_synthetic\n")
            page_range = ""
            if ex.get('start_page') and ex.get('end_page'):
                page_range = f"{ex['start_page']}-{ex['end_page']}"
            # Escape CSV fields
            escaped_title = ex['title'].replace('"', '""')
            escaped_text = ex['text'].replace('"', '""')
            f.write(f'0,"{escaped_title}",{ex.get("start_page", "")},{ex.get("end_page", "")},"{page_range}","{escaped_text}",False\n')

        # Write classification
        doc_type = _classify_exhibit(ex['title'], ex['text'])
        classification_path = os.path.join(TEMP_DIR, f"{exhibit_stem}_text_extraction_classification.json")
        with open(classification_path, 'w', encoding='utf-8') as f:
            json.dump({
                'document_type': doc_type,
                'confidence_score': 0.6,  # low confidence — pattern-based guess
                'is_exhibit': True,
                'exhibit_label': label,
                'parent_document': doc_stem,
            }, f, indent=2)

        manifest.append({
            'exhibit_label': label,
            'exhibit_title': ex['title'],
            'exhibit_stem': exhibit_stem,
            'document_type': doc_type,
            'start_page': ex.get('start_page'),
            'end_page': ex.get('end_page'),
            'text_length': len(ex['text']),
        })

    # Write manifest
    manifest_path = os.path.join(TEMP_DIR, f"{doc_stem}_exhibit_manifest.json")
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)

    return manifest_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python 07b_exhibit_split.py <path_to_text_extraction.md>")
        sys.exit(1)

    text_path = sys.argv[1]
    if not os.path.exists(text_path):
        print(f"ERROR: File not found: {text_path}")
        sys.exit(1)

    # Derive doc_stem from filename
    basename = os.path.basename(text_path)
    doc_stem = basename.replace('_text_extraction.md', '')

    print(f"  Looking for exhibits in '{doc_stem}'...")

    exhibits = []

    # Strategy 0: Native TOC bookmarks — exact page boundaries, most reliable
    bookmarks = _read_exhibit_bookmarks(doc_stem)
    if bookmarks:
        print(f"  [Strategy 0] Using {len(bookmarks)} native TOC bookmark(s)...")
        exhibits = _split_by_bookmarks(text_path, bookmarks)
        if exhibits:
            print(f"  [Strategy 0] Split into {len(exhibits)} exhibit(s) by page boundary.")

    # Strategy 1: Phase 1's exhibits.md (pre-split by 07_* TOC scripts)
    if not exhibits:
        exhibits = _read_exhibits_md(doc_stem) or []
        if exhibits:
            print(f"  [Strategy 1] Found {len(exhibits)} exhibit(s) in exhibits.md.")

    # Strategy 2: Scan text for exhibit boundary markers (fallback)
    # Also re-runs if strategy 1 returned one giant blob (>50k chars = unsplit)
    if not exhibits or (len(exhibits) == 1 and len(exhibits[0].get('text', '')) > 50000):
        print("  [Strategy 2] Scanning text extraction for exhibit markers...")
        with open(text_path, 'r', encoding='utf-8') as f:
            full_text = f.read()
        exhibits = _detect_exhibits_from_text(full_text)

    if not exhibits:
        print(f"SUCCESS: No exhibits found in '{doc_stem}'. Nothing to split.")
        return

    print(f"  Found {len(exhibits)} exhibit(s):")
    for ex in exhibits:
        doc_type = _classify_exhibit(ex['title'], ex.get('text', '')[:500])
        pages = f"pages {ex.get('start_page', '?')}-{ex.get('end_page', '?')}"
        print(f"    Exhibit {ex['label']}: {ex['title'][:60]}... ({doc_type}, {pages}, {len(ex.get('text', ''))} chars)")

    # Write exhibit files
    manifest_path = _write_exhibit_files(doc_stem, exhibits)

    print(
        f"SUCCESS: Split {len(exhibits)} exhibit(s) from '{doc_stem}'. "
        f"Manifest: {manifest_path}"
    )


if __name__ == "__main__":
    main()