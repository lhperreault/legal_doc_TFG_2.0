"""
nodes/compact.py — Conversation compaction node.

When a conversation thread accumulates more than COMPACT_THRESHOLD messages,
this node calls a lightweight LLM to write a structured summary of what was
discussed so far, then replaces the old messages with:

    [SystemMessage("Prior conversation summary:\n{summary}"),
     HumanMessage(current user query)]

This prevents context window overflow in long sessions while preserving
the key facts (documents consulted, claims identified, strategic conclusions)
that the lawyer has established in prior turns.

The last 4 messages before the current query are kept verbatim for
conversational continuity.
"""

import os

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage, RemoveMessage

# Compact when the accumulated message history exceeds this count.
# At 5 messages per turn (1 user + 1 AI + up to 3 tool rounds), this
# fires after roughly 4 full turns, which is a safe conservative threshold.
COMPACT_THRESHOLD = 20

# Keep this many of the most-recent messages verbatim (besides the current query).
KEEP_RECENT = 4

_model = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0.0,
    google_api_key=os.getenv("GEMINI_API_KEY"),
)

_SUMMARY_PROMPT = """You are a legal case assistant summarizing a prior conversation for context continuity.

The conversation so far is between a lawyer and an AI legal analysis tool.
Write a concise, structured summary that preserves:
- Key facts and findings established
- Documents and sections that were cited (include file names if mentioned)
- Claims, parties, and obligations identified
- Any strategic conclusions reached
- Outstanding questions or areas flagged for review

Be factual and specific. Use bullet points. Do not include conversational filler.
Maximum 400 words.

PRIOR CONVERSATION:
{conversation_text}

SUMMARY:"""


def _message_to_text(msg) -> str:
    """Render a message to plain text for the summarization prompt."""
    if isinstance(msg, HumanMessage):
        return f"User: {msg.content}"
    if isinstance(msg, AIMessage):
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        return f"Assistant: {content[:600]}"
    if isinstance(msg, ToolMessage):
        return f"[Tool result]: {(msg.content or '')[:300]}"
    if isinstance(msg, SystemMessage):
        return f"[System]: {(msg.content or '')[:200]}"
    return ""


def needs_compaction(state: dict) -> bool:
    """Return True if the message history is long enough to compact."""
    messages = state.get("messages", [])
    return len(messages) >= COMPACT_THRESHOLD


def compact_messages(state: dict) -> dict:
    """Summarize old messages and replace with a compact summary.

    Uses RemoveMessage to delete stale messages and re-inserts them in the
    correct order: [SystemMessage(summary), ...recent..., current_query].

    Keeps the last KEEP_RECENT messages verbatim (before the current query)
    so there's no jarring discontinuity for the agent on the next turn.
    """
    messages = state.get("messages", [])

    if len(messages) < COMPACT_THRESHOLD:
        return {}  # nothing to do — LangGraph merges empty dicts cleanly

    # The last message is always the current HumanMessage query.
    current_query = messages[-1]

    # Split: messages to summarize vs. recent messages to keep verbatim
    to_keep_recent = messages[-(KEEP_RECENT + 1):-1]   # last N before current query
    to_summarize   = messages[:-(KEEP_RECENT + 1)]     # everything older

    if not to_summarize:
        return {}

    # Build conversation text for summarization
    conversation_text = "\n".join(
        _message_to_text(m) for m in to_summarize if _message_to_text(m)
    )

    # Generate summary
    try:
        response = _model.invoke([
            SystemMessage(content="You are a legal document assistant."),
            HumanMessage(content=_SUMMARY_PROMPT.format(
                conversation_text=conversation_text[:8000]
            )),
        ])
        summary_text = response.content.strip() if isinstance(response.content, str) else ""
    except Exception as e:
        # If summarization fails, just drop the old messages rather than crash
        summary_text = f"[Prior conversation summary unavailable due to error: {e}]"

    # Use RemoveMessage to delete ALL existing messages (LangGraph's add_messages
    # reducer processes RemoveMessage by ID before appending new messages).
    # We then re-add to_keep_recent and current_query in correct order so the
    # final state is: [SystemMessage(summary), *to_keep_recent, current_query].
    all_to_remove = to_summarize + to_keep_recent + [current_query]
    removals = [
        RemoveMessage(id=m.id)
        for m in all_to_remove
        if getattr(m, "id", None)
    ]

    summary_msg = SystemMessage(content=f"## Prior Conversation Summary\n\n{summary_text}")

    return {
        "messages": removals + [summary_msg, *to_keep_recent, current_query],
        "conversation_summary": summary_text,
    }
