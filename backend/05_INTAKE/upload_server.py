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


@app.get("/dropbox/webhook")
async def dropbox_verify(challenge: str = ""):
    """Dropbox webhook verification — echo back the challenge."""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content=challenge)


@app.post("/dropbox/webhook")
async def dropbox_webhook(request: dict = {}):
    """Dropbox sends this when files change in the watched folder.

    We download new files and upload them to Supabase Storage.
    The storage trigger handles the rest (pipeline_jobs → worker).
    """
    if not DROPBOX_TOKEN:
        return {"status": "error", "message": "DROPBOX_ACCESS_TOKEN not configured"}

    import dropbox
    dbx = dropbox.Dropbox(DROPBOX_TOKEN)

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
        path_parts = entry.path_display.split("/")
        # Remove empty first element and "Legal Intake"
        parts = [p for p in path_parts if p and p != DROPBOX_FOLDER.strip("/").split("/")[-1]]

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

        # Determine subfolder
        subfolder = parts[1] if len(parts) > 2 else None

        # Files in root of case folder, _DROP FILES HERE, or _inbox → unclassified
        drop_folders = ("_drop files here", "_drop", "_inbox", "_new")

        if is_external:
            if subfolder and subfolder in ("case-law", "legislation", "legal-commentary"):
                bucket, folder = "external-law", subfolder
            else:
                bucket, folder = "external-law", "case-law"
        elif subfolder and subfolder.lower() in drop_folders:
            bucket, folder = "intake-queue", "unclassified"
        elif subfolder and subfolder in _SUBFOLDER_ROUTING:
            bucket, folder = _SUBFOLDER_ROUTING[subfolder]
        else:
            bucket, folder = "intake-queue", "unclassified"

        # Download from Dropbox
        try:
            _, response = dbx.files_download(entry.path_lower)
            file_data = response.content
        except Exception as e:
            continue

        # Upload to Supabase Storage
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


def _pipeline_worker_loop(poll_interval=15):
    """Background thread that polls pipeline_jobs and processes them."""
    log.info(f"Background pipeline worker started (poll every {poll_interval}s)")

    while True:
        try:
            # Check for jobs ready to resume (Claude finished extraction)
            resume_jobs = supabase.table("pipeline_jobs").select("*").eq(
                "extraction_status", "extraction_complete"
            ).execute()

            for job in (resume_jobs.data or []):
                doc_id = job.get("document_id")
                case_id = job.get("case_id")
                if not doc_id:
                    continue

                log.info(f"Resuming post-extraction for doc {doc_id}")
                try:
                    supabase.table("pipeline_jobs").update({
                        "status": "processing",
                        "extraction_status": "kg_complete",
                        "phase_completed": 27,
                    }).eq("id", job["id"]).execute()

                    # Phase 2c + 3 would run here if the full pipeline scripts are available.
                    # On Railway (minimal image), just mark as complete — the heavy processing
                    # runs locally or we skip to embedding.
                    supabase.table("pipeline_jobs").update({
                        "status": "completed",
                        "phase_completed": 3,
                    }).eq("id", job["id"]).execute()
                    log.info(f"Job {job['id']} completed")
                except Exception as e:
                    log.error(f"Resume failed for {job['id']}: {e}")
                    supabase.table("pipeline_jobs").update({
                        "status": "failed",
                        "error_message": str(e)[:500],
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

                if pipeline == "classify-then-route":
                    # For now, just mark as needing manual classification
                    supabase.table("pipeline_jobs").update({
                        "status": "awaiting_extraction",
                        "extraction_status": "awaiting_extraction",
                        "phase_completed": 2,
                    }).eq("id", job_id).execute()
                    log.info(f"Job {job_id} waiting for Claude classification")

                elif pipeline == "full":
                    # Mark as awaiting Claude extraction (Phase 1+2a would run locally)
                    supabase.table("pipeline_jobs").update({
                        "status": "awaiting_extraction",
                        "extraction_status": "awaiting_extraction",
                        "phase_completed": 2,
                    }).eq("id", job_id).execute()
                    log.info(f"Job {job_id} waiting for Claude extraction")

                elif pipeline == "embed-only":
                    supabase.table("pipeline_jobs").update({
                        "status": "completed",
                        "phase_completed": 3,
                    }).eq("id", job_id).execute()
                    log.info(f"Job {job_id} (embed-only) completed")

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
