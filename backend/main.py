"""
main.py - Entry point to run 01_Intake.py with a specified filename.
"""
import subprocess
import sys

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python main.py <filename>")
        sys.exit(1)
    filename = sys.argv[1]
    # Call 01_Intake.py with the filename argument
    result = subprocess.run([
        sys.executable,
        "backend/01_INITIAL/01_Intake.py",
        filename
    ])
    if result.returncode != 0:
        sys.exit(result.returncode)

    # After intake, run 02_doc_detection.py on the file in backend/data_storage/documents
    import os
    project_root = os.path.abspath(os.path.dirname(__file__))
    doc_path = os.path.join(project_root, 'data_storage', 'documents', filename)
    if not os.path.isfile(doc_path):
        print(f"File not found for doc detection: {doc_path}")
        sys.exit(1)
    result2 = subprocess.run([
        sys.executable,
        "backend/01_INITIAL/02_doc_detection.py",
        doc_path
    ])
    if result2.returncode != 0:
        sys.exit(result2.returncode)

    # Convert the CSV output to JSON for 03_image_extraction.py
    import pandas as pd
    csv_path = os.path.join(project_root, 'zz_temp_chunks', os.path.splitext(filename)[0] + '_structure_report.csv')
    json_path = os.path.join(project_root, 'zz_temp_chunks', '02_doc_detection_outcome.json')
    if not os.path.isfile(csv_path):
        print(f"CSV output not found for conversion: {csv_path}")
        sys.exit(1)
    df = pd.read_csv(csv_path)
    # Print number of tables found (length of table_regions list)
    import ast
    num_tables = 0
    if 'table_regions' in df.columns and pd.notnull(df['table_regions'][0]):
        try:
            table_regions = ast.literal_eval(df['table_regions'][0])
            num_tables = len(table_regions) if isinstance(table_regions, list) else 0
        except Exception:
            num_tables = 0
    print(f"SUCCESS: 02_doc_detection.py ran successfully. Number of tables detected: {num_tables}")

    # Now run 03_image_extraction.py with the document path
    result3 = subprocess.run([
        sys.executable,
        "backend/01_INITIAL/03_image_extraction.py",
        doc_path
    ])
    if result3.returncode != 0:
        sys.exit(result3.returncode)

    # Now run 04_text_extraction.py with structure CSV and PDF path
    structure_csv = os.path.join(project_root, 'zz_temp_chunks', os.path.splitext(filename)[0] + '_structure_report.csv')
    pdf_path = os.path.join(project_root, 'data_storage', 'documents', filename)
    result4 = subprocess.run([
        sys.executable,
        "backend/01_INITIAL/04_text_extraction.py",
        structure_csv,
        pdf_path
    ])
    if result4.returncode != 0:
        sys.exit(result4.returncode)

    # Check whether 02_doc_detection found a native embedded TOC.
    # If yes, skip classification + TOC detection and go straight to 07_Native_TOC.py.
    text_md = os.path.join(project_root, 'zz_temp_chunks', os.path.splitext(filename)[0] + '_text_extraction.md')
    has_native_toc = str(df.get('has_native_toc', [False])[0]).strip().lower() == 'true'
    if has_native_toc:
        print("Native embedded TOC detected → skipping steps 5-6, running 07_Native_TOC.py")
        result7 = subprocess.run([
            sys.executable,
            "backend/01_INITIAL/07_Native_TOC.py",
            text_md
        ])
        if result7.returncode != 0:
            sys.exit(result7.returncode)
        # Send to Supabase
        result8 = subprocess.run([
            sys.executable,
            "backend/01_INITIAL/08_Send_Supabase.py",
            text_md
        ])
        sys.exit(result8.returncode)

    # Now run 05_doc_classification.py with the text extraction output
    result5 = subprocess.run([
        sys.executable,
        "backend/01_INITIAL/05_doc_classification.py",
        text_md
    ])
    if result5.returncode != 0:
        sys.exit(result5.returncode)

    # Now run 06_TOC_detection.py with the text extraction output
    result6 = subprocess.run([
        sys.executable,
        "backend/01_INITIAL/06_TOC_detection.py",
        text_md
    ])
    if result6.returncode != 0:
        sys.exit(result6.returncode)

    # Read TOC detection result and branch to 07_Yes_TOC.py or 07_No_TOC.py
    toc_csv = os.path.join(
        project_root, 'zz_temp_chunks',
        os.path.splitext(filename)[0] + '_text_extraction_toc_detection.csv'
    )
    if not os.path.isfile(toc_csv):
        print(f"TOC detection CSV not found: {toc_csv}")
        sys.exit(1)

    toc_df = pd.read_csv(toc_csv)
    has_toc = str(toc_df.iloc[0]['has_toc']).strip().lower() == 'yes'

    file_ext = os.path.splitext(filename)[1].lower()
    is_html = file_ext in ['.html', '.htm', '.xhtml']

    if is_html:
        print("HTML/HTM/XHTML file detected → running 07_HTML_TOC.py")
        next_script = "backend/01_INITIAL/07_HTML_TOC.py"
    elif has_toc:
        print("TOC detected → running 07_Yes_TOC.py")
        next_script = "backend/01_INITIAL/07_Yes_TOC.py"
    else:
        print("No TOC detected → running 07_No_TOC.py")
        next_script = "backend/01_INITIAL/07_No_TOC.py"

    result7 = subprocess.run([
        sys.executable,
        next_script,
        text_md
    ])
    if result7.returncode != 0:
        sys.exit(result7.returncode)

    # Send to Supabase
    result8 = subprocess.run([
        sys.executable,
        "backend/01_INITIAL/08_Send_Supabase.py",
        text_md
    ])
    if result8.returncode != 0:
        sys.exit(result8.returncode)
