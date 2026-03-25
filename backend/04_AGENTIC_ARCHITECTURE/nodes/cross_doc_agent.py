"""
nodes/cross_doc_agent.py — Cross-document reasoning agent node.

Specializes in multi-document queries: exhibit tracing, conflict detection,
cross-document KG edge traversal (exhibit_of, breached_by, same_as).
"""

import os
import sys as _sys

_ARCH_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _ARCH_DIR not in _sys.path:
    _sys.path.insert(0, _ARCH_DIR)

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage

from tools import complaint_tools

_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'prompts', 'cross_doc_system.md'
)
with open(_SYSTEM_PROMPT_PATH, encoding='utf-8') as _f:
    _SYSTEM_PROMPT_TEMPLATE = _f.read()

_model_with_tools = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0.2,
    google_api_key=os.getenv("GEMINI_API_KEY"),
).bind_tools(complaint_tools)


def cross_doc_agent_node(state: dict) -> dict:
    """Cross-doc agent: reasons across documents using KG edges and multi-doc search."""
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
        "agent_name": "cross_doc_agent",
    }
