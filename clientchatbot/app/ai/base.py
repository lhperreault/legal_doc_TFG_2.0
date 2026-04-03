"""
AI Provider base protocol and shared types.
"""
from typing import Protocol, List, Dict, Any, Optional, runtime_checkable
from app.models.schemas import AIChatResponse


@runtime_checkable
class AIProvider(Protocol):
    """Protocol that all AI providers must implement."""

    async def chat_completion(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AIChatResponse:
        """
        Send a chat completion request to the AI provider.

        Args:
            system_prompt: The system instructions for the AI.
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool/function definitions.

        Returns:
            AIChatResponse with the AI's reply, any tool calls, and metadata.
        """
        ...
