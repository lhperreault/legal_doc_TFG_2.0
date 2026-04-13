"""
Folder Watcher: Monitors a local/Dropbox-synced folder and uploads to Supabase Storage.

Folder structure:
  {watch_dir}/
    _inbox/                        → classify case + doc type, then route
    _external/{case_name}/         → external-law bucket (embed only)
    {case_name}/                   → intake-queue or case-files (full pipeline)
    {case_name}/pleadings/         → case-files/{case_id}/pleadings/
    {case_name}/contracts/         → case-files/{case_id}/contracts/
    {case_name}/...                → (any known subfolder routes directly)

Case names are mapped to case_ids via Supabase on startup.
New case folders are detected and the user is prompted (or auto-matched).

Usage:
    python folder_watcher.py                                      # watch ./watch
    python folder_watcher.py --watch-dir "D:/Dropbox/Legal Intake"
    python folder_watcher.py --upload file.pdf --case-id <uuid>
    python folder_watcher.py --upload-dir ./docs --case-id <uuid>
    python folder_watcher.py --setup --case-id <uuid>             # create local folder structure
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from supabase import create_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("folder_watcher")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

INGESTABLE_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".html", ".htm", ".xhtml"}

# Subfolder → (bucket, folder)
SUBFOLDER_ROUTING = {
    "pleadings": ("case-files", "pleadings"),
    "contracts": ("case-files", "contracts"),
    "discovery": ("case-files", "discovery"),
    "evidence": ("case-files", "evidence"),
    "correspondence": ("case-files", "correspondence"),
    "court-orders": ("case-files", "court-orders"),
    "administrative": ("case-files", "administrative"),
    "case-law": ("external-law", "case-law"),
    "legislation": ("external-law", "legislation"),
    "legal-commentary": ("external-law", "legal-commentary"),
}

# Runtime state
_case_name_to_id = {}   # "Epic vs Apple" → "a724c9ac-..."
_uploaded_hashes = set()
_mapping_file = None     # path to case_mapping.json


# ── Case mapping ────────────────────────────────────────────────────────────

def load_case_mapping(watch_dir):
    """Load or build the case name → case_id mapping."""
    global _case_name_to_id, _mapping_file
    _mapping_file = Path(watch_dir) / ".case_mapping.json"

    # Load saved mapping
    if _mapping_file.exists():
        with open(_mapping_file) as f:
            _case_name_to_id = json.load(f)
        log.info(f"Loaded {len(_case_name_to_id)} case mappings from .case_mapping.json")

    # Refresh from Supabase
    refresh_case_mapping()


def refresh_case_mapping():
    """Pull all cases from Supabase and match to folder names."""
    resp = supabase.table("cases").select("id, case_name, status").eq("status", "active").execute()
    db_cases = {c["case_name"]: c["id"] for c in (resp.data or [])}

    # Merge (don't overwrite manual mappings)
    for name, cid in db_cases.items():
        # Exact match
        if name not in _case_name_to_id:
            _case_name_to_id[name] = cid
        # Also store normalized version (lowercase, no special chars)
        normalized = _normalize_name(name)
        if normalized not in _case_name_to_id:
            _case_name_to_id[normalized] = cid

    _save_mapping()
    log.info(f"Case mapping: {len(_case_name_to_id)} entries "
             f"({len(db_cases)} from Supabase)")


def _normalize_name(name):
    """Normalize a case name for fuzzy folder matching."""
    return name.lower().strip().replace("  ", " ")


def resolve_case_id(folder_name):
    """Resolve a folder name to a case_id. Returns None if not found."""
    # Exact match
    if folder_name in _case_name_to_id:
        return _case_name_to_id[folder_name]

    # Normalized match
    normalized = _normalize_name(folder_name)
    if normalized in _case_name_to_id:
        return _case_name_to_id[normalized]

    # Fuzzy: check if folder name is contained in any case name
    for case_name, case_id in _case_name_to_id.items():
        if normalized in _normalize_name(case_name) or _normalize_name(case_name) in normalized:
            return case_id

    return None


def register_case_folder(folder_name, case_id):
    """Manually register a folder name → case_id mapping."""
    _case_name_to_id[folder_name] = case_id
    _case_name_to_id[_normalize_name(folder_name)] = case_id
    _save_mapping()
    log.info(f"Registered: '{folder_name}' → {case_id}")


def _save_mapping():
    if _mapping_file:
        with open(_mapping_file, "w") as f:
            json.dump(_case_name_to_id, f, indent=2)


# ── File operations ─────────────────────────────────────────────────────────

def _hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_mime(path):
    ext = Path(path).suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".txt": "text/plain",
        ".html": "text/html",
        ".htm": "text/html",
        ".xhtml": "application/xhtml+xml",
    }.get(ext, "application/octet-stream")


def upload_to_storage(local_path, bucket, storage_path):
    """Upload a file to Supabase Storage."""
    mime = _get_mime(local_path)
    with open(local_path, "rb") as f:
        data = f.read()

    try:
        supabase.storage.from_(bucket).upload(storage_path, data, {"content-type": mime})
        log.info(f"Uploaded: {bucket}/{storage_path}")
        return True
    except Exception as e:
        err = str(e)
        if "Duplicate" in err or "already exists" in err.lower():
            log.debug(f"Already exists: {bucket}/{storage_path}")
            return True
        log.error(f"Upload failed: {err[:150]}")
        return False


# ── Inbox classifier ────────────────────────────────────────────────────────

def classify_inbox_file(local_path):
    """Classify a file from _inbox: determine which case and document type.

    Uses Gemini Flash for cheap classification based on filename + first page.
    Returns (case_id, bucket, folder) or (None, None, None) if can't determine.
    """
    import google.generativeai as genai
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

    file_name = os.path.basename(local_path)

    # Get list of active cases for context
    cases = supabase.table("cases").select(
        "id, case_name, our_client, opposing_party"
    ).eq("status", "active").execute()
    case_list = "\n".join(
        f"- {c['case_name']} (id: {c['id']}, client: {c.get('our_client', '?')}, "
        f"opposing: {c.get('opposing_party', '?')})"
        for c in (cases.data or [])
    )

    model = genai.GenerativeModel("gemini-2.0-flash")
    prompt = f"""You are a legal document classifier. Given a filename and the list of active cases, determine:
1. Which case this document belongs to
2. What type of document it is

Filename: {file_name}

Active cases:
{case_list}

Respond with JSON only:
{{
    "case_id": "<uuid of the matching case, or null if you can't determine>",
    "case_name": "<name of the matching case>",
    "document_type": "<one of: Pleading - Complaint, Pleading - Answer, Pleading - Motion, Brief, Contract - Agreement, Contract - Amendment, Contract - License, Discovery - Interrogatory, Discovery - Deposition, Evidence - Exhibit, Evidence - Declaration, Correspondence - Letter, Court Order, Administrative - Case Summary, Case Law, Legislation, Legal Commentary, Unknown>",
    "is_external": <true/false>,
    "confidence": <0.0 to 1.0>,
    "reasoning": "<brief explanation>"
}}"""

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
    except Exception as e:
        log.error(f"Classification failed for {file_name}: {e}")
        return None, None, None

    case_id = result.get("case_id")
    doc_type = result.get("document_type", "Unknown")
    is_external = result.get("is_external", False)
    confidence = result.get("confidence", 0)

    if not case_id or confidence < 0.5:
        log.warning(f"Low confidence ({confidence}) classifying {file_name}: "
                    f"{result.get('reasoning', 'unknown')}")
        return None, None, None

    # Map doc type to bucket/folder
    if is_external:
        bucket = "external-law"
        if "case law" in doc_type.lower():
            folder = "case-law"
        elif "legislation" in doc_type.lower():
            folder = "legislation"
        else:
            folder = "legal-commentary"
    else:
        bucket = "case-files"
        # Look up routing
        criteria = supabase.table("bucket_routing_criteria").select(
            "folder, document_types"
        ).eq("bucket", "case-files").execute()

        folder = "administrative"
        for row in (criteria.data or []):
            if doc_type in (row.get("document_types") or []):
                folder = row["folder"]
                break

    log.info(f"Classified {file_name}: case={result.get('case_name')}, "
             f"type={doc_type}, bucket={bucket}/{folder} (conf={confidence})")
    return case_id, bucket, folder


# ── Main processing ─────────────────────────────────────────────────────────

def process_file(file_path, case_id, subfolder=None, is_external=False):
    """Process a single file: upload to correct bucket in Supabase Storage."""
    file_name = os.path.basename(file_path)
    file_hash = _hash_file(file_path)

    if file_hash in _uploaded_hashes:
        return False

    ext = Path(file_path).suffix.lower()
    if ext not in INGESTABLE_EXTENSIONS:
        return False

    # Determine bucket and folder
    if subfolder and subfolder in SUBFOLDER_ROUTING:
        bucket, folder = SUBFOLDER_ROUTING[subfolder]
    elif is_external:
        bucket, folder = "external-law", "case-law"
    else:
        bucket, folder = "intake-queue", "unclassified"

    storage_path = f"{case_id}/{folder}/{file_name}"
    success = upload_to_storage(file_path, bucket, storage_path)

    if success:
        _uploaded_hashes.add(file_hash)
    return success


def watch_folder(watch_dir, poll_interval=5):
    """Watch a folder structure for new files."""
    watch_dir = Path(watch_dir)
    watch_dir.mkdir(parents=True, exist_ok=True)

    # Create standard subfolders
    (watch_dir / "_inbox").mkdir(exist_ok=True)
    (watch_dir / "_external").mkdir(exist_ok=True)
    (watch_dir / "_processed").mkdir(exist_ok=True)

    load_case_mapping(str(watch_dir))

    log.info(f"Watching: {watch_dir}")
    log.info(f"Structure:")
    log.info(f"  _inbox/           → auto-classify case + type")
    log.info(f"  _external/{{case}}/ → external law (embed only)")
    log.info(f"  {{case name}}/      → auto-classify type")
    log.info(f"  {{case name}}/pleadings/  → direct to pleadings")
    log.info(f"Known cases: {list(set(v for v in _case_name_to_id.values()))}")

    seen = set()
    refresh_counter = 0

    while True:
        try:
            # Refresh case mapping every 60 cycles
            refresh_counter += 1
            if refresh_counter >= 60:
                refresh_case_mapping()
                refresh_counter = 0

            for file_path in sorted(watch_dir.rglob("*")):
                if not file_path.is_file():
                    continue
                if file_path.name.startswith("."):
                    continue
                if str(file_path) in seen:
                    continue
                if "_processed" in str(file_path):
                    continue

                ext = file_path.suffix.lower()
                if ext not in INGESTABLE_EXTENSIONS:
                    continue

                relative = file_path.relative_to(watch_dir)
                parts = relative.parts

                if len(parts) < 2:
                    continue  # files in root are ignored

                top_folder = parts[0]

                # ── _inbox: classify everything ──
                if top_folder == "_inbox":
                    case_id, bucket, folder = classify_inbox_file(str(file_path))
                    if case_id:
                        storage_path = f"{case_id}/{folder}/{file_path.name}"
                        if upload_to_storage(str(file_path), bucket, storage_path):
                            _move_to_processed(file_path, watch_dir)
                            seen.add(str(file_path))
                    else:
                        log.warning(f"Could not classify: {file_path.name} — leaving in _inbox")
                        seen.add(str(file_path))  # don't retry every cycle
                    continue

                # ── _external/{case_name}/file.pdf ──
                if top_folder == "_external":
                    if len(parts) < 3:
                        continue
                    case_folder = parts[1]
                    case_id = resolve_case_id(case_folder)
                    if not case_id:
                        log.warning(f"Unknown case folder: _external/{case_folder}")
                        seen.add(str(file_path))
                        continue

                    subfolder = parts[2] if len(parts) > 3 else "case-law"
                    if subfolder in ("case-law", "legislation", "legal-commentary"):
                        bucket = "external-law"
                        folder = subfolder
                    else:
                        bucket = "external-law"
                        folder = "case-law"

                    storage_path = f"{case_id}/{folder}/{file_path.name}"
                    if upload_to_storage(str(file_path), bucket, storage_path):
                        _move_to_processed(file_path, watch_dir)
                        seen.add(str(file_path))
                    continue

                # ── {case_name}/[subfolder]/file.pdf ──
                case_folder = top_folder
                case_id = resolve_case_id(case_folder)
                if not case_id:
                    log.warning(f"Unknown case folder: {case_folder} — "
                                f"add it with: --register '{case_folder}' <case_id>")
                    seen.add(str(file_path))
                    continue

                # Check for subfolder
                subfolder = parts[1] if len(parts) > 2 else None
                drop_folders = ("_drop files here", "_drop", "_inbox", "_new")

                if subfolder and subfolder.lower() in drop_folders:
                    bucket, folder = "intake-queue", "unclassified"
                elif subfolder and subfolder in SUBFOLDER_ROUTING:
                    bucket, folder = SUBFOLDER_ROUTING[subfolder]
                else:
                    # No subfolder or unknown → unclassified
                    bucket, folder = "intake-queue", "unclassified"

                storage_path = f"{case_id}/{folder}/{file_path.name}"
                if upload_to_storage(str(file_path), bucket, storage_path):
                    _move_to_processed(file_path, watch_dir)
                    seen.add(str(file_path))

        except KeyboardInterrupt:
            log.info("Shutting down...")
            break
        except Exception as e:
            log.error(f"Watch error: {e}")

        time.sleep(poll_interval)


def _move_to_processed(file_path, watch_dir):
    """Move a processed file to _processed/ to keep the watch folder clean."""
    processed_dir = Path(watch_dir) / "_processed"
    dest = processed_dir / file_path.relative_to(watch_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        file_path.rename(dest)
        log.debug(f"Moved to _processed: {file_path.name}")
    except Exception:
        pass  # file might be locked, that's ok


def setup_case_folder(watch_dir, case_id):
    """Create the local folder structure for a case."""
    resp = supabase.table("cases").select("case_name").eq("id", case_id).single().execute()
    if not resp.data:
        log.error(f"Case {case_id} not found")
        return

    case_name = resp.data["case_name"]
    case_dir = Path(watch_dir) / case_name

    # Main drop folder — this is where users dump files
    (case_dir / "_DROP FILES HERE").mkdir(parents=True, exist_ok=True)

    # Organized subfolders for users who know the doc type
    folders = list(SUBFOLDER_ROUTING.keys())
    for folder in folders:
        (case_dir / folder).mkdir(parents=True, exist_ok=True)

    # Also create external folder
    ext_dir = Path(watch_dir) / "_external" / case_name
    for folder in ("case-law", "legislation", "legal-commentary"):
        (ext_dir / folder).mkdir(parents=True, exist_ok=True)

    # Register mapping
    load_case_mapping(str(watch_dir))
    register_case_folder(case_name, case_id)

    log.info(f"Created folder structure for '{case_name}' at {case_dir}")
    log.info(f"Subfolders: {', '.join(folders)}")


# ── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Watch folders and upload to Supabase Storage")
    parser.add_argument("--watch-dir", help="Folder to watch")
    parser.add_argument("--poll", type=int, default=5, help="Poll interval in seconds")
    parser.add_argument("--upload", help="Upload a single file")
    parser.add_argument("--upload-dir", help="Upload all files in a directory")
    parser.add_argument("--case-id", help="Case UUID")
    parser.add_argument("--subfolder", help="Target subfolder (pleadings, contracts, etc.)")
    parser.add_argument("--setup", action="store_true", help="Create local folder structure for a case")
    parser.add_argument("--register", nargs=2, metavar=("FOLDER_NAME", "CASE_ID"),
                        help="Register a folder name → case_id mapping")
    args = parser.parse_args()

    if args.register:
        folder_name, case_id = args.register
        watch_dir = args.watch_dir or str(PROJECT_ROOT / "watch")
        load_case_mapping(watch_dir)
        register_case_folder(folder_name, case_id)

    elif args.setup:
        if not args.case_id:
            print("ERROR: --case-id required with --setup", file=sys.stderr)
            sys.exit(1)
        watch_dir = args.watch_dir or str(PROJECT_ROOT / "watch")
        setup_case_folder(watch_dir, args.case_id)

    elif args.upload:
        if not args.case_id:
            print("ERROR: --case-id required", file=sys.stderr)
            sys.exit(1)
        process_file(args.upload, args.case_id, args.subfolder)

    elif args.upload_dir:
        if not args.case_id:
            print("ERROR: --case-id required", file=sys.stderr)
            sys.exit(1)
        d = Path(args.upload_dir)
        count = 0
        for f in sorted(d.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                sub = f.relative_to(d).parts[0] if len(f.relative_to(d).parts) > 1 else None
                if process_file(str(f), args.case_id, sub):
                    count += 1
        log.info(f"Uploaded {count} files")

    else:
        watch_dir = args.watch_dir or str(PROJECT_ROOT / "watch")
        watch_folder(watch_dir, args.poll)
