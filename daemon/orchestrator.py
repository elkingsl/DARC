"""DARC coordinator core: state, request queue, routing, confirmation.

Events (small dicts, forwarded verbatim over the Unix socket):
  {"type": "state",   "state": "PROCESSING"}
  {"type": "status",  "text": "capturing your screen..."}
  {"type": "token",   "id": 1, "text": "..."}
  {"type": "done",    "id": 1, "text": "<full reply>"}
  {"type": "error",   "id": 1, "message": "..."}
  {"type": "confirm", "id": 1, "command": "...", "explanation": "...",
                      "warning": "..."|null, "terminal": bool, "timeout": 60}
"""
import asyncio
import inspect
import itertools
from enum import Enum
from typing import Callable

from daemon.router import Route
from daemon.tools.browser import BrowserAgent, BrowserError
from daemon.tools.screen_reader import ScreenError
from daemon.tools.system_cmd import Proposal
from daemon.tools.web import WebError
from models.llm import LLMError, OllamaLLM


class State(str, Enum):
    STARTING = "STARTING"
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    CONFIRMING = "CONFIRMING"
    EXECUTING = "EXECUTING"
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
        # optional capabilities, attached by main.py (None = feature off)
        self.router = None      # daemon.router.Router
        self.screen = None      # daemon.tools.screen_reader.ScreenReader
        self.commands = None    # daemon.tools.system_cmd.SystemCommands
        self.apps = None        # daemon.tools.apps.AppLauncher
        self.web = None         # daemon.tools.web.WebReader
        self.browser = None     # daemon.tools.browser.BrowserSession
        self.browser_steps = 8
        self.confirm_timeout = 60
        # async fn(spoken_text, source) awaited after a reply (used for TTS)
        self.reply_hook: Callable | None = None
        self._pending = None    # (req_id, future, confirm-event dict)

    # ---- events -------------------------------------------------------
    def subscribe(self, callback: Callable) -> None:
        """callback(event: dict) may be sync or async."""
        self._listeners.append(callback)

    async def _emit(self, event: dict) -> None:
        for cb in self._listeners:
            result = cb(event)
            if inspect.isawaitable(result):
                await result

    async def emit(self, event: dict) -> None:
        await self._emit(event)

    async def _set_state(self, state: State) -> None:
        if state != self.state:
            self.state = state
            await self._emit({"type": "state", "state": state.value})

    async def mark(self, state: State) -> None:
        """Lets other components (the voice pipeline) set the visible state."""
        await self._set_state(state)

    async def _status(self, text: str) -> None:
        await self._emit({"type": "status", "text": text})

    # ---- lifecycle ----------------------------------------------------
    async def start(self) -> None:
        await self.llm.check()  # fail fast with a helpful message
        self._worker = asyncio.create_task(self._run())
        await self._set_state(State.IDLE)

    async def stop(self) -> None:
        self.resolve_confirmation(self._pending[0], False) if self._pending else None
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    # ---- public API ---------------------------------------------------
    def submit(self, text: str, source: str = "text") -> int:
        """Queue a user request ("text" or "voice"); returns its id."""
        req_id = next(self._ids)
        self._queue.put_nowait((req_id, text, source))
        return req_id

    async def begin_listening(self) -> bool:
        """Hook for the audio pipeline (wake word / hotkey)."""
        if self.state != State.IDLE:
            return False
        await self._set_state(State.LISTENING)
        return True

    async def cancel_listening(self) -> None:
        if self.state == State.LISTENING:
            await self._set_state(State.IDLE)

    def clear_history(self) -> None:
        self.history.clear()

    async def join(self) -> None:
        """Wait until all queued requests are finished."""
        await self._queue.join()

    @property
    def pending_event(self) -> dict | None:
        """The confirmation currently awaiting an answer (for new clients)."""
        return self._pending[2] if self._pending else None

    def resolve_confirmation(self, req_id, approved: bool) -> bool:
        if self._pending and self._pending[0] == req_id \
                and not self._pending[1].done():
            self._pending[1].set_result(bool(approved))
            return True
        return False

    # ---- internals ----------------------------------------------------
    def _build_messages(self, user_text: str) -> list[dict]:
        msgs = [{"role": "system", "content": self.system_prompt}]
        msgs += self.history[-self.max_messages:]
        msgs.append({"role": "user", "content": user_text})
        return msgs

    async def _run(self) -> None:
        while True:
            req_id, text, source = await self._queue.get()
            try:
                await self._handle(req_id, text, source)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let one bad request kill the daemon
                await self._emit({"type": "error", "id": req_id,
                                  "message": str(e)})
            finally:
                self._queue.task_done()
                if self._queue.empty():
                    await self._set_state(State.IDLE)

    async def _handle(self, req_id: int, text: str, source: str) -> None:
        await self._set_state(State.PROCESSING)
        skip: set[str] = set()
        for _ in range(7):  # a route that declines is skipped; chat is the floor
            route = self.router.classify(text, skip) if self.router else Route()
            if await self._dispatch(req_id, text, source, route):
                return
            skip.add(route.kind)
        await self._chat(req_id, text, source)

    async def _dispatch(self, req_id, text, source, route: Route) -> bool:
        """True = handled. False = not applicable, try the next route."""
        k = route.kind
        if k == "vision" and self.screen:
            await self._vision(req_id, text, source, route)
            return True
        if k == "window" and self.apps:
            return await self._window(req_id, text, source, route)
        if k == "open" and (self.apps or self.web):
            return await self._open(req_id, text, source, route)
        if k == "web" and self.web:
            return await self._web(req_id, text, source, route)
        if k == "browser" and self.browser:
            return await self._browser(req_id, text, source, route)
        if k == "command" and self.commands:
            return await self._command(req_id, text, source)
        if k == "chat":
            await self._chat(req_id, text, source)
            return True
        return False

    # ---- shared reply plumbing --------------------------------------------
    async def _stream(self, req_id: int, chunks) -> str:
        """Forward an async iterator of text chunks as token events."""
        parts: list[str] = []
        async for chunk in chunks:
            if not parts:
                await self._set_state(State.RESPONDING)
            parts.append(chunk)
            await self._emit({"type": "token", "id": req_id, "text": chunk})
        return "".join(parts)

    async def _say(self, req_id: int, text: str) -> None:
        await self._set_state(State.RESPONDING)
        await self._emit({"type": "token", "id": req_id, "text": text})

    async def _error(self, req_id: int, message: str) -> None:
        await self._emit({"type": "error", "id": req_id, "message": message})

    async def _finish(self, req_id: int, user_text: str, reply: str,
                      source: str, spoken: str | None = None,
                      remember: str | None = None) -> None:
        self.history += [{"role": "user", "content": user_text},
                         {"role": "assistant", "content": remember or reply}]
        await self._emit({"type": "done", "id": req_id, "text": reply})
        if self.reply_hook:  # e.g. speak it; state stays RESPONDING meanwhile
            try:
                await self.reply_hook(spoken or reply, source)
            except Exception as e:
                await self._error(req_id, f"voice output: {e}")

    # ---- chat ---------------------------------------------------------------
    async def _chat(self, req_id: int, text: str, source: str) -> None:
        try:
            reply = await self._stream(
                req_id, self.llm.stream_chat(self._build_messages(text)))
        except LLMError as e:
            await self._error(req_id, str(e))
            return  # failed turns are not added to context
        await self._finish(req_id, text, reply, source)

    # ---- vision ---------------------------------------------------------------
    async def _vision(self, req_id: int, text: str, source: str, route: Route):
        try:
            await self._status("select a region..." if route.region
                               else "capturing your screen...")
            image = await self.screen.capture(region=route.region)
            await self._status("looking at it (can take a minute)...")
            if self.screen.refine:
                desc = "".join([c async for c in self.screen.describe(image, text)])
                await self._status("thinking...")
                reply = await self._stream(req_id, self.llm.stream_chat(
                    self.screen.refine_messages(self.system_prompt, text, desc)))
            else:
                reply = await self._stream(req_id, self.screen.describe(image, text))
        except (LLMError, ScreenError) as e:
            await self._error(req_id, str(e))
            return
        await self._finish(req_id, text, reply, source)

    # ---- system commands ----------------------------------------------------
    async def _confirm(self, req_id: int, proposal) -> bool:
        event = {"type": "confirm", "id": req_id, "command": proposal.command,
                 "explanation": proposal.explanation, "warning": proposal.warning,
                 "terminal": proposal.needs_terminal,
                 "kind": getattr(proposal, "kind", "shell"),
                 "timeout": self.confirm_timeout}
        fut = asyncio.get_running_loop().create_future()
        self._pending = (req_id, fut, event)
        await self._set_state(State.CONFIRMING)
        await self._emit(event)
        try:
            return await asyncio.wait_for(fut, self.confirm_timeout)
        except asyncio.TimeoutError:
            return False  # no answer = no
        finally:
            self._pending = None

    async def _command(self, req_id: int, text: str, source: str) -> bool:
        """Returns False when the request turned out not to be a shell task."""
        cmds = self.commands
        try:
            await self._status("working out the command...")
            proposal = await cmds.propose(text)
        except LLMError as e:
            await self._error(req_id, str(e))
            return True
        if proposal is None:
            return False

        if proposal.refused:
            reply = f"I won't run that ({proposal.refused}).\n$ {proposal.command}"
            await self._say(req_id, reply)
            await self._finish(req_id, text, reply, source,
                               spoken="I won't run that, it looks dangerous.")
            return True

        if not await self._confirm(req_id, proposal):
            reply = "Cancelled. Nothing was run."
            await self._say(req_id, reply)
            await self._finish(req_id, text, reply, source)
            return True

        await self._set_state(State.EXECUTING)
        if proposal.needs_terminal:
            reply = await cmds.run_in_terminal(proposal)
            await self._say(req_id, reply)
            await self._finish(req_id, text, reply, source,
                               remember=f"[started `{proposal.command}` in a terminal]")
            return True

        result = await cmds.run(proposal)
        shown = f"$ {proposal.command}\n{result.output or '(no output)'}"
        if result.timed_out:
            shown += f"\n(stopped after {int(cmds.timeout)}s)"
        elif result.returncode != 0:
            shown += f"\n(exit code {result.returncode})"
        await self._say(req_id, shown + "\n\n")

        summary = ""
        if cmds.interpret:
            try:
                await self._status("reading the output...")
                summary = await self._stream(req_id, self.llm.stream_chat(
                    cmds.interpret_messages(self.system_prompt, text, proposal, result)))
            except LLMError as e:
                await self._error(req_id, str(e))
        ok = result.returncode == 0 and not result.timed_out
        await self._finish(
            req_id, text, shown + ("\n\n" + summary if summary else ""), source,
            spoken=summary or ("Done." if ok else "The command failed."),
            remember=f"[ran `{proposal.command}`, exit {result.returncode}] {summary}")
        return True

    # ---- windows + opening apps/links -----------------------------------------
    async def _quick(self, req_id, text, source, reply, spoken=None):
        await self._say(req_id, reply)
        await self._finish(req_id, text, reply, source, spoken=spoken or reply)
        return True

    async def _window(self, req_id, text, source, route: Route) -> bool:
        data = route.data or {}
        if data.get("op") == "close":  # closing can lose unsaved work: ask first
            p = Proposal("hyprctl dispatch killactive", "Closes the focused window.",
                         warning="Unsaved work in that window may be lost.")
            if not await self._confirm(req_id, p):
                return await self._quick(req_id, text, source, "Cancelled. Nothing was closed.")
            await self._set_state(State.EXECUTING)
        return await self._quick(req_id, text, source, await self.apps.window(data))

    async def _open(self, req_id, text, source, route: Route) -> bool:
        data = route.data or {}
        try:
            if data.get("search_site"):
                url = self.web and self.web.site_search_url(data["search_site"], data["q"])
                if not url:
                    return False
                self.web.open_url(url)
                return await self._quick(req_id, text, source,
                                         f"Searching {data['search_site']} for {data['q']}.")
            target = route.arg
            url = self.web.resolve_link(target) if self.web else None
            if url:
                self.web.open_url(url)
                return await self._quick(req_id, text, source, f"Opening {url}.",
                                         spoken=f"Opening {target}.")
            app = self.apps.resolve(target) if self.apps else None
            if app:
                return await self._quick(req_id, text, source, await self.apps.launch(app))
        except WebError as e:
            await self._error(req_id, str(e))
            return True
        return False  # not a link or an installed app: let the next route try

    # ---- reading the web ------------------------------------------------------
    async def _web(self, req_id, text, source, route: Route) -> bool:
        try:
            if route.data and route.data.get("url"):
                await self._status("reading the page...")
                page = await self.web.fetch(route.data["url"])
                sources = [{"n": 1, "title": page["title"], "url": page["url"],
                            "text": page["text"][:3500]}]
                question = route.data["question"]
            else:
                await self._status("searching the web...")
                question = route.arg
                sources = await self.web.research(question)
            await self._status("reading what I found...")
            reply = await self._stream(req_id, self.llm.stream_chat(
                self.web.answer_messages(self.system_prompt, question, sources),
                options={"num_ctx": 4096}))
        except WebError as e:
            await self._error(req_id, str(e))
            return True
        except LLMError as e:
            await self._error(req_id, str(e))
            return True
        footer = self.web.sources_footer(sources)
        await self._say(req_id, footer)
        await self._finish(req_id, text, reply + footer, source, spoken=reply,
                           remember=reply)
        return True

    # ---- browser control ------------------------------------------------------
    async def _browser(self, req_id, text, source, route: Route) -> bool:
        data = route.data or {}
        op, b = data.get("op"), self.browser
        try:
            if op == "task":
                await self._status("starting the browser...")

                async def confirm(desc, why, warning):
                    ok = await self._confirm(req_id, Proposal(
                        desc, why, warning=warning, kind="browser"))
                    if ok:
                        await self._set_state(State.EXECUTING)
                    return ok

                agent = BrowserAgent(b, self.llm, self.browser_steps)
                reply = await agent.run(data["goal"], self._status, confirm)
            elif op == "new_tab":
                url = await b.new_tab(data.get("url"))
                reply = f"New tab: {url}" if data.get("url") else "Opened a new tab."
            elif op == "close_tab":
                reply = await b.close_tab()
            elif op in ("next_tab", "prev_tab"):
                reply = await b.switch_tab(1 if op == "next_tab" else -1)
            elif op == "back":
                reply = await b.back()
            elif op == "forward":
                reply = await b.forward()
            elif op == "reload":
                reply = await b.reload()
            elif op == "scroll":
                reply = await b.scroll(data.get("direction", "down"))
            elif op == "read":
                await self._status("reading the page...")
                page = await b.read()
                sources = [{"n": 1, "title": page["title"], "url": page["url"],
                            "text": page["text"][:3500]}]
                reply = await self._stream(req_id, self.llm.stream_chat(
                    self.web.answer_messages(self.system_prompt, text, sources)
                    if self.web else self.screen.refine_messages(
                        self.system_prompt, text, sources[0]["text"]),
                    options={"num_ctx": 4096}))
                await self._finish(req_id, text, reply, source)
                return True
            else:
                return False
        except (BrowserError, LLMError) as e:
            await self._error(req_id, str(e))
            return True
        except Exception as e:  # playwright errors: report, never crash the daemon
            await self._error(req_id, f"browser: {str(e).splitlines()[0][:200]}")
            return True
        return await self._quick(req_id, text, source, reply)
