"""DARC coordinator core: state, request queue, context, and LLM routing.

Everything the outside world needs to know is emitted as small event dicts,
so Phase 2 can forward them straight over the Unix socket to the UI:
  {"type": "state",  "state": "PROCESSING"}
  {"type": "token",  "id": 1, "text": "..."}
  {"type": "done",   "id": 1, "text": "<full reply>"}
  {"type": "error",  "id": 1, "message": "..."}
"""
import asyncio
import inspect
import itertools
from enum import Enum
from typing import Callable

from models.llm import LLMError, OllamaLLM


class State(str, Enum):
    STARTING = "STARTING"
    IDLE = "IDLE"
    PROCESSING = "PROCESSING"
    RESPONDING = "RESPONDING"


class Orchestrator:
    def __init__(self, llm: OllamaLLM, system_prompt: str,
                 max_history_turns: int = 6):
        self.llm = llm
        self.system_prompt = system_prompt.strip()
        self.max_messages = max_history_turns * 2
        self.history: list[dict] = []
        self.state = State.STARTING
        self._queue: asyncio.Queue = asyncio.Queue()
        self._ids = itertools.count(1)
        self._listeners: list[Callable] = []
        self._worker: asyncio.Task | None = None

    # ---- events -------------------------------------------------------
    def subscribe(self, callback: Callable) -> None:
        """callback(event: dict) may be sync or async."""
        self._listeners.append(callback)

    async def _emit(self, event: dict) -> None:
        for cb in self._listeners:
            result = cb(event)
            if inspect.isawaitable(result):
                await result

    async def _set_state(self, state: State) -> None:
        if state != self.state:
            self.state = state
            await self._emit({"type": "state", "state": state.value})

    # ---- lifecycle ----------------------------------------------------
    async def start(self) -> None:
        await self.llm.check()  # fail fast with a helpful message
        self._worker = asyncio.create_task(self._run())
        await self._set_state(State.IDLE)

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    # ---- public API ---------------------------------------------------
    def submit(self, text: str) -> int:
        """Queue a user request; returns its id."""
        req_id = next(self._ids)
        self._queue.put_nowait((req_id, text))
        return req_id

    def clear_history(self) -> None:
        self.history.clear()

    async def join(self) -> None:
        """Wait until all queued requests are finished."""
        await self._queue.join()

    # ---- internals ----------------------------------------------------
    def _build_messages(self, user_text: str) -> list[dict]:
        msgs = [{"role": "system", "content": self.system_prompt}]
        msgs += self.history[-self.max_messages:]
        msgs.append({"role": "user", "content": user_text})
        return msgs

    async def _run(self) -> None:
        while True:
            req_id, text = await self._queue.get()
            try:
                await self._handle(req_id, text)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let one bad request kill the daemon
                await self._emit({"type": "error", "id": req_id,
                                  "message": str(e)})
            finally:
                self._queue.task_done()
                if self._queue.empty():
                    await self._set_state(State.IDLE)

    async def _handle(self, req_id: int, text: str) -> None:
        await self._set_state(State.PROCESSING)
        # Phase 1: everything goes to the LLM. The router lands here later
        # (vision -> moondream, system action -> command tool, etc.).
        parts: list[str] = []
        try:
            async for chunk in self.llm.stream_chat(self._build_messages(text)):
                if not parts:
                    await self._set_state(State.RESPONDING)
                parts.append(chunk)
                await self._emit({"type": "token", "id": req_id, "text": chunk})
        except LLMError as e:
            await self._emit({"type": "error", "id": req_id, "message": str(e)})
            return  # failed turns are not added to context
        reply = "".join(parts)
        self.history += [{"role": "user", "content": text},
                         {"role": "assistant", "content": reply}]
        await self._emit({"type": "done", "id": req_id, "text": reply})
