"""
nodes/classify.py — Query classification node.

Reads the user's last message and classifies it into one of:
  complaint, contract, general, clarification

Uses a lightweight Gemini call — no tools, no retrieval.
"""

import os

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage

_CLASSIFY_PROMPT_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'prompts', 'classify_system.md'
)

with open(_CLASSIFY_PROMPT_PATH, encoding='utf-8') as _f:
    _CLASSIFY_SYSTEM_PROMPT = _f.read()

_VALID_TYPES = {"complaint", "contract", "general", "clarification"}

_model = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0.0,
    google_api_key=os.getenv("GEMINI_API_KEY"),
)


def classify(state: dict) -> dict:
    """Classify the user's query and decide which agent handles it."""
    last_message = state["messages"][-1].content

    messages = [
        SystemMessage(content=_CLASSIFY_SYSTEM_PROMPT),
        {"role": "user", "content": f"Classify this question: {last_message}"},
    ]

    try:
        response = _model.invoke(messages)
        query_type = response.content.strip().lower()
        # Sanitize — only accept valid types
        if query_type not in _VALID_TYPES:
            query_type = "general"
    except Exception:
        query_type = "general"

    return {"query_type": query_type}


def route_by_type(state: dict) -> str:
    """Conditional edge: map query_type → agent node name."""
    qt = state.get("query_type", "general")
    if qt == "complaint":
        return "complaint_agent"
    elif qt == "contract":
        return "complaint_agent"   # reuse complaint agent; contract agent is a future node
    else:
        return "complaint_agent"   # general + clarification also use complaint agent for now
