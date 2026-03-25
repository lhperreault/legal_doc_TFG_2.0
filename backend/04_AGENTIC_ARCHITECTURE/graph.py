"""
graph.py — The compiled LangGraph state machine.

Wires together: classify → complaint_agent ↔ tools → respond

Entry point for all agent calls. Use build_graph() to get a compiled graph,
then invoke() or stream() it with a config containing case_id and thread_id.

Usage:
    from backend.04_AGENTIC_ARCHITECTURE.graph import build_graph
    from langchain_core.messages import HumanMessage

    graph = build_graph()
    config = {
        "configurable": {
            "thread_id": "session-123",
            "case_id":   "7d178a8c-eecb-42f6-b607-a3b847e4ec1e",
        }
    }
    result = graph.invoke(
        {
            "messages":            [HumanMessage(content="What are the main claims?")],
            "case_id":             "7d178a8c-eecb-42f6-b607-a3b847e4ec1e",
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
        },
        config=config,
    )
"""

import os

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver

import sys as _sys
_ARCH_DIR = os.path.dirname(os.path.abspath(__file__))
if _ARCH_DIR not in _sys.path:
    _sys.path.insert(0, _ARCH_DIR)

from state import AgentState
from nodes.classify import classify, route_by_type
from nodes.complaint_agent import complaint_agent_node, should_continue
from nodes.respond import respond
from tools import complaint_tools


def build_graph(checkpointer=None) -> "CompiledGraph":
    """
    Build and compile the LangGraph agent state machine.

    Args:
        checkpointer: A LangGraph checkpointer for multi-turn memory.
                      Defaults to MemorySaver (in-memory, for development).
                      In production, swap for a Supabase-backed checkpointer.

    Returns:
        A compiled LangGraph graph ready for invoke() or stream().
    """
    if checkpointer is None:
        checkpointer = MemorySaver()

    workflow = StateGraph(AgentState)

    # ── Nodes ────────────────────────────────────────────────────────────────
    workflow.add_node("classify",        classify)
    workflow.add_node("complaint_agent", complaint_agent_node)
    workflow.add_node("tools",           ToolNode(tools=complaint_tools))
    workflow.add_node("respond",         respond)

    # ── Edges ────────────────────────────────────────────────────────────────
    # Entry: every query starts at classify
    workflow.add_edge(START, "classify")

    # After classify: route to the appropriate agent based on query_type
    workflow.add_conditional_edges(
        "classify",
        route_by_type,
        {
            "complaint_agent": "complaint_agent",
        },
    )

    # After agent: either call tools or respond
    workflow.add_conditional_edges(
        "complaint_agent",
        should_continue,
        {
            "tools":   "tools",
            "respond": "respond",
        },
    )

    # After tools: always go back to the agent for reasoning
    workflow.add_edge("tools", "complaint_agent")

    # After respond: conversation turn is complete
    workflow.add_edge("respond", END)

    return workflow.compile(checkpointer=checkpointer)


# Module-level singleton (for import convenience in run.py)
# Lazily initialized on first use.
_graph = None


def get_graph() -> "CompiledGraph":
    """Return the module-level compiled graph (initialized once)."""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph
