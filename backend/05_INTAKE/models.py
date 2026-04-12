"""
Data models for the intake funnel.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class NormalizedIntake:
    """Channel-agnostic representation of an incoming document."""
    firm_id: uuid.UUID
    file_name: str
    file_path: str
    source_channel: str                    # 'upload' | 'email' | 'gdrive' | 'dropbox' | 'cms_webhook'
    source_ref: Optional[str] = None       # external ID (email message-id, drive file id, etc.)
    source_metadata: dict = field(default_factory=dict)
    explicit_case_hint: Optional[str] = None   # from plus-address, API header, or UI pre-fill
    process_priority: str = "soon"         # 'immediate' | 'soon' | 'overnight' | 'manual'
    processing_mode: str = "balanced"      # 'accuracy' | 'balanced' | 'fast'
    file_hash: Optional[str] = None


@dataclass
class RoutingResult:
    """Output of the routing cascade."""
    suggested_case_id: Optional[uuid.UUID] = None
    suggested_corpus_id: Optional[uuid.UUID] = None
    confidence: float = 0.0
    method: str = "unresolved"             # 'metadata' | 'filename' | 'llm' | 'user'
    reasoning: str = ""
    candidates: list[dict] = field(default_factory=list)  # top-N suggestions

    @property
    def needs_user_confirmation(self) -> bool:
        return self.confidence < 0.85 or self.method == "unresolved"

    def to_json(self) -> dict:
        return {
            "suggested_case_id": str(self.suggested_case_id) if self.suggested_case_id else None,
            "suggested_corpus_id": str(self.suggested_corpus_id) if self.suggested_corpus_id else None,
            "confidence": self.confidence,
            "method": self.method,
            "reasoning": self.reasoning,
            "candidates": self.candidates,
            "needs_user_confirmation": self.needs_user_confirmation,
        }
