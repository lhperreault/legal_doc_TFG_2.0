"""
schemas/response.py — Pydantic model for the structured agent response.

Every agent produces this shape. The frontend and persistence layer both
expect this structure regardless of which specialized agent generated it.
"""

from pydantic import BaseModel, Field


class ProvenanceLink(BaseModel):
    section_id: str
    document_id: str | None = None
    file_name: str
    document_type: str | None = None
    page_range: str | None = None
    quote_snippet: str | None = None     # first 200 chars of the relevant section


class AgentResponse(BaseModel):
    answer: str = Field(description="The agent's synthesized answer.")
    confidence: float = Field(ge=0.0, le=1.0, description="0.0–1.0 confidence score.")
    needs_review: bool = Field(
        description="True if confidence < 0.7 — flags for human review."
    )
    provenance_links: list[ProvenanceLink] = Field(
        default_factory=list,
        description="Document sections that ground the answer.",
    )
    reasoning_steps: list[str] = Field(
        default_factory=list,
        description="Ordered list of reasoning steps the agent took.",
    )
    agent_name: str = Field(description="Which agent generated this response.")
    query_type: str | None = None
