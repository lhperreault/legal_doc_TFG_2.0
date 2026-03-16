import os
import sys
import re
import io
import ast
import email
import json
import pandas as pd
import pdfplumber
from dotenv import load_dotenv

# Load environment variables
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.env'))

from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

try:
    import extract_msg  # For Outlook .msg files
except Exception:
    extract_msg = None

try:
    import pytesseract
except Exception:
    pytesseract = None

try:
    from unstructured.partition.pdf import partition_pdf
    from unstructured.partition.image import partition_image
except Exception:
    partition_pdf = None
    partition_image = None

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


# ==========================================
# HELPER & CLEANING FUNCTIONS
# ==========================================

def _md_heading(level, text):
    level = max(1, min(level, 6))
    return f"{'#' * level} {text}".strip()

def _clean_md_text(value):
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    
    # --- FIX ALL THE BROKEN QUOTES AND BOXES ---
    text = text.replace('“', '"').replace('”', '"')
    text = text.replace('“', '"').replace('”', '"')
    
    # \ufffd is the safe, invisible computer code for the "Question Mark Box"!
    text = text.replace('\ufffd', '"') 
    # --------------------------------------------
    
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

_UI_LABEL_RE = re.compile(r"^@?\s*(Feedback|Downloads|Forums|App Store Connect|Customer Support|©|®|™)\s*$", flags=re.IGNORECASE)
_CONTROL_NOISE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b\u200c\u200d\ufeff]")
_PHONE_RE = re.compile(r"(?<!\d)(1)?[.\-\s]?(\d{3})[.\-\s]?(\d{3})[.\-\s]?(\d{4})(?!\d)")
_NUMERIC_ONLY_LINE_RE = re.compile(r"^\s*\d{1,3}\s*$")

def _is_protected_line(line: str) -> bool:
    if re.match(r"^\s*#{1,6}\s", line): return True
    if re.match(r"^\s*\[(Context|Page)\b", line): return True
    if re.match(r"^\s*(---|\*\*\*)\s*$", line): return True
    if re.match(r"^\s*\|.*\|\s*$", line): return True
    return False

def _is_isolated_icon_line(value: str) -> bool:
    stripped = value.strip()
    if not stripped: return False
    if stripped in {"|", ">"}: return True
    if len(stripped) <= 3 and not re.search(r"[A-Za-z0-9]", stripped): return True
    return False

def _normalize_phone_numbers(line: str) -> str:
    def _phone_repl(match: re.Match) -> str:
        raw, lead, p1, p2, p3 = match.group(0), match.group(1), match.group(2), match.group(3), match.group(4)
        digits_only = re.sub(r"\D", "", raw)
        has_separator = bool(re.search(r"[.\-\s]", raw))
        if len(digits_only) == 11 and digits_only.startswith("1"): return f"1-{p1}-{p2}-{p3}"
        if len(digits_only) == 10 and has_separator: return f"{p1}-{p2}-{p3}"
        return raw
    return _PHONE_RE.sub(_phone_repl, line)

def _normalize_body_line(line: str) -> str:
    if not line: return ""
    text = line.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
    text = re.sub(r"(?<=\S)[\u2013\u2014](?=\S)", "--", text)
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    text = re.sub(r"^[|>]\s+(?=[A-Za-z])", "", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    return _normalize_phone_numbers(text)

def _detect_margin_line_number_indexes(lines: list[str]) -> set[int]:
    candidates = []
    for idx, raw_line in enumerate(lines):
        if _is_protected_line(raw_line): continue
        stripped = str(raw_line or "").strip()
        if not stripped or not _NUMERIC_ONLY_LINE_RE.match(stripped): continue
        try:
            number = int(stripped)
            if 1 <= number <= 400: candidates.append((idx, number))
        except Exception: continue
    if len(candidates) < 8: return set()
    sequential_steps = sum(1 for i in range(1, len(candidates)) if candidates[i][1] == candidates[i - 1][1] + 1 or candidates[i][1] == 1 or (candidates[i-1][1] >= 20 and candidates[i][1] <= 3))
    if sequential_steps < max(4, int((len(candidates) - 1) * 0.35)): return set()
    return {idx for idx, _ in candidates}

def clean_ocr_text(text: str) -> str:
    if text is None: return ""
    normalized = _CONTROL_NOISE_RE.sub("", str(text).replace("\r\n", "\n").replace("\r", "\n"))
    raw_lines = normalized.split("\n")
    margin_number_indexes = _detect_margin_line_number_indexes(raw_lines)
    prepared = []
    for idx, raw_line in enumerate(raw_lines):
        if _is_protected_line(raw_line):
            prepared.append((raw_line, True))
            continue
        stripped = raw_line.strip()
        if not stripped:
            prepared.append(("", False))
            continue
        if _UI_LABEL_RE.match(stripped) or idx in margin_number_indexes or _is_isolated_icon_line(stripped): continue
        cleaned = _normalize_body_line(stripped)
        if cleaned: prepared.append((cleaned, False))
    merged_lines = []
    i = 0
    while i < len(prepared):
        current, protected = prepared[i]
        if protected or current == "":
            merged_lines.append(current)
            i += 1
            continue
        combined, j = current, i
        while j + 1 < len(prepared):
            nxt, nxt_protected = prepared[j + 1]
            if nxt_protected or nxt == "": break
            if re.search(r"[A-Za-z]-$", combined) and re.match(r"^[a-z]", nxt):
                combined = combined[:-1] + nxt
                j += 1; continue
            if re.search(r"[a-z,]$", combined) and re.match(r"^[a-z]", nxt):
                combined = combined + " " + nxt
                j += 1; continue
            break
        merged_lines.append(combined)
        i = j + 1
    return re.sub(r"\n{3,}", "\n\n", "\n".join(merged_lines)).strip("\n")


# ==========================================
# EMAIL PARSING ENGINES
# ==========================================

def _format_email_markdown(index, from_v, to_v, subject_v, date_v, body_v):
    body = _clean_md_text(body_v)
    return "\n".join([
        _md_heading(2, f"Email {index}"),
        f"- **From:** {from_v or ''}", f"- **To:** {to_v or ''}", f"- **Subject:** {subject_v or ''}", f"- **Date:** {date_v or ''}",
        "", _md_heading(3, "Body"), body or "(No body extracted)", ""
    ]).strip()

def _decode_header_value(value):
    if not value: return ""
    try: return str(make_header(decode_header(str(value))))
    except Exception: return str(value)

def _sanitize_subject(value):
    subject = _clean_md_text(value)
    if not subject or subject.lower().startswith(("mime-version:", "content-type:", "content-transfer-encoding:", "message-id:", "x-")): return ""
    return subject

def _decode_message_part(part):
    payload = part.get_payload(decode=True)
    if payload is None: return _clean_md_text(part.get_payload())
    if isinstance(payload, bytes):
        charset = part.get_content_charset() or "utf-8"
        try: return payload.decode(charset, errors="replace")
        except Exception: return payload.decode("latin1", errors="replace")
    return _clean_md_text(payload)

def _strip_quoted_reply_blocks(text):
    text = _clean_md_text(text)
    if not text: return ""
    split_patterns = [r"(?im)^\s*On\s.+?wrote:\s*$", r"(?im)^\s*From:\s.+$", r"(?im)^\s*-----\s*Original Message\s*-----\s*$", r"(?im)^\s*Sent from my\s.+$"]
    for pattern in split_patterns:
        match = re.search(pattern, text)
        if match:
            text = text[:match.start()].strip()
            break
    return _clean_md_text(text)

def _extract_email_body(msg):
    plain_parts, html_parts = [], []
    if msg.is_multipart():
        for part in msg.walk():
            content_type, disposition = part.get_content_type(), (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition: continue
            if content_type == "text/plain": plain_parts.append(_decode_message_part(part))
            elif content_type == "text/html": html_parts.append(_decode_message_part(part))
    else:
        content_type = msg.get_content_type()
        if content_type == "text/html": html_parts.append(_decode_message_part(msg))
        else: plain_parts.append(_decode_message_part(msg))
    
    if plain_parts: return _strip_quoted_reply_blocks("\n\n".join([p for p in plain_parts if p]))
    if html_parts:
        html_text = "\n\n".join([p for p in html_parts if p])
        html_text = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.IGNORECASE)
        html_text = re.sub(r"</p\s*>", "\n\n", html_text, flags=re.IGNORECASE)
        html_text = re.sub(r"<[^>]+>", " ", html_text)
        return _strip_quoted_reply_blocks(html_text)
    return ""

def _extract_header_fallback(raw_text, name):
    if not raw_text: return ""
    match = re.search(rf"(?im)^\s*{re.escape(name)}\s*:\s*(.+)$", raw_text)
    return match.group(1).strip() if match else ""

def _parse_email_like_text(raw_text):
    if not isinstance(raw_text, str) or not raw_text.strip(): return None
    source_text = raw_text.strip()
    try: msg = email.message_from_string(source_text, policy=policy.default)
    except Exception: msg = None

    from_v = _decode_header_value(msg.get("From")) if msg else ""
    to_v = _decode_header_value(msg.get("To")) if msg else ""
    subject_v = _decode_header_value(msg.get("Subject")) if msg else ""
    date_v = _decode_header_value(msg.get("Date")) if msg else ""
    body_v = _extract_email_body(msg) if msg else ""

    from_v = from_v or _extract_header_fallback(source_text, "From")
    to_v = to_v or _extract_header_fallback(source_text, "To")
    subject_v = _sanitize_subject(subject_v or _extract_header_fallback(source_text, "Subject"))
    date_v = date_v or _extract_header_fallback(source_text, "Date") or _extract_header_fallback(source_text, "Sent")

    if not body_v:
        separator_match = re.search(r"\n\s*\n", source_text)
        body_v = _strip_quoted_reply_blocks(source_text[separator_match.end():] if separator_match else source_text)

    return {"From": from_v, "To": to_v, "Subject": subject_v, "Date": date_v, "Body": _clean_md_text(body_v)[:5000]}

def universal_email_parser(file_path):
    print(f"🕵️‍♀️ Investigating: {file_path}")
    try:
        if file_path.endswith('.csv'): df = pd.read_csv(file_path, encoding='latin1', on_bad_lines='skip')
        elif file_path.endswith(('.xls', '.xlsx')): df = pd.read_excel(file_path)
        elif file_path.endswith('.json'): df = pd.read_json(file_path)
        else: return None, "Unsupported Container Format"

        mime_pattern = re.compile(r'(?i)(?:message-id:|mime-version:|content-type:|from:.*@|sent:.*20\d\d)', re.MULTILINE)
        target_col = next((col for col in df.columns if len(df[col].dropna().astype(str).head(5)) > 0 and df[col].dropna().astype(str).head(5).apply(lambda x: 1 if mime_pattern.search(x) else 0).sum() > (len(df[col].dropna().astype(str).head(5)) / 2)), None)
        
        if not target_col: return None, "No raw email content detected in any column."

        def parse_mime(raw_text):
            try:
                parsed = _parse_email_like_text(raw_text)
                if not parsed: return {}
                return {"from": parsed.get("From", ""), "to": parsed.get("To", ""), "subject": parsed.get("Subject", ""), "date": parsed.get("Date", ""), "body": parsed.get("Body", "")}
            except: return {}

        extracted_data = df[target_col].apply(parse_mime).apply(pd.Series)
        return extracted_data.dropna(subset=['body']), "Success"
    except Exception as e:
        return None, f"Critical Error: {str(e)}"

def parse_native_email(file_path):
    print(f"   [Parser] Running Native Email Parser on: {file_path}")
    metadata = {"from": "", "to": "", "subject": "", "date": "", "cc": ""}
    body_text = ""
    try:
        if file_path.lower().endswith('.msg'):
            if extract_msg is None: return f"{_md_heading(1, 'Parser Error')}\n\nextract_msg is not installed."
            msg = extract_msg.Message(file_path)
            metadata.update({"from": msg.sender, "to": msg.to, "cc": msg.cc, "subject": msg.subject, "date": msg.date})
            body_text = msg.body or ""
            msg.close()
        elif file_path.lower().endswith('.eml'):
            with open(file_path, 'rb') as f: raw_bytes = f.read()
            msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            metadata["from"] = _decode_header_value(msg['from']) or _extract_header_fallback(raw_text, "From")
            metadata["to"] = _decode_header_value(msg['to']) or _extract_header_fallback(raw_text, "To")
            metadata["subject"] = _decode_header_value(msg['subject']) or _extract_header_fallback(raw_text, "Subject")
            metadata["date"] = _decode_header_value(msg['date']) or _extract_header_fallback(raw_text, "Date")
            body_text = _extract_email_body(msg)
            if not body_text:
                separator_match = re.search(r"\n\s*\n", raw_text)
                body_text = _strip_quoted_reply_blocks(raw_text[separator_match.end():] if separator_match else raw_text)
        else: return f"{_md_heading(1, 'Parser Error')}\n\nUnsupported native email format."
    except Exception as e: return f"{_md_heading(1, 'Parser Error')}\n\nError parsing email: {str(e)}"

    parts = [_md_heading(1, "Email Document"), f"- **From:** {metadata['from'] or ''}", f"- **To:** {metadata['to'] or ''}"]
    if metadata['cc']: parts.append(f"- **Cc:** {metadata['cc']}")
    parts.extend([f"- **Subject:** {metadata['subject'] or ''}", f"- **Date:** {metadata['date'] or ''}", "", _md_heading(2, "Body"), _clean_md_text(body_text) or "(No body extracted)"])
    return _clean_md_text("\n".join(parts))


# ==========================================
# STANDARD PDF & HTML PARSERS
# ==========================================

def parse_standard_pdf(file_path):
    print(f"   [Parser] Running Hybrid Free Parser on: {file_path}")
    full_document_text = [_md_heading(1, "Document")]
    
    try:
        from PIL import Image # Ensure PIL is available
        doc = fitz.open(file_path)
        plumber_pdf = pdfplumber.open(file_path)
        
        for i in range(len(doc)):
            page_num = i + 1
            fitz_page = doc[i]
            text = fitz_page.get_text()
            
            # --- 1. THE GIBBERISH DETECTOR ---
            is_garbled = False
            if text and len(text.strip()) > 50:
                letters = sum(1 for c in text if c.isalpha())
                numbers = sum(1 for c in text if c.isnumeric())
                bad_codes = sum(1 for c in text if ord(c) < 32 and c not in ('\n', '\r', '\t', ' '))
                
                if (letters / len(text)) < 0.3 and (numbers / len(text)) < 0.3:
                    is_garbled = True
                elif bad_codes > 5:
                    is_garbled = True

            full_document_text.extend(["", _md_heading(2, f"Page {page_num}")])

            # --- 2. LOCALIZED OCR (If Garbled) ---
            if is_garbled and pytesseract is not None:
                print(f"     ⚠️ Page {page_num} has corrupted fonts! Running localized OCR...")
                pix = fitz_page.get_pixmap(matrix=fitz.Matrix(2, 2))
                image = Image.open(io.BytesIO(pix.tobytes("png")))
                
                # OVERWRITE the text with OCR, completely bypassing standard extraction
                ocr_text = pytesseract.image_to_string(image)
                full_document_text.append("> *Note: Text on this page was extracted via OCR due to corrupted PDF fonts.*\n")
                full_document_text.append(_clean_md_text(ocr_text))
                
                # Skip the table extraction for this page because the tables are also garbled!
                continue 

            # --- 3. STANDARD EXTRACTION (If Clean) ---
            # Append standard text
            full_document_text.append(_clean_md_text(text))
            
            # Extract and append tables for this page
            try:
                plumber_page = plumber_pdf.pages[i]
                tables = plumber_page.extract_tables()
                if tables:
                    full_document_text.extend(["", _md_heading(3, "Detected Table Data")])
                    for table_data in tables:
                        clean_table = [[cell if cell is not None else "" for cell in row] for row in table_data]
                        df = pd.DataFrame(clean_table[1:], columns=clean_table[0]) if len(clean_table) > 1 else pd.DataFrame(clean_table)
                        try: full_document_text.append(df.to_markdown(index=False))
                        except ImportError: full_document_text.append(df.to_string(index=False))
                        full_document_text.append("") # Spacing
            except Exception as e:
                pass # Gracefully ignore table errors
                
        doc.close()
        plumber_pdf.close()
        
    except Exception as e: 
        return f"{_md_heading(1, 'Parser Error')}\n\nError parsing PDF: {str(e)}"
        
    return _clean_md_text("\n\n".join(full_document_text))

def _inject_html_tracking_ids(file_path: str, soup) -> str:
    """
    Inject unique ai-chunk-NNNNN IDs into every readable structural element,
    preserving any existing IDs (native TOC anchors stay intact).
    Saves the tagged file to zz_temp_chunks/ui_assets/ and returns its path.
    """
    target_tags = ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'td', 'div']
    counter = 1
    for tag in soup.find_all(target_tags):
        if not tag.get_text(strip=True):
            continue
        if not tag.has_attr('id'):
            tag['id'] = f"ai-chunk-{counter:05d}"
            counter += 1

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ui_assets_dir = os.path.join(backend_dir, 'zz_temp_chunks', 'ui_assets')
    os.makedirs(ui_assets_dir, exist_ok=True)

    out_filename = os.path.splitext(os.path.basename(file_path))[0] + '_tagged.xhtml'
    out_path = os.path.join(ui_assets_dir, out_filename)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(str(soup))

    print(f"   [Tagger] Injected {counter - 1} tracking IDs → {out_path}")
    return out_path


def parse_html_filing(file_path):
    print(f"   [Parser] Running HTML Parser on: {file_path}")
    try:
        from bs4 import BeautifulSoup
        import io
        import pandas as pd
        import warnings

        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        # Use the XML parser for .xhtml/.xml files (e.g. iXBRL annual reports)
        # so that namespace attributes and existing anchor IDs are preserved
        # correctly in the tagged file written by _inject_html_tracking_ids.
        # Plain .html/.htm files continue to use the standard HTML parser.
        ext = os.path.splitext(file_path)[1].lower()
        parser = 'lxml-xml' if ext in ('.xhtml', '.xml') else 'lxml'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            soup = BeautifulSoup(content, parser)

        # Inject tracking IDs before stripping tags (saves tagged UI file as side effect)
        _inject_html_tracking_ids(file_path, soup)

        # Strip out code elements
        for tag in soup(['script', 'style', 'meta', 'noscript', 'svg']):
            tag.decompose()
        
        # Extract tables
        tables = soup.find_all('table')
        for i, table in enumerate(tables):
            try:
                dfs = pd.read_html(io.StringIO(str(table)))
                if dfs:
                    markdown_table = dfs[0].to_markdown(index=False)
                    table.replace_with(f"\n\n{_md_heading(3, f'Table {i+1}')}\n\n{markdown_table}\n\n")
            except Exception: 
                continue
        
        # --- THE FIX IS HERE ---
        # Get the text using get_text() with a simple space separator, then clean up the extra spaces
        raw_text = soup.get_text(separator='\n\n', strip=True)
        
        return _clean_md_text(f"{_md_heading(1, 'Document')}\n\n{raw_text}")
        
    except Exception as e: 
        return f"{_md_heading(1, 'Parser Error')}\n\nError parsing HTML: {str(e)}"
    
def parse_structured_data(file_path, strategy):
    print(f"   [Parser] Running Structured Data Parser on: {file_path}")
    try:
        if file_path.lower().endswith('.csv'): df = pd.read_csv(file_path, encoding='latin1', on_bad_lines='skip')
        else: df = pd.read_excel(file_path)

        if strategy == "Raw_Email_Container":
            def extract_mime(raw_text):
                try:
                    parsed = _parse_email_like_text(raw_text)
                    if not parsed: return None
                    parsed["Body"] = _clean_md_text(parsed.get("Body", ""))[:2000]
                    return parsed
                except: return None
            
            target_col = next((col for col in df.columns if "Message-ID:" in df[col].dropna().astype(str).head(5).to_string() or "From:" in df[col].dropna().astype(str).head(5).to_string()), None)
            if target_col:
                extracted_data = df[target_col].apply(extract_mime).dropna()
                output_parts = [_md_heading(1, "Extracted Emails")] + [_format_email_markdown(i + 1, d.get('From'), d.get('To'), d.get('Subject'), d.get('Date'), d.get('Body')) for i, d in enumerate(extracted_data)]
                return _clean_md_text("\n\n".join(output_parts))
            return f"{_md_heading(1, 'Parser Error')}\n\nIdentified as Email Container but could not find MIME column."
            
        elif strategy in ["Financial_Data_Table", "Structured_Email_CSV"]:
            return _clean_md_text(f"{_md_heading(1, 'Structured Data')}\n\n{df.to_markdown(index=False)}")
        return f"{_md_heading(1, 'Parser Error')}\n\nUnknown structured data strategy."
    except Exception as e: return f"{_md_heading(1, 'Parser Error')}\n\nError parsing spreadsheet: {str(e)}"


# ==========================================
# OCR & IMAGE EXTRACTION
# ==========================================

from PIL import Image

def parse_raw_image(file_path):
    print(f"   [Parser] Running Raw Image OCR on: {file_path}")
    try:
        from PIL import Image
        import pytesseract
        
        # Open the raw image file
        image = Image.open(file_path)
        
        # Feed it directly to the OCR engine
        text = pytesseract.image_to_string(image)
        
        return _clean_md_text(f"{_md_heading(1, 'Document')}\n\n> *Note: Text extracted via direct image OCR.*\n\n{text}")
        
    except Exception as e:
        return f"{_md_heading(1, 'Parser Error')}\n\nError running OCR on image: {str(e)}"


def _configure_tesseract_path():
    if pytesseract is None: return
    env_cmd = os.getenv("TESSERACT_CMD", "").strip()
    if env_cmd and os.path.exists(env_cmd):
        pytesseract.pytesseract.tesseract_cmd = env_cmd
        return
    for candidate in [r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe", r"C:\Users\lukep\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"]:
        if os.path.exists(candidate):
            pytesseract.pytesseract.tesseract_cmd = candidate
            return

_configure_tesseract_path()

def parse_scanned_image(file_path):
    print(f"   [Parser] Running Tesseract OCR on: {file_path}")
    extracted_text = [_md_heading(1, "OCR Extracted Document")]
    try:
        try:
            if pytesseract is None: raise RuntimeError("pytesseract missing")
            _ = pytesseract.get_tesseract_version()
        except Exception:
            return f"{_md_heading(1, 'Parser Error')}\n\nTesseract OCR is not installed or not in PATH."

        ext = os.path.splitext(file_path)[1].lower()
        if ext in ['.jpg', '.jpeg', '.png', '.bmp', '.tiff']:
            image = Image.open(file_path)
            extracted_text.extend([_md_heading(2, "Image"), _clean_md_text(pytesseract.image_to_string(image))])
        elif ext == '.pdf':
            doc = fitz.open(file_path)
            for i, page in enumerate(doc):
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                image = Image.open(io.BytesIO(pix.tobytes("png")))
                extracted_text.extend(["", _md_heading(2, f"Page {i+1} (Scanned)"), _clean_md_text(pytesseract.image_to_string(image))])
            doc.close()
    except Exception as e: return f"{_md_heading(1, 'Parser Error')}\n\nError during OCR: {str(e)}"
    return _clean_md_text("\n\n".join(extracted_text))

def parse_with_unstructured(file_path):
    print(f"   [Parser] Running Unstructured fallback on: {file_path}")
    ext, elements = os.path.splitext(file_path)[1].lower(), []
    try:
        if ext == ".pdf":
            if partition_pdf is None: return f"{_md_heading(1, 'Parser Error')}\n\nUnstructured PDF parser not installed."
            elements = partition_pdf(filename=file_path, strategy="auto", infer_table_structure=True)
        elif ext in [".jpg", ".jpeg", ".png", ".bmp", ".tiff"]:
            if partition_image is None: return f"{_md_heading(1, 'Parser Error')}\n\nUnstructured image parser not installed."
            elements = partition_image(filename=file_path)
        else: return f"{_md_heading(1, 'Parser Error')}\n\nUnstructured fallback supports PDF/image only."

        extracted = [_md_heading(1, "Unstructured Extracted Document")]
        for item in elements:
            text = _clean_md_text(str(item))
            if text: extracted.append(text)

        final_text = _clean_md_text("\n\n".join(extracted))
        if len(re.sub(r"\s+", "", final_text)) < 120: return f"{_md_heading(1, 'Parser Error')}\n\nUnstructured returned too little text."
        return final_text
    except Exception as e: return f"{_md_heading(1, 'Parser Error')}\n\nUnstructured parse failed: {str(e)}"


# ==========================================
# ADVANCED & FALLBACK EXTRACTORS
# ==========================================

try:
    from llama_parse import LlamaParse
except Exception:
    LlamaParse = None

def parse_complex_layout(file_path):
    print(f"   [Parser] 💰 Activating LlamaParse for: {file_path}")
    if LlamaParse is None: return "Error: llama_parse is not installed."
    api_key = os.getenv("LLAMA_CLOUD_API_KEY")
    if not api_key:
        return "Error: Missing LlamaCloud API Key."
    os.environ["LLAMA_CLOUD_API_KEY"] = api_key
    try:
        parser = LlamaParse(result_type="markdown", verbose=True, language="en")
        documents = parser.load_data(file_path)
        if not documents: return "Error: LlamaParse returned no content."
        return "\n\n".join([doc.text for doc in documents])
    except Exception as e: return f"Error during LlamaParse: {str(e)}"

def _extract_best_effort_text(file_path: str, max_chars: int = 120000) -> str:
    ext = os.path.splitext(str(file_path).lower())[1]
    try:
        if ext == ".pdf" and fitz is not None:
            parts = []
            with fitz.open(file_path) as doc:
                for idx, page in enumerate(doc):
                    text = page.get_text() or ""
                    if text.strip(): parts.append(f"[Page {idx+1}]\n{text.strip()}")
                    if sum(len(p) for p in parts) >= max_chars: break
            return "\n\n".join(parts)[:max_chars]
        if ext in [".txt", ".md", ".html", ".htm", ".xhtml", ".eml"]:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f: return f.read(max_chars)
        if ext in [".csv"]: return pd.read_csv(file_path, encoding="latin1", on_bad_lines="skip").head(200).to_markdown(index=False)[:max_chars]
        if ext in [".xlsx", ".xls"]: return pd.read_excel(file_path).head(200).to_markdown(index=False)[:max_chars]
    except Exception as e: return f"Best-effort extraction failed: {e}"
    return ""

# ==========================================
# SMART PDF PARSER  (native embedded TOC)
# ==========================================

def parse_smart_pdf(file_path: str) -> str:
    """
    For PDFs that have an embedded navigation structure (doc.get_toc()).
    Extracts the native TOC and full page text.

    Side-effect: saves {stem}_native_toc.json to zz_temp_chunks/ so that
    07_Native_TOC.py can consume it directly without re-opening the PDF.

    Returns standard markdown with ## Page N markers (same format as
    parse_standard_pdf) so the rest of the pipeline is unaffected.
    """
    import json
    print(f"   [Parser] Running Smart PDF Parser (native TOC) on: {file_path}")

    backend_dir   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    temp_dir      = os.path.join(backend_dir, "zz_temp_chunks")
    os.makedirs(temp_dir, exist_ok=True)
    stem          = os.path.splitext(os.path.basename(file_path))[0]
    toc_json_path = os.path.join(temp_dir, stem + "_native_toc.json")

    try:
        doc = fitz.open(file_path)

        # ── 1. Extract native TOC ────────────────────────────────────────────
        raw_toc = doc.get_toc()          # [[level, title, page], ...]
        toc_entries = [
            {"level": level, "title": title, "page": page}
            for level, title, page in raw_toc
        ]
        with open(toc_json_path, "w", encoding="utf-8") as f:
            json.dump({"entries": toc_entries}, f, ensure_ascii=False, indent=2)
        print(f"   [Smart PDF] Native TOC saved: {len(toc_entries)} entries → {toc_json_path}")

        # ── 2. Extract full page text (same format as parse_standard_pdf) ────
        parts = [_md_heading(1, "Document")]
        for i, page in enumerate(doc):
            page_num = i + 1
            text = page.get_text() or ""

            # Garbled-font check → local OCR fallback
            is_garbled = False
            if text and len(text.strip()) > 50:
                letters = sum(1 for c in text if c.isalpha())
                bad_codes = sum(1 for c in text if ord(c) < 32 and c not in ('\n', '\r', '\t', ' '))
                if (letters / len(text)) < 0.3 or bad_codes > 5:
                    is_garbled = True

            parts.append("")
            parts.append(_md_heading(2, f"Page {page_num}"))

            if is_garbled and pytesseract is not None:
                from PIL import Image as _PIL_Image
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                    if pix.colorspace and pix.colorspace.name not in ("DeviceRGB", "DeviceGray"):
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    img = _PIL_Image.open(io.BytesIO(pix.tobytes("png")))
                    ocr_text = pytesseract.image_to_string(img)
                    parts.append("> *Note: Text on this page was extracted via OCR due to corrupted PDF fonts.*\n")
                    parts.append(_clean_md_text(ocr_text))
                except Exception as ocr_err:
                    parts.append(_clean_md_text(text))
                    print(f"     OCR fallback failed on page {page_num}: {ocr_err}")
            else:
                parts.append(_clean_md_text(text))

        doc.close()
        return _clean_md_text("\n\n".join(parts))

    except Exception as exc:
        return f"{_md_heading(1, 'Parser Error')}\n\nSmart PDF parse failed: {exc}"


# ==========================================
# FALLBACK HELPERS
# ==========================================

def _result_is_bad(result) -> bool:
    """True if the parser returned an error message or too little usable text."""
    if not isinstance(result, str):
        return True
    s = result.strip()
    if s.startswith("# Parser Error") or s.lower().startswith("error:"):
        return True
    # Strip all markdown / whitespace noise and count real characters
    plain = re.sub(r"[#*>\-|`\[\]\s]", "", s)
    return len(plain) < 300


def _pdf_fallback_chain(pdf_path: str, already_tried: str) -> str:
    """
    Try PDF parsers in order until one produces good output.
    Skips whichever parser was already tried as the primary.
    Order: standard → OCR (Tesseract) → unstructured → LlamaParse (AI)
    """
    steps = [
        ("Standard",       "parse_standard_pdf",      lambda p: parse_standard_pdf(p)),
        ("OCR/Tesseract",  "parse_scanned_image",      lambda p: parse_scanned_image(p)),
        ("Unstructured",   "parse_with_unstructured",  lambda p: parse_with_unstructured(p)),
        ("LlamaParse(AI)", "parse_complex_layout",     lambda p: parse_complex_layout(p)),
    ]
    for label, fn_name, fn in steps:
        if fn_name == already_tried:
            continue
        print(f"   [Fallback] Trying {label} parser...")
        try:
            result = fn(pdf_path)
        except Exception as exc:
            print(f"   [Fallback] {label} raised: {exc}")
            result = f"# Parser Error\n\n{exc}"
        if not _result_is_bad(result):
            print(f"   [Fallback] {label} succeeded.")
            return result
        print(f"   [Fallback] {label} returned poor output, continuing...")
    return f"# Parser Error\n\nAll PDF parsers failed for: {pdf_path}"


# ==========================================
# MAIN EXECUTION ROUTER
# ==========================================

def main():
    if len(sys.argv) != 3:
        print('Usage: python 04_text_extraction.py <structure_csv> <pdf_path>')
        sys.exit(1)
        
    structure_csv = sys.argv[1]
    pdf_path = sys.argv[2]
    
    df = pd.read_csv(structure_csv)
    classification = df['classification'][0] if 'classification' in df.columns else ''
    file_type = df['file_type'][0] if 'file_type' in df.columns else ''
    
    # Route to the correct extraction function
    is_pdf = os.path.splitext(pdf_path)[1].lower() == '.pdf'
    has_native_toc = str(df.get('has_native_toc', [False])[0]).strip().lower() == 'true'
    primary_fn = None   # name of primary parser, used to skip it in fallback

    # Smart PDFs with embedded TOC bypass their classification-based parser
    # and go to parse_smart_pdf which saves the native TOC as a side-effect.
    if is_pdf and has_native_toc:
        result = parse_smart_pdf(pdf_path)
        primary_fn = 'parse_smart_pdf'
    elif classification == 'Complex_Layout':
        result = parse_complex_layout(pdf_path)
        primary_fn = 'parse_complex_layout'
    elif classification == 'Scanned_PDF':
        result = parse_scanned_image(pdf_path)
        primary_fn = 'parse_scanned_image'
    elif classification == 'Standard_Legal':
        result = parse_standard_pdf(pdf_path)
        primary_fn = 'parse_standard_pdf'
    elif classification == 'Email_PDF':
        result = universal_email_parser(pdf_path)
        primary_fn = None   # email parser — not in PDF fallback chain
    elif file_type == 'Native Email':
        result = parse_native_email(pdf_path)
        primary_fn = None
    elif file_type == 'Spreadsheet':
        strategy = ''
        if 'extraction_strategy' in df.columns and pd.notnull(df['extraction_strategy'][0]):
            try:
                strategy_dict = ast.literal_eval(df['extraction_strategy'][0]) if isinstance(df['extraction_strategy'][0], str) else df['extraction_strategy'][0]
                strategy = strategy_dict.get('text', '') if isinstance(strategy_dict, dict) else ''
            except Exception:
                strategy = ''
        result = parse_structured_data(pdf_path, strategy)
        primary_fn = None
    elif classification in ['Simple_Text', 'HTML_Complex'] or file_type == 'Text/HTML':
        result = parse_html_filing(pdf_path)
        primary_fn = None
    elif classification == 'Scanned_Image' or file_type == 'Image':
        result = parse_raw_image(pdf_path)
        primary_fn = None
    else:
        result = _extract_best_effort_text(pdf_path)
        primary_fn = None

    # ── PDF fallback chain ────────────────────────────────────────────────────
    # If the primary parser returned an error or very little text, and this is
    # a PDF, try the remaining parsers in order (standard → OCR → unstructured
    # → LlamaParse AI) until one produces a good result.
    if is_pdf and _result_is_bad(result):
        print(f"   [Fallback] Primary parser produced poor output — activating PDF fallback chain...")
        result = _pdf_fallback_chain(pdf_path, already_tried=primary_fn or "")
        
    # Write result to zz_temp_chunks
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    temp_chunks_dir = os.path.join(backend_dir, 'zz_temp_chunks')
    os.makedirs(temp_chunks_dir, exist_ok=True)
    
    out_path = os.path.join(temp_chunks_dir, os.path.splitext(os.path.basename(pdf_path))[0] + '_text_extraction.md')
    
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(result if isinstance(result, str) else str(result))
        
    print(f"SUCCESS: 04_text_extraction.py ran successfully. Output written to: {out_path}")

# ==========================================
# THIS MUST BE THE ABSOLUTE LAST THING IN THE FILE
# ==========================================
if __name__ == '__main__':
    main()