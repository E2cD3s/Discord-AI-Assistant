from __future__ import annotations

import asyncio
import codecs
import json
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp

from ..config import OllamaConfig
from ..logging_utils import get_logger

_LOGGER = get_logger(__name__)


class OllamaClient:
    """Async client for interacting with a local Ollama server."""

    def __init__(self, config: OllamaConfig) -> None:
        self._config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        timeout = aiohttp.ClientTimeout(total=self._config.request_timeout)
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        async with self._lock:
            if self._session and not self._session.closed:
                await self._session.close()

    async def ping(self) -> None:
        """Ensure the Ollama server is reachable before attempting inference."""

        session = await self._get_session()
        url = f"{self._config.host.rstrip('/')}/api/version"
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise RuntimeError(
                f"Unable to reach Ollama at {self._config.host}. Check that the service is running."
            ) from exc

    async def generate(
        self,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        stream: Optional[bool] = None,
    ) -> str:
        stream = self._config.stream if stream is None else stream
        if stream:
            chunks = []
            async for part in self.stream_generate(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                presence_penalty=presence_penalty,
                frequency_penalty=frequency_penalty,
            ):
                chunks.append(part)
            return "".join(chunks)

        payload = self._payload(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
            stream=False,
        )

        session = await self._get_session()
        url = f"{self._config.host.rstrip('/')}/api/chat"
        _LOGGER.debug("Sending Ollama request: %s", payload)
        async with session.post(url, json=payload) as response:
            response.raise_for_status()
            data = await response.json()
            return data.get("message", {}).get("content", "")

    async def stream_generate(
        self,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
    ) -> AsyncIterator[str]:
        payload = self._payload(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
            stream=True,
        )
        session = await self._get_session()
        url = f"{self._config.host.rstrip('/')}/api/chat"
        _LOGGER.debug("Streaming Ollama request: %s", payload)
        async with session.post(url, json=payload) as response:
            response.raise_for_status()
            decoder = codecs.getincrementaldecoder("utf-8")()
            text_buffer = ""
            done = False

            async for chunk_bytes in response.content:
                if not chunk_bytes:
                    continue

                text_buffer += decoder.decode(chunk_bytes)

                while "\n" in text_buffer:
                    line, text_buffer = text_buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        message_chunk = json.loads(line)
                    except json.JSONDecodeError:
                        # If decoding fails, prepend the data back to the buffer
                        text_buffer = f"{line}\n{text_buffer}" if text_buffer else line
                        break

                    done = message_chunk.get("done", False)
                    if not done:
                        content = message_chunk.get("message", {}).get("content")
                        if content:
                            yield content
                    if done:
                        break

                if done:
                    break

            if not done:
                # Process any remaining buffered data after the stream ends.
                text_buffer += decoder.decode(b"", final=True)
                for line in filter(None, (segment.strip() for segment in text_buffer.split("\n"))):
                    try:
                        message_chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if message_chunk.get("done", False):
                        break
                    content = message_chunk.get("message", {}).get("content")
                    if content:
                        yield content

    def _payload(
        self,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int],
        temperature: Optional[float],
        top_p: Optional[float],
        presence_penalty: Optional[float],
        frequency_penalty: Optional[float],
        stream: bool,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "stream": stream,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if presence_penalty is not None:
            payload["presence_penalty"] = presence_penalty
        if frequency_penalty is not None:
            payload["frequency_penalty"] = frequency_penalty
        if self._config.keep_alive is not None:
            payload["keep_alive"] = self._config.keep_alive
        return payload


__all__ = ["OllamaClient"]
