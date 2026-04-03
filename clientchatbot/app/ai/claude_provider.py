"""
Anthropic Claude AI provider implementation.
"""
import json
from typing import List, Dict, Any, Optional
from anthropic import AsyncAnthropic

from app.config import settings
from app.models.schemas import AIChatResponse


class ClaudeProvider:
    """Anthropic Claude chat completion provider with tool call support."""

    def __init__(self):
        self.client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.model = settings.claude_model

    def _convert_tools_to_claude_format(
        self, tools: Optional[List[Dict[str, Any]]]
    ) -> Optional[List[Dict[str, Any]]]:
        """Convert OpenAI-style tool defs to Claude's format."""
        if not tools:
            return None
        claude_tools = []
        for tool in tools:
            func = tool.get("function", tool)
            claude_tools.append({
                "name": func["name"],
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            })
        return claude_tools

    def _convert_messages_for_claude(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Convert messages to Claude format.
        Claude doesn't accept 'system' role in messages — it goes in the system param.
        Tool messages need to be converted to tool_result content blocks.
        """
        claude_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            if role == "system":
                continue  # System goes in the system parameter
            elif role == "tool":
                # Convert to Claude's tool_result format
                claude_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", "unknown"),
                        "content": msg.get("content", ""),
                    }],
                })
            else:
                claude_messages.append({
                    "role": role,
                    "content": msg.get("content", ""),
                })
        return claude_messages

    async def chat_completion(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AIChatResponse:
        claude_messages = self._convert_messages_for_claude(messages)
        claude_tools = self._convert_tools_to_claude_format(tools)

        kwargs = {
            "model": self.model,
            "max_tokens": 1000,
            "system": system_prompt,
            "messages": claude_messages,
        }
        if claude_tools:
            kwargs["tools"] = claude_tools

        response = await self.client.messages.create(**kwargs)

        # Parse response content blocks
        content = ""
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input if isinstance(block.input, dict) else {},
                })

        usage = {
            "prompt_tokens": response.usage.input_tokens,
            "completion_tokens": response.usage.output_tokens,
            "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
        }

        return AIChatResponse(
            content=content,
            tool_calls=tool_calls,
            model=self.model,
            provider="claude",
            usage=usage,
        )

    async def chat_completion_with_tool_results(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        assistant_content_blocks: List[Dict[str, Any]],
        tool_results: List[Dict[str, Any]],
    ) -> AIChatResponse:
        """
        Second-pass call after tool execution for Claude.
        """
        claude_messages = self._convert_messages_for_claude(messages)

        # Add the assistant's response (with tool_use blocks)
        claude_messages.append({
            "role": "assistant",
            "content": assistant_content_blocks,
        })

        # Add tool results
        result_blocks = []
        for tr in tool_results:
            result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tr.get("tool_call_id", tr.get("id", "unknown")),
                "content": tr.get("content", ""),
            })
        claude_messages.append({"role": "user", "content": result_blocks})

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=1000,
            system=system_prompt,
            messages=claude_messages,
        )

        content = ""
        for block in response.content:
            if block.type == "text":
                content += block.text

        usage = {
            "prompt_tokens": response.usage.input_tokens,
            "completion_tokens": response.usage.output_tokens,
            "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
        }

        return AIChatResponse(
            content=content,
            tool_calls=[],
            model=self.model,
            provider="claude",
            usage=usage,
        )
