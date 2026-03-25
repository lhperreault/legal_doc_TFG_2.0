"""
state.py — AgentState definition for the LangGraph agent.

Every node in the graph reads from and writes to this state dict.
The `messages` field accumulates via add_messages (LangGraph built-in reducer).
All other list fields accumulate via operator.add.
Scalar output fields (answer, confidence, etc.) are replaced on each write.
"""

import operator
from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    # ── Conversation ──────────────────────────────────────────────────────────
    # Full message history. add_messages appends new messages instead of replacing.
    # This is what enables multi-turn: prior messages survive across turns.
    messages: Annotated[list, add_messages]

    # ── Routing ───────────────────────────────────────────────────────────────
    case_id: str
    query_type: str | None          # "complaint", "contract", "general", "clarification"
    agent_name: str | None          # which specialized agent is active

    # ── Retrieved context (accumulated across tool calls in one turn) ─────────
    search_results: Annotated[list[dict], operator.add]
    kg_context: Annotated[list[dict], operator.add]
    extractions_context: Annotated[list[dict], operator.add]

    # ── Agent output ──────────────────────────────────────────────────────────
    answer: str | None
    confidence: float | None
    # [{section_id, page_range, quote_snippet, file_name, document_type}]
    provenance_links: list[dict]
    needs_review: bool
    # ["searched for X", "found Y", "synthesized Z"]
    reasoning_steps: list[str]

    # ── Control flow ──────────────────────────────────────────────────────────
    # Counts agent↔tool round-trips. Capped at MAX_TOOL_ROUNDS in should_continue.
    tool_call_count: int
