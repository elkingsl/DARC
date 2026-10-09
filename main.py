"""DARC entry point.

  python main.py            daemon + Dynamic Island UI
  python main.py --no-ui    daemon only (UI started separately / systemd)
  python main.py --no-voice skip the audio pipeline (wake word / STT / TTS)
  python main.py --cli      Phase-1 terminal chat (no socket, no UI)
"""
import argparse
import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import yaml

from daemon.orchestrator import Orchestrator
from daemon.router import Router
from daemon.server import SocketServer, default_socket_path
from daemon.tools.apps import AppLauncher
from daemon.tools.browser import BrowserSession
from daemon.tools.screen_reader import ScreenReader
from daemon.tools.web import WebReader
from daemon.tools.system_cmd import SystemCommands
from daemon.voice import VoicePipeline
from models.llm import LLMError, OllamaLLM

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"


def build_orchestrator(cfg: dict) -> Orchestrator:
    l, d = cfg["llm"], cfg["daemon"]
    llm = OllamaLLM(l["host"], l["model"],
                    keep_alive=l.get("keep_alive", "5m"),
                    options=l.get("options"),
                    timeout=l.get("timeout", 120), think=l.get("think"))
    return Orchestrator(llm, d["system_prompt"], d.get("max_history_turns", 6))


async def attach_tools(orch: Orchestrator, cfg: dict) -> list[str]:
    """Enable vision + command execution; each degrades to chat if unavailable."""
    notes: list[str] = []
    orch.router = Router()

    v = cfg.get("vision", {})
    if v.get("enabled", True):
        screen = ScreenReader(orch.llm, v.get("model", "moondream"),
                              v.get("keep_alive", "1m"), v.get("max_side", 1024),
                              v.get("refine", True))
        problems = screen.problems()
        try:
            await orch.llm.check(screen.model)
        except LLMError as e:
            problems.append(str(e))
        if problems:
            notes += [f"vision off: {p}" for p in problems]
        else:
            orch.screen = screen
            notes.append(f"vision ready ({screen.model}, "
                         f"{'Wayland: grim' if screen.wayland() else 'X11: maim'})")

    c = cfg.get("commands", {})
    if c.get("enabled", True):
        cmds = SystemCommands(orch.llm, c.get("terminal"), c.get("timeout_s", 30),
                              c.get("max_output_chars", 3000), c.get("interpret", True))
        problems = cmds.problems()
        if problems:
            notes += [f"commands off: {p}" for p in problems]
        else:
            orch.commands = cmds
            orch.confirm_timeout = c.get("confirm_timeout_s", 60)
            notes.append("commands ready (every command needs your confirmation)")

    a = cfg.get("apps", {})
    if a.get("enabled", True):
        orch.apps = AppLauncher(a.get("launcher", "hyprctl"),
                                cfg.get("commands", {}).get("terminal"))
        notes.append(f"apps ready ({len(orch.apps.apps())} installed apps found)")

    w = cfg.get("web", {})
    if w.get("enabled", True):
        orch.web = WebReader(w.get("timeout_s", 15), w.get("max_chars", 6000),
                             w.get("sites"), w.get("search_sites"), w.get("open_with"))
        notes.append("web ready (links, search, page reading)")

    br = cfg.get("browser", {})
    if br.get("enabled", True):
        session = BrowserSession(br.get("command") or br.get("chrome"),
                                 br.get("profile_dir"), br.get("headless", False),
                                 br.get("idle_close_s", 300), br.get("extra_args"))
        problems = session.problems()
        if problems:
            notes += [f"browser control off: {p}" for p in problems]
        else:
            orch.browser = session
            orch.browser_steps = br.get("max_steps", 8)
            notes.append(f"browser control ready ({session.kind()}, separate DARC profile)")
    return notes


def spawn_ui(cfg: dict, sock: str) -> subprocess.Popen:
    """The UI needs system PyGObject, so it runs on the system Python,
    not inside the uv venv."""
    py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    return subprocess.Popen(
        [py, str(ROOT / "ui" / "island.py"),
         "--position", cfg.get("ui", {}).get("position", "top-right"),
         "--socket", sock], env=env)


async def run_daemon(cfg: dict, with_ui: bool, with_voice: bool = True) -> None:
    orch = build_orchestrator(cfg)
    sock = default_socket_path()
    server = SocketServer(orch, sock)
    try:
        await orch.start()
        await server.start()
    except (LLMError, RuntimeError) as e:
        sys.exit(f"[startup failed] {e}")

    for note in await attach_tools(orch, cfg):
        print(f"[tools] {note}")

    voice = None
    if with_voice and cfg.get("audio", {}).get("enabled", True):
        voice = VoicePipeline(orch, cfg)
        for note in await voice.start():
            print(f"[voice] {note}")
        server.voice = voice

    ui = spawn_ui(cfg, sock) if with_ui else None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    print(f"DARC daemon up on {sock}  (Ctrl+C to stop)")
    await stop.wait()

    if ui and ui.poll() is None:
        ui.terminate()
    if voice:
        voice.stop()
    if orch.browser:
        await orch.browser.close()
    await server.stop()
    await orch.stop()


# ---- Phase 1 terminal chat, kept for debugging the LLM layer -----------
def printer(event: dict) -> None:
    t = event["type"]
    if t == "token":
        print(event["text"], end="", flush=True)
    elif t == "done":
        print("\n")
    elif t == "error":
        print(f"\n[error] {event['message']}\n")


async def run_cli(cfg: dict) -> None:
    darc = build_orchestrator(cfg)
    darc.subscribe(printer)
    try:
        await darc.start()
    except LLMError as e:
        sys.exit(f"[startup failed] {e}")
    print("DARC CLI. /clear resets context, /quit exits.\n")
    try:
        while True:
            try:
                line = (await asyncio.to_thread(input, "\nyou> ")).strip()
            except EOFError:
                break
            if line == "/quit":
                break
            if line == "/clear":
                darc.clear_history()
                print("context cleared")
            elif line:
                darc.submit(line)
                await darc.join()
    finally:
        await darc.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ui", action="store_true")
    ap.add_argument("--no-voice", action="store_true",
                    help="skip wake word / STT / TTS")
    ap.add_argument("--cli", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load(CONFIG.read_text())
    try:
        asyncio.run(run_cli(cfg) if args.cli
                    else run_daemon(cfg, with_ui=not args.no_ui,
                                    with_voice=not args.no_voice))
    except KeyboardInterrupt:
        pass
