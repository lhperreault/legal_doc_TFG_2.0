"""
nodes/complaint_agent.py — Complaint agent node + system prompt.

Calls Gemini with tools bound. The LLM decides which tools to invoke
based on the query. LangGraph's ToolNode executes the tools and feeds
results back here for the next reasoning step.
"""

import os

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage

import sys as _sys, os as _os
_ARCH_DIR = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '..'))
if _ARCH_DIR not in _sys.path:
    _sys.path.insert(0, _ARCH_DIR)

from tools import complaint_tools

_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'prompts', 'complaint_system.md'
)

with open(_SYSTEM_PROMPT_PATH, encoding='utf-8') as _f:
    _SYSTEM_PROMPT_TEMPLATE = _f.read()

_base_model = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0.2,
    google_api_key=os.getenv("GEMINI_API_KEY"),
)

_model_with_tools = _base_model.bind_tools(complaint_tools)

MAX_TOOL_ROUNDS = 5


def complaint_agent_node(state: dict) -> dict:
    """Complaint agent: calls Gemini with tools to answer the query.

    On each invocation, the LLM sees the full conversation history
    (including prior tool calls and results) and decides what to do next.
    It either calls another tool or produces a final answer.
    """
    case_id = state.get("case_id", "")

    system_message = SystemMessage(
        content=_SYSTEM_PROMPT_TEMPLATE.format(case_id=case_id)
    )

    response = _model_with_tools.invoke(
        [system_message] + state["messages"]
    )

    return {
        "messages": [response],
        "tool_call_count": state.get("tool_call_count", 0) + 1,
        "agent_name": "complaint_agent",
    }


def should_continue(state: dict) -> str:
    """Decide whether to execute tools or move to the respond node.

    Returns 'tools' if the LLM requested tool calls (and under the limit).
    Returns 'respond' if the LLM produced a final answer or limit reached.
    """
    last_message = state["messages"][-1]

    has_tool_calls = (
        hasattr(last_message, "tool_calls")
        and last_message.tool_calls
    )

    if has_tool_calls:
        if state.get("tool_call_count", 0) >= MAX_TOOL_ROUNDS:
            # Safety limit: force response even if agent wants more tools
            return "respond"
        return "tools"

    return "respond"
