"""
AI provider factory — returns the correct provider based on config.
"""
from app.config import settings
from app.ai.openai_provider import OpenAIProvider
from app.ai.claude_provider import ClaudeProvider


def get_ai_provider():
    """
    Returns an AI provider instance based on the AI_PROVIDER env var.
    """
    provider = settings.ai_provider.lower()

    if provider == "claude" or provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required when AI_PROVIDER=claude")
        return ClaudeProvider()

    elif provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when AI_PROVIDER=openai")
        return OpenAIProvider()

    else:
        raise ValueError(f"Unknown AI_PROVIDER: {provider}. Use 'openai' or 'claude'.")
