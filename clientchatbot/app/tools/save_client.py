"""
Tool: save_client_info — updates client record in Supabase.
"""
from typing import Dict, Any
from app.services import client_service


async def execute(client_id: str, args: Dict[str, Any]) -> Dict[str, str]:
    """Save or update client info gathered during conversation."""
    updates = {}

    if args.get("first_name"):
        updates["first_name"] = args["first_name"]
    if args.get("last_name"):
        updates["last_name"] = args["last_name"]
    if args.get("phone"):
        updates["phone"] = args["phone"]

    # Store extra info in metadata
    metadata_updates = {}
    if args.get("company_name"):
        metadata_updates["company_name"] = args["company_name"]
    if args.get("situation_summary"):
        metadata_updates["situation_summary"] = args["situation_summary"]
    if args.get("urgency"):
        metadata_updates["urgency"] = args["urgency"]

    if metadata_updates:
        # Merge with existing metadata
        existing = await client_service.lookup_by_email("")  # We'll use client_id
        from app.services.supabase_client import get_supabase
        sb = get_supabase()
        result = sb.table("chatbot_clients").select("metadata").eq("id", client_id).execute()
        existing_meta = result.data[0].get("metadata", {}) if result.data else {}
        existing_meta.update(metadata_updates)
        updates["metadata"] = existing_meta

    if updates:
        await client_service.update_client(client_id, updates)

    return {"status": "saved"}
