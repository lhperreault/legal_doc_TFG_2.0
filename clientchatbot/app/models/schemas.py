"""
Pydantic models for API requests and responses.
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from uuid import UUID


# ─── Chat ────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    client_email: str
    message: str
    practice_area: str = "immigration"


class ClientInfo(BaseModel):
    id: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: str
    is_new: bool = False


class ChatResponse(BaseModel):
    conversation_id: str
    reply: str
    suggestions: Optional[List[str]] = None
    requires_review: bool = False
    client: Optional[ClientInfo] = None


# ─── Client Lookup ───────────────────────────────────────────────────────────

class ClientLookupRequest(BaseModel):
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    channel: str = "website"


class ClientLookupResponse(BaseModel):
    client: ClientInfo
    active_cases: List[Dict[str, Any]] = []
    conversation_id: Optional[str] = None


# ─── AI Provider ─────────────────────────────────────────────────────────────

class AIChatResponse(BaseModel):
    content: str
    tool_calls: List[Dict[str, Any]] = []
    model: str
    provider: str
    usage: Dict[str, int] = {}
