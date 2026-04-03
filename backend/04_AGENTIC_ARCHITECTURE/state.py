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

    # ── Case context (fetched from Supabase at query time, injected into prompts) ──
    case_stage:     str | None      # "filing" | "discovery" | "motions" | "trial" | "appeal" | "closed"
    case_context:   str | None      # free-form background description written by the user
    party_role:     str | None      # "plaintiff" | "defendant" | "appellant" | "appellee"
    our_client:     str | None      # name of the client we represent
    opposing_party: str | None      # name of the opposing party
    court_name:     str | None      # court/jurisdiction

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

    # ── Compaction ────────────────────────────────────────────────────────────
    # Stores the last generated conversation summary (for debugging / auditing).
    conversation_summary: str | None

    # ── Control flow ──────────────────────────────────────────────────────────
    # Counts agent↔tool round-trips. Capped at MAX_TOOL_ROUNDS in should_continue.
    tool_call_count: int


# ── Stage-aware guidance injected into every system prompt ───────────────────

_STAGE_GUIDANCE: dict[str, str] = {
    "filing":    "The case is in the **filing/pleading stage**. Focus on standing, claims structure, parties, jurisdiction, and the sufficiency of the complaint or answer.",
    "discovery": "The case is in **discovery**. Focus on evidence gathering, disclosure obligations, interrogatory responses, deposition content, and document production requests.",
    "motions":   "The case is in **motion practice**. Focus on legal standards for dispositive motions, briefing requirements, procedural rules, and the strength of motion arguments.",
    "trial":     "The case is at the **trial stage**. Focus on evidentiary rules, admissibility, jury instructions, witness credibility, and trial strategy.",
    "appeal":    "The case is on **appeal**. Focus on preserved errors, standards of review (de novo, abuse of discretion, clear error), the appellate record, and briefing arguments.",
    "closed":    "The case is **closed**. Focus on final judgment terms, settlement obligations, post-judgment enforcement, or lessons learned.",
}


def build_case_context_block(state: dict) -> str:
    """Build a formatted case-context block to inject at the top of every system prompt.

    Gives the LLM critical situational awareness: who the client is, what stage the
    litigation is in, and what strategic lens to apply when answering questions.
    """
    stage    = (state.get("case_stage") or "").strip()
    context  = (state.get("case_context") or "").strip()
    role     = (state.get("party_role") or "").strip()
    client   = (state.get("our_client") or "").strip()
    opponent = (state.get("opposing_party") or "").strip()
    court    = (state.get("court_name") or "").strip()

    guidance = _STAGE_GUIDANCE.get(stage, "Litigation stage is not yet set — answer generally.")

    lines = ["## Active Case Context\n"]
    lines.append(f"- **Litigation Stage:** {stage or 'unknown'} — {guidance}")
    if client:
        lines.append(f"- **Our Client:** {client}")
    if role:
        lines.append(f"- **Our Role:** {role.capitalize()}")
    if opponent:
        lines.append(f"- **Opposing Party:** {opponent}")
    if court:
        lines.append(f"- **Court / Jurisdiction:** {court}")
    if context:
        lines.append(f"- **Case Background:** {context}")

    lines.append(
        f"\n> Always interpret questions through the lens of a {stage or 'litigation'}-stage case. "
        "Priorities, risks, and relevant legal standards differ significantly by stage."
    )

    return "\n".join(lines)
