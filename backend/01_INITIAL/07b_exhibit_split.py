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

# Patterns for ALL strategies (Strategies 0, 1, 1.5 use their own logic;
# these are only used by the Strategy 2 fallback — reduced to top-level markers only)
EXHIBIT_PATTERNS_S2 = [
    # "EXHIBIT A", "Exhibit 1", "Exhibit 13", "EXHIBIT A-1", etc.
    r'^(?:EXHIBIT|Exhibit|exhibit)\s+([A-Z][A-Z0-9]*|[0-9]+(?:[-–][A-Z0-9]+)?)\b',
    # "Appendix A", "APPENDIX 1", "Appendix 13"
    r'^(?:APPENDIX|Appendix|appendix)\s+([A-Z][A-Z0-9]*|[0-9]+)\b',
    # NOTE: Schedule and Attachment are intentionally excluded — they appear
    # frequently as internal sub-structure within exhibits and cause over-splitting.
]

# Full pattern list kept for _extract_exhibit_title guard (don't consume sub-headers)
_ALL_EXHIBIT_PATTERNS = EXHIBIT_PATTERNS_S2 + [
    r'^(?:ATTACHMENT|Attachment|attachment)\s+([A-Z][A-Z0-9]*|[0-9]+)\b',
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
    """
    with open(text_path, 'r', encoding='utf-8') as f:
        full_text = f.read()

    page_positions: dict[int, int] = {}
    for m in _PAGE_POS_RE.finditer(full_text):
        pg = int(m.group(1) or m.group(2))
        if pg not in page_positions:
            page_positions[pg] = m.start()

    if not page_positions:
        return []

    max_page = max(page_positions.keys())

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
# Strategy 1: Read from _exhibits.md (pre-split by 07_* TOC scripts)
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

        label_match = re.match(r'(?:Exhibit|EXHIBIT|Appendix|Schedule|Attachment)\s+([A-Z0-9](?:[-–][A-Z0-9])?)', header)
        if label_match:
            label = label_match.group(1)
        else:
            label = header[:20]

        text = '\n'.join(lines[1:]).strip()
        if len(text) < 100:
            continue

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
# Strategy 1.5: Split using exhibit_references from classification JSON
# ---------------------------------------------------------------------------

def _load_exhibit_reference_labels(doc_stem: str) -> list[str] | None:
    """
    Read exhibit_references from the classification JSON produced by 05_doc_classification.py.
    Returns a list of uppercase labels (e.g. ["1", "2", "A", "B"]) or None if unavailable.
    """
    class_path = os.path.join(TEMP_DIR, f"{doc_stem}_text_extraction_classification.json")
    if not os.path.exists(class_path):
        return None
    try:
        with open(class_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return None

    refs = data.get('exhibit_references') or []
    if not refs:
        return None

    labels = []
    seen = set()
    for ref in refs:
        # Parse the trailing label from strings like "Exhibit A", "Exhibit 1"
        m = re.search(r'\b([A-Z0-9]+)\s*$', ref.strip(), re.IGNORECASE)
        if m:
            lbl = m.group(1).upper()
            if lbl not in seen:
                seen.add(lbl)
                labels.append(lbl)

    return labels if labels else None


def _split_by_exhibit_references(doc_stem: str, text_path: str) -> list[dict] | None:
    """
    Strategy 1.5: Use exhibit_references from classification JSON as ground truth.

    Only splits on the known top-level exhibit labels (e.g. A, B, 1, 2).
    This prevents internal sub-structure ("Schedule 1", "Attachment 3") from
    creating spurious splits.

    Scans _exhibits.md when available (already filtered to exhibit content),
    otherwise scans the full text but locates the exhibit start boundary first.
    """
    labels = _load_exhibit_reference_labels(doc_stem)
    if not labels:
        return None

    # Prefer scanning _exhibits.md — it contains ONLY exhibit content
    exhibits_md_path = os.path.join(TEMP_DIR, f"{doc_stem}_exhibits.md")
    if os.path.exists(exhibits_md_path):
        with open(exhibits_md_path, 'r', encoding='utf-8') as f:
            scan_text = f.read()
        scan_from = 0
    else:
        with open(text_path, 'r', encoding='utf-8') as f:
            scan_text = f.read()
        # Find exhibit boundary = earliest line-start occurrence of any known label
        scan_from = len(scan_text)
        for label in labels:
            pat = re.compile(
                r'(?im)^[ \t]*exhibit\s+' + re.escape(label) + r'\b'
            )
            m = pat.search(scan_text)
            if m and m.start() < scan_from:
                scan_from = m.start()
        if scan_from == len(scan_text):
            return None  # no known exhibit label found anywhere

    # Build per-label pattern: line-start match, case-insensitive
    label_patterns = {
        label: re.compile(r'(?im)^[ \t]*exhibit\s+' + re.escape(label) + r'\b')
        for label in labels
    }

    # Find first occurrence of each label after the exhibit boundary
    label_positions: dict[str, int] = {}
    for label, pattern in label_patterns.items():
        m = pattern.search(scan_text, scan_from)
        if m:
            label_positions[label] = m.start()

    if not label_positions:
        return None

    # Sort by position in text
    ordered = sorted(label_positions.items(), key=lambda x: x[1])

    exhibits = []
    for i, (label, start_pos) in enumerate(ordered):
        end_pos = ordered[i + 1][1] if i + 1 < len(ordered) else len(scan_text)
        chunk = scan_text[start_pos:end_pos].strip()

        if len(chunk) < 100:
            continue

        pages = PAGE_MARKER_RE.findall(chunk)
        start_page = int(pages[0][0] or pages[0][1]) if pages else None
        end_page   = int(pages[-1][0] or pages[-1][1]) if pages else None

        chunk_lines = chunk.split('\n')
        title = _extract_exhibit_title(chunk_lines, 0)

        exhibits.append({
            'label':      label,
            'title':      title or f"Exhibit {label}",
            'text':       chunk,
            'start_page': start_page,
            'end_page':   end_page,
        })

    return exhibits if exhibits else None


# ---------------------------------------------------------------------------
# Strategy 2 (fallback): Scan text for exhibit boundary markers
# ---------------------------------------------------------------------------

def _detect_exhibits_from_text(text: str) -> list[dict]:
    """
    Fallback strategy: scan the text extraction markdown for exhibit boundaries.

    Uses reduced pattern set (Exhibit/Appendix only — no Schedule/Attachment).
    Requires boundary-like positioning: match must be near a page marker OR
    preceded by 2+ blank lines.

    Returns a list of dicts: {label, title, start_char, end_char, start_page, end_page, text}
    """
    lines = text.split('\n')
    exhibits: list[dict] = []
    current_exhibit = None
    current_page = None
    char_offset = 0
    consecutive_blanks = 0
    lines_since_page_marker = 999  # large initial value

    for line in lines:
        # Track page numbers
        page_match = PAGE_MARKER_RE.match(line)
        if page_match:
            current_page = int(page_match.group(1) or page_match.group(2))
            lines_since_page_marker = 0
            char_offset += len(line) + 1
            consecutive_blanks = 0
            continue

        # Track blank lines
        if not line.strip():
            consecutive_blanks += 1
            char_offset += len(line) + 1
            continue

        lines_since_page_marker += 1

        # Check for exhibit boundary — only EXHIBIT_PATTERNS_S2 (no Schedule/Attachment)
        for pattern in EXHIBIT_PATTERNS_S2:
            match = re.match(pattern, line.strip())
            if match:
                # Require boundary-like positioning to filter inline references
                near_page_boundary = lines_since_page_marker <= 3
                preceded_by_blanks = consecutive_blanks >= 2
                if not (near_page_boundary or preceded_by_blanks):
                    break  # skip — likely an inline reference

                # Close previous exhibit
                if current_exhibit:
                    current_exhibit['end_char'] = char_offset
                    current_exhibit['end_page'] = current_page
                    current_exhibit['text'] = text[current_exhibit['start_char']:current_exhibit['end_char']].strip()
                    if len(current_exhibit['text']) > 100:
                        exhibits.append(current_exhibit)

                label = match.group(1)
                line_idx = lines.index(line) if line in lines else 0
                title = _extract_exhibit_title(lines, line_idx)

                current_exhibit = {
                    'label':      label,
                    'title':      title or f"Exhibit {label}",
                    'start_char': char_offset,
                    'end_char':   None,
                    'start_page': current_page,
                    'end_page':   None,
                    'text':       None,
                }
                break

        char_offset += len(line) + 1
        consecutive_blanks = 0

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
    a descriptive title.
    """
    title_parts = []
    for i in range(start_idx + 1, min(start_idx + 5, len(lines))):
        line = lines[i].strip()
        if not line:
            continue
        if PAGE_MARKER_RE.match(line):
            break
        if any(re.match(p, line) for p in _ALL_EXHIBIT_PATTERNS):
            break
        if len(line) > 200:
            break
        title_parts.append(line)
        if len(' '.join(title_parts)) > 150:
            break

    return ' '.join(title_parts).strip() if title_parts else None


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
    return 'Exhibit'


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _write_exhibit_files(doc_stem: str, exhibits: list[dict]):
    """
    Write each exhibit's text to a separate file in zz_temp_chunks/ and
    write a manifest JSON that 08_Send_Supabase.py will read.

    Deduplicates labels: if two exhibits share a label, later ones get a
    numeric suffix (_2, _3, …) so they don't overwrite each other.
    """
    manifest = []
    seen_labels: dict[str, int] = {}

    for ex in exhibits:
        label = ex['label']

        # Deduplicate: if label already used, add suffix
        if label in seen_labels:
            seen_labels[label] += 1
            unique_label = f"{label}_{seen_labels[label]}"
        else:
            seen_labels[label] = 1
            unique_label = label

        safe_label   = re.sub(r'[^\w\-]', '_', unique_label)
        exhibit_stem = f"{doc_stem}_exhibit_{safe_label}"

        # Write exhibit text as its own markdown file
        text_path = os.path.join(TEMP_DIR, f"{exhibit_stem}_text_extraction.md")
        with open(text_path, 'w', encoding='utf-8') as f:
            f.write(ex['text'])

        # Write a simple sections CSV
        sections_path = os.path.join(TEMP_DIR, f"{exhibit_stem}_07_toc_sections.csv")
        with open(sections_path, 'w', encoding='utf-8') as f:
            f.write("level,section,start_page,end_page,page_range,section_text,is_synthetic\n")
            page_range = ""
            if ex.get('start_page') and ex.get('end_page'):
                page_range = f"{ex['start_page']}-{ex['end_page']}"
            escaped_title = ex['title'].replace('"', '""')
            escaped_text  = ex['text'].replace('"', '""')
            f.write(f'0,"{escaped_title}",{ex.get("start_page", "")},{ex.get("end_page", "")},"{page_range}","{escaped_text}",False\n')

        # Write classification
        doc_type = _classify_exhibit(ex['title'], ex['text'])
        classification_path = os.path.join(TEMP_DIR, f"{exhibit_stem}_text_extraction_classification.json")
        with open(classification_path, 'w', encoding='utf-8') as f:
            json.dump({
                'document_type':    doc_type,
                'confidence_score': 0.6,
                'is_exhibit':       True,
                'exhibit_label':    label,
                'parent_document':  doc_stem,
            }, f, indent=2)

        manifest.append({
            'exhibit_label':  label,
            'exhibit_title':  ex['title'],
            'exhibit_stem':   exhibit_stem,
            'document_type':  doc_type,
            'start_page':     ex.get('start_page'),
            'end_page':       ex.get('end_page'),
            'text_length':    len(ex['text']),
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

    # Strategy 1.5: Use exhibit_references from classification JSON as ground truth.
    # Fires when Strategy 1 produced nothing OR a single unsplit blob (>50k chars).
    # Uses only the known top-level labels so internal sub-structure is ignored.
    is_unsplit_blob = len(exhibits) == 1 and len(exhibits[0].get('text', '')) > 50000
    if not exhibits or is_unsplit_blob:
        refs_result = _split_by_exhibit_references(doc_stem, text_path)
        if refs_result:
            exhibits = refs_result
            print(f"  [Strategy 1.5] Split into {len(exhibits)} exhibit(s) using known exhibit references.")

    # Strategy 2: Scan text for exhibit boundary markers (last resort).
    # Uses reduced pattern set (Exhibit/Appendix only) with boundary-context gating.
    is_unsplit_blob = len(exhibits) == 1 and len(exhibits[0].get('text', '')) > 50000
    if not exhibits or is_unsplit_blob:
        print("  [Strategy 2] Scanning text extraction for exhibit markers (fallback)...")
        # Prefer scanning _exhibits.md — it contains only exhibit content
        exhibits_md_path = os.path.join(TEMP_DIR, f"{doc_stem}_exhibits.md")
        if os.path.exists(exhibits_md_path):
            with open(exhibits_md_path, 'r', encoding='utf-8') as f:
                scan_text = f.read()
            print("  [Strategy 2] Scanning _exhibits.md (exhibit content only).")
        else:
            with open(text_path, 'r', encoding='utf-8') as f:
                scan_text = f.read()
            print("  [Strategy 2] Scanning full text extraction.")
        exhibits = _detect_exhibits_from_text(scan_text)

    if not exhibits:
        print(f"SUCCESS: No exhibits found in '{doc_stem}'. Nothing to split.")
        return

    print(f"  Found {len(exhibits)} exhibit(s):")
    for ex in exhibits:
        doc_type = _classify_exhibit(ex['title'], ex.get('text', '')[:500])
        pages = f"pages {ex.get('start_page', '?')}-{ex.get('end_page', '?')}"
        print(f"    Exhibit {ex['label']}: {ex['title'][:60]} ({doc_type}, {pages}, {len(ex.get('text', ''))} chars)")

    manifest_path = _write_exhibit_files(doc_stem, exhibits)

    print(
        f"SUCCESS: Split {len(exhibits)} exhibit(s) from '{doc_stem}'. "
        f"Manifest: {manifest_path}"
    )


if __name__ == "__main__":
    main()
