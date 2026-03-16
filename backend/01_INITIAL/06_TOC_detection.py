import sys
import os
import re
import pandas as pd

def detect_table_of_contents(text: str) -> dict:
    """
    Detect if a Table of Contents exists using strict headers, dot leaders, 
    whitespace gaps, and legal enumeration structures.
    """
    results = {
        "has_toc": False,
        "methods_triggered": []
    }

    # METHOD 1: Strict Header Check
    # Matches "Table of Contents" or "Contents" alone on a line (ignores in-sentence use)
    header_pattern = re.compile(r'(?im)^#{0,4}\s*(table\s+of\s+contents|contents|index)\s*$')
    if header_pattern.search(text):
        results["methods_triggered"].append("Strict Header Match")

    # METHOD 2: Classic Dot Leaders
    # Example: "Introduction ........... 4"
    dot_pattern = re.compile(r'(?m)^.{5,120}?(?:\.{3,})\s*\d{1,4}\s*$')
    dot_matches = dot_pattern.findall(text)
    if len(dot_matches) >= 3:
        results["methods_triggered"].append(f"Dot Leaders (found {len(dot_matches)} rows)")

    # METHOD 3: Spaced Page Numbers (No Dots)
    # Example: "Purchase Price          14"
    # Negative lookahead (?!.*[$%]) ensures we aren't accidentally reading a financial table!
    space_num_pattern = re.compile(r'(?m)^(?!\s*[\d\.]+\s*$)(?!.*[$%]).{5,100}?\s{3,}\d{1,4}\s*$')
    space_matches = space_num_pattern.findall(text)
    if len(space_matches) >= 4:
        results["methods_triggered"].append(f"Spaced Page Numbers (found {len(space_matches)} rows)")

    # METHOD 4: Legal Enumeration
    # Example: "Article II: Definitions 5" or "Section 1.1 Scope 6"
    # Catches TOCs even if the parser squashed the whitespace down to a single space.
    legal_enum_pattern = re.compile(r'(?im)^(?:article|section|clause|exhibit|\d+\.\d+|[IVX]+\.?)\s+.{5,100}?\s+\d{1,4}\s*$')
    legal_matches = legal_enum_pattern.findall(text)
    if len(legal_matches) >= 4:
        results["methods_triggered"].append(f"Legal Enumeration Format (found {len(legal_matches)} rows)")

    # --- DECISION LOGIC ---
    # It has a TOC if we found a strict header, OR if we found a repeated structural pattern
    if "Strict Header Match" in results["methods_triggered"]:
        results["has_toc"] = True
    elif len(dot_matches) >= 4 or len(space_matches) >= 5 or len(legal_matches) >= 4:
        results["has_toc"] = True

    # Summary output
    print("=" * 50)
    print("TABLE OF CONTENTS DETECTION")
    print("=" * 50)
    print(f"Result:              {'✅ YES' if results['has_toc'] else '❌ NO'}")
    if results["methods_triggered"]:
        print("Triggers:")
        for m in results["methods_triggered"]:
            print(f"  → {m}")
    print("=" * 50)

    return results

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python 06_TOC_detection.py <text_extraction_md>")
        sys.exit(1)
    text_path = sys.argv[1]
    if not os.path.isfile(text_path):
        print(f"File not found: {text_path}")
        sys.exit(1)
    with open(text_path, 'r', encoding='utf-8') as f:
        document_text = f.read()
    result = detect_table_of_contents(document_text)
    # Save result as CSV (yes/no)
    out_csv = os.path.join(os.path.dirname(text_path), os.path.splitext(os.path.basename(text_path))[0] + '_toc_detection.csv')
    pd.DataFrame([{ 'has_toc': 'yes' if result['has_toc'] else 'no' }]).to_csv(out_csv, index=False)
    print(f"TOC detection result saved to: {out_csv}")