"""
Upload Server: Accepts file uploads via HTTP and stores them in Supabase Storage.

Claude Desktop can call this endpoint to upload files that were shared in chat.
Also works as a general-purpose upload API for any client.

Endpoints:
    POST /upload
        - multipart/form-data with fields: file, case_id, bucket, folder
        - Returns: { storage_path, bucket, folder, pipeline_job_id }

    GET /health
        - Returns: { status: "ok" }

Usage:
    python upload_server.py                    # runs on port 8787
    python upload_server.py --port 9000        # custom port
"""

import os
import sys
import uuid
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Legal Pipeline Upload Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

VALID_BUCKETS = {"case-files", "external-law", "reference", "intake-queue"}

VALID_FOLDERS = {
    "case-files": ["pleadings", "contracts", "discovery", "evidence",
                   "correspondence", "court-orders", "administrative"],
    "external-law": ["case-law", "legislation", "legal-commentary"],
    "reference": ["templates", "precedents", "knowledge"],
    "intake-queue": ["unclassified", "bulk"],
}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    case_id: str = Form(...),
    bucket: str = Form(default="intake-queue"),
    folder: str = Form(default="unclassified"),
):
    """Upload a file to Supabase Storage.

    The storage trigger automatically creates a pipeline_jobs entry.
    """
    # Validate
    if bucket not in VALID_BUCKETS:
        raise HTTPException(400, f"Invalid bucket: {bucket}. Must be one of: {VALID_BUCKETS}")

    if folder not in VALID_FOLDERS.get(bucket, []):
        raise HTTPException(400, f"Invalid folder '{folder}' for bucket '{bucket}'. "
                            f"Must be one of: {VALID_FOLDERS[bucket]}")

    # Validate case_id exists
    case = supabase.table("cases").select("id").eq("id", case_id).execute()
    if not case.data:
        raise HTTPException(404, f"Case {case_id} not found")

    # Read file
    content = await file.read()
    file_name = file.filename or f"upload_{uuid.uuid4().hex[:8]}.pdf"

    # Determine content type
    content_type = file.content_type or "application/pdf"
    if content_type not in ALLOWED_MIME_TYPES:
        # Try to infer from extension
        ext = Path(file_name).suffix.lower()
        content_type = {
            ".pdf": "application/pdf",
            ".html": "text/html",
            ".htm": "text/html",
            ".xhtml": "application/xhtml+xml",
            ".txt": "text/plain",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }.get(ext, "application/pdf")

    # Upload to Supabase Storage
    storage_path = f"{case_id}/{folder}/{file_name}"

    try:
        supabase.storage.from_(bucket).upload(
            storage_path, content, {"content-type": content_type}
        )
    except Exception as e:
        err = str(e)
        if "Duplicate" in err or "already exists" in err.lower():
            # File already exists — overwrite by removing first
            supabase.storage.from_(bucket).remove([storage_path])
            supabase.storage.from_(bucket).upload(
                storage_path, content, {"content-type": content_type}
            )
        else:
            raise HTTPException(500, f"Storage upload failed: {err[:200]}")

    # The storage trigger auto-creates a pipeline_jobs row.
    # Give it a moment, then look it up.
    import time
    time.sleep(0.5)

    job = supabase.table("pipeline_jobs").select("id, pipeline, priority, status").eq(
        "file_name", file_name
    ).eq("case_id", case_id).order("created_at", desc=True).limit(1).execute()

    job_id = job.data[0]["id"] if job.data else None

    return {
        "status": "uploaded",
        "bucket": bucket,
        "folder": folder,
        "storage_path": storage_path,
        "file_name": file_name,
        "file_size": len(content),
        "content_type": content_type,
        "pipeline_job_id": job_id,
        "message": f"File uploaded to {bucket}/{storage_path}. "
                   f"Pipeline job created — worker will process it automatically.",
    }


@app.post("/upload/batch")
async def upload_batch(
    files: list[UploadFile] = File(...),
    case_id: str = Form(...),
    bucket: str = Form(default="intake-queue"),
    folder: str = Form(default="unclassified"),
):
    """Upload multiple files at once."""
    results = []
    for file in files:
        try:
            content = await file.read()
            file_name = file.filename or f"upload_{uuid.uuid4().hex[:8]}.pdf"
            content_type = file.content_type or "application/pdf"

            storage_path = f"{case_id}/{folder}/{file_name}"
            supabase.storage.from_(bucket).upload(
                storage_path, content, {"content-type": content_type}
            )
            results.append({"file_name": file_name, "status": "uploaded", "path": storage_path})
        except Exception as e:
            results.append({"file_name": file.filename, "status": "error", "error": str(e)[:100]})

    return {
        "status": "ok",
        "total": len(files),
        "uploaded": sum(1 for r in results if r["status"] == "uploaded"),
        "failed": sum(1 for r in results if r["status"] == "error"),
        "results": results,
    }


# ── Dropbox webhook integration ─────────────────────────────────────────────
# Dropbox sends a GET to verify, then POSTs when files change.
# We download new files and upload them to Supabase Storage.

DROPBOX_FOLDER = os.getenv("DROPBOX_WATCH_FOLDER", "/Legal Intake")
DROPBOX_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN", "")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY", "")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET", "")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN", "")


def _exchange_refresh_token():
    """Manually exchange the refresh token for a fresh access token.

    The Dropbox SDK's built-in refresh has been unreliable on Railway
    (we see 'invalid_access_token' even after the SDK logs 'Refreshing').
    Doing it ourselves via a direct HTTP call is known-good.
    """
    import requests
    # Strip any accidental whitespace/newlines from env vars
    refresh_token = (DROPBOX_REFRESH_TOKEN or "").strip()
    app_key = (DROPBOX_APP_KEY or "").strip()
    app_secret = (DROPBOX_APP_SECRET or "").strip()

    log.info(
        f"Dropbox refresh: key_len={len(app_key)} secret_len={len(app_secret)} "
        f"rt_len={len(refresh_token)} rt_prefix={refresh_token[:8]}"
    )

    resp = requests.post(
        "https://api.dropbox.com/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        auth=(app_key, app_secret),
        timeout=15,
    )
    if resp.status_code != 200:
        log.error(f"Dropbox token exchange failed: {resp.status_code} {resp.text[:400]}")
        raise RuntimeError(
            f"Dropbox token exchange failed ({resp.status_code}): {resp.text[:200]}"
        )
    return resp.json()["access_token"]


def _get_dropbox_client():
    """Create a Dropbox client with a fresh access token.

    Prefers manual refresh-token exchange (auto-renews forever). Falls
    back to raw access token env var if refresh creds aren't set.
    """
    import dropbox
    if DROPBOX_REFRESH_TOKEN and DROPBOX_APP_KEY and DROPBOX_APP_SECRET:
        access_token = _exchange_refresh_token()
        return dropbox.Dropbox(access_token)
    return dropbox.Dropbox(DROPBOX_TOKEN)

# Subfolder name → (bucket, folder)
_SUBFOLDER_ROUTING = {
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

_INGESTABLE_EXT = {".pdf", ".docx", ".doc", ".txt", ".html", ".htm", ".xhtml"}

# Store cursor for incremental sync
_dropbox_cursor = None


@app.post("/case/create-folders")
async def create_case_folders(request: dict):
    """Create the Dropbox folder structure for a new case.

    Called by Claude Desktop (firm project) right after inserting the
    cases row in Supabase. Creates:

        /Legal Intake/{case_name}/_DROP FILES HERE/
        /Legal Intake/{case_name}/{pleadings|contracts|discovery|...}/
        /Legal Intake/_external/{case_name}/{case-law|legislation|legal-commentary}/

    Request body: { "case_name": "Epic vs Apple" }

    The case_name MUST match the cases.case_name in Supabase exactly —
    the webhook routes files by that name.
    """
    case_name = (request.get("case_name") or "").strip()
    if not case_name:
        raise HTTPException(400, "case_name is required")
    if "/" in case_name or "\\" in case_name:
        raise HTTPException(400, "case_name cannot contain slashes")

    if not (DROPBOX_REFRESH_TOKEN or DROPBOX_TOKEN):
        raise HTTPException(500, "Dropbox credentials not configured")

    import dropbox
    from dropbox.exceptions import ApiError
    dbx = _get_dropbox_client()

    base = f"{DROPBOX_FOLDER.rstrip('/')}/{case_name}"
    ext_base = f"{DROPBOX_FOLDER.rstrip('/')}/_external/{case_name}"

    case_subfolders = [
        "_DROP FILES HERE",
        "pleadings", "contracts", "discovery", "evidence",
        "correspondence", "court-orders", "administrative",
    ]
    ext_subfolders = ["case-law", "legislation", "legal-commentary"]

    paths = [f"{base}/{sub}" for sub in case_subfolders] + \
            [f"{ext_base}/{sub}" for sub in ext_subfolders]

    created = []
    already_existed = []
    errors = []

    for path in paths:
        try:
            dbx.files_create_folder_v2(path)
            created.append(path)
        except ApiError as e:
            # path/conflict/folder means it already exists — that's fine
            err_str = str(e)
            if "conflict" in err_str.lower() or "already" in err_str.lower():
                already_existed.append(path)
            else:
                errors.append({"path": path, "error": err_str[:200]})
        except Exception as e:
            errors.append({"path": path, "error": str(e)[:200]})

    return {
        "status": "ok" if not errors else "partial",
        "case_name": case_name,
        "dropbox_case_folder": base,
        "dropbox_external_folder": ext_base,
        "created": created,
        "already_existed": already_existed,
        "errors": errors,
    }


@app.get("/dropbox/webhook")
async def dropbox_verify(challenge: str = ""):
    """Dropbox webhook verification — echo back the challenge."""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content=challenge)


@app.get("/dropbox/debug")
async def dropbox_debug():
    """Debug: list all files Dropbox can see in the watch folder."""
    if not (DROPBOX_REFRESH_TOKEN or DROPBOX_TOKEN):
        return {"error": "no token"}

    dbx = _get_dropbox_client()

    try:
        result = dbx.files_list_folder(DROPBOX_FOLDER, recursive=True)
    except Exception as e:
        return {"error": str(e)[:300]}

    entries = list(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)

    files = []
    for e in entries:
        files.append({
            "name": getattr(e, "name", "?"),
            "path": getattr(e, "path_display", "?"),
            "type": type(e).__name__,
        })

    # Also show parsing
    cases = supabase.table("cases").select("id, case_name").eq("status", "active").execute()
    case_map = {c["case_name"].lower().strip(): c["id"] for c in (cases.data or [])}

    return {
        "watch_folder": DROPBOX_FOLDER,
        "total_entries": len(entries),
        "case_map": case_map,
        "files": files,
    }


@app.post("/dropbox/webhook")
async def dropbox_webhook(request: dict = {}):
    """Dropbox sends this when files change in the watched folder.

    We download new files and upload them to Supabase Storage.
    The storage trigger handles the rest (pipeline_jobs → worker).
    """
    if not (DROPBOX_REFRESH_TOKEN or DROPBOX_TOKEN):
        return {"status": "error", "message": "Dropbox credentials not configured"}

    dbx = _get_dropbox_client()

    # Always do a full folder list — cursor-based sync doesn't persist across
    # Railway deploys/restarts, so we track "already uploaded" via Supabase Storage
    # (duplicate uploads are caught by the storage layer).
    try:
        result = dbx.files_list_folder(DROPBOX_FOLDER, recursive=True)
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}

    entries = list(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)

    # Load case mapping from Supabase
    cases = supabase.table("cases").select("id, case_name").eq("status", "active").execute()
    case_map = {}
    for c in (cases.data or []):
        case_map[c["case_name"].lower().strip()] = c["id"]

    uploaded = []

    for entry in entries:
        if not hasattr(entry, "name"):
            continue
        ext = os.path.splitext(entry.name)[1].lower()
        if ext not in _INGESTABLE_EXT:
            continue

        # Parse path: /Legal Intake/{case_name}/{subfolder}/{file}
        # or: /Legal Intake/{case_name}/{file}
        # or: /Legal Intake/_external/{case_name}/{subfolder}/{file}
        # Strip the watch folder prefix from the path
        rel_path = entry.path_display
        folder_prefix = DROPBOX_FOLDER.rstrip("/")
        if rel_path.lower().startswith(folder_prefix.lower()):
            rel_path = rel_path[len(folder_prefix):]
        parts = [p for p in rel_path.split("/") if p]

        if len(parts) < 2:
            continue

        is_external = parts[0] == "_external"
        if is_external:
            parts = parts[1:]  # remove _external prefix

        case_name = parts[0]
        case_id = case_map.get(case_name.lower().strip())
        if not case_id:
            # Try fuzzy match
            for name, cid in case_map.items():
                if case_name.lower() in name or name in case_name.lower():
                    case_id = cid
                    break
        if not case_id:
            continue

        # Determine subfolder + optional nested subslug (user override)
        # Dropbox path shapes we handle (parts = [case_name, ..., file]):
        #   1. {case_name}/_DROP FILES HERE/{file}             → intake-queue/unclassified (fine routing runs)
        #   2. {case_name}/{parent}/{file}                     → case-files/{parent}     (fine routing picks subslug)
        #   3. {case_name}/{parent}/{subslug}/{file}           → case-files/{parent}/{subslug} (user override; fine routing skipped)
        #   4. {case_name}/{anything else}/{file}              → intake-queue/unclassified
        subfolder = parts[1] if len(parts) >= 3 else None
        # Nested subslug exists only when path is {case_name}/{parent}/{subslug}/{file} (len 4)
        nested_subslug = parts[2] if len(parts) >= 4 else None

        drop_folders = ("_drop files here", "_drop", "_inbox", "_new")

        nested_path = None  # extra path segment under bucket/folder, e.g. "motion-to-dismiss"

        if is_external:
            if subfolder and subfolder in ("case-law", "legislation", "legal-commentary"):
                bucket, folder = "external-law", subfolder
            else:
                bucket, folder = "external-law", "case-law"
        elif subfolder and subfolder.lower() in drop_folders:
            bucket, folder = "intake-queue", "unclassified"
        elif subfolder and subfolder in _SUBFOLDER_ROUTING:
            bucket, folder = _SUBFOLDER_ROUTING[subfolder]
            # If a nested subslug was also given, preserve it (user override wins).
            # Defensive-slugify in case the Dropbox folder name has extra whitespace.
            if nested_subslug:
                from backend.utils.slug import slugify as _slugify
                slug = _slugify(nested_subslug)
                if slug:
                    nested_path = slug
        else:
            bucket, folder = "intake-queue", "unclassified"

        # Download from Dropbox
        try:
            _, response = dbx.files_download(entry.path_lower)
            file_data = response.content
        except Exception as e:
            continue

        # Upload to Supabase Storage — include nested subslug when present
        if nested_path:
            storage_path = f"{case_id}/{folder}/{nested_path}/{entry.name}"
        else:
            storage_path = f"{case_id}/{folder}/{entry.name}"
        mime = {
            ".pdf": "application/pdf", ".html": "text/html", ".txt": "text/plain",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }.get(ext, "application/pdf")

        try:
            supabase.storage.from_(bucket).upload(
                storage_path, file_data, {"content-type": mime}
            )
            uploaded.append({"file": entry.name, "bucket": bucket, "folder": folder})
        except Exception as e:
            if "Duplicate" not in str(e):
                continue

    return {
        "status": "ok",
        "processed": len(entries),
        "uploaded": len(uploaded),
        "files": uploaded,
    }


# ── Built-in pipeline worker (runs as background thread) ────────────────────
# So we only need ONE Railway service instead of two.

import threading
import time
import logging

log = logging.getLogger("pipeline_worker_bg")


PIPELINE_DIR = Path(__file__).resolve().parent.parent
PHASE2_DIR = PIPELINE_DIR / "02_MIDDLE"
PHASE3_MAIN = PIPELINE_DIR / "03_SEARCH" / "main.py"


def _run_script(script_path, *args, timeout=600):
    """Run a pipeline script as subprocess."""
    import subprocess
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, str(script_path)] + list(args)
    log.info(f"Running: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PIPELINE_DIR.parent), env=env, timeout=timeout,
    )
    if result.stdout:
        log.info(f"STDOUT ({script_path.name}): {result.stdout[-800:]}")
    if result.returncode != 0:
        error_detail = result.stderr[-800:] if result.stderr else "(no stderr)"
        log.error(f"FAILED ({script_path.name}): exit={result.returncode} stderr={error_detail}")
        raise RuntimeError(f"{script_path.name} failed: {error_detail}")
    return result.stdout


def _run_bulk_pipeline(job):
    """Run the full pipeline for a Dropbox/bulk-uploaded document.

    Downloads from storage, runs Phase 1 → 2 → 3 (Gemini does extraction).
    """
    job_id = job["id"]
    bucket = job["bucket"]
    file_path = job["file_path"]
    case_id = str(job["case_id"]) if job.get("case_id") else None
    file_name = job["file_name"]

    log.info(f"[{job_id[:8]}] Running full pipeline for {file_name}")

    # Download file from Supabase Storage → place directly in
    # backend/data_storage/documents/ where 01_Intake.py expects it.
    docs_dir = PIPELINE_DIR / "data_storage" / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    data = supabase.storage.from_(bucket).download(file_path)
    local_path = docs_dir / file_name
    with open(local_path, "wb") as f:
        f.write(data)
    log.info(f"[{job_id[:8]}] Downloaded {bucket}/{file_path} → {local_path}")

    # Phase 1: Initial processing
    # main.py expects just the filename, not a full path — 01_Intake.py
    # looks for it in data_storage/documents/.
    supabase.table("pipeline_jobs").update({
        "status": "processing", "phase_completed": 0
    }).eq("id", job_id).execute()

    phase1_main = PIPELINE_DIR / "01_INITIAL" / "main.py"
    phase1_args = [file_name, "--mode", "bulk", "--processing-mode", "balanced"]
    if case_id:
        phase1_args += ["--case-id", case_id]

    try:
        stdout = _run_script(phase1_main, *phase1_args, timeout=600)
    except Exception as e:
        raise RuntimeError(f"Phase 1 failed: {e}")

    # Find document_id — 08_Send_Supabase prints "[08] Document upserted (id=<uuid>)"
    import re
    doc_id = None
    uuid_match = re.search(r"id=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", stdout)
    if uuid_match:
        doc_id = uuid_match.group(1)
    if not doc_id:
        # Fallback: bare UUID on its own line
        for line in stdout.strip().split("\n"):
            line = line.strip()
            if line and len(line) == 36 and line.count("-") == 4:
                doc_id = line
    if not doc_id and case_id:
        docs = supabase.table("documents").select("id").eq(
            "case_id", case_id
        ).eq("file_name", file_name.rsplit(".", 1)[0]).order(
            "created_at", desc=True
        ).limit(1).execute()
        if docs.data:
            doc_id = docs.data[0]["id"]

    supabase.table("pipeline_jobs").update({
        "phase_completed": 1, "document_id": doc_id
    }).eq("id", job_id).execute()
    log.info(f"[{job_id[:8]}] Phase 1 complete. doc_id={doc_id}")

    if not doc_id:
        raise RuntimeError("Could not find document_id after Phase 1")

    # Phase 2: Full (Gemini does extraction in bulk mode)
    phase2_main = PIPELINE_DIR / "02_MIDDLE" / "main.py"
    file_stem = file_name.rsplit(".", 1)[0]
    phase2_args = ["--file_name", file_stem, "--document_id", doc_id, "--mode", "bulk"]

    try:
        _run_script(phase2_main, *phase2_args, timeout=900)
    except Exception as e:
        raise RuntimeError(f"Phase 2 failed: {e}")

    supabase.table("pipeline_jobs").update({
        "phase_completed": 25, "extraction_status": "extraction_complete",
        "extraction_method": "gemini"
    }).eq("id", job_id).execute()
    log.info(f"[{job_id[:8]}] Phase 2 complete (Gemini extraction)")

    # Phase 3 is triggered inside Phase 2's main.py already
    supabase.table("pipeline_jobs").update({
        "phase_completed": 3, "status": "completed"
    }).eq("id", job_id).execute()
    log.info(f"[{job_id[:8]}] Pipeline complete for {file_name}")


def _pipeline_worker_loop(poll_interval=15):
    """Background thread that polls pipeline_jobs and processes them."""
    log.info(f"Background pipeline worker started (poll every {poll_interval}s)")

    while True:
        try:
            # Check for jobs ready to resume (Claude finished extraction)
            resume_jobs = supabase.table("pipeline_jobs").select("*").eq(
                "extraction_status", "extraction_complete"
            ).eq("status", "awaiting_extraction").execute()

            for job in (resume_jobs.data or []):
                doc_id = job.get("document_id")
                case_id = str(job["case_id"]) if job.get("case_id") else None
                if not doc_id:
                    continue

                log.info(f"Resuming post-extraction for doc {doc_id}")
                try:
                    supabase.table("pipeline_jobs").update({
                        "status": "processing"
                    }).eq("id", job["id"]).execute()

                    # 07D: metadata promotion
                    if case_id:
                        try:
                            _run_script(PHASE2_DIR / "07D_case_meta_promotion.py",
                                        "--document_id", doc_id)
                        except Exception:
                            pass

                    # 04A: KG build
                    try:
                        _run_script(PHASE2_DIR / "04A_kg_inner_build.py",
                                    "--document_id", doc_id)
                    except Exception:
                        pass

                    supabase.table("pipeline_jobs").update({
                        "extraction_status": "kg_complete", "phase_completed": 27
                    }).eq("id", job["id"]).execute()

                    # Phase 3: embeddings
                    if case_id:
                        try:
                            _run_script(PHASE3_MAIN, "--case_id", case_id,
                                        "--document_id", doc_id)
                        except Exception:
                            pass

                    supabase.table("pipeline_jobs").update({
                        "status": "completed", "phase_completed": 3
                    }).eq("id", job["id"]).execute()
                    log.info(f"Job {job['id']} resumed and completed")
                except Exception as e:
                    log.error(f"Resume failed for {job['id']}: {e}")
                    supabase.table("pipeline_jobs").update({
                        "status": "failed", "error_message": str(e)[:500],
                    }).eq("id", job["id"]).execute()

            # Check for new pending jobs
            result = supabase.rpc("claim_pipeline_job", {
                "p_pipeline_types": ["full", "embed-only", "classify-then-route"]
            }).execute()

            if result.data:
                job = result.data[0]
                job_id = job["id"]
                pipeline = job["pipeline"]
                log.info(f"Claimed job {job_id}: {job['file_name']} [{pipeline}]")

                try:
                    if pipeline == "full":
                        _run_bulk_pipeline(job)

                    elif pipeline == "classify-then-route":
                        # Classify then re-upload to correct bucket
                        # For now, run as full pipeline on unclassified docs
                        _run_bulk_pipeline(job)

                    elif pipeline == "embed-only":
                        # Lightweight: just embed
                        case_id = str(job["case_id"]) if job.get("case_id") else None
                        if case_id:
                            try:
                                _run_script(PHASE3_MAIN, "--case_id", case_id)
                            except Exception:
                                pass
                        supabase.table("pipeline_jobs").update({
                            "status": "completed", "phase_completed": 3
                        }).eq("id", job_id).execute()
                        log.info(f"Job {job_id} (embed-only) completed")

                except Exception as e:
                    log.error(f"Job {job_id} failed: {e}")
                    supabase.table("pipeline_jobs").update({
                        "status": "failed", "error_message": str(e)[:500],
                    }).eq("id", job_id).execute()

        except Exception as e:
            log.error(f"Worker loop error: {e}")

        time.sleep(poll_interval)


@app.on_event("startup")
def start_background_worker():
    """Start the pipeline worker as a daemon thread when the server starts."""
    worker = threading.Thread(target=_pipeline_worker_loop, daemon=True)
    worker.start()
    log.info("Pipeline worker background thread started")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    print(f"\nUpload server + pipeline worker running at http://{args.host}:{args.port}")
    print(f"POST /upload — upload a file")
    print(f"POST /upload/batch — upload multiple files")
    print(f"GET  /dropbox/webhook — Dropbox verification")
    print(f"POST /dropbox/webhook — Dropbox file sync")
    print(f"GET  /health — health check")
    print(f"Background: pipeline worker polling every 15s\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
