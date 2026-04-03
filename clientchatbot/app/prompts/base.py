"""
Prompt builder — assembles the system prompt with client context and knowledge.
"""
from typing import Optional, Dict, Any, List


def build_system_prompt(
    practice_area: str,
    client: Optional[Dict[str, Any]] = None,
    is_new: bool = True,
    active_cases: Optional[List[Dict[str, Any]]] = None,
    knowledge_entries: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Build the full system prompt for a given practice area and client context.
    """
    # Get the template for this practice area
    if practice_area == "immigration":
        from app.prompts.immigration import SYSTEM_PROMPT_TEMPLATE
    elif practice_area == "commercial_litigation":
        from app.prompts.commercial_litigation import SYSTEM_PROMPT_TEMPLATE
    else:
        from app.prompts.immigration import SYSTEM_PROMPT_TEMPLATE

    # Build client context
    client_context = _build_client_context(client, is_new, active_cases)

    # Build knowledge context
    knowledge_context = _build_knowledge_context(knowledge_entries)

    return SYSTEM_PROMPT_TEMPLATE.format(
        client_context=client_context,
        knowledge_context=knowledge_context,
    )


def get_suggestions(practice_area: str, is_new: bool) -> List[str]:
    """Get the appropriate suggestion buttons for the practice area."""
    if practice_area == "immigration":
        from app.prompts.immigration import INTAKE_SUGGESTIONS, FOLLOWUP_SUGGESTIONS
    elif practice_area == "commercial_litigation":
        from app.prompts.commercial_litigation import INTAKE_SUGGESTIONS, FOLLOWUP_SUGGESTIONS
    else:
        from app.prompts.immigration import INTAKE_SUGGESTIONS, FOLLOWUP_SUGGESTIONS

    return INTAKE_SUGGESTIONS if is_new else FOLLOWUP_SUGGESTIONS


def _build_client_context(
    client: Optional[Dict[str, Any]],
    is_new: bool,
    active_cases: Optional[List[Dict[str, Any]]],
) -> str:
    if not client or is_new:
        return (
            "CLIENT STATUS: This is a NEW client visiting the website for the first time. "
            "You do not know anything about them yet. Do NOT assume any prior relationship. "
            "Start with a warm welcome and intake questions."
        )

    parts = [f"CLIENT STATUS: This is a RETURNING client."]

    name_parts = []
    if client.get("first_name"):
        name_parts.append(client["first_name"])
    if client.get("last_name"):
        name_parts.append(client["last_name"])
    if name_parts:
        parts.append(f"Name: {' '.join(name_parts)}")

    if client.get("email"):
        parts.append(f"Email: {client['email']}")
    if client.get("phone"):
        parts.append(f"Phone: {client['phone']}")

    if active_cases:
        parts.append(f"\nActive cases ({len(active_cases)}):")
        for case in active_cases[:3]:  # Limit to 3 most recent
            status = case.get("case_status", "unknown")
            title = case.get("title", "Untitled case")
            area = case.get("practice_area", "")
            parts.append(f"  - {title} ({area}) — Status: {status}")

    return "\n".join(parts)


def _build_knowledge_context(
    knowledge_entries: Optional[List[Dict[str, Any]]],
) -> str:
    if not knowledge_entries:
        return ""

    parts = ["\n--- KNOWLEDGE BASE ---"]
    for entry in knowledge_entries[:10]:  # Limit entries to avoid token bloat
        title = entry.get("title", "")
        content = entry.get("content", "")
        category = entry.get("category", "")
        if title and content:
            parts.append(f"\n[{category.upper()}] {title}:\n{content}")

    return "\n".join(parts)
