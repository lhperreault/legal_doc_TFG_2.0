import os
import re as _re_detect
import pandas as pd
import fitz  # PyMuPDF
import pdfplumber

_CHECKBOX_RE = _re_detect.compile(r"[\u2610\u2611\u2612\u2713\u2714\u2717\u2718]")


def _detect_form_signals(doc) -> tuple:
    """
    Returns (has_checkboxes, is_form_like).
    Scans the first 10 pages only.
    has_checkboxes: True if any /Btn widget annotation OR checkbox unicode char found.
    is_form_like:   True if >55% of non-empty lines are short (<40 chars), typical of forms.
    """
    pages_to_scan = min(len(doc), 10)
    checkbox_found = False
    total_lines = 0
    short_lines = 0

    for i in range(pages_to_scan):
        page = doc[i]

        # PDF form widget annotations (/Btn = checkbox / radio button, type index 2)
        if not checkbox_found:
            for annot in page.annots():
                if annot.type[0] == 2:
                    checkbox_found = True
                    break

        text = page.get_text()

        # Unicode checkbox characters
        if not checkbox_found and _CHECKBOX_RE.search(text):
            checkbox_found = True

        # Short-line ratio (form labels are short)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        total_lines += len(lines)
        short_lines += sum(1 for ln in lines if len(ln) < 40)

    short_ratio = (short_lines / total_lines) if total_lines > 0 else 0.0
    return checkbox_found, short_ratio >= 0.55

def find_email_column(df):
    for col in df.columns:
        if 'email' in col.lower():
            return col
    return None

def detect_email_signature(text, header_row=None):
    # Simple heuristic: look for common email fields
    if header_row is not None:
        headers = [h.lower() for h in header_row]
        if any(h in headers for h in ['from', 'to', 'subject', 'date']):
            return True
    if text is not None:
        lowered = text.lower()
        if any(x in lowered for x in ['from:', 'to:', 'subject:', 'sent:', 'date:']):
            return True
    return False

def analyze_document_structure(file_path):
    """
    Two-Stage Router:
    Stage 1: File type classification
    Stage 2: Visual element detection (tables, images, charts)
    Returns a structured report with extraction recommendations.
    """
    filename = os.path.basename(file_path)
    _, ext = os.path.splitext(filename)
    ext = ext.lower()

    result = {
        "file_name": filename,
        "file_type": None,
        "classification": None,
        "has_tables": False,
        "has_images": False,
        "has_native_toc": False,   # True when PDF has embedded bookmarks via doc.get_toc()
        "table_regions": [],
        "image_regions": [],
        "extraction_strategy": {}
    }

    # ==========================================
    # STAGE 1: FILE TYPE CLASSIFICATION
    # ==========================================

    # Native Emails
    if ext in ['.eml', '.msg', '.mbox']:
        result["file_type"] = "Native Email"
        result["classification"] = "Native_Email"
        result["extraction_strategy"]["text"] = "extract_msg library"
        df = pd.DataFrame([result])
        return df

    # Data Tables (CSV/Excel)
    if ext in ['.csv', '.xlsx', '.xls']:
        result["file_type"] = "Spreadsheet"
        try:
            if ext == '.csv':
                df_sample = pd.read_csv(file_path, nrows=5, encoding='latin1')
            else:
                df_sample = pd.read_excel(file_path, nrows=5)
            email_col = find_email_column(df_sample)
            if email_col:
                result["classification"] = "Raw_Email_Container"
                result["extraction_strategy"]["text"] = "Universal Email Parser"
            elif detect_email_signature(None, header_row=df_sample.columns):
                result["classification"] = "Structured_Email_CSV"
                result["extraction_strategy"]["text"] = "Pandas mapping"
            else:
                result["classification"] = "Financial_Data_Table"
                result["extraction_strategy"]["text"] = "Pandas/SQL"
                result["has_tables"] = True
                result["table_regions"].append({"location": "entire_file"})
        except Exception as e:
            result["classification"] = "Corrupt_File"
            result["extraction_strategy"]["error"] = str(e)
        df = pd.DataFrame([result])
        return df

    # HTML & TEXT INTELLIGENCE
    if ext in ['.txt', '.html', '.htm', '.xhtml']:
        result["file_type"] = "Text/HTML"
        result["classification"] = "Simple_Text"
        result["extraction_strategy"]["text"] = "Python native (free)"
        if ext in ['.html', '.htm', '.xhtml']:
            try:
                from bs4 import BeautifulSoup
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content_sample = f.read()
                soup = BeautifulSoup(content_sample, 'html.parser')
                tables = soup.find_all('table')
                table_count = len(tables)
                if table_count > 0:
                    result["has_tables"] = True
                    result["classification"] = "HTML_Complex"
                    result["extraction_strategy"]["text"] = "BeautifulSoup + Pandas (Table-Aware)"
                    for i in range(table_count):
                        result["table_regions"].append({
                            "location": f"HTML Table #{i+1}",
                            "type": "<table> tag"
                        })
            except Exception as e:
                result["extraction_strategy"]["warning"] = f"HTML check failed: {str(e)}"
        df = pd.DataFrame([result])
        return df

    # Images
    if ext in ['.jpg', '.png', '.jpeg', '.tiff', '.bmp', '.gif']:
        result["file_type"] = "Image"
        result["classification"] = "Scanned_Image"
        result["has_images"] = True
        result["image_regions"].append({"location": "entire_file"})
        result["extraction_strategy"]["image"] = "Tesseract OCR (free)"
        df = pd.DataFrame([result])
        return df

    # ==========================================
    # STAGE 2: PDF INTELLIGENCE (WITH REGION DETECTION)
    # ==========================================

    if ext == '.pdf':
        result["file_type"] = "PDF"
        try:
            doc = fitz.open(file_path)
            total_pages = len(doc)

            # ── Native TOC detection ──────────────────────────────────────────
            # doc.get_toc() returns [[level, title, page], ...] for "smart" PDFs.
            # We require at least 3 entries to distinguish a real TOC from
            # incidental bookmarks.
            native_toc = doc.get_toc()
            if len(native_toc) >= 3:
                result["has_native_toc"] = True
                print(f"    Native TOC detected: {len(native_toc)} entries")
            # ─────────────────────────────────────────────────────────────────

            # ── Form / checkbox detection ─────────────────────────────────────
            has_checkboxes, is_form_like = _detect_form_signals(doc)
            result["has_checkboxes"] = has_checkboxes
            result["is_form_like"]   = is_form_like
            if has_checkboxes or is_form_like:
                print(f"    Form signals: has_checkboxes={has_checkboxes}, is_form_like={is_form_like}")
            # ─────────────────────────────────────────────────────────────────
            text_lengths = []
            pages_with_images = 0
            for page_num in range(len(doc)):
                page = doc[page_num]
                text = page.get_text()
                text_lengths.append(len((text or "").strip()))
                raw_image_list = page.get_images()
                valid_images = []
                
                for img in raw_image_list:
                    width = img[2]
                    height = img[3]
                    
                    # 1. Filter out anything that is too small on ANY side (kills icons and thin lines)
                    if width < 30 or height < 30:
                        continue
                        
                    # 2. Filter out extreme aspect ratios (kills long text banners)
                    if min(width, height) > 0:
                        aspect_ratio = max(width, height) / min(width, height)
                        # If it is more than 6 times wider than it is tall (or vice versa), it's probably text/layout
                        if aspect_ratio > 6:
                            continue
                            
                    # If it passes both checks, it's a real picture/chart
                    valid_images.append(img)

                # Only proceed if we actually found valid, real images
                if valid_images:
                    result["has_images"] = True
                    pages_with_images += 1
                    for img_index, img in enumerate(valid_images):
                        result["image_regions"].append({
                            "page": page_num + 1,
                            "image_index": img_index,
                            "xref": img[0]
                        })
            total_text_chars = sum(text_lengths)
            low_text_pages = sum(1 for count in text_lengths if count < 80)
            low_text_ratio = (low_text_pages / total_pages) if total_pages else 0.0
            image_page_ratio = (pages_with_images / total_pages) if total_pages else 0.0
            result["extraction_strategy"]["diagnostics"] = {
                "total_pages": total_pages,
                "total_text_chars": total_text_chars,
                "low_text_pages": low_text_pages,
                "low_text_ratio": round(low_text_ratio, 3),
                "pages_with_images": pages_with_images,
                "image_page_ratio": round(image_page_ratio, 3),
            }
            result["extraction_strategy"]["has_checkboxes"] = has_checkboxes
            result["extraction_strategy"]["is_form_like"]   = is_form_like
            page_one_text = doc[0].get_text() if total_pages > 0 else ""
            if (
                total_pages == 0
                or total_text_chars < max(200, total_pages * 60)
                or (result["has_images"] and low_text_ratio >= 0.7)
                or (result["has_images"] and image_page_ratio >= 0.8 and total_text_chars < total_pages * 250)
            ):
                result["classification"] = "Scanned_PDF"
                result["extraction_strategy"]["full_document"] = "Tesseract OCR (free); optional Unstructured fallback via ENABLE_UNSTRUCTURED_FALLBACK"
                doc.close()
                df = pd.DataFrame([result])
                return df
            if detect_email_signature(page_one_text[:1000]):
                result["classification"] = "Email_PDF"
                result["extraction_strategy"]["text"] = "Regex extraction (free)"
                doc.close()
                df = pd.DataFrame([result])
                return df
            doc.close()
            with pdfplumber.open(file_path) as pdf:
                for page_num, page in enumerate(pdf.pages):
                    tables = page.find_tables()
                    if tables:
                        for table_index, table in enumerate(tables):
                            row_count = 0
                            col_count = 0
                            if hasattr(table, "rows") and table.rows:
                                row_count = len(table.rows)
                                first_row = table.rows[0]
                                if hasattr(first_row, "cells") and first_row.cells is not None:
                                    col_count = len(first_row.cells)
                                else:
                                    try:
                                        col_count = len(first_row)
                                    except Exception:
                                        col_count = 0
                            
                            # --- THE FAKE TABLE FILTER ---
                            # If it doesn't have at least 2 rows AND 2 columns, it's a footer/line, not a table!
                            if row_count < 2 or col_count < 2:
                                continue
                            # -----------------------------
                            
                            # If it passes the filter, we officially count it as a real table
                            result["has_tables"] = True
                            result["table_regions"].append({
                                "page": page_num + 1,
                                "table_index": table_index,
                                "bbox": table.bbox,
                                "rows": row_count,
                                "cols": col_count,
                            })
                # D. Classify based on complexity
                first_page = pdf.pages[0]
                horizontal_lines = len(first_page.lines)
                
                # Count the total number of tables found across the ENTIRE document
                total_tables = len(result["table_regions"])
                
                # Only flag as Complex if it's a massive data dump (e.g., more than 15 tables)
                if total_tables > 15:
                    result["classification"] = "Complex_Layout"
                    result["extraction_strategy"]["tables_only"] = "LlamaParse recommended for heavy data (paid)"
                    result["extraction_strategy"]["text"] = "PyMuPDF for non-table areas (free)"
                else:
                    # Let the Hybrid Parser handle normal documents with standard tables!
                    result["classification"] = "Standard_Legal"
                    result["extraction_strategy"]["text"] = "PyMuPDF + pdfplumber Hybrid (free)"
        except Exception as e:
            result["classification"] = "Error"
            result["extraction_strategy"]["error"] = str(e)
        df = pd.DataFrame([result])
        return df

    # Unknown format
    result["file_type"] = "Unknown"
    result["classification"] = "Unknown_Format"
    df = pd.DataFrame([result])
    return df

def analyze_and_save(file_path):
    df = analyze_document_structure(file_path)
    # Always output to backend/zz_temp_chunks
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    temp_chunks_dir = os.path.join(backend_dir, 'zz_temp_chunks')
    os.makedirs(temp_chunks_dir, exist_ok=True)
    out_path = os.path.join(temp_chunks_dir, os.path.splitext(os.path.basename(file_path))[0] + '_structure_report.csv')
    df.to_csv(out_path, index=False)
    return out_path


# If run as a script, process the file and log outcome
if __name__ == '__main__':
    import sys
    if len(sys.argv) != 2:
        print('Usage: python 02_doc_detection.py <file_path>')
        sys.exit(1)
    file_path = sys.argv[1]
    out_csv = analyze_and_save(file_path)
    # Print parser type to terminal
    import pandas as pd
    df = pd.read_csv(out_csv)
    classification = df['classification'][0] if 'classification' in df.columns else ''
    extraction_strategy = ''
    if 'extraction_strategy' in df.columns and pd.notnull(df['extraction_strategy'][0]):
        extraction_strategy = df['extraction_strategy'][0]
    print(f"INFO: Parser type selected: classification='{classification}', extraction_strategy={extraction_strategy}")
    # Print success to terminal
    print('SUCCESS: Document analysis complete. Output written to:', out_csv)
