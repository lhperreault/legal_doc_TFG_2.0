"""
Tool definitions for the chatbot — in OpenAI format.
Claude provider converts these automatically.
"""

# All tools available to the AI
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_client_info",
            "description": (
                "Save or update client information to the database. Call this silently "
                "when the client provides their name, phone, or other personal details "
                "during the conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string", "description": "Client's first name"},
                    "last_name": {"type": "string", "description": "Client's last name"},
                    "phone": {"type": "string", "description": "Client's phone number"},
                    "company_name": {"type": "string", "description": "Client's company (for commercial litigation)"},
                    "situation_summary": {
                        "type": "string",
                        "description": "Brief summary of the client's situation or case",
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "urgent"],
                        "description": "How urgent is this matter",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flag_for_review",
            "description": (
                "Flag your response for lawyer review before sending to the client. "
                "Use this when the client asks for specific legal advice, case outcome "
                "predictions, or anything that requires an attorney's judgment. "
                "The client will receive a placeholder message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why this needs lawyer review",
                    },
                    "draft_response": {
                        "type": "string",
                        "description": "Your draft response for the lawyer to review/edit",
                    },
                },
                "required": ["reason", "draft_response"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_case",
            "description": (
                "Create a new case for the client when enough information has been gathered. "
                "Call this after understanding the client's situation during intake."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Brief title for the case (e.g., 'H-1B Visa Application', 'Contract Dispute with Acme Corp')",
                    },
                    "description": {
                        "type": "string",
                        "description": "Summary of the case details gathered so far",
                    },
                },
                "required": ["title"],
            },
        },
    },
]
