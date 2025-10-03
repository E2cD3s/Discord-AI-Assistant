from __future__ import annotations

import asyncio
from collections import deque
from typing import Deque, Dict, List, Tuple

from ..config import ConversationConfig
from ..logging_utils import get_logger
from .ollama_client import OllamaClient

_LOGGER = get_logger(__name__)


class ConversationManager:
    """Maintains per-channel conversation history and interfaces with Ollama."""

    def __init__(self, config: ConversationConfig, client: OllamaClient) -> None:
        self._config = config
        self._client = client
        self._conversations: Dict[int, Deque[Tuple[str, str]]] = {}
        self._locks: Dict[int, asyncio.Lock] = {}

    def _get_history(self, channel_id: int) -> Deque[Tuple[str, str]]:
        if channel_id not in self._conversations:
            self._conversations[channel_id] = deque(maxlen=self._config.history_turns)
        return self._conversations[channel_id]

    def _get_lock(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._locks:
            self._locks[channel_id] = asyncio.Lock()
        return self._locks[channel_id]

    async def reset(self, channel_id: int) -> None:
        async with self._get_lock(channel_id):
            self._conversations.pop(channel_id, None)
            _LOGGER.debug("Conversation reset for channel %s", channel_id)

    async def generate_reply(self, channel_id: int, user_message: str) -> str:
        lock = self._get_lock(channel_id)
        async with lock:
            history = self._get_history(channel_id)
            history.append(("user", user_message))
            messages = self._build_messages(history)
            _LOGGER.debug("Sending conversation with %d messages", len(messages))
            reply = await self._client.generate(
                messages,
                max_tokens=self._config.max_tokens,
                temperature=self._config.temperature,
                top_p=self._config.top_p,
                presence_penalty=self._config.presence_penalty,
                frequency_penalty=self._config.frequency_penalty,
            )
            history.append(("assistant", reply))
            return reply

    def _build_messages(self, history: Deque[Tuple[str, str]]) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = []
        if self._config.system_prompt:
            messages.append({"role": "system", "content": self._config.system_prompt})
        for role, content in history:
            messages.append({"role": role, "content": content})
        return messages


__all__ = ["ConversationManager"]
