"""Async streaming wrapper around the local Ollama chat API."""
import asyncio
import json
import threading
from typing import AsyncIterator

import requests


class LLMError(Exception):
    """Raised when Ollama is unreachable or returns an error."""


_DONE = object()


class OllamaLLM:
    def __init__(self, host: str, model: str, keep_alive: str = "5m",
                 options: dict | None = None, timeout: float = 120):
        self.url = host.rstrip("/")
        self.model = model
        self.keep_alive = keep_alive
        self.options = options or {}
        self.timeout = timeout

    async def check(self) -> None:
        """Verify Ollama is up and the model has been pulled."""
        def _check():
            try:
                r = requests.get(f"{self.url}/api/tags", timeout=5)
                r.raise_for_status()
            except requests.RequestException as e:
                raise LLMError(f"Ollama not reachable at {self.url} ({e}). "
                               "Is `systemctl status ollama` active?") from e
            names = {m["name"] for m in r.json().get("models", [])}
            wanted = self.model if ":" in self.model else f"{self.model}:latest"
            if wanted not in names:
                raise LLMError(f"Model '{self.model}' not found. "
                               f"Run: ollama pull {self.model}")
        await asyncio.to_thread(_check)

    async def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        """Yield response text chunks as Ollama generates them."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": self.options,
        }

        def put(item):
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def worker():
            try:
                with requests.post(f"{self.url}/api/chat", json=payload,
                                   stream=True,
                                   timeout=(5, self.timeout)) as r:
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if stop.is_set():
                            break
                        if not line:
                            continue
                        data = json.loads(line)
                        if "error" in data:
                            raise LLMError(data["error"])
                        chunk = data.get("message", {}).get("content", "")
                        if chunk:
                            put(chunk)
                        if data.get("done"):
                            break
            except Exception as e:  # forwarded to the async side
                put(e)
            finally:
                put(_DONE)

        loop.run_in_executor(None, worker)
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                if isinstance(item, Exception):
                    if isinstance(item, LLMError):
                        raise item
                    raise LLMError(str(item)) from item
                yield item
        finally:
            stop.set()  # stops the HTTP read if the consumer bails early
