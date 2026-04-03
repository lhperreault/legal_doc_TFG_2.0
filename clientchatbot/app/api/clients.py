"""
Client lookup endpoint — used by the widget's pre-chat form.
"""
from fastapi import APIRouter, HTTPException

from app.models.schemas import ClientLookupRequest, ClientLookupResponse, ClientInfo
from app.services import client_service, case_service, conversation_service

router = APIRouter()


@router.post("/api/clients/lookup")
async def lookup_client(req: ClientLookupRequest) -> ClientLookupResponse:
    """
    Look up a client by email. If not found, creates a new client.
    Also creates a new conversation for this session.
    Returns client info, active cases, and a conversation_id.
    """
    try:
        # Get or create client
        client, is_new = await client_service.get_or_create_client(
            email=req.email,
            first_name=req.first_name,
            last_name=req.last_name,
            phone=req.phone,
            channel=req.channel,
        )

        # Get active cases (empty for new clients)
        active_cases = []
        if not is_new:
            active_cases = await case_service.get_active_cases(client["id"])

        # Create a new conversation for this session
        from app.config import settings
        conversation = await conversation_service.create_conversation(
            client_id=client["id"],
            practice_area=settings.default_practice_area,
            channel=req.channel,
        )

        client_info = ClientInfo(
            id=client["id"],
            first_name=client.get("first_name"),
            last_name=client.get("last_name"),
            email=client["email"],
            is_new=is_new,
        )

        return ClientLookupResponse(
            client=client_info,
            active_cases=active_cases,
            conversation_id=conversation["id"],
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Client lookup failed: {str(e)}")
