"""
Pipeline Worker: Polls pipeline_jobs table and processes files.

Split pipeline architecture:
  Phase 1: intake → text extraction → classification → sections → Supabase
  Phase 2a: section refine → AST tree → semantic labels → Supabase
  ── PAUSE: awaiting_extraction ──
  Phase 2b (Claude or Gemini): 03A entity extraction + 03B legal structure
  Phase 2c: 07D metadata promotion → 04A KG build
  Phase 3: embeddings

Who does extraction (03A/03B)?
  - User in chat (1-5 files): Claude does it via MCP, better quality, free with Pro
  - Bulk upload (200 docs): Gemini Flash does it here, fast + parallel
  - Off-hours batch: Claude picks up pending jobs via MCP when idle

Usage:
    python -m backend.05_INTAKE.pipeline_worker                   # poll (default)
    python -m backend.05_INTAKE.pipeline_worker --poll 10         # poll every 10s
    python -m backend.05_INTAKE.pipeline_worker --watch           # realtime
    python -m backend.05_INTAKE.pipeline_worker --once            # one job
    python -m backend.05_INTAKE.pipeline_worker --bulk            # bulk mode: Gemini does extraction
    python -m backend.05_INTAKE.pipeline_worker --resume          # resume jobs after extraction
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Add project root to path
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
log = logging.getLogger("pipeline_worker")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Pipeline script paths
PHASE_DIR = PROJECT_ROOT / "backend"
PHASE1_MAIN = PHASE_DIR / "01_INITIAL" / "main.py"
PHASE2_DIR = PHASE_DIR / "02_MIDDLE"
PHASE3_MAIN = PHASE_DIR / "03_SEARCH" / "main.py"


# ── Job management ──────────────────────────────────────────────────────────────

def claim_job():
    """Claim the next pending job from the queue."""
    result = supabase.rpc("claim_pipeline_job", {
        "p_pipeline_types": ["full", "embed-only", "classify-then-route"]
    }).execute()

    if result.data and len(result.data) > 0:
        return result.data[0]
    return None


def update_job(job_id, **fields):
    """Update arbitrary fields on a pipeline job."""
    supabase.table("pipeline_jobs").update(fields).eq("id", job_id).execute()


def complete_job(job_id, status="completed", error=None,
                 routed_bucket=None, routed_folder=None, doc_type=None):
    """Mark a job as completed or failed."""
    supabase.rpc("complete_pipeline_job", {
        "p_job_id": job_id,
        "p_status": status,
        "p_error": error,
        "p_routed_bucket": routed_bucket,
        "p_routed_folder": routed_folder,
        "p_doc_type": doc_type,
    }).execute()


# ── File operations ─────────────────────────────────────────────────────────────

def download_file(bucket, file_path):
    """Download a file from Supabase Storage to a temp directory."""
    file_name = file_path.split("/")[-1]
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_")
    local_path = os.path.join(tmp_dir, file_name)

    data = supabase.storage.from_(bucket).download(file_path)
    with open(local_path, "wb") as f:
        f.write(data)

    log.info(f"Downloaded {bucket}/{file_path} -> {local_path}")
    return local_path


def classify_document(local_path, case_id=None):
    """Classify document type and determine target bucket/folder."""
    import google.generativeai as genai
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

    file_name = os.path.basename(local_path)
    model = genai.GenerativeModel("gemini-2.0-flash")
    prompt = f"""Classify this legal document based on its filename and determine where it should be stored.

Filename: {file_name}

Respond with JSON only:
{{
    "document_type": "<type from: Pleading - Complaint, Pleading - Answer, Pleading - Motion, Brief, Contract - Agreement, Contract - Amendment, Contract - License, Discovery - Interrogatory, Discovery - Deposition, Evidence - Exhibit, Evidence - Declaration, Correspondence - Letter, Court Order, Administrative - Case Summary, Case Law, Legislation, Legal Commentary, Unknown>",
    "is_external": <true if this is external case law/legislation/commentary, false if it's a case document>,
    "confidence": <0.0 to 1.0>
}}"""

    response = model.generate_content(prompt)
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    result = json.loads(text)
    doc_type = result["document_type"]
    is_external = result.get("is_external", False)

    if is_external:
        bucket = "external-law"
        if "case law" in doc_type.lower():
            folder = "case-law"
        elif "legislation" in doc_type.lower() or "statute" in doc_type.lower():
            folder = "legislation"
        else:
            folder = "legal-commentary"
    else:
        bucket = "case-files"
        criteria = supabase.table("bucket_routing_criteria").select(
            "folder,document_types"
        ).eq("bucket", "case-files").execute()

        folder = "administrative"
        for row in criteria.data:
            if doc_type in (row.get("document_types") or []):
                folder = row["folder"]
                break

    return doc_type, bucket, folder, result.get("confidence", 0.5)


def move_file(source_bucket, source_path, dest_bucket, dest_folder, case_id):
    """Move a file from one bucket/path to another."""
    file_name = source_path.split("/")[-1]
    dest_path = f"{case_id}/{dest_folder}/{file_name}"

    data = supabase.storage.from_(source_bucket).download(source_path)
    supabase.storage.from_(dest_bucket).upload(dest_path, data)
    supabase.storage.from_(source_bucket).remove([source_path])

    log.info(f"Moved {source_bucket}/{source_path} -> {dest_bucket}/{dest_path}")
    return dest_path


def _run_script(script_path, *args, timeout=600):
    """Run a Python script as subprocess, raise on failure."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, str(script_path)] + list(args),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PROJECT_ROOT), env=env, timeout=timeout,
    )
    if result.stdout.strip():
        log.info(result.stdout.strip()[-500:])
    if result.returncode != 0:
        raise RuntimeError(f"{script_path.name} failed: {result.stderr[-500:]}")
    return result.stdout


def _find_document_id(file_name, case_id, stdout=""):
    """Extract or look up document_id after Phase 1."""
    # Try parsing from stdout (Phase 1 prints UUID on last line)
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if line and len(line) == 36 and "-" in line:
            return line

    # Fallback: query database
    docs = supabase.table("documents").select("id").eq(
        "case_id", str(case_id)
    ).eq("file_name", file_name).order("created_at", desc=True).limit(1).execute()
    if docs.data:
        return docs.data[0]["id"]

    raise RuntimeError(f"Could not find document_id for {file_name}")


# ── Pipeline stages ─────────────────────────────────────────────────────────────

def run_phase1(local_path, case_id):
    """Phase 1: Document intake, text extraction, classification, sections → Supabase."""
    file_name = os.path.basename(local_path)
    log.info(f"Phase 1: Intake for {file_name}")

    stdout = _run_script(
        PHASE1_MAIN,
        "--file", local_path,
        "--case-id", str(case_id),
        "--mode", "bulk",
        "--processing-mode", "balanced",
        timeout=600,
    )
    doc_id = _find_document_id(file_name, case_id, stdout)
    log.info(f"Phase 1 complete. document_id={doc_id}")
    return doc_id


def run_phase2a(document_id, case_id):
    """Phase 2a: Section refine → AST tree → Semantic labels.

    Stops BEFORE entity extraction (03A/03B).
    """
    log.info(f"Phase 2a: Structure analysis for doc {document_id}")

    # 07C: Case-context re-classification
    if case_id:
        _run_script(PHASE2_DIR / "07C_case_context_classification.py",
                     "--document_id", document_id)

    # 00: Section refinement
    _run_script(PHASE2_DIR / "00_section_refine.py",
                "--document_id", document_id)

    # 01: AST tree build
    _run_script(PHASE2_DIR / "01_AST_tree_build.py",
                "--document_id", document_id)

    # 02: Semantic labeling
    _run_script(PHASE2_DIR / "02_AST_semantic_label.py",
                "--document_id", document_id)

    log.info(f"Phase 2a complete. Sections structured and labeled.")


def run_phase2b_gemini(document_id):
    """Phase 2b: Entity extraction + legal structure via Gemini Flash (bulk mode)."""
    log.info(f"Phase 2b (Gemini): Extraction for doc {document_id}")

    from concurrent.futures import ThreadPoolExecutor

    def _run_3a():
        _run_script(PHASE2_DIR / "03A_entity_extraction.py",
                     "--document_id", document_id)

    def _run_3b():
        _run_script(PHASE2_DIR / "03B_legal_structure_extraction.py",
                     "--document_id", document_id)

    # Check if 03B should run
    doc = supabase.table("documents").select(
        "document_type, filing_purpose"
    ).eq("id", document_id).single().execute()
    doc_data = doc.data or {}
    doc_type = (doc_data.get("document_type") or "").lower()
    filing_purpose = (doc_data.get("filing_purpose") or "").lower()

    run_3b = (
        any(doc_type.startswith(p) for p in
            ("complaint", "brief", "motion", "appeal", "answer", "counterclaim", "pleading"))
        or filing_purpose in ("operative_pleading", "motion", "brief")
    )

    with ThreadPoolExecutor(max_workers=2) as ex:
        f3a = ex.submit(_run_3a)
        f3b = ex.submit(_run_3b) if run_3b else None
        f3a.result()
        if f3b:
            f3b.result()

    log.info(f"Phase 2b (Gemini) complete. Entities extracted.")


def run_phase2c(document_id, case_id):
    """Phase 2c: Post-extraction — metadata promotion + KG build."""
    log.info(f"Phase 2c: KG build for doc {document_id}")

    # 07D: Promote entities to case metadata
    if case_id:
        _run_script(PHASE2_DIR / "07D_case_meta_promotion.py",
                     "--document_id", document_id)

    # 04A: Knowledge graph (intra-document)
    _run_script(PHASE2_DIR / "04A_kg_inner_build.py",
                "--document_id", document_id)

    log.info(f"Phase 2c complete. KG built.")


def run_phase3(case_id, document_id=None):
    """Phase 3: Embed sections for search."""
    log.info(f"Phase 3: Embeddings for case {case_id}")

    args = ["--case_id", str(case_id)]
    if document_id:
        args += ["--document_id", document_id]

    _run_script(PHASE3_MAIN, *args, timeout=600)
    log.info(f"Phase 3 complete. Search-ready.")


# ── Pipeline modes ──────────────────────────────────────────────────────────────

def run_full_pipeline_split(local_path, case_id, job, bulk=False):
    """Full pipeline with extraction split.

    If bulk=True: Gemini does 03A/03B inline (fast, cheap, parallel).
    If bulk=False: Pauses after Phase 2a, sets status to 'awaiting_extraction'
                   for Claude to pick up via MCP.
    """
    job_id = job["id"]
    file_name = os.path.basename(local_path)

    # Phase 1
    doc_id = run_phase1(local_path, case_id)
    update_job(job_id, document_id=doc_id, phase_completed=1)

    # Phase 2a: structure + labels
    run_phase2a(doc_id, case_id)
    update_job(job_id, phase_completed=2)

    if bulk:
        # Bulk mode: Gemini does extraction, no pause
        run_phase2b_gemini(doc_id)
        update_job(job_id, phase_completed=25, extraction_status="extraction_complete",
                   extraction_method="gemini")

        # Phase 2c: KG build
        run_phase2c(doc_id, case_id)
        update_job(job_id, phase_completed=27, extraction_status="kg_complete")

        # Phase 3: embeddings
        run_phase3(case_id, doc_id)
        update_job(job_id, phase_completed=3)

        complete_job(job_id, status="completed")
        log.info(f"Full pipeline (bulk) completed for {file_name}")

    else:
        # Chat mode: pause here for Claude to do extraction via MCP
        update_job(job_id,
                   extraction_status="awaiting_extraction",
                   status="awaiting_extraction")
        log.info(f"Phase 2a done for {file_name}. "
                 f"Awaiting Claude extraction (doc_id={doc_id})")


def resume_after_extraction(job):
    """Resume pipeline after Claude (or Gemini batch) has done 03A/03B.

    Called when extraction_status changes to 'extraction_complete'.
    """
    job_id = job["id"]
    doc_id = job.get("document_id")
    case_id = job.get("case_id")

    if not doc_id:
        raise RuntimeError("No document_id on job — cannot resume")

    log.info(f"Resuming post-extraction for doc {doc_id}")

    # Phase 2c: KG build (uses the extractions Claude wrote)
    run_phase2c(doc_id, str(case_id) if case_id else None)
    update_job(job_id, phase_completed=27, extraction_status="kg_complete")

    # Phase 3: embeddings
    if case_id:
        run_phase3(str(case_id), doc_id)
        update_job(job_id, phase_completed=3)

    complete_job(job_id, status="completed")
    log.info(f"Pipeline resumed and completed for doc {doc_id}")


def run_embed_only(local_path, case_id, firm_id, job):
    """Embed-only pipeline for external/reference docs."""
    file_name = os.path.basename(local_path)
    log.info(f"Running EMBED-ONLY pipeline for {file_name}")

    doc_data = {
        "file_name": file_name,
        "document_type": job.get("classified_document_type", "External Reference"),
        "case_id": str(case_id) if case_id else None,
        "is_primary_filing": False,
    }
    doc_result = supabase.table("documents").insert(doc_data).execute()
    doc_id = doc_result.data[0]["id"]
    update_job(job["id"], document_id=doc_id)

    if case_id:
        run_phase3(str(case_id), doc_id)

    complete_job(job["id"], status="completed")
    log.info(f"Embed-only completed for {file_name}")


# ── Job processing ──────────────────────────────────────────────────────────────

def process_job(job, bulk=False):
    """Process a single pipeline job."""
    job_id = job["id"]
    bucket = job["bucket"]
    file_path = job["file_path"]
    pipeline = job["pipeline"]
    case_id = job.get("case_id")
    firm_id = job.get("firm_id")

    log.info(f"Processing job {job_id}: {bucket}/{file_path} [{pipeline}]"
             + (" (BULK)" if bulk else ""))

    try:
        if pipeline == "classify-then-route":
            local_path = download_file(bucket, file_path)
            doc_type, dest_bucket, dest_folder, confidence = classify_document(
                local_path, case_id
            )
            log.info(f"Classified: {doc_type} -> {dest_bucket}/{dest_folder} ({confidence})")

            if case_id:
                move_file(bucket, file_path, dest_bucket, dest_folder, case_id)

            complete_job(job_id, status="completed",
                         routed_bucket=dest_bucket, routed_folder=dest_folder,
                         doc_type=doc_type)

        elif pipeline == "full":
            local_path = download_file(bucket, file_path)
            run_full_pipeline_split(local_path, case_id, job, bulk=bulk)

        elif pipeline == "embed-only":
            local_path = download_file(bucket, file_path)
            run_embed_only(local_path, case_id, firm_id, job)

        else:
            raise ValueError(f"Unknown pipeline type: {pipeline}")

    except Exception as e:
        log.error(f"Job {job_id} failed: {e}")
        complete_job(job_id, status="failed", error=str(e)[:500])


def process_resume_jobs():
    """Find and resume jobs where extraction is complete."""
    jobs = supabase.table("pipeline_jobs").select("*").eq(
        "extraction_status", "extraction_complete"
    ).execute()

    if not jobs.data:
        log.debug("No jobs ready to resume")
        return False

    for job in jobs.data:
        try:
            update_job(job["id"], status="processing")
            resume_after_extraction(job)
        except Exception as e:
            log.error(f"Resume failed for job {job['id']}: {e}")
            complete_job(job["id"], status="failed", error=str(e)[:500])

    return True


# ── Entry points ────────────────────────────────────────────────────────────────

def run_once(bulk=False):
    """Claim and process one job."""
    job = claim_job()
    if job:
        process_job(job, bulk=bulk)
        return True
    else:
        log.debug("No pending jobs")
        return False


def run_poll(interval=10, bulk=False):
    """Poll for jobs at a fixed interval."""
    mode = "BULK (Gemini extraction)" if bulk else "CHAT (pause for Claude)"
    log.info(f"Polling for pipeline jobs every {interval}s [{mode}]... (Ctrl+C to stop)")
    while True:
        try:
            # First check for jobs ready to resume (extraction_complete)
            process_resume_jobs()

            # Then check for new pending jobs
            had_job = True
            while had_job:
                had_job = run_once(bulk=bulk)
        except KeyboardInterrupt:
            log.info("Shutting down...")
            break
        except Exception as e:
            log.error(f"Worker error: {e}")

        time.sleep(interval)


def run_watch(bulk=False):
    """Use Supabase Realtime to watch for new jobs."""
    mode = "BULK" if bulk else "CHAT"
    log.info(f"Watching for pipeline jobs via Realtime [{mode}]... (Ctrl+C to stop)")

    def on_change(payload):
        # Check for resume jobs first
        process_resume_jobs()
        # Then new jobs
        run_once(bulk=bulk)

    channel = supabase.channel("pipeline_jobs")
    channel.on_postgres_changes(
        "INSERT",
        schema="public",
        table="pipeline_jobs",
        callback=on_change,
    )
    # Also watch for extraction_status updates (Claude finished)
    channel.on_postgres_changes(
        "UPDATE",
        schema="public",
        table="pipeline_jobs",
        callback=on_change,
    )
    channel.subscribe()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        channel.unsubscribe()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pipeline worker")
    parser.add_argument("--poll", type=int, metavar="SECONDS",
                        help="Poll interval in seconds")
    parser.add_argument("--watch", action="store_true",
                        help="Use Supabase Realtime instead of polling")
    parser.add_argument("--once", action="store_true",
                        help="Process one job and exit")
    parser.add_argument("--bulk", action="store_true",
                        help="Bulk mode: Gemini does extraction (no pause for Claude)")
    parser.add_argument("--resume", action="store_true",
                        help="Only resume jobs where extraction is complete")
    args = parser.parse_args()

    if args.resume:
        process_resume_jobs()
    elif args.watch:
        run_watch(bulk=args.bulk)
    elif args.poll:
        run_poll(args.poll, bulk=args.bulk)
    elif args.once:
        run_once(bulk=args.bulk)
    else:
        run_poll(10, bulk=args.bulk)
