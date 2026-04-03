"""
Client service — lookup, create, and update clients in Supabase.
"""
from typing import Optional, Dict, Any
from app.services.supabase_client import get_supabase


async def lookup_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Look up a client by email. Returns the client row or None."""
    sb = get_supabase()
    result = sb.table("chatbot_clients").select("*").eq("email", email).execute()
    if result.data and len(result.data) > 0:
        return result.data[0]
    return None


async def create_client(
    email: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    phone: Optional[str] = None,
    channel: str = "website",
) -> Dict[str, Any]:
    """Create a new client record. Returns the created row."""
    sb = get_supabase()
    data = {"email": email, "channel": channel}
    if first_name:
        data["first_name"] = first_name
    if last_name:
        data["last_name"] = last_name
    if phone:
        data["phone"] = phone

    result = sb.table("chatbot_clients").insert(data).execute()
    return result.data[0]


async def update_client(client_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Update a client record by ID."""
    sb = get_supabase()
    result = (
        sb.table("chatbot_clients")
        .update(updates)
        .eq("id", client_id)
        .execute()
    )
    return result.data[0] if result.data else {}


async def get_or_create_client(
    email: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    phone: Optional[str] = None,
    channel: str = "website",
) -> tuple[Dict[str, Any], bool]:
    """
    Look up client by email; create if not found.
    Returns (client_dict, is_new).
    """
    existing = await lookup_by_email(email)
    if existing:
        return existing, False
    new_client = await create_client(email, first_name, last_name, phone, channel)
    return new_client, True
