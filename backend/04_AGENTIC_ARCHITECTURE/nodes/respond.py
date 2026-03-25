"""
nodes/respond.py — Response formatting and HITL flagging node.

Takes the LLM's final answer from the message history, extracts provenance
links from the conversation context, calculates confidence, and structures
the output into the AgentResponse schema.

Also persists the response to Supabase via persistence.py.
"""

import re

from langchain_core.messages import AIMessage, ToolMessage


# ---------------------------------------------------------------------------
# Provenance extraction
# ---------------------------------------------------------------------------

def _extract_provenance(messages: list, search_results: list[dict]) -> list[dict]:
    """
    Build provenance links by matching cited section titles/file names
    from the AI's response against the search results that were retrieved.
    """
    # Collect all AI message content in this turn
    def _msg_text(m) -> str:
        if isinstance(m.content, str):
            return m.content
        if isinstance(m.content, list):
            return " ".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in m.content
            )
        return ""

    ai_text = " ".join(
        _msg_text(m) for m in messages
        if isinstance(m, AIMessage) and m.content
    )

    links = []
    seen_ids = set()

    for r in search_results:
        sec_id = r.get("section_id", "")
        if sec_id in seen_ids:
            continue

        title    = r.get("section_title") or ""
        fname    = r.get("file_name") or ""
        combined = (title + " " + fname).lower()

        # Include if the section was actually mentioned in the AI's response
        # or if it had a high combined score (≥ 0.65)
        score    = r.get("scores", {}).get("combined", 0)
        mentioned = any(
            kw and kw.lower() in ai_text.lower()
            for kw in [title, fname]
            if kw and len(kw) > 4
        )

        if mentioned or score >= 0.65:
            text = r.get("section_text") or ""
            links.append({
                "section_id":    sec_id,
                "file_name":     fname,
                "document_type": r.get("document_type"),
                "page_range":    r.get("page_range"),
                "quote_snippet": text[:200] if text else None,
            })
            seen_ids.add(sec_id)

    return links


# ---------------------------------------------------------------------------
# Confidence calculation
# ---------------------------------------------------------------------------

def _calculate_confidence(
    provenance_links: list[dict],
    search_results: list[dict],
    has_kg_paths: bool = False,
) -> float:
    """
    Heuristic confidence score:
      - Base: 0.5
      - +0.05 per provenance link (up to +0.25)
      - +0.10 if top search result score ≥ 0.75
      - +0.10 if KG paths were found
      - -0.10 if any provenance link came from a synthetic section
    """
    confidence = 0.5

    # Provenance breadth
    confidence += min(len(provenance_links) * 0.05, 0.25)

    # Search quality
    if search_results:
        top_score = max(
            r.get("scores", {}).get("combined", 0) for r in search_results
        )
        if top_score >= 0.75:
            confidence += 0.10

    # KG evidence paths found
    if has_kg_paths:
        confidence += 0.10

    # Penalty for synthetic sections (less reliable)
    synthetic_count = sum(
        1 for r in search_results if r.get("is_synthetic")
    )
    if synthetic_count > 0:
        confidence -= 0.05

    return round(min(max(confidence, 0.0), 1.0), 2)


# ---------------------------------------------------------------------------
# Reasoning steps extraction
# ---------------------------------------------------------------------------

def _extract_reasoning_steps(messages: list) -> list[str]:
    """Extract a concise list of reasoning steps from tool calls + AI messages."""
    steps = []
    for m in messages:
        if isinstance(m, AIMessage) and hasattr(m, "tool_calls") and m.tool_calls:
            for tc in m.tool_calls:
                tool_name = tc.get("name", "unknown_tool")
                args      = tc.get("args", {})
                arg_str   = ", ".join(f"{k}={repr(v)}" for k, v in list(args.items())[:2])
                steps.append(f"Called {tool_name}({arg_str})")
        elif isinstance(m, ToolMessage):
            content = (m.content or "")[:100].replace("\n", " ")
            steps.append(f"Tool returned: {content}...")
    return steps


# ---------------------------------------------------------------------------
# Main respond node
# ---------------------------------------------------------------------------

def respond(state: dict) -> dict:
    """Format the agent's final answer and apply HITL flagging.

    Reads the last AI message as the answer, builds provenance links
    from retrieved search results, calculates confidence, and writes
    the structured output to state.
    """
    messages       = state.get("messages", [])
    search_results = state.get("search_results", [])
    kg_context     = state.get("kg_context", [])

    # Get the last AI message as the final answer
    answer = ""
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            answer = m.content if isinstance(m.content, str) else " ".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in m.content
            )
            if answer.strip():
                break

    # Build provenance
    provenance_links = _extract_provenance(messages, search_results)

    # Check if KG paths were retrieved (tool was called)
    has_kg_paths = len(kg_context) > 0 or any(
        isinstance(m, ToolMessage) and "claim" in (m.content or "").lower()
        for m in messages
    )

    # Calculate confidence
    confidence = _calculate_confidence(provenance_links, search_results, has_kg_paths)

    # HITL flag
    needs_review = confidence < 0.7

    # Reasoning steps
    reasoning_steps = _extract_reasoning_steps(messages)

    # Persist to Supabase (non-fatal if it fails)
    try:
        import sys as _sys, os as _os
        _arch = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '..'))
        if _arch not in _sys.path:
            _sys.path.insert(0, _arch)
        from persistence import save_response
        save_response(
            case_id         = state.get("case_id", ""),
            session_id      = state.get("session_id", ""),
            query           = messages[0].content if messages else "",
            agent_name      = state.get("agent_name", "complaint_agent"),
            answer          = answer,
            confidence      = confidence,
            needs_review    = needs_review,
            provenance_links= provenance_links,
            reasoning_steps = reasoning_steps,
            messages        = messages,
        )
    except Exception:
        pass  # persistence failure never blocks the response

    return {
        "answer":           answer,
        "confidence":       confidence,
        "provenance_links": provenance_links,
        "needs_review":     needs_review,
        "reasoning_steps":  reasoning_steps,
    }
