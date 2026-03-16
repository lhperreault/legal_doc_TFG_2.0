# =========================
# Imports
# =========================
import os
import shutil

# =========================
# Function: pull_data_from_mockfiles
# =========================
def pull_data_from_mockfiles(filename):
    # Find zz_Mockfiles at the project root
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    mockfiles_dir = os.path.join(project_root, 'zz_Mockfiles')
    source_path = os.path.join(mockfiles_dir, filename)
    if not os.path.isfile(source_path):
        print(f"File '{filename}' not found in zz_Mockfiles.")
        return False, filename
    # Destination is backend/data_storage/documents
    backend_dir = os.path.join(project_root, 'backend')
    dest_dir = os.path.join(backend_dir, 'data_storage', 'documents')
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)
    try:
        shutil.copy2(source_path, dest_path)
        print(f"Successfully pulled '{filename}' to {dest_path}.")
        return True, filename
    except Exception as e:
        print(f"Failed to pull file: {e}")
        return False, filename

# =========================
# Function: write_outcome_to_md
# =========================
def write_outcome_to_md(success, filename):
    # Write outcome.md to zz_temp_chunks in backend directory
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    temp_chunks_dir = os.path.join(backend_dir, 'zz_temp_chunks')
    os.makedirs(temp_chunks_dir, exist_ok=True)
    outcome_file = os.path.join(temp_chunks_dir, 'outcome.md')
    status = 'Success' if success else 'Failure'
    with open(outcome_file, 'w', encoding='utf-8') as f:
        f.write(f"# Outcome\n\n")
        f.write(f"**Status:** {status}\n\n")
        f.write(f"**File:** {filename}\n")
    print(f"Outcome written to {outcome_file}")

# =========================
# Main Execution
# =========================
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Pull a file from Mockfiles directory.')
    parser.add_argument('filename', help='The filename to pull from Mockfiles')
    args = parser.parse_args()
    success, filename = pull_data_from_mockfiles(args.filename)
    write_outcome_to_md(success, filename)
