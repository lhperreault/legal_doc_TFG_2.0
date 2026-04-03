"""
Case service — create and manage cases.
"""
from typing import Optional, List, Dict, Any
from app.services.supabase_client import get_supabase


async def create_case(
    client_id: str,
    practice_area: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new case. Returns the created row."""
    sb = get_supabase()
    data = {
        "client_id": client_id,
        "practice_area": practice_area,
        "case_status": "intake",
    }
    if title:
        data["title"] = title
    if description:
        data["description"] = description

    result = sb.table("chatbot_cases").insert(data).execute()
    return result.data[0]


async def get_active_cases(client_id: str) -> List[Dict[str, Any]]:
    """Get all non-closed/archived cases for a client."""
    sb = get_supabase()
    result = (
        sb.table("chatbot_cases")
        .select("*")
        .eq("client_id", client_id)
        .not_.in_("case_status", ["closed", "archived"])
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


async def update_case(case_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Update a case by ID."""
    sb = get_supabase()
    result = (
        sb.table("chatbot_cases")
        .update(updates)
        .eq("id", case_id)
        .execute()
    )
    return result.data[0] if result.data else {}
