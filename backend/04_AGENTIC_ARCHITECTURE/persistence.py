"""
persistence.py — Save agent responses to Supabase for HITL audit trail.

Requires these tables in Supabase (run the SQL below once):

    CREATE TABLE IF NOT EXISTS agent_responses (
        id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id           UUID NOT NULL REFERENCES cases(id),
        session_id        TEXT NOT NULL,
        query             TEXT NOT NULL,
        agent_name        TEXT NOT NULL,
        answer            TEXT NOT NULL,
        confidence        FLOAT NOT NULL,
        needs_review      BOOLEAN DEFAULT FALSE,
        provenance_links  JSONB,
        reasoning_steps   JSONB,
        tool_calls_made   JSONB,
        created_at        TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS reviews (
        id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        agent_response_id   UUID NOT NULL REFERENCES agent_responses(id),
        reviewer_id         TEXT,
        review_action       TEXT NOT NULL,   -- "approved", "corrected", "rejected"
        correction_text     TEXT,
        correction_notes    TEXT,
        reviewed_at         TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE INDEX idx_agent_responses_case   ON agent_responses (case_id);
    CREATE INDEX idx_agent_responses_review ON agent_responses (needs_review, case_id);
    CREATE INDEX idx_reviews_response       ON reviews (agent_response_id);
"""

import json
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))


def _get_supabase():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise EnvironmentError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set")
    return create_client(url, key)


def _extract_tool_calls(messages: list) -> list[dict]:
    """Summarize tool calls made during this turn."""
    from langchain_core.messages import AIMessage, ToolMessage

    tool_summaries = []
    tool_results   = {m.tool_call_id: m.content for m in messages if isinstance(m, ToolMessage)}

    for m in messages:
        if isinstance(m, AIMessage) and hasattr(m, "tool_calls") and m.tool_calls:
            for tc in m.tool_calls:
                result = tool_results.get(tc.get("id", ""), "")
                tool_summaries.append({
                    "tool_name":      tc.get("name"),
                    "args":           tc.get("args", {}),
                    "result_summary": (result or "")[:300],
                })

    return tool_summaries


def save_response(
    case_id:          str,
    session_id:       str,
    query:            str,
    agent_name:       str,
    answer:           str,
    confidence:       float,
    needs_review:     bool,
    provenance_links: list[dict],
    reasoning_steps:  list[str],
    messages:         list,
) -> str | None:
    """
    Persist an agent response to the agent_responses table.
    Returns the new row's UUID, or None on failure.
    """
    if not case_id or not answer:
        return None

    try:
        sb = _get_supabase()
    except Exception:
        return None

    tool_calls_made = _extract_tool_calls(messages)

    row = {
        "case_id":         case_id,
        "session_id":      session_id or "unknown",
        "query":           query[:2000],
        "agent_name":      agent_name,
        "answer":          answer,
        "confidence":      round(float(confidence), 4),
        "needs_review":    needs_review,
        "provenance_links": provenance_links,
        "reasoning_steps":  reasoning_steps,
        "tool_calls_made":  tool_calls_made,
    }

    try:
        resp = sb.table("agent_responses").insert(row).execute()
        return resp.data[0]["id"] if resp.data else None
    except Exception:
        return None


def get_review_queue(case_id: str, limit: int = 50) -> list[dict]:
    """Fetch agent responses that need human review for a case."""
    try:
        sb = _get_supabase()
        resp = (
            sb.table("agent_responses")
            .select("id, query, agent_name, confidence, answer, provenance_links, created_at")
            .eq("case_id", case_id)
            .eq("needs_review", True)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data or []
    except Exception:
        return []


def submit_review(
    agent_response_id: str,
    reviewer_id:       str,
    review_action:     str,
    correction_text:   str | None = None,
    correction_notes:  str | None = None,
) -> bool:
    """Submit a human review for a flagged agent response. Returns True on success."""
    if review_action not in ("approved", "corrected", "rejected"):
        raise ValueError(f"Invalid review_action: {review_action}")

    try:
        sb = _get_supabase()
        sb.table("reviews").insert({
            "agent_response_id": agent_response_id,
            "reviewer_id":       reviewer_id,
            "review_action":     review_action,
            "correction_text":   correction_text,
            "correction_notes":  correction_notes,
        }).execute()
        return True
    except Exception:
        return False
