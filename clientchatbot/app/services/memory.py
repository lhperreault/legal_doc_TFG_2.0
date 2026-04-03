"""
Conversation memory buffer — in-memory with periodic flush to Supabase.

Flush triggers:
1. Buffer hits FLUSH_THRESHOLD messages for a conversation
2. Timer fires after FLUSH_INTERVAL_SECONDS of inactivity
3. Server shutdown calls flush_all()
"""
import asyncio
from typing import Dict, List, Any
from app.config import settings


class ConversationBuffer:
    """In-memory message buffer with periodic Supabase flush."""

    def __init__(self):
        self._buffers: Dict[str, List[Dict[str, Any]]] = {}
        self._timers: Dict[str, asyncio.Task] = {}

    async def add_message(self, conversation_id: str, message: Dict[str, Any]) -> None:
        """Add a message to the buffer. Triggers flush if threshold reached."""
        if conversation_id not in self._buffers:
            self._buffers[conversation_id] = []

        self._buffers[conversation_id].append(message)

        # Reset the inactivity timer
        self._reset_timer(conversation_id)

        # Check threshold
        if len(self._buffers[conversation_id]) >= settings.flush_threshold:
            await self.flush(conversation_id)

    def get_buffered_messages(self, conversation_id: str) -> List[Dict[str, Any]]:
        """Get messages currently in the buffer (not yet flushed)."""
        return self._buffers.get(conversation_id, [])

    async def flush(self, conversation_id: str) -> None:
        """Flush buffered messages for a conversation to Supabase."""
        messages = self._buffers.pop(conversation_id, [])
        if not messages:
            return

        # Cancel any pending timer
        self._cancel_timer(conversation_id)

        try:
            from app.services.conversation_service import save_messages
            await save_messages(conversation_id, messages)
        except Exception as e:
            # Put messages back on failure
            print(f"[memory] Flush failed for {conversation_id}: {e}")
            if conversation_id not in self._buffers:
                self._buffers[conversation_id] = []
            self._buffers[conversation_id] = messages + self._buffers[conversation_id]

    async def flush_all(self) -> None:
        """Flush all buffered conversations. Called on shutdown."""
        conversation_ids = list(self._buffers.keys())
        for cid in conversation_ids:
            await self.flush(cid)

    def _reset_timer(self, conversation_id: str) -> None:
        """Reset the inactivity flush timer for a conversation."""
        self._cancel_timer(conversation_id)
        try:
            loop = asyncio.get_running_loop()
            self._timers[conversation_id] = loop.create_task(
                self._timer_flush(conversation_id)
            )
        except RuntimeError:
            # No running loop — skip timer (e.g., in tests)
            pass

    def _cancel_timer(self, conversation_id: str) -> None:
        """Cancel an existing timer for a conversation."""
        timer = self._timers.pop(conversation_id, None)
        if timer and not timer.done():
            timer.cancel()

    async def _timer_flush(self, conversation_id: str) -> None:
        """Wait for the flush interval, then flush."""
        try:
            await asyncio.sleep(settings.flush_interval_seconds)
            await self.flush(conversation_id)
        except asyncio.CancelledError:
            pass  # Timer was reset or cancelled


# Singleton buffer instance
conversation_buffer = ConversationBuffer()
