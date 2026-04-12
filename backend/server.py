"""
backend/server.py — FastAPI HTTP server for the frontend.

Exposes:
  POST /api/ingest   — receives a file upload + case_id, saves the file,
                       then runs the pipeline (Phase 1 → 3 → 4) and
                       streams Server-Sent Events (SSE) as each phase completes.
                       Phase 2 (02_MIDDLE) runs silently in the background
                       after Phase 1 without blocking the SSE stream.

SSE event format:
  data: {"phase": 1, "status": "start"|"done"|"error", "label": "..."}
  data: {"phase": 2, "status": "start"|"done"|"error", "label": "..."}
  data: {"phase": 3, "status": "start"|"done"|"error", "label": "..."}
  data: {"done": true, "document_id": "...", "case_id": "..."}

Usage:
    python backend/server.py
    # or
    uvicorn backend.server:app --reload --port 8000
"""
import asyncio
import json
import os
import subprocess
import sys
import uuid

# Load .env from the project root before any module-level API key reads
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Legal Pipeline API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR    = os.path.join(BACKEND_DIR, "data_storage", "documents")
PHASE1_MAIN     = os.path.join(BACKEND_DIR, "01_INITIAL",              "main.py")
PHASE2_MAIN     = os.path.join(BACKEND_DIR, "02_MIDDLE",               "main.py")
PHASE3_MAIN     = os.path.join(BACKEND_DIR, "03_SEARCH",               "main.py")
PHASE4_SUMMARY  = os.path.join(BACKEND_DIR, "04_AGENTIC_ARCHITECTURE", "document_summary.py")


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


async def _run(args: list[str]) -> int:
    """Run a subprocess without blocking the event loop."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: subprocess.run(args, capture_output=False),
    )
    return result.returncode


LOGS_DIR = os.path.join(BACKEND_DIR, "data_storage", "logs")


def _run_background(args: list[str], log_name: str = "bg") -> None:
    """Fire-and-forget subprocess — stdout/stderr written to data_storage/logs/."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_path = os.path.join(LOGS_DIR, f"{log_name}.log")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    with open(log_path, "a", encoding="utf-8") as lf:
        subprocess.Popen(args, stdout=lf, stderr=lf, env=env)


# ---------------------------------------------------------------------------
# Step tracking — writes to document_processing_steps for Realtime checklist
# ---------------------------------------------------------------------------

def _get_sb():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        return create_client(url, key)
    except Exception:
        return None


def _upsert_step(
    document_id: str,
    case_id: str | None,
    step_name: str,
    display_label: str,
    status: str,
    error: str | None = None,
) -> None:
    """Write a step row so the frontend Realtime checklist can show progress."""
    if not document_id:
        return
    sb = _get_sb()
    if not sb:
        return
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        row: dict = {
            "document_id":   document_id,
            "step_name":     step_name,
            "display_label": display_label,
            "status":        status,
        }
        if case_id:
            row["case_id"] = case_id
        if status == "running":
            row["started_at"] = now
        if status in ("done", "error"):
            row["completed_at"] = now
        if error:
            row["error_message"] = error
        sb.table("document_processing_steps").upsert(
            row, on_conflict="document_id,step_name"
        ).execute()
    except Exception as e:
        print(f"[steps] WARNING: could not write step '{step_name}': {e}", file=sys.stderr)


def _lookup_document_id(file_stem: str) -> str | None:
    """Return the UUID of the document whose file_name == file_stem, or None."""
    sb = _get_sb()
    if not sb:
        return None
    try:
        resp = (
            sb.table("documents")
            .select("id")
            .eq("file_name", file_stem)
            .maybe_single()
            .execute()
        )
        return resp.data["id"] if resp.data else None
    except Exception:
        return None


async def _pipeline_stream(filename: str, case_id: str):
    file_stem = os.path.splitext(filename)[0]
    short_id  = case_id[:8]

    # ── Phase 1: intake → text extraction → classification → TOC → Supabase ──
    yield _sse({"phase": 1, "status": "start", "label": "Extracting & classifying"})
    rc = await _run([sys.executable, PHASE1_MAIN, filename, "--case-id", case_id])
    if rc != 0:
        yield _sse({"phase": 1, "status": "error", "label": "Extracting & classifying"})
        return
    yield _sse({"phase": 1, "status": "done", "label": "Extracting & classifying"})

    # Resolve the real document UUID now that Phase 1 has written it to Supabase
    document_id = _lookup_document_id(file_stem)

    # Mark all Phase 1 sub-steps as done (they completed synchronously above)
    if document_id:
        for sname, slabel in [
            ("text_extraction",    "Text extraction"),
            ("doc_classification", "Document classification"),
            ("toc_split",          "Section splitting"),
            ("saved_to_db",        "Saved to database"),
        ]:
            _upsert_step(document_id, case_id, sname, slabel, "done")

    # ── Phase 2: AST + entity extraction — background, non-blocking ──────────
    #    Passes --document_id so Phase 2 can write its own step rows.
    _run_background(
        [sys.executable, PHASE2_MAIN, "--file_name", file_stem,
         "--document_id", document_id or ""],
        log_name=f"02_middle_{short_id}",
    )

    # ── Phase 3: section embedding — background (was previously blocking) ─────
    #    Phase 3 writes its own "embeddings" step row and fires Phase 4 summary.
    _run_background(
        [sys.executable, PHASE3_MAIN, "--case_id", case_id,
         "--document_id", document_id or ""],
        log_name=f"03_embed_{short_id}",
    )

    # ── SSE done — frontend subscribes to Realtime for the live checklist ─────
    yield _sse({"done": True, "document_id": document_id or filename, "case_id": case_id})


@app.post("/api/ingest")
async def ingest(
    file: UploadFile = File(...),
    case_id: str | None = Form(None),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Sanitise filename and make unique to avoid collisions
    safe_name   = os.path.basename(file.filename)
    stem, ext   = os.path.splitext(safe_name)
    unique_name = f"{stem}_{uuid.uuid4().hex[:8]}{ext}"

    dest = os.path.join(DOCS_DIR, unique_name)
    os.makedirs(DOCS_DIR, exist_ok=True)

    contents = await file.read()
    with open(dest, "wb") as f:
        f.write(contents)

    # Ensure case_id is always set so Phase 3 & 4 can use it
    if not case_id:
        case_id = str(uuid.uuid4())

    return StreamingResponse(
        _pipeline_stream(unique_name, case_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Intake queue endpoints — multi-channel ingestion
# ---------------------------------------------------------------------------

DEFAULT_FIRM_ID = "00000000-0000-4000-a000-000000000001"


@app.post("/api/intake/upload")
async def intake_upload(
    file: UploadFile = File(...),
    firm_id: str = Form(DEFAULT_FIRM_ID),
    case_id: str | None = Form(None),
    corpus_id: str | None = Form(None),
    priority: str = Form("soon"),
    processing_mode: str = Form("balanced"),
):
    """Upload a document into the intake queue with routing + scheduling."""
    import hashlib

    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    safe_name   = os.path.basename(file.filename)
    stem, ext   = os.path.splitext(safe_name)
    unique_name = f"{stem}_{uuid.uuid4().hex[:8]}{ext}"
    dest = os.path.join(DOCS_DIR, unique_name)
    os.makedirs(DOCS_DIR, exist_ok=True)

    contents = await file.read()
    file_hash = hashlib.sha256(contents).hexdigest()

    with open(dest, "wb") as f:
        f.write(contents)

    sb = _get_sb()
    if not sb:
        raise HTTPException(status_code=500, detail="Database unavailable")

    # Deduplication check
    dup_resp = (
        sb.table("intake_queue")
        .select("id, status")
        .eq("file_hash", file_hash)
        .eq("firm_id", firm_id)
        .execute()
    )
    if dup_resp.data:
        existing = dup_resp.data[0]
        return {
            "intake_id": existing["id"],
            "status": existing["status"],
            "deduplicated": True,
            "message": "Duplicate file already in queue",
        }

    # Determine initial status
    if case_id:
        # Case provided — auto-confirm routing, use immediate priority
        status = "confirmed"
        actual_priority = "immediate" if priority == "soon" else priority
    else:
        status = "pending"
        actual_priority = priority

    row = {
        "firm_id": firm_id,
        "source_channel": "upload",
        "file_path": dest,
        "file_name": unique_name,
        "file_hash": file_hash,
        "status": status,
        "process_priority": actual_priority,
        "processing_mode": processing_mode,
    }
    if case_id:
        row["target_case_id"] = case_id
    if corpus_id:
        row["target_corpus_id"] = corpus_id

    resp = sb.table("intake_queue").insert(row).execute()
    intake_id = resp.data[0]["id"]

    # If auto-confirmed with immediate priority, dispatch now
    if status == "confirmed" and actual_priority == "immediate":
        import asyncio
        sys.path.insert(0, os.path.join(BACKEND_DIR, "05_INTAKE"))
        from scheduler_worker import dispatch_intake_item
        asyncio.create_task(dispatch_intake_item(uuid.UUID(intake_id)))

    return {
        "intake_id": intake_id,
        "status": status,
        "priority": actual_priority,
        "deduplicated": False,
    }


@app.get("/api/intake/queue")
async def list_intake_queue(
    firm_id: str = Query(DEFAULT_FIRM_ID),
    status: str | None = Query(None),
    limit: int = Query(50),
    offset: int = Query(0),
):
    """List intake queue items for a firm."""
    sb = _get_sb()
    if not sb:
        raise HTTPException(status_code=500, detail="Database unavailable")

    q = (
        sb.table("intake_queue")
        .select("*", count="exact")
        .eq("firm_id", firm_id)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if status:
        q = q.eq("status", status)

    resp = q.execute()
    return {
        "items": resp.data or [],
        "total": resp.count or 0,
    }


class ConfirmRouting(BaseModel):
    case_id: str
    corpus_id: str | None = None


@app.post("/api/intake/queue/{intake_id}/confirm")
async def confirm_intake_routing(intake_id: str, body: ConfirmRouting):
    """User confirms the routing suggestion for an intake item."""
    sb = _get_sb()
    if not sb:
        raise HTTPException(status_code=500, detail="Database unavailable")

    from datetime import datetime, timezone
    update = {
        "status": "confirmed",
        "target_case_id": body.case_id,
        "user_decision": {
            "action": "confirm",
            "decided_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    if body.corpus_id:
        update["target_corpus_id"] = body.corpus_id

    sb.table("intake_queue").update(update).eq("id", intake_id).execute()
    return {"confirmed": True, "intake_id": intake_id}


class ReassignRouting(BaseModel):
    case_id: str
    corpus_id: str | None = None


@app.post("/api/intake/queue/{intake_id}/reassign")
async def reassign_intake(intake_id: str, body: ReassignRouting):
    """User overrides the routing suggestion with a different case."""
    sb = _get_sb()
    if not sb:
        raise HTTPException(status_code=500, detail="Database unavailable")

    from datetime import datetime, timezone
    sb.table("intake_queue").update({
        "status": "confirmed",
        "target_case_id": body.case_id,
        "target_corpus_id": body.corpus_id,
        "user_decision": {
            "action": "reassign",
            "decided_at": datetime.now(timezone.utc).isoformat(),
        },
    }).eq("id", intake_id).execute()
    return {"reassigned": True, "intake_id": intake_id}


@app.post("/api/intake/email")
async def intake_email_webhook(request: Request):
    """Inbound email webhook (Postmark or SendGrid)."""
    content_type = request.headers.get("content-type", "")

    if "json" in content_type:
        payload = await request.json()
        sys.path.insert(0, os.path.join(BACKEND_DIR, "05_INTAKE", "adapters"))
        from email import parse_postmark_inbound
        intakes = parse_postmark_inbound(payload)
    else:
        form = await request.form()
        form_data = dict(form)
        sys.path.insert(0, os.path.join(BACKEND_DIR, "05_INTAKE", "adapters"))
        from email import parse_sendgrid_inbound
        intakes = parse_sendgrid_inbound(form_data)

    sb = _get_sb()
    if not sb:
        raise HTTPException(status_code=500, detail="Database unavailable")

    intake_ids = []
    for intake in intakes:
        row = {
            "firm_id": str(intake.firm_id),
            "source_channel": intake.source_channel,
            "source_ref": intake.source_ref,
            "source_metadata": intake.source_metadata,
            "file_name": intake.file_name,
            "file_hash": intake.file_hash,
            "status": "pending",
            "process_priority": intake.process_priority,
            "processing_mode": intake.processing_mode,
        }
        if intake.explicit_case_hint:
            row["explicit_case_hint"] = intake.explicit_case_hint
        resp = sb.table("intake_queue").insert(row).execute()
        intake_ids.append(resp.data[0]["id"])

    return {"received": len(intake_ids), "intake_ids": intake_ids}


# ---------------------------------------------------------------------------
# GDrive / Dropbox / CMS webhook channel endpoints
# ---------------------------------------------------------------------------

@app.post("/api/intake/gdrive/webhook")
async def intake_gdrive_webhook(request: Request):
    """Google Drive push notification webhook."""
    headers = dict(request.headers)
    body = await request.body()
    sys.path.insert(0, os.path.join(BACKEND_DIR, "05_INTAKE", "adapters"))
    from gdrive import handle_gdrive_webhook
    result = handle_gdrive_webhook(headers, body)
    return result


@app.get("/api/intake/dropbox/webhook")
async def intake_dropbox_verify(challenge: str = ""):
    """Dropbox webhook verification (GET with challenge)."""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(challenge, headers={"X-Content-Type-Options": "nosniff"})


@app.post("/api/intake/dropbox/webhook")
async def intake_dropbox_webhook(request: Request):
    """Dropbox change notification webhook."""
    payload = await request.json()
    sys.path.insert(0, os.path.join(BACKEND_DIR, "05_INTAKE", "adapters"))
    from dropbox import handle_dropbox_webhook
    return handle_dropbox_webhook(payload)


@app.post("/api/intake/webhook/{firm_id}")
async def intake_cms_webhook(
    firm_id: str,
    request: Request,
    api_key: str | None = None,
):
    """Generic CMS webhook receiver. Requires API key in header or query param."""
    key = api_key or request.headers.get("x-api-key", "")
    if not key:
        raise HTTPException(status_code=401, detail="API key required (X-API-Key header or ?api_key=)")

    sys.path.insert(0, os.path.join(BACKEND_DIR, "05_INTAKE", "adapters"))
    from cms_webhook import verify_api_key, handle_cms_webhook

    if not verify_api_key(firm_id, key):
        raise HTTPException(status_code=403, detail="Invalid or expired API key")

    content_type = request.headers.get("content-type", "")

    if "multipart" in content_type:
        form = await request.form()
        files = []
        metadata = {}
        for field_name, field_value in form.items():
            if hasattr(field_value, "read"):
                content = await field_value.read()
                files.append((field_value.filename or field_name, content))
            else:
                metadata[field_name] = str(field_value)
        result = handle_cms_webhook(firm_id, key, files, metadata)
    elif "json" in content_type:
        body = await request.json()
        # JSON mode: expects {files: [{name, content_base64}], metadata: {...}}
        import base64
        raw_files = body.get("files", [])
        files = []
        for f in raw_files:
            content = base64.b64decode(f.get("content_base64", ""))
            files.append((f.get("name", "document.pdf"), content))
        result = handle_cms_webhook(firm_id, key, files, body.get("metadata", {}))
    else:
        raise HTTPException(status_code=415, detail="Expected multipart/form-data or application/json")

    return result


# ---------------------------------------------------------------------------
# Connected channels management
# ---------------------------------------------------------------------------

@app.get("/api/channels")
async def list_channels(firm_id: str = Query(DEFAULT_FIRM_ID)):
    """List all connected channels for a firm."""
    sb = _get_sb()
    if not sb:
        raise HTTPException(status_code=500, detail="Database unavailable")
    resp = (
        sb.table("connected_channels")
        .select("id, firm_id, channel_type, display_name, is_active, default_priority, last_sync_at, created_at")
        .eq("firm_id", firm_id)
        .order("created_at", desc=True)
        .execute()
    )
    return {"channels": resp.data or []}


class CreateChannel(BaseModel):
    firm_id: str = DEFAULT_FIRM_ID
    channel_type: str        # 'gdrive' | 'dropbox' | 'cms_webhook' | 'email'
    display_name: str
    config: dict = {}
    default_priority: str = "overnight"
    default_case_id: str | None = None
    default_corpus_id: str | None = None


@app.post("/api/channels")
async def create_channel(body: CreateChannel):
    """Create a new connected channel."""
    sb = _get_sb()
    if not sb:
        raise HTTPException(status_code=500, detail="Database unavailable")

    row = {
        "firm_id": body.firm_id,
        "channel_type": body.channel_type,
        "display_name": body.display_name,
        "config": body.config,
        "default_priority": body.default_priority,
    }
    if body.default_case_id:
        row["default_case_id"] = body.default_case_id
    if body.default_corpus_id:
        row["default_corpus_id"] = body.default_corpus_id

    resp = sb.table("connected_channels").insert(row).execute()
    channel_id = resp.data[0]["id"]

    # If CMS webhook, auto-generate an API key
    if body.channel_type == "cms_webhook":
        sys.path.insert(0, os.path.join(BACKEND_DIR, "05_INTAKE", "adapters"))
        from cms_webhook import generate_api_key
        api_key = generate_api_key(body.firm_id, channel_id)
        return {"channel_id": channel_id, "api_key": api_key}

    return {"channel_id": channel_id}


@app.delete("/api/channels/{channel_id}")
async def delete_channel(channel_id: str):
    """Deactivate a connected channel."""
    sb = _get_sb()
    if not sb:
        raise HTTPException(status_code=500, detail="Database unavailable")
    sb.table("connected_channels").update({"is_active": False}).eq("id", channel_id).execute()
    return {"deactivated": True}


# ---------------------------------------------------------------------------
# Notification endpoints
# ---------------------------------------------------------------------------

@app.get("/api/notifications")
async def list_notifications(
    firm_id: str = Query(DEFAULT_FIRM_ID),
    unread_only: bool = Query(True),
    limit: int = Query(20),
):
    """List notifications for a firm."""
    sb = _get_sb()
    if not sb:
        raise HTTPException(status_code=500, detail="Database unavailable")

    q = (
        sb.table("notifications")
        .select("*")
        .eq("firm_id", firm_id)
        .order("created_at", desc=True)
        .limit(limit)
    )
    if unread_only:
        q = q.eq("read", False)

    resp = q.execute()
    return {"notifications": resp.data or []}


@app.patch("/api/notifications/{notification_id}/read")
async def mark_notification_read(notification_id: str):
    """Mark a single notification as read."""
    sb = _get_sb()
    if not sb:
        raise HTTPException(status_code=500, detail="Database unavailable")

    sb.table("notifications").update({"read": True}).eq("id", notification_id).execute()
    return {"marked_read": True}


# ---------------------------------------------------------------------------
# /api/query — Legal agent chat endpoint
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Persistent checkpointer — PostgresSaver (Supabase) with MemorySaver fallback
# ---------------------------------------------------------------------------

def _create_checkpointer():
    """Return a PostgresSaver backed by Supabase if DATABASE_URL is set.

    DATABASE_URL must be the direct PostgreSQL connection string from
    Supabase → Project Settings → Database → Connection string (URI, port 5432).
    Example: postgresql://postgres:{password}@db.{ref}.supabase.co:5432/postgres

    Falls back to MemorySaver (in-process RAM, lost on restart) when the
    env var is absent or the postgres packages are not installed.
    """
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        from langgraph.checkpoint.memory import MemorySaver
        print("[checkpointer] DATABASE_URL not set — using in-memory MemorySaver (memory lost on restart)")
        return MemorySaver()

    try:
        from psycopg_pool import ConnectionPool
        from langgraph.checkpoint.postgres import PostgresSaver

        pool = ConnectionPool(
            conninfo=db_url,
            max_size=10,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=True,
        )
        checkpointer = PostgresSaver(pool)
        checkpointer.setup()  # creates checkpoints + checkpoint_writes tables if they don't exist
        print("[checkpointer] PostgresSaver initialised — multi-turn memory persists across restarts")
        return checkpointer
    except ImportError:
        print("[checkpointer] psycopg_pool / langgraph-checkpoint-postgres not installed — falling back to MemorySaver")
        print("[checkpointer]   pip install 'psycopg[binary]' psycopg_pool langgraph-checkpoint-postgres")
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()
    except Exception as e:
        print(f"[checkpointer] PostgresSaver init failed ({e}) — falling back to MemorySaver")
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()


# Lazily-built graph (expensive to initialise — built once, reused across requests)
_graph_cache = None
_graph_lock  = asyncio.Lock()


def _build_graph_sync():
    """Import and build the LangGraph agent. Runs in an executor thread."""
    import importlib.util as _ilu
    from langchain_core.messages import HumanMessage  # noqa: F401 — triggers langchain init

    agentic_dir = os.path.join(BACKEND_DIR, "04_AGENTIC_ARCHITECTURE")
    project_root = os.path.join(BACKEND_DIR, "..")
    for p in (project_root, agentic_dir):
        if p not in sys.path:
            sys.path.insert(0, p)

    spec = _ilu.spec_from_file_location("graph_module", os.path.join(agentic_dir, "graph.py"))
    mod  = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)

    checkpointer = _create_checkpointer()
    return mod.build_graph(checkpointer=checkpointer)


async def _get_graph():
    global _graph_cache
    async with _graph_lock:
        if _graph_cache is None:
            loop = asyncio.get_event_loop()
            _graph_cache = await loop.run_in_executor(None, _build_graph_sync)
    return _graph_cache


class QueryRequest(BaseModel):
    case_id:    str
    query:      str
    session_id: str | None = None


@app.post("/api/query")
async def query_agent(body: QueryRequest):
    from langchain_core.messages import HumanMessage
    import datetime

    session_id = body.session_id or str(uuid.uuid4())
    thread_id  = f"case-{body.case_id}-{session_id}"

    # Fetch case metadata so agents know what stage the litigation is in.
    # A filing-stage case needs very different analysis than a trial or appeal.
    _case_meta: dict = {}
    try:
        from supabase import create_client as _sb_create
        _sb = _sb_create(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_ROLE_KEY"],
        )
        _row = (
            _sb.table("cases")
            .select("case_stage,case_context,party_role,our_client,opposing_party,court_name")
            .eq("id", body.case_id)
            .maybe_single()
            .execute()
        )
        _case_meta = _row.data or {}
    except Exception:
        pass  # non-fatal — agents still work, just without stage context

    state = {
        "messages":            [HumanMessage(content=body.query)],
        "case_id":             body.case_id,
        "tool_call_count":     0,
        "search_results":      [],
        "kg_context":          [],
        "extractions_context": [],
        "provenance_links":    [],
        "reasoning_steps":     [],
        "needs_review":        False,
        "query_type":          None,
        "agent_name":          None,
        "answer":              None,
        "confidence":          None,
        "conversation_summary": None,
        # Case context — injected into agent system prompts
        "case_stage":      _case_meta.get("case_stage"),
        "case_context":    _case_meta.get("case_context"),
        "party_role":      _case_meta.get("party_role"),
        "our_client":      _case_meta.get("our_client"),
        "opposing_party":  _case_meta.get("opposing_party"),
        "court_name":      _case_meta.get("court_name"),
    }
    config = {"configurable": {"thread_id": thread_id, "case_id": body.case_id}}

    try:
        graph = await _get_graph()
        loop  = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: graph.invoke(state, config=config)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent error: {e}")

    return {
        "id":               str(uuid.uuid4()),
        "case_id":          body.case_id,
        "session_id":       session_id,
        "query":            body.query,
        "agent_name":       result.get("agent_name") or "unknown",
        "answer":           result.get("answer") or "(no answer)",
        "confidence":       float(result.get("confidence") or 0),
        "needs_review":     bool(result.get("needs_review", False)),
        "provenance_links": result.get("provenance_links") or [],
        "reasoning_steps":  result.get("reasoning_steps") or [],
        "tool_calls_made":  [],
        "created_at":       datetime.datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# /api/case/{case_id}/sessions — Session / memory management
# ---------------------------------------------------------------------------

def _get_supabase_client():
    from supabase import create_client
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )


@app.get("/api/case/{case_id}/sessions")
async def list_sessions(case_id: str):
    """List all conversation sessions for a case.

    Pulls from agent_responses (the audit log) and groups by session_id so
    the UI can show what conversations exist and how heavy they are.
    """
    try:
        sb = _get_supabase_client()
        resp = (
            sb.table("agent_responses")
            .select("session_id, created_at, query, agent_name, confidence")
            .eq("case_id", case_id)
            .order("created_at", desc=False)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not fetch sessions: {e}")

    sessions: dict[str, dict] = {}
    for row in rows:
        sid = row["session_id"]
        if sid not in sessions:
            sessions[sid] = {
                "session_id":    sid,
                "message_count": 0,
                "first_query":   row.get("query", "")[:80],
                "last_active":   row.get("created_at"),
            }
        sessions[sid]["message_count"] += 1
        sessions[sid]["last_active"] = row.get("created_at")

    # Sort most-recent first
    result = sorted(sessions.values(), key=lambda s: s["last_active"] or "", reverse=True)
    return {"sessions": result}


@app.delete("/api/case/{case_id}/sessions/{session_id}")
async def clear_session_memory(case_id: str, session_id: str):
    """Clear LangGraph checkpoint state for a session.

    This resets what the AI remembers for that conversation thread.
    The audit log in agent_responses is NOT deleted — that stays for review.

    Requires DATABASE_URL to be set (PostgresSaver mode). In MemorySaver mode,
    the in-process thread is simply dropped from the cache (best effort).
    """
    thread_id = f"case-{case_id}-{session_id}"
    db_url = os.environ.get("DATABASE_URL")

    if db_url:
        try:
            import psycopg
            with psycopg.connect(db_url, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM checkpoint_writes WHERE thread_id = %s",
                        (thread_id,),
                    )
                    cur.execute(
                        "DELETE FROM checkpoints WHERE thread_id = %s",
                        (thread_id,),
                    )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to clear checkpoints: {e}")
    else:
        # MemorySaver — clear from the in-memory cache if graph is loaded
        global _graph_cache
        if _graph_cache is not None:
            try:
                _graph_cache.checkpointer.storage.pop(thread_id, None)
            except Exception:
                pass  # MemorySaver internals may vary

    return {
        "cleared":   True,
        "thread_id": thread_id,
        "note":      "AI memory cleared. Audit log (agent_responses) preserved.",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False, app_dir=BACKEND_DIR)
