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

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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


async def _pipeline_stream(filename: str, case_id: str):
    file_stem = os.path.splitext(filename)[0]
    short_id  = case_id[:8]

    # Phase 1: intake → text extraction → classification → TOC → Supabase
    yield _sse({"phase": 1, "status": "start", "label": "Extracting & classifying"})
    rc = await _run([sys.executable, PHASE1_MAIN, filename, "--case-id", case_id])
    if rc != 0:
        yield _sse({"phase": 1, "status": "error", "label": "Extracting & classifying"})
        return
    yield _sse({"phase": 1, "status": "done", "label": "Extracting & classifying"})

    # Phase 2: AST + entity extraction — logs to data_storage/logs/02_middle_<id>.log
    _run_background(
        [sys.executable, PHASE2_MAIN, "--file_name", file_stem],
        log_name=f"02_middle_{short_id}",
    )

    # Phase 3: section embedding → vector store
    yield _sse({"phase": 2, "status": "start", "label": "Embedding & indexing for search"})
    rc = await _run([sys.executable, PHASE3_MAIN, "--case_id", case_id])
    if rc != 0:
        yield _sse({"phase": 2, "status": "error", "label": "Embedding & indexing for search"})
        return
    yield _sse({"phase": 2, "status": "done", "label": "Embedding & indexing for search"})

    # Phase 4: generate professional case summary — populates the legal pad UI
    yield _sse({"phase": 3, "status": "start", "label": "Generating case summary"})
    rc = await _run([sys.executable, PHASE4_SUMMARY, "--case_id", case_id])
    if rc != 0:
        yield _sse({"phase": 3, "status": "error", "label": "Generating case summary"})
        return
    yield _sse({"phase": 3, "status": "done", "label": "Generating case summary"})

    # Checklist is triggered by 02_MIDDLE when it finishes — running it here
    # concurrently with 02_MIDDLE's Gemini-heavy extraction steps risks rate limits.

    yield _sse({"done": True, "document_id": filename, "case_id": case_id})


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
