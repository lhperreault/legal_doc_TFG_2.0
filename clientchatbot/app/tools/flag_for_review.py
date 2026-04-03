"""
Tool: flag_for_review — marks a response for lawyer review.
"""
from typing import Dict, Any


async def execute(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flag the AI's draft response for lawyer review.
    Returns metadata that the chat endpoint uses to set requires_review=True.
    """
    return {
        "status": "flagged",
        "reason": args.get("reason", "Requires attorney review"),
        "draft_response": args.get("draft_response", ""),
        "placeholder": (
            "That's a great question. Let me check with one of our attorneys "
            "and get back to you with a proper answer. We want to make sure "
            "you get accurate guidance on this."
        ),
    }
