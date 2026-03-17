"""
01_INITIAL/main.py — Phase 1 pipeline:
  intake → detection → extraction → classification → TOC → Supabase

Run from any directory:
    python backend/01_INITIAL/main.py <filename>
"""
import ast
import os
import subprocess
import sys

import pandas as pd

PHASE_DIR   = os.path.dirname(os.path.abspath(__file__))   # …/backend/01_INITIAL
BACKEND_DIR = os.path.dirname(PHASE_DIR)                   # …/backend


def _run(script_name: str, *args):
    """Run a script in this phase's directory; exit on failure."""
    result = subprocess.run(
        [sys.executable, os.path.join(PHASE_DIR, script_name)] + list(args)
    )
    if result.returncode != 0:
        sys.exit(result.returncode)


def main():
    if len(sys.argv) != 2:
        print("Usage: python main.py <filename>")
        sys.exit(1)

    filename   = sys.argv[1]
    temp_dir   = os.path.join(BACKEND_DIR, "zz_temp_chunks")
    doc_path   = os.path.join(BACKEND_DIR, "data_storage", "documents", filename)
    stem       = os.path.splitext(filename)[0]
    struct_csv = os.path.join(temp_dir, stem + "_structure_report.csv")
    text_md    = os.path.join(temp_dir, stem + "_text_extraction.md")

    # 1. Intake
    _run("01_Intake.py", filename)

    # 2. Doc detection
    if not os.path.isfile(doc_path):
        print(f"File not found after intake: {doc_path}")
        sys.exit(1)
    _run("02_doc_detection.py", doc_path)

    # Parse structure report
    if not os.path.isfile(struct_csv):
        print(f"Structure report not found: {struct_csv}")
        sys.exit(1)
    df = pd.read_csv(struct_csv)

    num_tables = 0
    if "table_regions" in df.columns and pd.notnull(df["table_regions"][0]):
        try:
            table_regions = ast.literal_eval(df["table_regions"][0])
            num_tables = len(table_regions) if isinstance(table_regions, list) else 0
        except Exception:
            pass
    print(f"SUCCESS: 02_doc_detection.py ran successfully. Number of tables detected: {num_tables}")

    # 3. Image extraction
    _run("03_image_extraction.py", doc_path)

    # 4. Text extraction
    _run("04_text_extraction.py", struct_csv, doc_path)

    # Branch: native TOC?
    has_native_toc = str(df.get("has_native_toc", [False])[0]).strip().lower() == "true"
    if has_native_toc:
        print("Native embedded TOC detected → skipping steps 5-6, running 07_Native_TOC.py")
        _run("07_Native_TOC.py", text_md)
        _run("08_Send_Supabase.py", text_md)
        return

    # 5. Classification
    _run("05_doc_classification.py", text_md)

    # 6. TOC detection
    _run("06_TOC_detection.py", text_md)

    # Branch: TOC or no TOC?
    toc_csv = os.path.join(temp_dir, stem + "_text_extraction_toc_detection.csv")
    if not os.path.isfile(toc_csv):
        print(f"TOC detection CSV not found: {toc_csv}")
        sys.exit(1)
    toc_df  = pd.read_csv(toc_csv)
    has_toc = str(toc_df.iloc[0]["has_toc"]).strip().lower() == "yes"

    file_ext = os.path.splitext(filename)[1].lower()
    is_html  = file_ext in (".html", ".htm", ".xhtml")

    if is_html:
        print("HTML/HTM/XHTML file detected → running 07_HTML_TOC.py")
        _run("07_HTML_TOC.py", text_md)
    elif has_toc:
        print("TOC detected → running 07_Yes_TOC.py")
        _run("07_Yes_TOC.py", text_md)
    else:
        print("No TOC detected → running 07_No_TOC.py")
        _run("07_No_TOC.py", text_md)

    # 8. Send to Supabase
    _run("08_Send_Supabase.py", text_md)


if __name__ == "__main__":
    main()
