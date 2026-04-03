"""
OpenAI AI provider implementation.
Mirrors the tool-call loop from the lp-chatbot's api/chat.js.
"""
import json
from typing import List, Dict, Any, Optional
from openai import AsyncOpenAI

from app.config import settings
from app.models.schemas import AIChatResponse


class OpenAIProvider:
    """OpenAI chat completion provider with tool call support."""

    def __init__(self):
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)
        self.model = settings.openai_model

    async def chat_completion(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AIChatResponse:
        # Build the messages array with system prompt
        full_messages = [{"role": "system", "content": system_prompt}] + messages

        # First call
        kwargs = {
            "model": self.model,
            "messages": full_messages,
            "max_tokens": 1000,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = await self.client.chat.completions.create(**kwargs)
        assistant_msg = response.choices[0].message

        # Extract usage
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        # Check for tool calls
        tool_calls = []
        if assistant_msg.tool_calls:
            for tc in assistant_msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": args,
                })

        content = assistant_msg.content or ""

        return AIChatResponse(
            content=content,
            tool_calls=tool_calls,
            model=self.model,
            provider="openai",
            usage=usage,
        )

    async def chat_completion_with_tool_results(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        assistant_message: Dict[str, Any],
        tool_results: List[Dict[str, Any]],
    ) -> AIChatResponse:
        """
        Second-pass call after tool execution.
        Sends the original messages + assistant's tool call message + tool results.
        """
        full_messages = (
            [{"role": "system", "content": system_prompt}]
            + messages
            + [assistant_message]
            + tool_results
        )

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            max_tokens=1000,
        )

        assistant_msg = response.choices[0].message
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return AIChatResponse(
            content=assistant_msg.content or "",
            tool_calls=[],
            model=self.model,
            provider="openai",
            usage=usage,
        )
