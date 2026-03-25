"""
nodes/general_agent.py — General-purpose agent node.

Handles case overviews, timelines, broad party/document summaries,
and any question that doesn't fit a specialized agent.
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
    os.path.dirname(__file__), '..', 'prompts', 'general_system.md'
)
with open(_SYSTEM_PROMPT_PATH, encoding='utf-8') as _f:
    _SYSTEM_PROMPT_TEMPLATE = _f.read()

_model_with_tools = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0.2,
    google_api_key=os.getenv("GEMINI_API_KEY"),
).bind_tools(complaint_tools)


def general_agent_node(state: dict) -> dict:
    """General agent: handles overviews, timelines, and cross-cutting case questions."""
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
        "agent_name": "general_agent",
    }
