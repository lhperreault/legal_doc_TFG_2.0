"""
Conversation service — create conversations, load/save messages.
"""
from typing import Optional, List, Dict, Any
from app.services.supabase_client import get_supabase


async def create_conversation(
    client_id: str,
    practice_area: str,
    channel: str = "website",
    case_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new conversation. Returns the created row."""
    sb = get_supabase()
    data = {
        "client_id": client_id,
        "practice_area": practice_area,
        "channel": channel,
    }
    if case_id:
        data["case_id"] = case_id

    result = sb.table("chatbot_conversations").insert(data).execute()
    return result.data[0]


async def get_conversation(conversation_id: str) -> Optional[Dict[str, Any]]:
    """Get a conversation by ID."""
    sb = get_supabase()
    result = (
        sb.table("chatbot_conversations")
        .select("*")
        .eq("id", conversation_id)
        .execute()
    )
    return result.data[0] if result.data else None


async def get_recent_messages(
    conversation_id: str, limit: int = 20
) -> List[Dict[str, Any]]:
    """Get the most recent messages for a conversation from Supabase."""
    sb = get_supabase()
    result = (
        sb.table("chatbot_messages")
        .select("role, content, tool_name, tool_args, tool_result, created_at")
        .eq("conversation_id", conversation_id)
        .order("created_at", desc=False)
        .limit(limit)
        .execute()
    )
    return result.data or []


async def save_messages(
    conversation_id: str,
    messages: List[Dict[str, Any]],
) -> None:
    """Batch insert messages into Supabase."""
    if not messages:
        return
    sb = get_supabase()
    rows = []
    for msg in messages:
        row = {
            "conversation_id": conversation_id,
            "role": msg["role"],
            "content": msg.get("content", ""),
        }
        # Optional fields
        if msg.get("tool_name"):
            row["tool_name"] = msg["tool_name"]
        if msg.get("tool_args"):
            row["tool_args"] = msg["tool_args"]
        if msg.get("tool_result"):
            row["tool_result"] = msg["tool_result"]
        if msg.get("requires_review"):
            row["requires_review"] = True
            row["review_status"] = "pending_review"
        if msg.get("ai_provider"):
            row["ai_provider"] = msg["ai_provider"]
        if msg.get("ai_model"):
            row["ai_model"] = msg["ai_model"]
        if msg.get("token_count"):
            row["token_count"] = msg["token_count"]
        rows.append(row)

    sb.table("chatbot_messages").insert(rows).execute()


async def get_client_conversations(
    client_id: str, limit: int = 5
) -> List[Dict[str, Any]]:
    """Get recent conversations for a client."""
    sb = get_supabase()
    result = (
        sb.table("chatbot_conversations")
        .select("id, practice_area, channel, started_at, ended_at, case_id")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []
