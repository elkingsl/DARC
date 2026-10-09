"""Async streaming wrapper around the local Ollama chat API."""
import asyncio
import json
import threading
from typing import AsyncIterator

import requests


class LLMError(Exception):
    """Raised when Ollama is unreachable or returns an error."""


_DONE = object()


class ThinkStripper:
    """Drops <think>...</think> blocks from a token stream (tags may be split
    across chunks). Safety net for 'thinking' models such as Qwen3."""
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf, self.inside = "", False

    def feed(self, text: str) -> str:
        self.buf += text
        out: list[str] = []
        while True:
            if self.inside:
                i = self.buf.find(self.CLOSE)
                if i < 0:  # still thinking: keep only a possible partial tag
                    self.buf = self.buf[-(len(self.CLOSE) - 1):]
                    return "".join(out)
                self.buf = self.buf[i + len(self.CLOSE):].lstrip("\n")
                self.inside = False
            else:
                i = self.buf.find(self.OPEN)
                if i < 0:
                    hold = next((k for k in range(min(len(self.OPEN) - 1, len(self.buf)), 0, -1)
                                 if self.OPEN.startswith(self.buf[-k:])), 0)
                    out.append(self.buf[:len(self.buf) - hold])
                    self.buf = self.buf[len(self.buf) - hold:]
                    return "".join(out)
                out.append(self.buf[:i])
                self.buf = self.buf[i + len(self.OPEN):]
                self.inside = True

    def flush(self) -> str:
        rest = "" if self.inside else self.buf
        self.buf = ""
        return rest


class OllamaLLM:
    def __init__(self, host: str, model: str, keep_alive: str = "5m",
                 options: dict | None = None, timeout: float = 120,
                 think: bool | None = None):
        self.url = host.rstrip("/")
        self.model = model
        self.keep_alive = keep_alive
        self.options = options or {}
        self.timeout = timeout
        self.think = think  # False = ask thinking models not to think (faster)

    async def check(self, model: str | None = None) -> None:
        """Verify Ollama is up and the model has been pulled."""
        model = model or self.model

        def _check():
            try:
                r = requests.get(f"{self.url}/api/tags", timeout=5)
                r.raise_for_status()
            except requests.RequestException as e:
                raise LLMError(f"Ollama not reachable at {self.url} ({e}). "
                               "Is `systemctl status ollama` active?") from e
            names = {m["name"].lower() for m in r.json().get("models", [])}
            wanted = model.lower() if ":" in model else f"{model.lower()}:latest"
            if wanted not in names:
                raise LLMError(f"Model '{model}' not found. "
                               f"Run: ollama pull {model}")
        await asyncio.to_thread(_check)

    async def complete(self, messages: list[dict], **kw) -> str:
        """Non-streaming convenience: the whole reply as one string."""
        return "".join([c async for c in self.stream_chat(messages, **kw)])

    async def stream_chat(self, messages: list[dict], *, model: str | None = None,
                          keep_alive: str | None = None,
                          options: dict | None = None,
                          fmt: str | None = None) -> AsyncIterator[str]:
        """Yield response text chunks as Ollama generates them.
        model/keep_alive/options override the defaults for this call;
        fmt="json" asks Ollama to constrain output to valid JSON."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": keep_alive or self.keep_alive,
            "options": {**self.options, **(options or {})},
        }
        if fmt:
            payload["format"] = fmt
        if self.think is not None:
            payload["think"] = self.think

        def put(item):
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def worker():
            try:
                for attempt in (0, 1):
                    with requests.post(f"{self.url}/api/chat", json=payload,
                                       stream=True,
                                       timeout=(5, self.timeout)) as r:
                        if (r.status_code == 400 and "think" in payload
                                and attempt == 0 and "think" in r.text.lower()):
                            payload.pop("think")  # model has no thinking mode
                            continue
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
                        break
            except Exception as e:  # forwarded to the async side
                put(e)
            finally:
                put(_DONE)

        loop.run_in_executor(None, worker)
        strip = ThinkStripper()
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                if isinstance(item, Exception):
                    if isinstance(item, LLMError):
                        raise item
                    raise LLMError(str(item)) from item
                visible = strip.feed(item)
                if visible:
                    yield visible
            tail = strip.flush()
            if tail:
                yield tail
        finally:
            stop.set()  # stops the HTTP read if the consumer bails early
