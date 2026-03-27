"""
01_INITIAL/main.py — Phase 1 pipeline:
  intake → detection → extraction → classification → TOC → Supabase

Run from any directory:
    python backend/01_INITIAL/main.py <filename>
"""
import ast
import json
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


def _run_exhibit_split(text_md: str, temp_dir: str, stem: str):
    """
    Run 07b → 05 (per exhibit) → 08b, unless the document is itself an exhibit.
    Documents classified as 'Exhibit - *' are already a single exhibit
    and should not be scanned for sub-exhibits.
    """
    class_json = os.path.join(temp_dir, stem + "_text_extraction_classification.json")
    if os.path.isfile(class_json):
        with open(class_json, encoding="utf-8") as f:
            doc_type = json.load(f).get("document_type", "")
        if doc_type.lower().startswith("exhibit"):
            print(f"  Document type '{doc_type}' is itself an exhibit — skipping exhibit split.")
            return

    _run("07b_exhibit_split.py", text_md)

    # Run proper GPT classification on each exhibit before uploading.
    # This overwrites the pattern-based guess 07b wrote with a real classification.
    manifest_path = os.path.join(temp_dir, stem + "_exhibit_manifest.json")
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        for exhibit in manifest:
            exhibit_text_md = os.path.join(temp_dir, f"{exhibit['exhibit_stem']}_text_extraction.md")
            if os.path.isfile(exhibit_text_md):
                print(f"  [05] Classifying exhibit {exhibit['exhibit_label']}...")
                _run("05_doc_classification.py", exhibit_text_md)

    _run("08b_Send_Exhibits_Supabase.py", text_md)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run Phase 1 pipeline on a single document.")
    parser.add_argument("filename",          help="Filename in data_storage/documents (or zz_Mockfiles via 01_Intake)")
    parser.add_argument("--case-id",         default=None, help="Supabase case UUID to attach this document to")
    parser.add_argument("--primary",         action="store_true", help="Mark this document as is_primary_filing=True")
    args = parser.parse_args()

    filename   = args.filename
    case_id    = args.case_id
    is_primary = args.primary

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

    # 5. Classification (always runs, regardless of TOC type)
    _run("05_doc_classification.py", text_md)

    # Build optional extra args for 08_Send_Supabase.py
    send_extra = []
    if case_id:
        send_extra += ["--case-id", case_id]
    if is_primary:
        send_extra += ["--primary"]

    # Branch: native TOC?
    has_native_toc = str(df.get("has_native_toc", [False])[0]).strip().lower() == "true"
    if has_native_toc:
        print("Native embedded TOC detected → skipping step 6, running 07_Native_TOC.py")
        _run("07_Native_TOC.py", text_md)
        _run("08_Send_Supabase.py", text_md, *send_extra)
        _run_exhibit_split(text_md, temp_dir, stem)
        return

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
    _run("08_Send_Supabase.py", text_md, *send_extra)

    # 7b/8b. Exhibit separation (runs after parent is in Supabase)
    _run_exhibit_split(text_md, temp_dir, stem)


if __name__ == "__main__":
    main()
