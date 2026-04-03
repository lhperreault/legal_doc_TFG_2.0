"""
POST /api/chat — Main chat endpoint.
Handles the full conversation loop: client lookup, context building,
AI response, tool execution, and memory management.
"""
import json
from fastapi import APIRouter, HTTPException

from app.config import settings
from app.models.schemas import ChatRequest, ChatResponse, ClientInfo
from app.ai.factory import get_ai_provider
from app.prompts.base import build_system_prompt, get_suggestions
from app.tools.definitions import TOOLS
from app.services import client_service, conversation_service, case_service
from app.services.memory import conversation_buffer

router = APIRouter()


async def execute_tool(tool_name: str, args: dict, client_id: str, conversation_id: str) -> dict:
    """Execute a tool call and return the result."""

    if tool_name == "save_client_info":
        from app.tools.save_client import execute
        return await execute(client_id, args)

    elif tool_name == "flag_for_review":
        from app.tools.flag_for_review import execute
        return await execute(args)

    elif tool_name == "create_case":
        # Get the conversation to find the practice area
        convo = await conversation_service.get_conversation(conversation_id)
        practice_area = convo.get("practice_area", settings.default_practice_area) if convo else settings.default_practice_area
        case = await case_service.create_case(
            client_id=client_id,
            practice_area=practice_area,
            title=args.get("title"),
            description=args.get("description"),
        )
        # Link the conversation to this case
        from app.services.supabase_client import get_supabase
        sb = get_supabase()
        sb.table("chatbot_conversations").update({"case_id": case["id"]}).eq("id", conversation_id).execute()
        return {"status": "case_created", "case_id": case["id"]}

    else:
        return {"error": f"Unknown tool: {tool_name}"}


@router.post("/api/chat")
async def chat(req: ChatRequest) -> ChatResponse:
    """
    Main chat endpoint. Receives a message, returns an AI response.
    """
    try:
        # 1. Get or create client
        client, is_new = await client_service.get_or_create_client(
            email=req.client_email
        )
        client_id = client["id"]

        # 2. Get or validate conversation
        conversation_id = req.conversation_id
        if not conversation_id:
            convo = await conversation_service.create_conversation(
                client_id=client_id,
                practice_area=req.practice_area,
            )
            conversation_id = convo["id"]

        # 3. Load recent messages from Supabase + merge buffered messages
        db_messages = await conversation_service.get_recent_messages(conversation_id, limit=20)
        buffered = conversation_buffer.get_buffered_messages(conversation_id)

        # Build message history for AI
        history = []
        for msg in db_messages:
            history.append({"role": msg["role"], "content": msg["content"]})
        for msg in buffered:
            history.append({"role": msg["role"], "content": msg.get("content", "")})

        # Add the current user message
        history.append({"role": "user", "content": req.message})

        # 4. Get active cases for context
        active_cases = await case_service.get_active_cases(client_id) if not is_new else []

        # 5. Build system prompt
        system_prompt = build_system_prompt(
            practice_area=req.practice_area,
            client=client,
            is_new=is_new,
            active_cases=active_cases,
        )

        # 6. Call AI provider
        ai_provider = get_ai_provider()
        ai_response = await ai_provider.chat_completion(
            system_prompt=system_prompt,
            messages=history,
            tools=TOOLS,
        )

        # 7. Handle tool calls
        requires_review = False
        review_placeholder = None

        if ai_response.tool_calls:
            tool_results_for_ai = []

            for tc in ai_response.tool_calls:
                result = await execute_tool(
                    tool_name=tc["name"],
                    args=tc["arguments"],
                    client_id=client_id,
                    conversation_id=conversation_id,
                )

                # Check if this was a flag_for_review
                if tc["name"] == "flag_for_review" and result.get("status") == "flagged":
                    requires_review = True
                    review_placeholder = result.get("placeholder", "")

                tool_results_for_ai.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result),
                })

                # Save tool call as a message in buffer
                await conversation_buffer.add_message(conversation_id, {
                    "role": "tool",
                    "content": json.dumps(result),
                    "tool_name": tc["name"],
                    "tool_args": tc["arguments"],
                    "tool_result": result,
                })

            # Second AI call with tool results (unless flagged for review)
            if not requires_review:
                # Build the assistant message for the second call
                if ai_response.provider == "openai":
                    assistant_msg_for_second_call = {
                        "role": "assistant",
                        "content": ai_response.content or None,
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": json.dumps(tc["arguments"]),
                                },
                            }
                            for tc in ai_response.tool_calls
                        ],
                    }
                    ai_response = await ai_provider.chat_completion_with_tool_results(
                        system_prompt=system_prompt,
                        messages=history,
                        assistant_message=assistant_msg_for_second_call,
                        tool_results=tool_results_for_ai,
                    )
                elif ai_response.provider == "claude":
                    # For Claude, we need to reconstruct the content blocks
                    content_blocks = []
                    if ai_response.content:
                        content_blocks.append({"type": "text", "text": ai_response.content})
                    for tc in ai_response.tool_calls:
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["arguments"],
                        })
                    ai_response = await ai_provider.chat_completion_with_tool_results(
                        system_prompt=system_prompt,
                        messages=history,
                        assistant_content_blocks=content_blocks,
                        tool_results=[
                            {"id": tr["tool_call_id"], "content": tr["content"]}
                            for tr in tool_results_for_ai
                        ],
                    )

        # 8. Determine final reply
        if requires_review:
            reply_text = review_placeholder or (
                "Let me check with one of our attorneys on that. "
                "I'll get back to you shortly."
            )
        else:
            reply_text = ai_response.content or "I'm sorry, I couldn't generate a response. Please try again."

        # 9. Buffer the user message and assistant reply
        await conversation_buffer.add_message(conversation_id, {
            "role": "user",
            "content": req.message,
        })
        await conversation_buffer.add_message(conversation_id, {
            "role": "assistant",
            "content": reply_text,
            "requires_review": requires_review,
            "ai_provider": ai_response.provider,
            "ai_model": ai_response.model,
            "token_count": ai_response.usage.get("total_tokens"),
        })

        # 10. Get suggestions
        suggestions = get_suggestions(req.practice_area, is_new)
        # Only show suggestions on first message or when appropriate
        if len(history) > 3:
            suggestions = None  # Let the AI conversation flow naturally

        return ChatResponse(
            conversation_id=conversation_id,
            reply=reply_text,
            suggestions=suggestions,
            requires_review=requires_review,
            client=ClientInfo(
                id=client_id,
                first_name=client.get("first_name"),
                last_name=client.get("last_name"),
                email=client["email"],
                is_new=is_new,
            ),
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[chat] Error: {e}")
        raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")
