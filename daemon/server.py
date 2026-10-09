"""Unix-socket server: newline-delimited JSON between UIs and the orchestrator.

Client -> daemon:
  {"type":"submit","text":"..."}   queue a request
  {"type":"clear"}                 reset context
  {"type":"listen"} / {"type":"cancel"}   enter/leave LISTENING (audio hook)
  {"type":"confirm","id":N,"approved":true|false}   answer a command prompt
  {"type":"ping"}                  -> {"type":"pong"}
Daemon -> client:
  {"type":"hello","state":"IDLE"}  sent on connect, then every orchestrator
  event (state / token / done / error).
"""
import asyncio
import json
import os

MAX_TEXT = 4000


def default_socket_path() -> str:
    rt = os.environ.get("XDG_RUNTIME_DIR")
    return os.path.join(rt, "darc.sock") if rt else f"/tmp/darc-{os.getuid()}.sock"


class SocketServer:
    def __init__(self, orch, path: str | None = None):
        self.orch = orch
        self.path = path or default_socket_path()
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        self.voice = None  # set by main.py when the voice pipeline is up
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        await self._claim_path()
        self._server = await asyncio.start_unix_server(
            self._on_client, path=self.path, limit=2 ** 16)
        os.chmod(self.path, 0o600)
        self.orch.subscribe(self._broadcast)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for w in list(self._clients):
            w.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    async def _claim_path(self) -> None:
        """Remove a stale socket, but refuse to steal a live one."""
        if not os.path.exists(self.path):
            return
        try:
            _, w = await asyncio.open_unix_connection(self.path)
        except OSError:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
        else:
            w.close()
            raise RuntimeError(f"DARC daemon already running ({self.path})")

    # ---- clients ------------------------------------------------------
    @staticmethod
    def _encode(obj: dict) -> bytes:
        return (json.dumps(obj) + "\n").encode()

    async def _on_client(self, reader, writer) -> None:
        self._clients.add(writer)
        writer.write(self._encode({"type": "hello",
                                   "state": self.orch.state.value,
                                   "pending": self.orch.pending_event}))
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, ConnectionError):
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                await self._dispatch(msg, writer)
        finally:
            self._clients.discard(writer)
            writer.close()

    async def _dispatch(self, msg, writer) -> None:
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "submit":
            text = msg.get("text")
            if isinstance(text, str) and text.strip():
                self.orch.submit(text.strip()[:MAX_TEXT])
        elif kind == "clear":
            self.orch.clear_history()
        elif kind == "listen":
            if self.voice and self.voice.can_listen:
                task = asyncio.create_task(self.voice.start_session())
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:  # no audio: just show the LISTENING state (UI testing)
                await self.orch.begin_listening()
        elif kind == "cancel":
            if self.voice:
                await self.voice.cancel()
            else:
                await self.orch.cancel_listening()
        elif kind == "confirm":
            self.orch.resolve_confirmation(msg.get("id"), msg.get("approved") is True)
        elif kind == "ping":
            writer.write(self._encode({"type": "pong"}))

    def _broadcast(self, event: dict) -> None:
        data = self._encode(event)
        for w in list(self._clients):
            if w.is_closing():
                self._clients.discard(w)
            else:
                w.write(data)  # small local messages; no per-client drain
