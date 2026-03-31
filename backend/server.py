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
    with open(log_path, "a") as lf:
        subprocess.Popen(args, stdout=lf, stderr=lf)


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
    return mod.build_graph()


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False, app_dir=BACKEND_DIR)
